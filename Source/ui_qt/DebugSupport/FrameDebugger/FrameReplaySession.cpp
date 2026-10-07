#include "FrameReplaySession.h"
#include "DebuggerBackend.h"
#include "GsStateUtils.h"
#include "GsContextView.h"
#include "gs/GsDebuggerInterface.h"
#include "../../GSH_OpenGLQt.h"
#include "../../openglwindow.h"
#include "MemStream.h"
#include "PtrStream.h"
#include <QOffscreenSurface>
#include <QImage>
#include <QBuffer>
#if HAS_GSH_VULKAN
#include "gs/GSH_Vulkan/GSH_VulkanOffscreen.h"
#endif
using namespace Debugger;
FrameReplaySession::FrameReplaySession()
{
	m_worker = std::thread([this] { while(!m_end) { m_mailbox.WaitForCall(); while(m_mailbox.IsPending()) m_mailbox.ReceiveCall(); } });
}
FrameReplaySession::~FrameReplaySession()
{
	m_mailbox.SendCall([this] { m_end = true; }, true);
	m_worker.join();
	Release();
}
void FrameReplaySession::Run(std::function<void()> op) { m_mailbox.SendCall(std::move(op)); }
void FrameReplaySession::Create(bool vulkan)
{
	Release();
	if(vulkan)
	{
#if HAS_GSH_VULKAN
		gs = std::make_unique<CGSH_VulkanOffscreen>();
#else
		throw Error("UNSUPPORTED", "Vulkan was not built.");
#endif
	}
	else
	{
		m_surface = new QOffscreenSurface;
		m_surface->setFormat(OpenGLWindow::GetSurfaceFormat()); m_surface->create();
		gs = std::make_unique<CGSH_OpenGLQt>(m_surface);
	}
	gs->SetLoggingEnabled(false); gs->Initialize(); gs->Reset();
}
void FrameReplaySession::Release()
{
	if(gs) { gs->Release(); gs.reset(); }
	if(m_surface) { m_surface->destroy(); delete m_surface; m_surface = nullptr; }
}
void FrameReplaySession::Load(const QByteArray& data)
{
	loaded = false;
	Framework::CPtrStream stream(const_cast<char*>(data.data()), data.size());
	dump.Reset(); dump.Read(stream); dump.IdentifyDrawingKicks(); vu.Reset(); loaded = true; command = -1;
}
QByteArray FrameReplaySession::Export()
{
	if(!loaded) throw Error("NO_REPLAY", "No frame dump loaded.");
	Framework::CMemStream stream; dump.Write(stream); return QByteArray(reinterpret_cast<char*>(stream.GetBuffer()), stream.GetSize());
}
void FrameReplaySession::RestoreVu()
{
	vu.Reset();
	memcpy(vu.GetMicroMem1(), metadata.microMem1, PS2::MICROMEM1SIZE);
	memcpy(vu.GetVuMem1(), metadata.vuMem1, PS2::VUMEM1SIZE);
	vu.GetVu1Context()->m_State = metadata.vu1State;
	vu.SetVpu1Top(metadata.vpu1Top); vu.SetVpu1Itop(metadata.vpu1Itop);
}
void FrameReplaySession::Replay(int32 target)
{
	if(!gs || !loaded) throw Error("NO_REPLAY", "No frame dump or renderer.");
	if(target < -1) throw Error("INVALID_PARAMS", "Invalid command index.");
	gs->Reset(); gs->InitFromFrameDump(&dump);
	int32 index = 0; metadata = CGsPacketMetadata();
	for(const auto& packet : dump.GetPackets())
	{
		if(index - 1 >= target) break;
		metadata = packet.metadata;
		if(packet.registerWrites.empty()) { gs->ProcessWriteBuffer(nullptr); gs->FeedImageData(packet.imageData.data(), packet.imageData.size()); }
		else for(const auto& write : packet.registerWrites) { if(index - 1 >= target) break; gs->WriteRegister(write); index++; }
	}
	gs->ProcessWriteBuffer(nullptr); gs->Finish(true);
	kick = DRAWINGKICK_INFO();
	auto& kicks = dump.GetDrawingKicks();
	if(target >= 0) { auto i = kicks.upper_bound(target); if(i != kicks.begin()) kick = std::prev(i)->second; }
	command = target; RestoreVu();
}
QJsonValue FrameReplaySession::Query(const QString& method, const QJsonObject& p)
{
	if(!loaded) throw Error("NO_REPLAY", "No frame dump loaded.");
	if(method == "replay.packets")
	{
		QJsonArray rows; int index = 0, command = 0;
		for(const auto& packet : dump.GetPackets()) { rows.append(QJsonObject{{"index", index++}, {"command", command}, {"path", int(packet.metadata.pathIndex)}, {"writes", int(packet.registerWrites.size())}, {"image_size", int(packet.imageData.size())}}); command += packet.registerWrites.size(); }
		return Page(rows, p);
	}
	if(method == "replay.writes")
	{
		auto i = Number(p, "packet"); if(i >= dump.GetPackets().size()) throw Error("INVALID_PARAMS", "Invalid packet index.");
		QJsonArray rows; for(const auto& w : dump.GetPackets()[i].registerWrites) rows.append(QJsonObject{{"register", int(w.first)}, {"value", Hex(w.second, 16)}, {"description", QString::fromStdString(CGSHandler::DisassembleWrite(w.first, w.second))}});
		return Page(rows, p);
	}
	if(method == "replay.kicks")
	{
		QJsonArray rows; for(const auto& k : dump.GetDrawingKicks()) { QJsonArray vertices; for(const auto& v : k.second.vertex) vertices.append(QJsonObject{{"x", v.x}, {"y", v.y}}); rows.append(QJsonObject{{"command", int(k.first)}, {"context", int(k.second.context)}, {"primitive", int(k.second.primType)}, {"vertices", vertices}}); }
		return Page(rows, p);
	}
	if(method == "replay.seek" || method == "replay.next_kick" || method == "replay.previous_kick")
	{
		int target = Count(p, "command", 0, INT32_MAX);
		if(method != "replay.seek") { auto& kicks = dump.GetDrawingKicks(); auto i = method == "replay.next_kick" ? kicks.upper_bound(command < 0 ? 0 : command) : kicks.lower_bound(command < 0 ? 0 : command); if(method.endsWith("previous_kick")) { if(i == kicks.begin()) throw Error("END_OF_LIST", "No previous drawing kick."); --i; } else if(i == kicks.end()) throw Error("END_OF_LIST", "No next drawing kick."); target = i->first; }
		uint64 total = 0; for(auto& packet : dump.GetPackets()) total += packet.registerWrites.size(); if(total == 0 || uint64(target) >= total) throw Error("INVALID_PARAMS", "Command index out of range.");
		Replay(target); return QJsonObject{{"command", command}, {"vu1_available", metadata.pathIndex == 1}};
	}
	if(method == "replay.options")
	{
		auto mode = Text(p, "display", displayMode); if(mode != "raw" && mode != "448p" && mode != "448i") throw Error("INVALID_PARAMS", "Display must be raw, 448p or 448i.");
		displayMode = mode;
		auto iface = dynamic_cast<CGsDebuggerInterface*>(gs.get()); if(!iface) throw Error("UNSUPPORTED", "Renderer has no debugger interface.");
		// Renderer settings and reads always run on its own GS thread.
		for(const char* key : {"alpha", "depth", "blend"}) if(p.contains(key) && !p[key].isBool()) throw Error("INVALID_PARAMS", "Expected a boolean option.");
		gs->SendGSCall([&] { if(p.contains("alpha")) iface->SetAlphaTestingEnabled(p["alpha"].toBool()); if(p.contains("depth")) iface->SetDepthTestingEnabled(p["depth"].toBool()); if(p.contains("blend")) iface->SetAlphaBlendingEnabled(p["blend"].toBool()); }, true);
		Replay(command); return QJsonObject{{"display", displayMode}};
	}
	if(method == "replay.context")
	{
		auto context = Count(p, "context", 0, 1); QJsonObject result;
		gs->SendGSCall([&] { QJsonArray regs; for(int i = 0; i < CGSHandler::REGISTER_MAX; i++) regs.append(Hex(gs->GetRegisters()[i], 16)); result = {{"command", command}, {"context", QString::fromStdString(CGsStateUtils::GetContextState(gs.get(), context))}, {"input_vertices", QString::fromStdString(CGsStateUtils::GetInputState(gs.get()))}, {"registers", regs}}; }, true);
		return result;
	}
	if(method.startsWith("replay.vu1."))
	{
		if(metadata.pathIndex != 1) throw Error("NO_VU_CAPTURE", "Select a GIF path 1 packet first.");
		auto op = method.mid(QString("replay.vu1.").size());
		if(op == "step") { vu.StepVu1(); return QJsonObject{{"pc", Hex(vu.GetVu1Context()->m_State.nPC)}}; }
		if(op == "gif") { auto a = metadata.vuMemPacketAddress; if(a >= PS2::VUMEM1SIZE) throw Error("INVALID_PARAMS", "Captured GIF address is invalid."); return QJsonObject{{"address", Hex(a)}, {"data", QString::fromLatin1(QByteArray(reinterpret_cast<char*>(vu.GetVuMem1() + a), PS2::VUMEM1SIZE - a).toBase64())}}; }
		return Backend::CpuCall(*vu.GetVu1Context(), op, p);
	}
	throw Error("METHOD_NOT_FOUND", "Unknown replay method.");
}
Framework::CBitmap FrameReplaySession::Image(const QJsonObject& p)
{
	if(!loaded) throw Error("NO_REPLAY", "No frame dump loaded.");
	int context = Count(p, "context", 0, 1), mip = Count(p, "mip", 0, 6); auto kind = Text(p, "kind", "framebuffer"); auto channel = Text(p, "channel", "raw");
	auto iface = dynamic_cast<CGsDebuggerInterface*>(gs.get()); if(!iface) throw Error("UNSUPPORTED", "Renderer has no debugger image interface.");
	Framework::CBitmap bitmap; std::exception_ptr error;
	gs->SendGSCall([&] {
		try {
			auto regs = gs->GetRegisters();
			if(kind == "framebuffer") bitmap = iface->GetFramebuffer(regs[GS_REG_FRAME_1 + context]);
			else if(kind == "depthbuffer") bitmap = iface->GetDepthbuffer(regs[GS_REG_FRAME_1 + context], regs[GS_REG_ZBUF_1 + context]);
			else if(kind == "texture")
			{
				auto t0 = make_convertible<CGSHandler::TEX0>(regs[GS_REG_TEX0_1 + context]);
				auto t1 = make_convertible<CGSHandler::TEX1>(regs[GS_REG_TEX1_1 + context]);
				if(mip > t1.nMaxMip) throw Error("NO_IMAGE", "Mip level is not present.");
				bitmap = iface->GetTexture(t0, t1.nMaxMip, regs[GS_REG_MIPTBP1_1 + context], regs[GS_REG_MIPTBP2_1 + context], mip);
				// OpenGL exposes packed RGB5_A1, which CBitmap::GetPixel cannot decode.
				if(bitmap.GetBitsPerPixel() == 16)
				{
					Framework::CBitmap rgba(bitmap.GetWidth(), bitmap.GetHeight(), 32);
					for(unsigned y = 0; y < bitmap.GetHeight(); y++) for(unsigned x = 0; x < bitmap.GetWidth(); x++)
					{
						uint16 pixel;
						memcpy(&pixel, bitmap.GetPixels() + y * bitmap.GetPitch() + x * 2, sizeof(pixel));
						rgba.SetPixel(x, y, Framework::CColor(((pixel >> 11) & 31) * 255 / 31, ((pixel >> 6) & 31) * 255 / 31,
						                                      ((pixel >> 1) & 31) * 255 / 31, (pixel & 1) ? 255 : 0));
					}
					bitmap = std::move(rgba);
				}
				if(channel == "clut" && bitmap.GetBitsPerPixel() == 8)
				{
					std::array<uint32, 256> clut; gs->MakeLinearCLUT(t0, clut);
					bitmap = CGsContextView::LookupBitmap(bitmap, clut);
				}
			}
			else throw Error("INVALID_PARAMS", "Unknown image kind.");
			if(bitmap.IsEmpty()) throw Error("NO_IMAGE", "Renderer has no requested image.");
			if(channel == "alpha") { if(bitmap.GetBitsPerPixel() != 32) throw Error("UNSUPPORTED", "Alpha export requires a 32-bit image."); bitmap = CGsContextView::ExtractAlpha32(bitmap); }
			else if(channel != "raw" && channel != "clut") throw Error("INVALID_PARAMS", "Unknown image channel.");
			if(kind != "texture") { auto scale = iface->GetFramebufferScale(); if(displayMode != "raw") bitmap = bitmap.ResizeCanvas(640 * scale, (displayMode == "448i" ? 224 : 448) * scale); if(!p.contains("highlight") || p["highlight"].toBool()) CGsContextView::DrawKick(bitmap, kick, context, scale); if(displayMode == "448i") bitmap = bitmap.Resize(640 * scale, 448 * scale); }
		} catch(...) { error = std::current_exception(); }
	}, true);
	if(error) std::rethrow_exception(error);
	return bitmap;
}
QByteArray FrameReplaySession::Png(const Framework::CBitmap& bitmap)
{
	if(bitmap.IsEmpty()) throw Error("NO_IMAGE", "No rendered image is available.");
	auto bpp = bitmap.GetBitsPerPixel();
	if(bpp != 8 && bpp != 24 && bpp != 32) throw Error("UNSUPPORTED", "PNG export does not support this pixel format.");
	QImage image(bitmap.GetWidth(), bitmap.GetHeight(), QImage::Format_RGBA8888);
	for(unsigned y = 0; y < bitmap.GetHeight(); y++) for(unsigned x = 0; x < bitmap.GetWidth(); x++)
	{
		auto color = bitmap.GetPixel(x, y);
		image.setPixelColor(x, y, bpp == 8 ? QColor(color.r, color.r, color.r, 255) : QColor(color.r, color.g, color.b, bpp == 24 ? 255 : color.a));
	}
	QByteArray result; QBuffer buffer(&result); buffer.open(QIODevice::WriteOnly); if(!image.save(&buffer, "PNG")) throw Error("TASK_FAILED", "PNG encoder failed."); return result;
}
