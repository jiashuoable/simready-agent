"""
VLM-based physics inference for SimReady assets.

Pipeline 位置：在 validate_and_repair 完成后、stage_public_output 之前。

输入:
    - image_path        用户原图（用作 VLM 输入）
    - usd_entry         repaired USD 入口（用来读 bbox + 写 attribute）
    - bbox_size         几何包围盒 (x,y,z)，来自 simready report

VLM 推理项（v1 范围）:
    - dimensions_m       用户真实世界中物体的近似尺寸 (米)
    - friction_static    静摩擦系数
    - friction_dynamic   动摩擦系数
    - material_hint      自由文本（wood / metal / plastic ...）
    - confidence         整体置信度
    - notes              一句自然语言说明

写回 USD:
    - 顶层 mesh prim 应用 UsdPhysics.MaterialAPI：staticFriction / dynamicFriction
    - 顶层 mesh prim 加 customAttribute：vlm:dimensions_m, vlm:material_hint,
      vlm:confidence, vlm:source

降级策略:
    VLM 失败或 ARK_API_KEY 缺失时返回 None（caller 跳过该 stage，不阻断主流程）。
"""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any

DEFAULT_MODEL = os.getenv("VLM_MODEL", "doubao-1-5-vision-pro-250328")
DEFAULT_TIMEOUT_S = float(os.getenv("VLM_TIMEOUT_S", "30"))


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass
class PhysicsHints:
    dimensions_m: list[float]       # [x, y, z] 真实世界尺寸 (m)
    friction_static: float
    friction_dynamic: float
    material_hint: str               # wood / metal / plastic / fabric / glass / ceramic / rubber / other
    confidence: float
    notes: str
    source: str = "vlm"              # "vlm" | "default"
    model: str = ""
    elapsed_s: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# 静态默认值（VLM 失败时兜底，PRD §1.3 异常场景）
DEFAULT_HINTS = PhysicsHints(
    dimensions_m=[0.3, 0.3, 0.3],
    friction_static=0.5,
    friction_dynamic=0.4,
    material_hint="other",
    confidence=0.0,
    notes="VLM 未启用或失败，使用静态默认值",
    source="default",
    model="",
    elapsed_s=0.0,
)


# ---------------------------------------------------------------------------
# Prompt
# ---------------------------------------------------------------------------
_VLM_SYSTEM_PROMPT = """你是一个物理仿真助手。看到一张物体的参考图后，要推理这个物体在现实世界中的：
1. 真实尺寸（米），按 [长, 宽, 高] 三轴给出，量级要贴合常识（一把椅子 ~0.5×0.5×0.9，一台冰箱 ~0.7×0.7×1.8）
2. 表面静摩擦系数 (0.0-1.5)
3. 表面动摩擦系数 (0.0-1.5，通常略小于静摩擦)
4. 主要材质：wood / metal / plastic / glass / fabric / ceramic / rubber / other
5. 整体推理置信度 (0.0-1.0)

输出严格 JSON，**不要任何其它解释或 markdown 包裹**：
{
  "dimensions_m": [float, float, float],
  "friction_static": float,
  "friction_dynamic": float,
  "material_hint": "wood|metal|plastic|glass|fabric|ceramic|rubber|other",
  "confidence": float,
  "notes": "一句话中文说明"
}"""


def _build_user_message(bbox_size: list[float] | None) -> str:
    parts = ["请按上述要求分析这张物体图片。"]
    if bbox_size:
        parts.append(
            f"\n参考：几何包围盒（USD 单位）为 {bbox_size[0]:.3f} × {bbox_size[1]:.3f} × {bbox_size[2]:.3f}，"
            "可用于核对比例（但你给出的 dimensions_m 是真实世界米数，不是 USD 单位）。"
        )
    return "".join(parts)


# ---------------------------------------------------------------------------
# VLM 调用
# ---------------------------------------------------------------------------
def _encode_image(image_path: str) -> str | None:
    try:
        with open(image_path, "rb") as f:
            data = f.read()
        ext = Path(image_path).suffix.lower().lstrip(".") or "jpeg"
        if ext == "jpg":
            ext = "jpeg"
        b64 = base64.b64encode(data).decode("utf-8")
        return f"data:image/{ext};base64,{b64}"
    except Exception as e:
        print(f"[vlm] failed to encode image {image_path}: {e}")
        return None


def _call_ark_vlm(image_path: str, bbox_size: list[float] | None,
                  model: str, timeout_s: float) -> dict | None:
    """调用火山方舟 VLM，返回解析后的 JSON dict 或 None。"""
    api_key = os.getenv("ARK_API_KEY")
    if not api_key:
        print("[vlm] ARK_API_KEY missing, skipping VLM")
        return None

    image_data_url = _encode_image(image_path)
    if not image_data_url:
        return None

    try:
        from volcenginesdkarkruntime import Ark
    except ImportError:
        print("[vlm] volcenginesdkarkruntime not installed")
        return None

    client = Ark(api_key=api_key, timeout=timeout_s)
    user_text = _build_user_message(bbox_size)

    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": _VLM_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": [
                        {"type": "image_url", "image_url": {"url": image_data_url}},
                        {"type": "text", "text": user_text},
                    ],
                },
            ],
            temperature=0.2,
        )
    except Exception as e:
        print(f"[vlm] ARK call failed: {e}")
        return None

    try:
        text = resp.choices[0].message.content.strip()
    except Exception as e:
        print(f"[vlm] unexpected response shape: {e}")
        return None

    # 模型可能包了 ```json ... ```，剥掉
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:].lstrip()

    try:
        return json.loads(text)
    except Exception as e:
        print(f"[vlm] failed to parse JSON: {e}; raw={text[:200]}")
        return None


# ---------------------------------------------------------------------------
# 校验 & 兜底
# ---------------------------------------------------------------------------
def _coerce_hints(raw: dict, model_id: str, elapsed_s: float) -> PhysicsHints:
    def _f(name: str, lo: float, hi: float, default: float) -> float:
        try:
            v = float(raw.get(name, default))
        except (TypeError, ValueError):
            v = default
        return max(lo, min(hi, v))

    dims = raw.get("dimensions_m") or [0.3, 0.3, 0.3]
    if not isinstance(dims, list) or len(dims) != 3:
        dims = [0.3, 0.3, 0.3]
    try:
        dims = [max(0.005, min(20.0, float(d))) for d in dims]
    except (TypeError, ValueError):
        dims = [0.3, 0.3, 0.3]

    material = str(raw.get("material_hint") or "other").lower()
    if material not in {"wood", "metal", "plastic", "glass", "fabric", "ceramic", "rubber", "other"}:
        material = "other"

    notes = str(raw.get("notes") or "")[:200]

    return PhysicsHints(
        dimensions_m=dims,
        friction_static=_f("friction_static", 0.0, 1.5, 0.5),
        friction_dynamic=_f("friction_dynamic", 0.0, 1.5, 0.4),
        material_hint=material,
        confidence=_f("confidence", 0.0, 1.0, 0.5),
        notes=notes,
        source="vlm",
        model=model_id,
        elapsed_s=elapsed_s,
    )


def infer_physics(
    image_path: str,
    bbox_size: list[float] | None = None,
    model: str = DEFAULT_MODEL,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> PhysicsHints:
    """对外主入口。返回 PhysicsHints；VLM 失败时返回 DEFAULT_HINTS（source=default）。"""
    t0 = time.time()
    raw = _call_ark_vlm(image_path, bbox_size, model=model, timeout_s=timeout_s)
    if raw is None:
        h = PhysicsHints(**DEFAULT_HINTS.to_dict())
        h.elapsed_s = time.time() - t0
        return h
    return _coerce_hints(raw, model_id=model, elapsed_s=time.time() - t0)


# ---------------------------------------------------------------------------
# 写回 USD
# ---------------------------------------------------------------------------
def write_to_usd(usd_path: str, hints: PhysicsHints) -> dict:
    """把 hints 写到 USD：UsdPhysics.MaterialAPI 摩擦 + customAttribute 尺寸/材质。

    返回 {"ok": bool, "error": str|None, "applied_prim": str|None}.
    USD Python (pxr) 必须可用；不可用则返回 ok=False 但不抛异常。
    """
    try:
        from pxr import Usd, UsdGeom, UsdPhysics, Sdf
    except ImportError as e:
        return {"ok": False, "error": f"pxr not available: {e}", "applied_prim": None}

    stage = Usd.Stage.Open(usd_path)
    if not stage:
        return {"ok": False, "error": f"failed to open {usd_path}", "applied_prim": None}

    # 找一个 mesh prim 作为应用目标。优先级：
    # 1) defaultPrim 如果是 Mesh
    # 2) stage 第一个 UsdGeom.Mesh
    target_prim = None
    default = stage.GetDefaultPrim()
    if default and default.IsA(UsdGeom.Mesh):
        target_prim = default
    if target_prim is None:
        for prim in stage.Traverse():
            if prim.IsA(UsdGeom.Mesh):
                target_prim = prim
                break

    # 没 mesh 时也写到 defaultPrim，保证 hints 能落进 USD 即可
    if target_prim is None and default:
        target_prim = default
    if target_prim is None:
        return {"ok": False, "error": "no usable prim found", "applied_prim": None}

    # --- 摩擦：UsdPhysics.MaterialAPI ---
    try:
        material_api = UsdPhysics.MaterialAPI.Apply(target_prim)
        material_api.CreateStaticFrictionAttr(hints.friction_static, writeSparsely=False)
        material_api.CreateDynamicFrictionAttr(hints.friction_dynamic, writeSparsely=False)
        material_api.CreateRestitutionAttr(0.0, writeSparsely=False)
    except Exception as e:
        return {"ok": False, "error": f"failed to apply MaterialAPI: {e}",
                "applied_prim": target_prim.GetPath().pathString}

    # --- 尺寸 / 材质 / 元信息：customAttribute ---
    try:
        dims_attr = target_prim.CreateAttribute(
            "vlm:dimensions_m", Sdf.ValueTypeNames.Float3, custom=True
        )
        dims_attr.Set(tuple(hints.dimensions_m))

        material_attr = target_prim.CreateAttribute(
            "vlm:material_hint", Sdf.ValueTypeNames.String, custom=True
        )
        material_attr.Set(hints.material_hint)

        conf_attr = target_prim.CreateAttribute(
            "vlm:confidence", Sdf.ValueTypeNames.Float, custom=True
        )
        conf_attr.Set(hints.confidence)

        src_attr = target_prim.CreateAttribute(
            "vlm:source", Sdf.ValueTypeNames.String, custom=True
        )
        src_attr.Set(hints.source)

        if hints.notes:
            notes_attr = target_prim.CreateAttribute(
                "vlm:notes", Sdf.ValueTypeNames.String, custom=True
            )
            notes_attr.Set(hints.notes)
    except Exception as e:
        return {"ok": False, "error": f"failed to write custom attrs: {e}",
                "applied_prim": target_prim.GetPath().pathString}

    try:
        stage.GetRootLayer().Save()
    except Exception as e:
        return {"ok": False, "error": f"failed to save layer: {e}",
                "applied_prim": target_prim.GetPath().pathString}

    return {"ok": True, "error": None, "applied_prim": target_prim.GetPath().pathString}


# ---------------------------------------------------------------------------
# 报告文本（供 pipeline 推送给用户）
# ---------------------------------------------------------------------------
def format_hints_text(hints: PhysicsHints, apply_result: dict | None = None) -> str:
    dims = hints.dimensions_m
    src = "VLM" if hints.source == "vlm" else "默认值"
    head = f"🧠 物理属性推理（{src}, confidence {hints.confidence:.2f}）"
    body = [
        head,
        f"   ├─ 估算尺寸：{dims[0]:.2f} × {dims[1]:.2f} × {dims[2]:.2f} m",
        f"   ├─ 材质：{hints.material_hint}",
        f"   ├─ 静摩擦：{hints.friction_static:.2f}",
        f"   └─ 动摩擦：{hints.friction_dynamic:.2f}",
    ]
    if hints.notes:
        body.append(f"📝 {hints.notes}")
    if apply_result is not None:
        if apply_result.get("ok"):
            body.append(f"ℹ️ 已写入 USD physics 属性（{apply_result.get('applied_prim')}）。")
        else:
            body.append(f"⚠️ USD 写入失败：{apply_result.get('error')}")
    return "\n".join(body)
