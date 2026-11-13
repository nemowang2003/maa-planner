# maa-planner

面向原生 Windows 和 MuMu 的多账号 MaaCore 日常规划与执行程序。daemon 负责配置、排期、共享依赖、私有资源、任务互斥和恢复；独立客户端通过 Socket.IO 控制 API 查询和发起任务。

当前执行平台只支持原生 Windows，要求 MuMuEmulator12 增强截图。WSL／Linux 可用于开发测试，不能运行真实游戏 daemon。程序不会隐式读取 GUI 任务，也不会自动启用示例账号或日程。

## 安装与启动

需要 Python 3.14、uv 和兼容的 skland-api 源码。项目通过 uv 的路径依赖引用相邻的 `../skland-api`；该依赖版本须支持 `binding_list(app_code="arknights")`，发布前应先完成依赖库发布。

```powershell
uv sync
uv run maa-planner daemon config example
uv run maa-planner daemon --config 'C:\path\config.toml' start
uv run maa-planner daemon status
uv run maa-planner daemon stop
```

先填写账号认证、MuMu 绑定与所需文件，再启用日程。最小配置由 config example 输出；多账号与 expert 完整示例见 [example.toml](docs/example.toml)。

## 能力与边界

- 配置驱动的多账号日常、共享准备、单实例互斥及冻结执行快照。
- 基建库存策略、手动进入搓玉、指定干员加工、剿灭与普通刷理智 expert。
- 按活动区域选作业集并逐关执行，普通与突袭独立认证；中间剧情由 Core 导航，区域拖尾显式观看并凭证据记账。
- 活动领奖、商店资格与兑换、材料库存及商店证据共同决定垃圾时间。
- 日程推迟／跳过、临时日常、账号暂停、历史失败选择性重试，以及失败业务重新规划与退避恢复。
- 可选 Windows 登录会话托管、APK 更新排期、安装后的资源就绪检查，以及缓存自动清理。
- 企业微信控制与通知客户端、Server酱只读通知客户端。
- 显式公告采集、活动素材准备、确定性资源制作及离线回放工具；公告模型解析与自动资源发布尚未实现。

Core 任务链结束不等于游戏业务成功。通关、商店完成和资源就绪均依赖对应证据；未知状态不当作成功，也不自动重放未知游戏现场。新空闲准备检查最新 Core，活跃任务使用其固定版本；保留旧安装不代表允许自动回退。

缓存默认 APK 闲置 1 天、其他可重建缓存闲置 3 天后清理，同平台架构保留最新两版 Core；冻结请求、恢复现场、日志及已发布素材快照保留。详见 [缓存自动清理](docs/cache-cleanup.md)。

## 文档

| 主题 | 文档 |
| --- | --- |
| 配置、账号、expert 与目录 | [配置说明](docs/configuration.md) |
| 控制方法与协议 | [控制 API](docs/control-api.md) |
| 运行、托管与维护 | [运行指南](docs/runtime.md)、[托管 Core](docs/managed-runtime.md) |
| 架构与执行 | [daemon 架构](docs/daemon-design.md)、[Core 后端](docs/core-backend.md) |
| 自动通关与恢复 | [Copilot](docs/daemon-copilot.md)、[业务恢复](docs/daily-step-retry.md) |
| 通知客户端 | [企业微信](docs/wecom-client.md)、[Server酱](docs/serverchan-client.md) |
| 素材与工具 | [素材准备](docs/activity-assets.md)、[最小素材包](docs/asset-upload-selection.md)、[资源制作](docs/resource-tools.md) |
| 领奖识别 | [ROI 自动制备](docs/activity-reward-roi.md) |

## 开发检查

```powershell
uv run pytest -q
uv run pytest -q tests/native
uv run pre-commit run --all-files
uv build
```

默认业务测试使用隔离状态与模拟 Core；原生适配验收分别在 Linux、Windows 执行。普通检查不启动真实游戏、消耗理智、兑换或发送通知，实际游戏操作需要明确授权。
