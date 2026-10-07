#pragma once
#include <QObject>
#include <QTcpServer>
#include <QTcpSocket>
#include <QTimer>
#include <QJsonObject>
#include <deque>
#include <map>
#include <atomic>
#include "DebuggerBackend.h"
#include "FrameDebugger/FrameReplaySession.h"

class RemoteDebuggerServer : public QObject
{
public:
	explicit RemoteDebuggerServer(CPS2VM&, QObject* parent = nullptr);
	~RemoteDebuggerServer();
	void Listen(const QString& endpoint);
private:
	struct Request
	{
		QJsonValue id;
		QString key, method;
		QJsonObject params;
		uint64 generation;
		qint64 deadline;
		std::shared_ptr<std::atomic<bool>> connection;
		std::atomic<int> state{0}; // 0 queued, 1 executing, 2 canceled/complete
	};
	struct Artifact { QByteArray data; QString mime; uint64 generation; int expected = -1; };
	struct Capture { QString id; uint64 owner, generation; qint64 deadline; std::shared_ptr<std::atomic<bool>> alive; CGSHandler::FrameDumpFailureEvent::Connection failureConnection; QString state = "pending", artifact; QJsonObject error; };
	void Accept();
	void Read();
	void Receive(const QJsonObject&);
	void Dispatch();
	void Complete(std::shared_ptr<Request>, const QJsonValue&, const QJsonObject& error = {});
	void Send(const QJsonObject&);
	void Event(const QString&, const QJsonObject&);
	void Disconnect();
	void Tick();
	void GsRequest(std::shared_ptr<Request>);
	void LiveGsRequest(std::shared_ptr<Request>);
	void ReplayRequest(std::shared_ptr<Request>);
	void CancelCapture(const QString& code, const QString& message);
	void CaptureDone(std::shared_ptr<Capture>, QByteArray, QJsonObject error = {});
	QJsonObject AddArtifact(QByteArray, const QString&, uint64 generation);
	bool Start(const std::shared_ptr<Request>&);
	static QJsonObject Error(const QString&, const QString&);
	static QString Key(const QJsonValue&);
	CPS2VM& m_vm;
	Debugger::Backend m_backend;
	QTcpServer m_server;
	QTcpSocket* m_socket = nullptr;
	QByteArray m_input;
	QTimer m_timer;
	bool m_hello = false, m_closing = false;
	std::shared_ptr<std::atomic<bool>> m_connection;
	std::deque<std::shared_ptr<Request>> m_queue;
	std::map<QString, std::shared_ptr<Request>> m_requests;
	std::shared_ptr<Request> m_active;
	std::map<QString, Artifact> m_artifacts;
	std::map<QString, std::shared_ptr<Capture>> m_tasks;
	std::shared_ptr<Capture> m_capture;
	std::unique_ptr<FrameReplaySession> m_replay;
	QString m_replayId;
	uint64 m_knownGeneration = 0, m_nextId = 1, m_replayGeneration = 0;
	std::atomic<uint64> m_frames{0};
	Framework::CSignal<void()>::Connection m_frameConnection, m_programConnection;
	CPS2VM::DebugStoppedEvent::Connection m_stopConnection;
};
