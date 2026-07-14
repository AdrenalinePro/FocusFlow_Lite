#!/usr/bin/env python3
"""
FocusFlow Lite — 屏幕内容监控模块 (screen_monitor.py)
=====================================================

D 同学主责模块。定期截取笔记本屏幕，调用 minimax vision API 分析内容，
判断用户当前是在专注工作、一般浏览、摸鱼还是已离开。

功能:
  1. 定时截屏 (默认 30s 间隔)
  2. 感知哈希 (pHash) 图像变化检测，避免重复调用 API
  3. 调用 minimax vision API 进行屏幕内容理解
  4. 输出四分类: 专注工作 / 一般浏览 / 摸鱼 / 离开
  5. 白名单机制 (用户可配置允许的应用)
  6. API 降级策略 (网络异常 / 限流 / 超时)
  7. 本地缓存上次成功的结果
  8. PyQt5 信号 + 普通回调两种集成方式

技术栈:
  - mss              (高速屏幕截图)
  - Pillow           (图像缩放/编码)
  - imagehash        (感知哈希, 变化检测)
  - requests         (HTTP 调用 minimax API)
  - base64           (图像编码传输)

性能指标:
  - 截图延迟: < 50ms
  - 截图→判定: < 3s (含 API 往返)
  - API 调用节流: 仅在图像变化 > 5% 时调用
  - 降级响应: < 100ms (使用缓存)

使用示例:
    # 方式 1: 独立测试
    python screen_monitor.py --demo

    # 方式 2: 代码集成
    from screen_monitor import ScreenMonitor
    monitor = ScreenMonitor(api_key="YOUR_KEY", interval=30)
    monitor.start()
    state = monitor.get_last_state()

    # 方式 3: PyQt5 信号集成
    from screen_monitor import ScreenMonitorQt
    monitor = ScreenMonitorQt(api_key="YOUR_KEY")
    monitor.screen_signal.connect(on_screen_update)
    monitor.start()

作者: D 同学 (FocusFlow 小组)
日期: 2026-07-14
"""

import time
import json
import base64
import hashlib
import logging
import threading
import io
from typing import Optional, Callable, Dict, Any, List, Tuple
from dataclasses import dataclass, field
from enum import Enum

import numpy as np
from PIL import Image

# ---------------------------------------------------------------------------
# 日志配置
# ---------------------------------------------------------------------------
logger = logging.getLogger("screen_monitor")
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

class ScreenState(Enum):
    """屏幕内容状态枚举"""
    FOCUSED_WORK = "专注工作"    # 编程、写作、看PDF、看课程视频
    CASUAL_BROWSE = "一般浏览"   # 查资料、看文档
    SLACKING = "摸鱼"            # 看B站、刷抖音、逛淘宝、玩游戏
    AWAY = "离开"                # 黑屏、锁屏、无人
    UNKNOWN = "未知"             # 初始化 / API 不可用


# 状态 → 融合模型编码 (供 UNO Q 13 维特征向量使用)
SCREEN_STATE_CODES: Dict[ScreenState, float] = {
    ScreenState.AWAY: 0.0,
    ScreenState.SLACKING: 0.3,
    ScreenState.CASUAL_BROWSE: 0.6,
    ScreenState.FOCUSED_WORK: 1.0,
    ScreenState.UNKNOWN: 0.5,
}

# 默认白名单应用 (可被用户配置覆盖)
DEFAULT_WHITELIST = [
    "vscode", "visual studio code",
    "pycharm", "intellij", "clion", "goland", "webstorm",
    "sublime text", "atom", "notepad++", "vim", "neovim",
    "word", "pages", "wps", "libreoffice",
    "pdf", "adobe acrobat", "zotero", "mendeley", "endnote",
    "zoom", "腾讯会议", "teams", "dingtalk", "飞书",
    "terminal", "cmd", "powershell", "iterm",
    "jupyter", "colab", "kaggle",
    "obsidian", "notion", "typora", "markdown",
    "matlab", "rstudio", "spyder",
    "blender", "figma", "photoshop",  # 设计类也算工作
]


@dataclass
class ScreenResult:
    """单次屏幕分析结果"""
    timestamp: float = 0.0
    state: ScreenState = ScreenState.UNKNOWN
    confidence: float = 0.5         # 置信度 (0-1)
    app: str = ""                   # 识别的应用名
    reason: str = ""                # 判断理由
    image_changed: bool = False     # 图像是否变化
    from_cache: bool = False        # 是否来自缓存
    from_fallback: bool = False     # 是否降级结果
    latency: float = 0.0            # API 调用延迟 (秒)
    raw_response: str = ""          # API 原始返回 (调试用)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "state": self.state.value,
            "state_code": SCREEN_STATE_CODES.get(self.state, 0.5),
            "confidence": self.confidence,
            "app": self.app,
            "reason": self.reason,
            "image_changed": self.image_changed,
            "latency": self.latency,
            "from_cache": self.from_cache,
            "from_fallback": self.from_fallback,
        }

    # ---- 3 维融合特征向量 ----
    @property
    def feature_vector(self) -> Tuple[float, float, int]:
        """
        供 BLE 发送给 UNO Q 融合模型的特征向量 (3维)。
        格式: (screen_state_code, screen_confidence, app_category)
        app_category: 0=离开, 1=摸鱼, 2=一般浏览, 3=专注工作, -1=未知
        """
        category_map = {
            ScreenState.AWAY: 0,
            ScreenState.SLACKING: 1,
            ScreenState.CASUAL_BROWSE: 2,
            ScreenState.FOCUSED_WORK: 3,
            ScreenState.UNKNOWN: -1,
        }
        state_code = SCREEN_STATE_CODES.get(self.state, 0.5)
        category = category_map.get(self.state, -1)
        return (
            round(state_code, 2),
            round(self.confidence, 3),
            category,
        )


# ---------------------------------------------------------------------------
# Minimax Vision API 客户端
# ---------------------------------------------------------------------------

class MinimaxVisionClient:
    """
    minimax vision API 客户端封装。

    支持:
      - 图片压缩 + base64 编码
      - 结构化 Prompt 工程
      - 超时 & 重试
      - JSON 解析 & 容错
    """

    # API 配置 (待 Day 1 验证后调整)
    DEFAULT_ENDPOINT = "https://api.minimaxi.com/v1/text/chatcompletion_v2"
    DEFAULT_MODEL = "MiniMax-M3"
    DEFAULT_TIMEOUT = 30  # 秒 (vision 请求较慢)
    MAX_RETRIES = 2

    # Prompt 模板
    ANALYSIS_PROMPT = """你是一个学习状态分析助手。请分析这张屏幕截图，判断用户当前是否在专注学习/工作。

可能的状态：
- 专注工作（如编程IDE、写作软件、PDF阅读器、课程视频、专业软件）
- 一般浏览（如查资料、看文档网页、搜索引擎、GitHub）
- 摸鱼（如B站/YouTube、抖音/快手、淘宝/京东、游戏、娱乐新闻、社交网络刷帖）
- 离开（如黑屏、锁屏界面、桌面无任何应用）

判断规则：
1. 如果屏幕是代码编辑器、终端、专业软件 → 专注工作
2. 如果屏幕是文档/资料/搜索引擎但无明显娱乐内容 → 一般浏览
3. 如果屏幕是视频网站、购物网站、游戏、娱乐社交 → 摸鱼
4. 如果屏幕是锁屏/桌面/全黑 → 离开
5. 如果同时有工作和娱乐内容，按主要窗口判断

只返回 JSON，不要其他文字：
{"state": "专注工作"|"一般浏览"|"摸鱼"|"离开", "confidence": 0-1, "app": "识别的应用名称", "reason": "一句话理由"}"""

    def __init__(
        self,
        api_key: str,
        endpoint: Optional[str] = None,
        model: Optional[str] = None,
        timeout: int = DEFAULT_TIMEOUT,
    ):
        self.api_key = api_key
        self.endpoint = endpoint or self.DEFAULT_ENDPOINT
        self.model = model or self.DEFAULT_MODEL
        self.timeout = timeout

        # 调用统计
        self._call_count = 0
        self._error_count = 0
        self._total_latency = 0.0

        logger.info(f"MinimaxVisionClient 已初始化: endpoint={self.endpoint}, "
                     f"model={self.model}, timeout={timeout}s")

    def analyze(
        self,
        image: Image.Image,
        prompt: Optional[str] = None,
    ) -> ScreenResult:
        """
        调用 minimax vision API 分析屏幕截图。

        参数:
          image:  PIL Image 对象 (建议 512x512)
          prompt: 自定义 prompt (可选, 默认使用 ANALYSIS_PROMPT)

        返回:
          ScreenResult 对象
        """
        result = ScreenResult(timestamp=time.time())

        # 1. 图片编码
        try:
            img_base64 = self._encode_image(image)
        except Exception as e:
            logger.error(f"图片编码失败: {e}")
            result.state = ScreenState.UNKNOWN
            result.reason = f"图片编码失败: {e}"
            result.from_fallback = True
            return result

        # 2. 构造请求
        prompt_text = prompt or self.ANALYSIS_PROMPT
        payload = {
            "model": self.model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt_text},
                    {
                        "type": "image_url",
                        "image_url": {
                            "url": f"data:image/jpeg;base64,{img_base64}"
                        },
                    },
                ],
            }],
        }
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

        # 3. 发送请求 (含重试)
        import requests

        last_error = None
        for attempt in range(1 + self.MAX_RETRIES):
            try:
                t0 = time.time()
                resp = requests.post(
                    self.endpoint,
                    headers=headers,
                    json=payload,
                    timeout=self.timeout,
                )
                latency = time.time() - t0
                self._call_count += 1
                self._total_latency += latency

                if resp.status_code == 200:
                    data = resp.json()

                    # ---- MiniMax 业务层错误码 (HTTP 200 但仍可能失败) ----
                    base_resp = data.get("base_resp", {})
                    biz_code = base_resp.get("status_code", 0)
                    biz_msg = base_resp.get("status_msg", "")

                    if biz_code == 1004:
                        logger.error(f"API 鉴权失败: {biz_msg}")
                        self._error_count += 1
                        result.latency = latency
                        result.state = ScreenState.UNKNOWN
                        result.reason = "API Key 无效，请检查"
                        result.from_fallback = True
                        return result

                    if biz_code == 1008:
                        logger.error(f"API 余额不足: {biz_msg}")
                        self._error_count += 1
                        result.latency = latency
                        result.state = ScreenState.UNKNOWN
                        result.reason = "API 余额不足，请充值"
                        result.from_fallback = True
                        return result

                    if biz_code != 0:
                        logger.warning(f"API 业务错误 ({biz_code}): {biz_msg}")
                        self._error_count += 1
                        result.latency = latency
                        result.state = ScreenState.UNKNOWN
                        result.reason = f"API 错误: {biz_msg}"
                        result.from_fallback = True
                        return result

                    parsed = self._parse_response(data)
                    result.latency = round(latency, 2)
                    result.state = parsed["state"]
                    result.confidence = parsed["confidence"]
                    result.app = parsed["app"]
                    result.reason = parsed["reason"]
                    result.raw_response = json.dumps(data, ensure_ascii=False)
                    logger.debug(
                        f"API 调用成功 ({latency:.2f}s): "
                        f"state={result.state.value}, "
                        f"app={result.app}, conf={result.confidence:.2f}"
                    )
                    return result

                elif resp.status_code == 429:
                    # 限流: 退避重试
                    wait = 2 ** attempt
                    logger.warning(f"API 限流 (429), {wait}s 后重试...")
                    time.sleep(wait)
                    last_error = f"HTTP 429 (rate limited)"

                elif resp.status_code >= 500:
                    # 服务端错误: 退避重试
                    wait = 2 ** attempt
                    logger.warning(f"API 服务端错误 ({resp.status_code}), "
                                   f"{wait}s 后重试...")
                    time.sleep(wait)
                    last_error = f"HTTP {resp.status_code}"

                else:
                    # 客户端错误: 不重试
                    logger.error(f"API 客户端错误: HTTP {resp.status_code}, "
                                 f"body={resp.text[:200]}")
                    self._error_count += 1
                    result.state = ScreenState.UNKNOWN
                    result.reason = f"API 客户端错误: HTTP {resp.status_code}"
                    result.from_fallback = True
                    return result

            except requests.Timeout:
                logger.warning(f"API 超时 ({self.timeout}s), "
                               f"尝试 {attempt + 1}/{1 + self.MAX_RETRIES}")
                last_error = "timeout"

            except requests.ConnectionError as e:
                logger.warning(f"API 连接失败: {e}, "
                               f"尝试 {attempt + 1}/{1 + self.MAX_RETRIES}")
                last_error = f"connection error: {e}"

            except Exception as e:
                logger.error(f"API 调用异常: {e}", exc_info=True)
                self._error_count += 1
                result.state = ScreenState.UNKNOWN
                result.reason = f"API 异常: {e}"
                result.from_fallback = True
                return result

        # 所有重试耗尽
        self._error_count += 1
        logger.error(f"API 调用失败 (已重试 {self.MAX_RETRIES} 次): {last_error}")
        result.state = ScreenState.UNKNOWN
        result.reason = f"API 不可用: {last_error}"
        result.from_fallback = True
        return result

    @staticmethod
    def _encode_image(image: Image.Image, quality: int = 55) -> str:
        """PIL Image → base64 JPEG 字符串。"""
        buf = io.BytesIO()
        # 确保 RGB 模式
        if image.mode in ("RGBA", "P", "LA"):
            image = image.convert("RGB")
        image.save(buf, format="JPEG", quality=quality)
        return base64.b64encode(buf.getvalue()).decode("ascii")

    @staticmethod
    def _parse_response(data: Dict) -> Dict[str, Any]:
        """
        从 minimax API 返回的 JSON 中提取分析结果。

        兼容多种返回格式:
          标准格式: {"choices":[{"message":{"content":"{...json...}"}}]}
          直接格式: {"state": "...", ...}
        """
        # 尝试标准路径
        try:
            content = data["choices"][0]["message"]["content"]
            # 去除可能的 markdown 代码块标记
            content = content.strip()
            if content.startswith("```"):
                # 移除 ```json ... ``` 包裹
                lines = content.split("\n")
                content = "\n".join(
                    l for l in lines
                    if not l.strip().startswith("```")
                )
            parsed = json.loads(content.strip())
            return {
                "state": ScreenState(parsed.get("state", "未知")),
                "confidence": float(parsed.get("confidence", 0.5)),
                "app": str(parsed.get("app", "")),
                "reason": str(parsed.get("reason", "")),
            }
        except (KeyError, IndexError, json.JSONDecodeError, ValueError):
            pass

        # 尝试直接在顶层找
        try:
            if "state" in data:
                return {
                    "state": ScreenState(data.get("state", "未知")),
                    "confidence": float(data.get("confidence", 0.5)),
                    "app": str(data.get("app", "")),
                    "reason": str(data.get("reason", "")),
                }
        except (ValueError, TypeError):
            pass

        # 解析失败
        logger.warning(f"无法解析 API 返回: {str(data)[:300]}")
        return {
            "state": ScreenState.UNKNOWN,
            "confidence": 0.5,
            "app": "",
            "reason": "API 返回格式无法解析",
        }

    @property
    def stats(self) -> Dict[str, Any]:
        """获取 API 调用统计。"""
        return {
            "call_count": self._call_count,
            "error_count": self._error_count,
            "avg_latency": (
                self._total_latency / self._call_count
                if self._call_count > 0 else 0
            ),
        }


# ---------------------------------------------------------------------------
# 屏幕截图器
# ---------------------------------------------------------------------------

class ScreenCapturer:
    """
    屏幕截图器。

    使用 mss 库实现高速截屏 (比 PIL.ImageGrab 快 2-3 倍)。
    支持多显示器，默认截取主显示器。
    """

    def __init__(self, monitor_index: int = 1, resize_to: Tuple[int, int] = (640, 640)):
        self.monitor_index = monitor_index
        self.resize_to = resize_to
        self._sct = None

    def _ensure_mss(self):
        """延迟导入 mss。"""
        if self._sct is None:
            import mss
            self._sct = mss.MSS()

    def capture(self) -> Image.Image:
        """
        截取屏幕并返回缩放后的 PIL Image。

        返回:
          RGB 模式的 PIL Image，尺寸为 resize_to。
        """
        self._ensure_mss()

        # 获取监视器区域
        # mss monitors: [0]=全部拼接, [1]=主显示器, [2]=副显...
        monitors = self._sct.monitors
        if self.monitor_index >= len(monitors):
            logger.warning(
                f"监视器 {self.monitor_index} 不存在，使用主监视器 (1)"
            )
            self.monitor_index = 1

        monitor = monitors[self.monitor_index]

        # 截屏 (返回 BGRA 原始字节)
        sct_img = self._sct.grab(monitor)

        # BGRA → RGB (用 numpy，比 Pillow raw decoder 更可靠)
        raw = np.frombuffer(sct_img.bgra, dtype=np.uint8)
        raw = raw.reshape(sct_img.height, sct_img.width, 4)
        rgb = raw[:, :, [2, 1, 0]]  # BGRA → RGB (丢弃 Alpha)
        pil_img = Image.fromarray(rgb)

        # 缩放
        if self.resize_to:
            pil_img = pil_img.resize(self.resize_to, Image.LANCZOS)

        return pil_img

    def close(self):
        """释放 mss 资源。"""
        if self._sct is not None:
            try:
                self._sct.close()
            except Exception:
                pass  # mss 在 Windows 上有已知的 ReleaseDC bug
            self._sct = None


# ---------------------------------------------------------------------------
# 核心类: ScreenMonitor
# ---------------------------------------------------------------------------

class ScreenMonitor:
    """
    屏幕内容监控器。

    特性:
      - 定时截屏 (可配置间隔)
      - 感知哈希图像变化检测 (避免无效 API 调用)
      - minimax vision API 行为分析
      - 白名单机制
      - API 降级策略 (缓存上次结果 / 仅依赖脑电+眼动)

    参数:
      api_key:         minimax API 密钥
      interval:        截图间隔 (秒, 默认 30)
      change_threshold: 图像变化阈值 (0-1, 默认 0.95, 1-hamming_dist/64)
      whitelist:       用户白名单应用列表
      callback:        结果回调 callback(ScreenResult) -> None
      enable_api:      是否启用 API 调用 (False 时仅本地判断)
    """

    def __init__(
        self,
        api_key: str,
        interval: float = 30.0,
        change_threshold: float = 0.95,
        whitelist: Optional[List[str]] = None,
        callback: Optional[Callable[["ScreenResult"], None]] = None,
        enable_api: bool = True,
        enable_logging: bool = True,
    ):
        self.interval = interval
        self.change_threshold = change_threshold
        self.enable_api = enable_api
        self._callback = callback

        if not enable_logging:
            logger.setLevel(logging.WARNING)

        # API 客户端
        self._api_client = MinimaxVisionClient(api_key=api_key) if api_key else None

        # 截图器
        self._capturer = ScreenCapturer()

        # 白名单
        self.whitelist: List[str] = [
            w.lower() for w in (whitelist or DEFAULT_WHITELIST)
        ]

        # ---- 运行时状态 ----
        self._lock = threading.Lock()
        self._running = False
        self._thread: Optional[threading.Thread] = None

        # 图像变化检测 (感知哈希)
        self._last_phash: Optional[Any] = None   # imagehash 对象
        self._last_image_hash: str = ""          # hex 字符串

        # 结果缓存
        self._last_result = ScreenResult(state=ScreenState.UNKNOWN)
        self._last_successful_result = ScreenResult(state=ScreenState.UNKNOWN)

        # 统计
        self._stats = {
            "total_captures": 0,
            "api_calls": 0,
            "api_errors": 0,
            "cache_hits": 0,
            "fallback_uses": 0,
            "session_start": 0.0,
        }

        logger.info(f"ScreenMonitor 初始化: interval={interval}s, "
                     f"change_threshold={change_threshold}, "
                     f"api={'enabled' if enable_api else 'disabled'}, "
                     f"whitelist={len(self.whitelist)} apps")

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """启动后台监控线程。"""
        if self._running:
            logger.warning("ScreenMonitor 已在运行中")
            return True

        self._running = True
        self._stats["session_start"] = time.time()
        self._thread = threading.Thread(
            target=self._run_loop, name="ScreenMonitor", daemon=True
        )
        self._thread.start()
        logger.info("ScreenMonitor 线程已启动")
        return True

    def stop(self) -> None:
        """停止监控并释放资源。"""
        logger.info("ScreenMonitor 正在停止...")
        self._running = False

        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None

        self._capturer.close()
        logger.info("ScreenMonitor 已停止")

    def get_last_state(self) -> ScreenResult:
        """获取最近的屏幕状态 (线程安全)。"""
        with self._lock:
            return self._last_result

    def get_stats(self) -> Dict[str, Any]:
        """获取运行统计。"""
        with self._lock:
            s = dict(self._stats)
        if self._api_client:
            s["api"] = self._api_client.stats
        s["session_elapsed"] = (
            time.time() - s["session_start"] if s["session_start"] > 0 else 0
        )
        return s

    def reset_stats(self) -> None:
        """重置所有屏幕监控统计数据 (开始新会话时调用)。"""
        with self._lock:
            self._stats = {
                "total_captures": 0,
                "api_calls": 0,
                "cache_hits": 0,
                "fallback_uses": 0,
                "slacking_count": 0,
                "session_start": time.time(),
            }

    def add_to_whitelist(self, app_name: str) -> None:
        """添加应用到白名单。"""
        app_lower = app_name.lower().strip()
        if app_lower and app_lower not in self.whitelist:
            self.whitelist.append(app_lower)
            logger.info(f"已添加到白名单: {app_name}")

    def remove_from_whitelist(self, app_name: str) -> None:
        """从白名单移除应用。"""
        app_lower = app_name.lower().strip()
        if app_lower in self.whitelist:
            self.whitelist.remove(app_lower)
            logger.info(f"已从白名单移除: {app_name}")

    def force_analyze_now(self) -> ScreenResult:
        """立即执行一次截图和分析 (同步调用, 阻塞)。"""
        img = self._capturer.capture()
        changed = self._image_changed(img)

        if not changed:
            # 使用缓存
            with self._lock:
                result = self._last_successful_result
                result.image_changed = False
                result.from_cache = True
            return result

        result = self._analyze(img)
        with self._lock:
            self._last_result = result
            if not result.from_fallback:
                self._last_successful_result = result
        return result

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def last_state_str(self) -> str:
        return self._last_result.state.value

    # ------------------------------------------------------------------
    # 内部: 主循环
    # ------------------------------------------------------------------

    def _run_loop(self) -> None:
        """后台主循环: 定时截图 → 变化检测 → API 分析。"""
        while self._running:
            loop_start = time.time()

            try:
                # 1. 截图
                img = self._capturer.capture()
                with self._lock:
                    self._stats["total_captures"] += 1

                # 2. 图像变化检测
                changed = self._image_changed(img)

                if not changed:
                    # 图像未显著变化 → 使用上次结果
                    with self._lock:
                        self._stats["cache_hits"] += 1
                        cached = self._last_successful_result
                        cached.image_changed = False
                        cached.from_cache = True
                        self._last_result = cached

                    if self._callback:
                        self._callback(self._last_result)
                else:
                    # 3. 图像变化 → 调用 API (或降级)
                    result = self._analyze(img)

                    with self._lock:
                        self._last_result = result
                        if not result.from_fallback:
                            self._last_successful_result = result

                    # 4. 回调
                    if self._callback:
                        self._callback(result)

            except Exception as e:
                logger.error(f"监控循环异常: {e}", exc_info=True)

            # 等待到下一个间隔
            elapsed = time.time() - loop_start
            sleep_time = max(0, self.interval - elapsed)
            # 分段 sleep 以便快速响应 stop
            while sleep_time > 0 and self._running:
                time.sleep(min(0.5, sleep_time))
                sleep_time -= 0.5

    # ------------------------------------------------------------------
    # 内部: 分析逻辑
    # ------------------------------------------------------------------

    def _analyze(self, image: Image.Image) -> ScreenResult:
        """
        分析屏幕截图: API 调用 → 白名单校验 → 降级处理。
        """
        result = ScreenResult(
            timestamp=time.time(),
            image_changed=True,
        )

        if self.enable_api and self._api_client:
            # 调用 minimax API
            with self._lock:
                self._stats["api_calls"] += 1

            api_result = self._api_client.analyze(image)

            if api_result.from_fallback:
                # API 失败 → 降级
                with self._lock:
                    self._stats["api_errors"] += 1
                    self._stats["fallback_uses"] += 1
                return self._fallback_analyze(image, api_result.reason)

            # API 成功 → 白名单校验
            result.state = api_result.state
            result.confidence = api_result.confidence
            result.app = api_result.app
            result.reason = api_result.reason
            result.raw_response = api_result.raw_response

            # 白名单校正: 如果在白名单中但被判定为摸鱼，降级为一般浏览
            result = self._apply_whitelist(result)

        else:
            # API 禁用 → 仅本地判断
            result = self._fallback_analyze(image, "API 已禁用")
            result.from_fallback = True

        return result

    def _apply_whitelist(self, result: ScreenResult) -> ScreenResult:
        """
        白名单校正: 如果识别出的应用在白名单中，但被 API 判定为摸鱼，
        则降级为"一般浏览"（给予用户信任）。
        """
        if result.state != ScreenState.SLACKING:
            return result

        app_lower = result.app.lower().strip()
        for wl in self.whitelist:
            if wl in app_lower or app_lower in wl:
                logger.info(
                    f"白名单校正: '{result.app}' 在白名单中, "
                    f"摸鱼→一般浏览 (原置信度={result.confidence:.2f})"
                )
                result.state = ScreenState.CASUAL_BROWSE
                result.confidence = max(result.confidence, 0.6)
                result.reason += " [白名单校正]"
                return result

        return result

    def _fallback_analyze(
        self, image: Image.Image, reason: str
    ) -> ScreenResult:
        """
        降级分析: API 不可用时使用简单规则。

        降级策略:
          1. 检测是否黑屏/锁屏 (图像平均亮度)
          2. 使用上次成功的 API 结果 (带过期标记)
          3. 如果都不可用, 返回 UNKNOWN
        """
        result = ScreenResult(
            timestamp=time.time(),
            state=ScreenState.UNKNOWN,
            confidence=0.5,
            from_fallback=True,
            reason=f"降级: {reason}",
        )

        # 简单亮度检测: 判断是否黑屏/锁屏
        gray = image.convert("L")
        pixels = np.array(gray)
        mean_brightness = float(np.mean(pixels))

        if mean_brightness < 15:
            # 几乎全黑 → 锁屏/休眠
            result.state = ScreenState.AWAY
            result.confidence = 0.85
            result.app = "锁屏/黑屏"
            result.reason = "屏幕几乎全黑 (降级判断)"
            return result

        if mean_brightness < 30 and np.std(pixels) < 15:
            # 暗且均匀 → 可能是锁屏
            result.state = ScreenState.AWAY
            result.confidence = 0.7
            result.app = "可能锁屏"
            result.reason = "屏幕较暗且均匀 (降级判断)"
            return result

        # 使用上次成功的缓存结果
        with self._lock:
            cached = self._last_successful_result

        if cached.state != ScreenState.UNKNOWN:
            result.state = cached.state
            result.confidence = max(cached.confidence - 0.15, 0.3)
            result.app = cached.app
            result.reason = f"使用缓存 ({cached.state.value}), {reason}"
            result.from_cache = True
        else:
            result.reason = f"无可用缓存, {reason}"

        return result

    # ------------------------------------------------------------------
    # 内部: 图像变化检测 (感知哈希)
    # ------------------------------------------------------------------

    def _image_changed(self, image: Image.Image) -> bool:
        """
        基于感知哈希 (pHash) 判断图像是否显著变化。

        原理:
          - pHash 对缩放、亮度微调不敏感
          - 汉明距离衡量两张图的相似度
          - similarity = 1 - hamming_dist / hash_bits
          - similarity < threshold → 图像有变化

        返回 True 表示需要重新分析。
        """
        try:
            import imagehash

            phash = imagehash.phash(image)
            phash_str = str(phash)

            if self._last_phash is None:
                # 第一帧
                self._last_phash = phash
                self._last_image_hash = phash_str
                return True

            # 汉明距离 (0-64, 因为是 64-bit hash)
            hamming_dist = self._last_phash - phash
            similarity = 1.0 - (hamming_dist / 64.0)

            if similarity < self.change_threshold:
                # 图像有显著变化
                self._last_phash = phash
                self._last_image_hash = phash_str
                logger.debug(
                    f"图像变化检测: hamming={hamming_dist}/64, "
                    f"similarity={similarity:.3f} < {self.change_threshold} → 需重新分析"
                )
                return True
            else:
                logger.debug(
                    f"图像未显著变化: hamming={hamming_dist}/64, "
                    f"similarity={similarity:.3f} >= {self.change_threshold} → 跳过"
                )
                return False

        except ImportError:
            # 没有 imagehash 库 → 使用 MD5 简单替代
            logger.warning("未安装 imagehash，使用 MD5 进行变化检测 (效果较差)")
            return self._image_changed_md5(image)

    def _image_changed_md5(self, image: Image.Image) -> bool:
        """MD5 变化检测 (imagehash 不可用时的降级方案)。"""
        img_bytes = image.tobytes()
        md5 = hashlib.md5(img_bytes).hexdigest()

        if self._last_image_hash == "":
            self._last_image_hash = md5
            return True

        if md5 != self._last_image_hash:
            self._last_image_hash = md5
            return True

        return False

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
# PyQt5 集成版本: ScreenMonitorQt
# ---------------------------------------------------------------------------

class ScreenMonitorQt(ScreenMonitor):
    """
    ScreenMonitor 的 PyQt5 集成版本。
    使用 Signal 模式，适合嵌入 PyQt5 GUI。

    用法:
        monitor = ScreenMonitorQt(api_key="YOUR_KEY")
        monitor.screen_signal.connect(self.on_screen_update)
        monitor.start()

    信号:
        screen_signal(dict)  — 每次分析结果
        stats_signal(dict)   — 统计更新
        error_signal(str)    — 错误消息
    """

    def __init__(self, *args, **kwargs):
        try:
            from PyQt5.QtCore import QObject, pyqtSignal
        except ImportError:
            raise ImportError(
                "ScreenMonitorQt 需要 PyQt5。请执行: pip install PyQt5\n"
                "如果不需要 GUI 集成，请使用 ScreenMonitor 基类。"
            )

        self._signal_holder = _ScreenSignalHolder()
        self.screen_signal = self._signal_holder.screen_signal
        self.stats_signal = self._signal_holder.stats_signal
        self.error_signal = self._signal_holder.error_signal

        kwargs["callback"] = self._qt_callback
        super().__init__(*args, **kwargs)

    def _qt_callback(self, result: ScreenResult) -> None:
        """将 ScreenResult 转为 dict，通过信号发射。"""
        try:
            self.screen_signal.emit(result.to_dict())
        except Exception as e:
            logger.error(f"信号发射失败: {e}")

    def emit_stats(self) -> None:
        """主动发射统计信号。"""
        try:
            self.stats_signal.emit(self.get_stats())
        except Exception as e:
            logger.error(f"统计信号发射失败: {e}")


class _ScreenSignalHolder:
    """ScreenMonitor 的 PyQt5 信号容器。"""
    def __init__(self):
        from PyQt5.QtCore import QObject, pyqtSignal

        class _Holder(QObject):
            screen_signal = pyqtSignal(dict)   # ScreenResult.to_dict()
            stats_signal = pyqtSignal(dict)    # get_stats()
            error_signal = pyqtSignal(str)     # 错误消息

        self._holder = _Holder()

    @property
    def screen_signal(self):
        return self._holder.screen_signal

    @property
    def stats_signal(self):
        return self._holder.stats_signal

    @property
    def error_signal(self):
        return self._holder.error_signal


# ---------------------------------------------------------------------------
# 命令行入口 (独立测试)
# ---------------------------------------------------------------------------

def demo(api_key="", interval=5.0, enable_api=True):
    """命令行演示: 定时截图并打印分析结果。"""
    import os
    import sys
    import signal as _signal

    # 优先级: 参数 > 环境变量 > apikey.txt 文件 > 空
    if not api_key:
        api_key = os.environ.get("MINIMAX_API_KEY", "")

    if not api_key:
        # 尝试从项目目录下的 apikey.txt 读取
        key_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "apikey.txt")
        if os.path.exists(key_file):
            with open(key_file, "r") as f:
                api_key = f.read().strip()
            if api_key:
                print(f"[从 apikey.txt 读取 Key]")

    if not api_key:
        print("=" * 60)
        print("⚠️  未配置 minimax API Key")
        print("=" * 60)
        print()
        print("请通过以下方式之一配置 API Key:")
        print("  1. 命令行: python screen_monitor.py --demo --api-key YOUR_KEY")
        print("  2. 环境变量: setx MINIMAX_API_KEY YOUR_KEY")
        print("  3. 代码传入: ScreenMonitor(api_key='your_key')")
        print()
        print("当前将使用降级模式运行 (仅本地亮度检测)...")
        print()
        enable_api = False

    shutdown = threading.Event()
    _signal.signal(_signal.SIGINT, lambda s, f: shutdown.set())

    def print_callback(result: ScreenResult):
        """打印分析结果"""
        d = result.to_dict()
        icon_map = {
            "专注工作": "🟢", "一般浏览": "🟡",
            "摸鱼": "🔴", "离开": "⚫", "未知": "⚪",
        }
        icon = icon_map.get(d["state"], "❓")
        flags = []
        if d["from_cache"]:
            flags.append("缓存")
        if d["from_fallback"]:
            flags.append("降级")
        if not d["image_changed"]:
            flags.append("无变化")

        flag_str = f" [{', '.join(flags)}]" if flags else ""
        latency_str = f" ({d.get('latency', 0):.1f}s)" if d.get('latency', 0) > 0 else ""
        print(
            f"{icon} {d['state']:6s} | "
            f"应用: {d['app']:20s} | "
            f"置信度: {d['confidence']:.2f}{latency_str} | "
            f"{d['reason'][:40]}{flag_str}"
        )

    print("=" * 60)
    print("FocusFlow Lite — 屏幕内容监控 Demo")
    print("=" * 60)
    print(f"API: {'启用' if enable_api else '禁用 (降级模式)'}")
    print("按 Ctrl+C 退出\n")

    monitor = ScreenMonitor(
        api_key=api_key,
        interval=interval,
        enable_api=enable_api,
        callback=print_callback,
    )

    with monitor:
        tick = 0
        while not shutdown.is_set() and monitor.is_running:
            time.sleep(1)
            tick += 1

    print("\n\n监控结束。统计信息:")
    stats = monitor.get_stats()
    print(f"  总截图: {stats['total_captures']}")
    print(f"  API 调用: {stats['api_calls']}")
    print(f"  API 错误: {stats['api_errors']}")
    print(f"  缓存命中: {stats['cache_hits']}")
    print(f"  降级使用: {stats['fallback_uses']}")
    print(f"  运行时长: {stats.get('session_elapsed', 0):.0f}s")
    if "api" in stats:
        print(f"  API 平均延迟: {stats['api']['avg_latency']:.2f}s")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="FocusFlow Lite 屏幕内容监控")
    parser.add_argument("--demo", action="store_true", default=True,
                        help="运行命令行演示")
    parser.add_argument("--api-key", type=str, default="",
                        help="minimax API Key (或设置环境变量 MINIMAX_API_KEY)")
    parser.add_argument("--interval", type=float, default=10.0,
                        help="截图间隔 (秒, 默认 10)")
    parser.add_argument("--no-api", action="store_true",
                        help="禁用 API, 仅使用降级模式")
    args = parser.parse_args()

    demo(
        api_key=args.api_key,
        interval=args.interval,
        enable_api=not args.no_api,
    )
