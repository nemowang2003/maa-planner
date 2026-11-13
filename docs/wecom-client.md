# 企业微信客户端

`maa-planner-wecom-client` 是独立前台程序。它通过企业微信智能机器人的出站 WebSocket 长连接接收命令，通过公开 Socket.IO API 操作 daemon，不导入 daemon 实现、不访问 MuMu，不隐式启动 daemon。启动、停止和重连与 daemon 分开管理。

接入格式参考本机 `codex-wecom-relay`，并核对企业微信官方 [智能机器人 SDK](https://github.com/WecomTeam/aibot-node-sdk) 的认证、消息回调、应用心跳、回执和主动发送协议。这里不依赖 Codex，不复制 relay 的持久化投递、线程令牌或 app-server 功能。

## 配置与启动

凭证及权限放在独立的 `wecom.json` 中，不放入 daemon 的 config.toml：

| 系统 | 默认配置文件 |
| --- | --- |
| Windows | `%APPDATA%\maa-planner-wecom-client\wecom.json`，通常位于用户的 AppData\Roaming 下 |

使用 platformdirs 的用户配置目录，明确禁用额外的厂商目录；可用 `--config PATH` 覆盖。文件不会自动创建，`--example` 仅向标准输出打印模板，不启用账号或建立连接。Windows 使用当前用户私有的配置目录。凭证不放在命令行，不打印配置内容，日志不记录 BotID、Secret、用户命令或远端原始帧。

```powershell
uv run maa-planner-wecom-client --example
uv run maa-planner-wecom-client --config .\wecom.json --check
uv run maa-planner-wecom-client --config .\wecom.json
```

Windows 可直接使用相同程序，将配置路径换成原生 Windows 路径。安装包会生成独立的 `maa-planner-wecom-client` 入口；`maa-planner wecom` 只是同一个入口的别名。Ctrl+C／SIGTERM（Windows 另支持 SIGBREAK）结束客户端并关闭连接，不停止 daemon 或取消已接纳游戏任务。本次不部署服务、自启任务或任何真实企业微信连接。

daemon 地址默认从 [控制端点文件](control-api.md#调用边界) 发现，可用 `--endpoint PATH` 覆盖。只连接已有的本机 daemon，断线期间不会创建或启动它。实际部署时，客户端与游戏 daemon 在同一台原生 Windows 主机运行。Linux 的模拟器环境及交互方式尚不明确，因此暂不支持 Linux 部署。

```json
{
  "bot_id": "填写机器人 BotID",
  "secret": "填写机器人 Secret",
  "admins": [
    "admin-user-id"
  ],
  "routes": {
    "nemo": [
      "admin-user-id",
      "user-id"
    ],
    "wyf": [
      "admin-user-id"
    ]
  }
}
```

四个字段都必须明确配置，不接受其他字段。允许使用机器人的用户由 admins 与所有 routes 收件人的并集自动计算，无需单独配置白名单。列表不允许重复项；admins、routes 和账号收件人列表都可以为空，两者全空则无人可操作。`admin` 是独立管理员告警频道，自动仅投递给 `admins`，不能放进 `routes`，也不能作为游戏账号名；管理员可用 `/alerts admin` 查询该频道。管理员不会自动加入任何账号路由；要接收某账号告警，仍须将其显式列在该账号的 routes 中。修改配置后重启客户端生效。

## 用户权限与账号路由

routes 的键是 daemon 的 profile 账号名，不是 MuMu 安装目录或实例编号。一个账号可通知多个用户，一个用户可拥有多个账号。

- 既不在 admins 也不在任何 routes 中的用户：忽略消息，不调用 daemon，不向其发送回复。
- 普通路由用户：只可查询、执行、取消 routes 中分配给自己的账号。命名方法按 schema 清单检查 profile，批次检查每个成员；查询或取消已有操作先读取所属账号，跨账号批次拒绝。拒绝时不返回该操作的结果，也不发送取消请求。
- admins：可使用完整 API，包括全局状态、配置、事件历史、维护及 daemon 生命周期操作，也可操作未给自己配置通知路由的账号。
- 账号告警／解除：仅发给该账号 routes 中的用户；无路由则不发送。管理员不会自动收到所有账号告警。没有账号的全局告警发给管理员。
- 显式操作的完成通知：仅发给发起该操作的用户；批次发送父操作完成结果，子操作详情可用 /get 查询。自动日程的账号告警仍按 routes 发送。

私聊命令通过原回调的 req_id 回复，使用一次性完成的 stream 消息；独立告警和操作完成通知使用用户 userid 主动发到私聊。群聊中授权用户发来的命令也只回复其私聊，不向群聊回显结果，不以群成员身份推导权限。收到的身份以企业微信认证连接内的回调为准，不从命令正文提取 userid。

## 命令

| 命令 | 行为 |
| --- | --- |
| `/help` | 帮助，不依赖 daemon 在线 |
| `/status` | 完整 daemon 状态，仅管理员 |
| `/alerts [账号名]` | 普通用户省略账号时逐个查询自己的账号；指定账号时校验权限。管理员省略账号查询全部 |
| `/get 操作ID` | 查询操作状态／结果，普通用户先校验所属账号／全部批次成员 |
| `/cancel 操作ID` | 按相同账号权限请求合作式取消，不把请求取消当成任务已经结束 |
| `/api {"method":"...","params":{...}}` | 完整公开 API 入口；普通用户仍受账号权限约束，未知或全局方法仅管理员可调用 |

例如查询属于自己的账号告警：

```text
/alerts nemo
```

管理员可以显式提交公共信息查询：

```text
/api {"method":"activity.fetch","params":{}}
```

异步命名方法省略 idempotency_key 时，根据 BotID 和企业微信 msgid 生成稳定去重键；显式键保持原值。operation_id 由 daemon 生成，用于 /get；提交响应丢失时通过 operation.find 按去重键找回。其他字段遵循 [控制 API](control-api.md)，不提供自然语言意图解析。当前没有“运行一次日程”的 API，因此不提供虚假的 /run daily 命令；显式 run 仍需提交固定 plan，执行／维护必须由用户明确发送对应命令。

同一用户的命令按收到顺序处理，不同用户可并行。接纳响应先发送，随后才发送对应的完成通知，即使操作很快结束也保持该顺序。

## 连接、重复消息与边界

企业微信和 daemon 各自重连，指数退避从 1 秒到最多 30 秒。企业微信使用 cmd=ping 的应用心跳并等待回执；不主动发 WebSocket 心跳，不经过环境代理，也不协商压缩。认证明确失败时退出；连接被同 BotID 的另一个实例替代时退出，避免两个客户端反复争抢。

只投递在线消息，无持久化发件箱、通知游标或离线补发。需要时显式 /alerts 或 /get 查询。企业微信发送失败不会重做游戏操作，丢失平台回执也不自动重发消息。

回调按 msgid 在内存去重，处理中消息单独记录，最近完成的 4096 个 ID 跨连接重连保留；进程重启不恢复这份缓存。异步命名方法另有上述稳定去重键及 daemon 当前的操作去重；不因此承诺其他全局命令在进程崩溃后恰好执行一次。超时、断线显示结果可能未知，不自动重发执行请求。

接收队列和发送队列有容量上限，平台发送逐条等待回执；积压或回执超时会结束当前连接，避免无限缓存。Markdown 按 20,480 UTF-8 字节分片，不拆坏 Unicode 字符；长回复可能分成多条；回调回复的每片使用独立 stream.id，finish=true，不逐字刷新。首版不支持附件上传或模板卡片。

## 平台主动推送条件

企业微信[智能机器人长连接文档](https://developer.work.weixin.qq.com/document/path/101463)规定：用户需要先在相应会话中给机器人发过消息，机器人才能主动向该会话推送。群中互动不代表已经建立用户私聊的推送条件；本客户端只向配置用户私聊发送通知，首次使用请先私聊机器人发送 /help。

每个会话的回复和主动推送合计限制为 30 条／分钟、1000 条／小时；改用回调回复不能绕过频控。文档“回复消息”一节的 24 小时窗口针对收到回调后的回复，“主动推送消息”一节没有列出 24 小时窗口，不将其解释成用户必须每天发一次消息。平台拒绝仍以实际回执为准；本客户端记录错误码，不自动重试或重做游戏操作。
