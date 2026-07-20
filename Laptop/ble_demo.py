#!/usr/bin/env python3
"""
FocusFlow Lite — BLE 蓝牙连接演示 & 测试脚本
============================================

这个脚本让你在没有 UNO Q 硬件的情况下，完整验证蓝牙通信协议。
直接运行就能看到效果。

用法:
    python ble_demo.py              # 模拟器演示 (无需硬件)
    python ble_demo.py --real       # 真实 BLE 连接 (需要 UNO Q 在身边)

作者: D 同学 (FocusFlow 小组)
日期: 2026-07-20
"""

import sys
import os
import time
import argparse

# 添加项目路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def demo_simulator():
    """
    ============================================================
    模拟器演示 — 不需要任何硬件，在你的笔记本上直接跑
    ============================================================

    这个演示模拟了完整的通信流程:
      笔记本                                    UNO Q (模拟)
      ──────                                   ────────────
      ① 发 sensor_data (每秒)      ──────►    收到，打印
      ② 发 distraction_event (走神) ──────►   收到，模拟反馈
      ③ 发 state_sync (状态切换)   ──────►    收到，确认
    """
    from ble_communication import BLESimulator, SystemState, EventType

    print("""
╔══════════════════════════════════════════════════════════════╗
║        FocusFlow Lite — BLE 蓝牙通信模拟演示                ║
║                                                              ║
║  模拟笔记本 ←→ UNO Q 之间的完整蓝牙数据收发                   ║
║  无需任何硬件，所有 UNO Q 响应均为模拟                         ║
╚══════════════════════════════════════════════════════════════╝
""")

    # ================================================================
    # 第 1 步: 创建 BLE 模拟器 (相当于"假装连上了 UNO Q")
    # ================================================================
    print("【第 1 步】启动 BLE 连接...")
    sim = BLESimulator(verbose=True, simulate_responses=True)
    sim.start()
    # 此时模拟器内部状态 = "已连接"，就像真的连上了 UNO Q 一样

    # ================================================================
    # 第 2 步: 发送传感器数据 (这是真正运行时每秒都会发的)
    # ================================================================
    print("\n【第 2 步】模拟发送 3 次传感器数据 (实际运行中是每秒 1 次)...\n")

    for i in range(3):
        print(f"  ── 第 {i+1} 次发送 ──")
        sim.send_sensor_data(
            # ---- 眼动特征 (5维) ----
            # 格式: (yaw偏航角, pitch俯仰角, 是否专注0/1, 状态持续时间秒, 置信度)
            eye_features=(5.2, -3.1, 1, 12.5, 0.92),
            # ---- 屏幕特征 (3维) ----
            # 格式: (状态编码, 置信度, 应用类别)
            #  状态编码: 0=离开, 0.3=摸鱼, 0.6=一般浏览, 1.0=专注工作
            #  应用类别: -1=未知, 0=离开, 1=摸鱼, 2=一般浏览, 3=专注工作
            screen_features=(1.0, 0.88, 3),
            eye_state="专注",
            screen_state="专注工作",
            screen_app="VSCode",
        )
        time.sleep(0.5)

    # ================================================================
    # 第 3 步: 模拟走神事件 (检测到摸鱼时即时发送)
    # ================================================================
    print("\n【第 3 步】模拟走神事件 — 检测到用户在刷 B 站...\n")

    sim.send_distraction_event(
        event_type=EventType.SLACKING,   # 摸鱼事件
        severity="high",                  # 高严重度
        source="screen",                  # 由屏幕监控检测到
        details={
            "app": "哔哩哔哩",
            "reason": "屏幕监控 API 识别到 B 站视频播放页",
            "duration_sec": 15.0,
        },
    )
    time.sleep(0.5)

    # ================================================================
    # 第 4 步: 模拟状态切换 (番茄钟到了 → 休息)
    # ================================================================
    print("\n【第 4 步】模拟番茄钟结束 → 进入休息状态...\n")

    sim.set_state(SystemState.RESTING)
    time.sleep(1)
    sim.set_state(SystemState.MONITORING)

    # ================================================================
    # 第 5 步: 停止
    # ================================================================
    print("\n【第 5 步】停止连接...")
    sim.stop()

    print("""
╔══════════════════════════════════════════════════════════════╗
║  模拟演示结束 ✅                                              ║
║                                                              ║
║  上面展示的就是笔记本实际运行时和 UNO Q 之间的通信内容。       ║
║  真实连接时，上面打印的这些 JSON 会通过蓝牙发送出去，          ║
║  UNO Q 收到后会做融合推理 + 触发振动/OLED 更新。               ║
╚══════════════════════════════════════════════════════════════╝
""")


def demo_real_ble():
    """
    ============================================================
    真实 BLE 连接 — 需要 UNO Q 在身边且已启动 BLE Server
    ============================================================

    前置条件:
      1. UNO Q 已开机，Linux 侧运行了 ble_server.py
      2. 笔记本蓝牙已打开
      3. pip install bleak (BLE 库)
    """
    try:
        from ble_communication import BLEClient, SystemState
    except ImportError:
        print("❌ 请先安装 bleak: pip install bleak")
        return

    print("""
╔══════════════════════════════════════════════════════════════╗
║     FocusFlow Lite — 真实 BLE 连接                          ║
║     正在扫描 UNO-Q-FF01...                                   ║
╚══════════════════════════════════════════════════════════════╝
""")

    client = BLEClient(device_name="UNO-Q-FF01")

    # 注册回调: 当收到 UNO Q 发来的反馈指令时
    def on_feedback(cmd):
        print(f"\n📩 收到 UNO Q 反馈: {cmd.get('cmd_type')} → {cmd.get('target')}")
        if cmd.get('cmd_type') == 'vibrate_and_display':
            print(f"   振动模式: {cmd['params']['pattern']}")
            print(f"   OLED 显示: {cmd['display']['line1']}")

    client.on_feedback_cmd = on_feedback

    def on_connection(connected, info):
        if connected:
            print(f"✅ 已连接到 {info.get('device_name', 'UNO Q')}")
        else:
            print(f"❌ 连接断开: {info.get('reason', 'unknown')}")

    client.on_connection_change = on_connection

    # 开始连接
    client.start()

    # 等待连接
    print("等待连接... (10s 超时)")
    waited = 0
    while not client.is_connected and waited < 10:
        time.sleep(0.5)
        waited += 0.5

    if not client.is_connected:
        print("\n❌ 连接失败。请检查:")
        print("   1. UNO Q 是否已开机?")
        print("   2. UNO Q Linux 端是否运行了 ble_server.py?")
        print("   3. 笔记本蓝牙是否已打开?")
        print("   4. 设备名是否为 'UNO-Q-FF01'?")
        return

    # 发送测试数据
    print("\n开始发送测试数据...")
    for i in range(5):
        client.send_sensor_data(
            eye_features=(5.2, -3.1, 1, 12.5, 0.92),
            screen_features=(1.0, 0.88, 3),
            eye_state="专注",
            screen_state="专注工作",
            screen_app="VSCode",
        )
        print(f"  已发送 {i+1}/5")
        time.sleep(1)

    client.stop()
    print("\n测试完成 ✅")


# ═══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="FocusFlow Lite BLE 连接演示")
    parser.add_argument("--real", action="store_true",
                        help="使用真实 BLE 连接 (需要 UNO Q)")
    args = parser.parse_args()

    if args.real:
        demo_real_ble()
    else:
        demo_simulator()
