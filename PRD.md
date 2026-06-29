# SimReady Agent — PRD

| 项 | 值 |
|---|---|
| 文档版本 | v0.1 |
| 起草日期 | 2026-06-15 |
| 模块 | `openclaw_code/simready/` |
| 主要负责 | SimReady Agent 团队 |

---

## 1. 背景与目标

### 1.1 背景
当前业务场景下，3D 内容创作者/仿真工程师拿到一张参考图后，需要经历多步手工流程才能得到一个**可直接进 Isaac Sandbox 跑物理仿真**的 USD 资产：
1. 调 Seed3D 生成几何 + 贴图；
2. 解压、定位 USD 入口；
3. 跑 omni-asset-cli 静态校验，根据报告手工修复；
4. 跑 usd-simready-inspector 补齐 SimReady 元数据；
5. 通过 VLM/经验估算物理属性（质量、摩擦、碰撞形状）并手工写回 USD；
6. 把产物交付给下游 Isaac 仿真。

整套流程涉及 4 个 CLI、3 套 Python 环境、约 5–10 分钟人力，且任何一步出错都需要回溯。

### 1.2 目标
在飞书会话内提供"**发图即得 SimReady USD**"的零交互体验：
- **触发零成本**：用户只需向 Bot 发送一张图片，无需输入指令；
- **端到端 ≤ 2 分钟**：从图片到带物理参数的 USD + 校验报告；
- **可控可观测**：用户可在卡片中按需启用/关闭三类处理（物理补齐、检测报告、Sandbox 校验），全程进度可见；
- **与 LLM agent 解耦**：pipeline 由独立常驻进程基于日志事件驱动，agent hallucinate 也不影响触发可靠性。

### 1.3 非目标（v1 不做）
- 视频、多图、带描述文本的复合输入；
- 用户自定义 prompt 引导生成；
- 任务排队/限流/优先级调度；
- 产物长期留存与版本管理；
- 跨工作空间/多租户。

---

## 2. 用户场景

### 2.1 主场景：单图生成 SimReady USD
1. 用户在飞书 1v1 会话向 SimReady Bot 发送一张物体参考图；
2. ≤5s：Bot 回复**配置卡片**，三个开关默认全部开启；
3. 用户**勾选/取消**任意开关后点击"🚀 开始处理"；若 5 分钟内未点击，按当前勾选状态自动开跑；
4. Bot 推送 "📥 收到任务"，开始 Seed3D 生成（30–90s）；
5. Bot 推送 "✅ 3D 生成完成 → 🔧 进入 SimReady 校验/修复"；
6. Bot 推送**修复前问题清单**（带严重等级、修复建议）；
7. Bot 推送 **VLM 物理推理结果**（质量、摩擦、碰撞形状等，附置信度）；
8. Bot 推送总结（整体状态、包围盒、Up Axis、缺失依赖、是否需人工复核）；
9. Bot 上传修复后 `.usdc` 文件 + Isaac 实时预览链接。

### 2.2 异常场景
- **Seed3D 生成失败**：推送"建议换更清晰、主体居中的图片重试"，结束；
- **VLM 物理推理失败**：降级使用静态默认值（dynamic rigid body + 1kg + 0.5 friction），并在卡片标注降级；
- **校验/修复失败**：推送原因，仍交付 Seed3D 原生 USD；
- **凭据缺失**：Notifier 自动降级为本地 stdout，pipeline 继续跑，产物落盘。

---

## 3. 系统架构

```
飞书用户上传图片
       │
       ▼
openclaw-lark (主进程)
   └─ 写入 /tmp/openclaw/openclaw-*.log
       │
       ▼  日志 tail + 正则关联 (open_id ↔ image_path)
[feishu_watcher.py]   ← 独立常驻进程
       │
       ▼ (异步线程)
[pipeline.run_pipeline()]
   ├─ Stage 0  推送配置卡片 (3 checkbox + 提交按钮)
   │           ↑ 等待用户提交 / 5min 超时
   ├─ Stage A  Seed3D 生成几何 + 贴图        (image → zip)
   ├─ Stage B  VLM 物理属性推理 (新增)       (image + USD → physics_hints.json)
   ├─ Stage C  omni-asset-cli validate       (静态校验)
   ├─ Stage D  usd_simready_cli process      (注入 SimReady 元数据 + 物理参数)
   ├─ Stage E  stage_public_output           (产物搬到 /root/simready_output/<id>/)
   └─ Stage F  飞书推送报告 + 文件 + Isaac 链接
```

---

## 4. 功能需求

### 4.1 触发：日志驱动的图片侦测（已实现，沿用）
- **常驻进程** `feishu_watcher.py` tail `/tmp/openclaw/openclaw-*.log`；
- 正则 `RE_RECEIVED` 抓 (open_id, chat_id, chat_type)，正则 `RE_IMAGE_SAVED` 抓 image_path；
- 30s TTL 关联窗口 + 启动后 8s 静默 + 进程内 `_seen_images` 去重；
- 状态文件 `/tmp/feishu_watcher.pos` 支持重启续读。

**入口约束**：
- 文件后缀 ∈ {jpg, jpeg, png, webp, bmp}；
- image 文件必须落盘存在；
- p2p 与群聊都支持，receive_id_type 自动选择。

### 4.2 ⭐ 配置卡片：可交互 checkbox（v1 必须实现）

**当前实现差距**：现版本卡片仅作视觉展示，按钮渲染了但**没有 callback handler 接管**。本版本必须落地真实交互。

#### 4.2.1 卡片元素
| 控件 | 字段 | 默认值 | 说明 |
|---|---|---|---|
| Checkbox | `use_vlm_physics` | ✅ | 启用 VLM 物理属性补齐 + SimReady 修复 |
| Checkbox | `repair_3d` | ✅ | 打印静态检测报告（issue 清单） |
| Checkbox | `use_sandbox` | ✅ | 启动 Sandbox 仿真校验环境（推送 Isaac 链接） |
| Button | `simready_submit` | — | "🚀 开始处理"，立即冻结配置开跑 |

#### 4.2.2 状态机
```
[卡片发出 t0]
    ├── 用户点 toggle → 翻转 opt[name]，更新卡片（同 message_id 局部刷新）
    ├── 用户点 submit → 冻结 opts，触发 pipeline，卡片变灰显示"已提交"
    └── t0 + 300s 仍未 submit → 冻结当前 opts 自动触发，卡片显示"超时自动开跑"
```

#### 4.2.3 持久化
- 用 `request_id` 作主键，把 `(opts, status, sender)` 写到 `/tmp/simready_pending/<request_id>.json`；
- pipeline 启动后该文件迁移到 `running/`，结束后归档到 `done/`，方便排障。

#### 4.2.4 接入方式（设计选项，PRD 不锁定）
- **方案 A**：在 openclaw-lark 内注册 card action handler，回调直接写 pending 文件；
- **方案 B**：watcher 进程内自起一个轻量 HTTP server（127.0.0.1）+ 飞书事件订阅转发；
- 推荐 A，复用 openclaw 已有的飞书 SDK 与 token 管理。

### 4.3 ⭐ VLM 物理属性补齐（v1 新增）

#### 4.3.1 目标
仅靠 Seed3D 生成的几何 + `--author-rigid-body dynamic` 的硬编码默认值，物理仿真效果较差（所有物体一律 1kg、摩擦 0.5、convex hull 碰撞）。本阶段引入 VLM 推理，让物理参数贴合真实物体。

#### 4.3.2 输入
- 用户原图（image_path）；
- Seed3D 生成的 USD 入口（`mesh_textured_pbr.usd`）；
- 几何摘要：bbox 尺寸、面数、是否带贴图。

#### 4.3.3 推理项
| 字段 | 类型 | 取值范围 | 用途 |
|---|---|---|---|
| `category` | str | furniture / appliance / decor / toy / tool / container / other | 后续 SimReady reference 匹配 |
| `mass_kg` | float | 0.01 – 500 | USD `physics:mass` |
| `friction_static` | float | 0.0 – 1.5 | PhysicsMaterial |
| `friction_dynamic` | float | 0.0 – 1.5 | PhysicsMaterial |
| `restitution` | float | 0.0 – 1.0 | PhysicsMaterial |
| `collider_shape` | enum | convexHull / convexDecomposition / boundingCube / triangleMesh / sphere / capsule | UsdPhysics.CollisionAPI |
| `rigid_body_mode` | enum | dynamic / static / kinematic | 默认 dynamic |
| `material_hint` | str | wood / metal / plastic / glass / fabric / ceramic / rubber / other | 影响 friction/restitution 后处理 |
| `confidence` | float | 0.0 – 1.0 | <0.5 触发 review_required |
| `notes` | str | 自由文本 | 推送给用户的可读说明 |

#### 4.3.4 调用契约
- **模型**：默认 `claude-sonnet-4-6`（性价比/速度均衡）；可经环境变量切到 `claude-opus-4-7`；
- **输入消息**：image (base64) + structured prompt，要求严格按 JSON Schema 输出；
- **输出**：上表的 JSON 对象，落盘到 `<work_dir>/<request_id>/physics_hints.json`；
- **超时**：≤30s，超时降级为静态默认值并标注 `degraded=true`；
- **降级路径**：VLM 失败 → 写默认 hints + `degraded=true`，pipeline 继续。

#### 4.3.5 与 SimReady 修复的衔接
- 修改 `stage_simready_process` 调 `usd_simready_cli process` 时，把 `physics_hints.json` 的字段透传为 CLI 参数（`--mass`、`--friction-static`、`--collider-shape` 等）；
- usd-simready-inspector 侧需要新增/确认这些 CLI 参数已被支持；若不支持，新增一个轻量 post-processor 直接写 USD attribute（USD Python API）。

#### 4.3.6 用户可见输出
推送一条独立消息（在"修复前问题清单"之后、"总结"之前）：
```
🧠 物理属性推理（VLM, confidence 0.82）
   ├─ 类别：furniture (chair)
   ├─ 材质：wood
   ├─ 质量：3.2 kg
   ├─ 摩擦：static 0.6 / dynamic 0.45
   ├─ 弹性：0.15
   └─ 碰撞：convexDecomposition
ℹ️ 已写入 USD physics 属性。
```

### 4.4 SimReady 校验与修复（已实现，沿用）
- Stage：`stage_validate` → `stage_simready_process`，与 4.3 衔接见 4.3.5；
- 报告 issue 清单脱敏后推送（去除 `/root/.openclaw/` 内部路径）；
- mock 模式保留：CLI 缺失或 `--mock` 时写桩报告，不阻塞 e2e。

### 4.5 产物交付（已实现，沿用）
- 公共目录 `/root/simready_output/<request_id>/`：
  - `model.usdc`、`textures/`、贴图原图、`seed3d_raw.zip`、`report.json`、新增 `physics_hints.json`；
- 飞书推送：
  - 配置卡片 → 进度文本 → issue 清单 → 物理推理 → 总结 → USD 文件 → Isaac 预览链接。

---

## 5. 非功能需求

| 维度 | 指标 |
|---|---|
| 端到端延迟 (P50) | ≤ 90s（Seed3D 60s + VLM 8s + Repair 10s + 其他 12s） |
| 端到端延迟 (P95) | ≤ 150s |
| 并发 | v1 不做队列，单机同时跑 ≤2 条任务（受 Seed3D 外部配额限制） |
| 重启可靠性 | watcher 杀掉重启不丢任何**未来**消息；不强求重放历史 |
| 凭据降级 | FEISHU 凭据缺失时 Notifier no-op，pipeline 仍出本地产物 |
| 资产留存 | v1 不清理 `/root/simready_output/`；v2 加 7 天 TTL |
| 日志 | watcher stdout + pipeline stdout 重定向到 `simready/watcher.log` |
| 可观测性 | v1 仅文本日志；v2 暴露 Prometheus（task_count、stage_duration、failure_rate） |

---

## 6. 接口与数据契约

### 6.1 PendingTask 文件 schema
路径：`/tmp/simready_pending/<request_id>.json`
```json
{
  "request_id": "fs-1718432100-abc123",
  "image_path": "/tmp/openclaw/img/xxx.jpg",
  "sender": {"open_id": "ou_xxx", "chat_id": "oc_xxx", "chat_type": "p2p"},
  "opts": {"use_vlm_physics": true, "repair_3d": true, "use_sandbox": true},
  "card_message_id": "om_xxx",
  "status": "pending|running|done|failed",
  "created_at": 1718432100,
  "submitted_at": null
}
```

### 6.2 PipelineReport（沿用 + 扩展）
新增 `physics` 节：
```json
{
  "request_id": "...",
  "overall_status": "ok|warn|failed",
  "stages": [...],
  "physics": {
    "source": "vlm|default",
    "degraded": false,
    "hints": { ... 见 4.3.3 ... }
  },
  "summary": { ... }
}
```

### 6.3 飞书消息序列
| # | 类型 | 触发时机 |
|---|---|---|
| 1 | interactive (config card) | 图片落盘后 ≤5s |
| 2 | text "📥 收到任务" | 用户提交 / 超时自动开跑 |
| 3 | text "✅ 3D 生成完成…" | Stage A 结束 |
| 4 | text "🧠 物理属性推理…" | Stage B 结束（仅 use_vlm_physics=true） |
| 5 | text issue 清单 | Stage C 结束（仅 repair_3d=true） |
| 6 | text 总结 | Stage D 结束 |
| 7 | file (.usdc) | Stage E 结束 |
| 8 | text Isaac 链接 | Stage F |

---

## 7. 风险与待澄清

| 风险 | 影响 | 缓解 |
|---|---|---|
| 飞书 card action 在 openclaw 主进程里没有 handler | 4.2 无法落地 | 优先走方案 A，必要时短期用方案 B 兜底 |
| usd-simready-inspector CLI 不支持物理参数透传 | 4.3.5 需自实现 USD post-processor | 起一个 30 行的 USD Python 脚本写 attribute，已可控 |
| 多用户并发同时发图 | Seed3D 配额耗尽 / VLM 限流 | v1 文档化"建议串行"；v2 加任务队列 |
| `/root/simready_output/` 无清理 | 磁盘累积 | v2 加定时 TTL job |
| VLM 推理结果质量不稳定 | 仿真不真实 | 输出 confidence + 用户可手工覆写 hints（v2） |

---

## 8. 里程碑

| 阶段 | 内容 | 工期 |
|---|---|---|
| M1 | 卡片可交互（4.2 方案 A 落地）+ 提交/超时状态机 | 3 天 |
| M2 | VLM 物理推理阶段（4.3）+ 写入 USD attribute | 4 天 |
| M3 | 端到端联调 + 飞书消息序列对齐 + 文档 | 2 天 |
| M4 | demo 录制 + 内部试用收集反馈 | 2 天 |

---

## 9. 附录

### 9.1 现有代码索引
- 触发：[feishu_watcher.py](feishu_watcher.py)
- 主流程：[pipeline.py](pipeline.py)
- Seed3D：[seed3D/seed3D.py](seed3D/seed3D.py)
- 校验+修复：[seed3D/validate_and_repair.py](seed3D/validate_and_repair.py)
- SimReady CLI：[usd-simready-inspector/usd_simready_cli.py](usd-simready-inspector/usd_simready_cli.py)
- omni-asset-cli：[omni-asset-cli/omni_asset_cli.py](omni-asset-cli/omni_asset_cli.py)

### 9.2 关键路径常量
- 输入图临时目录：`/tmp/openclaw/`（openclaw 写入）
- watcher 状态：`/tmp/feishu_watcher.pos`
- pending tasks：`/tmp/simready_pending/`
- pipeline 内部 workspace：`simready/seed3D/workspace/<request_id>/`
- 用户可见产物：`/root/simready_output/<request_id>/`
- Isaac Streaming：火山 ML 平台 `di-20260508104954-x5gr2`
