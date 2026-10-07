#include "RemoteDebuggerServer.h"
#include "AppConfig.h"
#include "QStringUtils.h"
#include "MemStream.h"
#include "gs/GSH_OpenGL/GSH_OpenGL.h"
#include <QJsonDocument>
#include <QDateTime>
#include <QFile>
#include <QThread>
#include <cmath>
#include <chrono>
#include <QtEndian>
#include <cstdio>

using namespace Debugger;
static constexpr int MESSAGE_LIMIT = 8 * 1024 * 1024;
static constexpr int QUEUE_LIMIT = 16 * 1024 * 1024;
static constexpr int ARTIFACT_LIMIT = 128 * 1024 * 1024;
static qint64 Now() { return std::chrono::duration_cast<std::chrono::milliseconds>(std::chrono::steady_clock::now().time_since_epoch()).count(); }
static const char* methods[] = {
	"status", "pause", "continue", "step", "registers.read", "registers.write", "memory.read", "memory.write",
	"breakpoints.list", "breakpoints.add", "breakpoints.remove", "disassemble", "effective_address", "callstack", "expression.evaluate",
	"search.word", "search.half", "search.callers", "analysis.run", "analysis.functions", "analysis.vu", "assemble.jal", "elf.header", "elf.programs", "elf.sections", "elf.symbols",
	"modules", "kernel.types", "kernel.objects", "tags.list", "tags.set", "tags.remove", "tags.save", "tags.load", "screenshot", "gs.draw", "gs.state", "gs.dump", "frame.capture", "logs.read",
	"task.status", "task.cancel", "request.cancel", "artifact.read", "artifact.begin", "artifact.append", "artifact.release",
	"replay.open", "replay.close", "replay.export", "replay.packets", "replay.writes", "replay.kicks", "replay.seek", "replay.next_kick", "replay.previous_kick", "replay.context", "replay.options", "replay.image",
	"replay.vu1.registers.read", "replay.vu1.registers.write", "replay.vu1.memory.read", "replay.vu1.memory.write", "replay.vu1.disassemble", "replay.vu1.effective_address", "replay.vu1.step", "replay.vu1.gif", "replay.vu1.analysis.vu"
};
QString RemoteDebuggerServer::Key(const QJsonValue& id) { return QString::fromUtf8(QJsonDocument(QJsonArray{id}).toJson(QJsonDocument::Compact)); }
QJsonObject RemoteDebuggerServer::Error(const QString& c, const QString& m) { return {{"code", c}, {"message", m}}; }
RemoteDebuggerServer::RemoteDebuggerServer(CPS2VM& vm, QObject* parent) : QObject(parent), m_vm(vm), m_backend(vm)
{
	connect(&m_server, &QTcpServer::newConnection, this, [this] { Accept(); });
	connect(&m_timer, &QTimer::timeout, this, [this] { Tick(); }); m_timer.start(50);
	m_frameConnection = vm.OnNewFrame.Connect([this] { ++m_frames; });
	m_programConnection = vm.m_ee->m_os->OnExecutableChange.Connect([this] {
		auto generation = m_vm.DebugGeneration(); auto name = QString::fromUtf8(m_vm.m_ee->m_os->GetExecutableName());
		QMetaObject::invokeMethod(this, [this, generation, name] { m_knownGeneration = generation; m_artifacts.clear(); CancelCapture("STALE_GENERATION", "Program changed."); Event("program_changed", {{"generation", Hex(generation, 16)}, {"program", name}}); }, Qt::QueuedConnection);
	});
	m_stopConnection = vm.OnDebugStopped.Connect([this](std::string reason, std::string cpu, uint32 pc) {
		auto snapshot = m_backend.Snapshot(); snapshot["reason"] = QString::fromStdString(reason); snapshot["cpu"] = QString::fromStdString(cpu); snapshot["stopped_pc"] = Hex(pc);
		QMetaObject::invokeMethod(this, [this, snapshot, reason] { Event("stopped", snapshot); if(reason == "step" && m_active && m_active->method == "step" && (m_active->state == 1 || m_active->state == 2)) Complete(m_active, snapshot); }, Qt::QueuedConnection);
	});
}
RemoteDebuggerServer::~RemoteDebuggerServer()
{
	m_closing = true; m_timer.stop(); m_server.close(); Disconnect();
	// Drain callbacks while alive. Disabling or rebinding preserves execution state.
	m_vm.DebugInvoke([this] {
		m_stopConnection.reset(); m_programConnection.reset(); m_frameConnection.reset();
		if(auto gs = m_vm.GetGSHandler()) gs->SendGSCall([this] { if(m_capture) m_capture->failureConnection.reset(); }, true);
	}, true);
	m_replay.reset();
}
void RemoteDebuggerServer::Listen(const QString& endpoint)
{
	auto split = endpoint.lastIndexOf(':'); if(split <= 0) throw std::runtime_error("Use IP:port or [IPv6]:port.");
	auto host = endpoint.left(split); if(host.startsWith('[') && host.endsWith(']')) host = host.mid(1, host.size() - 2);
	bool ok; auto port = endpoint.mid(split + 1).toUShort(&ok); QHostAddress address(host);
	if(!ok || !port || address.isNull()) throw std::runtime_error("Enter a valid IP address and port 1..65535.");
	if(!m_server.listen(address, port)) throw std::runtime_error(m_server.errorString().toStdString());
	fprintf(stderr, "Remote debugger listening on %s:%u\n", host.toUtf8().constData(), port);
}
void RemoteDebuggerServer::Accept()
{
	while(auto s = m_server.nextPendingConnection())
	{
		if(m_socket) { QJsonObject message{{"type", "event"}, {"event", "connection_rejected"}, {"error", Error("CONTROLLER_BUSY", "One control client is already connected.")}}; auto bytes = QJsonDocument(message).toJson(QJsonDocument::Compact); char length[4]; qToBigEndian<quint32>(bytes.size(), length); s->write(length, 4); s->write(bytes); s->disconnectFromHost(); connect(s, &QTcpSocket::disconnected, s, &QObject::deleteLater); continue; }
		m_socket = s; m_connection = std::make_shared<std::atomic<bool>>(true); m_hello = false; m_input.clear();
		connect(s, &QTcpSocket::readyRead, this, [this] { Read(); });
		connect(s, &QTcpSocket::disconnected, this, [this, s] { if(m_socket == s) Disconnect(); s->deleteLater(); });
		QTimer::singleShot(5000, this, [this, s, alive = m_connection] { if(m_socket == s && *alive && !m_hello) s->abort(); });
	}
}
void RemoteDebuggerServer::Disconnect()
{
	if(m_connection) *m_connection = false;
	for(auto& r : m_requests) r.second->state = 2;
	m_queue.clear(); m_requests.clear(); m_active.reset(); CancelCapture("CANCELED", "Client disconnected."); m_tasks.clear(); m_artifacts.clear(); m_hello = false;
	if(m_socket) { auto s = m_socket; m_socket = nullptr; s->abort(); s->deleteLater(); }
	// Replay cleanup must drain its worker before GUI-owned surface destruction.
	m_replay.reset(); m_replayId.clear();
}
void RemoteDebuggerServer::Send(const QJsonObject& message)
{
	if(!m_socket || m_closing) return;
	auto data = QJsonDocument(message).toJson(QJsonDocument::Compact);
	if(data.size() > MESSAGE_LIMIT || m_socket->bytesToWrite() + data.size() + 4 > QUEUE_LIMIT) { m_socket->abort(); return; }
	char length[4]; qToBigEndian<quint32>(data.size(), length); m_socket->write(length, 4); m_socket->write(data);
}
void RemoteDebuggerServer::Event(const QString& name, const QJsonObject& data) { if(m_hello) Send({{"type", "event"}, {"event", name}, {"data", data}}); }
void RemoteDebuggerServer::Read()
{
	while(m_socket && m_socket->bytesAvailable())
	{
		m_input.append(m_socket->read(std::min<qint64>(m_socket->bytesAvailable(), MESSAGE_LIMIT + 4 - m_input.size())));
		while(m_socket && m_input.size() >= 4)
		{
			auto length = qFromBigEndian<quint32>(m_input.constData());
			if(!length || length > MESSAGE_LIMIT) { m_socket->abort(); return; }
			if(m_input.size() < int(length + 4)) break;
			auto bytes = m_input.mid(4, length); m_input.remove(0, length + 4); QJsonParseError error; auto doc = QJsonDocument::fromJson(bytes, &error);
			if(error.error != QJsonParseError::NoError || !doc.isObject()) { Send({{"type", "response"}, {"id", QJsonValue(QJsonValue::Null)}, {"error", Error("INVALID_JSON", "Expected a JSON object.")}}); continue; }
			Receive(doc.object());
		}
	}
}
void RemoteDebuggerServer::Receive(const QJsonObject& message)
{
	auto id = message["id"]; QString key = Key(id);
	auto replyError = [&](const QString& code, const QString& text) { Send({{"type", "response"}, {"id", id}, {"error", Error(code, text)}}); };
	if(id.isString() && id.toString().size() > 128) { replyError("INVALID_REQUEST", "Request ID exceeds 128 characters."); return; }
	if((!id.isString() && (!id.isDouble() || id.toDouble() < 0 || id.toDouble() > 9007199254740991.0 || floor(id.toDouble()) != id.toDouble())) || !message["method"].isString() || (message.contains("params") && !message["params"].isObject())) { replyError("INVALID_REQUEST", "Expected id, method and object params."); return; }
	if(m_requests.count(key)) { replyError("DUPLICATE_ID", "Request ID is still in use."); return; }
	auto method = message["method"].toString(); auto params = message["params"].toObject();
	if(!m_hello && method != "hello") { replyError("HANDSHAKE_REQUIRED", "Send hello with version 1 first."); return; }
	if(method == "request.cancel")
	{
		auto i = m_requests.find(Key(params["id"])); bool canceled = false; bool found = i != m_requests.end();
		if(i != m_requests.end()) { auto r = i->second; int expected = 0; canceled = r->state.compare_exchange_strong(expected, 2); if(canceled) Complete(r, {}, Error("CANCELED", "Queued request canceled.")); }
		Send({{"type", "response"}, {"id", id}, {"result", QJsonObject{{"canceled", canceled}, {"started", !canceled && found}}}}); return;
	}
	if(m_requests.size() >= 64) { replyError("QUEUE_FULL", "Debugger request queue is full."); return; }
	try
	{
		auto request = std::make_shared<Request>(); request->id = id; request->key = key; request->method = method; request->params = params;
		request->generation = Number(params, "generation", m_vm.DebugGeneration(), UINT64_MAX);
		request->deadline = Now() + Count(params, "timeout_ms", 10000, 120000); request->connection = m_connection;
		m_requests[key] = request; m_queue.push_back(request); Dispatch();
	}
	catch(const Debugger::Error& e) { replyError(e.code, e.what()); }
}
bool RemoteDebuggerServer::Start(const std::shared_ptr<Request>& r)
{
	if(!*r->connection) return false;
	if(Now() >= r->deadline) { Complete(r, {}, Error("TIMEOUT", "Request expired before execution.")); return false; }
	if(r->generation != m_vm.DebugGeneration()) { Complete(r, {}, Error("STALE_GENERATION", "Program or renderer changed.")); return false; }
	int expected = 0; return r->state.compare_exchange_strong(expected, 1);
}
void RemoteDebuggerServer::Complete(std::shared_ptr<Request> r, const QJsonValue& result, const QJsonObject& error)
{
	if(QThread::currentThread() != thread()) { QMetaObject::invokeMethod(this, [this, r, result, error] { Complete(r, result, error); }, Qt::QueuedConnection); return; }
	if(!*r->connection) return;
	if(!m_requests.count(r->key) || m_requests[r->key] != r) { if(m_active == r) { m_active.reset(); Dispatch(); } return; }
	bool stillExecuting = error["code"] == "TIMEOUT" && r->state == 1;
	r->state = 2; QJsonObject response{{"type", "response"}, {"id", r->id}}; if(error.empty()) response["result"] = result; else response["error"] = error; Send(response);
	m_requests.erase(r->key); if(m_active == r && !stillExecuting) m_active.reset(); Dispatch();
}
void RemoteDebuggerServer::Dispatch()
{
	if(m_closing || m_active) return;
	while(!m_queue.empty()) { m_active = m_queue.front(); m_queue.pop_front(); if(m_active->state != 2) break; m_active.reset(); }
	if(!m_active) return; auto r = m_active;
	try
	{
		if(r->method.startsWith("artifact.") || r->method.startsWith("task."))
		{
			if(!Start(r)) return;
			if(r->method.startsWith("task.")) { auto id = Text(r->params, "task"); auto i = m_tasks.find(id); if(i == m_tasks.end()) throw Debugger::Error("STALE_REFERENCE", "Unknown task."); if(r->method == "task.cancel" && i->second == m_capture && i->second->state == "pending") CancelCapture("CANCELED", "Capture canceled."); else if(r->method != "task.status" && r->method != "task.cancel") throw Debugger::Error("METHOD_NOT_FOUND", "Unknown task operation."); Complete(r, QJsonObject{{"task", id}, {"state", i->second->state}, {"artifact", i->second->artifact}, {"error", i->second->error}}); return; }
			if(r->method == "artifact.begin") { auto size = Count(r->params, "size", 0, ARTIFACT_LIMIT); uint64 reserved = size; for(const auto& a : m_artifacts) reserved += std::max<qint64>(a.second.data.size(), a.second.expected); if(reserved > ARTIFACT_LIMIT) throw Debugger::Error("QUEUE_FULL", "Upload reservation exceeds artifact quota."); auto artifact = AddArtifact({}, Text(r->params, "mime", "application/x-play-frame-dump"), r->generation); m_artifacts[artifact["artifact"].toString()].expected = size; Complete(r, artifact); return; }
			auto id = Text(r->params, "artifact"); auto i = m_artifacts.find(id); if(i == m_artifacts.end() || i->second.generation != r->generation) throw Debugger::Error("STALE_REFERENCE", "Unknown or stale artifact.");
			if(r->method == "artifact.release") { m_artifacts.erase(i); Complete(r, QJsonObject{{"ok", true}}); return; }
			if(r->method == "artifact.append") { auto bytes = Decode(r->params["data"]); auto offset = Number(r->params, "offset"); if(offset != uint64(i->second.data.size()) || i->second.expected < 0 || offset + bytes.size() > uint64(i->second.expected)) throw Debugger::Error("INVALID_PARAMS", "Nonsequential artifact upload or size exceeded."); i->second.data.append(bytes); Complete(r, QJsonObject{{"size", i->second.data.size()}}); return; }
			if(r->method != "artifact.read") throw Debugger::Error("METHOD_NOT_FOUND", "Unknown artifact operation.");
			auto offset = Number(r->params, "offset"); auto size = Count(r->params, "size", 262144, 262144); if(offset > uint64(i->second.data.size())) throw Debugger::Error("INVALID_PARAMS", "Artifact offset exceeds size."); auto bytes = i->second.data.mid(offset, size); Complete(r, QJsonObject{{"data", QString::fromLatin1(bytes.toBase64())}, {"size", i->second.data.size()}, {"mime", i->second.mime}, {"eof", offset + bytes.size() == uint64(i->second.data.size())}}); return;
		}
		if(r->method.startsWith("replay.")) { ReplayRequest(r); return; }
		if(r->method == "gs.state" || r->method == "gs.dump") { LiveGsRequest(r); return; }
		if(r->method == "screenshot" || r->method == "gs.draw" || r->method == "frame.capture") { GsRequest(r); return; }
		m_vm.DebugInvoke([this, r] {
			try
			{
				if(!Start(r)) return;
				if(r->method == "hello")
				{
					if(Number(r->params, "version") != 1) throw Debugger::Error("VERSION_MISMATCH", "Only protocol version 1 is supported.");
					m_vm.DebugControl("pause"); auto snapshot = m_backend.Snapshot(); QJsonArray capabilities; for(auto name : methods) capabilities.append(name);
					QJsonObject cpus; for(auto cpu : {"ee", "iop", "vu0", "vu1"}) cpus[cpu] = Backend::RegisterDescriptions(m_backend.Cpu(cpu)); cpus["replay.vu1"] = cpus["vu1"];
					snapshot["version"] = 1; snapshot["capabilities"] = capabilities; snapshot["cpus"] = cpus; snapshot["message_limit"] = MESSAGE_LIMIT; snapshot["block_limit"] = 262144; snapshot["renderer"] = dynamic_cast<CGSH_OpenGL*>(m_vm.GetGSHandler()) ? "opengl" : "vulkan";
					QMetaObject::invokeMethod(this, [this, r, snapshot] { m_hello = true; m_knownGeneration = r->generation; Complete(r, snapshot); }, Qt::QueuedConnection); return;
				}
				if(r->method == "pause" || r->method == "continue" || r->method == "step") { auto cpu = Text(r->params, "cpu", "ee"); m_backend.Cpu(cpu); if(r->method == "step" && m_vm.GetStatus() != CVirtualMachine::PAUSED) throw Debugger::Error("TARGET_RUNNING", "Pause before single stepping."); m_vm.DebugControl(r->method.toStdString(), cpu.toStdString()); if(r->method != "step") Complete(r, m_backend.Snapshot()); return; }
				if(r->method == "logs.read") { auto channel = Text(r->params, "channel", "stdout"); if(channel != "stdout" && channel != "stderr") throw Debugger::Error("INVALID_PARAMS", "Log channel must be stdout or stderr."); QFile file(PathToQString(CAppConfig::GetInstance().GetBasePath() / (channel == "stdout" ? "ps2_stdout.txt" : "ps2_stderr.txt"))); auto offset = Number(r->params, "offset", 0, UINT64_MAX); auto size = Count(r->params, "size", 65536, 262144); QByteArray bytes; if(file.open(QIODevice::ReadOnly) && offset <= uint64(file.size())) { file.seek(offset); bytes = file.read(size); } Complete(r, QJsonObject{{"data", QString::fromLatin1(bytes.toBase64())}, {"next_offset", Hex(offset + bytes.size(), 16)}}); return; }
				auto result = m_backend.Call(r->method, r->params); if(r->method == "status") { auto s = result.toObject(); s["frames"] = Hex(m_frames.load(), 16); result = s; } Complete(r, result);
			}
			catch(const Debugger::Error& e) { Complete(r, {}, Error(e.code, e.what())); }
			catch(const std::exception& e) { Complete(r, {}, Error("TASK_FAILED", e.what())); }
		});
	}
	catch(const Debugger::Error& e) { Complete(r, {}, Error(e.code, e.what())); }
	catch(const std::exception& e) { Complete(r, {}, Error("TASK_FAILED", e.what())); }
}
QJsonObject RemoteDebuggerServer::AddArtifact(QByteArray data, const QString& mime, uint64 generation)
{
	uint64 total = data.size(); for(const auto& a : m_artifacts) total += std::max<qint64>(a.second.data.size(), a.second.expected);
	if(total > ARTIFACT_LIMIT || m_artifacts.size() >= 16) throw Debugger::Error("QUEUE_FULL", "Release artifacts before creating more.");
	auto id = QString("artifact-%1").arg(m_nextId++); int size = data.size(); m_artifacts.emplace(id, Artifact{std::move(data), mime, generation}); Event("artifact_completed", {{"artifact", id}, {"size", size}, {"mime", mime}}); return {{"artifact", id}, {"size", size}, {"mime", mime}};
}
void RemoteDebuggerServer::LiveGsRequest(std::shared_ptr<Request> r)
{
	m_vm.DebugInvoke([this, r] {
		if(!Start(r)) return;
		try
		{
			if(m_vm.GetStatus() != CVirtualMachine::PAUSED) throw Debugger::Error("TARGET_RUNNING", "Pause before reading live GS state.");
			auto gs = m_vm.GetGSHandler();
			if(!gs) throw Debugger::Error("UNSUPPORTED", "No rendering backend.");
			auto snapshot = m_backend.Snapshot(); snapshot["frames"] = Hex(m_frames.load(), 16);
			snapshot["renderer"] = dynamic_cast<CGSH_OpenGL*>(gs) ? "opengl" : "vulkan";
			gs->SubmitWriteBuffer();
			// Hold the VM mailbox until the GS snapshot is copied; GUI continue/reset
			// cannot execute between the paused check and the GPU memory readback.
			gs->SendGSCall([this, r, gs, snapshot] {
				try
				{
					if(!*r->connection || r->state == 2 || Now() >= r->deadline) { Complete(r, {}, Error("TIMEOUT", "GS snapshot expired before execution.")); return; }
					auto result = Backend::GsState(*gs, r->method == "gs.dump"); result["target"] = snapshot;
					if(r->method == "gs.state") { Complete(r, result); return; }
					auto bytes = QJsonDocument(result).toJson(QJsonDocument::Indented);
					QMetaObject::invokeMethod(this, [this, r, bytes] {
						try { if(*r->connection) Complete(r, r->state == 2 ? QJsonValue() : QJsonValue(AddArtifact(bytes, "application/json", r->generation))); }
						catch(const Debugger::Error& e) { Complete(r, {}, Error(e.code, e.what())); }
					}, Qt::QueuedConnection);
				}
				catch(const std::exception& e) { Complete(r, {}, Error("TASK_FAILED", e.what())); }
			}, true);
		}
		catch(const Debugger::Error& e) { Complete(r, {}, Error(e.code, e.what())); }
		catch(const std::exception& e) { Complete(r, {}, Error("TASK_FAILED", e.what())); }
	});
}

void RemoteDebuggerServer::GsRequest(std::shared_ptr<Request> r)
{
	// VM mailbox pins the live GS until its own queue has accepted this operation.
	if(r->method == "frame.capture")
	{
		if(m_capture && m_capture->state == "pending") throw Debugger::Error("CAPTURE_BUSY", "A frame capture is pending.");
		if(m_tasks.size() >= 16) { for(auto i = m_tasks.begin(); i != m_tasks.end();) { if(i->second->state != "pending") i = m_tasks.erase(i); else ++i; } }
		auto task = std::make_shared<Capture>(); task->owner = m_nextId++; task->id = QString("task-%1").arg(task->owner); task->generation = r->generation; task->alive = std::make_shared<std::atomic<bool>>(true); task->deadline = r->deadline; m_capture = task; m_tasks[task->id] = task;
	}
	auto task = r->method == "frame.capture" ? m_capture : nullptr;
	m_vm.DebugInvoke([this, r, task] {
		if(!Start(r)) { if(task) *task->alive = false; return; }
		auto gs = m_vm.GetGSHandler(); if(!gs) { Complete(r, {}, Error("UNSUPPORTED", "No rendering backend.")); return; }
		gs->SendGSCall([this, r, task, gs] {
			try {
				if(!*r->connection || r->state == 2 || r->generation != m_vm.DebugGeneration()) { Complete(r, {}, Error("STALE_GENERATION", "Renderer changed.")); return; }
				if(r->method == "gs.draw") { if(r->params.contains("enabled")) { if(!r->params["enabled"].isBool()) throw Debugger::Error("INVALID_PARAMS", "Expected enabled boolean."); gs->SetDrawEnabled(r->params["enabled"].toBool()); } Complete(r, QJsonObject{{"enabled", gs->GetDrawEnabled()}}); }
				else if(task)
				{
					if(!*task->alive) { Complete(r, {}, Error("CANCELED", "Capture canceled.")); return; }
					task->failureConnection = gs->OnFrameDumpFailure.Connect([this, weak = std::weak_ptr<Capture>(task)](uint64 owner, std::string message) { if(auto task = weak.lock(); task && owner == task->owner) CaptureDone(task, {}, Error("CAPTURE_LIMIT", QString::fromStdString(message))); });
					if(!gs->ArmFrameDump(task->owner, [this, task](const CFrameDump& dump) { if(!*task->alive) return; try { Framework::CMemStream stream; dump.Write(stream); CaptureDone(task, QByteArray(reinterpret_cast<char*>(stream.GetBuffer()), stream.GetSize())); } catch(const std::exception& e) { CaptureDone(task, {}, Error("TASK_FAILED", e.what())); } })) { CaptureDone(task, {}, Error("CAPTURE_BUSY", "Local frame capture is active.")); throw Debugger::Error("CAPTURE_BUSY", "Local frame capture is active."); }
					Complete(r, QJsonObject{{"task", task->id}, {"state", "pending"}});
				}
				else { auto bytes = FrameReplaySession::Png(gs->GetScreenshot()); QMetaObject::invokeMethod(this, [this, r, bytes] { try { if(*r->connection) Complete(r, r->state == 2 ? QJsonValue() : QJsonValue(AddArtifact(bytes, "image/png", r->generation))); } catch(const Debugger::Error& e) { Complete(r, {}, Error(e.code, e.what())); } }, Qt::QueuedConnection); }
			} catch(const Debugger::Error& e) { Complete(r, {}, Error(e.code, e.what())); }
			catch(const std::exception& e) { Complete(r, {}, Error("UNSUPPORTED", e.what())); }
		});
	});
}
void RemoteDebuggerServer::CancelCapture(const QString& code, const QString& message)
{
	auto task = m_capture; if(!task || task->state != "pending") return;
	*task->alive = false; task->state = "failed"; task->error = Error(code, message);
	m_vm.DebugInvoke([this, owner = task->owner] { if(auto gs = m_vm.GetGSHandler()) gs->SendGSCall([gs, owner] { gs->CancelFrameDump(owner); }); });
	Event("task_failed", {{"task", task->id}, {"error", task->error}});
}
void RemoteDebuggerServer::CaptureDone(std::shared_ptr<Capture> task, QByteArray data, QJsonObject error)
{
	QMetaObject::invokeMethod(this, [this, task, data, error] {
		if(!*task->alive) return;
		try { if(task->generation != m_vm.DebugGeneration()) throw Debugger::Error("STALE_GENERATION", "Program changed during capture."); if(!error.empty()) { task->error = error; task->state = "failed"; Event("task_failed", {{"task", task->id}, {"error", error}}); } else { auto a = AddArtifact(data, "application/x-play-frame-dump", task->generation); task->artifact = a["artifact"].toString(); task->state = "complete"; Event("task_completed", {{"task", task->id}, {"artifact", a}}); } *task->alive = false; }
		catch(const Debugger::Error& e) { CancelCapture(e.code, e.what()); }
	}, Qt::QueuedConnection);
}
void RemoteDebuggerServer::ReplayRequest(std::shared_ptr<Request> r)
{
	if(!Start(r)) return;
	if(r->method == "replay.close") { m_replay.reset(); m_replayId.clear(); Complete(r, QJsonObject{{"ok", true}}); return; }
	QByteArray input;
	if(r->method == "replay.open")
	{
		auto id = Text(r->params, "artifact"); auto i = m_artifacts.find(id); if(i == m_artifacts.end() || i->second.generation != r->generation || (i->second.expected >= 0 && i->second.expected != i->second.data.size())) throw Debugger::Error("STALE_REFERENCE", "Unknown, stale or incomplete dump artifact."); input = i->second.data;
		auto backend = Text(r->params, "renderer", "opengl"); if(backend != "opengl" && backend != "vulkan") throw Debugger::Error("INVALID_PARAMS", "Renderer must be opengl or vulkan.");
		m_replay.reset(); m_replay = std::make_unique<FrameReplaySession>(); m_replay->Create(backend == "vulkan"); m_replayId = QString("replay-%1").arg(m_nextId++); m_replayGeneration = r->generation;
	}
	else if(!m_replay || m_replayGeneration != r->generation || Text(r->params, "session") != m_replayId) throw Debugger::Error("STALE_REFERENCE", "Unknown replay session.");
	auto session = m_replay.get(); auto sessionId = m_replayId;
	session->Run([this, session, sessionId, r, input] {
		try { if(!*r->connection || r->state == 2) { Complete(r, {}); return; } QJsonValue result;
			if(r->method == "replay.open") { session->Load(input); session->Replay(0); result = QJsonObject{{"session", sessionId}, {"packets", int(session->dump.GetPackets().size())}}; }
			else if(r->method == "replay.image" || r->method == "replay.export") { auto data = r->method == "replay.image" ? FrameReplaySession::Png(session->Image(r->params)) : session->Export(); auto mime = r->method == "replay.image" ? "image/png" : "application/x-play-frame-dump"; QMetaObject::invokeMethod(this, [this, r, data, mime] { try { if(*r->connection) Complete(r, r->state == 2 ? QJsonValue() : QJsonValue(AddArtifact(data, mime, r->generation))); } catch(const Debugger::Error& e) { Complete(r, {}, Error(e.code, e.what())); } }, Qt::QueuedConnection); return; }
			else result = session->Query(r->method, r->params); Complete(r, result);
		} catch(const Debugger::Error& e) { Complete(r, {}, Error(e.code, e.what())); } catch(const std::exception& e) { Complete(r, {}, Error("TASK_FAILED", e.what())); }
	});
}
void RemoteDebuggerServer::Tick()
{
	if(m_capture && m_capture->state == "pending" && Now() >= m_capture->deadline) CancelCapture("TIMEOUT", "No completed frame before capture deadline; target was not resumed.");
	if(m_active && m_active->state != 2 && Now() >= m_active->deadline) Complete(m_active, {}, Error("TIMEOUT", m_active->state == 1 ? "Operation has started; completion is unknown. Do not retry a mutation." : "Request expired before execution."));
	if(m_hello && m_knownGeneration != m_vm.DebugGeneration()) { m_knownGeneration = m_vm.DebugGeneration(); m_artifacts.clear(); CancelCapture("STALE_GENERATION", "Target changed."); Event("program_changed", {{"generation", Hex(m_knownGeneration, 16)}}); }
}
