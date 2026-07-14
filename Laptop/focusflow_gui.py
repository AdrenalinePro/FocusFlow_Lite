#!/usr/bin/env python3
"""
FocusFlow Lite — PyQt5 可视化 Demo (focusflow_gui.py)
=====================================================

图形界面整合眼动追踪 + 屏幕监控，替代终端仪表盘。

用法:
    python focusflow_gui.py
    python focusflow_gui.py --calibrate 10
    python focusflow_gui.py --camera-only
"""

import sys
import os
import time
import signal
import argparse
from typing import Optional

import cv2
import numpy as np

from PyQt5.QtCore import Qt, QTimer, QRectF
from PyQt5.QtGui import (
    QImage, QPixmap, QPainter, QColor, QFont,
    QPen, QBrush,
)
from PyQt5.QtWidgets import (
    QApplication, QMainWindow, QWidget, QLabel,
    QVBoxLayout, QHBoxLayout, QPushButton,
    QFrame, QSizePolicy, QMessageBox, QGridLayout,
)

from eye_tracker import EyeTracker, GazeResult, GazeState
from screen_monitor import ScreenMonitor, ScreenResult, ScreenState

# ═══════════════════════════════════════════════════════════════════════════════
# 配色
# ═══════════════════════════════════════════════════════════════════════════════
DARK_BG   = "#1a1a2e"
CARD_BG   = "#16213e"
BORDER    = "#0f3460"
BLUE      = "#4da6ff"
GREEN     = "#4dff88"
RED       = "#ff4d6a"
YELLOW    = "#ffd54f"
GRAY      = "#888899"
TEXT      = "#e0e0e0"
TEXT2     = "#a0a0b0"


# ═══════════════════════════════════════════════════════════════════════════════
# 摄像头画面组件 (QWidget + QPainter，自适应缩放)
# ═══════════════════════════════════════════════════════════════════════════════
class CameraWidget(QWidget):
    """用 QPainter 绘制摄像头画面，自动保持宽高比填充。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._frame = None       # BGR numpy array
        self.setMinimumSize(320, 240)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_frame(self, bgr: np.ndarray):
        self._frame = bgr
        self.update()  # 触发 paintEvent

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)

        if self._frame is None:
            painter.fillRect(self.rect(), QColor("#0a0a15"))
            painter.setPen(QColor(GRAY))
            painter.setFont(QFont("Segoe UI", 14))
            painter.drawText(self.rect(), Qt.AlignCenter, "等待摄像头...")
            painter.end()
            return

        rgb = cv2.cvtColor(self._frame, cv2.COLOR_BGR2RGB)
        h, w, ch = rgb.shape
        bytes_per_line = ch * w
        qimg = QImage(rgb.data, w, h, bytes_per_line, QImage.Format_RGB888)
        pix = QPixmap.fromImage(qimg)

        # 缩放到当前 widget 大小，保持宽高比
        scaled = pix.scaled(self.width(), self.height(),
                            Qt.KeepAspectRatio, Qt.SmoothTransformation)

        # 居中绘制
        x = (self.width() - scaled.width()) // 2
        y = (self.height() - scaled.height()) // 2

        # 背景
        painter.fillRect(self.rect(), QColor("#0a0a15"))
        painter.drawPixmap(x, y, scaled)

        # 边框
        painter.setPen(QPen(QColor(BORDER), 2))
        painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 10, 10)

        painter.end()


# ═══════════════════════════════════════════════════════════════════════════════
# 状态卡片
# ═══════════════════════════════════════════════════════════════════════════════
class StatusCard(QFrame):
    def __init__(self, title: str, icon: str = "", parent=None):
        super().__init__(parent)
        self.setStyleSheet(f"""
            StatusCard {{
                background-color: {CARD_BG};
                border: 1px solid {BORDER};
                border-radius: 10px;
            }}
        """)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFixedHeight(130)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 8)
        lay.setSpacing(4)

        # 标题
        tr = QHBoxLayout()
        ic = QLabel(icon); ic.setFont(QFont("Segoe UI Emoji", 16))
        ti = QLabel(title); ti.setStyleSheet(f"color:{TEXT2};font-size:12px;font-weight:bold;")
        tr.addWidget(ic); tr.addWidget(ti); tr.addStretch()
        lay.addLayout(tr)

        # 状态
        self.sl = QLabel("--")
        self.sl.setFont(QFont("Segoe UI", 20, QFont.Bold))
        self.sl.setFixedHeight(30)
        lay.addWidget(self.sl)

        # 详情
        self.dl = QLabel("")
        self.dl.setStyleSheet(f"color:{TEXT2};font-size:11px;")
        self.dl.setWordWrap(True)
        lay.addWidget(self.dl)

    def update_state(self, text: str, color: str, detail: str = ""):
        self.sl.setText(text)
        self.sl.setStyleSheet(f"color:{color};font-size:20px;font-weight:bold;")
        self.dl.setText(detail)


# ═══════════════════════════════════════════════════════════════════════════════
# 专注度弧形仪表
# ═══════════════════════════════════════════════════════════════════════════════
class FocusArc(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._v = 0.0
        self.setMinimumSize(80, 80)

    def set_value(self, v):
        self._v = max(0.0, min(1.0, v))
        self.update()

    def paintEvent(self, e):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        cx, cy = w / 2, h / 2
        r = min(w, h) / 2 - 8
        pw = 8

        # 背景弧
        p.setPen(QPen(QColor("#1a1a30"), pw, Qt.SolidLine))
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 180 * 16, 180 * 16)

        # 颜色
        if self._v >= 0.85:   c = QColor(GREEN)
        elif self._v >= 0.6:  c = QColor(YELLOW)
        else:                 c = QColor(RED)

        span = int(180 * self._v * 16)
        p.setPen(QPen(c, pw, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(QRectF(cx - r, cy - r, r * 2, r * 2), 180 * 16, -span)

        # 数字
        p.setPen(QColor(TEXT))
        p.setFont(QFont("Segoe UI", 22, QFont.Bold))
        p.drawText(QRectF(0, cy - 12, w, 28), Qt.AlignCenter, f"{int(self._v * 100)}")
        p.setPen(QColor(TEXT2)); p.setFont(QFont("Segoe UI", 9))
        p.drawText(QRectF(0, cy + 16, w, 16), Qt.AlignCenter, "%")
        p.end()


# ═══════════════════════════════════════════════════════════════════════════════
# 主窗口
# ═══════════════════════════════════════════════════════════════════════════════
class FocusFlowWindow(QMainWindow):
    def __init__(self, eye_tracker=None, screen_monitor=None, calibrate_sec=0):
        super().__init__()
        self.eye = eye_tracker
        self.screen = screen_monitor
        self._fps_count = 0
        self._fps_last = time.time()

        # 会话状态
        self._session_active = False
        self._session_start_ts = 0.0       # 会话开始时间戳
        self._session_eye_start = None     # 会话开始时的眼动统计快照
        self._session_scr_start = None     # 会话开始时的屏幕统计快照

        self._build_ui()
        self._start_timers()

        if self.eye and calibrate_sec > 0:
            QTimer.singleShot(2000, lambda: self._calibrate(calibrate_sec))

    # ── UI 构建 ────────────────────────────────────────────────────────
    def _build_ui(self):
        self.setWindowTitle("FocusFlow Lite — 智能专注度辅助系统")
        self.setMinimumSize(1100, 680)
        self.resize(1280, 780)
        self.setStyleSheet(f"""
            QMainWindow {{ background-color:{DARK_BG}; }}
            QLabel {{ color:{TEXT}; }}
            QPushButton {{
                background:{CARD_BG}; color:{TEXT}; border:1px solid {BORDER};
                border-radius:6px; padding:6px 14px; font-size:12px; font-weight:bold;
            }}
            QPushButton:hover {{ border-color:{BLUE}; }}
        """)

        cw = QWidget(); self.setCentralWidget(cw)
        root = QHBoxLayout(cw)
        root.setContentsMargins(14, 10, 14, 10)
        root.setSpacing(12)

        # ===== 左面板 =====
        left = QVBoxLayout(); left.setSpacing(6)

        left.addWidget(self._label("📷 摄像头实时画面 (无延迟)", 11))

        self.cam = CameraWidget()
        left.addWidget(self.cam, stretch=1)

        # 延迟说明
        lat = QFrame()
        lat.setStyleSheet(f"background:{CARD_BG};border:1px solid {BORDER};border-radius:8px;")
        ll = QVBoxLayout(lat); ll.setContentsMargins(10,6,10,6); ll.setSpacing(2)
        ll.addWidget(self._label("⏱ 分析延迟说明", 10, YELLOW))
        ll.addWidget(self._label(
            "摄像头: 实时  |  眼动判定: ~50ms + 最长2s防抖  |  屏幕分析: ~3s (API)",
            9, GRAY))
        left.addWidget(lat)

        # 底部状态行
        sr = QHBoxLayout()
        self.cam_dot = self._label("●", 10, GREEN); sr.addWidget(self.cam_dot)
        self.cam_txt = self._label("工作中", 10, TEXT2); sr.addWidget(self.cam_txt)
        sr.addStretch()
        self.fps_lbl = self._label("FPS: --", 10, GRAY); sr.addWidget(self.fps_lbl)
        left.addLayout(sr)

        root.addLayout(left, stretch=6)

        # ===== 右面板 =====
        right = QVBoxLayout(); right.setSpacing(10); right.setContentsMargins(0,0,0,0)

        self.eye_card = StatusCard("眼动追踪", "👁"); right.addWidget(self.eye_card)
        self.scr_card = StatusCard("屏幕监控", "📺"); right.addWidget(self.scr_card)

        # 综合专注度
        gf = QFrame()
        gf.setStyleSheet(f"background:{CARD_BG};border:1px solid {BORDER};border-radius:10px;")
        gf.setFixedHeight(140)
        gl = QHBoxLayout(gf); gl.setContentsMargins(12,8,12,8); gl.setSpacing(8)
        ginfo = QVBoxLayout()
        ginfo.addWidget(self._label("📊 综合专注度", 11, TEXT2))
        ginfo.addStretch()
        gl.addLayout(ginfo)
        self.gauge = FocusArc(); gl.addWidget(self.gauge)
        right.addWidget(gf)

        # 告警
        af = QFrame()
        af.setStyleSheet(f"background:{CARD_BG};border:1px solid {BORDER};border-radius:10px;")
        af.setFixedHeight(70)
        al = QVBoxLayout(af); al.setContentsMargins(10,6,10,4); al.setSpacing(2)
        al.addWidget(self._label("⚠ 告警", 11, TEXT2))
        self.alt = QLabel("无")
        self.alt.setStyleSheet(f"color:{GREEN};font-size:11px;"); self.alt.setWordWrap(True)
        al.addWidget(self.alt)
        right.addWidget(af)

        # 开始/结束按钮 — 右下角并排大按钮
        btn_row = QHBoxLayout(); btn_row.setSpacing(12)
        self.start_btn = QPushButton("▶ 开始记录")
        self.start_btn.setMinimumHeight(44)
        self.start_btn.setStyleSheet(
            f"QPushButton {{ background:#1a4a1a; border:2px solid {GREEN}; "
            f"border-radius:8px; font-size:15px; font-weight:bold; padding:8px 20px; }}"
            f"QPushButton:hover {{ background:{GREEN}; color:#000; }}"
        )
        self.start_btn.clicked.connect(self._start_session)
        btn_row.addWidget(self.start_btn)

        self.stop_btn = QPushButton("⏹ 结束并评估")
        self.stop_btn.setMinimumHeight(44)
        self.stop_btn.setStyleSheet(
            f"QPushButton {{ background:#4a1a1a; border:2px solid {RED}; "
            f"border-radius:8px; font-size:15px; font-weight:bold; padding:8px 20px; }}"
            f"QPushButton:hover {{ background:{RED}; color:#000; }}"
        )
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._stop_session)
        btn_row.addWidget(self.stop_btn)
        right.addLayout(btn_row)

        root.addLayout(right, stretch=4)

        # ===== 状态栏 =====
        self.statusBar().setStyleSheet(f"background:{CARD_BG};color:{TEXT2};border-top:1px solid {BORDER};font-size:11px;padding:2px 10px;")
        self.ses_lbl = QLabel("⏱ 未开始"); self.statusBar().addWidget(self.ses_lbl)
        self.stats_lbl = QLabel("专注比: --% | 走神: 0次"); self.statusBar().addPermanentWidget(self.stats_lbl)

        # 校准按钮
        self.calib_btn = QPushButton("🔵 校准 (15s)")
        self.calib_btn.clicked.connect(lambda: self._calibrate(15))
        self.statusBar().addPermanentWidget(self.calib_btn)

    def _label(self, text, size, color=TEXT):
        l = QLabel(text)
        l.setStyleSheet(f"color:{color};font-size:{size}px;font-weight:bold;" if size>=10 else f"color:{color};font-size:{size}px;")
        return l

    # ── 定时器 ─────────────────────────────────────────────────────────
    def _start_timers(self):
        self._cam_timer = QTimer(self); self._cam_timer.timeout.connect(self._tick_cam); self._cam_timer.start(33)
        self._ui_timer  = QTimer(self); self._ui_timer.timeout.connect(self._tick_ui);  self._ui_timer.start(500)
        self._fps_timer = QTimer(self); self._fps_timer.timeout.connect(self._tick_fps); self._fps_timer.start(1000)

    # ── 画面刷新 ───────────────────────────────────────────────────────
    def _tick_cam(self):
        if not self.eye: return
        self._fps_count += 1
        frame = self.eye.get_annotated_frame()
        if frame is None:
            self.cam.update()
            return
        # 叠加信息
        g = self.eye.get_state()
        y, p, r = g.head_pose.yaw, g.head_pose.pitch, g.head_pose.roll
        cv2.putText(frame, f"Y:{y:+.0f} P:{p:+.0f} R:{r:+.0f}",
                    (8, 26), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255,255,255), 1)
        cv2.putText(frame, g.state.value, (frame.shape[1]-70, 26),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.6, (100,255,100) if g.state==GazeState.FOCUSED else (100,100,255), 2)
        if not g.face_detected:
            cv2.putText(frame, "No Face", (frame.shape[1]//2-60, frame.shape[0]//2),
                        cv2.FONT_HERSHEY_SIMPLEX, 1.0, (100,100,255), 2)
        self.cam.set_frame(frame)

    def _tick_fps(self):
        now = time.time(); elapsed = now - self._fps_last
        fps = self._fps_count / elapsed if elapsed > 0 else 0
        self.fps_lbl.setText(f"FPS: {fps:.0f}")
        self._fps_count = 0; self._fps_last = now

    # ── UI 刷新 ────────────────────────────────────────────────────────
    def _tick_ui(self):
        g = self.eye.get_state() if self.eye else None
        s = self.screen.get_last_state() if self.screen else None

        # -- 眼动 --
        if g:
            c = {"专注":GREEN,"走神":RED,"未知":GRAY,"校准中":YELLOW}.get(g.state.value, GRAY)
            d = f"yaw:{g.head_pose.yaw:+.1f}  pitch:{g.head_pose.pitch:+.1f}  roll:{g.head_pose.roll:+.1f}\n专注度:{g.focus_score:.2f}  持续:{g.state_duration:.0f}s"
            if not g.face_detected: d += "\n⚠ 未检测到人脸"
            self.eye_card.update_state(g.state.value, c, d)

        # -- 屏幕 --
        if s:
            c = {"专注工作":GREEN,"一般浏览":YELLOW,"摸鱼":RED,"离开":GRAY,"未知":GRAY}.get(s.state.value, GRAY)
            app = s.app or "N/A"
            d = f"当前应用: {app}\n置信度: {s.confidence:.2f}"
            self.scr_card.update_state(f"{s.state.value} ({app})", c, d)

        # -- 综合 --
        if g and s:
            ov = 0.4*g.focus_score+0.6*s.confidence if (g.face_detected and s.state!=ScreenState.UNKNOWN) else (g.focus_score if g.face_detected else (s.confidence if s.state!=ScreenState.UNKNOWN else 0.5))
        elif g: ov = g.focus_score if g.face_detected else 0.3
        elif s: ov = s.confidence
        else: ov = 0.5
        self.gauge.set_value(ov)

        # -- 告警 --
        alerts = []
        if s and s.state==ScreenState.SLACKING: alerts.append(f"🔴 摸鱼: {s.app or '?'}")
        if g and g.state==GazeState.DISTRACTED and g.state_duration>10: alerts.append(f"🟠 持续走神 {g.state_duration:.0f}s")
        if g and not g.face_detected and self.eye and self.eye.is_camera_active: alerts.append(f"⚫ 人脸丢失 {g.state_duration:.0f}s")
        if alerts:
            self.alt.setText("\n".join(alerts)); self.alt.setStyleSheet(f"color:{RED};font-size:11px;")
        else:
            self.alt.setText("无"); self.alt.setStyleSheet(f"color:{GREEN};font-size:11px;")

        # -- 状态栏 --
        if self._session_active:
            e = time.time() - self._session_start_ts
            self.ses_lbl.setText(f"⏱ 记录中 {int(e//3600):02d}:{int(e%3600//60):02d}:{int(e%60):02d}")
        else:
            self.ses_lbl.setText("⏱ 未开始" if not self._session_start_ts else "⏱ 已结束")

        if self.eye:
            st = self.eye.get_stats()
            self.stats_lbl.setText(f"专注比: {st.get('focus_ratio',0)*100:.0f}% | 走神: {st.get('distraction_events',0)}次")

        # 摄像头指示
        if self.eye and self.eye.is_camera_active:
            self.cam_dot.setStyleSheet(f"color:{GREEN};font-size:10px;")
            self.cam_txt.setText("工作中")
        else:
            self.cam_dot.setStyleSheet(f"color:{RED};font-size:10px;")
            self.cam_txt.setText("未就绪")

        self.calib_btn.setEnabled(not (self.eye and self.eye.is_calibrating()))
        self.calib_btn.setText("🔵 校准中..." if (self.eye and self.eye.is_calibrating()) else "🔵 校准 (15s)")

    # ── 校准 ───────────────────────────────────────────────────────────
    def _calibrate(self, sec):
        if not self.eye or self.eye.is_calibrating(): return
        if not self.eye.has_seen_face:
            QMessageBox.warning(self, "无法校准", "尚未检测到人脸\n请正对摄像头，确保光线充足")
            return
        self.eye.start_calibration(sec)
        QTimer.singleShot(int(sec*1000)+600, self._calib_done)

    def _calib_done(self):
        if self.eye:
            by, bp = self.eye.get_baseline()
            self.statusBar().showMessage(f"✅ 校准完成: yaw={by:.2f}° pitch={bp:.2f}°", 5000)

    # ── 会话管理 ───────────────────────────────────────────────────────
    def _start_session(self):
        """开始记录新会话。"""
        if not self.eye and not self.screen:
            QMessageBox.warning(self, "无法开始", "没有运行中的模块")
            return

        # 重置统计
        if self.eye:
            self.eye.reset_stats()
        if self.screen:
            self.screen.reset_stats()

        self._session_active = True
        self._session_start_ts = time.time()
        self._session_eye_start = self.eye.get_stats() if self.eye else None
        self._session_scr_start = self.screen.get_stats() if self.screen else None

        self.start_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.statusBar().showMessage("▶ 会话已开始，正在记录...", 3000)

    def _stop_session(self):
        """结束会话并显示评估报告。"""
        if not self._session_active:
            return

        self._session_active = False
        session_end = time.time()
        duration = session_end - self._session_start_ts

        # 收集最终统计
        eye_stats = self.eye.get_stats() if self.eye else None
        scr_stats = self.screen.get_stats() if self.screen else None

        self.start_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.statusBar().showMessage("⏹ 会话已结束", 3000)

        # 显示报告
        self._show_report(duration, eye_stats, scr_stats)

    def _show_report(self, duration, eye_stats, scr_stats):
        """弹出评估报告对话框。"""
        h = int(duration // 3600)
        m = int((duration % 3600) // 60)
        s = int(duration % 60)
        dur_str = f"{h}小时{m}分{s}秒" if h > 0 else f"{m}分{s}秒"

        # 眼动分析
        eye_lines = []
        if eye_stats:
            fr = eye_stats.get("focus_ratio", 0) * 100
            total = eye_stats.get("total_frames", 0)
            foc = eye_stats.get("focused_frames", 0)
            dis = eye_stats.get("distracted_frames", 0)
            nof = eye_stats.get("no_face_frames", 0)
            evt = eye_stats.get("distraction_events", 0)

            # 评级
            if fr >= 85: grade, emoji = "优秀", "🌟"
            elif fr >= 70: grade, emoji = "良好", "👍"
            elif fr >= 50: grade, emoji = "一般", "⚡"
            else: grade, emoji = "需改进", "💪"

            eye_lines = [
                f"👁 眼动追踪分析",
                f"   专注比例: {fr:.1f}%  ({emoji} {grade})",
                f"   总帧数: {total}  专注帧: {foc}  走神帧: {dis}  无脸帧: {nof}",
                f"   走神事件次数: {evt}",
            ]

        # 屏幕分析
        scr_lines = []
        if scr_stats:
            api_calls = scr_stats.get("api_calls", 0)
            slack = scr_stats.get("slacking_count", 0)
            total_cap = scr_stats.get("total_captures", 0)
            scr_lines = [
                f"",
                f"📺 屏幕监控分析",
                f"   总截图: {total_cap}  API调用: {api_calls}",
                f"   摸鱼检测: {slack} 次",
            ]

        # 综合建议
        tips = []
        if eye_stats and eye_stats.get("focus_ratio", 0) < 0.6:
            tips.append("• 建议使用番茄钟法 (25分钟专注+5分钟休息)")
        if scr_stats and scr_stats.get("slacking_count", 0) > 3:
            tips.append("• 摸鱼次数较多，建议屏蔽娱乐应用通知")
        if eye_stats and eye_stats.get("no_face_frames", 0) > eye_stats.get("total_frames", 1) * 0.3:
            tips.append("• 频繁离开座位，建议设定连续学习目标")
        if not tips:
            tips.append("• 表现不错！继续保持 👍")

        msg = (
            f"<h2 style='color:#4da6ff;'>📋 FocusFlow Lite 评估报告</h2>"
            f"<p><b>会话时长:</b> {dur_str}</p>"
            f"<hr>"
            f"<pre style='font-size:13px;'>"
            + "\n".join(eye_lines + scr_lines) +
            f"</pre>"
            f"<hr>"
            f"<p><b>💡 改进建议:</b></p>"
            f"<p>{'<br>'.join(tips)}</p>"
        )

        mb = QMessageBox(self)
        mb.setWindowTitle("FocusFlow Lite — 评估报告")
        mb.setTextFormat(Qt.RichText)
        mb.setText(msg)
        mb.setStandardButtons(QMessageBox.Ok)
        mb.setStyleSheet(f"""
            QMessageBox {{
                background-color: {DARK_BG};
            }}
            QMessageBox QLabel {{
                color: {TEXT};
                min-width: 450px;
            }}
        """)
        mb.exec_()

    def closeEvent(self, ev):
        """关闭窗口 — 异步停止后台线程，避免卡死。"""
        # 保存引用，清除以防止 stop() 被重复调用
        eye = self.eye
        screen = self.screen
        self.eye = None
        self.screen = None

        # 先隐藏窗口，让用户感知关闭是即时的
        self.hide()

        # 停止定时器
        self._cam_timer.stop()
        self._ui_timer.stop()
        self._fps_timer.stop()

        # 后台线程停止 (设置标志让循环退出，不 join 阻塞)
        if eye:
            eye._running = False
        if screen:
            screen._running = False

        # 用 QTimer 延迟释放资源，不阻塞事件循环
        def _cleanup():
            if eye:
                try:
                    if eye._cap:
                        eye._cap.release()
                    if eye._face_mesh:
                        try:
                            if hasattr(eye._face_mesh, 'close'):
                                eye._face_mesh.close()
                        except Exception:
                            pass
                except Exception:
                    pass
            if screen:
                try:
                    screen.stop()
                except Exception:
                    pass

        QTimer.singleShot(100, _cleanup)
        ev.accept()


# ═══════════════════════════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════════════════════════
def main():
    p = argparse.ArgumentParser(description="FocusFlow Lite GUI")
    p.add_argument("--api-key", default="")
    p.add_argument("--camera-only", action="store_true")
    p.add_argument("--screen-only", action="store_true")
    p.add_argument("--camera-id", type=int, default=0)
    p.add_argument("--fps", type=int, default=15)
    p.add_argument("--interval", type=float, default=30.0)
    p.add_argument("--calibrate", type=float, default=0)
    p.add_argument("--no-api", action="store_true")
    args = p.parse_args()

    api_key = args.api_key or os.environ.get("MINIMAX_API_KEY", "")
    if not api_key:
        kf = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apikey.txt")
        if os.path.exists(kf):
            with open(kf) as f: api_key = f.read().strip()
    use_api = bool(api_key) and not args.no_api

    et = sm = None
    if not args.screen_only:
        et = EyeTracker(camera_id=args.camera_id, fps=args.fps, enable_logging=False)
        if not et.start():
            print("❌ 摄像头启动失败"); return 1
        print("✓ 眼动追踪已启动")
        print("⏳ 等待摄像头就绪...", end="", flush=True)
        t0 = time.time()
        while not et.is_camera_active:
            if time.time()-t0>15: print("超时"); break
            time.sleep(0.2)
        print(" 就绪")

    if not args.camera_only:
        sm = ScreenMonitor(api_key=api_key, interval=args.interval, enable_api=use_api, enable_logging=False)
        sm.start()
        print(f"✓ 屏幕监控已启动 (API={'开' if use_api else '关'})")

    app = QApplication(sys.argv)
    w = FocusFlowWindow(eye_tracker=et, screen_monitor=sm, calibrate_sec=args.calibrate)
    w.show()
    try:
        sys.exit(app.exec_())
    finally:
        if et: et.stop()
        if sm: sm.stop()

if __name__ == "__main__":
    main()
