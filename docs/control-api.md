# 控制 API

## 调用边界

控制端点使用 Socket.IO，通过 WebSocket 传输请求、ACK 和通知。daemon 仅监听 `127.0.0.1` 的系统分配端口；随机 token 与 url 写入当前用户私有的 control.json，Windows 使用独占 DACL。令牌不写命令行或日志，每次启动重新生成。

`maa_planner.schema` 是独立协议清单：公开方法、参数字段、响应、错误和通知，以及纯数据校验。它不读取文件、环境变量或 daemon 状态，不包含执行命令。`maa_planner.endpoint` 负责本机端点发现，`maa_planner.client.ControlClient` 负责通信；两者都不导入 daemon。接入端不读取 daemon 业务配置、不访问模拟器。

服务端的 ControlAPI 只适配协议，依赖注入的操作与控制服务，不接收 daemon。OperationService 管理接纳、取消、结果和通知；ObservationService 执行观测与规划；配置服务管理配置快照。daemon 装配服务，并通过 ExecutionAccess 提供准备、准入与执行入口；协议层和操作服务不遍历 daemon 内部队列。日常与显式运行继续共用规划器及原生 worker。

```python
import asyncio
from maa_planner.client import ControlClient

async def main():
    async with ControlClient() as client:
        print(await client.call("api.describe"))
        operation = await client.call(
            "copilot.run", idempotency_key="client-generated-key",
            profile="nemo", url="prts://s56648", select=[3, 5, 7],
        )
        print(operation["operation_id"])

asyncio.run(main())
```

默认端点为应用 state 目录下的 control.json，见 [存储归属](configuration.md#存储归属)。ControlClient 可指定路径，daemon CLI 使用 `--endpoint PATH`。客户端不隐式启动 daemon。

## 协议

连接使用 `auth={"token": token}` 和 `transports=["websocket"]`。只有 request 与 notification 两种应用事件；ACK 由 Socket.IO 编号关联，允许响应交错及通知穿插，无额外 RPC 编号。

```json
{"method":"copilot.run","params":{"idempotency_key":"nemo-tl","profile":"nemo","url":"prts://s56648","select":[3,5,7]}}
```

```json
{"result":{"operation_id":"c8071c0318584d3ebce3aee65c2607d6","idempotency_key":"nemo-tl","method":"copilot.run","profile":"nemo","status":"preparing"}}
```

失败返回 error，包含稳定 code、message、details；details.method 标识请求方法，冲突／占用可以附 operation_id 或配置版本。参数错误为 invalid_argument，未知操作为 not_found，去重或配置冲突为 conflict，维护／实例占用为 busy，其他不可用为 unavailable。ControlClient 抛出 APIError，保留 code 与 details。

单条消息上限 1 MiB，每会话最多 32 个并行请求。通知队列上限 256 条／4 MiB，发送超过 5 秒或积压超限时关闭该会话。默认请求超时 30 秒；超时、断线不自动重发，不代表未接纳，也不会取消已接纳操作。客户端关闭自动重连。

## 方法

`api.describe` 从同一份 schema 清单返回全部方法的 kind、required、optional、profile。kind 为 query、command 或 operation；operation 方法立即返回操作，可继续查询／取消。参数直接位于 params，不使用 action 或 arguments 包装。

| 方法 | 参数 | 结果／行为 |
| --- | --- | --- |
| api.describe | 无 | 方法清单 |
| daemon.status | 无 | 生命周期、配置版本、运行／准备／等待、恢复、维护状态 |
| daemon.stop / daemon.restart | 可选 force | 排空或强制请求停止，返回受理后的状态 |
| config.get | 无 | TOML、生效配置、revision、文件错误 |
| config.replace | toml，可选 expected_revision | 校验并原子保存完整配置，返回新配置 |
| operation.get | operation_id | 查询操作 |
| operation.find | idempotency_key | 按去重键找回操作，未接纳返回 null |
| operation.cancel | operation_id | 合作式取消，返回当前状态 |
| event.list | 可选 after、limit | 日志事件，limit 为 1～100 |
| alert.list | 可选 profile、active_only | 当前告警，active_only 默认 true |
| stage.confirm | profile、activity_id、stage_id，可选 is_raid | 逐关人工通关声明，不访问游戏 |
| stage.unconfirm | profile、activity_id、stage_id，可选 is_raid | 撤销该单一目标的认证，返回 removed |
| stage.progress.get | profile、activity_id | 本地通关证据 |
| daily.next | profile | 查询最早的可控周期机会，返回 revision 与目标 state_version |
| infrast.orundum.get | profile | 查询当前手动搓玉意图，没有意图返回 null |

以下方法均需要 idempotency_key；除 execution.batch 外可提供 latest_start_at（有限 Unix 秒，只限制启动）。profile 列“可选”时，省略表示全部启用账号。

| 方法 | profile | 其他参数／行为 |
| --- | --- | --- |
| execution.run | 必填 | plan，可选 options、execution_timeout；执行固定 Core 计划 |
| copilot.run | 必填 | url，可选 formation、ignore_requirements、is_raid、use_sanity_potion、start_at、select |
| execution.batch | 不接受 | jobs，共享准备的一批 execution.run／copilot.run |
| schedule.plan.preview | 必填 | schedule_id，预览当前日常规划，不启动游戏 |
| account.context.fetch | 必填 | 获取账号状态摘要，不返回凭证 |
| emulator.list | 可选 | 查询模拟器与绑定 |
| activity.fetch | 不接受 | 获取公共活动及分区窗口 |
| news.collect | 不接受 | max_pages、max_articles、failure_backoff，见文末 |
| assets.prepare | 不接受 | activity_id、bundles、resolve_dependencies、max_bundle_bytes、max_output_bytes、export_timeout，见文末 |
| runtime.ensure | 不接受 | 检查并按需安装最新 Core，返回实际加载版本 |
| resource.prepare_and_check | 不接受 | 按需准备 Core 后检查资源及 GUI 占用 |
| resource.update | 不接受 | 显式更新资源 |
| runtime.prepare | 不接受 | 准备 Core、资源与热更新数据 |
| game.inspect | 可选 | 只检查 APK metadata |
| game.update | 可选 | 按需下载安装 APK 并确认资源就绪 |
| execution.recover | 必填 | 核对进程后解除 Core 恢复隔离 |

runtime.ensure、resource.prepare_and_check、resource.update、runtime.prepare、game.update 与 execution.recover 使用维护独占。它们的准备／安装副作用是接口语义的一部分；已有工作时拒绝显式维护，新工作在维护期间拒绝。不同模拟器可并发，同一实例的即时请求繁忙时拒绝，不自动排队。

## 幂等、期限和取消

每次主动操作由 client 生成新的 idempotency_key（1～200 字符）；网络重发必须复用原键。服务端生成独立 operation_id，批次成员也由服务端分配 ID。相同键及规范化参数返回原操作，不重新执行；不同参数返回 conflict。修改 latest_start_at 不延长原操作。去重作用域为此 daemon 状态根目录；活跃映射在当前状态中，终态依据结构化日志恢复，重启保留。删除日志会失去相应历史找回及去重保障，不承诺游戏动作恰好执行一次。

响应丢失时调用 operation.find；未找到才说明没有可找回记录。客户端不根据超时擅自生成新键重跑。接纳后使用固定配置与文件快照，后续编辑不修改本次请求。

execution.run／copilot.run 未显式设置 latest_start_at 时，默认启动窗口 60 秒，自动依赖准备耗时不计入这个隐式窗口；显式期限不延长。准备期间过期不启动 worker，已开始运行不因启动期限到达而取消。启动后的执行超时由 execution_timeout 另行控制。

查询和执行可合作式取消，状态在实际结束后才终结；维护不能中途取消，stop／restart 等待安全完成。取消与完成可能竞态。终态为 succeeded、failed、cancelled、blocked、recovery_required。崩溃后的不明执行不自动重放，先核对恢复现场。

operation.get 和 operation.find 不推进或持久写入执行状态；完成回调及调度协调负责更新与批次汇总。SQLite 只保存当前状态和未完成工作，终态、事件及解除告警从日志查询。

## 会话与通知

notification 包含 kind、profile、data。账号告警在提交及日志持久写入后广播 alert.raised／alert.resolved，相同持续告警不重复 raised。operation.updated 只发给在线发起会话，data 使用公开 operation 结构，含服务端 operation_id 和原 idempotency_key；客户端可按键路由早于提交 ACK 到达的通知。重复提交不会抢走其他会话的订阅。

推送没有离线队列、ACK、重放或游标。历史事件通过 event.list 主动分页查询。

## 完整配置替换

config.replace 接受完整 toml 和可选 expected_revision；版本冲突返回 conflict，并附 expected_revision、actual_revision。未提供版本时覆盖当前配置。无效或保存失败的配置不接纳；已接纳的运行及维护保留原快照。

## 固定计划与结果

execution.run 的 plan 包含 tasks 及可选 attachments、resource_tasks、resource_images；附件是内嵌 JSON，不接受任意 filename 路径、条件 variants 或执行中重读文件。原生基建附件通过 @file:base.json 引用。纯 Copilot／SSSCopilot／ParadoxCopilot 计划要求游戏已运行；普通即时运行仍拒绝接管前台手动游戏。

run／copilot 依赖准备后在操作中提供 runtime、attempt_id；最终实际路径和版本以此为准，config.get 中 unprepared 占位不代表已安装。结果中的 game_result 当前仍为 unknown，宿主成功不代表业务成功。task_results 记录 Core 任务终态与 subtask_errors；完整原始回调见 log_directory 下 callbacks.jsonl。逐关 copilot_results、activity_copilot_results、商店 event_shop_results 提供业务 outcome、reason 和 evidence，不能只凭任务链完成推断领取、兑换或通关成功。

APK 详细结果仅在当前进程保留最多 16 份／5 分钟，过期或重启后 result_available=false；新查询使用新的去重键。URL、版本历史及具体 APK 错误不写 SQL。当前未完成资源状态仅保存账号及目标包名／版本，确认后删除。安装制品在本次安装阶段结束后删除；game.inspect 不删除制品。

## 批次执行

```json
{"method":"execution.batch","params":{"idempotency_key":"morning","jobs":[{"method":"execution.run","profile":"nemo","plan":{"tasks":[{"type":"Award"}]}},{"method":"copilot.run","profile":"wyf","url":"prts://s56648","select":[3,5],"formation":true}]}}
```

jobs 为 1～100 个成员，同一账号只能出现一次；成员不接受 idempotency_key，仅支持 execution.run／copilot.run 及对应直接参数，可各设 latest_start_at。父操作 result.operations 提供成员 ID，operation.get 的 result.members 返回概要；详情单独查询成员。成员失败不取消其他成员。全部成功为 succeeded，全部取消为 cancelled，其他终态组合为 failed；daemon 主动汇总并通知，不依赖查询触发。可取消成员或整个批次。

同批共用一次准备；后来的准备单元在依赖兼容时复用活跃 Core／基础资源，仍刷新自己的公共活动信息。空闲时重新检查 Core、资源和热更新，不按请求时间偶然重叠推断共享关系。activity.fetch 和 schedule.plan.preview 刷新活动观测，不安装二进制或启动游戏；当前没有独立活动定时轮询。

## 指定干员加工的配置与预览

加工通过 `config.replace` 更新完整 TOML 中对应 Infrast 任务的 processing 配置；没有另设直接消耗材料的加工 API。字段及策略示例见 [指定干员加工](configuration.md#指定干员加工)。配置变更影响随后生成的计划，不修改已接纳运行的快照。

`schedule.plan.preview` 使用 `schedule_id` 选择日程，其返回值增加：

| 字段 | 含义 |
| --- | --- |
| infrast_processing | 加工阶段列表：before_task 关联基建任务，catalog_commit 固定配方来源，recipes 为配置目标及干员策略筛出的配方，operators 保留顺序、选人附件和 allowed_items，stock 为准备时的库存快照 |
| infrast_processing_error | 配方／库存准备失败的原因；失败时不制备加工阶段，保留原换班 |
| infrast_processing_warnings | 九色鹿的 unsupported_operator 警告；plan 仅展示，不写该业务告警 |

正式日程中，`infrast_processing` 告警以日程 ID 为 subject，details.reason=unsupported_operator、operators=["九色鹿"]；公共准备失败使用 `infrast_processing_prepare`，同样关联日程 ID。缺人、心情或原料不足不告警，未知执行错误沿用 task_retry。原始加工结果保存在本次日志的 infrast_processing_results，普通业务状态与基建共用；库存未补齐不等于所有基建工作失败，结果中的 remaining 给出仍有缺口或数量未知的材料。

## APK 自动排期与安装后资源就绪

全局 game_update 配置的 time 默认北京时间 11:00，启用时到点接纳一个独占 game.update 操作；已有工作先结束，同刻日常继续保留，停止的模拟器记 deferred。排期及检查间隔见 [配置](configuration.md#apk-自动更新与资源就绪)。

`daemon.status.game_update` 包含 enabled、time、timezone、next_at、pending_at、operation_id、login_exemption 和 resources_pending；后者是尚未确认完成的账号到目标包名／版本的映射，属于当前状态。自动维护的 operation_id 可继续通过 operation.get 查询，结果仍遵守 5 分钟／16 份的进程内保留规则。

`login_exemption` 为当天全局跳过依据，包含 day、profile、attempt_id、started_at、observed_at 和 evidence。固定假设为停机更新连续占用北京时间 6:00–12:00；窗口内任一账号有新登录动作及后续首页匹配、StartUp 正常完成，即全局取消当天等待中的检查并跳过随后自动检查。它通过 worker 即时上报，重启保留、午夜清除；已有首页或黄色 START 不提供登录证据。未完成更新状态不会因此删除，显式 game.update 仍可执行。

显式 game.update 和自动排期共用安装及资源流程；每个 units 项增加 resources。安装或恢复资源检查成功时 resources.status=ready，并提供图标回调 evidence 与 log_directory；检查失败为 pending，保留未完成状态。up_to_date 且没有待确认记录时不启动游戏；已有 pending 且安装版本符合官网时只检查资源，不重装。自动排期的 deferred 表示模拟器关闭／ADB 未就绪，本次未更新，下一次排期重查。

下载阶段复用 GameStart.png 黄色 START 标记，只识别、不点击；检测后才提交关闭游戏。取消或超时不会提交 CloseDown；force／停止信号会请求 Core 停止，正在执行的 APK 写入仍等安全收尾。日常恢复排期不负责重放 APK 维护，下一次显式／定时操作重新核对当前 metadata。game_resources 为账号资源就绪失败告警，admin 的 game_update 为定时维护失败告警；成功后解除。

## 日常恢复排期查询

`daemon.status.scheduled_retries` 返回日常恢复排期的概要，仅包含 waiting、running 或 recovery_required 当前记录，以及 id、profile、number、due_at、expires_at、failed_attempt 和 errors 等诊断字段，不返回完整 RunRequest。`event.list` 的 kind=scheduled_retry 可查询状态变化，成功、取消、过期及耗尽记录退出该队列，终态历史从日志查询；`alert.list` 可查 task_retry。历史失败可通过 daily.retry 选择性重试；不提供修改自动退避时间的接口，显式 run／copilot 操作不会自动加入该队列。具体默认间隔、恢复粒度和保护边界见[步骤恢复](daily-step-retry.md)。

执行结果的 `business_results` 提供业务 id、status 和失败 error；刷理智的 prefix／主体／suffix 共同决定同一个业务结果。恢复排期 pending 列出待重新规划的业务标识，不返回旧 Core 请求或阶段快照。

执行结果保留诊断字段 `task_checkpoints`：每项含 index、task_id、status、evidence。它记录本次物化任务序列的终态，错误证据引用 callbacks.jsonl，不是恢复执行位置。自动通关内部错误通过 `activity_copilot_results[].warning.core_error` 提供，明确错误会使外层 failed；业务三连败等停止仍用阶段报告表达。

运行结果可包含 `session_failure`：非 StartUp 任务明确命中 Core 掉线节点时，kind 为 `game_disconnected`，并提供 task_id、node、evidence 和 observed_at。该结果表示停止本轮后续游戏任务，不是普通 SubTaskError，也不以 TaskChainCompleted 认定成功。日程可进入既有 task_retry 恢复排期；显式 run／copilot 仍不自动排期。未检测到明确掉线时字段为 null，旧历史可能没有该字段。

## 日常计划预览

`schedule.plan.preview` 必须提供 `profile`、`idempotency_key` 和 `schedule_id="morning"`。它使用接纳时的配置，在查询时刻按 Asia/Shanghai 解析任务日期条件、基建 period、expert 与附件；返回 `planned_at`、`tasks`、`attachments`、`resource_tasks`、`resource_images`、基建 `decisions`，以及 `annihilation`、`farming`、`activity_copilot`／`activity_copilot_error`、`activity_shops`／`activity_shop_error`。日程必须包含该账号；允许预览尚未启用的日程，返回其 enabled 状态。基建、普通 Fight 或剿灭 expert 会采集目标账号的 Skland；当天存在普通 Fight 与开放活动时，也会采集／复用本次 Skland 干员清单；商店资格／垃圾时间复核也可能补充库存。没有这些需求的纯固定日程不请求 Skland。

普通 Fight 的预览及显式 run 固定计划统一省略 times、series=0，worker 也执行相同归一化；普通作战不提供次数上限，用药和源石参数保留。日程会在 plan 中复用自动搜索及 Skland 拥有筛选，activity_copilot.selection 提供各区域的集合选择、缺少干员与排除原因，以及 formation_reuse／formation_reuse_reason；可复用编队的目标带 formation_group，私有附件已补齐干员并集；activity_copilot_error 表示准备错误，此时自动模式的 tasks 已过滤普通 Fight。activity_copilot 额外固定本次规划时已开放关卡的 required 清单及 catalog_complete 标记，独立于作业 targets 和项目商店入口；预览仍保留候选普通 Fight，实际执行是否跳过取决于本轮通关结果。预览中的剿灭仍显示逻辑 Fight，专用 Custom 图由 worker 执行前生成，不在 plan.resource_tasks 中展开。

预览不会占用日常安排标识、启动 worker、解析模拟器绑定、安装 Core 或执行游戏任务。它不是未来时刻的承诺，也不会把基建选择当成已经应用；真实执行仍在准备阶段重新读取并固定自己的观测和附件。旧的无 schedule_id 空建议接口已移除。

只有存在当前开放活动且当天有启用的普通 Fight 时，预览才搜索／下载／缓存作业及关卡关联数据。预览显示 activity_copilot 的目标与 completed 快照，失败原因在 activity_copilot_error；非活动时该阶段为空，仍不安装或启动 Core。个人历史只读取本地账本，不用 Skland 数量推算。

自动搜索结果 activity_copilot_results.selection 记录所选集合 ID、名称、区域、缺少干员及候选拒绝原因；没有可用集合时 warning.reason=no_usable_set、阶段 stopped，不证明活动完成。显式 copilot API 的 URL 行为不变。自动通关结果 activity_copilot_results 中的 farming_gate 提供 allowed、reason 和 missing：reason 为 ready、stages_incomplete 或 catalog_incomplete，missing 为缺失的 [普通关 ID, is_raid] 列表。阶段结束时完整普通／突袭／教学账本尚有缺项或清单未知，跳过所有后续普通 Fight 及其附属商店阶段，其他日常继续；三连败、未知战果及理智不足未全通均遵循此条件。task_results 仅包含实际提交给 Core 的任务，跳过的 Fight 不分配 task_id。原有及新成功记录只取当前账号／活动快照，不从 Skland 计数推断。无自动通关阶段时不启用此门控；旧阶段结果缺少 farming_gate 时不推断曾允许刷图。

## 人工通关确认

```json
{
  "method": "stage.confirm",
  "params": {
    "activity_id": "act52side",
    "stage_id": "act52side_ex01",
    "is_raid": true,
    "profile": "nemo"
  }
}
```

stage_id 使用该活动普通关的游戏 ID，is_raid 默认 false。普通与突袭是两个独立通关目标；confirm／unconfirm 均只修改指定目标，不补记或撤销其他难度、前置、后续或整个区域，不采用范围确认。需要 profile 为已配置账号；活动、关卡和难度按用户声明记录，返回 source=api 和 observed_at，不启动游戏、不校验实际战果。stage.unconfirm 使用相同参数，删除该目标认证；removed=true 表示本次确实删除，重复撤销返回 false，实际变更记录 stage_unconfirmed 事件。撤销使目标回到未认证状态，不是一次游戏内未通关观测，不取消或改写已冻结执行；随后规划按未认证目标处理。stage.progress.get 使用相同 profile/activity_id 查询，source=copilot 或 source=story 的记录还含 attempt_id 及回调引用。中间剧情不参与刷图或商店认证，导航由 Core 处理；区域拖尾剧情必须显式观看，daemon 凭准确目标的完整观看证据自动登记 source=story，无需客户端上报，也允许手动单关 confirm／unconfirm。缺教学作业时自动阶段返回 no_tutorial_copilot，不把通用剧情回调当作具体关卡通关证据。没有记录按待打处理，既不是进度为零的 Skland 观测，也不代表读取过游戏历史；后续计划才会固定这些确认。

## 结构化告警

`alert.list` 方法接受可选 `profile` 和 `active_only`（默认 true），返回当前未解除告警；active_only=false 时补充日志中的已解除记录。`event.list` 中的 `kind="alert"` 包含同一告警内容与 `transition="raised"|"resolved"`，可主动分页查询历史。共享 schema 提供 Alert 类型；alert_id 表示一次持续的告警，恢复后再次触发会产生新 ID。

同一账号、category、subject 的连续不足观测只更新状态详情，不重复产生 raised 事件；确认恢复时发出 resolved。账号分别记录，daemon 重启保留状态。采集失败、没有观测或任务未运行不能解除告警。`active` 表示最后可信观测仍未解除，不能理解成后台持续采集。

force／可捕获信号中断另有 `execution_interrupted` 告警，profile 为执行账号、subject 为 attempt_id，details 含 reason；它是持久化的中断记录，不因重启自动解除。`daemon_interrupted` 事件也记录停止原因。外部强杀无法保证即时告警。

当前已接入 `preparation`、`activity_source`、`skland_prepare`、`farming_plan`、`infrast_material`、`infrast_processing`、`infrast_processing_prepare`、`annihilation_copilot`、`activity_copilot`、`activity_copilot_prepare`、`activity_shop`、`activity_shop_prepare`、`task_retry`。共享准备失败在 `admin` 频道记录 `preparation`；公共活动推荐无法解析时记录 `activity_source`，恢复后解除。Skland 准备失败按账号和日程记录 `skland_prepare`，成功获取后解除。普通 Fight 因自定义后备关卡全部关闭而无法选图时记录 `farming_plan`；活动推荐数据错误在读取公共快照时已记录 `activity_source`，不在日程失败处重复告警。task_retry 按日常 occurrence_id 关联明确 Core 任务错误、退避次数和下次恢复时间；正常恢复成功后解除。基建告警 subject 为物品 ID，details 包含库存、告警／停止阈值与 occurrence_id；其他类别分别关联活动、作业 URL 或日程，details 保留失败原因及可用执行信息。plan 预览不产生上述业务告警，但成功获取活动快照可能记录生命周期／清理事件。加工无法选人、心情或原料不足时不发送通知；活动作战截止不发送外部通知。普通 Fight expert 的芯片／后备选图不是独立补库存任务，不为其添加失败重试或重复告警。进入垃圾时间的策略提示尚未接入，不能与告警或 activity_ended 事件混为同类。企业微信投递不承诺消息已送达或恰好投递一次。

执行结果的 annihilation_results 保存剿灭业务证据，包含 task_id、stage、outcome、reason、current/total、observed_at 和 evidence。outcome 为 completed、incomplete、copilot_required、skipped 或 unknown；专用 Custom 不认证本周完成，completed 仅兼容旧结果。下一 batch 是否跳过仅由新取得的 Skland 周进度决定。plan 预览新增 annihilation 决策；event.list 中的 annihilation_decision/annihilation_result 可用于跟踪本周安排，annihilation_copilot 告警用于需要作业补救的情况，尚不代表已自动找到或执行作业。

自动日程不启动模拟器；显式 `execution.run`（含 batch 成员）和 game.update 允许启动停止的实例，copilot 仍要求游戏已运行。`daemon.stop`／`daemon.restart` 的 force=true 停止 Core 并保留模拟器现场，取消执行及父通道 EOF 同样保留；已有游戏动作不可撤销，写入维护仍等待安全完成。

管理员告警使用保留频道 `admin`，沿用告警与通知的 `profile` 字段；`alert.list` 的 `profile="admin"` 查询该频道，不指定 profile 的可信本机调用包含全部频道。`admin` 不能作为游戏账号名。企业微信入口只允许管理员查询／接收该频道；Server酱通过单独的 `admin` 路由指定管理员目的地。本机控制端点仍按现有可信客户端鉴权，不新增账号级会话权限。

准备诊断：`daemon.status.preparation` 按 environment／core／resources／hot_update／catalog 保存本进程最近观测，包含 status、observed_at、版本／指纹、installed／updated 和失败原因；`event.list` 的 preparation 保存历史。Core 安装与资源更新只在执行准备中发生，启动 daemon 不隐式准备，活动查询不安装 Core；目录版本固定到同一提交。失败不会回退到旧 Core。

activities 与包含活动观测的 plan 返回 activity_regions：每项含 key、activity_id、catalog_commit、regions_known 和 regions（zone_id、name、starts_at、ends_at、status）。区域截止由游戏表最后一秒转换为半开区间；窗口缺失为 unknown，不推断已解锁／通关。目录失败时 activity_regions=null、activity_regions_error 有原因，MAA 基础活动结果仍保留。

生命周期命令：原始 daemon.stop／daemon.restart RPC 仍返回受理 ACK；CLI 等待原进程退出，restart 再等待新 started_at 代次及有效配置就绪。默认持续等待，可通过 --timeout 限制等待，超时不自动重发／撤销。排空允许已经接纳的批次成员完成，批次父终态不依赖在线 client。

## 一次性官网公告采集

全局 `news.collect`，不接受 profile，必须显式提供 `params={"max_pages":2,"max_articles":20,"failure_backoff":60}`。三个整数分别为本次分页预算（1～100）、正文预算（1～1000）与失败退避秒数（1～86400），没有默认轮询或自动后台触发。

每次从官网第一页追赶到 end 或预算上限，按 cid 去重；正文预算优先覆盖尚未抓取的 ID，再按最早检查时间复查，连续小预算调用不会饿死后面的公告；截断时 scan_complete=false，返回 pending_ids，不能宣称追赶完成。相同 ID 的正文、标题等变化新增内容寻址修订；保留网页原始响应和结构化正文。支持 ETag／Last-Modified 与 304 校验缓存，分开 published_at、first_discovered_at、fetched_at、checked_at；旧抓取时间不会因 304 变成新抓取。来源错误记录退避及原始快照，取消不抹掉已完成证据。响应提供发现／修订 ID、摘要、完整性和 index 路径，不直接返回全部 HTML。

缓存位于配置 cache_directory/official-news；不读取账号、不安装 Core、不启动模拟器、不发送通知，也不修改 MAA 活动执行资格。公告中的预计时间仍是新闻证据，不能当作实际开放或 APK 下载／安装触发；解析模型及基于公告的额外触发仍待设计；APK 现使用独立的可配置每日排期。

plan.tasks 中可能包含项目自动插入的活动任务奖励 Custom，其 params.activity_reward 固定活动 ID、窗口、导航版本及 page_source（任务数据表、目录提交、活动与任务 ID）；它独立于 shop_completed、garbage 与完整通关资格。执行结果的 activity_reward_results 区分 claimed、nothing_to_claim、unavailable、incomplete、unknown，日程结果同步 activity_reward_result 事件。已确认任务页但无领取按钮时正常返回 nothing_to_claim，下次仍重新检查；此字段不表示所有活动奖励完成。入口模板、相对点击位置与商店横幅从固定解包路径生成，plan 和日程共用准备逻辑；resource_images 引用冻结 PNG 附件，素材准备失败通过 activity_shop_error／准备告警明确报告。

activity_reward 标记和结果携带 claim_roi_key；按钮回调提供有效区域时，结果另外包含 claim_roi。日程完成后 daemon 更新当前 ROI 资源缓存，下次 plan／日程准备自动复用；无按钮不写空值，缓存失效可全屏重新发现，不需要配置 ROI。缓存写入失败记录 activity_reward_roi_cache_error，不改变已完成的业务结果。详见 [ROI 制备](activity-reward-roi.md)。

plan 的 annihilation_copilot 返回剿灭 expert 的当前开放候选、固定作业、排除原因和目录版本；annihilation_copilot_error 表示补救准备失败，不取消原有委托。worker 只在 NoProxy 且唯一地图匹配时尝试冻结附件，见 [补救协议](daemon-copilot.md#剿灭-expert-无代理补救)。

## 私有活动素材准备

全局 assets.prepare，不接受 profile，params 必须显式给出 activity_id、bundles、resolve_dependencies、max_bundle_bytes、max_output_bytes、export_timeout。只下载／解码并发布私有 PNG 素材快照，不安装 APK、不启动游戏、不自动部署模板。版本、APK 基础资源边界、固定导出器、缓存／失败隔离及完整请求示例见 [素材流水线](activity-assets.md)。该操作可取消；产物 exported 不代表游戏验证成功。

## 单次日常控制

以下操作均要求 profile 和 idempotency_key，返回现有 operation 回执；时间为有限 Unix 秒。这组操作使用自身排期字段，不接受 latest_start_at。schema 与 client 仍不依赖 daemon，职责与状态归属见 [边界设计](daemon-design.md)。

| 方法 | 其他参数 | 行为 |
| --- | --- | --- |
| daily.postpone | occurrence_id、expected_revision、expected_state_version、not_before | 推迟选定的一次机会；必须晚于当前 due_at，且严格早于原下一次排期 expires_at |
| daily.skip | occurrence_id、expected_revision、expected_state_version | 完全跳过选定的一次周期机会 |
| daily.run | expires_at，schedule_id／inline 二选一，可选 not_before | 新建一次临时日常；省略 not_before 表示立即到期，繁忙则等待 |
| infrast.orundum.request | 无 | 登记手动进入搓玉的意图，等下次日常 expert 基建执行 |

先调用 daily.next，选择返回的 occurrence_id，再连同 revision 和该目标的 state_version 提交推迟／跳过。同刻多条日程分别返回，由客户端明确选择；查询不推进游标。只有尚未开始准备的机会可以修改，版本过期或目标已准备／运行时返回 conflict。推迟不改变身份、周期 TOML 或后续排期；提前跳过也不会推进游标而漏掉其他机会。维护期间仍可修改这些纯排期状态。

同键同参数返回原回执，同键不同参数 conflict；断线或响应丢失后使用 operation.find 找回。控制回执成功表示安排已经保存，不代表游戏运行成功。已接纳推迟在重启后保留；配置 revision 改变使旧推迟明确失效，不改绑下一次机会。已接纳跳过继续抑制原身份。

daily.run 引用现有日程时，该日程须属于目标账号，但可处于停用状态；本次不受周期 times／weekdays 限制，任务自身的 weekdays 仍有效。inline 支持日常任务及 expert、session、options、execution_timeout 等日常定义字段，不接受 id、profiles、times、weekdays、enabled 等周期字段。相对文件路径以 daemon 配置目录为基准。

```json
{"method":"daily.run","params":{"profile":"nemo","idempotency_key":"manual-daily-001","expires_at":1900000000,"inline":{"tasks":[{"type":"Recruit"}]}}}
```

接纳固定账号绑定、配置和显式文件字节，包括 expert 的多份基建文件；实际准备和恢复仍刷新库存、活动和自动作业。临时日常允许启动停止的实例，不消耗周期机会，也不占用等待期间的执行资源。expires_at 限制首次和恢复尝试的启动，过期不强停已运行的尝试；运行超时由 execution_timeout 控制。

临时日常与周期日常共用规划和业务恢复。operation.get 展示 occurrence、当前 attempt、恢复次数和下一恢复时间，首次失败不会提前终结整个操作。operation.cancel 取消整个临时日常，包括未开始的恢复；准备中的实际工作结束前不会释放其依赖或删除附件。排空保留尚在等待的临时预约；重启恢复明确未开始的工作，未知执行保留 recovery_required，不盲目重放。

搓玉登记成功只表示意图已保存，不立即开游戏、换班或消费材料。首次手动进入直接使用配置的 orundum 文件，由 Core 处理配方，不另加库存门槛；原有库存退出规则保持，自动配方切换仍待实现，材料优先级及切换阈值仍待明确。程序根据本次固定布局的房间产物回调确认模式已应用，然后消费意图；Core 链完成本身不算确认，已消费意图也不会在退出后自动复活。

若 Core 已确认搓玉而 Skland 仍返回更早的普通模式，基建准备明确等待新观测，避免旧数据将它切回。此处只比较已知应用与观测的先后，不设置任意数据年龄门槛。应用中的未知中断保留 uncertain 供核查，正常业务失败仍采用既有恢复；查询不消费意图。生产模式确认不代表干员换班全部成功。

## 暂停与历史失败重试

`daily.pause` 要求 profile、idempotency_key、布尔 paused；true 暂停该账号新的周期日常及其自动恢复派发，false 恢复。`daily.status(profile)` 查询暂停状态，daemon.status.daily_paused_profiles 列出暂停账号。暂停跨重启保留，不修改配置；准备和已运行任务继续，临时日常、手动重试和维护仍可用。等待机会仍按原期限过期；普通周期等待项在 daemon 停止时仍沿用原有取消规则，恢复不会补造已取消的机会。

`daily.retry` 要求 profile、idempotency_key、occurrence_id、expected_revision、task_ids、expires_at，可选 not_before。例如 task_ids=["task:1"] 选择原日程 tasks 的第二项。仅接受已经终结为 failed 或过期且有明确失败证据、没有活跃自动恢复的日常；目标必须有明确业务结果，所选任务必须失败或未执行。已成功／跳过任务、未知中断、仍在自动恢复的目标均拒绝。当前配置和原安排 revision 必须与 expected_revision 一致，防止任务索引改变后重试错误业务。

重试接纳为独立临时日常，回执携带 retry_of 和 task_ids，原历史保持。沿用原日常定义，接纳时重新固定账号绑定与选中任务的文件字节，运行时重新采集库存和规划，选中输入缺失则接纳失败；不会重放旧 Core 请求。选中业务不受当前星期限制，顺序和任务索引保持，已成功的其他业务不执行；session 首尾仍按定义执行。新的窗口、取消、幂等、恢复及重启行为与 daily.run 相同。`daily.get(profile, occurrence_id)` 只读返回原安排和最近 attempt 的 business_results，可取得原任务标识；缺少可信证据时使用 daily.run 明确发起新日常。
