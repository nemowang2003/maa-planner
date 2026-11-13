# 原生 Core 执行后端

程序运行于原生 Windows，使用同一 Python 环境的 daemon 和直接加载 Core 的 worker。Windows 仅支持 MuMu，固定使用 MuMuEmulator12 连接并启用增强截图。Linux 的模拟器环境及交互方式尚不明确，因此暂不支持 Linux 运行；WSL 不支持运行 daemon。

## 执行边界

daemon 负责排期、统一依赖准备、实例互斥、准入与取消；worker 负责单个实例的生命周期，并在自身进程中通过 ctypes 加载官方 MaaCore.dll。一次执行只有 daemon → worker 两层，不再启动额外的 host。任务以 Core 的 type/params 为输入，唤醒、换班与普通作战复用 Core；所有普通 Fight 在计划固定及 worker 提交时移除 times、固定 series=0，保留用药与源石参数；剿灭 Fight 在 worker 中展开为项目的全权委托 Custom 图。

自动日程不启动 MuMu；显式 run 可按需启动。前台手动游戏和 GUI 占用仍受保护。纯 Copilot 要求游戏和模拟器已经运行。不同实例可以并行，没有全局执行数量上限。Windows 文件锁支持多个读者共享资源与写入维护互斥。

Windows 虚拟环境可能使用解释器转发进程；daemon 启动通过进程关系确认就绪，不能假定启动器 PID 就是实际解释器 PID。daemon 在 worker 报告实际 PID 与创建时间后，通过 Job Object 取得其进程树所有权，随后才允许准备和运行任务。关闭所有权句柄会结束 worker 与遗留的 Core 子进程。MuMuManager 和共享 ADB 服务通过显式 breakaway 独立于该 Job，避免被异常清理连带关闭；普通结束仍由 worker 关闭自己启动的模拟器。

## 配置与资源

配置节为 `[maa_core]`：可指定安装 directory、外部 resource_dir、adb_path 和 touch_mode；connection_preset 若填写只能为 MuMuEmulator12。无平台切换或版本锁定。旧 `[maa_cli]` 不兼容，需手动迁移；用户原配置不会被自动修改。

每个新准备单元查询 Core 最新稳定 Release，下载官方包、验证实际加载版本，再检查公共资源与热更新。已有准备的使用者继续共享固定版本；失败不回退旧版本。不再下载、安装、探测或启动 maa-cli。

任务、基建文件和作业固定到不可变请求。每次执行在私有目录物化附件，创建指向共享基础 resource 的私有链接，加载同一份热更新 resource/tasks.json；不再运行 CLI 导航 HTTP 代理，不改写共享任务定义。外服资源按相应 client_type 加载。

options 可传 static_options（cpu_ocr、gpu_ocr）和实例选项 touch_mode、deployment_with_pause、adb_lite_enabled、kill_adb_on_exit、client_type；禁止 kill_adb_on_exit=true。不支持非空 CLI behavior 选项，会明确报错；ReportRequest 原始回调保留供诊断，尚无额外的自动上传适配。

增强截图配置使用绑定的 MuMu 安装路径与实际实例编号，避免错误连接其他账号。Core 的 FastestWayToScreencap 回调必须确认 MumuExtras；未确认时拒绝提交任务，运行期间切回普通方式时停止。启用增强截图不代表启用增强触控，默认继续 MiniTouch。

## 结果与诊断

Core 原始消息及 JSON payload 顺序保存为 callbacks.jsonl。task_results 按 AsstAppendTask 返回的 task_id 关联任务名和类型，状态为 pending、running、completed、error、stopped，另记录 subtask_errors。TaskChainError 还记录 error_evidence；task_checkpoints 保存本次 Core 计划任务索引、Core task_id、终态与错误回调引用，包括逻辑剿灭在 worker 内展开的 Custom。普通任务逐项提交，明确失败后记录错误并继续其余独立步骤，失败刷图关联的商店跳过；StartUp 失败、连接或增强截图异常及取消仍停止本轮。任务链 completed 不代表所有房间或业务目标成功；game_result 仍为 unknown。业务判断逐步根据结构化回调实现。

活动商店从结构化 SubTaskCompleted 识别 Underfunded／UnderfundedOCR／CheckUnlimited；代币不足为 insufficient_currency，明确开始并完成的商店任务遇到无限商品时，按约定布局判断为 completed，其他情况 unknown。无终态证据不推测清空，不再解析 Core 文字日志。Core 目前未通过结构化回调报告所有商品名称识别跳过，不能将该项诊断声称为已具备。

执行结束后，将 callbacks.jsonl、core-result.json 和核心／宿主日志归档到状态目录 logs/<attempt>/，结果 log_directory 指向归档位置。任务结果经过 worker 管道持久写入结构化日志，终态退出 SQLite；operation.get 从日志查询终态结果。daemon 与 worker 通过私有标准输入／输出 JSONL 通道通信；Core 的原生 stdout 重定向到 worker.log；子进程继承空 stdin 和诊断 stdout，避免读取私有控制通道或混入协议。取消或父通道 EOF 请求 Core 正常停止并保留模拟器现场；原生调用卡住时由 daemon 的独立超时监督终止拥有的进程树。异常退出保留恢复记录与模拟器现场，不自动重跑。无法确认退出时保留恢复隔离与现场，禁止自动重放。

旧 CLI 恢复记录仍参与互斥检查，必须人工核对迁移，不能由 Core 恢复器直接删除。已完成的旧执行结果可以继续查询；旧未完成的执行请求不转换为新任务重跑。

日常 Core 步骤错误的检测、告警和延后恢复见[公共恢复排期](daily-step-retry.md)。恢复由 daemon 安排新 attempt，worker 不自行重放整条日常；显式 run／copilot 操作及崩溃中断不自动加入该队列。

## 每次运行的自有任务资源

完整任务 JSON 可以包含 `resource_tasks`，内容为 Core tasks.json 的节点对象；项目节点必须使用 `Planner@` 前缀，可以通过 `baseTask` 复用 Core 节点。节点固定在不可变请求中，worker 将其写入该次运行的 `task-resources/resource/tasks.json`，在基础、外服和热更新资源之后加载。相邻账号的资源视图与共享文件不受影响；这些节点不会注册新的 C++ 插件。`Planner@Annihilation…` 用于 worker 自动生成的剿灭资源，后于用户私有节点加载，应避免同名。

日程任务可指定 `resource_file = "processing-nodes.json"`，文件内容同为节点对象，在准备阶段读取一次。多个任务共用同名同内容节点可以合并；同名不同内容明确报错。`Custom.params.task_names` 是候选入口，不是顺序阶段列表；顺序流程通过节点 `next` 串联，或配置多个独立 Custom 任务。

剿灭 Fight 在 worker 中展开成专用 Custom 任务，节点写入该次运行的 `annihilation-resources/resource/tasks.json`，不修改共享资源。复用 Core 的 `UsePrts-AnnihilationCheck` 与 `UsePrts-AnnihilationSuccessCheck`，每次行动前确认全权委托激活；结算返回后重新检查。私有链封闭 next／sub／exceededNext／onErrorNext，不进入普通代理的 UsePrts／StartButton2 分支。支持默认剿灭入口及切尔诺伯格、龙门外环、龙门市区；默认入口仍依赖 Core 的终端 ToDoList 识别，未找到则结束该阶段并告警。

剿灭结果单独返回 annihilation_results。专用 Custom 的终止节点区分 copilot_required、skipped、incomplete 或 unknown；归约按已注册的剿灭 task_id、Custom 类型与 ProcessTask 完成回调关联节点。达到 times 上限不代表本周完成，未确认全权委托不等于已确认缺券。times 限制开始次数；Custom 没有 MedicineCounter 插件，当前用药和源石参数必须为 0，非零参数在准入前拒绝，不悄悄忽略。没有明确任务链错误的业务终止由该阶段处理后继续日常；若专用 Custom 收到 TaskChainError，记录失败并继续其余独立日常，本轮结束后由公共日常恢复排期接管。连接异常与取消仍终止本次执行。保留旧 Fight StageDrops 额度回调的结果解析兼容，但不再用它写本周完成标签。

列表式 Copilot 的逐项结算证据在 copilot_results 中返回，必须同时关联列表项、文件、关卡、难度与结算节点。日程自动搜索固定启用，仅在当天存在普通 Fight 与开放活动时触发。daemon 的自动搜索先固定选中的集合、Skland 拥有判断及干员组选择；活动自动通关的 worker 不联网搜索或重读账号数据。自动模式的普通区 support_unit_usage=0、EX／S 通常为 1；全员拥有且可复用的一摆到底集合为 0。首个实际目标成功后同集合后续目标关闭自动编队，策略固定在私有请求中。日常的 activity_copilot 阶段在同一 worker、Core 实例和互斥范围内逐关执行，阶段结束后，以同版本本次规划时已开放关卡的 required 清单、原有账本及本轮有证据的成功判断 farming_gate；缺少完整普通／突袭／教学及区域拖尾剧情认证（不含中间剧情）或 catalog_complete 为假时，跳过所有后续普通 Fight 及其附属商店，继续其他日常。该规则独立于商店入口配置，不因作业集完成推断全活动完成；理智不足、三连败或未知战果未全通均适用。task_results 只记录已提交的 Core 任务，跳过原因和缺项由 activity_copilot_results.farming_gate 返回；阶段报告继续保留诊断证据；自动通关、刷图和活动商店共同归属一个刷理智业务，并允许后续独立日常继续；但 warning.core_error 表示明确 Core 执行错误时，外层状态为 failed，不因后续任务成功变成 succeeded。调度器随后重新规划整个失败的刷理智业务，重新读取认证账本，跳过其余已成功的独立任务。成功记录和阶段告警经私有协议即时交回 daemon 写入账本，worker 不写 SQLite。同区战败／二星重试使用 Core 自有结算退出任务及就绪回调，不清空区域缓存重新返回首页；退出无法确认时停止。首败不新增告警，三次明确失败后结束的策略保持。导航与重试的完整诊断在回调日志和 core-result.json 中保留，见 [自动通关](daemon-copilot.md#日常自动通关)。

专用链的识别依据为 [Core 6.21.3 资源](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.3/resource/tasks/tasks.json)。静态检查覆盖普通代理不可达、结算后门控和次数上限；离线 Core v6.20.1 验证了四种入口的资源加载与 Custom 注册（未连接模拟器）。这些验证不证明目标新版 Core 的真实游戏行为。

**回调格式适配**：[AbstractTask::callback](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.3/src/MaaCore/Task/AbstractTask.cpp) 将 details.task 裁成最后一个 @ 后的节点名。剿灭归约使用已注册任务上下文，兼容裁剪后的节点及完整名称；仅将 SubTaskCompleted 中的 NoProxy／OrdinaryProxy／Unavailable／InsufficientSanity／Limit／Uncertain 作为终止证据。其他任务 ID、任务链、子任务类型、未完成节点与未知节点不会提供剿灭终止证据。

日常 activity_shops 在普通 Fight 后独立检查完整活动通关资格，结合本轮 Copilot 新成功，导航后调用 SS@Store@Begin；结果经 activity_shop worker 消息即时发送 daemon，并在最终 RunResult.activity_shop_results 中保留。连接／取消按原执行保护处理，普通导航失败与购买未知告警后继续日常。商店证据在作战有效期内按账号／活动／范围／黑名单持久化，后续任务失败不撤销；垃圾时间另须材料库存达标，材料不足不重复购买已换完商店。

作战截止即结束活动策略，daemon 立即清理带明确截止的商店决策状态（包括负面和未知结果），不等待领奖期；通关认证、执行结果及历史事件保留。私有 JSON 与 PNG 附件均已固定到请求并按次加载，确定性候选制作和离线回放见 [资源工具](resource-tools.md)。

自动请求默认不授予模拟器启动许可，停止的实例在 runner 中返回 blocked；底层 MuMu 启动前再次检查，防止准备期间关闭实例后被自动重启。显式 run API 允许启动，正常结束仅关闭本次启动的实例；取消保留现场。

商店首次导航失败采用 [全账号停用策略](daemon-copilot.md#商店首次导航失败后停用)：daemon 保存按活动期次共享的首次结果，worker 导航前查询最新许可，首次探测互斥。navigation_failed／navigation_unverified 的首次失败仅 admin 告警，后续账号跳过；未尝试、取消和代币不足不触发该停用。SQLite 版本 12 增加 activity_shop_navigation；schema 13 增加 scheduled_retries 保存公共恢复排期；当前 schema 14 将历史结果、事件、解除告警和终结排期转存结构化日志，SQLite 仅保留当前状态。

日程任务的 `resource_images = { "Planner@Entry.png" = "entry.png" }` 将相对于配置文件的 PNG 在准备阶段固定到请求附件。显式任务 JSON 用 `resource_images: {"Planner@Entry.png":"@file:entry.png"}` 和 base64 字符串附件；模板节点引用 `Planner@Entry.png`。文件上限 2 MiB、边长 4096、总像素 4194304，检查块结构与 CRC；最终图像解码由 Core 完成。图片写入每次运行的 `task-resources/resource/template/`，与私有 JSON 同层加载。源文件变化不会改变已有请求，禁止覆盖共享模板名。

活动任务奖励通过带 activity_reward 标记的项目 Custom 任务执行，标记在提交 Core 前移除；只透传私有入口节点。worker 按任务 ID、Custom／ProcessTask 回调以及标题→控件→点击→Received 的顺序关联证据，单纯任务完成不构成已领取。activity_reward_results 随 RunResult 保存，日程写入 activity_reward_result 事件。收到明确回执后，后续 Core 错误不会抹除该次领取证据。

剿灭 expert 的 annihilation_copilot 只固定当期轮换地图与拥有清单；现场 NoProxy 后才搜索该关，私有固化所选作业及哈希，结算／代理复核及恢复委托见 [补救流程](daemon-copilot.md#剿灭-expert-无代理补救)。验证节点图没有普通战斗开始分支，只有现场选中并保存到本次私有目录的 Copilot 作业可以实际开战，脚本随日志归档。补救成功不会保存为周额度完成。
