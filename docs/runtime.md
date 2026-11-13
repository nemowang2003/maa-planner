# daemon 运行与配置

当前验收进展：

主程序只管理 daemon 生命周期和离线配置；业务操作通过 [控制 API](control-api.md) 发起。daemon 与独立 Python Core worker 均运行在原生 Windows，只支持 MuMu 并启用增强截图。Linux 的模拟器环境及交互方式尚不明确，因此暂不支持 Linux 运行；WSL 不支持启动 daemon。详见 [执行后端](core-backend.md)。

## 单一配置

默认路径：`%LOCALAPPDATA%/maa-planner/config.toml`。可以用 `maa-planner daemon --config PATH start` 指定其他文件。

```powershell
uv run maa-planner daemon config example > .\planner-example.toml
uv run maa-planner daemon config validate
uv run maa-planner daemon config dump
```

config example 输出最小配置；多账号、共享与专属时段、每次不同参数和 expert 基建的完整示例位于 [docs/example.toml](example.toml)。示例文件不会自动覆盖现有配置。文件通过操作系统原生事件监听，直接修改或原子替换均会触发加载。API config.get 返回原始 TOML、实际生效配置、revision 与错误；config.replace 提交完整 TOML，可携带 expected_revision 检查版本。已有显式任务使用旧快照，不因配置修改而取消。无效文件保留有效快照并拒绝新业务请求。

```toml
# 运行配置默认位于 %LOCALAPPDATA%/maa-planner/config.toml。
# 不需要每个账号各自安装 MAA GUI。
[mumu]
path = 'C:\Program Files\Netease\MuMu'

[[profiles]]
name = "nemo"
# emulator_name = "nemo"  # 默认为 name
# auth_name = "nemo"  # 森空岛认证文件名称，默认为 name
# character_name = "角色昵称"
enabled = true
# gui_path = 'C:\MAA\profiles\nemo'  # 可选：只用于防止与手动 GUI 同时操作

[maa_core]
# 可选覆盖安装根目录；内部路径由 daemon 管理。
directory = 'C:\MAA\core-runtime'
# 可选：与 GUI 共用现有资源；省略则使用 daemon 自己的资源。
# resource_dir = 'C:\MAA\share\maa-resource\resource'
connection_preset = "MuMuEmulator12"
touch_mode = "MiniTouch"

# 森空岛认证目录可省略，默认复用此位置。
# [skland]
# auth_dir = "~/.config/skland-api/auth"
```

运行路径的完整字段见 [配置项与 Core 共享](configuration.md)，完整日程模板见 [example.toml](example.toml)；官方在线来源由程序内置。不执行 WSL／Windows 路径转换；相对路径相对于配置文件目录。每个账号按 `emulator_name` 或默认的 `name` 匹配唯一 MuMu 实例，ADB 地址由模拟器查询。`enabled=false` 的目标不接受显式任务。`gui_path` 不参与 GUI 配置读写，不要求安装 GUI。

旧配置不能直接改名沿用：请先备份，按当前模板将旧 `runtime_install.directory/platform` 合并为 `maa_core.directory`，仅保留可选资源和连接设置，删除已移除的内部路径、版本和缓存字段。旧规划配置中的 `[users]` 应合入相应 `[[profiles]]` 的认证映射；`[planner]`、默认刷图、公招、材料权重、长期 profile 输出等未生效字段已删除。不存在两份配置的隐式合并或自动回退。日常上线时间和任务通过 [[schedules]] 配置。

## 生命周期

```powershell
uv run maa-planner daemon start
uv run maa-planner daemon status
uv run maa-planner daemon restart
uv run maa-planner daemon stop
```

`start` 是一次性启动命令：创建独立会话的后台进程，输入关闭、输出写入状态目录的 daemon.log，确认 API 和有效配置就绪后返回 PID 等状态。重复启动会报错，跨端点或状态目录也不能绕过用户级单实例锁。start/run 可指定 --endpoint、--state-dir；status/stop/restart 只接受 --endpoint（stop/restart 另接受 --force），通过端点文件发现并认证连接已有 daemon，不隐式启动新实例。run 是内部前台执行入口，供启动器与诊断使用。

不同模拟器可并行执行，无全局任务数量上限；同一模拟器仍互斥。

普通 stop/restart 停止接收新操作，排空已接纳的后台操作和 worker；运行中的任务不会被主动取消，已接纳批次尚在准备／准入中的成员继续完成；新的操作被拒绝。尚在等待的 daily.run 临时预约持久保留，不让排空等待到预约日期；已开始准备／执行的临时日常按合作式收尾结束。restart 完成清理、关闭 SQL 和控制服务 后，重新启动 daemon 解释器进程（Windows 不保证 PID 保持不变），不需要外部服务管理器。配置无效时拒绝重启；排空过程中配置变为无效则取消重启并保留 daemon。

`--force` 请求取消执行任务并保留模拟器现场；内部前台 run 可用 Ctrl+C。取消只停止 Core，不发送模拟器 shutdown；已经执行的 CloseDown 等游戏动作不能撤销。正常完成仍会关闭本次经显式请求启动的模拟器。Windows 的外部强杀不等同于合作式取消，优先使用控制 API。已经开始的维护仍等待安全完成，不能把线程取消当成 APK 安装或文件发布已经停止。原始控制 RPC 返回接纳状态，CLI 随后等待原进程退出；restart 还等待新实例配置与控制服务就绪，PID 不变也通过 started_at 区分新代次。--timeout 可显式限制等待秒数，默认持续等待，超时不撤销已接纳请求。

SIGINT／SIGTERM 及 Windows SIGBREAK 请求停止并保留模拟器现场；force 和可捕获信号写入 daemon_interrupted 事件，为当时执行／准备中的 worker 保存 execution_interrupted 告警。重复停止不重复产生事件；告警作为中断记录保留，不因 daemon 重启自动解除。父控制通道 EOF 同样让 worker 停止 Core 并保留模拟器。

Windows 默认状态根目录为 `%LOCALAPPDATA%/maa-planner/daemon/`，包含只保存当前状态的 SQLite、daemon.log、logs、workers 和 recovery；用户级单实例锁及资源锁在其应用父目录。显式 XDG_STATE_HOME 可覆盖应用状态根目录，start/run 的 --state-dir 只覆盖该实例的数据库／日志目录，不改变用户级锁。默认端点文件为应用 state 根下的 control.json，包含仅本机可访问的 Socket.IO 地址及随机令牌，每次启动重新发布；文件权限仅授予当前用户。Windows 默认路径见 [存储归属](configuration.md#存储归属)。 端点目录及祖先路径不得包含符号链接或 junction；同目录 control.json.owner 保存归属摘要，启动持有固定端点锁后才允许替换本程序留下的旧描述文件。没有归属证据或内容已被替换时保留原文件并拒绝接管，可改用明确的新 --endpoint。关闭也只删除本代发布且内容未被改写的描述文件；归属记录与固定锁保留。异常退出不自动重放操作。明确 Core 步骤错误的[自动恢复排期](daily-step-retry.md)已实现；账号暂停和选择性手动重试见 [控制 API](control-api.md#暂停与历史失败重试)，其余维护排期尚未实现。

日常任务通过 [schedules](configuration.md#日常排期) 配置上线时间、有序任务和 Core 参数。daemon 等待最近业务时刻或状态事件，到期后共用环境准备，分别交给已有 runner；没有 schedules 时不自动生成日常。示例包含静态 Core 任务组合和 expert 基建，默认未启用；它不代表完整目标日常已经实现，需要填写真实文件与账号。日常单次运行／推迟／跳过及账号暂停、选择性重试 API 已实现，见 [控制 API](control-api.md)。自动日程／策略不启动模拟器：worker 发现实例停止时返回 blocked，本次日程结束，不因后续唤醒重放；下次安排重新检查。即使准备期间实例关闭，实际启动前仍拒绝 launch。显式 run 和 game.update API 可以启动模拟器；copilot 要求模拟器和游戏已经运行。

日常任务错误恢复是公共机制，适用于所有已配置的 Core 任务类型，并非 Copilot 专用。会话首尾由 daemon 根据日程 session 编排，恢复 attempt 先执行 StartUp 准备游戏会话，再从未完成步骤继续；已成功的独立日常任务不重放，失败业务整体重新规划；自动通关、刷图、活动商店同属刷理智业务，通关状态只读认证账本。其余独立任务继续尝试，本轮结束后默认依次延后 5／10／15 分钟，最多三次；status.scheduled_retries 和 task_retry 告警提供状态。取消、blocked、recovery_required 及缺少明确任务错误的未知结果不自动重放；商店首次失败停用规则保持。显式 run／copilot 操作没有这套自动恢复排期。

APK 自动维护独立于 schedules，默认每天北京时间 11:00，可在 game_update 修改或停用；到点后暂停新的自动日常与恢复，已有工作完成后执行，不启动已关闭模拟器。资源阶段使用私有 Custom 轮询黄色 START 图标，确认后关闭游戏，不执行登录或日常；显式 game.update 仍可启动实例。配置、超时和未完成状态见 [维护排期](configuration.md#apk-自动更新与资源就绪)。

## Windows 登录会话托管（可选）

`daemon service` 提供同一登录用户会话内的托管选项。MuMu 依赖用户会话，SCM 服务与桌面之间存在 Session 0 隔离，因此此入口使用 Task Scheduler 的 InteractiveToken、最低权限和当前用户登录触发器，不要求管理员或保存密码。SCM 服务＋会话代理未实现。微软说明见 [服务交互](https://learn.microsoft.com/en-us/windows/win32/services/interactive-services)。

先在原生 Windows 环境使用 `uv sync`，确认配置和手动运行环境有效；命令中的 `--config` 必须放在 service 之前，自定义 endpoint／state-dir 每次管理时保持一致：

```powershell
uv run maa-planner daemon --config 'C:\path\config.toml' service plan
uv run maa-planner daemon --config 'C:\path\config.toml' service install
uv run maa-planner daemon --config 'C:\path\config.toml' service start
uv run maa-planner daemon service status
uv run maa-planner daemon service restart --timeout 120
uv run maa-planner daemon service stop --timeout 120
uv run maa-planner daemon service remove
```

plan 只输出任务名称和 XML，不注册或启动。install 校验配置、注册或更新任务，不立即启动；已运行的任务须先安全停止再更新。任务名称按当前用户 SID 固定，同名任务属于其他 endpoint 时拒绝覆盖或删除。任务固定当前 Python 环境、配置和路径，更新部署位置或虚拟环境后需重新 install；不自动执行 uv sync 或下载依赖。status 返回安装、任务状态和最近退出码，实际 daemon 就绪状态使用 daemon status 查询。

start 启用任务并显式启动，等待控制端点和有效配置就绪，默认 15 秒，可用 --timeout；超时不撤销启动。stop 先禁用后续登录／失败拉起，再通过既有 API 排空并等待退出；--force 请求取消 Core，维护仍等待安全完成。控制端点不可用且任务仍运行时明确失败，不调用 Stop-ScheduledTask 强杀。start 会重新启用此前被 stop 禁用的任务；remove 只删除已停止任务。非托管 daemon 使用原有 daemon stop／restart 管理。单独 daemon.stop 只停止本次托管进程，不禁用下一次登录自启。

任务直接监督前台 `service run`，不调用脱离监督的 daemon start；托管 restart 清理旧代次后在同进程构造新 daemon，PID 保持，started_at 改变。非零退出配置每分钟重启、最多三次，正常 stop 不触发失败恢复；没有运行时间上限，不因切换电池自动停止。任务设置依据 [Task Scheduler schema](https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-schema)。重新启动进程沿用恢复隔离，不重放未知游戏动作；明确等待中的自动恢复仍按原期限和配置检查。

日志写入 state-dir/daemon.log。Windows 控制台关闭、注销及关机通知尝试进入现有中断收尾并保留模拟器现场；系统可在短时限内强制结束进程，加载 GUI DLL 时注销／关机控制台通知也可能不可达，因此不能保证收尾完成或即时告警。需要可靠停止时提前用 service stop，硬中断仍由下一次启动隔离未知执行。边界见 [Console handler](https://learn.microsoft.com/en-us/windows/console/setconsolectrlhandler) 和 [系统时限](https://learn.microsoft.com/en-us/windows/console/handlerroutine)。

## 维护边界

维护通过命名 API 的 game.inspect、game.update、resource.prepare_and_check、resource.update、runtime.ensure、runtime.prepare、recover 方法发起。当前没有终端业务客户端；协议与参数见 [控制 API](control-api.md)。

维护由 daemon 发起，worker 不检查或更新其他账号。runtime.prepare 会执行完整依赖准备，包含可能的资源写入；runtime.ensure 也可能安装 Core，不能当成纯只读探测。未配置外部 resource_dir 时，resource.prepare_and_check/resource.update 会先确保托管 Core 已准备。写入维护需要 daemon 内没有执行或准备中的操作；维护期间拒绝新业务操作，状态和结果查询仍可用。锁仍根据真实模拟器、可选 GUI 路径和共享资源目标加锁，能与其他本机调用互斥。

资源更新沿用增量合并，不删除旧文件，最后写版本标记。写入前完整校验归档并拒绝重复路径；本次会覆盖的旧文件先备份，任一文件写入失败时恢复已替换文件、删除本次新增文件和目录，随后仍让本次准备失败，不回退旧 Core 执行。操作前仍应确认所有外部使用者退出。每个新的准备单元检查 Core 最新稳定版，在安装根目录准备独立版本目录并供本批 worker 使用；有活跃任务时复用其环境、跳过 ensure；不覆盖运行中的版本，也不逐次重写全局配置。网络或验证失败不使用旧版本执行，详见 [版本检查流程](configuration.md#安装与更新)。

官服 APK 每次跟随官网 latest URL，用有预算的 HTTP Range 读取 manifest，与已启动实例的实际 metadata 比较。纯检查不启动模拟器，不回退到整包下载；需要安装时才下载单份 APK、校验、执行 `adb install -r` 并复查，不降级、不卸载、不清数据。不保存 APK 版本或安装历史；控制接口中的 metadata 结果也只在当前进程短暂保留。一次显式更新期间所有账号共用单份 latest.apk，在制品锁内完成整轮处理后立即删除；安装失败或取消也删除，重试时重新比较实际版本并按需下载。历史遗留的 latest.apk 可在本次更新中按 manifest 复用，随后删除；纯检查不清理安装包，进程硬终止遗留文件由后续显式更新处理。

确认过期的旧游戏允许强制停止并安装，即使 GUI 仍打开。APK 自动维护使用可配置每日排期，默认北京时间 11:00；到点暂停新的自动日常和恢复，等待已有工作结束。自动不启动关闭实例，显式 game.update 可启动。安装后由私有 Custom 默认每 30 秒匹配黄色 START，确认后关闭游戏，不走登录流程。按固定 6:00–12:00 停机假设，窗口内任一账号的新登录成功可全局跳过当天自动检查；当天事实跨重启保留、午夜清除，已有未完成更新记录保留。详情见 [排期及资源就绪](configuration.md#apk-自动更新与资源就绪)。

后续工作的状态与优先方向见 [后续规划](configuration.md)。企业微信独立入口为 maa-planner-wecom-client，配置和离线检查见 [企业微信客户端](wecom-client.md)；不会随 daemon 自动启动。Server酱只读入口为 maa-planner-serverchan-client，见 [Server酱客户端](serverchan-client.md)。

旧 runtime_install、game_update 节和 maa_cli 配置、版本字段已移除；保留会校验失败。安装、缓存、临时目录与批次语义见 [配置边界](configuration.md)。

## 缓存维护

后台缓存清理复用现有独占维护，不启动游戏或重放任务；默认下载归档闲置 3 天、可重建缓存闲置 3 天后清理，旧 Core 保留同平台架构最新两版。未完成恢复会暂停清理。配置、保护范围及事件字段见 [缓存自动清理](cache-cleanup.md)。
