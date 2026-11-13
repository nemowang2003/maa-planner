# 托管 Core 与原生运行环境

当前运行平台是原生 Windows + MuMu + 同平台 Python Core worker。Linux 的模拟器环境及交互方式尚不明确，因此暂不支持 Linux 运行；WSL 不支持启动 daemon。

配置使用 `[maa_core]`，只保留可选 directory、resource_dir、adb_path、connection_preset、touch_mode。平台由宿主决定，连接仅支持 MuMuEmulator12，并要求增强截图生效。旧 `[maa_cli]` 不接受，按 [配置说明](configuration.md) 手动迁移；程序不覆盖原配置。

新准备单元读取 Core 最新稳定 Release，在新目录安装并验证官方 MaaCore.dll 及资源；不再下载或启动 maa-cli。下载归 cache，安装归 data，生成配置和附件归单次 tmp。共享资源更新仍使用现有写入互斥，活跃任务继续读取其固定环境，不原地更新 Core。

批次成员共享一次准备；有活跃执行或准备时，新 batch 复用固定 Core／基础资源，跳过安装及资源写入，但重新获取一次公共热更新、目录与作业集列表。同一 batch 成员固定相同公共快照，晚到成员也保留该准备对象。全部结束后新准备重新检查 Core，失败不回退旧版本。公共活动和 Core 共用 daemon 下载的热更新快照，Core 直接加载增量资源，无本地 CLI HTTP 代理。

独立 worker 使用 ctypes、任务 JSON 与完整回调；资源加载、MuMu 增强截图及进程所有权见 [执行后端](core-backend.md)。官方接口参考 [Core C API](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.1/include/AsstCaller.h)，增强截图参数参考 [MuMu Extras](https://github.com/MaaAssistantArknights/MaaAssistantArknights/blob/v6.21.1/src/MaaCore/Controller/AdbController.cpp)。

离线开发验证在隔离临时目录进行。

准备诊断可通过 status.preparation 与 preparation 历史事件查询：分别记录 Core、公共资源、热更新指纹及关卡目录提交、是否安装／更新和失败阶段。启动 daemon 本身不执行准备，查询计划不安装 Core。
