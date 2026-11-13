# daemon 即时 copilot

启动 daemon 后，通过控制 API 提交普通在线作业或作业集：

```sh
uv run maa-planner daemon start
```

API 请求示例：

```json
{
  "method": "copilot.run",
  "params": {
    "url": "prts://s56648",
    "select": [
      3,
      5,
      7
    ],
    "formation": false,
    "ignore_requirements": true,
    "idempotency_key": "nemo-tl-subset",
    "profile": "nemo"
  }
}
```

通过 API operation.get / operation.cancel 查询或取消。当前没有终端业务客户端；未来由企业微信进程生成这些消息。

formation、ignore_requirements 默认 false。关闭编队时沿用游戏中已有编队、技能和模组选择。`use_sanity_potion` 是可选布尔值，默认 false；显式 true 时允许 Core 在理智不足时吃理智药，例如 `params={"url":"prts://123","use_sanity_potion":true}`。不启用源石恢复。此参数不是药品数量预算；Core 6.21.3 的 Copilot 用药计数上限内部为 999999。下述自动通关启用用药，同关连续失败三次后停止该阶段。单份 URL 使用 `prts://ID`，作业集使用 `prts://sID`。

`is_raid` 为可选布尔值，默认 false；设为 true 时请求切换突袭难度，对本次选中的全部作业生效，不混合普通与突袭。例如 `params={"url":"prts://123","is_raid":true,"formation":true}`。提交普通与突袭作业应分别使用不同 idempotency_key；同一 ID 改变难度会报参数冲突。省略与显式 false 等价，不会为未启用的选项改变规范化参数。

start_at 和 select 是集内从 1 开始的位置，不是关卡编号，两者互斥。select=[7,3] 仍按原顺序执行第 3、7 份。重复、空选择、非法或越界序号均报错；只下载选中的作业，不自动插入 ST/TR 前置关卡。

## 条件和状态

目标模拟器及官服游戏必须已经运行、ADB 可读；否则提交直接报错，不下载作业、不启动 worker、模拟器或游戏。配置了 gui_path 时，对应手动 GUI 必须退出。同一实例繁忙时不排队，不同实例可并行执行。

运行中的游戏可以被接管，但仍需人工准备正确界面。单份作业通常从编队选择界面开始，作业集从同一区域地图开始；进程检查不保证界面、关卡解锁或编队正确。runner 在启动前和最终准入后再次检查游戏进程。

突袭单作业会生成仅含一项的 `copilot_list`，也需要准备同一区域地图或 Core 可导航的关卡详情界面；普通单作业保留原 `filename` 入口。[Core 6.21.1 的 Copilot 参数实现](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.1/src/MaaCore/Task/Interface/CopilotTask.cpp) 仅在列表项上读取 is_raid，[列表执行器](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.1/src/MaaCore/Task/Miscellaneous/MultiCopilotTaskPlugin.cpp) 在导航后尝试切换突袭。false 不主动切回普通难度；调用方仍须准备正确模式。本参数不证明关卡支持突袭、已经解锁或作业适用，也不支持以此表达主线磨难／险地难度。

daemon 先加入或创建统一依赖准备，再下载并固定作业附件，使用准备后的资源解析关卡，验证后才启动 worker。准备期间取消或请求过期会阻止启动；配置变化不影响已接纳的快照；缺少资源或无法唯一识别关卡会明确失败。默认启动窗口 60 秒（依赖准备耗时除外），显式期限使用 expires_at；不因窗口到期中断已经开始的战斗。

同 idempotency_key 同参数返回已有状态；不同参数冲突。重启不重放请求。查询结果包含阶段、错误、attempt 和日志位置；进程成功仍不能证明游戏通关。

## 战果与非三星边界

Core 6.21.3 的列表式 Copilot 在战斗后追加 `Copilot@WaitUntilEndOfAction`，其结算分支通过 `StageDrops-Stars-3` 或 `StageDrops-Stars-Adverse` 后才返回选关界面，非三星会阻止继续列表；普通单作业 filename 入口没有这项相同的结算检查。此为结算后的检查，不能避免已经产生的非完美通关理智消耗，没有核实到 Copilot 可配置的“漏怪立即撤退”开关。不能把停止 Core 等同于已在游戏中撤退。

回调是消息码与 JSON，而非统一的胜负返回对象。列表装载产生 `CopilotListLoadTaskFileSuccess`（含 stage_name、file_name、id），ProcessTask 的 SubTaskCompleted 可提供星级识别节点；回调的 details.task 会裁掉最后一个 @ 之前的前缀。planner 已关联当前列表项、文件名、关卡、难度与结算上下文，输出 `copilot_results` 的 completed／failed／unknown 及回调行引用。没有结算证据不能仅凭任务链 completed 判定完成，通用错误也不能当作战斗失败重试。顶层 game_result 仍为 unknown，因为整份日常还包含其他业务任务。

源码依据：[CopilotTask](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.3/src/MaaCore/Task/Interface/CopilotTask.cpp)、[结算任务](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.3/resource/tasks/tasks.json)、[ProcessTask 回调](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.3/src/MaaCore/Task/ProcessTask.cpp)。历史源码核查之外，

## 日常自动通关

既有 `[[schedules]]` 始终启用活动自动通关，从 PRTS Plus 自动搜索、按账号选择作业集，无需开关配置。存在当前开放活动且当天有启用的普通 Fight 时才准备，并在首个普通 Fight 前插入自动通关阶段，建议日程保持“唤醒→基建→剿灭→普通 Fight→后续日常”的顺序。无普通 Fight 的日程不触发自动搜索，包含普通 Fight 的日程目前仅支持官服／Bilibili 自动通关。示例日程保持停用，程序不改写真实配置；自动搜索会核对本次 Skland 的拥有干员与待打目标覆盖，不自动修改真实配置。

自动模式固定使用 `https://prts.maa.plus`，以 creator_id=7661 查询萨拉托加的公开作业集；不登录、不读取关注列表，也不自动扩大作者范围。每页 50、最多 20 页，超过边界或重复分页报错，不使用截断结果。同一共享准备单元只查询一次集合列表，各账号分别固定作业与阵容。来源：[集合详情](https://prts.maa.plus/set/get?id=56648)。

每次安排按作战截止顺序选择最早尚未完整认证的一个活动；均已完整认证时不再搜索下载。普通区、EX、S 各自选一个能够覆盖本账号当前开放且待打战斗目标的集合，不跨作者、不同集合逐关拼凑。标题中的活动缩写或完整活动名只用于缩小候选，最终通过作业内 stage_name 映射同版本游戏表的活动 ID、code 和区域：TL-数字及 TL-TR-数字归普通区，TL-EX-数字、TL-S-数字分别处理。于是标题含 TL 的 EX 集合不会被当作普通区集合；复刻仍按游戏关卡关系映射，不继承旧活动通关。未开放区域不搜索，其目标仍保留在完整活动清单里；剧情／教学路径的现有边界不变。

每个区域优先检验名称含“一摆到底”的集合，同类按集合 ID 从新到旧；只有优先集合无法覆盖待打目标或阵容不适用，才尝试同作者其他集合。干员拥有情况来自本次 Skland cultivate.characters 的 ID，再关联 player_info.charInfoMap 名称；名称表不是拥有证明，缺失／无效清单不当作无人拥有。与基建／库存采集共用本次角色响应，避免重复采集。普通区每关必须全员拥有，EX／S **每一关**允许至多一名未拥有干员；不同关可以缺不同的人。干员组按任选其一计算，并求互不重复的实际阵容，固定所选候选到私有附件，不把组内所有候选计为必需。

名称含“一摆到底”的集合，如果整集合各关经干员组匹配后的实际需求都已拥有，会在本次私有附件中为每关补齐全部需求干员的并集（包括账本已完成关卡的需求），不修改下载缓存或共享资源。保留各关原有动作、干员组别名及 skill_usage／skill_times；合并编队所需技能、模组与原有练度要求，不增加额外练度门槛。并集超过 12 人、技能／模组等编队要求冲突或有干员未拥有时，保留逐关编队。plan.selection 的 formation_reuse 与 formation_reuse_reason 说明是否启用及退回原因；启用时 targets 带 formation_group，同集合禁用助战。

本次运行中，同一集合第一个实际执行的目标仍自动编队；成功后，从下一目标（含突袭）起关闭自动编队。跳过已认证关卡不视为已经编队；新 worker／恢复运行、切换集合或普通逐关作业，以及明确战败后的重试，都重新编队，不持久化“游戏阵容仍有效”的假设。此优化只作用于后来固定的新请求，不修改已经运行的快照。

这里按用户规则只以拥有情况筛选，不额外添加等级／模组门槛，也不关闭 Core 的原始属性检查。普通区提交 support_unit_usage=0，EX／S 提交 1，仅在需要时借助战。参数依据：[Core 自动编队](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.3/src/MaaCore/Task/Miscellaneous/BattleFormationTask.h)。

自动计划中的 selection 记录各区域的 selected／no_usable_set、set_id、集合名、逐作业缺少的干员及 rejected 原因；终态 activity_copilot_results.selection 保留这些证据。某区域没有可用集合时可先完成已选区域，阶段随后标为 stopped 并告警，完整账本仍缺项则不刷普通图。整个搜索／Skland 准备失败时在生成计划时跳过普通 Fight 和附属商店，记录 activity_copilot_prepare 告警，其余可准备的日常继续；不默默改用其他作者或联网失败前的陈旧结果。plan 预览走同样选择流程，但不启动模拟器、游戏或 Core。

日程只使用自动搜索，不再提供显式作业链接入口。手动指定 URL 请使用显式 copilot API。未完成目标由通关账本重新计算，当前没有跨 batch 的作业执行游标。

planner 下载并固定作业内容，读取 JSON 的 difficulty：1 仅普通、2 仅突袭、3 两者；缺省／0 按已确认策略仅普通。再用当前活动和关卡关系核对实际存在的难度，按解锁依赖排序为 **1普通→1突袭→2普通→2突袭**，没有突袭时只有普通；同一目标有多个作业时选原集内首次出现的适用作业，避免重复战斗。普通／突袭分别记账，已确认的目标单独跳过，不将另一难度当作已完成。

本地账本只接纳本次 Copilot 的成功证据或用户通过 `stage.confirm` 明确确认。没有记录即待打，不根据 Skland 分区数量或游戏界面历史标记自动补记。旧活动和复刻使用不同 activity_id，不能继承原活动记录。用户 API 逐关确认或撤销影响随后固定的计划，不改写已接纳运行中的快照。普通与突袭分别记账，互不补记或撤销，不采用范围确认。

私有 Custom 导航负责从首页进入活动及区域地图；同一区域连续成功时只导航一次，其余由列表式 Copilot 负责地图找关、滑动、点击、核对和突袭切换。切换区域或新 worker 进入时重新导航；同区明确战败／二星后的重试不返首页，先复用 Core 结算退出流程。导航节点、作业和关卡映射均固定到私有请求，不改共享 GUI 资源，也不在 worker 联网下载。

导航按首页、终端、活动与区域页面分步识别。活动入口 OCR 标签与区域名称来自当前版本游戏数据表（包括 zoneNameSecond），不再依赖 TL 专属节点或手写区域标题；Area 必须产生 Map 回调证据才算到达。

每步收到明确 error／failed 时最多尝试三次，仅重试未完成步骤；已完成的首页、终端或活动入口不重放。终端点击完成但就绪检查报错时，从 TerminalReady 续行。取消、未解锁、未知状态及缺少地图证据不盲目重试，活动过期时停止。此处的短间隔恢复限于本次导航调用；另有[公共步骤错误恢复排期](daily-step-retry.md)，明确 Core 错误会告警并延后重新规划所属刷理智业务，不自动重放崩溃前任务；不为临时“数据已更新”弹窗编写专用处理。商店仍遵守首次导航失败全账号停用，不套用该重试规则。

每次只执行一个目标，按上述条件自动编队或复用集合编队，并允许吃理智药，不启用源石恢复。明确战败／二星后，仅在还有重试次数时调用 Core 自有 Copilot@ClickCornerUntilStartButton；须任务完成且收到 BattleStartPreFlag／BattleStartPreFlag-Return 回调，才重提同一关并重新编队。退出失败或缺少就绪证据返回 settlement_exit_failed，停止本阶段，不回退到私有返首页导航。首败不新增告警；已有取消、有效期和一般 Core 错误恢复规则保留。明确战败或识别到二星结算时重试当前目标，第三次失败（共三次尝试）停止本阶段、持久化告警，再按完整活动账本决定是否允许普通 Fight；信用、公招、领奖等后续任务继续。下一关重新计数；导航成功不清零同关失败次数。未解锁／导航失败／战果无法确认也停止本阶段并告警，不能盲目开战或把未知战果当作战败重试三次。活动有效期在阶段内再次检查，结束后不新开战斗。

自动通关阶段结束后，用同版本游戏表中本次规划时已开放的完整普通／突袭／教学目标，与本账号原有账本及本轮有证据的成功合并比较。仍有缺项时（包括理智不足导致停止）跳过本轮所有后续普通 Fight，保留理智供下一次通关；不要求先识别出“理智不足”回调，三连败、未知战果等停止原因也遵循同一完整性条件。作业集全部完成但未覆盖当前已开放关卡，仍会跳过刷图；未开放区域不会阻塞，开放后的下一次规划重新纳入检查。完整清单缺失或不可信时也跳过；旧计划缺少完整清单按未知处理。该规则不依赖商店入口资源，不以 Skland 计数补记通关，不影响剿灭及信用、公招、领奖等其他任务；依附于被跳过 Fight 的商店阶段不触发。没有自动通关阶段的日常沿用原有刷图流程。

认证成功的关卡会在后续日常结束前发送给 daemon 保存，重启保留；告警通过既有 alerts/events 提供，已有独立企业微信／Server酱客户端；未启动客户端时没有外部消息投递。明确的 Core 任务链错误通过 warning.core_error 上传，外层运行返回 failed，并由公共调度层安排恢复；正常业务停止与独立后续任务沿用原规则。自动阶段结果为 `activity_copilot_results`，包含逐关结果、失败次数、停止原因与证据，以及 farming_gate：allowed 表示能否继续普通刷图，reason 为 ready／stages_incomplete／catalog_incomplete，missing 为仍未认证的 [普通关 ID, is_raid] 列表。中间导航和已处理的失败尝试保留在 callbacks.jsonl／core-result.json 的 handled_task_results；普通 task_results 仅记录实际提交给 Core 的配置任务，跳过的 Fight 由 farming_gate 解释，阶段停止本身不使后续日常失败。

`plan` 预览显示 activity_copilot 的作业 targets、本地 completed 快照、当前已开放关卡的 required 清单与 catalog_complete 标记；刷图是否跳过取决于随后实际通关结果，准备失败时返回 activity_copilot_error；真实日常自动通关准备失败会记录 activity_copilot_prepare 告警，过滤本轮普通 Fight、明确记录刷理智业务失败后继续其余可准备日常，再按业务恢复排期重新判断。作业集阶段完成只证明该集合覆盖的目标完成；Fight 后另用完整活动目标、全部区域开放与本地账本判断是否可购买，不能直接把作业集完成当成兑换资格。漏怪保护按用户要求交给上游 [PR #17535](https://github.com/MaaAssistantArknights/MaaAssistantArknights/pull/17535)，本项目未合入该补丁。

## 数据边界

在线准备只支持普通 Copilot，保留原 opers/actions（自动搜索会固定干员组的所选替代项），下载有大小和数量限制。每个作业源最多 200 次请求、单响应 4 MiB、总返回 16 MiB，触及总限额停止准备，不继续联网或把截断结果当作完整搜索。关卡名取自当前共享资源的 Tile-Pos。任务和附件经 SQL/worker 固定传递，runner 不访问在线作业站。

本地或更复杂的 Copilot、SSSCopilot、ParadoxCopilot 可通过 run 操作提交内嵌 plan/attachments。协议、生命周期、事件游标和取消语义见 [控制 API](control-api.md)。

多个账号的作业可通过 execution.batch 提交，共享依赖准备。作业响应保存在宿主默认 cache/copilot 目录（见 [存储归属](configuration.md#存储归属)）并进行 ETag 验证；单次运行生成的配置和附件在安全结束后删除，异常恢复前保留。

### 商店首次导航失败后停用

基础策略按同一次活动、所有账号共享首次导航结果。第一次实际执行私有商店导航失败，或任务链结束却没有 ShopReady 证据时，保存停用状态并仅向 `admin` 告警。之后该活动的所有账号跳过商店导航及购买，继续普通刷图和其余日常；停用不代表商店换完，也不能据此进入垃圾时间。

停用按游戏活动 ID 与本期开始时间保存，跨 daemon 重启、其他账号、商品黑名单／关卡范围变化及入口素材修改保持；不会因资源更新自动解除。到作战截止清理决策状态并解除该 admin 告警，执行历史保留，新活动重新判断。没有新增手动重置 API。

首次进入商店成功后，即使代币不足或购买结果未知，也记录为导航成功；后续导航错误沿用已有异常处理，不将已成功的首次结果改成首次失败。资格不足、刷图失败、未配置入口、取消及尚未执行的阶段不计作首次导航失败。旧报告没有独立导航结果时不推断其首次结果。

同一活动首次探测只允许一个 worker 执行；其他账号遇到正在探测的活动时，本阶段记为 skipped／navigation_pending，继续日常，后续安排再检查。每次真正导航前经私有 worker 协议查询 daemon；即使账号准备阶段固定的是旧计划，首次失败后也返回 navigation_disabled。worker 不直接读写 SQLite，取消／异常退出会释放内存探测占用，不自动重放崩溃任务。

结果新增 navigation_outcome（not_attempted／succeeded／failed）、navigation_phase 和 navigation_status_before_attempt；计划预览提供 navigation_status（untried／succeeded／disabled）。告警 category=activity_shop_navigation，包含首个账号、活动、失败阶段／原因、任务与 attempt_id，以及 all_profiles_disabled=true。本策略只处理现有私有导航的首次结果，不表示自有 PNG 模板或自动制作资源已实现。

### 剧情与教学前置路径

关卡之间的剧情不进入 activity_scope.required，不需要人工登记，由 Core 在必要的找关导航中处理。Core v6.22.0 的 [MultiCopilotTaskPlugin](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.22.0/src/MaaCore/Task/Miscellaneous/MultiCopilotTaskPlugin.cpp) 在 OCR 找关失败后调用 Copilot@ChapterSwipeToTheRightAndPlot；这不是作业列表结束后的剧情收尾，也不能根据未匹配到图标推断已看过。

区域拖尾剧情独立处理：同一区域内，没有任何普通作战节点位于其后置解锁路径的纯剧情节点，纳入明确目标及认证范围；跨区 EX／S 后继不使普通区拖尾变成中间剧情。TL-ST-4 因此必须位于 TL-10 之后、依赖它的 EX／S 之前。连续拖尾剧情逐个按依赖处理，未开放区域仍等待开放。只缺剧情时不搜索战斗作业集。

缺少本地已看记录的拖尾生成 kind=tail_story、script_id=0 的私有 Custom 目标，不提交 Copilot 战斗。先核对前置已完成，再定位准确关卡，进入剧情并跳过，确认返回地图。必须获得同一次成功任务中有序的 PlotPanel、PlotStart、PlotSkipped、PlotReturned 证据，daemon 才以 source=story 登记并立即持久化；通用 EndOfPlot 或单纯任务链完成均不补账。取消、未知或任务错误保留未确认状态，停止后续自动通关及普通刷图，其他日常继续。已登记的拖尾在后续运行及重启后跳过；stage.confirm／unconfirm 也可单关登记或撤销，仍无范围推断。

TR 教学作业纳入普通区和全员拥有策略，仍限定作者 7661。集合缺少教学作业不丢弃已覆盖的其他战斗：按游戏解锁依赖排序，穿过剧情前置，仅在执行顺序走到缺作业的教学目标时停止本次自动抄作业，warning.reason=no_tutorial_copilot；不开始该关、不算战败、不跳过教学继续后续关卡，其他日常继续。教学目标已通过 stage.confirm 确认时跳过；真实作业存在时正常交给 Copilot。

缺作业标记 kind=missing_tutorial、script_id=0，不含 filename；它不是作业附件，不能用空路径表示。该标记随日程冻结、物化后仍由执行器按 no_tutorial_copilot 停止，不提交该关的 Core 导航或战斗。完整活动刷图门控仍保留；通用剧情回调不自动补账，独立记账只用于上述明确的拖尾目标。

## 剿灭 expert 无代理补救

日程准备只固定同一 catalog.commit 中唯一当期轮换剿灭的名称／窗口及账号本次 Skland 拥有清单，不查作业、不下载作业附件，不把三张常驻或过往开放剿灭作为候选。plan 同样不查询作业。窗口缺失／矛盾时报准备告警，不回退常驻；固定／显式剿灭可保持原纯委托模式，补救不处理常驻关卡。

expert 的委托图在使用代理前核对当期地图。现有全权委托已激活且可用才开始委托；可激活则先激活再核对，普通代理、缺券、激活失败和未知状态不进入普通战斗。只有当期地图出现明确 NoProxy 且任务正常结束，才在运行现场查询作者 7661 的该关作业，下载完整正文核对 stage_name，用固定拥有清单筛选全员拥有的第一个候选。没有适用作业、查询失败、取消或已过窗口均停止补救并保留 copilot_required；不会预搜常驻或靠常驻补额度。

选中作业保存为该次私有 annihilation-rescue.json，记录 ID／SHA256 并随日志归档。它是明确 NoProxy 后允许的 worker 公开查询例外，不重读账号认证／库存，也不改变其他已固定任务。补救自动编队、只执行一次，不借助战、不吃药、不用源石。

随后必须先获得 Settled／SettledGeneric，再获得 Ready（全权委托激活）回调，最后复核仍为同一当期地图，才恢复委托。单纯 Copilot／Custom 链完成不算补救成功。未知、战败或缺少代理证据保留 copilot_required，继续日常并沿用已有告警／下批再判断。

RunResult.annihilation_results 的 rescue 提供地图 ID、作业 ID／内容哈希、原因与回调证据；outcome=rescued 只表示恢复了全权委托资格，不表示本周额度已满。恢复后的委托另有独立结果；其 Core 错误仍走公共步骤恢复，业务结果不会把第一次 NoProxy 的完成覆盖掉后续失败。取消不会开始新作业。

返回首页的私有链已共用战败／结算恢复：先识别首页、深／浅色返回按钮，也可通过 FightMissionFailed 关闭战败页，或识别 EndOfAction 后用 ClickCorner 关闭结算。所有恢复边显式关闭上游后续开始战斗的路径，不盲点开始；仍保留次数与局部导航重试限制。这些分支保留给初始入口或其他需要返回首页的流程；自动抄作业的同区战败重试已改用 Core 自有结算退出任务，不再经过它们。浅色返回模板仅为兼容补充，没有证据表明它是上次失败原因。真实重测尚未进行。
