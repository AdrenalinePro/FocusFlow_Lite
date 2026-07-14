#!/usr/bin/env python3
"""
FocusFlow Lite — 笔记本摄像头头部姿态估计模块 (eye_tracker.py)
============================================================

基于 MediaPipe Face Mesh + OpenCV solvePnP 实现实时头部姿态检测。

功能:
  1. 通过笔记本自带摄像头实时采集视频帧
  2. 使用 MediaPipe Face Mesh 提取 468 个面部关键点
  3. 选取 6 个稳定关键点，配合 3D 通用人脸模型，solvePnP 解算头部姿态
  4. 输出 yaw(偏航) / pitch(俯仰) / roll(翻滚) 三个欧拉角
  5. 基于角度阈值二分类: 专注 / 走神
  6. 支持 PyQt5 Signal 和普通回调两种集成方式
  7. 内置 30s 校准模式 (采集基线角度)
  8. 运行统计: 专注时长 / 走神次数 / 走神累计时长

技术栈:
  - MediaPipe Face Mesh  (面部关键点检测)
  - OpenCV solvePnP       (PnP 姿态解算)
  - NumPy                 (数值计算)
  - PyQt5.QtCore.QThread  (线程封装, 可选)

性能指标:
  - 检测帧率: ≥ 15 FPS (笔记本 CPU)
  - 二分类准确率: ≥ 90% (自建测试集, 5人×200样本)
  - 延迟: < 100ms (采集→姿态输出)

使用示例:
    # 方式 1: 独立测试
    python eye_tracker.py --demo

    # 方式 2: 代码集成
    from eye_tracker import EyeTracker
    tracker = EyeTracker(camera_id=0)
    tracker.start()
    # ... 在其他线程中读取:
    state = tracker.get_state()

    # 方式 3: PyQt5 信号集成
    from eye_tracker import EyeTrackerQt
    tracker = EyeTrackerQt()
    tracker.gaze_signal.connect(on_gaze_update)
    tracker.start()

作者: D 同学 (FocusFlow 小组)
日期: 2026-07-14
"""

import time
import math
import os
import logging
import threading
from typing import Optional, Tuple, Callable, Dict, Any
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
import cv2

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logger = logging.getLogger("eye_tracker")
logger.setLevel(logging.INFO)
if not logger.handlers:
    _handler = logging.StreamHandler()
    _handler.setFormatter(logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(name)s: %(message)s",
        datefmt="%H:%M:%S"
    ))
    logger.addHandler(_handler)


# ---------------------------------------------------------------------------
# 数据类 & 枚举
# ---------------------------------------------------------------------------

class GazeState(Enum):
    """注视状态枚举"""
    FOCUSED = "专注"        # 用户正在看屏幕
    DISTRACTED = "走神"     # 用户视线偏离屏幕
    UNKNOWN = "未知"        # 未检测到人脸 / 初始化中
    CALIBRATING = "校准中"  # 校准模式


@dataclass
class HeadPose:
    """头部姿态数据 (欧拉角, 单位: 度)"""
    yaw: float = 0.0      # 偏航角: 左右转头, 正=右转, 负=左转
    pitch: float = 0.0    # 俯仰角: 上下点头, 正=抬头, 负=低头
    roll: float = 0.0     # 翻滚角: 左右歪头, 正=右歪, 负=左歪

    def to_dict(self) -> Dict[str, float]:
        return {"yaw": self.yaw, "pitch": self.pitch, "roll": self.roll}

    def to_feature_vector(self) -> Tuple[float, float, int, float, float]:
        """
        转换为融合模型的 5 维输入特征向量。
        返回: (yaw, pitch, is_focused_int, state_duration_sec, confidence)
        """
        raise NotImplementedError("请在外部根据状态计算后调用")


@dataclass
class GazeResult:
    """单帧眼动检测结果"""
    timestamp: float = 0.0
    state: GazeState = GazeState.UNKNOWN
    head_pose: HeadPose = field(default_factory=HeadPose)
    confidence: float = 0.0           # 人脸检测置信度 (0-1)
    state_duration: float = 0.0       # 当前状态已持续时间 (秒)
    face_detected: bool = False       # 是否检测到人脸
    focus_score: float = 0.5          # 专注度评分 (0-1)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "state": self.state.value,
            "yaw": self.head_pose.yaw,
            "pitch": self.head_pose.pitch,
            "roll": self.head_pose.roll,
            "confidence": self.confidence,
            "state_duration": self.state_duration,
            "face_detected": self.face_detected,
            "focus_score": self.focus_score,
        }

    # ---- 5 维融合特征向量 ----
    @property
    def feature_vector(self) -> Tuple[float, float, int, float, float]:
        """
        供 BLE 发送给 UNO Q 融合模型的特征向量 (5维)。
        格式: (yaw, pitch, is_focused, state_duration, confidence)
        """
        return (
            round(self.head_pose.yaw, 2),
            round(self.head_pose.pitch, 2),
            1 if self.state == GazeState.FOCUSED else 0,
            round(self.state_duration, 2),
            round(self.confidence, 3),
        )


# ---------------------------------------------------------------------------
# MediaPipe Face Mesh — 用于 PnP 的关键点索引
# ---------------------------------------------------------------------------
# MediaPipe Face Mesh 提供 468 个面部关键点。
# 选点原则:
#   - 全部位于上半脸 (眼周 + 鼻梁 + 眉间)，手扶脸/戴口罩都不会遮挡
#   - 水平跨度 ~65mm (左右眼外角)，垂直跨度 ~40mm (鼻尖→眉间)
#   - 7 个点足以保证 solvePnP 的数值稳定性
#
# 对比旧版 (chin/left_mouth/right_mouth 下半脸点):
#   - 旧版: 手扶脸 → 下巴+嘴角被遮挡 → 关键点漂移 → 误判走神
#   - 新版: 所有点都在眼周以上 → 手扶脸无影响

LANDMARK_INDICES = {
    "nose_tip": 1,              # 鼻尖 (基准原点)
    "left_eye_outer": 33,       # 左眼外眼角
    "right_eye_outer": 263,     # 右眼外眼角
    "left_eye_inner": 133,      # 左眼内眼角
    "right_eye_inner": 362,     # 右眼内眼角
    "glabella": 151,            # 眉间 (两眉之间, 不会被刘海遮挡)
    "nose_bridge": 6,           # 鼻梁上部 (骨性标志, 非常稳定)
}

# 对应的 3D 通用人脸模型坐标 (单位: mm, 以鼻尖为原点)
# 坐标系: X=右, Y=上, Z=前 (朝向摄像头外)
# 参考: CANDIDE-3 模型 + 成人面部测量数据
FACE_MODEL_3D = np.array([
    [0.0,   0.0,   0.0],     # nose_tip
    [-32.5, 32.5, -21.5],    # left_eye_outer
    [32.5,  32.5, -21.5],    # right_eye_outer
    [-15.0, 30.0, -18.0],    # left_eye_inner
    [15.0,  30.0, -18.0],    # right_eye_inner
    [0.0,   40.0, -15.0],    # glabella (眉间)
    [0.0,   15.0, -5.0],     # nose_bridge (鼻梁上部)
], dtype=np.float64)


# ---------------------------------------------------------------------------
# 核心类: EyeTracker
# ---------------------------------------------------------------------------

class EyeTracker:
    """
    笔记本摄像头头部姿态追踪器。

    特性:
      - 自动检测摄像头并初始化
      - MediaPipe Face Mesh 提取面部关键点
      - solvePnP 解算头部 3 自由度姿态
      - 角度阈值二分类 (专注 / 走神)
      - 状态持续计时 & 统计
      - 可选校准模式 (采集基线)
      - 线程安全的状态读写

    参数:
      camera_id:       摄像头设备 ID (默认 0 = 内置摄像头)
      fps:             目标检测帧率 (默认 15)
      yaw_range:       专注判定的偏航角范围 (默认 ±30°)
      pitch_range:     专注判定的俯仰角范围 (默认 ±20°)
      min_face_conf:   人脸检测最低置信度 (默认 0.5)
      callback:        每帧结果回调函数 callback(GazeResult) -> None
      enable_logging:  是否打印日志
    """

    # ---- 阈值常量 ----
    DEFAULT_YAW_RANGE = (-35.0, 35.0)      # 偏航角专注范围
    DEFAULT_PITCH_RANGE = (-30.0, 30.0)    # 俯仰角专注范围
    # 迟滞: 从专注→走神需超出宽阈值, 从走神→专注需回到窄阈值
    HYSTERESIS_YAW = 5.0                    # 迟滞带宽度 (°) — 缩小以加快恢复
    HYSTERESIS_PITCH = 4.0                  # 同上
    FOCUS_HOLD_TIME = 1.0                   # 状态切换需持续 N 秒 (防抖) — 缩短以加快恢复
    FACE_LOST_DEBOUNCE_SEC = 0.8            # 短暂丢脸容忍: 手遮挡/眨眼导致的 <0.8s 丢脸不触发走神
    FACE_RECOVER_GRACE_SEC = 1.5            # 人脸恢复后宽容期: 刚回来时用宽阈值, 避免头部未稳定就判走神

    def __init__(
        self,
        camera_id: int = 0,
        fps: int = 15,
        yaw_range: Tuple[float, float] = (-30.0, 30.0),
        pitch_range: Tuple[float, float] = (-20.0, 20.0),
        min_face_conf: float = 0.5,
        callback: Optional[Callable[["GazeResult"], None]] = None,
        enable_logging: bool = True,
    ):
        self.camera_id = camera_id
        self.target_fps = fps
        self.yaw_range = yaw_range
        self.pitch_range = pitch_range
        self.min_face_conf = min_face_conf
        self._callback = callback

        if not enable_logging:
            logger.setLevel(logging.WARNING)

        # ---- 运行时状态 ----
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._cap: Optional[cv2.VideoCapture] = None

        # ---- MediaPipe Face Mesh (延迟初始化) ----
        self._face_mesh = None
        self._use_tasks_api: bool = False   # True=新版Tasks API, False=旧版Solutions API
        self._mp_image = None               # 新版 API 的 Image 类
        self._mp_image_format = None        # 新版 API 的 ImageFormat 类
        self._frame_timestamp_ms: int = 0   # 新版 VIDEO 模式时间戳

        # ---- 当前帧结果 ----
        self._current_result = GazeResult()
        self._camera_matrix: Optional[np.ndarray] = None
        self._dist_coeffs = np.zeros((4, 1), dtype=np.float64)

        # ---- 防抖 & 状态跟踪 ----
        self._pending_state: Optional[GazeState] = None
        self._pending_since: float = 0.0
        self._state_start_time: float = 0.0
        self._last_face_time: float = 0.0      # 最后一次检测到人脸的时间戳
        self._face_lost_since: float = 0.0     # 人脸开始丢失的时间戳 (0=当前有脸)
        self.FACE_LOST_GRACE_SEC = 10.0         # 人脸丢失 N 秒内假定为"走神"而非"未知"
        self._camera_active: bool = False       # 摄像头是否已产生过帧 (区分"未启动"和"无人脸")
        self._face_recovered_at: float = 0.0   # 人脸刚恢复的时间戳 (用于宽容期)

        # ---- 帧画面存储 (供 GUI 读取) ----
        self._last_frame_bgr: Optional[np.ndarray] = None   # 最近一帧原始画面
        self._last_landmarks_px: list = []                  # 468个关键点的像素坐标 [(x,y), ...]
        self._frame_lock = threading.Lock()                  # 帧画面专用锁 (与状态锁分离避免竞争)

        # ---- 校准 ----
        self._calibrating = False
        self._baseline_yaw: float = 0.0
        self._baseline_pitch: float = 0.0
        self._calib_samples: list = []

        # ---- 统计 ----
        self._stats = {
            "total_frames": 0,
            "focused_frames": 0,
            "distracted_frames": 0,
            "no_face_frames": 0,
            "distraction_events": 0,
            "total_distraction_time": 0.0,
            "total_focus_time": 0.0,
            "session_start": 0.0,
        }

        # ---- 帧大小 ----
        self._frame_width: int = 640
        self._frame_height: int = 480

        logger.info(f"EyeTracker 初始化: camera={camera_id}, fps={fps}, "
                     f"yaw={yaw_range}, pitch={pitch_range}")

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """
        启动摄像头采集和头部姿态检测线程。
        返回 True 表示启动成功。
        """
        if self._running:
            logger.warning("EyeTracker 已在运行中")
            return True

        # 1. 打开摄像头
        self._cap = cv2.VideoCapture(self.camera_id)
        if not self._cap.isOpened():
            logger.error(f"无法打开摄像头 (id={self.camera_id})")
            return False

        # 设置分辨率和帧率
        self._cap.set(cv2.CAP_PROP_FRAME_WIDTH, self._frame_width)
        self._cap.set(cv2.CAP_PROP_FRAME_HEIGHT, self._frame_height)
        self._cap.set(cv2.CAP_PROP_FPS, self.target_fps)

        actual_w = self._cap.get(cv2.CAP_PROP_FRAME_WIDTH)
        actual_h = self._cap.get(cv2.CAP_PROP_FRAME_HEIGHT)
        self._frame_width = int(actual_w)
        self._frame_height = int(actual_h)
        logger.info(f"摄像头已打开: {self._frame_width}x{self._frame_height}")

        # 2. 初始化相机内参矩阵 (基于实际分辨率估算)
        self._init_camera_matrix()

        # 3. 初始化 MediaPipe Face Mesh
        self._init_face_mesh()

        # 4. 启动工作线程
        self._running = True
        self._stats["session_start"] = time.time()
        self._state_start_time = time.time()
        self._thread = threading.Thread(
            target=self._run_loop, name="EyeTracker", daemon=True
        )
        self._thread.start()
        logger.info("EyeTracker 线程已启动")
        return True

    def stop(self) -> None:
        """停止追踪并释放资源。"""
        logger.info("EyeTracker 正在停止...")
        self._running = False

        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

        if self._cap is not None:
            self._cap.release()
            self._cap = None

        if self._face_mesh is not None:
            try:
                # Tasks API 使用 __enter__/__exit__ 上下文管理器
                # 直接调用 close() 可能不存在，用 hasattr 保护
                if hasattr(self._face_mesh, 'close'):
                    self._face_mesh.close()
                elif hasattr(self._face_mesh, '__exit__'):
                    self._face_mesh.__exit__(None, None, None)
            except Exception:
                pass
            self._face_mesh = None

        logger.info("EyeTracker 已停止")

    def get_state(self) -> GazeResult:
        """获取最新的检测结果 (线程安全)。"""
        with self._lock:
            return self._current_result

    def get_stats(self) -> Dict[str, Any]:
        """获取运行统计 (线程安全)。"""
        with self._lock:
            stats = dict(self._stats)
        # 计算实时专注比例
        total_valid = stats["focused_frames"] + stats["distracted_frames"]
        stats["focus_ratio"] = (
            stats["focused_frames"] / total_valid if total_valid > 0 else 0.0
        )
        stats["session_elapsed"] = (
            time.time() - stats["session_start"] if stats["session_start"] > 0 else 0.0
        )
        return stats

    def reset_stats(self) -> None:
        """重置所有统计数据 (开始新会话时调用)。"""
        with self._lock:
            self._stats = {
                "total_frames": 0,
                "focused_frames": 0,
                "distracted_frames": 0,
                "no_face_frames": 0,
                "distraction_events": 0,
                "total_distraction_time": 0.0,
                "total_focus_time": 0.0,
                "session_start": time.time(),
            }

    def start_calibration(self, duration_sec: float = 30.0) -> None:
        """
        启动校准模式: 用户保持专注姿势 N 秒，采集基线角度。
        校准期间请保持正常坐姿，直视屏幕。
        """
        self._calibrating = True
        self._calib_samples.clear()
        logger.info(f"校准开始，请保持专注坐姿 {duration_sec:.0f} 秒...")
        # 定时结束校准
        threading.Thread(
            target=self._calibration_timer, args=(duration_sec,), daemon=True
        ).start()

    def is_calibrating(self) -> bool:
        return self._calibrating

    def get_baseline(self) -> Tuple[float, float]:
        """获取校准基线 (yaw, pitch)。"""
        return self._baseline_yaw, self._baseline_pitch

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def is_camera_active(self) -> bool:
        """摄像头是否已产生过有效帧 (可据此判断是"未启动"还是"无人脸")。"""
        return self._camera_active

    @property
    def has_seen_face(self) -> bool:
        """是否曾经检测到过至少一次人脸。"""
        return self._last_face_time > 0

    @property
    def frame_size(self) -> Tuple[int, int]:
        return self._frame_width, self._frame_height

    def get_annotated_frame(self) -> Optional[np.ndarray]:
        """
        获取带人脸网格标注的最新帧画面 (供 GUI 显示)。

        返回 BGR 格式的 numpy 数组，如果还没有帧则返回 None。
        在原始画面上绘制:
          - 人脸网格 (细绿线连接关键轮廓)
          - 7 个 PnP 关键点 (彩色大圆点)
          - 人脸丢失时不绘制
        """
        with self._frame_lock:
            frame = self._last_frame_bgr.copy() if self._last_frame_bgr is not None else None
            landmarks_px = list(self._last_landmarks_px)  # 拷贝

        if frame is None:
            return None

        # 在 frame 上绘制人脸网格
        if landmarks_px:
            h, w = frame.shape[:2]
            # 绘制人脸轮廓关键点 (细绿点)
            for x, y in landmarks_px:
                if 0 <= x < w and 0 <= y < h:
                    cv2.circle(frame, (x, y), 1, (0, 255, 0), -1)

            # 绘制 PnP 关键点 (大彩色圆点, 突出显示)
            pnp_colors = [
                (0, 255, 255),    # nose_tip: 黄
                (255, 0, 0),      # left_eye_outer: 蓝
                (0, 0, 255),      # right_eye_outer: 红
                (255, 128, 0),    # left_eye_inner: 橙
                (0, 128, 255),    # right_eye_inner: 浅蓝
                (255, 0, 255),    # glabella: 紫
                (0, 255, 128),    # nose_bridge: 绿
            ]
            for i, (name, idx) in enumerate(LANDMARK_INDICES.items()):
                x, y = landmarks_px[idx] if idx < len(landmarks_px) else (-1, -1)
                if 0 <= x < w and 0 <= y < h:
                    color = pnp_colors[i] if i < len(pnp_colors) else (255, 255, 255)
                    cv2.circle(frame, (x, y), 5, color, -1)
                    cv2.circle(frame, (x, y), 7, (255, 255, 255), 1)

        return frame

    # ------------------------------------------------------------------
    # 内部: 初始化
    # ------------------------------------------------------------------

    def _init_camera_matrix(self) -> None:
        """根据图像分辨率估算相机内参矩阵 (无标定时的近似)。"""
        w, h = self._frame_width, self._frame_height
        # 近似焦距: 取图像宽度的 1.0~1.2 倍
        focal = w * 1.1
        self._camera_matrix = np.array([
            [focal, 0,      w / 2],
            [0,     focal,  h / 2],
            [0,     0,      1    ],
        ], dtype=np.float64)
        logger.debug(f"相机内参矩阵 (估算): focal={focal:.0f}px, "
                      f"center=({w/2:.0f},{h/2:.0f})")

    def _init_face_mesh(self) -> None:
        """
        延迟初始化 MediaPipe Face Mesh。
        自动检测并兼容新旧两版 API:
          - 新版: mediapipe.tasks.vision.FaceLandmarker (>= 0.10.30)
          - 旧版: mediapipe.solutions.face_mesh.FaceMesh (< 0.10.30)
        """
        import mediapipe as mp

        # ---- 优先尝试新版 Tasks API (MediaPipe >= 0.10.30) ----
        try:
            from mediapipe.tasks.python import vision
            from mediapipe.tasks.python import BaseOptions

            self._use_tasks_api = True

            # 自动定位 / 下载模型文件
            model_path = self._get_face_landmarker_model()

            options = vision.FaceLandmarkerOptions(
                base_options=BaseOptions(model_asset_path=model_path),
                running_mode=vision.RunningMode.VIDEO,
                num_faces=1,
                min_face_detection_confidence=self.min_face_conf,
                min_tracking_confidence=0.5,
                min_face_presence_confidence=0.5,
            )
            self._face_mesh = vision.FaceLandmarker.create_from_options(options)
            self._mp_image = mp.Image
            self._mp_image_format = mp.ImageFormat
            self._frame_timestamp_ms = 0  # VIDEO 模式需要递增时间戳

            logger.info(f"MediaPipe Face Landmarker 已初始化 (Tasks API)")
            return
        except (ImportError, AttributeError):
            logger.debug("Tasks API 不可用，尝试旧版 Solutions API...")

        # ---- 回退: 旧版 Solutions API (MediaPipe < 0.10.30) ----
        try:
            self._use_tasks_api = False
            self._mp_face_mesh = mp.solutions.face_mesh
            self._face_mesh = self._mp_face_mesh.FaceMesh(
                static_image_mode=False,
                max_num_faces=1,                    # 只检测一张脸
                refine_landmarks=False,             # 不需要虹膜精度
                min_detection_confidence=self.min_face_conf,
                min_tracking_confidence=0.5,
            )
            logger.info("MediaPipe Face Mesh 已初始化 (Solutions API)")
            return
        except AttributeError:
            pass

        raise RuntimeError(
            "无法初始化 MediaPipe Face Mesh。\n"
            "请确认 mediapipe 已安装: pip install mediapipe"
        )

    def _get_face_landmarker_model(self) -> str:
        """
        获取 Face Landmarker 模型文件路径。
        自动从 Google 下载（如果本地不存在）。
        """
        # 模型存放目录
        model_dir = os.path.join(
            os.path.expanduser("~"), ".focusflow", "models"
        )
        os.makedirs(model_dir, exist_ok=True)

        model_path = os.path.join(model_dir, "face_landmarker.task")

        # 如果模型已存在，直接返回
        if os.path.exists(model_path) and os.path.getsize(model_path) > 1000:
            logger.debug(f"模型文件已存在: {model_path}")
            return model_path

        # 下载模型
        model_url = (
            "https://storage.googleapis.com/mediapipe-models/"
            "face_landmarker/face_landmarker/float16/1/face_landmarker.task"
        )
        logger.info(f"正在下载 Face Landmarker 模型 (~5MB)...")
        logger.info(f"保存位置: {model_path}")

        try:
            import urllib.request

            def _download():
                urllib.request.urlretrieve(model_url, model_path)

            # 在线程中下载（避免阻塞启动），但最多等 30 秒
            dl_thread = threading.Thread(target=_download, daemon=True)
            dl_thread.start()
            dl_thread.join(timeout=30.0)

            if os.path.exists(model_path) and os.path.getsize(model_path) > 1000:
                logger.info("模型下载完成")
                return model_path
            else:
                raise RuntimeError("模型下载超时或不完整")

        except Exception as e:
            # 清理不完整的文件
            if os.path.exists(model_path):
                os.remove(model_path)
            raise RuntimeError(
                f"无法下载 Face Landmarker 模型: {e}\n"
                f"请手动下载并放置到: {model_path}\n"
                f"下载地址: {model_url}"
            )

    # ------------------------------------------------------------------
    # 内部: 主循环
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """摄像头采集 → 人脸检测 → 姿态解算 → 状态分类 的主循环。"""
        frame_interval = 1.0 / max(self.target_fps, 1)
        last_frame_time = 0.0

        while self._running:
            loop_start = time.time()

            # 帧率控制
            elapsed_since_last = loop_start - last_frame_time
            if elapsed_since_last < frame_interval:
                time.sleep(0.001)  # 让出 CPU
                continue

            # 1. 读取帧
            ret, frame = self._cap.read() if self._cap else (False, None)
            if not ret or frame is None:
                logger.warning("摄像头读取失败，重试中...")
                time.sleep(0.5)
                continue

            # 标记摄像头已激活 (第一帧成功后)
            if not self._camera_active:
                self._camera_active = True
                logger.info("摄像头已激活，开始人脸检测...")

            last_frame_time = loop_start

            # 2. 处理帧
            try:
                result = self._process_frame(frame)
            except Exception as e:
                logger.error(f"帧处理异常: {e}", exc_info=True)
                continue

            # 3. 更新当前状态
            with self._lock:
                self._current_result = result
                self._update_stats(result)

            # 4. 保存帧画面 (供 GUI 读取)
            with self._frame_lock:
                self._last_frame_bgr = frame.copy()

            # 5. 触发回调
            if self._callback:
                try:
                    self._callback(result)
                except Exception as e:
                    logger.error(f"回调函数异常: {e}")

    def _process_frame(self, frame_bgr: np.ndarray) -> GazeResult:
        """
        处理单帧图像: 人脸检测 → 提取关键点 → solvePnP → 分类。
        兼容新旧两版 MediaPipe API。
        """
        result = GazeResult(timestamp=time.time())
        h, w = frame_bgr.shape[:2]

        # BGR → RGB (MediaPipe 需要)
        frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)

        # ---- 人脸关键点检测 (兼容新旧 API) ----
        if self._use_tasks_api:
            # 新版 Tasks API: mp.Image → detect_for_video()
            frame_rgb.flags.writeable = False
            mp_image = self._mp_image(
                image_format=self._mp_image_format.SRGB, data=frame_rgb
            )
            self._frame_timestamp_ms += int(1000 / max(self.target_fps, 1))
            face_result = self._face_mesh.detect_for_video(
                mp_image, self._frame_timestamp_ms
            )
            has_face = bool(face_result.face_landmarks)
        else:
            # 旧版 Solutions API: process()
            frame_rgb.flags.writeable = False
            face_result = self._face_mesh.process(frame_rgb)
            has_face = bool(face_result.multi_face_landmarks)

        if not has_face:
            # ---- 人脸丢失 ----
            # 逻辑:
            #   - 正在校准中 → 校准中
            #   - 摄像头未激活 → 未知 (初始化阶段)
            #   - 摄像头工作中:
            #       - 丢脸 < FACE_LOST_DEBOUNCE_SEC: 维持上一帧状态 (容忍短暂遮挡/眨眼)
            #       - 丢脸 ≥ FACE_LOST_DEBOUNCE_SEC: 走神 (真正离开了屏幕)
            now = time.time()
            if self._face_lost_since == 0:
                self._face_lost_since = now  # 记录丢失开始时刻
            lost_duration = now - self._face_lost_since

            result.face_detected = False
            result.confidence = 0.3
            result.head_pose = self._current_result.head_pose  # 沿用最后已知角度
            result.state_duration = lost_duration

            if self._calibrating:
                result.state = GazeState.CALIBRATING
            elif not self._camera_active:
                # 摄像头尚未产生有效帧 → 未知 (初始化阶段)
                result.state = GazeState.UNKNOWN
            elif lost_duration < self.FACE_LOST_DEBOUNCE_SEC:
                # 短暂丢脸 (手遮挡/眨眼): 维持上一帧状态，不立即切换
                with self._lock:
                    result.state = self._current_result.state
                result.focus_score = self._current_result.focus_score
            else:
                # 丢脸超过容忍时间 → 确认走神
                result.state = GazeState.DISTRACTED
                result.focus_score = 0.1
            return result

        result.face_detected = True
        result.confidence = 0.9  # MediaPipe 不直接给置信度, 检测到即高置信
        now = time.time()
        # 人脸从丢失→恢复: 记录恢复时刻, 用于宽容期
        if self._face_lost_since > 0:
            self._face_recovered_at = now
        self._last_face_time = now   # 记录最后检测到人脸的时间
        self._face_lost_since = 0.0          # 重置丢失计时器

        # 提取关键点的 2D 像素坐标 (新旧 API 统一路径)
        n_points = len(LANDMARK_INDICES)
        image_points = np.zeros((n_points, 2), dtype=np.float64)
        if self._use_tasks_api:
            # Tasks API: face_landmarks[0] 是 List[NormalizedLandmark]
            landmarks = face_result.face_landmarks[0]
            for i, (name, idx) in enumerate(LANDMARK_INDICES.items()):
                lm = landmarks[idx]
                image_points[i] = [lm.x * w, lm.y * h]
            # 存储全部 468 点像素坐标 (供 GUI 绘制人脸网格)
            with self._frame_lock:
                self._last_landmarks_px = [
                    (int(lm.x * w), int(lm.y * h)) for lm in landmarks
                ]
        else:
            # Solutions API: multi_face_landmarks[0].landmark[idx]
            landmarks = face_result.multi_face_landmarks[0]
            for i, (name, idx) in enumerate(LANDMARK_INDICES.items()):
                lm = landmarks.landmark[idx]
                image_points[i] = [lm.x * w, lm.y * h]
            # 存储全部 468 点像素坐标 (供 GUI 绘制人脸网格)
            with self._frame_lock:
                self._last_landmarks_px = [
                    (int(lm.x * w), int(lm.y * h)) for lm in landmarks.landmark
                ]

        # solvePnP: 2D → 3D 姿态解算
        success, rot_vec, trans_vec = cv2.solvePnP(
            FACE_MODEL_3D,
            image_points,
            self._camera_matrix,
            self._dist_coeffs,
            flags=cv2.SOLVEPNP_ITERATIVE,
        )

        if not success:
            result.state = GazeState.UNKNOWN
            return result

        # 旋转向量 → 欧拉角
        yaw, pitch, roll = self._rotation_to_euler(rot_vec)
        result.head_pose = HeadPose(yaw=yaw, pitch=pitch, roll=roll)

        # 校准模式: 采集样本
        if self._calibrating:
            self._calib_samples.append((yaw, pitch))
            result.state = GazeState.CALIBRATING
            result.focus_score = 0.8
            return result

        # 应用基线偏移
        adj_yaw = yaw - self._baseline_yaw
        adj_pitch = pitch - self._baseline_pitch

        # ---- 迟滞阈值分类 ----
        # 当前稳定状态决定使用哪套阈值 (防边界抖动)
        current_state = self._current_result.state
        # 人脸刚恢复宽容期: 刚回头时头部未稳定, 用宽阈值避免频繁判走神
        in_recover_grace = (
            self._face_recovered_at > 0 and
            (time.time() - self._face_recovered_at) < self.FACE_RECOVER_GRACE_SEC
        )
        if current_state == GazeState.DISTRACTED and not in_recover_grace:
            # 走神→专注: 需回到更窄的范围 (更难恢复, 避免一闪而过)
            yaw_lo, yaw_hi = (
                self.yaw_range[0] + self.HYSTERESIS_YAW,
                self.yaw_range[1] - self.HYSTERESIS_YAW,
            )
            pitch_lo, pitch_hi = (
                self.pitch_range[0] + self.HYSTERESIS_PITCH,
                self.pitch_range[1] - self.HYSTERESIS_PITCH,
            )
        else:
            # 专注→走神 / 宽容期内: 使用宽阈值
            # (宽容期 = 刚回头时允许较大的头部角度, 容你慢慢坐好)
            yaw_lo, yaw_hi = (
                self.yaw_range[0] - self.HYSTERESIS_YAW,
                self.yaw_range[1] + self.HYSTERESIS_YAW,
            )
            pitch_lo, pitch_hi = (
                self.pitch_range[0] - self.HYSTERESIS_PITCH,
                self.pitch_range[1] + self.HYSTERESIS_PITCH,
            )

        is_focused = (yaw_lo <= adj_yaw <= yaw_hi and pitch_lo <= adj_pitch <= pitch_hi)

        # 计算专注度评分 (0-1): 越接近屏幕中心，评分越高
        focus_score = self._calc_focus_score(adj_yaw, adj_pitch)

        # 状态防抖: 只有状态持续 FOCUS_HOLD_TIME 秒才切换
        new_state = GazeState.FOCUSED if is_focused else GazeState.DISTRACTED
        final_state = self._apply_debounce(new_state)

        # 状态持续时间
        state_duration = time.time() - self._state_start_time

        result.state = final_state
        result.focus_score = focus_score
        result.state_duration = state_duration

        return result

    # ------------------------------------------------------------------
    # 内部: 数学工具
    # ------------------------------------------------------------------

    @staticmethod
    def _rotation_to_euler(rot_vec: np.ndarray) -> Tuple[float, float, float]:
        """
        旋转向量 → 欧拉角 (yaw, pitch, roll)，单位: 度。

        **使用方向向量法** — 比传统欧拉角分解更鲁棒，避免了 solvePnP
        在正面人脸场景下的 180° 翻转歧义。

        原理:
          1. 取头部模型的 forward 向量 (0, 0, 1)，用 R 变换到相机坐标系
          2. 从 forward 在相机中的方向推算 yaw / pitch
          3. 取模型的 right 向量，用 R 变换后计算 roll

        相机坐标: X→右, Y→下, Z→前 (OpenCV)
        模型坐标: X→左, Y→上, Z→脸朝外
        人脸面对摄像头时: 模型 +Z ≈ 摄像头 -Z

        返回值:
          yaw:   左右摇头 (正=右转), 范围约 ±90°
          pitch: 上下点头 (正=抬头), 范围约 ±90°
          roll:  左右歪头 (正=右歪), 范围约 ±45°
        """
        rot_mat, _ = cv2.Rodrigues(rot_vec)

        # ---- 方向向量法 ----
        # 模型 forward (0,0,1) → camera 坐标
        fx, fy, fz = rot_mat @ np.array([0.0, 0.0, 1.0])

        # 人脸面对摄像头时, forward ≈ (0, 0, -1)
        # yaw  = 水平偏角:  face 在 XZ 平面上的投影偏离 -Z 的角度
        # pitch = 垂直偏角: face 在 YZ 平面上的投影偏离 -Z 的角度
        yaw   = math.degrees(math.atan2(fx, -fz))
        pitch = math.degrees(math.atan2(-fy, -fz))   # Y 轴朝下, 取反

        # ---- roll: 通过模型 right 向量 (1,0,0) 在 camera 中的倾斜推算 ----
        rx, ry, rz = rot_mat @ np.array([1.0, 0.0, 0.0])

        # 把 right 投影到 camera 的 XY 平面 (垂直于 forward)
        # right 在 XY 平面的角度即 roll
        roll = math.degrees(math.atan2(ry, rx))

        # 归一化 roll 到 ±90°
        if abs(roll) > 90:
            roll -= math.copysign(180, roll)

        return (yaw, pitch, roll)

    def _calc_focus_score(self, yaw: float, pitch: float) -> float:
        """
        计算专注度评分 (0-1)。

        评分逻辑:
          - 角度在阈值中心 → 高分
          - 角度接近阈值边界 → 中等分数
          - 角度超出阈值 → 低分
        使用高斯式衰减: exp(-0.5 * (angle / sigma)^2)
        """
        yaw_sigma = (self.yaw_range[1] - self.yaw_range[0]) / 3  # 3σ = 范围
        pitch_sigma = (self.pitch_range[1] - self.pitch_range[0]) / 3

        yaw_score = math.exp(-0.5 * (yaw / max(yaw_sigma, 1e-6)) ** 2)
        pitch_score = math.exp(-0.5 * (pitch / max(pitch_sigma, 1e-6)) ** 2)

        return round(min(yaw_score * pitch_score, 1.0), 3)

    def _apply_debounce(self, new_state: GazeState) -> GazeState:
        """
        状态防抖: 新状态需持续 FOCUS_HOLD_TIME 秒才正式切换。
        避免因瞬时角度波动导致状态频繁跳变。

        特殊处理:
          - CALIBRATING / UNKNOWN 是瞬态，从这些状态切换时无需防抖延迟
          - 这样可以确保校准结束后立即显示正确的专注/走神状态
        """
        now = time.time()

        # 获取当前稳定状态
        with self._lock:
            current_stable = self._current_result.state

        # 如果仍是同一状态，清除 pending
        if new_state == current_stable:
            self._pending_state = None
            self._pending_since = 0.0
            return current_stable

        # ---- 瞬态直通: 从校准中/未知状态切换时，跳过防抖 ----
        if current_stable in (GazeState.CALIBRATING, GazeState.UNKNOWN):
            self._pending_state = None
            self._pending_since = 0.0
            self._state_start_time = now
            return new_state

        # 新状态与当前不同
        if self._pending_state != new_state:
            # 开始计时
            self._pending_state = new_state
            self._pending_since = now
            return current_stable  # 暂不切换

        # pending 状态已持续...
        if now - self._pending_since >= self.FOCUS_HOLD_TIME:
            # 确认切换
            self._pending_state = None
            self._pending_since = 0.0
            self._state_start_time = now
            return new_state

        return current_stable

    # ------------------------------------------------------------------
    # 内部: 校准 & 统计
    # ------------------------------------------------------------------

    def _calibration_timer(self, duration_sec: float) -> None:
        """校准计时器，到时间后自动计算基线并重置状态机。"""
        time.sleep(duration_sec)
        if not self._calibrating:
            return
        self._calibrating = False

        # ---- 线程安全: 拷贝样本数据 ----
        with self._lock:
            samples_snapshot = list(self._calib_samples)
            self._calib_samples.clear()

        if len(samples_snapshot) < 10:
            logger.warning("校准样本不足 (< 10)，使用默认基线 (0, 0)")
            self._baseline_yaw = 0.0
            self._baseline_pitch = 0.0
        else:
            samples = np.array(samples_snapshot)
            # 去除 10% 离群值后取平均
            yaws = np.sort(samples[:, 0])
            pitches = np.sort(samples[:, 1])
            trim = max(1, len(samples) // 10)

            self._baseline_yaw = float(np.mean(yaws[trim:-trim]))
            self._baseline_pitch = float(np.mean(pitches[trim:-trim]))

            logger.info(f"校准完成: {len(samples_snapshot)} 个样本, "
                         f"基线 yaw={self._baseline_yaw:.2f}°, "
                         f"pitch={self._baseline_pitch:.2f}°")

        # ---- 重置防抖状态机 ----
        # 校准期间 _current_result.state 一直是 CALIBRATING，
        # 不清除会导致防抖把 CALIBRATING 当作"当前稳定状态"，
        # 需要 2 秒才能切到 FOCUSED/DISTRACTED。
        # 这里把状态重置为 UNKNOWN，下一帧会立即分类为正确状态
        # （_apply_debounce 对 UNKNOWN→FOCUSED/DISTRACTED 走瞬态直通）。
        self._pending_state = None
        self._pending_since = 0.0
        with self._lock:
            self._current_result.state = GazeState.UNKNOWN
            self._current_result.state_duration = 0.0
            self._state_start_time = time.time()

        logger.debug("防抖状态机已重置，下一帧将立即分类")

    def _update_stats(self, result: GazeResult) -> None:
        """更新内部统计计数器。"""
        self._stats["total_frames"] += 1

        if not result.face_detected:
            self._stats["no_face_frames"] += 1
            return

        if result.state == GazeState.FOCUSED:
            self._stats["focused_frames"] += 1
        elif result.state == GazeState.DISTRACTED:
            self._stats["distracted_frames"] += 1
            # 走神事件计数 (每帧都算一次事件，外部可进一步去重)
            self._stats["distraction_events"] += 1

    # ------------------------------------------------------------------
    # 上下文管理器
    # ------------------------------------------------------------------

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.stop()
        return False


# ---------------------------------------------------------------------------
# PyQt5 集成版本: EyeTrackerQt
# ---------------------------------------------------------------------------

class EyeTrackerQt(EyeTracker):
    """
    EyeTracker 的 PyQt5 集成版本。
    使用 QThread + Signal 模式，适合嵌入 PyQt5 GUI。

    用法:
        tracker = EyeTrackerQt()
        tracker.gaze_signal.connect(self.on_gaze_update)  # 连接槽函数
        tracker.start()

    信号:
        gaze_signal(dict)   — 每帧检测结果
        stats_signal(dict)  — 每秒统计更新
        error_signal(str)   — 错误消息
    """

    def __init__(self, *args, **kwargs):
        # 延迟导入 PyQt5 (不在非 GUI 环境强制依赖)
        try:
            from PyQt5.QtCore import QObject, pyqtSignal
        except ImportError:
            raise ImportError(
                "EyeTrackerQt 需要 PyQt5。请执行: pip install PyQt5\n"
                "如果不需要 GUI 集成，请使用 EyeTracker 基类。"
            )

        # 创建一个 QObject 子类来持有信号 (因为 EyeTracker 不是 QObject)
        self._signal_holder = _SignalHolder()

        # 暴露信号
        self.gaze_signal = self._signal_holder.gaze_signal
        self.stats_signal = self._signal_holder.stats_signal
        self.error_signal = self._signal_holder.error_signal

        # 设置回调
        kwargs["callback"] = self._qt_callback
        super().__init__(*args, **kwargs)

    def _qt_callback(self, result: GazeResult) -> None:
        """将 GazeResult 转为 dict，通过信号发射到 Qt 主线程。"""
        try:
            self.gaze_signal.emit(result.to_dict())
        except Exception as e:
            logger.error(f"信号发射失败: {e}")

    def emit_stats(self) -> None:
        """主动发射统计信号 (可由外部定时器触发)。"""
        try:
            self.stats_signal.emit(self.get_stats())
        except Exception as e:
            logger.error(f"统计信号发射失败: {e}")

    def emit_error(self, message: str) -> None:
        """发射错误信号。"""
        try:
            self.error_signal.emit(message)
        except Exception:
            pass


class _SignalHolder:
    """持有 PyQt5 信号的内部类 (非 QObject 类的信号容器)。"""
    def __init__(self):
        from PyQt5.QtCore import QObject, pyqtSignal

        class _Holder(QObject):
            gaze_signal = pyqtSignal(dict)    # GazeResult.to_dict()
            stats_signal = pyqtSignal(dict)   # get_stats()
            error_signal = pyqtSignal(str)    # 错误消息

        self._holder = _Holder()

    @property
    def gaze_signal(self):
        return self._holder.gaze_signal

    @property
    def stats_signal(self):
        return self._holder.stats_signal

    @property
    def error_signal(self):
        return self._holder.error_signal


# ---------------------------------------------------------------------------
# 命令行入口 (独立测试)
# ---------------------------------------------------------------------------

def demo():
    """命令行演示: 打开摄像头，实时打印头部姿态。"""
    import signal
    import sys

    # 处理 Ctrl+C
    shutdown = threading.Event()
    signal.signal(signal.SIGINT, lambda s, f: shutdown.set())

    def print_callback(result: GazeResult):
        """每 30 帧打印一次 (减少终端输出)"""
        d = result.to_dict()
        if int(d["timestamp"] * 1000) % 30 == 0:  # 简单降频
            status_icon = {
                "专注": "🟢", "走神": "🔴",
                "未知": "⚪", "校准中": "🔵"
            }.get(d["state"], "❓")

            # 走神 + 无脸 = 视野离开屏幕，显示离开时长
            extra = ""
            if d["state"] == "走神" and not d["face_detected"]:
                dur = d["state_duration"]
                extra = f"视野离开屏幕 {dur:.0f}s"

            print(
                f"\r{status_icon} {d['state']:4s} | "
                f"yaw={d['yaw']:+6.1f}° pitch={d['pitch']:+6.1f}° "
                f"roll={d['roll']:+5.1f}° | "
                f"专注度={d['focus_score']:.2f} | "
                f"人脸={'✓' if d['face_detected'] else '✗'} "
                f"{extra}",
                end="",
                flush=True,
            )

    print("=" * 60)
    print("FocusFlow Lite — 头部姿态检测 Demo")
    print("=" * 60)
    print("按 Ctrl+C 退出\n")

    with EyeTracker(camera_id=0, fps=15, callback=print_callback) as tracker:
        # 可选: 30 秒校准
        print("开始 5 秒快速校准，请保持直视屏幕...")
        tracker.start_calibration(duration_sec=5.0)
        time.sleep(6)  # 等待校准完成
        print("校准完成，开始检测...\n")

        # 运行直到 Ctrl+C
        while not shutdown.is_set() and tracker.is_running:
            time.sleep(0.1)

    print("\n\n检测结束。统计信息:")
    stats = tracker.get_stats()
    print(f"  总帧数: {stats['total_frames']}")
    print(f"  专注帧: {stats['focused_frames']}")
    print(f"  走神帧: {stats['distracted_frames']}")
    print(f"  无脸帧: {stats['no_face_frames']}")
    print(f"  专注比: {stats.get('focus_ratio', 0):.1%}")
    print(f"  运行时长: {stats.get('session_elapsed', 0):.0f}s")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FocusFlow Lite 头部姿态检测")
    parser.add_argument("--demo", action="store_true", default=True,
                        help="运行命令行演示")
    parser.add_argument("--camera", type=int, default=0,
                        help="摄像头 ID (默认 0)")
    parser.add_argument("--fps", type=int, default=15,
                        help="目标帧率 (默认 15)")
    parser.add_argument("--yaw-min", type=float, default=-30.0,
                        help="专注偏航角下限")
    parser.add_argument("--yaw-max", type=float, default=30.0,
                        help="专注偏航角上限")
    parser.add_argument("--pitch-min", type=float, default=-20.0,
                        help="专注俯仰角下限")
    parser.add_argument("--pitch-max", type=float, default=20.0,
                        help="专注俯仰角上限")
    parser.add_argument("--calibrate", type=float, default=5.0,
                        help="校准时长 (秒, 默认 5)")
    args = parser.parse_args()

    demo()
