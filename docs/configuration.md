# 配置与目录边界

当前验收进展：

完整的多账号 expert 示例见 [example.toml](example.toml)。`maa-planner daemon config example` 仅输出最小运行配置；完整示例需要从文档复制并填写实际账号与文件。模板不会隐式合并到用户配置。

## 用户配置

| 配置 | 用途与默认值 |
| --- | --- |
| mumu.path | MuMu 安装目录；操作模拟器时需要 |
| profiles[].name | 必填的唯一账号标识；至少一个 profile |
| profiles[].emulator_name | MuMu 实例名，默认 name |
| profiles[].auth_name | 森空岛认证文件名，不含 .json，默认 name |
| profiles[].character_name | 可选角色昵称；省略选择首个绑定角色 |
| profiles[].enabled | 默认 true；false 拒绝新执行 |
| profiles[].gui_path | 可选人工 GUI 目录，只用于实际运行进程检查 |
| maa_core.directory | 可选安装根目录，默认由 platformdirs.user_data_path 推导，见下方存储表 |
| maa_core.resource_dir | 可选外部共享资源；省略使用对应安装内的资源 |
| maa_core.adb_path | 可选覆盖；默认使用 MuMu 自带 ADB |
| maa_core.connection_preset | 只能为 MuMuEmulator12，默认即此配置；必须启用增强截图 |
| maa_core.touch_mode | 可选触控方式，默认 MiniTouch |
| skland.auth_dir | 可选，默认 `~/.config/skland-api/auth`；复用认证而非复制凭证 |

默认安装子目录、可执行文件、Core 库、临时工作目录和热更新目录均由 daemon 推导。新配置不接受 executable、data_dir、library_dir、navigation、work_dir、任何版本字段，或 runtime_install、services 配置节。缓存目录使用平台默认缓存位置，无需为 APK 单独配置目录。未知字段明确报错。

相对路径相对于配置文件；`~` 展开到用户目录。只使用宿主原生路径，不支持 WSL 转换。路径配置应指向目录，不能只提供一个 Core DLL。

## 日常排期

配置中的 `[[schedules]]` 由默认 DailyPolicy 执行。省略 schedules 时不产生日常任务；[example.toml](example.toml) 定义 nemo、minepig、wyf、zy 四个 profile 和七个日程，日程均先设为 `enabled = false`，填写真实文件路径和参数后再启用。程序不隐式读取 GUI 配置，也不为未配置的账号补充刷图、用药或任务。

| 示例时刻 | 参与账号 | 参数与任务的区别 |
| --- | --- | --- |
| 02:40 | 四个账号 | 共用凌晨奖励、仓库任务 |
| 12:10 | nemo | expert 基建、剿灭 expert、普通 Fight expert（后备 1-7）、信用、公招、奖励、主题和退出 |
| 12:10 | minepig | 固定三赤金、CE-6；不做库存选图，活动期仍采集干员以自动通关 |
| 12:10 | wyf、zy | 共用 CA-5 材料任务 |
| 15:40 | 仅 wyf | expert 使用自己的四份文件及不同基建参数，作战改为 AP-5 |
| 19:40 | 四个账号 | 共用晚间公招、奖励；公招次数、刷新及奖励开关与此前不同 |
| 23:40 | 仅 nemo | 邮件、干员及仓库数据更新 |

共享上线时刻不要求共用日程：中午三个日程分别描述不同账号的任务。一个日程中的所有 times 使用同一套参数，因此同账号每次参数不同时，像 nemo 的中午与夜间一样使用不同日程 id。expert 日程各自提供对应账号的四份原生基建文件，固定策略与 expert 可以在不同账号、不同日程中分别使用。

| 日程字段 | 含义 |
| --- | --- |
| id | 必填、稳定且唯一的字母／数字／下划线／连字符标识，最长 80 位 |
| profiles | 必填、非空、无重复，引用已定义的账号；停用账号不参与排期 |
| times | 必填、非空、无重复的 HH:MM 列表，固定 Asia/Shanghai，每天循环 |
| enabled、weekdays | enabled 默认 true；weekdays 默认为 1 至 7，表示上海自然日的周一至周日 |
| tasks | 按数组顺序执行的任务；每项提供 type、可选 name、params、enabled、weekdays |
| execution_timeout | 单次 worker 执行超时秒数，默认 7200，不包含之前的 daemon 依赖准备时间 |
| options | 可选 Core static_options、instance_options；不接受非空 CLI behavior；禁止 kill_adb_on_exit |
| client_type | 可选，日程与会话默认客户端 |
| session | 可选，startup／closedown 参数对象；daemon 在每次尝试的业务首尾编排 Core 会话任务，不作为普通业务恢复。tasks 只包含业务任务，首尾会话由 session 声明 |

任务的 `params` 使用 Core 参数名，以不可变 JSON 固定到本次请求中；不读取 GUI 的启用开关或隐藏字段。静态任务的 enabled／weekdays 过滤在生成计划时完成；worker 仍执行固定的自动通关／商店阶段并检查实时资格和业务证据，但不在战斗途中重新选择 Fight 关卡。所有任务都未启用或不适用于当天时，本次记录为 skipped，不准备环境、不启动 runner。完整任务参数参考 [Core 集成协议](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.1/docs/zh-cn/protocol/integration.md)。示例的普通作战使用不同的明确关卡及恢复额度，统一省略 times、固定 series=0；剿灭 Fight 的 strategy=expert 判断本次 Skland 本周进度，未完成下个 batch 重试，不保存尝试或 Core 完成标签，普通 Fight 默认固定执行，显式启用 expert 后按下文活动规则选图。

剿灭 Fight 在 worker 内转换为全权委托专用 Custom 链，每次行动前确认激活，不能转普通代理。times 仍为上限；当前 medicine、stone、expiring_medicine、medicine_expire_days 必须为 0。终止回调已按注册任务上下文适配 Core 裁剪后的节点名，详见 [执行后端](core-backend.md#每次运行的自有任务资源)。

基建任务默认 `strategy = "fixed"`，可提供 `file = "base/两赤金.json"`；基建 expert 用于 `type = "Infrast"`，必须在 tasks.files 中恰好提供 normal、three_gold、orundum、sell_only 四份文件。各账号文件或其他参数不同时，分别配置日程；同配置版本下同刻到期仍共用本批公共准备。未配置加工的固定基建任务自身不触发 Skland 库存请求，也不调用基建库存切换；同一日程中其他 expert 或默认自动通关仍可能请求数据。

原生基建文件通过 `file` 或 `files` 指定；不要在 params 中填写 filename。准备时只读取选中的文件，并固定为请求附件，Core 收到 mode=10000、filename 和 plan_index。任务的 `plan_index` 默认 auto：按原生 plans 的 period 选择子计划，使用上海当前分钟，结束分钟包含其全部秒数，支持跨午夜区间；匹配缺失或多个方案会明确失败。单方案无 period 时使用 0；也可以明确填写从 0 开始的方案序号。

expert 从 Skland 仓库原始物品 ID/数量、制造站产物和贸易站策略识别当前四种 243 模式，从源石碎片配方 costs 识别固源岩或装置。每次请求最新返回的数据，不把上次计划或进程退出码当成模式已经应用。库存字符串数量会校验并转为整数；数据缺失、布局不属于四种模式、配方不明或时间戳格式异常均使本账号本次失败，不影响其他账号。player_info.status.storeTs 是游戏状态的时间，不等同于 HTTP 获取时间；允许它落后数小时或更久，不设置最大年龄。旧示例中的 max_snapshot_age 字段已删除，使用旧副本时应移除该字段。请求失败不会以旧本地缓存冒充本次成功获取。规则见 [基建 expert](configuration.md)。

启动或接纳新配置时只安排未来时刻，不补跑此前的日程；正好位于计划时刻时可以执行。运行中延迟醒来只取最近一个尚未过期的时段，不重放积压。同实例在 daemon 内繁忙时，本次日常等待空闲，至该日程下次上线时刻过期；外部 GUI 占用、Skland 认证或依赖准备错误沿用执行保护；它们不是已经执行的 Core 步骤错误，不因此自动排入恢复队列。游戏内登录失效若使 Core 任务链报错，则适用下述公共恢复。

daemon 等待下一次日程／待执行任务期限、已知活动作战截止或状态事件，没有固定每分钟协调。配置更新重新计算未来时间；尚未开始的旧配置任务失效，运行中的任务继续使用已固定参数。每次安排以“日程 id、账号、计划时刻”形成稳定标识并持久化状态，重启不会重复执行已处理的安排，运行中断的安排标为 recovery_required 而不自动重放；明确失败后已经安排、尚未开始的恢复排期可跨重启保留。单次推迟／跳过及临时日常已通过控制 API 接通；周期安排仍遵守上述 revision 规则，已接纳临时日常使用固定配置及附件。账号暂停通过 daily.pause 持久控制新的周期日常及自动恢复派发，不改 TOML，已准备／运行工作继续；手动日常仍可使用。

日常 Core 步骤收到明确 TaskChainError 后，继续其余独立任务，本轮结束后通过 task_retry 告警并安排[按业务任务重新规划](daily-step-retry.md)，默认间隔 5／10／15 分钟、最多三次，不能越过本日程下一次执行时刻，活动有效期在重新规划时检查。恢复先准备游戏会话，再重新规划失败及未执行的业务任务；刷理智整体重新判断自动通关、选图和商店，重新搜索作业并读取认证账本。已成功的独立任务不重跑，每次执行内仍固定私有任务、附件及资源。用药与源石额度按每次尝试生效。该间隔目前是内置策略，没有对应 TOML 配置字段；通过 [daily.run](control-api.md#单次日常控制) 可手动提交一次完整临时日常，其恢复以显式 expires_at 为界，不代表重试某个历史失败。商店首次导航失败停用策略优先，不自动重试商店。

## 指定干员加工

基建任务可显式增加 `processing`；省略时完全不执行加工。配置只用于 `Infrast`，随其 enabled／weekdays 及业务恢复范围生效；加工阶段结束后仍执行原有 Core 基建任务，原换班参数及文件不变。

```toml
[[schedules.tasks]]
type = "Infrast"
params = { facility = ["Mfg", "Trade", "Power", "Control", "Reception", "Office", "Dorm"] }

[schedules.tasks.processing]
operators = ["年", "芳汀", "九色鹿"]
targets = { T3 = 20, T4 = 10, "聚合剂" = 15 }
[schedules.tasks.processing.strategy]
"年" = ["聚合剂", "T3", "-炽合金块"]
"芳汀" = ["T4"]
```

- `operators` 按列表顺序使用，不自动替换为列表外干员；九色鹿产生 `infrast_processing` 告警并跳过，不制作其选人附件。
- `targets` 显式给出库存下限。这里按用户定义，`T3` 表示紫色、`T4` 表示橙色，对应游戏物品表的 TIER_4／TIER_5；具体材料名覆盖组目标，`0` 不补，未指定目标的材料不补。仅支持精英化材料，不合成技能书／基建材料，不互换芯片。
- `strategy` 可选，以列表中的干员名为键。允许项先取并集，再应用 `-材料名` 黑名单，与列表中的书写先后无关；材料名和 T3／T4 均可用于允许项。省略某干员表示允许全部已配置目标；空列表不允许任何配方；仅列黑名单时从全部配方中排除。策略允许不代表需要加工，仍必须存在库存缺口。轮到该干员时无可加工缺口则不选人，继续下一位。

以上示例中，聚合剂的目标为 15，其他橙色材料为 10，紫色材料为 20。年允许加工聚合剂和紫色材料，但不允许炽合金块；芳汀只允许橙色材料。九色鹿始终跳过。因此炽合金块即使存在库存缺口，也不会由这个列表中的干员加工；`targets` 定义目标，`strategy` 不会扩大目标范围。需要彻底停补某材料时，应在 targets 中将其设为 0，而不是只给某个干员加黑名单。

`operators` 非空时必须显式提供非空 `targets`；策略的键必须是 operators 中的干员。材料名在固定配方表中核对，拼写错误或不属于紫／橙精英化材料时明确报错。`T3`／`T4` 是本配置约定，不沿用游戏物品表的数字编号。

配方和稀有度来自同一游戏数据提交的 building_data.workshopFormulas 与 item_table.items，计划固定来源和配方。Skland 的 manufactureFormulaInfoMap 是制造站当前配方，不是完整加工表。开始加工时要求 Core DepotInfo 的 done=true 完整回调，材料缺项不按零处理；龙门币使用明确的 Skland 数量作预算，实际合成仍需游戏允许。橙色可加工缺口优先，然后补紫色；不递归制作下级原料。副产物不提前计入可用库存，下一次识别再采纳。

逐次要求目标图标匹配、加工数量为 1、无超负荷／缺人／原料不足证据，以及获得物品后回到加工站的回调；不调用自动选人的 MaterialSynthesis@Begin。无法选中、心情或原料不足、明确未解锁时跳过；导航／数量／结果未知则停止加工、将该基建业务记为错误，仍继续原换班。一般错误随基建业务重新规划恢复；取消、连接异常或掉线停止整轮，沿用公共恢复规则。每次恢复重新识别库存，不保存加工历史或阶段检查点，不另行恢复加工前的干员。结果写入该次日志中的 infrast_processing_results 和 Processing 私有结果文件。

公共数据准备失败产生 `infrast_processing_prepare` 告警，本次保留原换班；plan 预览展示阶段、准备错误及九色鹿警告，不触发这些业务告警。

## APK 自动更新与资源就绪

`game_update` 是独立的全局维护排期，默认启用，每日北京时间 11:00 执行，仅处理启用的账号。完整示例显式关闭，启用后生效：

```toml
[game_update]
enabled = true
time = "11:00"
poll_interval = 30
resource_timeout = 3600
```

| 字段 | 含义 |
| --- | --- |
| enabled | 是否启用自动排期，默认 true |
| time | Asia/Shanghai 的每日 HH:MM，默认 11:00 |
| poll_interval | 安装后的图标检查间隔，支持 10／30／60 秒，默认 30 |
| resource_timeout | 单个账号等待资源就绪的秒数上限，默认 3600，必须为有限正数 |

本项目按用户确认的固定假设判断当天是否需要检查：若发生停机更新，会连续占用北京时间 6:00–12:00。任一账号在这个窗口内新登录成功，说明当天没有这种停机更新，**全局跳过当天自动 APK 检查**。已有首页、黄色 START、单纯任务链完成不计；要求登录动作开始和随后首页识别都在该窗口内，且 StartUp 正常完成。开始时间取动作开始回调，避免把 6 点前开始、6 点后才结束的动作算入。该判断是项目产品假设，不是公告发现或官网维护时间的动态推断。

新登录成功立即通过 worker 回传，取消等待中的检查，不必等整轮日常完成；当天事实保存为单条当前状态，重启仍生效，午夜清除。其他实例关闭仍可能留有旧版本；按用户选择仍全局跳过，已有未完成安装／资源状态保留，显式 game.update 不受此豁免限制。该窗口只用于豁免，不自动禁用日常任务，普通失败仍沿用有限恢复。

没有配置 MuMu 或没有启用账号时不派发。启动或修改配置只安排随后时刻，不补跑已过时刻；同一时刻通过当前排期游标去重，重启不重复派发。到点后暂停新的自动日常及恢复派发，等待现有任务／准备及 API 操作结束，再取得原有维护独占；同刻日常排期保留，维护结束后继续。自动更新不启动停止的模拟器，关闭或 ADB 未就绪的实例记 deferred，下一次排期重新检查；显式 game.update API 仍允许启动实例。错误不加入日常 5／10／15 分钟恢复队列，可以发起新的显式更新或等待下一次排期。

继续使用官网 APK 的 Range manifest 比较已安装 metadata，落后才下载；同次更新共用一份 APK，安装后复核，制品在安装阶段结束时删除。进入安装前只保存当前未完成更新的账号及目标包名／版本，不保存下载 URL 或版本历史。确认资源就绪并完成收尾后删除这条状态；中断后新操作先读真实安装版本，已安装的 APK 不会因为尚未确认资源就绪而重装。崩溃中的操作不自动重放。

安装后通过原生 worker 在互斥与准入下启动游戏。私有 Custom JSON 任务复用 Core 的 GameStart 节点与 GameStart.png 黄色菱形 START 模板，将动作固定为 DoNothing、后续链清空；每隔配置的时间重新匹配，识别到后结束等待，再由 CloseDown 关闭游戏。没有完整 StartUp、登录、自动通关或日常业务。只有明确的资源下载提示才允许点击下载确认；未匹配到标记时继续等待，超时／取消保留未完成状态和现场，不提交关闭游戏任务。不根据进程、流量或文件体积推定就绪，也不根据任务链完成代替图标回调。

`daemon.status.game_update` 提供下一次时刻、等待中的时刻、维护 operation_id、login_exemption 和 resources_pending。账号资源错误使用 game_resources 告警，定时维护失败使用 admin 的 game_update 告警；恢复后解除。APK URL 与详细检查结果继续只在当前进程短暂保留，参见 [控制 API](control-api.md)。

## 安装与更新

daemon 管理原生 Windows 官方 Core 最新稳定版，不再依赖 maa-cli。Linux 的模拟器环境及交互方式尚不明确，因此暂不支持 Linux 运行。

每个新准备单元查询 Core releases/latest，安装到新目录并实际加载验证；下载进入 cache。既有安装也验证实际版本，网络或验证失败不回退旧版。旧安装清理仍待实现。

## 批次与复用

API execution.batch 的成员、policy 一次返回的多项请求共用一次准备：检查 Core、检查并按需更新资源、刷新热更新 JSON、解析同一份活动数据。并行数量不受全局额度限制；同一模拟器仍互斥。成员可以分别查询、取消；整体也可取消。

已有执行或准备中的任务时，新任务复用其准备对象，跳过全部 ensure；等待中的任务加入同一次准备。单次任务晚于上一次检查完成到达也不会再次检查，直到最后一个使用者结束。运行平台、安装目录或外部资源路径与活跃环境不兼容时明确拒绝，不更改正在运行的依赖。批次各成员即使准备时间不同也保留同一份结果；全部结束后下一次请求重新检查。

热更新数据的下载由 daemon 统一完成。planner 直接解析其中 StageActivityV2.json；Core worker 直接加载私有基础资源视图与同一份增量快照，不运行 HTTP 代理。日常任务、基建及每周剿灭 expert 已接入上述 schedules；普通 Fight expert 使用同一份活动快照选图，固定 Fight 保留显式关卡。

活动自动通关见 [Copilot](daemon-copilot.md#日常自动通关)。每关先普通再突袭，同关连续失败三次后告警并继续其余日常；阶段结束后本次规划时已开放关卡的账本未全通或清单未知时跳过普通刷图。自动通关固定启用，只有当天有启用的普通 Fight 且存在开放活动时才触发；纯领奖、基建或剿灭日程不会自动搜索。自动模式按区域优先“一摆到底”，普通区全员拥有、EX／S 每关最多缺一人并允许借助战；每次安排前分析本次 Skland 干员清单，数据或搜索失败跳过普通 Fight，继续其他可准备任务。只使用本地 Copilot／API 通关账本，不从 Skland 计数补记历史。补充关卡表缓存到 cache/stage-catalog，同一准备单元固定同一个游戏数据 commit，活动资格仍以共享 MAA 快照为准；关卡关系与来源边界见 [日常数据](activity-assets.md)。

## 存储归属

| 内容 | 位置／生命周期 |
| --- | --- |
| config.toml、用户基建计划与本地作业 | 用户配置或指定的源文件，不能当缓存删除 |
| 已安装 Core、实际资源 | platformdirs.user_data_path("maa-planner", appauthor=False) 下 runtimes，可由 maa_core.directory 覆盖 |
| 发布包、APK、作业响应、热更新 JSON、游戏表 | platformdirs.user_cache_path("maa-planner", appauthor=False)；APK 在 apk，每次更新的安装阶段结束立即删除；作业在 copilot，游戏表在 stage-catalog |
| SQLite、告警／账本／期限、恢复记录与日志 | state_directory()；默认应用 state 根下 daemon，可由 start/run 的 --state-dir 覆盖数据库／日志根 |
| 单次配置、附件、链接视图、Core 运行数据 | 安装根 tmp 下私有目录；确认安全退出后清理，恢复隔离未解除时保留 |
| 控制端点 | 应用 state 根下 control.json，保存 Socket.IO 回环地址与令牌；当前用户私有，退出时仅删除本代未被改写的描述文件，独占锁及 control.json.owner 归属记录保留 |

宿主默认根目录（未覆盖配置或环境时）：

| 根目录 | 原生 Windows |
| --- | --- |
| 配置文件 | `%LOCALAPPDATA%/maa-planner/config.toml` |
| data | `%LOCALAPPDATA%/maa-planner` |
| cache | `%LOCALAPPDATA%/maa-planner/Cache` |
| 应用 state | `%LOCALAPPDATA%/maa-planner` |
| 默认端点文件 | `%LOCALAPPDATA%/maa-planner/control.json` |

所有 platformdirs 调用显式设置 appauthor=False，禁用作者目录，避免应用名重复；appauthor=None 会回退到应用名，不能省去该层目录。state_directory() 支持显式 XDG_STATE_HOME，设置后应用 state 为该目录下 maa-planner。控制端点只监听 127.0.0.1 的动态端口，不随配置文件路径变化；--endpoint 可覆盖端点文件位置，但不改变用户级单实例锁。Windows 端点文件发布前设置当前用户独占 DACL。

默认 daemon 根为应用 state 下 daemon，保存 SQLite、daemon.log、workers、logs 和 recovery；用户级单实例锁在应用 state 根，资源锁在其 locks 子目录，不随 --state-dir 改变。Core 回调和日志在清理临时目录前转存 logs/<attempt>。异常退出或无法确认 worker 已退出时保留工作目录与恢复隔离，recover 核对后回收；当前没有定时目录清理器。

SQLite 只保存当前业务事实、未解除告警、正在执行／等待执行／恢复隔离的任务，以及仍被引用的配置快照。终结的 attempt、操作、日程和恢复记录先持久写入 `logs/state-history.jsonl`，随后退出状态表；事件直接写日志，不再建 SQLite 历史表。每个日程／账号只保留最近接纳时刻作为当前排期游标，防止重启重复执行。旧库升级到 schema 14 时先转存历史再清理，保留通关账本、商店停用及未完成执行；升级日志写入失败不删除对应状态。执行结果已落盘但 SQL 删除／提交失败时，重启按日志中的确定结果补清理 attempt、操作和终结排期，不重放游戏动作；没有确定结果的执行保持 recovery_required。

`operation.get`、`event.list` 和历史结果查询读取结构化日志；日志里的请求标识也用于已有接口的重复请求检查，不会因查询而把终态重新写回 SQLite。日志不自动按年龄删除；手动删除会失去相关历史查询与旧请求标识检查。日志末尾被硬中断留下的不完整记录会在下次读取前截去，中间损坏则明确报错。当前配置与执行依赖不是可随意清空的缓存；APK 仍在同次更新结束后按既有规则删除。

作业请求支持 ETag 重新验证，网络失败不静默使用旧作业。任务接纳后的内容固定，不在执行中重读作业站或源文件。活动作战截止会清理带明确期限的账号商店决策状态；通关认证保留在 SQLite，历史结果与事件只保留在日志，缺少期限的旧记录不猜测删除。这项业务状态清理不等于文件缓存垃圾回收。

## 内置约定

官方 URL、游戏包名、资源压缩包内的路径集中在 [constants.py](../src/maa_planner/daemon/constants.py)。公共活动数据不再通过 skland-api activity 模块获取；森空岛账号认证和个人数据继续使用 skland-api。

业务日历、排期和日期展示固定 Asia/Shanghai，不接受时区配置，不读取主机本地时区。期限和内部时刻使用 Unix 时间戳，超时使用单调时钟。API 日期携带 +08:00，拒绝无时区 datetime；上游活动时间尊重其声明的偏移，再转换为上海时间。

GUI 占用只按实际进程完整路径匹配 MAA.exe / MAA.Updater.exe；目录存在、其他目录的 GUI 或路径不可读取的进程不算已确认占用。GUI 打开但闲置仍视为可能操作模拟器。Core 独立于 GUI 安装；多个 GUI 更新器不遵守 planner 的锁，不建议只共享单个 DLL。

runtime.ensure 会确保最新版 Core，resource.prepare_and_check 在未配置外部资源时先准备 Core；两者可能下载安装，均使用维护独占。runtime.prepare 执行完整准备且可能更新基础资源。具体边界见 [控制 API](control-api.md)。

## 活动刷图 expert

普通 `Fight` 任务设置 `strategy = "expert"` 后，使用共享的 MAA 活动快照与本账号 Skland 仓库选图。与剿灭 expert 可以同时配置；未启用 expert 的作战任务保持固定关卡。

```toml
[[schedules.tasks]]
name = "理智作战"
type = "Fight"
strategy = "expert"
fallback_stages = ["1-7"]  # 可改为 ["AP-5", "1-7"]。
# chip_targets 默认：16 种可直接掉落的芯片／芯片组各 20；{} 禁用芯片保持。
params = { series = 0, medicine = 0, stone = 0 }
```

按作战结束时间从早到晚考察当前有效期内的 SideStory，逐个跳过已自动确认为垃圾时间的活动，选择首个未进入垃圾时间的活动；结束时间相同则沿用公共快照中的顺序。开始包含、结束不包含。自动垃圾时间要求材料库存达标与商店换完同时成立，见下文；不把作业集完成等同于全活动完成。没有这样的活动时进入芯片库存保持，再考虑后备关卡。仅支持官服/Bilibili。

所有普通 Fight（expert、固定日程和显式 run）均不设置 times 次数上限，并固定 series=0，让 Core 自动选择连战倍率。旧配置中的 times 会在固定计划与 worker 提交时移除，series 会被覆盖为 0；不要用 times=0 停用任务，日程停用应使用 enabled=false。剿灭 Custom 的 times 尝试上限、公招次数及日程的 times 时刻列表独立保留。

从 MAA 推荐的普通材料图中排除[材料黑名单](activity-assets.md)，选择库存严格小于 100 且库存最低的材料，库存并列选推荐列表中的第一个。没有候选时从“搓玉效率”推荐中取数值最高的关卡，效率并列同样选第一个，并保留原始描述。推荐图数量不固定，不要求恰好三个。库存请求与整个 items 解析必须成功；只对成功结果中缺失的单个 ID 按 0。普通选图不要求提供基建布局或周剿灭进度。

缺少普通材料图、缺少搓玉推荐或效率数值无效时报规划错误，不猜测缺失数据；这会使该账号本次安排准备失败，其他账号继续独立准备。普通刷图按账号已解锁且可代理、导航成功的前提执行，本轮不增加这些情况的自动恢复策略。若本轮插入了自动 Copilot，阶段结束后本次规划时已开放关卡的账本仍有未认证关卡（包括理智不足未全通），则跳过所有后续普通 Fight，继续其余日常；即使作业集跑完也须核对完整普通／突袭／教学清单，清单未知也跳过。该检查不依赖商店配置，详见 [日常自动通关](daemon-copilot.md#日常自动通关)。

非活动与垃圾时间的选图顺序为：

1. 在今日开放芯片本的两种可能掉落中，取 `目标数量 - 当前库存` 最大的正缺口，选择可掉落该芯片的关卡。两种掉落分别比较，不相加；闭关芯片的缺口不阻止回退。默认 16 种芯片／芯片组各 20，不包含需要制造的双芯片。缺口并列按目标表顺序选择。
2. 没有今日可补的芯片缺口时，按 `fallback_stages` 从前往后选首个开放关卡。默认 `["1-7"]`；`["AP-5", "1-7"]` 表示 AP-5 开放时选 AP-5，否则选 1-7。建议末尾保留常驻图；当天所有候选都关闭时报规划错误。支持主线编号、物资筹备及 PR 芯片关卡，不把未知活动关卡当成常驻图。

`chip_targets` 可用物品 ID 映射覆盖整张目标表，例如 `{ "3261" = 20, "3231" = 20 }` 只保持这两种芯片；空表禁用，0 表示该项无需补。仅接受可直接掉落的芯片／芯片组 ID 和非负整数。`fallback_stages` 为非空且不重复的列表。两者仅用于普通 Fight expert。旧配置没有该列表但提供 params.stage 时，兼容使用该关卡作为单项后备；两者都没有时默认 1-7。

通过现有 `config.get` 读取配置，再用 `config.replace` 提交修改后的完整 TOML（建议携带 expected_revision），即可调整上述键；已接纳计划保留原快照，不会被新配置改写。库存表请求／解析失败仍明确失败，成功库存缺失单个 ID 才按 0。开放日按上海 04:00 换日，活动资源全开放窗口覆盖周历；这不证明账号已解锁或可代理。

expert 选择 stage；普通 Fight 统一移除 times、固定 series=0，用药、源石和其他 Core 参数保留配置原值。每个普通 Fight expert 在本次日程准备时固定一次选择，明确允许超过库存目标，不自动添加 drops 提前停止条件，也不在战斗途中换图；下次日程才重新观测选图。省略 times 使用 Core 默认次数上限，理智与恢复额度仍会约束执行；series=0 表示自动连战倍率，见 [Core 协议](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.3/docs/zh-cn/protocol/integration.md)。`plan` 返回 farming 决策；实际日程保存 farming_decision 事件。示例不自动启用到真实账号。

## 活动商店与自动垃圾时间

商店入口由项目按游戏活动 ID 维护，随计划快照固定版本；当前内置 TL「炮火所掩埋的」。日程不再接受 `shop_entries`，旧配置需删除该字段。未知活动保留入口缺失状态，不猜测文字或点击位置。购买黑名单使用独立的 `shop_blacklist` 商品名列表，默认模组增补仪、模组增补条、家具零件，不复用活动刷材料黑名单。以下示例不会自动启用到真实账号：

```toml
[[schedules]]
id = "daily"
profiles = ["nemo"]
times = ["12:10"]
enabled = false
shop_blacklist = ["模组增补仪", "模组增补条", "家具零件"]
garbage_material_threshold = 100
# garbage_material_targets = { "31113" = 100 }  # 可选的逐材料阈值覆盖；0 表示豁免。
# 后面照常配置 tasks；需要至少一个普通 Fight，官服或 Bilibili。
```

商店和领奖入口从解包素材的固定路径自动生成，包含基础和 `_02` 变体；使用掩码模板与源图推导的相对点击位置，不配置活动专有标题或屏幕坐标。商店就绪检查使用自动选取的横幅细节。准备阶段固定 PNG 附件，缺少或校验失败的资源明确报错，不修改共享 Core 资源或解除本期商店停用。

领取按钮 ROI 也不需要配置：先确认任务页，全屏发现“一键领取／键领取”后自动缓存区域，下次优先复用，未命中则回退全屏。没有按钮正常结束，不生成空 ROI 或全部奖励已领完标记。详见 [自动制备与缓存](activity-reward-roi.md)。

在每个实际执行的普通 Fight 结束后检查商店资格；项目尚无入口的活动不会尝试导航或购买；自动通关未全通而跳过 Fight 时，其附属商店阶段也不触发。全活动范围来自与自动通关共用的同一版本游戏表：activity 的区域映射、zone 的开放窗口、stage 的全部普通关及 hardStagedId 指定的突袭；教学及区域拖尾剧情也在清单内；中间纯剧情关不进入认证清单。只有所有区域已开放、关卡清单完整，且所有目标均有当前账号的本地 Copilot／API／拖尾观看认证记录，才执行导航和购买。本轮 Copilot 刚认证的成功立即参与资格检查；作业集覆盖不完整、尚未开放区域或缺失数据都不能提前通过。教学若未被 Copilot 认证，可用 stage.confirm 逐关人工确认；中间剧情导航交给 Core，不要求认证；拖尾剧情由显式观看任务取得目标定位、进入、跳过和返回证据后自动登记 source=story，也允许单关 API 登记，不从 Skland 计数自动补记。

导航可从已识别的活动区域直接进入商店；需要时再经首页／终端进入活动。活动及商店入口使用固定解包路径自动生成的模板，商店 ready 使用自动提取的横幅细节，确认页面后才提交 SS@Store@Begin。使用私有资源节点，不改共享 GUI 文件。导航失败、入口缺失及未知购买结果保存告警并继续后续日常；底层连接错误或取消仍按执行器原有规则处理。

Core 必须明确报告商店开始、遇到 CheckUnlimited 且任务链完成，才保存商店已换完证据；代币不足表示未换完，其他结果保留未知。证据即时回传 daemon，不等整套日常完成；后续领奖失败不会丢失已确认的商店证据。商店换完后不再重复购买；只有材料条件也满足，下一轮普通 Fight expert 才进入芯片保持／后备选图，本轮已选关卡不变。材料未达标不会阻止满足完整通关资格后的购买。此规则沿用“有限目标商品排列在无限池之前、黑名单商品主动放弃”的约定，不声称 Core 已能报告所有商品名 OCR 跳过。

状态在活动作战期间按账号、活动 ID、完整目标范围和购买黑名单隔离；复刻、新增关卡或黑名单变化需重新核对。每次规划仍验证当前开放条件与通关账本，并重新检查 api.maa.plus 的全部推荐普通材料：非刷图黑名单材料库存均须至少 100，刷图黑名单材料固定豁免。默认阈值由 garbage_material_threshold 指定，可用 garbage_material_targets 按物品 ID 覆盖；阈值必须为非负整数，黑名单材料不能设置正阈值。推荐材料清单为空或无法观察库存不判达标，不要求恰好三个。库存下降可退出垃圾时间，但不撤销商店证据、不重复购买。旧 garbage_activities 只兼容空列表，非空手工标记会报错，不能绕过双条件。plan.activity_shops 展示完整目标、缺失目标、eligibility、shop_completed、materials（库存、阈值与豁免）、materials_complete 和 garbage；activity_shop_error 表示准备失败。结果与持久化告警可通过现有 events／alerts 查询。自动阶段仅覆盖活动作战有效期；作战截止后不安排领奖期补购。

## 每周剿灭 expert

仅当 Fight 任务显式 `strategy="expert"` 且 stage 为 `Annihilation` 或 `…@Annihilation` 时启用。当前仅支持官服/Bilibili；每个日程最多一个此类任务，必须明确指定正整数 times 作为单次上限。执行时展开为全权委托专用 Custom；当前不支持用药／源石，相关参数必须为 0。建议放在普通理智作战之前，并在每个可重试日期的日程中保留该任务，不要再只限定 weekdays=[1]。

按上海时间周一 04:00 重置。在重置后的首个配置上线时刻尝试；普通活动不延后。本次 Skland 确认本周额度完成后跳过。技术失败、周额度未满或缺少周进度证据都不算完成，下一个 batch 重新取得 Skland 并判断；不限制同一天重试。配置中没有后续上线时刻就不会凭空生成新的常规日常；明确的 Core 步骤错误仍可在当前日程有效期内安排公共恢复，且使用本次固定计划，不把恢复当作重新采集周进度的新 batch。运行中断的任务不因重启补跑。

例外仅是“原活动持续三周、现在复刻”的 SSReopen，并且本次复刻作战在本周结束：等待活动结束后的配置上线时刻，不在活动结束前提前兜底。用户确认作战于北京时间 04:00 结束，结束后至少留有完整一个游戏日；日程需保留之后的上线机会。历史原活动通过已有 Skland 响应关联，不能仅凭 SSReopen 标签或本次复刻时长判定，见 [历史活动数据](activity-assets.md)。

Skland campaign.reward 的 current/total 只有在 storeTs 属于本周时才能证明本周完成；跨周旧进度表示未知，库存的 storeTs 规则不变，也没有任意最大年龄限制。Core 的旧额度回调仅作为历史执行结果兼容解析，不写周完成标签。剿灭完成／尝试日期均不单独持久化，旧 weekly_annihilation 表在 schema 11 删除。

结果通过 annihilation_results 返回 incomplete／copilot_required／skipped／unknown，带时间及回调引用；completed 只兼容旧执行结果。计划通过 annihilation_decision 事件说明执行或暂缓原因。任务链成功及达到次数上限均不等于周额度完成。不保存影响下次调度的补救标签；自动识别新剿灭关卡、搜索和插入合适作业仍待实现，目前可使用已有 copilot API 指定链接补救。

## 活动结束与状态清理

商店正面、代币不足等负面和未知决策记录都只服务当前活动，在确认作战截止已到时立即清理，不额外延长保留期。活动结束只看全部作战截止，不看商店／领奖截止；到期或离线后重启会检查已知窗口并清理上述临时决策状态，通关认证与历史仍保留。没有活动时不准备自动 Copilot，普通 Fight expert 按芯片／后备规则选图。

## 主题切换

任务类型 `SwitchTheme` 使用 Core 的 `params.themes`，放在领取奖励之后、退出之前：

```toml
[[schedules.tasks]]
name = "切换主题"
type = "SwitchTheme"
params = { themes = [] }  # 按游戏内已解锁主题名填写；空列表跳过。
```

[Core 6.21.3 协议](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.3/docs/zh-cn/protocol/integration.md) 要求 themes 为名称列表，多个候选每次随机选一个。

当前接受的流程是在唤醒前采集／统计 Skland，固定基建与作战决策后再执行日常，末尾不刷新 Skland。唤醒与采集并行仅为可选后续优化，不是当前完整流程的必做前置。配置顺序为信用后公招，非自动重排规则。独立 OperBox／Depot 任务仍可使用，但不是 Skland 采集的替代。

商店入口私有任务按 [首次导航结果](daemon-copilot.md#商店首次导航失败后停用) 保护：本期活动第一次实际导航失败即停用所有账号的商店尝试，仅 admin 告警，跨重启和入口配置变化保持，到作战截止结束。更新项目导航资源不自动重试；该状态不等于商店换完，不影响刷图和其他日常。

## 活动任务奖励

项目从游戏数据表和固定解包路径自动准备当期活动任务入口及页面锚点，在同轮第一个 Award 或 CloseDown 前增加私有领取任务（两者都没有则置于末尾）。它独立于刷图／全通、商店停用和垃圾时间，不提供新的账号开关；缺少必要数据或素材时明确报告准备问题。

该任务先用游戏数据表的任务描述核对任务列表页面，再核对一键领取控件，再点击并等待获得物资弹窗、复用 Core 的弹窗关闭节点。已确认任务页但没有领取按钮时正常返回 nothing_to_claim，不视为失败或永久完成，下次仍重新检查。明确页面与点击／回执链才记录 claimed；未开放／未完成为 unavailable／incomplete，缺少证据或无回执为 unknown。claimed 只证明本次获得奖励，不证明全部里程碑已完成，也不参与垃圾时间资格。窗口失效时不提交任务。普通 Core 导航失败仍使用现有任务错误恢复；一键领取后无回执不会被伪装为业务成功。

剿灭 expert 已接入无代理时的作业补救，不需要新增配置字段；仅处理当期轮换图，准备阶段固定地图／拥有清单；现场明确无代理后才搜索作业并做干员预检，全员拥有且地图唯一才开战。固定／显式剿灭计划默认仍为纯委托。详见 [补救边界](daemon-copilot.md#剿灭-expert-无代理补救)。

## 缓存自动清理

[cleanup] 配置默认启用APK 闲置 1 天、下载归档 3 天、其他可重建缓存 3 天的保留规则，以及同平台架构最新两版 Core。只在空闲维护屏障下清理；冻结请求、恢复现场、历史日志、已发布快照及未知目录保留。可配置或关闭，详见 [保留规则与结果](cache-cleanup.md)。
