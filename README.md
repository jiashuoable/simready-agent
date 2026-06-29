# SimReady Agent

发图即得 SimReady USD —— 通过飞书发一张物体参考图，端到端拿到带物理参数、可直接进 Isaac Sandbox 跑仿真的 USD 资产。

## 项目状态

当前是 v1 的工作快照。**接手前请先读 [IMPLEMENTATION_STATUS.md](IMPLEMENTATION_STATUS.md)**，那里详细写了哪些能力已经跑通、哪些只是空架子、哪些是已知坑。

产品需求见 [PRD.md](PRD.md)。

## 仓库结构

```
simready/
├── pipeline.py                # 主流程编排 + 飞书消息封装
├── feishu_watcher.py          # 日志驱动的图片侦测常驻进程
├── seed3D/                    # Seed3D 调用 + 校验修复编排
│   ├── seed3D.py
│   └── validate_and_repair.py
├── content-agents/            # submodule (NVIDIA-Omniverse/content-agents)
├── omni-asset-cli/            # submodule (songshen06/omni-asset-cli)
└── usd-simready-inspector/    # submodule (songshen06/usd-simready-inspector)
```

## 快速开始

### 拉代码
```bash
git clone --recurse-submodules <repo-url> simready
cd simready
# 或者拉过 clone 之后再
git submodule update --init --recursive
```

### 装依赖
```bash
pip install requests volcenginesdkarkruntime
# 每个 submodule 自己的依赖按其 README/pyproject 来装
```

### 配置环境变量
```bash
cp .env.example .env
# 填 ARK_API_KEY 和（可选）FEISHU_APP_ID / FEISHU_APP_SECRET
export $(cat .env | xargs)
```

### 跑
常驻 watcher（推荐）：
```bash
python feishu_watcher.py
```

直跑单张图（开发/调试）：
```bash
python pipeline.py /path/to/image.jpg --request-id demo-001
```

## 已知缺口

最关键的三个 v1 缺口（详见 IMPLEMENTATION_STATUS.md §3 / §4）：

1. **配置卡片回调**：UI 渲染了 3 个 checkbox 但点了没人接，等同于摆设
2. **VLM 物理推理 Stage**：PRD §4.3 整节没实现
3. **Isaac Sandbox 链接**：`use_sandbox` 开关无实际效果
