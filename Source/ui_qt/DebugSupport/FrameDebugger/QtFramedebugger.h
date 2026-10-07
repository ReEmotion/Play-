#pragma once

#include <QMainWindow>

#include "filesystem_def.h"
#include "gs/GSHandler.h"
#include "GsContextView.h"
#include "FrameDump.h"
#include "Vu1Vm.h"
#include "FrameReplaySession.h"
#include "Vu1ProgramView.h"

namespace Ui
{
	class QtFramedebugger;
}

class QOffscreenSurface;

class QtFramedebugger : public QMainWindow
{
	Q_OBJECT

public:
	explicit QtFramedebugger();
	~QtFramedebugger();

protected:
	void showEvent(QShowEvent*) Q_DECL_OVERRIDE;

private slots:
	void on_action640x448_Interlaced_triggered(bool);
	void on_action640x448_Non_Interlaced_triggered(bool);
	void on_actionRaw_triggered(bool);
	void on_actionAlpha_Blend_Enabled_triggered(bool);
	void on_actionDepth_Test_Enabled_triggered(bool);
	void on_actionAlpha_Test_Enabled_triggered(bool);
	void on_actionGsHandlerOpenGL_triggered(bool);
	void on_actionGsHandlerVulkan_triggered(bool);
	void on_actionLoad_Dump_triggered();
	void on_actionStep_VU1_triggered();
	void on_context0Buffer_currentIndexChanged(int index);
	void on_context0Source_currentIndexChanged(int index);

	void on_context1Buffer_currentIndexChanged(int index);
	void on_context1Source_currentIndexChanged(int index);

	void on_tabWidget_currentChanged(int index);

	void on_prevKickbutton_clicked();
	void on_nextKickButton_clicked();

private:
	enum GS_HANDLERS
	{
		OPENGL,
#if HAS_GSH_VULKAN
		VULKAN,
#endif
		MAX_HANDLER
	};

	void selectionChanged();
	void ReleaseTreeViewModel();

	void UpdateDisplay(int32);
	void UpdateCurrentTab();
	int GetCurrentCmdIndex();

	void LoadFrameDump(fs::path);

	void CreateGsHandler();
	void ReleaseGsHandler();
	void SetFbDisplayMode(CGsContextView::FB_DISPLAY_MODE);
	void StepVu1();
	void Redraw();

	Ui::QtFramedebugger* ui;
	FrameReplaySession m_replay;
	std::unique_ptr<CGSHandler>& m_gs = m_replay.gs;
	CGsPacketMetadata& m_currentMetadata = m_replay.metadata;
	DRAWINGKICK_INFO& m_currentDrawingKick = m_replay.kick;
	CGsContextView::FB_DISPLAY_MODE m_fbDisplayMode = CGsContextView::FB_DISPLAY_MODE_RAW;
	CFrameDump& m_frameDump = m_replay.dump;
	CVu1Vm& m_vu1vm = m_replay.vu;

	std::unique_ptr<CGsContextView> m_gsContextView0;
	std::unique_ptr<CGsContextView> m_gsContextView1;
	std::unique_ptr<CVu1ProgramView> m_vu1ProgramView;
};
