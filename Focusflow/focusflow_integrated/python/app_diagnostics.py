"""Runtime diagnostics for the FocusFlow integration.

Prints a snapshot of every subsystem that has to come up for the app
to function: BLE adapter, dbus socket availability, Bridge socket, MCU
heartbeat, etc.  This script is meant to be run interactively from
inside the running container (``docker exec``).
"""
from __future__ import annotations

import os
import socket
import sys
from pathlib import Path

def check(label, ok, detail=""):
    mark = "✅" if ok else "❌"
    print(f"  {mark} {label:<32} {detail}")

def main():
    print("=" * 70)
    print("  FocusFlow runtime diagnostics")
    print("=" * 70)

    print("\n[1] Container environment")
    print(f"  APP_HOME={os.environ.get('APP_HOME', '?')}")
    print(f"  USER={os.environ.get('USER', '?')}")
    print(f"  Effective UID={os.geteuid()}  GID={os.getegid()}")

    print("\n[2] Bridge / Router IPC socket")
    sock_path = "/run/arduino-router.sock"
    check("arduino-router.sock", os.path.exists(sock_path),
          f"path={sock_path}")

    print("\n[3] System D-Bus availability")
    for p in ("/run/dbus/system_bus_socket", "/var/run/dbus/system_bus_socket"):
        check(p, os.path.exists(p), f"mode={oct(os.stat(p).st_mode) if os.path.exists(p) else '-'}")

    print("\n[4] Bluetooth adapter")
    for p in ("/sys/class/bluetooth/hci0", "/sys/class/rfkill/rfkill0"):
        check(p, os.path.exists(p))

    if os.path.exists("/sys/class/bluetooth/hci0"):
        try:
            with open("/sys/class/bluetooth/hci0/address") as f:
                addr = f.read().strip()
            check("hci0 address", bool(addr), addr)
        except Exception as exc:
            check("hci0 address", False, str(exc))

    print("\n[5] dbus_fast / dbus_next availability")
    try:
        import dbus_fast
        check("dbus_fast", True, dbus_fast.__file__)
    except Exception as exc:
        check("dbus_fast", False, str(exc))
    try:
        import dbus_next
        check("dbus_next", True, dbus_next.__file__)
    except Exception as exc:
        check("dbus_next", False, str(exc))

    print("\n[6] FocusFlow Python modules")
    for mod in ("linux.linux_ble_server", "ble_server",
                "focusflow_server", "tft_bridge",
                "wristband_controller", "reconnect_supervisor"):
        try:
            __import__(mod)
            check(f"import {mod}", True)
        except Exception as exc:
            check(f"import {mod}", False, str(exc))

    print("\n[7] Arduino App framework")
    try:
        from arduino.app_utils import App, Bridge
        check("arduino.app_utils", True)
        # Send a heartbeat ping
        Bridge.notify("diagnostics_ping", "focusflow")
        check("Bridge.notify", True, "ok")
    except Exception as exc:
        check("arduino.app_utils", False, str(exc))

if __name__ == "__main__":
    main()
