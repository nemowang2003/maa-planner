# daemon、规划和控制层

当前验收进展：

当前命令与配置见 [运行说明](runtime.md)，协议见 [控制 API](control-api.md)。实现状态与后续工作集中见 [后续规划](configuration.md)。已提供可选 Windows 登录会话托管管理入口，未执行实际部署；不依赖 NSSM 或 systemd。

职责调整与单次控制的设计依据见 [daemon 边界设计](daemon-design.md)，公开参数和使用方式以 [控制 API](control-api.md) 为准。

## 日常规划与调度接口

daemon.planning.expert 的基建函数回答“当前库存和实际生产状态应选择哪份原生配置”。日常任务的顺序与参数来自 schedules，由 planning.daily 固定为 Core 任务；普通 Fight 在固定计划及 worker 提交时统一移除 times、固定 series=0，剿灭尝试上限与用药／源石参数独立处理；明确标记 strategy=expert 的基建调用库存策略，普通 Fight 调用活动材料／芯片保持／有序后备选图策略：多活动按截止从早到晚跳过垃圾时间活动，材料库存并列取首个推荐，无材料缺口时取最高搓玉效率推荐。plan API 通过 schedule_id 明确选择日程，复用同一规划器预览任务与附件，不启动游戏；剿灭 Fight 可显式启用 expert，仅依据本次 Skland 本周进度判断是否完成，未完成下个 batch 重新观测；明确的 Core 步骤错误另经[公共恢复排期](daily-step-retry.md)告警并延后恢复。

policy 的 Context.activity 是最近查询或准备得到的可选快照，启动时可以为 None；policy 本身不负责联网刷新。日常准备共用环境和活动快照，基建 expert 需要的个人库存和配方由 daemon 采集。

daemon.policy.Policy 回答“当前时刻是否启动、启动谁、下次何时评估，以及准备完成后是否仍准入”。默认 DailyPolicy 根据 schedules 返回到期安排和最近唤醒时间，不做 I/O；DailyScheduler 负责去重、繁忙等待、准备、收集数据和生成单账号请求。EmptyPolicy 保留供注入使用。

主循环等待状态事件或最近的业务时刻（含已持久化的活动作战截止），不做固定每分钟轮询。文件变化、相关 API 操作、准备完成和 runner 结束都会唤醒协调；定时唤醒后重新验证状态，不由旧定时回调直接启动 runner。日常最终准入检查配置 revision、安排状态及有效期；显式 run/copilot 保留原有准入语义。

## 职责和入口

- 主程序将 daemon 命令交给 daemon 包入口；提供 start/status/stop/restart，离线辅助为 daemon config example/validate/dump。start 等待后台 daemon 就绪后返回。
- interfaces 下的常驻进程负责各自平台的消息收发，通过 schema 与独立 ControlClient 使用 Socket.IO 会话；企业微信已有独立可执行入口。
- ControlAPI 适配协议并委托应用服务；OperationService 保存幂等状态、跟踪操作和报告结果。Coordinator 统一登记资源使用、实例占用、维护屏障和活跃准备依赖；业务通过窄方法申请和释放，不再遍历各调度器容器猜测占用。
- daemon 是协调者，管理配置快照、worker、最终准入、实例互斥、排空与退出。
- worker 只处理一个不可变 RunRequest，不知道账号总数，不读全局配置、不写 SQLite。
- runner 只管理一个模拟器和一份 Core 任务，持有实际资源身份锁，不更新公共依赖。

schema 定义公开 API 的请求、响应、参数校验、操作及事件类型，不建立连接，不导入 daemon 或 interfaces。解析后的 Settings、运行依赖路径和 worker 的 RunRequest 属于 daemon 私有模型，不要求接口进程复用。daemon 与 interfaces 互不依赖。企业微信接收消息、内存去重、账号权限与私聊通知位于 interfaces/wecom 独立程序，详见 [企业微信客户端](wecom-client.md)；没有 Android 占位。接口进程断开不影响已接纳的操作。

DailyScheduler 持有一次日常的业务状态，DailyControl 处理周期目标的一次推迟／跳过及临时来源，ScheduledRetries 保留失败与未完成业务。临时日常复用 occurrence、实际规划和恢复流程，不另设通用 Job 队列；它的 operation 关联整个日常，而非仅第一轮 attempt。daily_preparation 共用预览和实际日常的账号采集及可选准备，调用方负责告警、状态和公共依赖生命周期。

资源准入中的 accepted_configuration 只允许已接纳临时任务继续使用固定配置，不授予维护插队或排空豁免。准备取消发出协作信号，等实际采集结束后再释放占用；共享环境准备没有取消入口时等待其现有 I/O 结束，不能只取消 awaiter 就宣称线程已经停止。

## 配置与状态

默认配置为 %LOCALAPPDATA%/maa-planner/config.toml，可用 --config 指定，包含账号名称、MuMu 绑定、森空岛认证映射、Core 运行依赖和日程。安装根目录和外部共享资源路径可选覆盖，内部路径自动推导，缓存使用 platformdirs 的宿主默认路径，Windows 使用本机默认目录；官方在线来源由程序内置；新准备单元查询 Core 最新稳定发布，已有执行则复用其环境并跳过 ensure，详见 [配置边界](configuration.md)。未知字段明确报错，不静默接受。

启动时读取配置，运行期间通过操作系统原生事件监听配置所在目录，过滤目标文件并合并短时间内的事件，能发现直接写入、删除重建及编辑器的原子替换。没有定时文件轮询，普通调度唤醒不会重新读取配置。

config.get 返回已接纳的原始 TOML、解析后的配置、revision 和文件错误；config.replace 接受完整 TOML 以及可选 expected_revision。API 修改与文件加载共用校验与配置锁；提交时先读取尚未处理的外部修改，再检查 revision，原子写入文件，切换内存快照并唤醒调度。TOML 的注释与排版按提交内容保留。两个 API 提交会串行处理；外部编辑器不参与配置锁，同时写入时仍以最终文件内容为准。

有效配置生成 revision 和 SQLite 快照；无效文件保留已有快照、报告错误并拒绝新操作。API 候选配置无效或写入失败时不会接纳它。已接纳的显式操作，包括准备中的 copilot，使用固定快照，后续配置变化不会取消它们；取消、期限与停止规则仍有效。策略产生但尚未获准启动的普通任务仍检查 revision。

不限制不同模拟器的并行任务数量；相同模拟器仍互斥，公共写入维护仍要求全局空闲。

SQLite 保存配置、执行 attempt、日常安排去重与状态、已解析依赖快照、显式操作、事件、告警、本地通关账本、商店证据和活动作战窗口，没有 APK 版本历史。旧 weekly_annihilation 表在 schema 11 删除，不保存剿灭尝试及 Core 完成标签。日常安排标识由日程 id、账号、计划时刻组成，不受无关配置 revision 变化影响；定时器本身不持久化。状态与锁的宿主默认路径见 [存储归属](configuration.md#存储归属)，显式 XDG_STATE_HOME 可覆盖锁的应用根目录；改变配置、通信端点或数据库目录不能绕过单实例／资源互斥。原生 Windows 使用本机状态目录，WSL 仅用于开发测试。

API 使用独立 schema 方法清单、无 daemon 依赖的 client 与薄 RPC 适配器。操作、观测／规划和配置分别由应用服务承担；OperationService 不接收 daemon，通过 ExecutionAccess 调用准备、准入与执行。daemon 作为装配及驱动入口，协调器拥有资源占用登记，日常等待及恢复队列由对应业务组件持有。operation.get／operation.find 不持久写入状态，批次与执行状态由协调回调主动更新。

顶层 operation_id 和批次成员 ID 由服务端生成。客户端以 idempotency_key 区分网络重试和主动再执行；活跃记录及终态日志支持跨重启找回，删除日志后不保证对应去重。此协议不代替崩溃恢复，也不证明游戏动作恰好执行一次。历史事件可主动分页查询；推送仅针对在线会话，不补发、不保存投递游标。NotificationHub 负责在线收件人与操作归属，操作通知携带去重键以支持 ACK 前到达；断线不取消操作。详见 [控制 API](control-api.md)。

## 单账号执行

RunRequest 固定 profile、模拟器编号/名称、Core 路径、任务 JSON、附件、实例选项及超时。GUI 路径仅为可选的外部占用检查，不读取或修改 GUI 配置。

1. daemon 为显式批次或日程选择共享准备对象。空闲时检查 Core、资源及热更新；已有使用者则复用，不兼容则拒绝。
2. daemon 固定依赖、任务、作业及账本快照，启动只负责单账号的 Python worker。worker 报告实际 PID／创建时间，daemon 取得 Job 所有权后允许其准备。
3. worker 获取模拟器独占锁、可选 GUI 锁和依赖读锁，检查恢复隔离、实例及实际二进制。普通任务按需启动模拟器，纯 Copilot 要求游戏已运行；准备私有附件和资源视图。
4. worker 经控制通道请求最终准入，daemon 复查停止、期限及日程有效期；显式操作保留接纳时配置。准入后由该 worker 直接加载并启动 Core，执行固定任务和业务阶段。
5. 确认安全退出，只关闭本次启动的模拟器，归档日志并清理临时目录；不确定时保留工作目录、模拟器现场及恢复隔离。

取消是请求，不能把线程或通信中断当作外部进程已停止。成功退出只证明执行器进程完成，游戏结果仍为 unknown。

## 维护与生命周期

共享资源和 APK 安装只由 daemon 管理。新的执行准备单元检查并按需更新资源、准备 Core 和热更新 JSON；同批及活跃环境的新成员复用准备结果。显式 runtime-prepare 使用同一准备流程，worker 不更新依赖。显式写入维护采用保守的全局维护屏障：有执行、准备或后台查询时拒绝写入维护；维护期间拒绝新的资源使用操作，纯状态查询与单次推迟／跳过、搓玉意图登记不占资源。共享目录仍使用真实身份读写锁，指向同一目标的链接不会形成不同的锁。

stop/restart 默认停止接纳并排空已有操作；restart 在资源、控制服务、SQL 清理后重新启动解释器；普通模式 Windows 的 PID 可能变化；service run 托管模式在同 PID 内重建 daemon 代次。force 请求取消 worker，已开始的写入维护仍等待安全完成。无效配置不会替换已有快照，排空期间配置失效会取消正常重启。

原始 stop/restart RPC 返回受理状态；CLI 等待原进程退出，restart 再等待新代次就绪，支持显式 --timeout。SCM 会话代理未实现，正式托管方式待用户确认，见 [运行文档](runtime.md#windows-登录会话托管可选)。

异常重启不重放运行中断的操作。明确 Core 错误后已排定、尚未开始的日常恢复可跨重启保留；未完成执行保留恢复要求，其他中断操作标为 recovery_required；恢复通过 daemon 的 recover 行为核对进程和模拟器，不杀人工进程、不关闭模拟器。
