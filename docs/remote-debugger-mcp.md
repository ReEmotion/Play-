# Play! debugger MCP

The CLI supports a stdio MCP server. It exposes every remote debugger RPC, existing CLI commands, events, screenshots, live GS dumps, and isolated frame replay. Discovery does not connect to or pause Play!; call `play_connect` to start a persistent control session.

## Install and launch

Use Python 3.10+ and the [official MCP Python SDK](https://github.com/modelcontextprotocol/python-sdk). The MCP dependency is optional; the existing debugger SDK and CLI still use only the standard library.

```bash
python3 -m venv .venv-mcp
.venv-mcp/bin/python -m pip install -r tools/remote_debugger/requirements-mcp.txt
.venv-mcp/bin/python tools/remote_debugger/playdebug.py --host 127.0.0.1 --port 28011 mcp
```

The equivalent standalone entry point is `tools/remote_debugger/playdebug_mcp.py`. Both use stdin/stdout exclusively for MCP messages and stderr for diagnostic output. Play! must already be running with its Remote Debugger enabled. Use the same SSH tunnel as the ordinary CLI for a remote emulator.

Copy [mcp.example.json](../tools/remote_debugger/mcp.example.json) into your MCP client's server configuration and replace both absolute paths. Change `--host` / `--port` to match the debugger. The server connects only when `play_connect` is called.

## Tools

| Tool | Arguments / behavior |
| --- | --- |
| `play_connect` | Connect and complete the debugger handshake, which pauses the VM. Subsequent calls reuse the connection. |
| `play_disconnect` | Disconnect while preserving the target execution state. |
| `play_<RPC>` | Every method in the [protocol catalog](remote-debugger.md#方法及覆盖目录), with `.` replaced by `_`. Accepts `params` and optional `timeout` in seconds. |
| `play_rpc` | `method`, `params`, optional `timeout`: generic RPC access. |
| `play_cli` | `command`, `arguments` (CLI strings), optional `cpu` and replay `session`. Calls the CLI implementation directly; does not execute an OS shell. |
| `play_wait_event` | Optional event `name` and `timeout`; waiting allows concurrent control calls. |
| `play_gs_dump_save` | Optional plain `filename`; saves the server-generated live GS JSON including 4 MiB GS RAM. Requires pause and does not advance CPU or frames. |
| `play_screenshot_save` | Optional `filename` and `inline_image`; saves PNG and returns an MCP image preview for images up to 4 MiB. |
| `play_artifact_save` | `artifact`, optional `filename`, `mime`, `release` (default true); downloads in bounded chunks. |
| `play_replay_open_file` | Local frame dump `path`, optional `renderer` (default `vulkan`); uploads a dump into isolated replay. |
| `play_export_release` | Generated file `name`; deletes that file and releases its resource/quota. |

For example, a sequence of MCP tool calls:

```text
play_connect {}
play_registers_read {"params":{"cpu":"ee"}}
play_continue {}
play_status {}
play_pause {}
play_gs_state {}
play_gs_dump_save {"filename":"gs-state.json"}
play_screenshot_save {"filename":"paused.png"}
play_cli {"command":"capture","arguments":["frame.dmp.zip","run=true","timeout=10"]}
play_pause {}
play_replay_open_file {"path":"<capture result.path>","renderer":"vulkan"}
play_replay_context {"params":{"session":"<replay result.session>","context":0}}
play_replay_close {"params":{"session":"<replay result.session>"}}
play_disconnect {}
```

`play_cli` screenshot, `gs-dump`, and capture output arguments must be plain filenames. `run=true` explicitly resumes for capture; `play_frame_capture` itself never resumes the VM. To control capture separately, use `play_frame_capture`, `play_continue`, and `play_task_status` / `play_task_cancel`.

## Results, resources, and cancellation

Successful tools return `structuredContent.result`; failures return `isError: true` and `structuredContent.error` with `code`, `message`, and `details`. A JSON text content block carries the same result for clients without structured result support. Protocol hex addresses, full-width register strings, Base64 bytes, pagination, and paused-target requirements are preserved.

`playdebug://guide` documents session behavior. `playdebug://methods` lists the complete RPC catalog. Saved exports return `name`, local `path`, `mime`, `size`, and a readable `playdebug://exports/...` resource URI, plus an MCP resource link. PNG resources contain binary data; JSON resources contain text. Resource references belong to the current MCP process.

Use `--output-dir /path/to/exports` to choose the export directory (default `/tmp/play-debug-artifacts` on Linux). Generated names are unique. At most 16 exports totaling 128 MiB are retained per process; release exports to reclaim quota. Only files created by this process can be read or deleted through its resource tools. Exported files remain on disk when the process exits.

The bridge maintains one debugger controller and serializes operations, with at most 32 pending operations. MCP cancellation cancels unsent local operations and propagates `request.cancel` for active RPCs; capture cancellation also requests `task.cancel`. Started mutations may already have completed: examine error details and query the state before deciding what to do. The bridge does not reconnect or retry mutations automatically. Closing MCP stdin closes the debugger connection and cancels pending work; disconnect does not automatically resume the VM. Remote artifacts and replay sessions expire on disconnect or target-generation changes.

## Verify

```bash
python3 tools/remote_debugger/tests/test_client.py
.venv-mcp/bin/python tools/remote_debugger/tests/test_mcp.py -v
./build-remote/Source/ui_qt/PlayDebugSupport/RemoteDebuggerFixture /tmp/mcp-vu.dmp.zip
.venv-mcp/bin/python tools/remote_debugger/tests/run_mcp_integration.py \
  --binary build-remote/Source/ui_qt/Play --vu-fixture /tmp/mcp-vu.dmp.zip
```

MCP tests cover discovery without target attachment, persistent control, structured errors, chunked exports/resources, CLI delegation, path validation, event/control concurrency, cancellation before sending mutations, stdio cleanup, and a `2025-11-25` MCP client handshake. The Linux integration uses an isolated configuration and a memory-capped Vulkan emulator; it covers live CPU/memory access, screenshots, GS dumps, frame capture/replay, captured VU1 stepping, and reconnection.
