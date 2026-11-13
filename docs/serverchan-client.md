# Server酱客户端

`maa-planner-serverchan-client` 是独立的只接收通知程序。它连接本机 daemon 的 Socket.IO 控制端点，不发送业务请求、不查询历史、不执行任务；每收到一条有账号路由的通知，就向该账号配置的每个 URL 发一次 HTTP POST。客户端不会隐式启动 daemon。

使用的是 [Server酱³ 官方 API](https://doc.sc3.ft07.com/en/serverchan3/server/api)：`https://<uid>.push.ft07.com/send/<sendkey>.send`，SendKey 为 `sctp<uid>t...`。复用 aiohttp，直接发送 JSON `title`、`desp`，不引入 Server酱 SDK、不调用 Turbo 接口、不将 SendKey 放入域名。配置中的完整 URL 原样使用；不自动增加或移除 `.send`。服务成功以 HTTP 2xx 且 JSON `code=0` 确认，参见 [官方响应定义](https://doc.sc3.ft07.com/en/serverchan3/server/sdk)。

## 配置

| 系统 | 默认配置文件 |
| --- | --- |
| Windows | `%APPDATA%\maa-planner-serverchan-client\serverchan.json` |

配置直接是账号名称到 URL 列表的映射，没有额外 routes 包装层：

```json
{
  "nemo": [
    "https://123.push.ft07.com/send/sctp123tREPLACE_ME.send",
    "https://456.push.ft07.com/send/sctp456tREPLACE_ME.send"
  ],
  "wyf": ["https://456.push.ft07.com/send/sctp456tREPLACE_ME.send"]
}
```

账号名使用 daemon 的 profile 名称；另外可配置 `"admin": ["管理员的 SC3 URL"]` 接收管理员频道通知，仅向该键的 URL 转发，不复用其他账号的收件人。此配置由管理员维护，admin 频道 URL 应只填写管理员自己的目的地。URL 中的 UID 与 SendKey 的 UID 须一致，仅接受该 SC3 HTTPS 地址格式，不带查询参数、片段、端口或用户名密码。相同账号内的重复 URL 报错；不同账号可以使用同一 URL。空映射和空 URL 列表表示不转发对应通知，不隐式订阅全部账号。

URL 本身含推送凭证，不打印到日志或配置 repr。Windows 使用当前用户私有配置目录。程序不创建真实配置；修改后重启客户端生效。

## 启动与离线检查

```powershell
uv run maa-planner-serverchan-client --example
uv run maa-planner-serverchan-client --config .\serverchan.json --check
uv run maa-planner-serverchan-client --config .\serverchan.json
```

`--example` 只输出模板；`--check` 只校验配置，均不连接 daemon、不向 URL 发推送。安装包提供独立可执行入口，`maa-planner serverchan` 是同一个入口的别名。`--endpoint PATH` 可覆盖 [本机端点文件](control-api.md#调用边界)，不改变 daemon 的配置。实际部署时，客户端与游戏 daemon 在同一台原生 Windows 主机运行。Linux 的模拟器环境及交互方式尚不明确，因此暂不支持 Linux 部署。

Ctrl+C／SIGTERM（Windows 另支持 SIGBREAK）只停止此客户端，不停止 daemon 或取消任务。没有部署服务、自启或真实推送 URL。

## 消息与失败行为

标题包含账号名和 notification.message／kind，desp 为通知的完整 JSON，保留账号、告警类型、原因及结构化详情。所有 kind 都按 profile 路由；无账号、没有匹配路由或路由为空时不发送。当前 daemon 广播的是告警／解除通知；操作进度和完成消息只发给发起会话，此只读客户端不会主动提交任务，因此不会替其他 client 查询或补造完成通知。

每个唯一 URL 有独立、有界队列和发送协程，保持该 URL 的接收顺序。某个 URL 拒绝、超时或过慢不会阻塞其他地址；队列满时丢弃该地址的新通知并记录不含 URL 的日志。HTTP 总超时 15 秒，不跟随重定向，不经过环境代理，错误日志不包含响应原文或 SendKey。

每条通知对每个目标只尝试一次，不因失败或回执丢失自动重试。daemon 断线后客户端重新读取端点、按 1～30 秒退避重连，不读取历史、不补发断线期间通知；已接收的内存队列可以继续投递。进程退出丢弃未完成队列，没有持久化发件箱或跨进程恢复。code=0 只表示接口接受，不保证手机已经展示。
