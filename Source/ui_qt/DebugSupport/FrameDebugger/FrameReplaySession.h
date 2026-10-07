#pragma once

#include "FrameDump.h"
#include "Vu1Vm.h"
#include "MailBox.h"
#include <QByteArray>
#include <QJsonObject>
#include <thread>
class QOffscreenSurface;

// Owns an isolated renderer and captured VU1; no live VM pointers.
class FrameReplaySession
{
public:
	FrameReplaySession();
	~FrameReplaySession();
	void Create(bool vulkan);
	void Release();
	void Run(std::function<void()>);
	void Load(const QByteArray&);
	QByteArray Export();
	void Replay(int32 target);
	QJsonValue Query(const QString&, const QJsonObject&);
	Framework::CBitmap Image(const QJsonObject&);
	static QByteArray Png(const Framework::CBitmap&);
	void RestoreVu();

	std::unique_ptr<CGSHandler> gs;
	CGsPacketMetadata metadata;
	DRAWINGKICK_INFO kick;
	CFrameDump dump;
	CVu1Vm vu;
	int command = -1;
	bool loaded = false;
	QString displayMode = "raw";
private:
	QOffscreenSurface* m_surface = nullptr;
	CMailBox m_mailbox;
	std::thread m_worker;
	bool m_end = false;
};
