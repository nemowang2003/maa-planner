# maa-planner

本仓库提供两个独立的 Python 包和两个命令：

- `maa_cli_planner`：根据森空岛数据规划 `maa-cli` 任务，当前决策层暂留空。
- `maa_desktop_planner`：检查 Windows 桌面版 MAA 更新，临时规划各副本的 `gui.new.json`，并行运行后恢复原配置。参见 [Desktop 使用说明](docs/windows-desktop.md)。

两套配置分别位于 `~/.config/maa_cli_planner/config.toml` 和 `~/.config/maa_desktop_planner/config.toml`。

## maa_cli_planner

当前实现聚焦于：

- 读取 `skland-api` 的本地认证文件。
- 拉取当前活动、仓库材料、理智、公招、基建状态。
- 用硬编码规则挑选活动关卡或默认关卡。
- 输出 `maa-cli` 的 `tasks/*.toml`，可选输出 `profiles/*.toml`。

## 安装

```bash
uv sync
```

依赖直接指向本地仓库 `/Users/nemo/skland-api`。

## 初始化配置

```bash
uv run maa_cli_planner init
```

默认会写入：

```text
~/.config/maa_cli_planner/config.toml
```

示例配置里默认读取 `skland-api` 的认证目录：

```text
~/.config/skland-api/auth/
```

也就是复用你现有 `skland auth add <name>` 生成的 JSON 文件。

如果一个账号绑定了多个角色，再手动补：

```toml
[users.main]
auth_name = "main"
character_name = "你的角色昵称"
```

未填写时默认选择该账号下的第一个明日方舟角色。

## 生成任务

```bash
uv run maa_cli_planner plan
```

默认输出到：

```text
~/.config/maa/tasks/daily-<user>.toml
```

如果配置了：

```toml
[planner]
write_profile = true
```

并补全 `[profile]`，还会额外写入：

```text
~/.config/maa/profiles/<profile_name>.toml
```

## 规则

当前“专家系统”比较简单：

- 活动关卡按 `materials` / `users.<name>.materials` 权重选最高分。
- 没有命中活动掉落偏好时，回退到 `defaults.fallback_stage`。
- 公招存在空位或可刷新次数时，追加 `Recruit` 任务。
- 无人机达到阈值或存在疲劳干员时，追加 `Infrast` 任务。
- 始终生成 `StartUp`、`Fight`、`Award`。

后续可以继续扩展成更细的策略，例如按周常进度、理智即将溢出、材料库存下限、活动剩余时间等动态调参。
