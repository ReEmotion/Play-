#include "DebuggerBackend.h"
#include "DebugExpressionEvaluator.h"
#include "MipsFunctionPatternDb.h"
#include "ee/VuAnalysis.h"
#include "StdStreamUtils.h"
#include "PathUtils.h"
#include "xml/Parser.h"
#include "xml/Utils.h"
#include <QRegularExpression>
#include <cstring>
#include <cmath>
#include <QMessageBox>
#include <QCoreApplication>
#include "ee/MA_VU.h"
#include "FrameDebugger/GsStateUtils.h"
#include "gs/GsDebuggerInterface.h"

namespace Debugger
{
void LocalCall(CPS2VM& vm, const QString& method, const QJsonObject& params)
{
	std::exception_ptr error;
	vm.DebugInvoke([&] { try { Backend(vm).Call(method, params); } catch(...) { error = std::current_exception(); } }, true);
	if(error) { try { std::rethrow_exception(error); } catch(const std::exception& e) { QMessageBox::warning(nullptr, "Debugger", e.what()); } }
}
QJsonObject Backend::GsState(CGSHandler& gs, bool includeRam)
{
	static const std::map<uint32, const char*> registerNames = {
		{GS_REG_PRIM, "PRIM"},
		{GS_REG_RGBAQ, "RGBAQ"},
		{GS_REG_ST, "ST"},
		{GS_REG_UV, "UV"},
		{GS_REG_XYZF2, "XYZF2"},
		{GS_REG_XYZ2, "XYZ2"},
		{GS_REG_TEX0_1, "TEX0_1"},
		{GS_REG_TEX0_2, "TEX0_2"},
		{GS_REG_CLAMP_1, "CLAMP_1"},
		{GS_REG_CLAMP_2, "CLAMP_2"},
		{GS_REG_FOG, "FOG"},
		{GS_REG_XYZF3, "XYZF3"},
		{GS_REG_XYZ3, "XYZ3"},
		{GS_REG_TEX1_1, "TEX1_1"},
		{GS_REG_TEX1_2, "TEX1_2"},
		{GS_REG_TEX2_1, "TEX2_1"},
		{GS_REG_TEX2_2, "TEX2_2"},
		{GS_REG_XYOFFSET_1, "XYOFFSET_1"},
		{GS_REG_XYOFFSET_2, "XYOFFSET_2"},
		{GS_REG_PRMODECONT, "PRMODECONT"},
		{GS_REG_PRMODE, "PRMODE"},
		{GS_REG_TEXCLUT, "TEXCLUT"},
		{GS_REG_SCANMSK, "SCANMSK"},
		{GS_REG_MIPTBP1_1, "MIPTBP1_1"},
		{GS_REG_MIPTBP1_2, "MIPTBP1_2"},
		{GS_REG_MIPTBP2_1, "MIPTBP2_1"},
		{GS_REG_MIPTBP2_2, "MIPTBP2_2"},
		{GS_REG_TEXA, "TEXA"},
		{GS_REG_FOGCOL, "FOGCOL"},
		{GS_REG_TEXFLUSH, "TEXFLUSH"},
		{GS_REG_SCISSOR_1, "SCISSOR_1"},
		{GS_REG_SCISSOR_2, "SCISSOR_2"},
		{GS_REG_ALPHA_1, "ALPHA_1"},
		{GS_REG_ALPHA_2, "ALPHA_2"},
		{GS_REG_COLCLAMP, "COLCLAMP"},
		{GS_REG_TEST_1, "TEST_1"},
		{GS_REG_TEST_2, "TEST_2"},
		{GS_REG_PABE, "PABE"},
		{GS_REG_FBA_1, "FBA_1"},
		{GS_REG_FBA_2, "FBA_2"},
		{GS_REG_FRAME_1, "FRAME_1"},
		{GS_REG_FRAME_2, "FRAME_2"},
		{GS_REG_ZBUF_1, "ZBUF_1"},
		{GS_REG_ZBUF_2, "ZBUF_2"},
		{GS_REG_BITBLTBUF, "BITBLTBUF"},
		{GS_REG_TRXPOS, "TRXPOS"},
		{GS_REG_TRXREG, "TRXREG"},
		{GS_REG_TRXDIR, "TRXDIR"},
		{GS_REG_HWREG, "HWREG"},
		{GS_REG_SIGNAL, "SIGNAL"},
		{GS_REG_FINISH, "FINISH"},
		{GS_REG_LABEL, "LABEL"}};
	auto state = gs.GetDebugState(includeRam);
	QJsonArray registers;
	for(uint32 i = 0; i < state.registers.size(); i++)
	{
		auto found = registerNames.find(i);
		auto name = found == registerNames.end() ? QString("REG_%1").arg(Hex(i, 2)) : QString::fromLatin1(found->second);
		registers.append(QJsonObject{{"index", Hex(i, 2)}, {"name", name}, {"value", Hex(state.registers[i], 16)}});
	}
	QJsonObject privateRegisters, heldRegisters;
	for(const auto& reg : state.privateRegisters) privateRegisters[QString::fromStdString(reg.first)] = Hex(reg.second, 16);
	for(const auto& reg : state.delayedRegisters) heldRegisters[QString::fromStdString(reg.first)] = Hex(reg.second);
	QJsonArray contexts;
	for(unsigned context = 0; context < 2; context++)
		contexts.append(QJsonObject{{"index", int(context)}, {"state", QString::fromStdString(CGsStateUtils::GetContextState(&gs, context))}});
	QJsonArray vertices;
	if(auto iface = dynamic_cast<CGsDebuggerInterface*>(&gs))
	{
		auto input = iface->GetInputVertices();
		for(unsigned i = 0; i < 3; i++)
			vertices.append(QJsonObject{{"position", Hex(input[i].position, 16)}, {"rgbaq", Hex(input[i].rgbaq, 16)},
									   {"st", Hex(input[i].st, 16)}, {"uv", Hex(input[i].uv, 16)}, {"fog", Hex(input[i].fog, 2)}});
	}
	QJsonObject result{{"format", "play-gs-state-v1"}, {"registers", registers}, {"private_registers", privateRegisters},
					   {"held_register_low_words", heldRegisters}, {"contexts", contexts}, {"vertices", vertices},
					   {"input_state", QString::fromStdString(CGsStateUtils::GetInputState(&gs))}, {"draw_enabled", gs.GetDrawEnabled()},
					   {"transfer", QJsonObject{{"size", Hex(state.transferSize)}, {"real_size", Hex(state.transferRealSize)},
												{"x", Hex(state.transferX)}, {"y", Hex(state.transferY)}, {"dirty", state.transferDirty}}}};
	if(includeRam)
		result["ram"] = QJsonObject{{"size", int(state.ram.size())}, {"encoding", "base64"},
									{"data", QString::fromLatin1(QByteArray(reinterpret_cast<const char*>(state.ram.data()), state.ram.size()).toBase64())}};
	return result;
}

QString Hex(uint64 value, int digits) { return "0x" + QString::number(value, 16).rightJustified(digits, '0'); }
QString Text(const QJsonObject& p, const char* key, const QString& fallback)
{
	if(!p.contains(key)) return fallback;
	if(!p[key].isString()) throw Error("INVALID_PARAMS", "Expected a string.");
	return p[key].toString();
}
uint64 Number(const QJsonObject& p, const char* key, uint64 fallback, uint64 maximum)
{
	if(!p.contains(key)) return fallback;
	auto value = p[key];
	bool ok = false;
	uint64 result = 0;
	if(value.isString())
	{
		auto s = value.toString();
		if(QRegularExpression("^0x[0-9a-fA-F]+$").match(s).hasMatch()) result = s.mid(2).toULongLong(&ok, 16);
	}
	else if(value.isDouble() && value.toDouble() >= 0 && value.toDouble() <= 9007199254740991.0 && std::floor(value.toDouble()) == value.toDouble())
	{
		result = uint64(value.toDouble()); ok = true;
	}
	if(!ok || result > maximum) throw Error("INVALID_PARAMS", "Invalid or out-of-range integer.");
	return result;
}
int Count(const QJsonObject& p, const char* key, int fallback, int maximum) { return int(Number(p, key, fallback, maximum)); }
QByteArray Decode(const QJsonValue& v)
{
	if(!v.isString()) throw Error("INVALID_PARAMS", "Expected Base64 data.");
	auto data = v.toString().toLatin1();
	if(data.size() > 349528) throw Error("INVALID_PARAMS", "Binary block exceeds 256 KiB.");
	auto decoded = QByteArray::fromBase64(data);
	if(decoded.size() > 262144 || decoded.toBase64() != data) throw Error("INVALID_PARAMS", "Invalid Base64.");
	return decoded;
}
QJsonObject Page(const QJsonArray& rows, const QJsonObject& p)
{
	int offset = Count(p, "offset", 0, INT32_MAX), limit = Count(p, "limit", 256, 4096);
	QJsonArray result;
	for(int i = offset; i < rows.size() && result.size() < limit; i++) result.append(rows[i]);
	return {{"items", result}, {"total", rows.size()}, {"next", offset + result.size() < rows.size() ? QJsonValue(offset + result.size()) : QJsonValue(QJsonValue::Null)}};
}

struct Register { QString name; void* data; int bytes; bool writable; };
static std::vector<Register> Registers(MIPSSTATE& s)
{
	std::vector<Register> r;
	auto add = [&](QString name, void* data, int bytes, bool writable = true) { r.push_back({name, data, bytes, writable}); };
	add("pc", &s.nPC, 4);
	add("delayed_jump", &s.nDelayedJumpAddr, 4, false);
	add("exception", &s.nHasException, 4, false);
	add("cycle_quota", &s.cycleQuota, 4, false);
	for(int i = 0; i < 32; i++) add(QString("gpr.%1").arg(CMIPS::m_sGPRName[i]), &s.nGPR[i], 16, i != 0);
	add("hi", s.nHI, 8); add("lo", s.nLO, 8); add("hi1", s.nHI1, 8); add("lo1", s.nLO1, 8);
	add("sa", &s.nSA, 4);
	for(int i = 0; i < 32; i++) { add(QString("cop0.%1").arg(i), &s.nCOP0[i], 4); add(QString("fpr.%1").arg(i), &s.nCOP1[i], 4); }
	add("cop0.pccr", &s.cop0_pccr, 4); add("cop0.pcr0", &s.cop0_pcr[0], 4); add("cop0.pcr1", &s.cop0_pcr[1], 4);
	add("fpu.acc", &s.nCOP1A, 4); add("fpu.fcsr", &s.nFCSR, 4);
	for(int i = 0; i < 33; i++) add(QString("vf.%1").arg(i), &s.nCOP2[i], 16, i != 0);
	add("vu.acc", &s.nCOP2A, 16);
	add("vu.preup", &s.nCOP2VF_PreUp, 16, false); add("vu.upres", &s.nCOP2VF_UpRes, 16, false);
#define SCALAR(name, member) add(name, &s.member, 4)
	SCALAR("vu.q", nCOP2Q); SCALAR("vu.i", nCOP2I); SCALAR("vu.p", nCOP2P); SCALAR("vu.r", nCOP2R);
	SCALAR("vu.cf", nCOP2CF); SCALAR("vu.mf", nCOP2MF); SCALAR("vu.sf", nCOP2SF); SCALAR("vu.df", nCOP2DF); SCALAR("vu.t", nCOP2T);
	for(int i = 0; i < 16; i++) add(QString("vi.%1").arg(i), &s.nCOP2VI[i], 4, i != 0);
	SCALAR("vu.cmsar0", cmsar0);
#undef SCALAR
#define INTERNAL(member) add(#member, &s.member, sizeof(s.member), false)
	INTERNAL(pipeQ.counter); INTERNAL(pipeQ.heldValue); INTERNAL(pipeP.counter); INTERNAL(pipeP.heldValue);
	for(int i = 0; i < FLAG_PIPELINE_SLOTS; i++)
	{
		add(QString("pipeMac.value.%1").arg(i), &s.pipeMac.values[i], 4, false);
		add(QString("pipeMac.time.%1").arg(i), &s.pipeMac.pipeTimes[i], 4, false);
		add(QString("pipeSticky.value.%1").arg(i), &s.pipeSticky.values[i], 4, false);
		add(QString("pipeSticky.time.%1").arg(i), &s.pipeSticky.pipeTimes[i], 4, false);
		add(QString("pipeClip.value.%1").arg(i), &s.pipeClip.values[i], 4, false);
		add(QString("pipeClip.time.%1").arg(i), &s.pipeClip.pipeTimes[i], 4, false);
	}
	INTERNAL(pipeMac.index); INTERNAL(pipeSticky.index); INTERNAL(pipeClip.index);
	for(int i = 0; i < 3; i++) add(QString("pipeFmacWrite.%1").arg(i), &s.pipeFmacWrite[i], 16, false);
	INTERNAL(pipeTime); INTERNAL(callMsEnabled); INTERNAL(callMsAddr); INTERNAL(savedIntReg); INTERNAL(savedIntRegTemp);
	INTERNAL(savedNextBlockIntRegIdx); INTERNAL(savedNextBlockIntRegVal); INTERNAL(savedNextBlockIntRegValTemp); INTERNAL(xgkickAddress);
	for(int i = 0; i < MIPSSTATE::TLB_ENTRY_MAX; i++)
	{
		add(QString("tlb.%1.lo0").arg(i), &s.tlbEntries[i].entryLo0, 4, false);
		add(QString("tlb.%1.lo1").arg(i), &s.tlbEntries[i].entryLo1, 4, false);
		add(QString("tlb.%1.hi").arg(i), &s.tlbEntries[i].entryHi, 4, false);
		add(QString("tlb.%1.mask").arg(i), &s.tlbEntries[i].pageMask, 4, false);
	}
#undef INTERNAL
	return r;
}
QJsonArray Backend::RegisterDescriptions(CMIPS& c)
{
	QJsonArray r;
	for(auto reg : Registers(c.m_State)) r.append(QJsonObject{{"name", reg.name}, {"bits", reg.bytes * 8}, {"writable", reg.writable}});
	return r;
}
CMIPS& Backend::Cpu(const QString& name)
{
	if(name == "ee") return m_vm.m_ee->m_EE;
	if(name == "iop") return m_vm.m_iop->m_cpu;
	if(name == "vu0") return m_vm.m_ee->m_VU0;
	if(name == "vu1") return m_vm.m_ee->m_VU1;
	throw Error("INVALID_CPU", "Expected ee, iop, vu0 or vu1.");
}
QJsonObject Backend::Snapshot()
{
	QJsonObject pcs;
	for(auto name : {"ee", "iop", "vu0", "vu1"}) pcs[name] = Hex(Cpu(name).m_State.nPC);
	return {{"state", m_vm.GetStatus() == CVirtualMachine::PAUSED ? "paused" : "running"}, {"generation", Hex(m_vm.DebugGeneration(), 16)},
	        {"pc", pcs}, {"program", QString::fromUtf8(m_vm.m_ee->m_os->GetExecutableName())}};
}
static uint32 Translate(CMIPS& c, uint32 a) { return c.m_pAddrTranslator ? c.m_pAddrTranslator(&c, a) : a; }
static const CMemoryMap::MEMORYMAPELEMENT& Map(CMIPS& c, uint32 a, bool instruction, bool write)
{
	auto m = instruction ? c.m_pMemoryMap->GetInstructionMap(a) : write ? c.m_pMemoryMap->GetWriteMap(a) : c.m_pMemoryMap->GetReadMap(a);
	if(!m || !m->pPointer || m->nType != CMemoryMap::MEMORYMAP_TYPE_MEMORY) throw Error("UNSAFE_MEMORY", "Unmapped, read-only or MMIO address.");
	return *m;
}
QByteArray Backend::Memory(CMIPS& c, uint32 address, uint32 size, bool instruction, bool write)
{
	if(size > 262144 || uint64(address) + size > 0x100000000ULL) throw Error("INVALID_PARAMS", "Memory range exceeds limit or wraps.");
	QByteArray bytes; bytes.resize(size);
	for(uint32 i = 0; i < size; i++) { auto a = Translate(c, address + i); const auto& m = Map(c, a, instruction, write); bytes[i] = static_cast<char*>(m.pPointer)[a - m.nStart]; }
	return bytes;
}
void Backend::WriteMemory(CMIPS& c, uint32 address, const QByteArray& bytes, bool instruction)
{
	Memory(c, address, bytes.size(), instruction, true); // Validate the entire range before mutation.
	for(int i = 0; i < bytes.size(); i++) { auto a = Translate(c, address + i); const auto& m = Map(c, a, instruction, true); static_cast<char*>(m.pPointer)[a - m.nStart] = bytes[i]; }
	// Clear every alias of the modified physical memory, not only the input address.
	c.m_executor->Reset();
}
void Backend::Breakpoint(CMIPS& c, uint32 address, bool enabled)
{
	if(address & 3) throw Error("INVALID_PARAMS", "Breakpoint must be instruction aligned.");
	Memory(c, address, 4, true);
	if(dynamic_cast<CMA_VU*>(c.m_pArch) && (address & 7)) throw Error("INVALID_PARAMS", "VU breakpoint must be 8-byte aligned.");
	address = Translate(c, address);
	if(enabled) c.m_breakpoints.insert(address); else c.m_breakpoints.erase(address);
	c.m_executor->Reset();
}
void Backend::Analyse(CMIPS& c, uint32 begin, uint32 end)
{
	if(!c.m_analysis) throw Error("UNSUPPORTED", "This CPU has no subroutine analyser.");
	if(begin > end || (begin | end) & 3 || end - begin > PS2::EE_RAM_SIZE) throw Error("INVALID_PARAMS", "Invalid analysis range.");
	for(uint64 a = begin; a < end; a += 262144) Memory(c, a, std::min<uint64>(262144, end - a), true);
	c.m_analysis->Clear(); c.m_analysis->Analyse(begin, end);
}
void Backend::FindFunctions(CPS2VM& vm, uint32 begin, uint32 end)
{
	if(begin > end || end > PS2::EE_RAM_SIZE) throw Error("INVALID_PARAMS", "Invalid EE function range.");
	auto patterns = Framework::PathUtils::GetAppResourcesPath() / "ee_functions.xml";
	if(!fs::exists(patterns)) patterns = QCoreApplication::applicationDirPath().toStdString() + "/ee_functions.xml";
	auto stream = Framework::CreateInputStdStream(patterns.native());
	auto document = Framework::Xml::CParser::ParseDocument(stream);
	auto node = document->Select("Functions");
	if(!node) throw Error("INVALID_PARAMS", "Invalid function patterns file.");
	CMipsFunctionPatternDb db(node);
	auto& c = vm.m_ee->m_EE;
	for(const auto& pattern : db.GetPatterns()) for(uint32 a = begin; a < end; a += 4)
	{
		if(pattern.Matches(reinterpret_cast<uint32*>(vm.m_ee->m_ram + a), end - a)) { c.m_Functions.InsertTag(a, pattern.name); break; }
	}
	std::map<std::string, std::string> strings;
	for(auto n : node->SelectNodes("FunctionComments/FunctionComment")) strings[Framework::Xml::GetAttributeStringValue(n, "Comment")] = Framework::Xml::GetAttributeStringValue(n, "Function");
	for(auto i = c.m_Comments.GetTagsBegin(); i != c.m_Comments.GetTagsEnd(); ++i)
	{
		auto sub = c.m_analysis->FindSubroutine(i->first); auto f = strings.find(i->second);
		if(sub && f != strings.end()) c.m_Functions.InsertTag(sub->start, f->second);
	}
	c.m_Functions.OnTagListChange();
}
QJsonValue Backend::CpuCall(CMIPS& c, const QString& method, const QJsonObject& p)
{
	if(method == "registers.read")
	{
		QJsonObject result;
		for(auto reg : Registers(c.m_State))
		{
			auto bytes = QByteArray(static_cast<char*>(reg.data), reg.bytes); std::reverse(bytes.begin(), bytes.end());
			result[reg.name] = "0x" + QString::fromLatin1(bytes.toHex());
		}
		return result;
	}
	if(method == "registers.write")
	{
		auto name = Text(p, "name"), value = Text(p, "value");
		for(auto reg : Registers(c.m_State)) if(reg.name == name)
		{
			if(!reg.writable) throw Error("READ_ONLY_REGISTER", "Register is read-only.");
			if(!QRegularExpression("^0x[0-9a-fA-F]{1," + QString::number(reg.bytes * 2) + "}$").match(value).hasMatch()) throw Error("INVALID_PARAMS", "Register value does not fit its width.");
			auto bytes = QByteArray::fromHex(value.mid(2).rightJustified(reg.bytes * 2, '0').toLatin1()); std::reverse(bytes.begin(), bytes.end());
			if(name == "pc") { uint32 pc; memcpy(&pc, bytes.data(), 4); if(pc & (dynamic_cast<CMA_VU*>(c.m_pArch) ? 7 : 3)) throw Error("INVALID_PARAMS", "PC must be aligned."); Memory(c, pc, dynamic_cast<CMA_VU*>(c.m_pArch) ? 8 : 4, true); c.m_State.nDelayedJumpAddr = MIPS_INVALID_PC; }
			memcpy(reg.data, bytes.data(), reg.bytes); c.m_executor->Reset(); return QJsonObject{{"written", true}};
		}
		throw Error("INVALID_PARAMS", "Unknown register.");
	}
	if(method == "memory.read" || method == "memory.write")
	{
		auto a = Number(p, "address"); auto space = Text(p, "space", "data");
		if(space != "data" && space != "instruction") throw Error("INVALID_PARAMS", "Expected data or instruction space.");
		if(method.endsWith("read")) return QJsonObject{{"address", Hex(a)}, {"data", QString::fromLatin1(Memory(c, a, Count(p, "size", 64, 262144), space == "instruction").toBase64())}};
		auto bytes = Decode(p["data"]); WriteMemory(c, a, bytes, space == "instruction"); return QJsonObject{{"written", bytes.size()}};
	}
	if(method == "breakpoints.list") { QJsonArray a; for(auto b : c.m_breakpoints) a.append(Hex(b)); return Page(a, p); }
	if(method == "breakpoints.add" || method == "breakpoints.remove") { Breakpoint(c, Number(p, "address"), method.endsWith("add")); return QJsonObject{{"ok", true}}; }
	if(method == "expression.evaluate") { try { return QJsonObject{{"value", Hex(CDebugExpressionEvaluator::Evaluate(Text(p, "expression").toStdString().c_str(), &c))}}; } catch(const std::exception& e) { throw Error("INVALID_PARAMS", e.what()); } }
	if(method == "disassemble" || method == "effective_address")
	{
		uint32 a = Number(p, "address", c.m_State.nPC); int count = method == "effective_address" ? 1 : Count(p, "count", 16, 4096);
		if(a & 3 || uint64(a) + uint64(count) * 4 > 0x100000000ULL) throw Error("INVALID_PARAMS", "Invalid instruction range.");
		QJsonArray rows;
		for(int i = 0; i < count; i++, a += 4)
		{
			auto bytes = Memory(c, a, 4, true); uint32 op; memcpy(&op, bytes.data(), 4); char mn[256] = {}, args[256] = {};
			c.m_pArch->GetInstructionMnemonic(&c, a, op, mn, sizeof(mn)); c.m_pArch->GetInstructionOperands(&c, a, op, args, sizeof(args));
			rows.append(QJsonObject{{"address", Hex(a)}, {"opcode", Hex(op)}, {"mnemonic", mn}, {"operands", args}, {"effective_address", Hex(c.m_pArch->GetInstructionEffectiveAddress(&c, a, op))}});
		}
		return rows;
	}
	if(method == "callstack")
	{
		if(!c.m_analysis) throw Error("UNSUPPORTED", "No call stack analyser for this CPU.");
		QJsonArray rows;
		for(auto a : CMIPSAnalysis::GetCallStack(&c, c.m_State.nPC, c.m_State.nGPR[CMIPS::SP].nV0, c.m_State.nGPR[CMIPS::RA].nV0)) rows.append(Hex(a));
		return Page(rows, p);
	}
	if(method == "search.word" || method == "search.half" || method == "search.callers")
	{
		uint32 begin = Number(p, "begin"), end = Number(p, "end", begin + 65536ULL), value = Number(p, "value");
		if(begin > end || (begin | end) & 3 || end - begin > 262144) throw Error("INVALID_PARAMS", "Search one aligned range of at most 256 KiB; paginate with next_address.");
		auto bytes = Memory(c, begin, end - begin, method == "search.callers"); QJsonArray hits;
		for(uint32 i = 0; i < uint32(bytes.size()); i += 4) { uint32 word; memcpy(&word, bytes.data() + i, 4); auto match = method == "search.callers" ? c.m_pArch->GetInstructionEffectiveAddress(&c, begin + i, word) == value : (word & (method == "search.half" ? 0xFFFF : UINT32_MAX)) == value; if(match) hits.append(Hex(begin + i)); }
		auto page = Page(hits, p); page["next_address"] = Hex(end); return page;
	}
	if(method == "analysis.vu") { auto begin = Number(p, "begin"), end = Number(p, "end", 0x4000); if((begin | end) & 7 || begin >= end || end > 0x4000) throw Error("INVALID_PARAMS", "Invalid microprogram range."); Memory(c, begin, end - begin, true); CVuAnalysis::Analyse(&c, begin, end - 8); return QJsonObject{{"ok", true}}; }
	if(method == "analysis.run") { Analyse(c, Number(p, "begin"), Number(p, "end")); return QJsonObject{{"ok", true}}; }
	if(method.startsWith("tags."))
	{
		auto kind = Text(p, "kind", "functions"); CMIPSTags* tags = kind == "functions" ? &c.m_Functions : kind == "variables" ? &c.m_Variables : kind == "comments" ? &c.m_Comments : nullptr;
		if(!tags) throw Error("INVALID_PARAMS", "Unknown tag kind.");
		if(method == "tags.list") { QJsonArray rows; for(auto i = tags->GetTagsBegin(); i != tags->GetTagsEnd(); ++i) rows.append(QJsonObject{{"address", Hex(i->first)}, {"text", QString::fromStdString(i->second)}}); return Page(rows, p); }
		if(method == "tags.set") { auto t = Text(p, "text"); if(t.size() > 4096) throw Error("INVALID_PARAMS", "Tag text exceeds limit."); tags->InsertTag(Number(p, "address"), t.toStdString()); }
		else if(method == "tags.remove") tags->RemoveTag(Number(p, "address"));
		else throw Error("METHOD_NOT_FOUND", "Unknown tag operation.");
		tags->OnTagListChange(); return QJsonObject{{"ok", true}};
	}
	throw Error("METHOD_NOT_FOUND", "Unknown debugger method.");
}

QJsonValue Backend::Call(const QString& method, const QJsonObject& p)
{
	if(method == "status") return Snapshot();
	auto cpu = Text(p, "cpu", "ee"); auto& c = Cpu(cpu);
	if(m_vm.GetStatus() != CVirtualMachine::PAUSED && !method.startsWith("breakpoints.")) throw Error("TARGET_RUNNING", "Pause the target before this operation.");
	if(p.contains("generation") && Number(p, "generation", 0, UINT64_MAX) != m_vm.DebugGeneration()) throw Error("STALE_GENERATION", "Program or rendering backend changed.");
	struct Notify { CPS2VM& vm; bool mutate; ~Notify() { if(mutate) vm.OnMachineStateChange(); } } notify{m_vm, method.endsWith("write") || method.startsWith("tags.") || method.startsWith("analysis.") || method == "assemble.jal"};
	if(method == "tags.save" || method == "tags.load")
	{
		auto name = m_vm.m_ee->m_os->GetExecutableName();
		if(!*name) throw Error("NO_PROGRAM", "No loaded program.");
		if(method == "tags.save") m_vm.SaveDebugTags(name); else m_vm.LoadDebugTags(name);
		return QJsonObject{{"ok", true}};
	}
	if(method == "analysis.functions") { auto range = m_vm.m_ee->m_os->GetExecutableRange(); FindFunctions(m_vm, Number(p, "begin", range.first), Number(p, "end", range.second & ~3)); return QJsonObject{{"ok", true}}; }
	if(method == "assemble.jal")
	{
		uint32 a = Number(p, "address"), target = Number(p, "target");
		if((a | target) & 3 || ((a + 4) & 0xF0000000) != (target & 0xF0000000)) throw Error("INVALID_PARAMS", "JAL target must be aligned and in the same 256 MiB region.");
		uint32 op = 0x0C000000 | ((target >> 2) & 0x03FFFFFF); WriteMemory(c, a, QByteArray(reinterpret_cast<char*>(&op), 4), true); return QJsonObject{{"opcode", Hex(op)}};
	}
	if(method == "modules" || method == "kernel.types" || method == "kernel.objects")
	{
		CBiosDebugInfoProvider* bios = cpu == "ee" ? static_cast<CBiosDebugInfoProvider*>(m_vm.m_ee->m_os) : cpu == "iop" ? m_vm.m_iop->m_bios.get() : nullptr;
		if(!bios) throw Error("UNSUPPORTED", "This CPU has no BIOS objects.");
		QJsonArray rows;
		if(method == "modules") for(const auto& m : bios->GetModulesDebugInfo()) rows.append(QJsonObject{{"name", QString::fromStdString(m.name)}, {"begin", Hex(m.begin)}, {"end", Hex(m.end)}});
		else if(method == "kernel.types") for(const auto& entry : bios->GetBiosObjectsDebugInfo()) { QJsonArray fields; for(auto& f : entry.second.fields) fields.append(QJsonObject{{"name", QString::fromStdString(f.name)}, {"type", int(f.type)}, {"attributes", int(f.attributes)}}); rows.append(QJsonObject{{"id", int(entry.first)}, {"name", QString::fromStdString(entry.second.name)}, {"fields", fields}}); }
		else for(const auto& object : bios->GetBiosObjects(Number(p, "type"))) { QJsonArray fields; for(const auto& f : object.fields) fields.append(std::holds_alternative<uint32>(f) ? QJsonValue(Hex(std::get<uint32>(f))) : QJsonValue(QString::fromStdString(std::get<std::string>(f)))); rows.append(fields); }
		return Page(rows, p);
	}
	if(method.startsWith("elf."))
	{
		auto elf = m_vm.m_ee->m_os->GetELF(); if(!elf) throw Error("NO_PROGRAM", "No loaded ELF."); auto& h = elf->GetHeader(); QJsonArray rows;
		if(method == "elf.header") return QJsonObject{{"ident", QString::fromLatin1(QByteArray(reinterpret_cast<const char*>(h.nId), 16).toHex())}, {"type", h.nType}, {"cpu", h.nCPU}, {"version", int(h.nVersion)}, {"entry", Hex(h.nEntryPoint)}, {"flags", Hex(h.nFlags)}, {"program_offset", Hex(h.nProgHeaderStart)}, {"section_offset", Hex(h.nSectHeaderStart)}, {"header_size", h.nSize}, {"program_entry_size", h.nProgHeaderEntrySize}, {"program_count", h.nProgHeaderCount}, {"section_entry_size", h.nSectHeaderEntrySize}, {"section_count", int(elf->GetSectionHeaderCount())}, {"string_table", h.nSectHeaderStringTableIndex}};
		if(method == "elf.programs") for(unsigned i = 0; i < h.nProgHeaderCount; i++) { auto s = elf->GetProgram(i); rows.append(QJsonObject{{"type", int(s->nType)}, {"offset", Hex(s->nOffset)}, {"virtual_address", Hex(s->nVAddress)}, {"physical_address", Hex(s->nPAddress)}, {"file_size", int(s->nFileSize)}, {"memory_size", int(s->nMemorySize)}, {"flags", Hex(s->nFlags)}, {"alignment", int(s->nAlignment)}}); }
		else if(method == "elf.sections") for(unsigned i = 0; i < elf->GetSectionHeaderCount(); i++) { auto s = elf->GetSection(i); rows.append(QJsonObject{{"name", elf->GetSectionName(i)}, {"type", int(s->nType)}, {"address", Hex(s->nStart)}, {"offset", Hex(s->nOffset)}, {"size", int(s->nSize)}, {"flags", Hex(s->nFlags)}, {"link", int(s->nIndex)}, {"info", int(s->nInfo)}, {"alignment", int(s->nAlignment)}, {"entry_size", int(s->nOther)}}); }
		else if(method == "elf.symbols") elf->EnumerateSymbols([&](const CELF32::SYMBOL& s, uint8 t, uint8 b, const char* n) { rows.append(QJsonObject{{"name", n}, {"address", Hex(s.nValue)}, {"size", int(s.nSize)}, {"type", t}, {"binding", b}, {"section", s.nSectionIndex}}); });
		else if(method != "elf.programs") throw Error("METHOD_NOT_FOUND", "Unknown ELF query.");
		return Page(rows, p);
	}
	auto result = CpuCall(c, method, p);
	if(method == "memory.write" || method == "assemble.jal") { m_vm.m_ee->m_EE.m_executor->Reset(); m_vm.m_iop->m_cpu.m_executor->Reset(); m_vm.m_ee->m_VU0.m_executor->Reset(); m_vm.m_ee->m_VU1.m_executor->Reset(); }

	return result;
}
}
