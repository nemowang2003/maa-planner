# 活动资源离线制作工具

`scripts/activity_resource_tools.py` 实现素材枚举、查看、并排比较、候选参数校验、确定性物化、正负样本回放、失败检查和不可变私有快照发布。所有参数显式提供，不调用模型、不采集游戏、不购买、不改共享资源、不清除本期商店停用。

工具通过 PEP 723 声明独立依赖，使用 `uv run scripts/activity_resource_tools.py --workspace /path/workspace.json <command>`。NixOS 的二进制 Python 轮子需要编译器运行库，可通过 Nix 提供；Windows 使用同一脚本。运行期不依赖 NumPy／OpenCV；固定路径导航的准备阶段使用 Pillow，见 [掩码导航](core-backend.md)。

## 工作包

从导出快照导入素材前，先生成[缺失任务的最小素材包](asset-upload-selection.md)：已有 Core 任务跳过，仅保留必要素材，不把共享解码依赖当上传输入。

workspace.json 的路径相对于工作包目录，拒绝逃逸到目录外的文件或符号链接。PNG 必须携带 SHA256；读取时再次校验，变更需建立新工作包。顶层字段：

| 字段 | 内容 |
| --- | --- |
| activity_id、res_version、bundle_hash、core_version | 活动及固定资源版本证据，不能省略 |
| assets | 素材列表，每项含 id、path、sha256 |
| frames | 截图列表，每项含 id、path、sha256、split、background、state、target |
| required_strata | 必须覆盖的 `[background, state]` 列表 |

split 为 development 或 validation；target 为 `[x,y,width,height]` 标注按钮区域，负样本填 null。截图必须是 Core 的 1280×720 坐标基准，不能将任意缩放截图当作同一坐标。每个背景／状态组合需有两组独立的正负样本；验证集不得复用开发集的像素。跨状态覆盖仅限显式声明且实际提供的组合，未声明的背景不视为通过。

候选 JSON 的所有字段均须提供，例如：

```json
{
  "asset_id": "shop-title",
  "crop": [20, 15, 110, 36],
  "size": [110, 36],
  "background": null,
  "roi": [0, 0, 1280, 720],
  "click": [900, 500, 80, 40],
  "threshold": 0.9,
  "model_version": "human",
  "prompt_version": "manual-v1"
}
```

这是格式示意，阈值、裁剪和点击框不是 TL 默认配置。crop 使用原图坐标；size 是输出模板尺寸，缩放固定采用 Lanczos。background=null 只允许完全不透明的裁剪，否则必须显式指定合成底色 `[R,G,B]`。不会将透明像素或 Core maskRange 的语义混为一谈。

## 命令

所有命令向标准输出返回 JSON。`list_assets` 返回来源、哈希及尺寸；`inspect_image ID --crop x y w h --output preview.png` 输出原尺寸裁剪及坐标映射；`compare_frames ID... --output frames.png` 原尺寸并排展示，最多八张，不自动缩放。输出不覆盖既有文件。

以下命令都需要 `--candidate candidate.json`：

- `propose_candidate` 校验参数，返回预期节点与模板哈希，不写图片。
- `materialize_candidate --output candidate.png` 输出确定性 PNG。
- `replay_candidate` 返回逐帧分数、匹配框、误命中／漏检、耗时及覆盖缺口。
- `inspect_failures` 只保留失败帧条目，可用 frame_id 再查看原图或匹配窗口。
- `submit_artifact --output snapshots` 重新回放，全部通过后发布内容寻址目录；失败不发布。

回放使用 OpenCV TM_CCOEFF_NORMED。正样本既要达到阈值，匹配框与点击框也必须位于标注区域内；负样本必须不命中。缺少覆盖、非独立验证样本或任一失败均为 needs_review。此离线匹配不冒充 MaaCore 的完整识别算法或实际页面到达。

发布目录包含 template.png、带 base64 私有附件的 plan.json、版本／参数／逐帧报告 manifest.json，最后写 READY。无 READY 的目录是不完整现场，不应加载；同一身份禁止覆盖。plan.json 可由 CoreTasks.read 读取，节点、图片都使用 Planner@ 命名空间。它是候选入口任务，不提供页面到达或购买成功证明，不自动加载到账号日程。实际验证需复用商店首次准入，不能通过资源发布绕过停用。

## 检查

工具的独立环境测试覆盖成功回放／私有附件往返、缺少验证样本、误命中、点击框错误、来源变更、目录逃逸及禁止覆盖；与 daemon 无关，不启动游戏。运行：

```bash
uv run --with pillow==12.2.0 --with numpy==2.5.4 \
  --with opencv-python-headless==5.0.0.93 \
  python -m pytest -q tests/tools
```

NixOS 可在上述命令前设置 `LD_LIBRARY_PATH="$(nix eval --raw nixpkgs#stdenv.cc.cc.lib.outPath)/lib"`。默认业务测试仍为 `uv run pytest -q`；模型调度、自动发布配置和真实截图包尚未实现，仍依赖设计／现场证据。
