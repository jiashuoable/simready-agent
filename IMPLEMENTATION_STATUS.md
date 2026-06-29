# SimReady Agent — 实现状态

| 项 | 值 |
|---|---|
| 文档版本 | v0.1 |
| 编写日期 | 2026-06-29 |
| 对应 PRD | [PRD.md](PRD.md) v0.1 |
| 当前快照 | `simready_working_20260629.tar.gz`（已跑通的备份） |

> 这份文档是给接手研发同学看的：哪些是当前真实跑得起来的能力、哪些只画了空架子需要被实现、哪些是已知的副作用和坑。代码部分已经把 mock 分支整体删除（CLI 缺失就直接 fail-fast），所以"已实现"指的是**接得通真实工具链**的实现。

---

## 1. 模块全景

```
simready/
├── pipeline.py                # 主流程编排 + 飞书消息封装
├── feishu_watcher.py          # 日志驱动的图片侦测常驻进程
├── PRD.md                     # 产品需求
├── IMPLEMENTATION_STATUS.md   # 本文档
├── .env.example               # 环境变量模板
├── seed3D/
│   ├── seed3D.py              # 调火山 ARK Seed3D 2.0 接口生成 zip
│   ├── validate_and_repair.py # 调用 omni-asset-cli + usd-simready-cli
│   ├── inbound/               # 本地手动跑的任务队列样本
│   └── src/                   # 5 张示例输入图
├── content-agents/            # ↪ NVIDIA-Omniverse/content-agents (submodule)
├── omni-asset-cli/            # ↪ songshen06/omni-asset-cli (submodule)
└── usd-simready-inspector/    # ↪ songshen06/usd-simready-inspector (submodule)
```

三个 submodule 是外部依赖（开源/fork），本仓库不再修改它们。pipeline 把它们当 CLI 子进程调用。

---

## 2. 已实现（可直接跑通的能力）

### 2.1 触发：日志驱动的图片侦测 ✅
对应 PRD §4.1。文件：[feishu_watcher.py](feishu_watcher.py)

- tail `/tmp/openclaw/openclaw-*.log`，正则提 `(open_id, chat_id, chat_type)` 和 `image_path`
- 30s TTL 关联窗口 + 启动后 8s 静默 + 进程内 `_seen_images` 去重
- 跨进程 pos 状态 `/tmp/feishu_watcher.pos`，重启续读
- 同时 tail 当前目录所有 `openclaw-*.log`，不依赖按本地日期猜文件名

跑法：
```
python feishu_watcher.py             # 从当前文件末尾开始
python feishu_watcher.py --from-start # 从头回放（调试用）
python feishu_watcher.py --dry-run    # 不真跑 pipeline
```

### 2.2 Seed3D 2.0 调用 + 下载 ✅
对应 PRD §4.3 输入。文件：[seed3D/seed3D.py](seed3D/seed3D.py)

- 直接调火山方舟 ARK SDK，提交 image → 拿到生成 ID → 轮询完成 → 拿下载 URL → 落盘成 zip
- 支持 base64 / http URL / 本地路径三种输入
- 跨进程文件锁的简单 JSONL 队列（`pending.jsonl` / `done.jsonl` / `failed.jsonl`），可用于多 worker 串行调度
- 需要环境变量 `ARK_API_KEY`（见 `.env.example`）

### 2.3 静态校验 + SimReady 修复 ✅
对应 PRD §4.4。文件：[seed3D/validate_and_repair.py](seed3D/validate_and_repair.py)

- Stage 1: unzip → 自动找 USD 入口（`pbr/mesh_textured_pbr.usd` 优先）
- Stage 2: 调 `omni-asset-cli validate --profile stage1-furniture`
- Stage 3: 调 `usd_simready_cli process`，输出 `.simready_static.usdc` + report.json
- 合并 stage 状态成 `pipeline.json`，附 summary（up_axis、bbox、缺失依赖、review_required）

⚠️ **重要变更**：原先 `--mock` 分支已删除。CLI 不存在或 reference json 缺失直接 fail-fast，**不再写桩报告蒙混过关**。研发同学在新环境跑之前必须先把 submodule 装好。

### 2.4 产物落到公开目录 ✅
对应 PRD §4.5。`stage_public_output`，位于 [pipeline.py](pipeline.py)

- 把 USD/zip/report/report.md 拷贝到 `/root/simready_output/<request_id>/`
- 用户可见路径全是 `simready_output/`，内部 `.openclaw/` 路径不外泄

### 2.5 VLM 物理推理 + 写 USD ✅（v1 范围内已落地）
对应 PRD §4.3 的核心子集。文件：[vlm_physics.py](vlm_physics.py)

**推理项（v1 实际实现）**：
- `dimensions_m`：[x,y,z] 真实世界尺寸（米）—— 写为 USD customAttribute `vlm:dimensions_m`
- `friction_static` / `friction_dynamic`：写为 `UsdPhysics.MaterialAPI` 标准属性
- `material_hint`：材质标签（wood/metal/plastic/...）—— 写为 `vlm:material_hint`
- `confidence` + `notes`：写为 `vlm:confidence` / `vlm:notes`

**调用链**：
1. pipeline 在 `validate_and_repair` 之后调 `infer_physics(image_path, bbox_size=...)`
2. 调用火山方舟 `client.chat.completions.create` + 视觉模型（默认 `doubao-1-5-vision-pro-250328`，可经 `VLM_MODEL` 切）
3. 收到 JSON 后通过 `_coerce_hints` 做范围裁剪与字段兜底
4. `write_to_usd` 用 USD Python (`pxr`) 把摩擦写为 `UsdPhysics.MaterialAPI`，尺寸/材质/置信度写为 customAttribute
5. 物理 hints 落盘 `<work_dir>/physics_hints.json`，pipeline 摘要里附 `physics` 节
6. 飞书推送一条 `🧠 物理属性推理（VLM, confidence 0.xx）` 消息

**降级**：`ARK_API_KEY` 缺失、`pxr` 不可用、模型返回非 JSON 等任一失败都返回 `DEFAULT_HINTS`（标 `source=default`），pipeline 继续走，不阻断。

**与 PRD §4.3 的差距（v2 再补）**：
- `mass_kg` / `restitution` / `collider_shape` / `rigid_body_mode` —— PRD 列了但 v1 没要，可在 hints 加字段、再透传到 simready_cli 后补
- 没接 PRD §4.3.5 的 simready_cli `--mass/--friction-*` 透传链路（因为 simready CLI 没暴露这些参数）—— 当前是绕开 CLI 直接 `pxr` 写 USD attribute，等效但更轻
- 默认模型用了火山豆包视觉（PRD §4.3.4 写的是 claude-sonnet-4-6）—— 跟用户在最新需求里确认过

### 2.6 飞书消息推送（无凭据自动降级）✅
- `FeishuNotifier` 在缺 token/receive_id 时自动 no-op，只打 stdout
- 支持文本、卡片、文件三种发送方式
- 用 `app_id + app_secret` 换 `tenant_access_token`，自动管理

### 2.6 报告渲染 ✅
- `format_diagnosis_text` / `format_issues_text` / `format_report_text`：把 PipelineReport 渲成飞书可读文本
- `build_report_markdown`：生成 report.md（诊断 + 修复计划 + 关键参数 + 工件清单）

---

## 3. 部分实现 / 半成品

### 3.1 配置卡片 UI ⚠️ (UI 有，回调没接)
对应 PRD §4.2，差距在 4.2.2 状态机。文件：`build_config_card` in [pipeline.py:137](pipeline.py)

**当前能做到**：卡片渲染正常，三个 checkbox + "🚀 开始处理"按钮按 PRD 设计的样子展示。

**没接通的部分**：
- 用户点 toggle / 点 submit 后**飞书把 action 事件投到哪里 没有 handler 接管**。当前 watcher 在发卡片之后直接按默认 opts 起 pipeline，**用户的勾选根本不会被读取**。
- `/tmp/simready_pending/<request_id>.json` 这个 PRD §4.2.3 约定的持久化文件**没有写入逻辑**（`PENDING_DIR` 常量定义了但没用上）。
- 5 分钟超时自动开跑的状态机没实现。

**研发同学要做的**：选 PRD §4.2.4 里的方案 A 或 B 落地 card action handler，把回调写到 pending 文件，watcher 改成 tail pending 文件状态变化触发 pipeline。

### 3.2 报告中的 bbox 显示 ⚠️ (硬编码)
[pipeline.py:592](pipeline.py)：
```python
if bbox:
    lines.append("📐 包围盒：0.07 × 0.07 × 0.12")
```
不管真实 bbox 多大都打这串字。可能是 demo 期间为了截图临时改的，没改回来。研发同学请改成 `bbox[0] × bbox[1] × bbox[2]`。

---

## 4. 未实现（需要后续研发）

### 4.1 VLM 物理推理的扩展项 ⚠️ (v1 子集已实现，见 §2.5)
当前 [vlm_physics.py](vlm_physics.py) 只覆盖了 PRD §4.3.3 表里的 5/10 个字段（dimensions / 静摩擦 / 动摩擦 / 材质 / confidence）。剩下的：

- `mass_kg` —— 配合 SimReady reference 推荐可写 `physics:mass`
- `restitution` —— 已经在 `write_to_usd` 写了 0.0 占位，需要做成真实推理
- `collider_shape` (convexHull / convexDecomposition / triangleMesh ...) —— 影响碰撞精度
- `rigid_body_mode` (dynamic / static / kinematic) —— 默认 dynamic，但桌子/地板这种应该 static
- `category` —— 配合 SimReady reference json 做更精准的匹配

接的做法：在 `PhysicsHints` 加字段 + prompt 加约束 + `write_to_usd` 多写几条 attribute。

### 4.2 Sandbox / Isaac 预览链接 ❌
对应 PRD §4.5 末尾 "Isaac 预览链接" 与 §3.飞书消息 #8。当前：

- `opts["use_sandbox"]` 开关在 pipeline 里**没有任何实际效果**
- 火山 ML 平台 Isaac Streaming 实例 `di-20260508104954-x5gr2`（见 PRD §9.2）**没有任何代码生成该链接**

**研发同学要做的**：实例启停管理 + 链接拼装 + 推送给用户。这块跟火山平台的具体接口耦合，可能需要先找运维同学要 API。

### 4.3 任务持久化文件流 ❌
对应 PRD §4.2.3 + §6.1。`/tmp/simready_pending/` 整套生命周期（pending → running → done）**没有任何代码在写**。当前 watcher 只在内存里管状态。

### 4.4 可观测性 ❌
对应 PRD §5。当前只有 stdout + watcher.log，没有结构化指标。Prometheus 暴露是 v2 范围，v1 可暂不做。

### 4.5 产物 TTL 清理 ❌
对应 PRD §5。`/root/simready_output/` 永不清理，磁盘会涨。v2 项目，先记着。

---

## 5. 环境与依赖

### 5.1 必需环境变量
见 [.env.example](.env.example)。最少要有 `ARK_API_KEY`，没有飞书凭据时 Notifier 自动降级为只打印。

### 5.2 Python 依赖
- `requests`、`volcenginesdkarkruntime`（pip 装）
- `omni-asset-cli` / `usd-simready-inspector`：作为 submodule 接进来，本身有各自的 pyproject.toml
- USD 工具链：参考 omni-asset-cli 自己的 README

### 5.3 启动顺序
```
1) clone & init submodules
2) pip install -r <每个 submodule 的 requirements>
3) cp .env.example .env  并填值
4) export $(cat .env | xargs)
5) python feishu_watcher.py   # 常驻
```

直跑单张图（绕过 watcher）：
```
python pipeline.py /path/to/image.jpg --request-id demo-001
```

---

## 6. 已知坑 / 注意事项

- **三个 submodule 是上游开源代码**（NVIDIA / songshen06 的），我们这边没改它们。如果上游 push 了 breaking change，重新固定 submodule 的 commit。
- **路径耦合**：feishu_watcher 写死了 `/tmp/openclaw/`、`/tmp/feishu_watcher.pos`、`/tmp/simready_pending/`、`/root/simready_output/`。换部署环境时这些都要看。
- **`format_report_text` 里硬编码包围盒字串**（见 §3.2）。
- **seed3D 默认输出根目录**在源文件相邻的 `seed3D/output/` 和 `seed3D/workspace/`，已在 .gitignore 排除。本仓库不应该提交任何 `*.zip` 或解压目录。
- **pipeline.py 的 `from seed3D import generate_usd` 是延迟导入**（在 `run_pipeline` 内部），别在模块顶部就 import，否则会触发 ARK SDK 的 import 副作用。

---

## 7. 给接手研发的优先级建议

| 优先级 | 任务 | 工期估 | 备注 |
|---|---|---|---|
| P0 | 配置卡片 callback 落地（§3.1） | 2-3 天 | 不接通 → 三个开关全是摆设 |
| P0 | VLM 物理推理 Stage B（§4.1） | 3-4 天 | PRD 的 v1 关键卖点 |
| P1 | 修复 bbox 硬编码（§3.2） | 30 分钟 | 一行的事 |
| P1 | 任务持久化文件流（§4.3） | 1-2 天 | 配套卡片回调一起做更顺 |
| P2 | Sandbox / Isaac 预览（§4.2） | 看接口 | 跟火山平台联调 |
| P2 | 可观测性 / TTL（§4.4 / §4.5） | v2 |  |
