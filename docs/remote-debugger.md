# Play! 远程调试器（协议 v1）

Linux Qt 首版；默认监听 `127.0.0.1:28011`。远程客户端操作通过 UI 或 `--elf` / `--disc` 已加载的目标，不提供程序上传、远程加载、GDB、DWARF、条件断点或监视点。转储上传仅用于独立帧回放。

## 构建和启动

```bash
cmake -S . -B build-remote -DBUILD_REMOTE_DEBUGGER=ON \
  -DCMAKE_BUILD_TYPE=Release -DCMAKE_C_FLAGS='-DSTDC -DSTDC99'
cmake --build build-remote -j 2
QT_QPA_PLATFORM=xcb ./build-remote/Source/ui_qt/Play \
  --remote-debug 127.0.0.1:28011 --disc '/home/developer/下载/Metal Slug 6.iso'
```

本机 GCC 构建现有 zstd zlibWrapper 需要上述 `STDC` 定义。`BUILD_REMOTE_DEBUGGER` 默认 ON，开启会启用 `DEBUGGER_INCLUDED`；显式设为 OFF 可关闭编译，关闭构建不依赖 Qt Network。已有构建目录保留 CMake 缓存，需要改为 ON。支持 IPv4 / IPv6，包括 `0.0.0.0:28011` 和 `[::1]:28011`。Linux Vulkan 使用 `QT_QPA_PLATFORM=xcb`。

在 **Options → Settings → Remote Debugger**（快捷键 `Ctrl+,`）中设置服务开关、监听地址和端口。监听地址可填写 `127.0.0.1`、`0.0.0.0` 或 IPv6 地址。新配置默认开启服务。修改地址失败会保留原服务及设置。

`--remote-debug address:port` 覆盖本次启动的设置，`--no-remote-debug` 本次禁用；参数不会覆盖保存的偏好。两者不可同时使用。默认启动若绑定失败会显示状态栏错误，模拟器继续启动。

也可通过 SSH 隧道跨机器连接：

```bash
ssh -N -L 28011:127.0.0.1:28011 user@emulator-host
python3 tools/remote_debugger/playdebug.py --host 127.0.0.1 --port 28011 shell
```

**Options → Settings → General → Allow Background Running**（也可在 Virtual Machine 菜单切换）独立控制失焦策略，默认开启，配置键为 `ui.allowbackgroundrun`。开启时焦点变化不会暂停或恢复 VM；关闭时失焦暂停，返回窗口仅恢复由失焦造成的暂停。与远程服务开关互不联动，原 `ui.pausewhenfocuslost` 值保留。

## SDK / CLI

CLI 同时支持 stdio MCP 服务：安装可选依赖后运行 `playdebug.py mcp`。配置、工具和验证见 [MCP 文档](remote-debugger-mcp.md)。

Python 3.9+，仅使用标准库。`tools/remote_debugger/playdebug.py` 可直接运行或导入。

```python
import sys
sys.path.insert(0, 'tools/remote_debugger')
from playdebug import Client, DebugError

with Client('127.0.0.1', 28011, timeout=10) as client:
    # hello 完成时整个 VM 已暂停。
    print(client.registers('ee')['gpr.T0'])
    print(client.disassemble(cpu='ee', address='0x00100000', count=8))
    client.breakpoints_add(cpu='ee', address='0x00100000')
    client.resume()
    stop = client.wait_event('stopped')
    print(stop['data'])
    client.screenshot('paused.png')  # 有已有画面即可，无须继续运行
    client.breakpoints_remove(cpu='ee', address='0x00100000')
    client.resume()
    client.capture('next-frame.dmp.zip', timeout=10)
```

所有目录中的方法均有同名 SDK 方法，`.` 替换成 `_`，使用协议关键字参数。`continue` 使用 `resume()`。`memory_read(address, size, cpu, space)` 返回 bytes；`memory_write` 接收 bytes；`download`、`upload_dump`、`wait_task`、`wait_event` 提供制品、任务和事件辅助。`call(method, params, **kwargs)` 是通用 RPC。读寄存器返回完整十六进制字符串，避免丢失高位。

```bash
python3 tools/remote_debugger/playdebug.py --json status
python3 tools/remote_debugger/playdebug.py --cpu ee registers-read
python3 tools/remote_debugger/playdebug.py disassemble address=0x00100000 count=16
python3 tools/remote_debugger/playdebug.py screenshot paused.png
# 新建连接会暂停；只有显式 run=true 才先继续进行帧捕获。
python3 tools/remote_debugger/playdebug.py capture next.dmp.zip run=true timeout=10
python3 tools/remote_debugger/playdebug.py rpc expression.evaluate '{"cpu":"ee","expression":"T0 + 4"}'
```

单次命令每次建立新控制会话，因此握手会暂停 VM。会话制品、任务和回放引用不能跨单次 CLI 调用使用。交互模式维持连接：

```text
ee> cpu vu1
vu1> regs
vu1> step
vu1> cpu ee
ee> break 0x00100000
ee> continue
ee> events
ee> delete 0x00100000
ee> screenshot paused.png
ee> continue
ee> capture frame.dmp.zip
ee> replay-open frame.dmp.zip renderer=vulkan
ee> replay-packets limit=20
ee> replay-seek command=100
ee> replay-image context=0 kind=framebuffer
```

CLI 每个 RPC 的明确命令是方法名把 `.` 和 `_` 换为 `-`，例如 `replay-next-kick`；`key=value` 参数支持字符串、十六进制、JSON 数字、布尔和数组。`--help` 列出全部命令。回放引用在 shell 中自动附加；原始 RPC 和 SDK 需传 `session`。制品图像通过 `artifact-read` 或 SDK `download` 获取。

## 当前实时 GS 状态

```bash
python3 tools/remote_debugger/playdebug.py --json gs-state
python3 tools/remote_debugger/playdebug.py gs-dump live-gs.json
```

交互 CLI 使用 `gs-state` / `gs-dump live-gs.json`。SDK 使用 `client.gs_state()` 和 `client.gs_dump('live-gs.json')`；后者下载并释放服务端制品，`client.call('gs.dump')` 可获取原始制品引用。两者要求暂停，运行中返回 `TARGET_RUNNING`；不会隐式继续或产生帧捕获任务。与 `frame.capture` / `replay.context` 分开：查询的是当前模拟器，回放接口查询独立会话。

JSON 格式 `play-gs-state-v1`：`target` 含程序、代次、PC、帧数和渲染器；`registers` 是 128 个 `{index, name, value}`，值为完整 64 位十六进制字符串；`private_registers`、`held_register_low_words`、`transfer`、`contexts`、`vertices`、`input_state` 保留原始值及现有本地解码说明。导出另含 `ram={size:4194304, encoding:"base64", data:...}`。文件由服务端直接生成，无需客户端解析帧转储。

VM 线程确认暂停并提交已有 GS 写入，持有 VM mailbox 等待 GS 线程完成快照；GS 线程同步 GPU 内存后复制数据。GUI 不执行网络回调中的同步等待；显式继续、重置不能在暂停确认与复制之间推进模拟器。

## 协议

每条消息是 4 字节无符号大端长度，后接 UTF-8 JSON。最多 8 MiB，二进制块最多 256 KiB。对象结构：

```json
{"id":1,"method":"hello","params":{"version":1}}
{"type":"response","id":1,"result":{"version":1,"state":"paused","generation":"0x0000000000000003"}}
{"type":"response","id":2,"error":{"code":"TARGET_RUNNING","message":"Pause the target before this operation."}}
{"type":"event","event":"stopped","data":{"reason":"breakpoint","cpu":"ee","stopped_pc":"0x00100000"}}
```

请求 ID 是非负安全整数或最多 128 字符的字符串，同一连接未完成请求不可重用。握手还返回完整能力列表、CPU 注册表、当前程序、PC、渲染后端和尺寸上限。最多一个客户端；第二客户端收到 `CONTROLLER_BUSY` 事件并断开。握手超时 5 秒。断开保持当前运行状态，重连握手暂停后接管。

公共参数：`cpu`（默认 `ee`）、`generation`（状态代次）、`timeout_ms`（默认 10000，最大 120000）。运行状态及地址由模拟线程生成。程序加载、重置和 GS 切换改变代次；过期请求返回 `STALE_GENERATION`，过期制品/回放引用返回 `STALE_REFERENCE`。SDK 在握手和状态查询后自动附加代次；原始客户端应自行附加。

地址、PC、寄存器和 64 位值采用 `0x` 字符串。寄存器名见 `hello.cpus`，GPR 使用现有名称，例如 `gpr.T0`、`gpr.SP`；浮点寄存器返回原始位模式。128 位值高位在左，内存 bytes 使用 PS2 小端序。通用数字参数也接受 JSON 安全整数。CPU 标识是 `ee`、`iop`、`vu0`、`vu1`，回放 CPU 为 `replay.vu1`，通过 `replay.vu1.*` 方法访问。

`registers.read` 覆盖 GPR、HI/LO、COP0、FPU、VU VF/VI/ACC、流水线和 TLB 字段。`hello` 标注每项位宽和可写性；常量寄存器、流水线内部状态和 TLB 原始字段只读。PC 检查 CPU 对齐并清除延迟跳转。写内存先验证整段再修改，默认拒绝函数映射/MMIO和未映射地址；指令修改、断点及寄存器写入失效 JIT 缓存。

寄存器、内存及分析/元数据操作要求暂停，运行时返回 `TARGET_RUNNING`。断点可在运行时修改。单步采用现有 VM 执行配额语义，以实际停止回调完成；JIT 可能执行一个基本块，未活动的 VU/IOP 可能不推进 PC。单步不冻结其他处理器。

## 方法及覆盖目录

列表统一支持 `offset`、`limit`（默认 256，最大 4096），返回 `items`、`total`、`next`。搜索每段最多 256 KiB，客户端分段扫描完整地址范围；地址空间检查避免 MMIO副作用。

| 本地能力 | 方法 | 特定参数 / 返回 |
|---|---|---|
| 执行控制 | `status`, `pause`, `continue`, `step` | 状态、代次、PC；status 含累计 frames；step 指定 cpu |
| 断点 | `breakpoints.add`, `.remove`, `.list` | address，添加/删除幂等 |
| 全寄存器 | `registers.read`, `.write` | 写：name、value；读：name → hex |
| 数据/指令内存 | `memory.read`, `.write` | address、space=data/instruction；读 size，写 Base64 data |
| 反汇编 / 有效地址 | `disassemble`, `effective_address` | address、count；VU 的上下指令分别按 4 字节返回 |
| 调用栈 / 表达式 | `callstack`, `expression.evaluate` | expression；沿用本地从左到右计算、GPR 名称和运算符 |
| 调用者 / 值搜索 | `search.callers`, `.word`, `.half` | begin、end（不含）、value；返回 next_address |
| EE 分析 / 常用函数 | `analysis.run`, `.functions` | begin、end；functions 默认 ELF 范围，使用 ee_functions.xml |
| VU 微程序分析 | `analysis.vu` | cpu=vu0/vu1、begin、end（不含），8 字节对齐 |
| JAL 修改 | `assemble.jal` | cpu、address、target，检查区域及对齐 |
| ELF | `elf.header`, `.programs`, `.sections`, `.symbols` | 现有 ELF 的完整描述字段，分页 |
| 模块 / 内核对象 | `modules`, `kernel.types`, `.objects` | cpu=ee/iop；objects 传 types 中的 type ID |
| 函数 / 变量 / 注释标签 | `tags.list`, `.set`, `.remove`, `.save`, `.load` | kind=functions/variables/comments、address、text；保存加载当前程序标签包 |
| 实时 GS 状态 | `gs.state` | 暂停时查询 128 个具名寄存器、私有/延迟寄存器、两个上下文、传输状态及输入顶点；不推进 PC / 帧 |
| 实时 GS 导出 | `gs.dump` | 服务端生成 JSON 制品，包含 `gs.state` 数据及 GPU 同步后的 4 MiB GS RAM（Base64）；无需等待下一帧 |
| 运行画面 | `screenshot`, `gs.draw` | PNG 制品；draw 读或传 enabled 布尔 |
| 下一帧捕获 | `frame.capture` | 返回 task；不隐式继续，含超时和取消 |
| 标准输出 / 错误 | `logs.read` | channel=stdout/stderr、offset、size，Base64 data、next_offset |
| 任务 | `task.status`, `.cancel`, `request.cancel` | task ID；取消尚未执行请求传 id |
| 制品 | `artifact.begin`, `.append`, `.read`, `.release` | size/mime，artifact、offset、Base64 data；顺序上传，显式释放 |
| 独立转储回放 | `replay.open`, `.close`, `.export` | artifact、renderer=opengl/vulkan；返回 session |
| 数据包 / 寄存器写入 | `replay.packets`, `.writes` | session；writes 传 packet，含反汇编描述 |
| 命令 / drawing kick | `replay.seek`, `.kicks`, `.next_kick`, `.previous_kick` | session、command，顶点及上下文数据 |
| GS 状态 / 输入顶点 | `replay.context` | session、context=0/1，完整 GS 寄存器及现有状态/顶点描述 |
| 回放显示及测试开关 | `replay.options` | display=raw/448p/448i、alpha/depth/blend 布尔 |
| 图像 / mip | `replay.image` | kind=framebuffer/depthbuffer/texture、context、mip=0..6、channel=raw/clut/alpha、highlight（默认 true） |
| 捕获 VU1 | `replay.vu1.registers.read`, `.registers.write`, `.memory.read`, `.memory.write`, `.disassemble`, `.effective_address`, `.analysis.vu`, `.step`, `.gif` | session，相关 CPU 参数；先定位 GIF path 1 数据包 |

每行都通过同一方法进入 SDK 和 CLI；纯布局、平移、缩放由客户端处理。图像能力沿用后端支持范围：OpenGL 原有深度图及 mip>0 导出未实现，会明确返回无图/不支持；Vulkan 支持这些数据，但未使用的纹理/mip同样没有图像。服务不会伪造制品。

## 线程、资源和失败处理

网络读写在 GUI 线程异步处理；每连接最多 64 个未完成请求，发送队列最多 16 MiB，慢客户端被断开。CPU 操作进入 VM mailbox，GS 操作进入 GS mailbox。独立回放解析和重放使用自己的工作线程、GS 和 VU1，不访问实时游戏；Qt surface 创建/销毁仍在 GUI 线程。本地 JAL、分析和函数识别复用后端，本地 CPU 视图和后端访问用 CPU 锁串行保护；寄存器视图更新无法取得锁时跳过，避免长分析卡住界面。

制品总额度 128 MiB，最多 16 个；上传预留额度。捕获最多 8 MiB 待处理 VU 元数据、64 MiB 未压缩转储、65536 个数据包。没有捕获时不复制 VU 元数据。过快生产、无帧边界或过大捕获返回 `CAPTURE_LIMIT` 并释放；ZIP 导入检查未压缩上限和数据尺寸。所有这些上限独立于模拟器其他图形缓存。

超时会取消尚未执行操作；已开始操作的结果可能未知，SDK **不自动重试修改**。已开始任务尚未回调时不会释放其串行执行位置，以免叠加重放操作。断连取消未执行请求和该连接的捕获任务。服务退出先取消、排空线程回调，再释放会话及 Qt 对象。

常见错误：`HANDSHAKE_REQUIRED`、`VERSION_MISMATCH`、`CONTROLLER_BUSY`、`INVALID_JSON`、`INVALID_REQUEST`、`INVALID_PARAMS`、`INVALID_CPU`、`METHOD_NOT_FOUND`、`DUPLICATE_ID`、`QUEUE_FULL`、`TARGET_RUNNING`、`READ_ONLY_REGISTER`、`UNSAFE_MEMORY`、`STALE_GENERATION`、`STALE_REFERENCE`、`NO_PROGRAM`、`NO_IMAGE`、`NO_REPLAY`、`NO_VU_CAPTURE`、`END_OF_LIST`、`CAPTURE_BUSY`、`CAPTURE_LIMIT`、`TIMEOUT`、`CANCELED`、`UNSUPPORTED`、`TASK_FAILED`。

事件：`stopped`（reason=step/pause/breakpoint，cpu、PC和快照）、`program_changed`、`artifact_completed`、`task_completed`、`task_failed`。第二客户端收到 `connection_rejected`。SDK 的事件队列最多 1024 条；消费不及时会明确报 `EVENT_OVERFLOW`。

## 验证

```bash
ctest --test-dir build-remote --output-on-failure
python3 tools/remote_debugger/tests/make_test_elf.py /tmp/test.elf
./build-remote/Source/ui_qt/PlayDebugSupport/RemoteDebuggerFixture /tmp/vu.dmp.zip
python3 tools/remote_debugger/tests/run_integration.py \
  --binary build-remote/Source/ui_qt/Play --vu-fixture /tmp/vu.dmp.zip
python3 tools/remote_debugger/tests/run_integration.py \
  --binary build-remote/Source/ui_qt/Play --renderer vulkan --vu-fixture /tmp/vu.dmp.zip
python3 tools/remote_debugger/tests/run_integration.py \
  --binary build-remote/Source/ui_qt/Play --oom-regression
python3 tools/remote_debugger/tests/run_integration.py \
  --binary build-remote/Source/ui_qt/Play --disc '/path/Metal Slug 6.iso'
```

设置及后台运行测试：`python3 tools/remote_debugger/tests/test_settings.py --binary build-remote/Source/ui_qt/Play`。

普通模式焦点测试另运行 `python3 tools/remote_debugger/tests/test_normal_focus.py --binary build/Source/ui_qt/Play`，只读自身测试进程的 RAM 计数器。

Linux 集成测试依赖当前图形会话和 systemd 用户会话，通过 scope 限制测试进程 MemoryMax=2G、MemorySwapMax=256M，使用独立临时配置并在结束时关闭窗口/进程。只同时运行一个模拟器；ISO 不复制、不提交。无需联网或第三方 Python 包。

实际验收与兼容性限制见 [验证记录](remote-debugger-validation.md)。
