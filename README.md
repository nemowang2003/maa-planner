# maa-planner

本仓库提供两个独立的 Python 包和两个命令：

- `maa_cli_planner`：根据森空岛数据规划 `maa-cli` 任务，当前决策层暂留空。
- `maa_desktop_planner`：检查 Windows 桌面版 MAA 更新，临时规划各副本的 `gui.new.json`，并行运行后恢复原配置。参见 [Desktop 使用说明](docs/windows-desktop.md)。

两套配置分别位于 `~/.config/maa_cli_planner/config.toml` 和 `~/.config/maa_desktop_planner/config.toml`。

## maa_cli_planner

当前实现聚焦于：

- 读取 `skland-api` 的本地认证文件。
- 通过 `AuthInfo.full_auth()` 完成认证。
- 手动调用 `skland_api.modules.*` 拉取当前活动、仓库材料、理智、公招、基建状态。
- 为后续专家系统保留独立决策层。
- 可选输出 `maa-cli` 的 `profiles/*.toml`。

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

当前专家系统仍留空，所以执行 `plan` 时不会写出任务或 profile 文件；建议先用：

```bash
uv run maa_cli_planner plan --dry-run
```

查看各个 `modules.*` 的采集结果摘要。

如果配置了：

```toml
[planner]
write_profile = true
```

并补全 `[profile]`，在专家系统返回非空任务后才会额外写入：

```text
~/.config/maa/profiles/<profile_name>.toml
```

## 当前流程

现在的职责拆分是：

- `skland_client.py` 负责认证、选角、`CharacterInfoLoader.full_load()` 和 `modules.*` 调用。
- `expert.py` 只保留一个空的规划接口，后续由你手写规则。
- `maa.py` 继续负责把规划结果写成 `maa-cli` 的 TOML。

## 开发验证与当前边界

```bash
uv sync --dev
uv run pytest -q
uv run ty check
uv run pre-commit run --all-files
```

CLI 已完成认证、选角、六个状态模块采集及 JSON 摘要输出。专家规则仍是明确保留的扩展点，示例中的材料权重和任务参数暂不参与决策。

Desktop 已完成版本检查、完整包地址定位、资源更新、临时配置、并行运行和异常恢复；程序完整包的自动安装与回滚尚未实现。桌面进程和恢复逻辑有自动化测试，仍需在真实 Windows / MAA 环境验收。

`prts作业站query.txt` 是作业站查询的手动参考，尚未接入规划流程。
