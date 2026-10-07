# 远程调试验证记录

环境：2026-10-07，Linux x86_64、Qt 6、GCC，31 GiB RAM；`QT_QPA_PLATFORM=xcb`。测试配置使用独立 `/tmp/play-remote-check-*` / `/tmp/play-remote-settings-*` / `/tmp/play-normal-focus-*` 目录，ISO 仅作为外部输入。

## 已通过

- 服务开启：Release 构建成功；8 项 CTest 全部通过（原测试、调试器 MemoryViewModel、Python SDK 六项测试）。
- 服务关闭：原 `build` Release 构建成功；原有 6 项 CTest 全部通过。
- 协议：慢客户端发送超限主动断开并允许新控制连接、逐字节握手、同一写入两条请求、非法 JSON、0 长度、超过 8 MiB 长度、版本不匹配、第二控制客户端、断开及重连接管。
- CLI：真实单次 --json status 及交互 CPU 选择、寄存器、单步、断点、继续和暂停流程。
- SDK：事件与响应混排、二进制多块下载、结构化异常、请求超时取消且不重试修改；握手方法目录与全部 SDK 入口一致。
- 确定性 ELF：四个 CPU 的实际停止事件与对应 CPU 名、完整寄存器描述/128 位往返、只读寄存器拒绝、MMIO 拒绝、内存往返、编译过的指令修改后再次执行得到新结果、断点/停止原因、JAL 编码。
- 元数据/分析：标签保存加载、表达式及除零处理、调用栈、搜索、EE/VU 分析、常用函数、ELF、EE/IOP 模块与内核对象。
- 图形：运行和暂停截图、帧捕获下载、暂停等待帧超时、任务取消；同一转储分别使用独立 OpenGL/Vulkan 会话查询、定位、导出帧缓冲和转储；Vulkan 深度图、基础纹理。
- 捕获 VU1 固定转储：VI1 从 0 经 IADDIU 变为 7，微程序反汇编、微程序/数据内存、受控寄存器写入、GIF 数据和分析。
- 普通模式：通过读取自身测试进程的 fixture RAM 计数器确认失焦时停止、重新聚焦后推进，保存偏好仍为 true，窗口正常退出。
- GUI Reset：实际 Ctrl+Shift+R 重置使旧请求、制品、回放引用失效并取消捕获任务。
- 实际 X11 焦点测试：运行后切换到独立 QA 窗口，帧仍推进；暂停及单步后返回模拟器窗口仍暂停；断开继续运行的会话后重连暂停接管。
- Qt 窗口正常关闭、remote service 排空回调和释放回放实例，退出码 0。

最终 OpenGL/重置/慢客户端/单次及交互 CLI 集成记录在 `/tmp/play-remote-check-bzi4a0ix`；最终 Vulkan 主渲染集成记录在 `/tmp/play-remote-check-ix0hk9kz`。普通焦点/退出测试记录在 `/tmp/play-normal-focus-2a6jjv2w`。这些是本次机器上的临时目录，不是交付文件依赖。测试脚本会生成新的目录。

## Qt 设置及后台运行补充

- `BUILD_REMOTE_DEBUGGER` 默认 ON；新配置默认启用服务，地址为 `127.0.0.1:28011`。
- Settings 新增带图标的 Remote Debugger 页面，可切换服务并填写 IPv4 / IPv6 地址和端口。`0.0.0.0` 实际监听、端口更换、配置保存和重启验证通过。
- 运行中重新绑定及关闭服务保留当前运行/暂停状态；地址占用时原服务、连接和保存设置保留。
- `--remote-debug` / `--no-remote-debug` 本次启动覆盖通过；关闭未修改的设置窗口不会覆盖启动参数或保存偏好。
- General 新增英文 `Allow Background Running`，与远程服务独立，默认开启。关闭时实际失焦暂停、返回继续；开启时后台推进，手动暂停后返回窗口仍暂停。旧 `ui.pausewhenfocuslost` 保留，新设置保存到 `ui.allowbackgroundrun`。
- 设置验证脚本：`tools/remote_debugger/tests/test_settings.py`；记录 `/tmp/play-remote-settings-26s910sa`。新界面不显示保存、失焦策略等额外说明。
- 最新 Vulkan 主渲染完整集成（协议、CPU、截图、捕获、双后端回放、VU1、GUI Reset、焦点、重连及真实 CLI）通过，记录 `/tmp/play-remote-check-osn26g0y`。该测试使用确定性 ELF，未替代 ISO 的玩法验证。
- 普通焦点测试兼容调试构建的 ELF 初始暂停语义：检测 `--debugger` 支持后显式 Ctrl+R 继续，再验证焦点策略。最终服务开启/关闭构建均通过，记录 `/tmp/play-normal-focus-ksos9ea9` / `/tmp/play-normal-focus-gr6qtjv4`。

## 实时 GS 接口补充

- 新增服务端 `gs.state` / `gs.dump`，握手能力列表、SDK 和明确 CLI 命令同步覆盖。
- 暂停时直接读取 live GS；返回 128 个具名 64 位寄存器、私有/延迟寄存器、传输状态、两个解码上下文及原始输入顶点。`gs.dump` 直接生成 JSON 制品，另含 GPU 同步后的 4 MiB GS RAM（Base64），SDK 下载并释放制品。
- 模拟线程持有 mailbox 等待 GS 线程完成快照；导出前后四类 CPU PC 及 frames 相同，运行时两个方法均明确返回 `TARGET_RUNNING`。不继续、不等待下一帧、不通过帧回放替代实时状态。
- OpenGL 完整集成通过：`/tmp/play-remote-check-9zxcvv6c`。Vulkan 确定性集成通过：`/tmp/play-remote-check-ha46a6ob`。均覆盖实际 SDK 与单次 CLI 查询、导出、多块下载。
- Metal Slug 6 使用 Vulkan 主渲染再次通过实时 GS、截图、捕获、双后端回放、重连和 CLI 流程：`/tmp/play-remote-check-7g48tz30`。`live-gs.json` / `cli-live-gs.json` 均直接来自接口。尚未确认游戏实际关卡可玩性。
- 实际游戏回放暴露了 OpenGL 16 位纹理无法被通用 Bitmap 编码器读取的问题：PSMCT16S 位宽修正；RGB5_A1 纹理在导出前解码，8 位索引图导出为不透明灰度；未知图像位宽返回 `UNSUPPORTED`。实际游戏完整图形流程重测通过。
- 最终启用 / 关闭构建通过，CTest 分别 8/8、6/6。

## MCP CLI adapter

- Added optional official MCP SDK 2.x stdio adapter and `playdebug.py mcp`; the original Python SDK / CLI remains standard-library-only.
- All protocol methods are discoverable tools, alongside persistent connect/disconnect, CLI/RPC delegation, event waits, artifact export, screenshot preview, live GS dump export, and frame dump replay import. Generated files have readable MCP resource links.
- Five MCP tests passed using the official Python client over real stdio, including a legacy `2025-11-25` handshake, structured errors, persistent session reuse, chunked exports, resource release, path validation, concurrent event/control calls, cancellation of active requests and unsent mutations, and EOF cleanup. SDK tests passed 7/7; enabled-build CTest passed 8/8.
- Real Vulkan MCP integration passed: `/tmp/play-mcp-check-uen_8bkw`. Verified full-width registers across four CPUs, memory write/read, background frame advancement, repeated connect without another pause, paused GS state / 4 MiB RAM dump with unchanged PCs/frames, readable JSON / PNG resources, MCP screenshot preview, CLI frame capture, Vulkan replay image export, captured VU1 stepping, explicit reconnect, and normal Qt/service exit. Artifacts and runtime log remain in that isolated test directory.

## uvx package

- Added Python distribution metadata and `play-debugger-mcp` / `playdebug` console entry points. The MCP configuration launches `uvx` from the GitHub feature branch with automatic dependency installation.
- Built the source distribution and a wheel from that distribution. Verified the wheel contains only the two Python modules, license, and package metadata; no emulator sources, ISO, ELF, or captured dumps are shipped.
- All five stdio MCP tests passed through `uvx` using the installed wheel, including cancellation and resource exports. Installed MCP / CLI entry point help checks passed.
- Repeated the real Vulkan workflow through the wheel's `uvx` entry point: `/tmp/play-mcp-check-obm4ijxu`. CPU/memory, persistent control, screenshot preview/resources, live GS RAM dump, CLI frame capture, Vulkan replay, captured VU1 stepping, reconnect, and Qt shutdown passed.

## OOM 原因与回归

15:41:35 内核记录全局 OOM，杀死 Play PID 175177：匿名 RSS 29390048 KiB（约 28 GiB）。此前高频测试程序不断提交 GS 寄存器写入；现有调试构建把完整 VU 元数据复制进每次异步 GS 调用，即使没有帧捕获，也会堆积大对象。

修复后只在捕获期间复制；元数据队列限制 8 MiB，转储限制 64 MiB，超限任务明确失败并释放。相同高频输入及捕获回归返回 `CAPTURE_LIMIT`；观察 RSS 约 446 MiB，仍能查询状态、暂停和重连，没有再次 OOM。后续集成测试使用 MemoryMax=2G / MemorySwapMax=256M 的 systemd scope，并且每次只运行一个模拟器。测试目录 `/tmp/play-remote-check-a5bmw9ro`。退出回归还发现原手柄监听线程 timespec 秒字段未初始化，可使 pselect 长时间阻塞；两个监听线程已统一零初始化，设备监听器先等待线程退出再清理设备列表，普通及远程退出重测通过。

## Metal Slug 6 实际结果

使用用户提供的 `/home/developer/下载/Metal Slug 6.iso`，程序标识 `SLPS_256.74;1`，OpenGL 主渲染。连接后确认暂停，随后后台继续；30 秒采样中帧数持续由约 259 增至 1752，EE 与 IOP PC 持续活动。运行/暂停截图、帧捕获、OpenGL/Vulkan 独立回放、转储导出、超时/取消、重连及正常退出流程通过。大于一个 256 KiB 块的实际游戏转储发现了 Base64 正则资源限制，已改为解码后规范编码校验并重测通过。目录 `/tmp/play-remote-check-bazqwbkv`。

**玩法兼容性未判定为通过。** 初期截图显示游戏加载图案，后期截图为灰屏，尚未确认进入标题菜单或实际关卡。帧推进和远程调试正常不能替代可玩性验证。

## 尚未独立验证 / 后端限制

- 用户手动加载和渲染切换时的所有 GUI 并发组合尚未逐一通过自动化验收；重置和代次失效已验证。
- OpenGL 深度图和 mip>0 是既有后端缺失，服务可调用相关方法并明确报告无图/不支持；未新增底层 OpenGL 实现。
- 回放与本地帧窗口共用 FrameReplaySession 和绘制 kick 逻辑；逐像素的本地窗口导出对照尚未单独完成。
- 主渲染 Vulkan 的确定性 ELF 已通过；该 ISO 使用 Vulkan 主渲染的实际玩法还未验证。

这些限制仍需后续验收，不应把调试流程通过理解为游戏玩法兼容性通过。
