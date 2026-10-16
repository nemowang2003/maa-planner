# maa_desktop_planner

这个包负责 Windows 桌面版 MAA。与 `maa_cli_planner` 使用独立配置：

- CLI：`~/.config/maa_cli_planner/config.toml`
- Desktop：`~/.config/maa_desktop_planner/config.toml`

在 Windows 上，`~` 指当前 Windows 用户目录。先运行 `uv run maa_desktop_planner init`，再把 `[[profiles]]` 改成实际安装目录。示例：

```toml
[runner]
update_resource = true
run_directly = true
exit_self = true
# hook = "my_planner:build_patch"

[[profiles]]
name = "nemo"
path = 'C:\Users\nemo\workspace\Apps\MAA\profiles\nemo'
channel = "stable"
architecture = "x64"

[[profiles]]
name = "wyf"
path = 'C:\Users\nemo\workspace\Apps\MAA\profiles\wyf'
channel = "stable"
architecture = "x64"
```

每个 profile 都有私有的 `config` 和 `data`。多个同版本、同架构的安装可以把 `resource`、`cache`、`externals` 链接到共享目录。资源检查和更新按链接解析后的实际目录去重，因此共享 `resource` 只更新一次。

## 运行流程

运行 `uv run maa_desktop_planner`（或显式加 `run`）后：

1. 检查所有目标 `MAA.exe` 和 `MAA.Updater.exe` 是否运行；有目标进程就报错。
2. 用 `MaaCore.dll` 的 `AsstGetVersion()` 查询已安装版本，并与 MAA 官方 `version/summary.json` 比较。发现新软件版本时输出相应架构的**完整包地址**；暂不自动安装。内置 OTA / 完整包更新不应直接处理共享的 `externals`。
3. 检查各个 `resource/version.json`；默认下载 MaaResource 的 `main.zip`，仅合并包中 `MaaResource-main/resource/` 的文件，不删除旧文件。MAA [官方手动更新说明](https://github.com/MaaAssistantArknights/MaaAssistantArknights/issues/10033)指出资源包是增量内容。
4. 计算每个 profile 的临时配置，保留 `gui.new.json.maa_desktop_planner.bak` 备份。无论 hook 返回什么，启动期间都关闭 MAA 内置的检查、下载、安装更新。`run_directly=true` 设置当前配置的 `Gui.StartUpSettings.RunDirectly`；`exit_self=true` 设置 `Gui.PostActions=ExitSelf`。
5. 逐个启动所有 `MAA.exe`（启动后并行运行），等待全部进程退出。即使启动失败或有进程返回非零退出码，也终止剩余进程并逐字节恢复各自的 `gui.new.json`。

如果 Python 被强制结束而没有执行清理，停止全部目标 MAA 后运行 `uv run maa_desktop_planner restore`。存在未恢复备份时，`run` 会拒绝再次启动。

`run_directly` 让 MAA 启动后直接执行当前任务队列；`exit_self` 让任务完成后的 PostAction 退出 MAA。这两项可以在 `[runner]` 关闭。每个 profile 原有的模拟器连接设置及任务队列仍由其自身的 `gui.new.json` 决定。

## 规划扩展点

`src/maa_desktop_planner/planner.py` 的默认 `build_patch(profile, gui_config)` 当前返回空字典。可在 `[runner]` 设置 `hook = "module:function"`，让自用 Python 函数返回递归合并到临时 `gui.new.json` 的 JSON 对象。函数收到 `Profile` 和当前配置的副本，可针对不同账号生成任务队列或连接参数。备份与恢复仍由 runner 统一处理；hook 不应直接修改磁盘文件。安全更新开关和上面的启动、退出选项在 hook 之后应用。

## 辅助命令

```powershell
uv run maa_desktop_planner check-software
uv run maa_desktop_planner plan-software --profile nemo
uv run maa_desktop_planner check-resource
uv run maa_desktop_planner update-resource
```

软件安装的完整自动流水线仍待实现：下载并校验完整包、解压到新目录、保留每个 profile 的 `config` 和 `data`、重建共享链接、切换目录并提供回滚。不要在旧目录直接覆盖；旧文件可能需要被清理。软件版本检查、完整包定位与资源更新逻辑已经写在 Python 中。

参考：[MAA 桌面版资源更新实现](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/a3c6005/src/MaaWpfGui/Models/ResourceUpdater.cs)、[桌面版软件版本检查](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/a3c6005/src/MaaWpfGui/ViewModels/Dialogs/VersionUpdateDialogViewModel.cs)。
