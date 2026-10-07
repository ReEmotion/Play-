#pragma once

#include <QJsonObject>
#include <QJsonArray>
#include <QByteArray>
#include "PS2VM.h"

namespace Debugger
{
struct Error : std::runtime_error
{
	QString code;
	Error(const char* code, const char* message) : std::runtime_error(message), code(code) {}
};
void LocalCall(CPS2VM&, const QString&, const QJsonObject&);
QString Hex(uint64 value, int digits = 8);
uint64 Number(const QJsonObject&, const char* key, uint64 fallback = 0, uint64 maximum = UINT32_MAX);
int Count(const QJsonObject&, const char* key, int fallback, int maximum);
QString Text(const QJsonObject&, const char* key, const QString& fallback = {});
QByteArray Decode(const QJsonValue&);
QJsonObject Page(const QJsonArray&, const QJsonObject&);

// This backend is called on the VM thread, or for an isolated paused replay CPU.
class Backend
{
public:
	explicit Backend(CPS2VM& vm) : m_vm(vm) {}
	CMIPS& Cpu(const QString& name);
	QJsonObject Snapshot();
	QJsonValue Call(const QString& method, const QJsonObject& params);
	static QJsonArray RegisterDescriptions(CMIPS&);
	static QJsonValue CpuCall(CMIPS&, const QString& method, const QJsonObject& params);
	static QByteArray Memory(CMIPS&, uint32 address, uint32 size, bool instruction, bool write = false);
	static void WriteMemory(CMIPS&, uint32, const QByteArray&, bool instruction);
	static void Breakpoint(CMIPS&, uint32, bool enabled);
	static void Analyse(CMIPS&, uint32, uint32);
	static void FindFunctions(CPS2VM&, uint32, uint32);
	// Called on the live GS thread or the independent replay GS thread.
	static QJsonObject GsState(CGSHandler&, bool includeRam = false);
private:
	CPS2VM& m_vm;
};
}
