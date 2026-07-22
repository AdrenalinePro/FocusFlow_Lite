"""Sandbox-bypassing debug helper for FocusFlow BLE.

Runs OUTSIDE the Codex CLI sandbox (the operator pre-approves this script
via ``/approvals``).  It exposes a small CLI that lets the agent do
everything it needs to diagnose BLE startup issues without going through
the sandbox on every command::

    python3 dev_session.py test [duration]
    python3 dev_session.py logs
    python3 dev_session.py bluez
    python3 dev_session.py bluez-objects
    python3 dev_session.py app
    python3 dev_session.py rfkill
    python3 dev_session.py capabilities
    python3 dev_session.py cleanup
    python3 dev_session.py env

Each subcommand prints a header so the output is easy to scan when the
agent pastes it back into the chat.  Errors are not swallowed — the
agent wants to see every ``Permission denied`` and ``NotReady``.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path("/home/arduino/focusble")
LOG_DIR = ROOT / "linux" / ".dev-logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LAST_TEST_LOG = LOG_DIR / "last_test.log"


def header(title: str) -> None:
    print("\n" + "=" * 72)
    print(f"  {title}")
    print("=" * 72, flush=True)


def run(label: str, *args: str, timeout: float = 60.0,
        check: bool = False) -> subprocess.CompletedProcess:
    """Run an external command, print stdout/stderr verbatim, return result.

    ``label`` is just for the header.  We never raise on non-zero exit
    because the agent wants to see e.g. ``bluetoothctl show`` output
    even when some property queries fail.
    """

    header(f"{label}: {' '.join(shlex.quote(a) for a in args)}")
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True, text=True,
            timeout=timeout, cwd=str(ROOT),
        )
    except FileNotFoundError as exc:
        print(f"command not found: {exc}")
        return subprocess.CompletedProcess(args, 127, "", str(exc))
    except subprocess.TimeoutExpired:
        print(f"!!! timeout after {timeout}s")
        return subprocess.CompletedProcess(args, 124, "", "timeout")
    if proc.stdout:
        print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    if proc.stderr:
        sys.stderr.write(proc.stderr)
        if not proc.stderr.endswith("\n"):
            sys.stderr.write("\n")
    print(f"--- rc={proc.returncode}", flush=True)
    return proc


# ---- subcommands -----------------------------------------------------------


def cmd_test(args: argparse.Namespace) -> int:
    """Run linux_ble_test.py and capture full output to last_test.log."""

    duration = args.duration
    cmd = [
        sys.executable,
        str(ROOT / "linux" / "linux_ble_test.py"),
        "--duration", str(duration),
        "--connect-timeout", "10",
    ]
    header(f"TEST ({duration}s): {' '.join(shlex.quote(c) for c in cmd)}")
    started = time.time()
    try:
        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            timeout=duration + 30, cwd=str(ROOT),
        )
    except subprocess.TimeoutExpired:
        print(f"!!! test hung, killing after {duration + 30}s")
        return 124
    LAST_TEST_LOG.write_text(
        f"# {' '.join(cmd)}\n# started={started} duration={duration}\n"
        + "----- STDOUT -----\n" + proc.stdout
        + "\n----- STDERR -----\n" + proc.stderr
        + f"\n----- rc={proc.returncode} -----\n"
    )
    print(proc.stdout, end="" if proc.stdout.endswith("\n") else "\n")
    sys.stderr.write(proc.stderr)
    if not proc.stderr.endswith("\n"):
        sys.stderr.write("\n")
    print(f"--- rc={proc.returncode} (saved to {LAST_TEST_LOG})", flush=True)
    return proc.returncode


def cmd_logs(_: argparse.Namespace) -> int:
    """Pull every relevant log on UNO Q."""

    if LAST_TEST_LOG.exists():
        header(f"LAST TEST OUTPUT ({LAST_TEST_LOG})")
        print(LAST_TEST_LOG.read_text())

    if shutil.which("journalctl"):
        run("JOURNAL", "journalctl", "-u", "bluetooth",
            "-n", "80", "--no-pager", "--no-full")
        run("JOURNAL", "journalctl", "-n", "40", "--no-pager")
    else:
        print("(no journalctl; skipping)")

    if shutil.which("dmesg"):
        run("DMESG", "bash", "-c", "dmesg 2>&1 | tail -60 || true")
    else:
        print("(no dmesg; skipping)")

    if shutil.which("bluetoothctl"):
        run("BT VER", "bluetoothctl", "--version")
    return 0


def cmd_bluez(_: argparse.Namespace) -> int:
    """Adapter state, controller mode, register state — all in one shot."""

    if not shutil.which("bluetoothctl"):
        print("(no bluetoothctl)")
        return 1
    run("BT SHOW", "bluetoothctl", "show")
    run("BT LIST", "bluetoothctl", "list")
    run("BT ADAPTERS", "bash", "-c",
        "for a in /sys/class/bluetooth/hci*; do "
        "echo \"$a: $(cat $a/type 2>/dev/null) "
        "addr=$(cat $a/address 2>/dev/null) "
        "up=$(cat $a/up 2>/dev/null)\"; done")
    run("MAIN CONF", "bash", "-c",
        "grep -E 'ControllerMode|Enable|MinLEMTU' "
        "/etc/bluetooth/main.conf 2>/dev/null || echo '(no main.conf)')")
    return 0


def cmd_bluez_objects(_: argparse.Namespace) -> int:
    """Enumerate every BlueZ object via ObjectManager.GetManagedObjects."""

    if not shutil.which("dbus-send"):
        print("(no dbus-send)")
        return 1
    # Two-stage: introspect first so we know which adapter paths exist
    run("INTROSPECT /", "dbus-send", "--system",
        "--dest=org.bluez", "--print-reply",
        "/", "org.freedesktop.DBus.ObjectManager.GetManagedObjects")
    return 0


def cmd_app(_: argparse.Namespace) -> int:
    """List registered GATT applications by name."""

    out = subprocess.run(
        ["dbus-send", "--system", "--dest=org.bluez", "--print-reply",
         "/", "org.freedesktop.DBus.ObjectManager.GetManagedObjects"],
        capture_output=True, text=True, timeout=15,
    )
    if out.returncode != 0:
        run("DBUS", "dbus-send", "--system",
            "--dest=org.bluez", "--print-reply",
            "/", "org.freedesktop.DBus.ObjectManager.GetManagedObjects")
        return out.returncode
    # Crude filter for our app and any FocusFlow UUID references
    interesting = []
    for line in out.stdout.splitlines():
        if any(token in line for token in (
            "com/focusflow",
            "19B10000-E8F2-537E-4F6C-D104768A1214",
            "GattService1",
        )):
            interesting.append(line)
    if interesting:
        header("REGISTERED GATT APPS (filtered)")
        print("\n".join(interesting))
    else:
        print("(no FocusFlow GATT service found in BlueZ tree)")
    return 0


def cmd_rfkill(_: argparse.Namespace) -> int:
    run("RFKILL", "rfkill", "list")
    return 0


def cmd_capabilities(_: argparse.Namespace) -> int:
    """Show the calling process capabilities + D-Bus socket perms."""

    run("ID", "id")
    run("CAPS", "bash", "-c",
        "grep -E '^Cap' /proc/self/status 2>/dev/null || true")
    run("PYTHON", "bash", "-c",
        f"{shlex.quote(sys.executable)} -V")
    run("DBUS SOCK", "bash", "-c",
        "ls -la /var/run/dbus/ 2>&1; "
        "stat -c '%a %U %G' /var/run/dbus/system_bus_socket 2>&1 || true")
    run("GROUPS", "bash", "-c",
        "cat /proc/$$/status | grep -E '^Groups' 2>&1 || true")
    return 0


def cmd_cleanup(_: argparse.Namespace) -> int:
    """Reset the BLE adapter state: power-cycle + remove orphan app if any.

    Use with care — powers the adapter down then up.  Any connected
    devices will be dropped.
    """

    if not shutil.which("bluetoothctl"):
        print("(no bluetoothctl)")
        return 1
    # Try to find and remove our stuck registration first via gatttool-style
    # introspection.  If BlueZ holds it after a crash, only a power cycle
    # clears it.
    run("BT POWER OFF", "bluetoothctl", "power", "off")
    time.sleep(1.0)
    run("BT POWER ON", "bluetoothctl", "power", "on")
    run("BT SHOW", "bluetoothctl", "show")
    return 0


def cmd_env(_: argparse.Namespace) -> int:
    """Show environment + Python site-packages relevant to dbus-fast."""

    run("ENV", "bash", "-c",
        "env | grep -E '^(PATH|HOME|USER|XDG|DISPLAY)=' | sort")
    run("PYTHONPATH", "bash", "-c",
        f"{shlex.quote(sys.executable)} -c 'import sys, dbus_fast; "
        "print(\"python:\", sys.version.split()[0]); "
        "print(\"exec:\", sys.executable); "
        "print(\"dbus_fast:\", dbus_fast.__file__)'")
    run("SITE", "bash", "-c",
        f"{shlex.quote(sys.executable)} -m site")
    return 0


# ---- CLI ------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    sub = p.add_subparsers(dest="cmd", required=True)
    sp = sub.add_parser("test", help="run linux_ble_test.py")
    sp.add_argument("--duration", type=float, default=30.0)
    sp.set_defaults(func=cmd_test)
    sub.add_parser("logs", help="last_test.log + journalctl + dmesg").set_defaults(func=cmd_logs)
    sub.add_parser("bluez", help="bluetoothctl + adapter paths + main.conf").set_defaults(func=cmd_bluez)
    sub.add_parser("bluez-objects", help="raw BlueZ GetManagedObjects").set_defaults(func=cmd_bluez_objects)
    sub.add_parser("app", help="filter BlueZ tree for FocusFlow service").set_defaults(func=cmd_app)
    sub.add_parser("rfkill", help="rfkill list").set_defaults(func=cmd_rfkill)
    sub.add_parser("capabilities", help="uid, groups, caps, dbus sock perms").set_defaults(func=cmd_capabilities)
    sub.add_parser("cleanup", help="power-cycle adapter (drops connections)").set_defaults(func=cmd_cleanup)
    sub.add_parser("env", help="env + dbus_fast location").set_defaults(func=cmd_env)
    return p


def main(argv: list[str]) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    print(f"dev_session.py pid={os.getpid()} uid={os.getuid()} euid={os.geteuid()}")
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
