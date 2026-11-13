# 缺失任务的最小素材包

解包产生的本地快照包含正确解码所需的共享依赖；这些缓存不是上传清单。上传／模型制作的输入必须先按“本次固定 Core 缺少的目标任务 → 必要素材 ID”筛选，不默认发送完整活动目录或全部依赖图片。

当前没有自动模型上传通道。`maa_planner.daemon.tools.asset_upload` 已实现离线筛选边界，后续服务接入只能发送这里生成的包；它不发送 HTTP 请求、不安装 Core、不部署模板、不采集游戏画面。

## 输入与命令

必须明确提供：

- assets 操作生成、带有效 READY 的导出快照。
- 本次实际使用的 Core 版本及完整资源目录，按 Core 加载顺序提供基础、外服／热更新和私有层。工具读取每层 tasks.json 及 tasks/ 下的全部 JSON，不能只拿主 tasks.json 判断活动任务缺失；例如 TL 专有节点在 tasks/Stages/TL.json。记录任务表集合哈希与数量，不把表内容或完整文件清单放入上传包。
- 每个目标任务所需的精确素材 ID。任务／素材用途尚不能确定时先明确需求，不退回全量上传，不支持通配符。
- 包总字节预算（包含 PNG、workspace.json 与 READY），及尚不存在的输出目录。

requirements.json 格式：

```json
[
  {
    "task": "TL-OpenOcr",
    "assets": ["dyn/activity/[uc]act55side/prefabs/act55side_activity_stage/bg_zone.png"]
  },
  {
    "task": "TLChapterToEX",
    "assets": ["dyn/activity/[uc]act55side/prefabs/act55side_activity_stage/bg_ex.png"]
  }
]
```

这是筛选示例，不代表 EX 需要改用 PNG；当前已验证的通用 EX OCR 仍可复用。缺少上游专用节点不等于现有项目回退不可用，调用方应先确认本次确实需要制作该任务，再列出素材。

```sh
uv run python -m maa_planner.daemon.tools.asset_upload \
  --snapshot /path/exported-snapshot \
  --core-resource /path/fixed-core/resource \
  --core-resource /path/fixed-hot/resource \
  --core-version 6.22.0 \
  --requirements /path/requirements.json \
  --max-bytes 100000 \
  --output /path/new-upload-package
```

需要的运行依赖仅为项目已有依赖，不要求 Pillow／OpenCV。Core 版本字符串由调用方固定，工具不加载 DLL 核验；必须使用同次准备的资源，不能把任意不完整目录当作已安装 Core。空／无效任务表、缺失继承节点明确报错，不推断“所有任务缺失”。支持 Core 前缀派生节点查找；OCR／JustReturn 可复用已知基节点，隐式 MatchTemplate 派生可能需要自己的 PNG，不能仅凭通用基节点存在就省略其素材。

## 输出与边界

Core 已提供的任务直接跳过；全部已有时返回 not_needed，不创建包。否则只复制缺失任务显式列出的素材，同内容按 SHA256 去重。重新检查导出来源身份、所选 PNG 的路径／哈希／大小及结构，拒绝符号链接、目录逃逸、缺失素材与超预算，不覆盖旧目录。

输出只包含所选 PNG、最小 workspace.json 及最后写入的 READY；不会附带完整导出清单、绝对本地路径、共享依赖图片或 Core 任务内容。workspace 可交给 [确定性制作工具](resource-tools.md) 查看素材；frames／required_strata 仍为空，须另建带截图和标注的新工作包后才能回放。selected_inputs 仅表示选料完成，不代表上传、离线模板验证或真实导航成功。

真实公开数据离线检查使用 6.22.0 的全部 89 份任务 JSON（按固定发布树 blob 校验）及已有 TL 617 张导出快照。上述示例跳过 TL-OpenOcr，只生成 1 张 bg_ex.png：43,616 字节图片、含清单共 44,667 字节。产物位于 /tmp/maa-minimal-upload-ma8coga_/packet；未上传到模型或外部服务。
