# 活动素材版本、下载与导出

全局 `assets.prepare` 操作将当前官方版本和显式 bundle 输入准备成不可变 PNG 快照。它不检查模拟器、不安装 APK、不加载 Core、不改共享 GUI 或解除商店停用；该显式操作导出后不自动启用模板。支持显式启用官方索引依赖闭包。日程和 plan 现在通过 [固定路径掩码导航](core-backend.md) 自动调用同一后端制作商店／领奖入口及商店横幅；其他模型选择与自动触发仍是后续工作。

## 请求与来源

调用 assets.prepare，不提供 profile；下面为直接参数，另需提供 idempotency_key，所有预算必须显式指定：

```json
{
  "activity_id": "act55side",
  "bundles": [
    "activity/[uc]act55side.ab"
  ],
  "resolve_dependencies": true,
  "max_bundle_bytes": 67108864,
  "max_output_bytes": 67108864,
  "export_timeout": 120
}
```

示例预算不是默认策略。bundles 最多 100 个根输入，拒绝目录逃逸和大小写别名；resolve_dependencies 必须显式指定。false 只处理声明输入，true 下载并验证同版本官方 .idx，按 bundle 向量索引遍历依赖，最多 1000 个输入。当前读取器为 prefix128-flatbuffers-reader1，布局变化、缺失目标或重复名称明确失败，闭包和索引哈希留作证据。索引压缩与展开各限 64 MiB；两个大小预算上限均为 1 GiB，导出超时上限一小时。bundle 预算按总展开数据约束，并限制每个下载容器；metadata 单次另限 8 MiB，固定导出器 ZIP 另限 256 MiB。不提供任意 URL、输出路径或 shell 命令参数。

网络配置来自官方 ak-conf.hypergryph.com，分别保存 clientVersion、resVersion、完整热更新清单的来源哈希和原始响应。manifest.versionId 必须与 resVersion 相同。普通热更新 bundle 按清单长度／MD5 验证，归档只读取唯一的指定成员，不展开其他路径；校验通过后缓存 AB 并计算 SHA256。共享 bundle 不因换资源版本而重复下载。匿名 cat=1／meta=1 的 .bin 条目使用四位修订 token，它不是 MD5；此类输入校验官方 HTTPS 来源、ZIP CRC／长度并计算 SHA256，缓存关联完整 manifest 哈希，不能跨清单把短 token 当作内容地址。

清单没有该输入时，优先从现有 game_cache/latest.apk 的 assets/AB/Android/ 对应路径取基础资源；缺少匹配缓存则用受限 HTTP Range 读取官网最新 APK 的目录、manifest 和选定成员，不下载整包、不安装游戏或启动模拟器。两种来源均核对官服包名及固定 clientVersion；远端校验 URL／ETag、成员 CRC 与长度。目录／manifest 保留原 2 MiB metadata 预算，版本确认后才允许按显式 bundle 预算读取成员，结果内容寻址缓存。缓存 APK 由已有 artifact 锁保护；导出不改写它。输入 bundle 和来源类型逐项保留，不能将 APK 版本当作热更新版本。

## 导出与发布

固定 ArknightsStudioCLI v1.2.1，Windows 使用已验证的 .NET 8 Portable 包，Linux 开发测试使用已验证的 Linux 包并在 .NET 8 上显式大版本前滚。需系统已有 dotnet；不自动安装系统依赖。缓存命中仍从固定 SHA256 的归档推导 DLL／依赖预期哈希，不以可修改的缓存 metadata 授权新可执行文件。

每次输入按原 bundle 路径复制到私有 job，仅导出 Sprite／AkPortraitSprite，保留 containerFull 和 PNG。进程支持取消、超时与输出预算，必要时结束自己的导出器进程树；日志、输入与来源 metadata 留在该 job。失败不改旧快照，也不发布半成品。

PNG 路径按上游单文件目录规则整理，拒绝大小写／路径冲突与目录逃逸；记录尺寸、大小和 SHA256。输入 bundle 哈希、导出器哈希与选项相同则复用导出结果；资源版本改变时建立新的来源快照，不改旧记录，也不重复解码同一输入。

Windows 文件 IO 使用扩展路径，支持未启用系统级长路径的机器。临时目录发布到 64 位内容哈希目录后，子图片路径可能超过 260 个字符；普通路径此时可能报文件不存在，不能将它误判为导出丢失。缓存发布、重新读取和导航模板制备均支持扩展路径；互斥仍按原缓存路径获取，不另建一套资源锁身份。原生回归覆盖深目录发布后读取及再次复用，不修改系统注册表。

完整暂存后才将目录原子发布到 cache_directory/activity-assets/snapshots/内容身份，READY 最后写入暂存内容。结果返回路径、身份、版本、素材数、复用状态及检查时间。无 READY 的 job／目录是失败现场，不作为已发布快照。dependency_scope 为 explicit_bundle_inputs 或 index_closure；后者证明当前官方索引中指定根输入的依赖闭包，不证明未来索引格式或所有活动素材都已验证。

产物只标记 exported，不代表模板匹配、页面到达或业务成功。解包依赖只留在本地缓存；交给模型前必须按 [最小素材包](asset-upload-selection.md) 对照实际 Core，筛选缺失任务明确需要的素材，不上传完整解包目录。后续按 [离线制作工具](resource-tools.md) 的工作包规范，固定 Core 版本并补充正负截图与标注后制作模板；实际游戏验证仍须沿用账号准入和商店首次结果保护。
