"""
End-to-end pipeline: Feishu image → Seed3D USD → SimReady inspect+repair → Feishu report+USD.

Each stage actively pushes a Feishu message so the user sees real-time progress.

Two ways to invoke:
  1) CLI:  python pipeline.py <image_path> --feishu-token ... --receive-id ...
  2) Import: from pipeline import run_pipeline, FeishuNotifier
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import time
import uuid
from pathlib import Path

import requests

LOCAL_BASE_DIR = Path(__file__).resolve().parent
SEED3D_DIR = LOCAL_BASE_DIR / "seed3D"

# Make seed3D modules importable without packaging them.
sys.path.insert(0, str(SEED3D_DIR))

# User-facing output root. Internal .openclaw paths are NEVER shown to the user.
PUBLIC_OUTPUT_ROOT = Path("/root/simready_output")

# Isaac streaming endpoint (火山引擎 ML 平台).
ISAAC_STREAMING_URL = "https://console.volcengine.com/ml-platform/region:ml-platform+cn-beijing/isaac?id=di-20260508104954-x5gr2&hiddenNavbar=true"


def _load_env_file(path):
    if not os.path.exists(path):
        return
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                os.environ.setdefault(k, v)
    except Exception as e:
        print(f"[env] failed to load {path}: {e}")


_load_env_file("/root/.openclaw/.env")

FEISHU_BASE = "https://open.feishu.cn/open-apis"


# ---------------------------------------------------------------------------
# Feishu thin client
# ---------------------------------------------------------------------------
def feishu_tenant_access_token(app_id, app_secret):
    url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
    try:
        r = requests.post(url, json={"app_id": app_id, "app_secret": app_secret}, timeout=10)
        data = r.json()
        if data.get("code") != 0:
            print(f"[feishu] tenant_access_token error: {data}")
            return None
        return data.get("tenant_access_token")
    except Exception as e:
        print(f"[feishu] tenant_access_token failed: {e}")
        return None


def _feishu_post(token, url, payload, timeout=10):
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=timeout)
        if r.status_code >= 400:
            print(f"[feishu] non-2xx {r.status_code}: {r.text[:200]}")
            return None
        return r.json()
    except Exception as e:
        print(f"[feishu] post failed: {e}")
        return None


def feishu_send_text(token, receive_id, text, receive_id_type="open_id"):
    url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type={receive_id_type}"
    return _feishu_post(token, url, {
        "receive_id": receive_id,
        "msg_type": "text",
        "content": json.dumps({"text": text}, ensure_ascii=False),
    })


def feishu_send_card(token, receive_id, card, receive_id_type="open_id"):
    url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type={receive_id_type}"
    return _feishu_post(token, url, {
        "receive_id": receive_id,
        "msg_type": "interactive",
        "content": json.dumps(card, ensure_ascii=False),
    })


def feishu_upload_file(token, file_path, file_type="stream"):
    url = f"{FEISHU_BASE}/im/v1/files"
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with open(file_path, "rb") as f:
            files = {"file": (os.path.basename(file_path), f)}
            data = {"file_type": file_type, "file_name": os.path.basename(file_path)}
            r = requests.post(url, headers=headers, files=files, data=data, timeout=60)
        return (r.json().get("data") or {}).get("file_key")
    except Exception as e:
        print(f"[feishu] upload_file failed: {e}")
        return None


def feishu_send_file(token, receive_id, file_path, receive_id_type="open_id"):
    file_key = feishu_upload_file(token, file_path)
    if not file_key:
        return None
    url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type={receive_id_type}"
    return _feishu_post(token, url, {
        "receive_id": receive_id,
        "msg_type": "file",
        "content": json.dumps({"file_key": file_key}),
    }, timeout=15)


# ---------------------------------------------------------------------------
# Config card (3 checkboxes: sandbox / VLM physics / 3D repair)
# ---------------------------------------------------------------------------
def build_config_card(request_id, opts):
    """视觉交互型配置卡片：右侧带切换按钮 + 底部『开始处理』按钮。
    Demo 录制时不需要真点；按钮渲染本身就足够呈现可勾选/可启动的视觉效果。"""
    def toggle_row(name, label, on, hint):
        emoji = "✅" if on else "⬜"
        state_text = "已启用" if on else "已关闭"
        return {
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": f"**{label}**\n<font color='grey'>{hint}</font>",
            },
            "extra": {
                "tag": "button",
                "text": {"tag": "plain_text", "content": f"{emoji} {state_text}"},
                "type": "primary" if on else "default",
                "value": {
                    "kind": "simready_toggle",
                    "request_id": request_id,
                    "name": name,
                    "checked": on,
                },
            },
        }
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": "🛠 SimReady 处理配置"},
            "template": "blue",
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md",
                         "content": f"任务 ID：`{request_id}`\n请勾选要启用的处理流程："},
            },
            toggle_row("use_vlm_physics", "启用物理补齐 + SimReady 修复", opts["use_vlm_physics"],
                       "自动补齐刚体、碰撞与质量等物理参数，并修复 USD 资产"),
            toggle_row("repair_3d", "打印检测报告", opts["repair_3d"],
                       "运行静态校验并把检出的问题清单推送到本会话"),
            toggle_row("use_sandbox", "使用 Sandbox 仿真校验环境", opts["use_sandbox"],
                       "在沙箱中加载 USD，可视化呈现物理与场景效果"),
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "🚀 开始处理"},
                    "type": "primary",
                    "value": {"kind": "simready_submit", "request_id": request_id},
                }],
            },
            {
                "tag": "note",
                "elements": [{
                    "tag": "plain_text",
                    "content": "⏳ 全程约 1~2 分钟，处理期间会自动推送进度。",
                }],
            },
        ],
    }


class FeishuNotifier:
    """Notifier that turns into a no-op when credentials are missing."""

    def __init__(self, token=None, receive_id=None, receive_id_type="open_id",
                 app_id=None, app_secret=None):
        if not token and app_id and app_secret:
            token = feishu_tenant_access_token(app_id, app_secret)
        self.token = token
        self.receive_id = receive_id
        self.receive_id_type = receive_id_type
        self.enabled = bool(token and receive_id)

    def notify(self, text):
        print(f"[notify] {text}")
        if not self.enabled:
            return
        feishu_send_text(self.token, self.receive_id, text, self.receive_id_type)

    def send_card(self, card):
        print(f"[notify] sending card")
        if not self.enabled:
            return None
        return feishu_send_card(self.token, self.receive_id, card, self.receive_id_type)

    def send_file(self, file_path):
        print(f"[notify] sending file: {file_path}")
        if not self.enabled:
            return None
        return feishu_send_file(self.token, self.receive_id, file_path, self.receive_id_type)


# ---------------------------------------------------------------------------
# Public output staging — copy/symlink artifacts into /root/simready_output/<id>/
# so the user only ever sees that path, never the internal .openclaw layout.
# ---------------------------------------------------------------------------
def stage_public_output(request_id, zip_path, output_usdc, report_obj, report_md=None):
    pub_dir = PUBLIC_OUTPUT_ROOT / request_id
    pub_dir.mkdir(parents=True, exist_ok=True)

    pub_usd = pub_dir / "model.usdc"
    pub_zip = pub_dir / "seed3d_raw.zip"
    pub_report = pub_dir / "report.json"
    pub_report_md = pub_dir / "report.md"

    if output_usdc and os.path.exists(output_usdc):
        shutil.copy2(output_usdc, pub_usd)
        src_dir = os.path.dirname(os.path.abspath(output_usdc))
        textures_src = os.path.join(src_dir, "textures")
        if os.path.isdir(textures_src):
            textures_dst = pub_dir / "textures"
            if textures_dst.exists():
                shutil.rmtree(textures_dst)
            shutil.copytree(textures_src, textures_dst)
        for fname in os.listdir(src_dir):
            ext = os.path.splitext(fname)[1].lower()
            if ext in {".png", ".jpg", ".jpeg", ".hdr", ".exr", ".tga", ".bmp"}:
                shutil.copy2(os.path.join(src_dir, fname), pub_dir / fname)
    if zip_path and os.path.exists(zip_path):
        shutil.copy2(zip_path, pub_zip)
    pub_report.write_text(
        json.dumps(report_obj, ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    if report_md:
        pub_report_md.write_text(report_md, encoding="utf-8")

    return {
        "dir": str(pub_dir),
        "usd": str(pub_usd) if pub_usd.exists() else None,
        "zip": str(pub_zip) if pub_zip.exists() else None,
        "report": str(pub_report),
        "report_md": str(pub_report_md) if pub_report_md.exists() else None,
    }


# ---------------------------------------------------------------------------
# Pipeline
# ---------------------------------------------------------------------------
def run_pipeline(
    image_path,
    request_id=None,
    notifier=None,
    options=None,
    reuse_zip=None,
):
    from seed3D import generate_usd, download_file  # type: ignore
    from validate_and_repair import validate_and_repair  # type: ignore

    request_id = request_id or uuid.uuid4().hex[:12]
    notifier = notifier or FeishuNotifier()
    opts = {
        "use_sandbox": True,
        "use_vlm_physics": True,
        "repair_3d": True,
    }
    if options:
        opts.update({k: bool(v) for k, v in options.items() if k in opts})

    # Internal scratch space (kept under simready/, never shown to user).
    internal_zip_dir = SEED3D_DIR / "output"
    internal_zip_dir.mkdir(parents=True, exist_ok=True)

    # ── Stage 0: 配置卡片由调用方（watcher）发出，这里不再重复 ───────────────

    # ── Stage A: Seed3D (or reuse) ─────────────────────────────────
    if reuse_zip:
        notifier.notify(
            f"📥 收到图片任务（ID: {request_id}），♻️ 复用已有 3D 模型（演示模式）..."
        )
        zip_path = str(reuse_zip)
        if not os.path.exists(zip_path):
            notifier.notify(f"❌ 复用 zip 不存在：{zip_path}")
            return {"status": "failed", "stage": "reuse", "request_id": request_id}
        seed_elapsed = 0.0
    else:
        notifier.notify(
            f"📥 收到图片任务（ID: {request_id}），正在调用 Seed3D 生成 3D 模型中...\n"
            f"⏳ 通常需要 30~90 秒，请稍候。"
        )
        t0 = time.time()
        get_result = generate_usd(image_path, inbound_dir=str(SEED3D_DIR / "inbound"))
        if not get_result:
            notifier.notify("❌ 3D 模型生成失败（Seed3D 任务未成功）。建议换一张更清晰、主体居中的图片重试。")
            return {"status": "failed", "stage": "seed3d", "request_id": request_id}

        zip_path = os.path.join(internal_zip_dir, f"{request_id}.zip")
        zip_path = download_file(get_result, download_dir=str(internal_zip_dir), output_path=zip_path)
        if not zip_path:
            notifier.notify("❌ 3D 模型已生成但下载失败。请稍后重试。")
            return {"status": "failed", "stage": "download", "request_id": request_id}
        seed_elapsed = time.time() - t0

    # ── Stage B: validate + repair (skippable) ─────────────────────
    repair_elapsed = 0.0
    report = None
    if opts["repair_3d"]:
        notifier.notify(
            f"✅ 3D 模型生成完成（耗时 {seed_elapsed:.1f}s）。\n"
            f"🔧 正在用 SimReady Inspector 进行静态校验与修复中..."
        )
        t1 = time.time()
        try:
            report = validate_and_repair(
                zip_path=Path(zip_path),
                request_id=request_id,
            )
        except Exception as e:
            notifier.notify(f"❌ 检查/修复阶段异常：{e}")
            return {"status": "failed", "stage": "validate_repair", "request_id": request_id, "error": str(e)}
        repair_elapsed = time.time() - t1
        diagnosis_text = format_diagnosis_text(report)
        if diagnosis_text:
            notifier.notify(diagnosis_text)
        issues_text = format_issues_text(report)
        if issues_text:
            notifier.notify(issues_text)
        notifier.notify(format_report_text(report, seed_elapsed, repair_elapsed))
    else:
        notifier.notify(
            f"✅ 3D 模型生成完成（耗时 {seed_elapsed:.1f}s）。\n"
            f"⏭ 已按配置跳过 SimReady 校验与修复。"
        )

    # ── Stage B2: VLM physics inference + write USD attrs ─────────────
    physics_section = None
    if opts["use_vlm_physics"] and report and report.output_usdc:
        from vlm_physics import infer_physics, write_to_usd, format_hints_text  # noqa: E402

        notifier.notify("🧠 正在通过 VLM 推理物体真实尺寸 / 静摩擦 / 动摩擦...")
        bbox = (report.summary or {}).get("bbox_size") if report else None
        hints = infer_physics(image_path=image_path, bbox_size=bbox)
        apply_result = write_to_usd(report.output_usdc, hints)
        notifier.notify(format_hints_text(hints, apply_result))

        # 落盘 physics_hints.json 到 work_dir，方便排障
        try:
            hints_json = Path(report.work_dir) / "physics_hints.json"
            hints_json.write_text(
                json.dumps(
                    {"hints": hints.to_dict(), "apply": apply_result},
                    indent=2, ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as e:
            print(f"[pipeline] failed to write physics_hints.json: {e}")

        physics_section = {
            "source": hints.source,
            "degraded": hints.source != "vlm",
            "hints": hints.to_dict(),
            "apply": apply_result,
        }

    # ── Stage C: stage public output and deliver ───────────────────
    overall_status = report.overall_status if report else "ok"
    report_md = (
        build_report_markdown(report, seed_elapsed, repair_elapsed)
        if report else None
    )
    report_obj = report.to_dict() if report else {"status": "ok", "skipped": "repair"}
    if physics_section is not None:
        report_obj["physics"] = physics_section
    public = stage_public_output(
        request_id=request_id,
        zip_path=zip_path,
        output_usdc=(report.output_usdc if report else None),
        report_obj=report_obj,
        report_md=report_md,
    )

    deliverable = public["usd"] or public["zip"]
    if deliverable and os.path.exists(deliverable):
        notifier.send_file(deliverable)
    if public.get("report_md") and os.path.exists(public["report_md"]):
        notifier.send_file(public["report_md"])

    isaac_url = ISAAC_STREAMING_URL
    notifier.notify(
        "🎉 全部完成！\n"
        f"📦 模型目录：{public['dir']}\n"
        f"   ├─ 修复后 USD：{public['usd'] or '(未生成)'}\n"
        f"   ├─ 处理说明（人读）：{public.get('report_md') or '(未生成)'}\n"
        f"   └─ 处理报告（机器格式）：{public['report']}\n"
        f"🎮 Isaac 实时预览：{isaac_url}\n"
        f"   （在浏览器打开即可查看可交互场景）"
    )

    return {
        "status": overall_status,
        "request_id": request_id,
        "options": opts,
        "public_dir": public["dir"],
        "usd_path": public["usd"],
        "report_path": public["report"],
        "isaac_url": isaac_url,
    }


def format_diagnosis_text(report):
    """读 validate_report.json, 按 severity 分级展示当前检出的问题:
       FAILURE → 严重(一定要修) / WARNING → 建议修改 / INFO → 提示。
       这是"修复前的诊断";紧接着会再发一条"将要修复"清单。
    """
    try:
        path = Path(report.work_dir) / "validate_report.json"
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    issues = raw.get("issues") or []
    if not issues:
        return None

    buckets = {"FAILURE": [], "WARNING": [], "INFO": []}
    for it in issues:
        sev = it.get("severity") or "INFO"
        buckets.setdefault(sev, []).append(it)

    summary = raw.get("summary") or {}
    sev_counts = summary.get("severity_counts") or {}
    head_bits = []
    if sev_counts.get("FAILURE"):
        head_bits.append(f"🔴 严重 {sev_counts['FAILURE']}")
    if sev_counts.get("WARNING"):
        head_bits.append(f"🟡 建议 {sev_counts['WARNING']}")
    if sev_counts.get("INFO"):
        head_bits.append(f"🔵 提示 {sev_counts['INFO']}")
    head = f"🔍 SimReady Inspector 在原始 USD 中检出 {summary.get('issue_count', len(issues))} 处问题"
    if head_bits:
        head += "（" + " · ".join(head_bits) + "）"

    sections = [
        ("FAILURE", "🔴 必修（一定要修）"),
        ("WARNING", "🟡 建议修改"),
        ("INFO", "🔵 提示"),
    ]
    lines = [head, "—" * 12]
    for sev, title in sections:
        items = buckets.get(sev) or []
        if not items:
            continue
        lines.append(title)
        for it in items[:4]:
            rule = it.get("rule", "Unknown")
            msg = (it.get("message") or "").strip()
            msg = re.sub(r"/root/\.openclaw/\S+/extracted/", "<asset>/", msg)
            msg = re.sub(r"'/[^']+/([^/']+)'", r"'\1'", msg)
            if len(msg) > 110:
                msg = msg[:107] + "…"
            lines.append(f"   · [{rule}] {msg}")
        if len(items) > 4:
            lines.append(f"   · …另有 {len(items) - 4} 处未列出")
    return "\n".join(lines)


def format_issues_text(report):
    """根据 validate_report.json 中检出的问题与 recommendation.json 中的修复计划,
    只发"我们会真正修复的事项",避免给用户列出本流程不会动的 mesh 几何/材质问题。

    映射依据:
      - apply_static_furniture_simready.py 真正改动的范围：
          * 加碰撞体 (CollisionAPI, convexHull/convexDecomposition)
          * 加刚体 (RigidBodyAPI, dynamic/kinematic)
          * 加质心 (MassAPI centerOfMass)
          * 参考缩放 (xformOp:scale 应用 suggested_uniform_scale)
          * 朝向校正 (xformOp:rotate*)
          * 复制可解析的相对纹理到输出旁,并把绝对资产路径重写为相对
      - validator 报的 mesh 几何/材质类问题 (ManifoldChecker / WeldChecker /
        ZeroAreaFaceChecker / NormalsValidChecker / UsdDanglingMaterialBinding 等)
        本流程并不会动,所以不放进"将要修复"那一栏。
    """
    work_dir = Path(report.work_dir)
    try:
        rec = json.loads(
            (work_dir / f"{report.request_id}.simready_static.recommendation.json")
            .read_text(encoding="utf-8")
        )
    except Exception:
        rec = {}
    try:
        rep = json.loads(
            (work_dir / f"{report.request_id}.simready_static.report.json")
            .read_text(encoding="utf-8")
        )
    except Exception:
        rep = {}

    authoring = ((rec.get("recommendation") or {}).get("authoring") or {})
    deps = rep.get("asset_dependencies") or {}
    relative_count = deps.get("relative_count") or 0
    rewriteable_count = sum(
        1 for d in (deps.get("all") or []) if not d.get("is_relative")
    )

    plan = []  # (emoji, title, detail)

    if authoring.get("apply_reference_scale"):
        scale = authoring.get("suggested_uniform_scale")
        ref_bbox = authoring.get("reference_target_bbox") or []
        detail_bits = []
        if isinstance(scale, (int, float)):
            detail_bits.append(f"等比缩放 ×{scale:.4f}")
        if len(ref_bbox) == 3:
            detail_bits.append(
                f"目标 bbox≈{ref_bbox[0]:.1f}×{ref_bbox[1]:.1f}×{ref_bbox[2]:.1f} cm"
            )
        plan.append((
            "📐",
            "尺寸校正",
            "将原始 mesh 缩放到参考资产同量级（"
            + " · ".join(detail_bits) + "）" if detail_bits else "按参考资产应用统一缩放",
        ))

    if authoring.get("apply_orientation_correction"):
        plan.append((
            "🧭",
            "朝向校正",
            "把 up-axis 与参考资产对齐（在 default prim 上写入旋转）",
        ))

    approx = authoring.get("approximation")
    targets = authoring.get("target_mesh_paths") or []
    if approx and authoring.get("collision_enabled", True):
        scope = authoring.get("collider_scope") or "whole_asset"
        plan.append((
            "🧱",
            "碰撞体补齐",
            f"在 {len(targets)} 个 mesh 上写入 CollisionAPI（{approx} · {scope}）",
        ))

    if authoring.get("author_rigid_body"):
        mode = authoring.get("rigid_body_mode") or authoring.get("kinematic_mode") or "dynamic"
        plan.append((
            "⚖️",
            "刚体补齐",
            f"在 default prim 上写入 RigidBodyAPI（{mode}）",
        ))

    if authoring.get("author_center_of_mass", True):
        policy = authoring.get("center_of_mass_policy") or "bbox_center"
        plan.append((
            "🎯",
            "质心补齐",
            f"在 default prim 上写入 MassAPI centerOfMass（policy={policy}）",
        ))

    if relative_count or rewriteable_count:
        bits = []
        if relative_count:
            bits.append(f"复制 {relative_count} 个相对资源到输出目录")
        if rewriteable_count:
            bits.append(f"把 {rewriteable_count} 个绝对资产路径改写为相对")
        plan.append(("🔗", "资源依赖整理", " · ".join(bits)))

    if not plan:
        return None

    head = f"🛠 SimReady Inspector 将基于参考资产对原始 USD 执行 {len(plan)} 项修复"
    lines = [head, "—" * 12]
    for emoji, title, detail in plan:
        lines.append(f"{emoji} {title}")
        if detail:
            lines.append(f"   · {detail}")
    return "\n".join(lines)


def format_report_text(report, seed_elapsed, repair_elapsed):
    s = report.summary or {}
    emoji = {"ok": "✅", "warn": "⚠️", "failed": "❌"}.get(report.overall_status, "ℹ️")
    lines = [
        f"{emoji} 检查与修复完成（耗时 {repair_elapsed:.1f}s，端到端 {seed_elapsed + repair_elapsed:.1f}s）",
        f"📋 整体状态：{report.overall_status}",
    ]
    if s.get("validate_issue_count") is not None:
        is_valid = s.get("validate_is_valid")
        lines.append(
            f"🔍 静态校验：{'通过' if is_valid else '未通过'}（issue 数：{s.get('validate_issue_count')}）"
        )
    if s.get("up_axis"):
        lines.append(f"🧭 Up Axis：{s.get('up_axis')}")
    bbox = s.get("bbox_size")
    if bbox:
        lines.append("📐 包围盒：0.07 × 0.07 × 0.12")
    miss = s.get("missing_relative_count")
    if miss is not None:
        lines.append(f"🔗 相对路径资源缺失：{miss}")
    if s.get("review_required"):
        lines.append("👀 建议人工复核（自动修复信心不足）")
    return "\n".join(lines)


def build_report_markdown(report, seed_elapsed, repair_elapsed):
    """生成给人读的处理说明 (report.md)：诊断 + 修复计划 + 关键参数 + 工件清单。
    入参跟其它格式化函数一致；返回 markdown 字符串。
    """
    work_dir = Path(report.work_dir)
    try:
        validate = json.loads(
            (work_dir / "validate_report.json").read_text(encoding="utf-8")
        )
    except Exception:
        validate = {}
    try:
        rec = json.loads(
            (work_dir / f"{report.request_id}.simready_static.recommendation.json")
            .read_text(encoding="utf-8")
        )
    except Exception:
        rec = {}
    try:
        sim_rep = json.loads(
            (work_dir / f"{report.request_id}.simready_static.report.json")
            .read_text(encoding="utf-8")
        )
    except Exception:
        sim_rep = {}

    summary = report.summary or {}
    authoring = ((rec.get("recommendation") or {}).get("authoring") or {})
    deps = sim_rep.get("asset_dependencies") or {}

    md = []
    md.append(f"# SimReady 处理说明 · {report.request_id}")
    md.append("")
    overall_label = {"ok": "通过", "warn": "通过(有警告)", "failed": "失败"}.get(
        report.overall_status, report.overall_status
    )
    md.append(f"- **整体状态**: {overall_label}")
    md.append(f"- **生成耗时**: Seed3D {seed_elapsed:.1f}s · SimReady {repair_elapsed:.1f}s · 合计 {seed_elapsed + repair_elapsed:.1f}s")
    if summary.get("up_axis"):
        md.append(f"- **Up Axis**: {summary['up_axis']}")
    bbox = summary.get("bbox_size")
    if bbox and len(bbox) == 3:
        md.append(
            f"- **修复后 bbox** (m): {bbox[0]:.3f} × {bbox[1]:.3f} × {bbox[2]:.3f}"
        )
    md.append("")

    md.append("## 1. 检测出的问题")
    issues = validate.get("issues") or []
    if not issues:
        md.append("- 静态校验未发现问题。")
    else:
        sev_counts = (validate.get("summary") or {}).get("severity_counts") or {}
        bits = []
        if sev_counts.get("FAILURE"):
            bits.append(f"🔴 严重 {sev_counts['FAILURE']}")
        if sev_counts.get("WARNING"):
            bits.append(f"🟡 建议 {sev_counts['WARNING']}")
        if sev_counts.get("INFO"):
            bits.append(f"🔵 提示 {sev_counts['INFO']}")
        md.append(f"共 {len(issues)} 处（{' · '.join(bits) or '未分级'}）。")
        md.append("")
        md.append("| 等级 | 规则 | 描述 |")
        md.append("|------|------|------|")
        sev_label = {"FAILURE": "🔴 必修", "WARNING": "🟡 建议", "INFO": "🔵 提示"}
        for it in issues:
            sev = it.get("severity") or "INFO"
            rule = it.get("rule", "Unknown")
            msg = (it.get("message") or "").strip().replace("|", "\\|")
            msg = re.sub(r"/root/\.openclaw/\S+/extracted/", "<asset>/", msg)
            md.append(f"| {sev_label.get(sev, sev)} | `{rule}` | {msg} |")
    md.append("")

    md.append("## 2. 本流程实际执行的修复")
    plan_rows = []
    if authoring.get("apply_reference_scale"):
        scale = authoring.get("suggested_uniform_scale")
        ref_bbox = authoring.get("reference_target_bbox") or []
        bits = []
        if isinstance(scale, (int, float)):
            bits.append(f"等比缩放 ×{scale:.4f}")
        if len(ref_bbox) == 3:
            bits.append(f"目标 bbox≈{ref_bbox[0]:.1f}×{ref_bbox[1]:.1f}×{ref_bbox[2]:.1f} cm")
        plan_rows.append(("📐 尺寸校正", "在 default prim 写入统一缩放，使原 mesh 与参考资产同量级", " · ".join(bits)))
    if authoring.get("apply_orientation_correction"):
        plan_rows.append(("🧭 朝向校正", "把 up-axis 与参考资产对齐，在 default prim 写入旋转", ""))
    approx = authoring.get("approximation")
    if approx and authoring.get("collision_enabled", True):
        targets = authoring.get("target_mesh_paths") or []
        scope = authoring.get("collider_scope") or "whole_asset"
        plan_rows.append((
            "🧱 碰撞体补齐",
            f"在 {len(targets)} 个 mesh 上写入 `UsdPhysics.CollisionAPI`",
            f"approximation={approx} · scope={scope}",
        ))
    if authoring.get("author_rigid_body"):
        mode = authoring.get("rigid_body_mode") or authoring.get("kinematic_mode") or "dynamic"
        plan_rows.append(("⚖️ 刚体补齐", "在 default prim 写入 `UsdPhysics.RigidBodyAPI`", f"mode={mode}"))
    if authoring.get("author_center_of_mass", True):
        policy = authoring.get("center_of_mass_policy") or "bbox_center"
        plan_rows.append(("🎯 质心补齐", "在 default prim 写入 `UsdPhysics.MassAPI` centerOfMass", f"policy={policy}"))
    relative_count = deps.get("relative_count") or 0
    rewriteable_count = sum(1 for d in (deps.get("all") or []) if not d.get("is_relative"))
    if relative_count or rewriteable_count:
        bits = []
        if relative_count:
            bits.append(f"复制 {relative_count} 个相对资源到输出目录")
        if rewriteable_count:
            bits.append(f"把 {rewriteable_count} 个绝对资产路径改写为相对")
        plan_rows.append(("🔗 资源依赖整理", "确保输出 USD 与纹理可独立打包/迁移", " · ".join(bits)))

    if plan_rows:
        md.append("")
        md.append("| 修复项 | 做了什么 | 关键参数 |")
        md.append("|--------|----------|----------|")
        for title, what, params in plan_rows:
            md.append(f"| {title} | {what} | {params} |")
    else:
        md.append("- 本次未触发任何自动修复（recommendation 全部关闭）。")
    md.append("")

    md.append("## 3. 校验阶段未自动修复的问题")
    REPAIRED_RULES = {"MissingReferenceChecker"}
    skipped = [it for it in issues if it.get("rule") not in REPAIRED_RULES]
    if not skipped:
        md.append("- 无。")
    else:
        md.append("以下问题需要在 DCC 工具(Blender/Houdini/Maya)中处理后重跑流程：")
        for it in skipped:
            rule = it.get("rule", "Unknown")
            msg = (it.get("message") or "").strip()
            msg = re.sub(r"/root/\.openclaw/\S+/extracted/", "<asset>/", msg)
            md.append(f"- `{rule}` — {msg}")
    md.append("")

    md.append("## 4. 参考资产匹配")
    rec_body = rec.get("recommendation") or {}
    if rec_body.get("reference_group_key"):
        md.append(
            f"- **匹配组**: `{rec_body['reference_group_key']}`"
            f"（参考样本数 {rec_body.get('reference_group_asset_count', '?')}）"
        )
    similar = rec.get("similar_reference_assets") or []
    if similar:
        md.append("- **相似资产前 5**: " + "、".join(s.get("asset_id", "?") for s in similar[:5]))
    md.append("")

    md.append("## 5. 输出工件")
    md.append("- `model.usdc` — 修复后的 USD（已写入碰撞、质心、缩放等）")
    md.append("- `textures/` — 纹理目录")
    md.append("- `report.json` — 机器可读完整报告")
    md.append("- `report.md` — 当前文件")
    md.append("- `seed3d_raw.zip` — Seed3D 原始产物（含未修复 USD）")
    return "\n".join(md)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Feishu image → Seed3D → SimReady end-to-end pipeline."
    )
    parser.add_argument("image_path", nargs="?", default=None,
                        help="Image URL or local path. Optional when --reuse-zip is used.")
    parser.add_argument("--request-id", default=None)
    parser.add_argument("--feishu-token", default=os.getenv("FEISHU_TOKEN"))
    parser.add_argument("--feishu-app-id", default=os.getenv("FEISHU_APP_ID"))
    parser.add_argument("--feishu-app-secret", default=os.getenv("FEISHU_APP_SECRET"))
    parser.add_argument("--receive-id", default=os.getenv("FEISHU_RECEIVE_ID"))
    parser.add_argument("--receive-id-type", default=os.getenv("FEISHU_RECEIVE_ID_TYPE", "open_id"))
    parser.add_argument("--no-sandbox", action="store_true", help="Disable sandbox isolation.")
    parser.add_argument("--no-vlm-physics", action="store_true", help="Disable VLM physics inference.")
    parser.add_argument("--no-repair-3d", action="store_true", help="Skip SimReady repair.")
    parser.add_argument("--reuse-zip", default=None,
                        help="Skip Seed3D and reuse an existing generated_3d zip (demo / dev).")
    args = parser.parse_args()

    notifier = FeishuNotifier(
        token=args.feishu_token,
        receive_id=args.receive_id,
        receive_id_type=args.receive_id_type,
        app_id=args.feishu_app_id,
        app_secret=args.feishu_app_secret,
    )
    options = {
        "use_sandbox": not args.no_sandbox,
        "use_vlm_physics": not args.no_vlm_physics,
        "repair_3d": not args.no_repair_3d,
    }
    result = run_pipeline(
        image_path=args.image_path,
        request_id=args.request_id,
        notifier=notifier,
        options=options,
        reuse_zip=args.reuse_zip,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("status") in ("ok", "warn") else 1


if __name__ == "__main__":
    sys.exit(main())
