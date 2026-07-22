#!/usr/bin/env python3
"""Apply the FocusFlow patches to dbus_fast.

This rewrites dbus_fast's message_bus.py to:
1. Replace the default GetManagedObjects handler so it INCLUDES the
   queried root path (the upstream handler filters it out, which
   breaks BlueZ's RegisterApplication).
2. Use the interface's ``get_properties()`` method to read property
   values (so hand-written GATT classes expose state via their own
   ``get_properties()`` instead of the @dbus_property decorator).

Also moves the Cython ``.so`` files aside so Python uses the patched
``.py`` source instead of the precompiled extension.

Works against dbus_fast 1.83 - 1.95.x.  Idempotent — a re-run is a
no-op once ``.focusflow-patched`` exists.
"""

from __future__ import annotations

import os
import pathlib
import re
import shutil
import sys


def _find_dbus_fast_dir() -> pathlib.Path:
    """Locate the dbus_fast install directory.

    Looks first inside the active venv (the Arduino App container
    case), then under the user site-packages (host developer case).
    """

    import sys
    for sp in sys.path:
        if not sp:
            continue
        candidate = pathlib.Path(sp) / "dbus_fast"
        if (candidate / "message_bus.py").is_file():
            return candidate
    raise SystemExit(
        "Could not locate dbus_fast install.  Tried: " + repr(sys.path)
    )


DBUS_FAST_DIR = _find_dbus_fast_dir()
MARKER = DBUS_FAST_DIR / ".focusflow-patched"
MESSAGE_BUS = DBUS_FAST_DIR / "message_bus.py"
SITE_PACKAGES = DBUS_FAST_DIR.parent


PATCHED_HANDLER = '''    def _focusflow_get_managed_objects(
        self, msg, send_reply,
    ):
        """FocusFlow-patched GetManagedObjects.

        1. Includes the queried root path (the upstream handler excludes
           it, breaking BlueZ's RegisterApplication).
        2. Reads property values via the interface's ``get_properties()``
           method (when available) so our hand-written GATT classes
           expose their state.
        """
        from dbus_fast import Message as _Msg

        root = msg.path
        nodes = []
        if root == "/":
            nodes = list(self._path_exports.keys())
        else:
            if root in self._path_exports:
                nodes.append(root)
            for node in self._path_exports:
                if node != root and node.startswith(root + "/"):
                    nodes.append(node)

        def collect(interface):
            gp = getattr(interface, "get_properties", None)
            if callable(gp):
                try:
                    value = gp()
                except Exception:
                    value = {}
                return value if value is not None else {}
            captured = {}

            def cb(_intf, values, _unknown, err):
                if err is None and values:
                    captured.update(values)

            ServiceInterface._get_all_property_values(interface, cb, None)
            return captured

        result = {}
        for node in nodes:
            result[node] = {}
            # dbus_fast >= 1.95 stores ``_path_exports[path]`` as a list
            # of interfaces (older versions used a dict).
            entries = self._path_exports[node]
            if isinstance(entries, dict):
                iterable = entries.values()
            else:
                iterable = entries
            for interface in iterable:
                result[node][interface.name] = collect(interface)

        send_reply(_Msg.new_method_return(msg, "a{oa{sa{sv}}}", [result]))

'''


def print_status() -> int:
    active = list(DBUS_FAST_DIR.rglob("*.so"))
    baks = list(DBUS_FAST_DIR.rglob("*.so.bak"))
    print(f"patch:    {'APPLIED' if MARKER.exists() else 'NOT APPLIED'}"
          f"  (marker: {MARKER})")
    print(f"cython .so active: {len(active)}  "
          f"(bak count: {len(baks)})")
    return 0


def restore() -> int:
    if MARKER.exists():
        MARKER.unlink()
    for bak in DBUS_FAST_DIR.rglob("*.so.bak"):
        original = bak.with_suffix("")  # drops the .bak
        if original.exists():
            original.unlink()
        bak.rename(original)
        print(f"  restored {original}")
    if MESSAGE_BUS.exists():
        text = MESSAGE_BUS.read_text()
        # Strip the patched method body.
        text = re.sub(
            r"\n    def _focusflow_get_managed_objects\(",
            "\n    def _OLD_FOCUSFLOW_REMOVED(",
            text,
        )
        # Restore the dispatch site.
        text = text.replace(
            "                return self._focusflow_get_managed_objects",
            "                return self._default_get_managed_objects_handler",
        )
        text = text.replace(
            "\n    def _OLD_FOCUSFLOW_REMOVED(",
            "\n    def _focusflow_get_managed_objects(",
        )
        # If we just added the placeholder back, the upstream method is
        # still there; drop the patched function entirely.
        if "_OLD_FOCUSFLOW_REMOVED" in text:
            text = re.sub(
                r"\n    def _OLD_FOCUSFLOW_REMOVED\(.*?(?=\n\n    def |\nclass |\Z)",
                "\n",
                text,
                flags=re.DOTALL,
            )
        MESSAGE_BUS.write_text(text)
        print(f"  removed patch from {MESSAGE_BUS}")
    # Wipe pycache so Python re-imports.
    for pycache in DBUS_FAST_DIR.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)
    print("done")
    return 0


def apply() -> int:
    if MARKER.exists():
        print("Already patched (marker exists).  Use --restore to undo.")
        return 0

    if not MESSAGE_BUS.exists():
        print(f"ERROR: {MESSAGE_BUS} not found; install dbus-fast first",
              file=sys.stderr)
        return 1

    # Step 1: move .so files aside so Python uses .py source.
    for so in DBUS_FAST_DIR.rglob("*.so"):
        bak = so.with_suffix(so.suffix + ".bak")
        if bak.exists():
            so.unlink()
        else:
            so.rename(bak)
    for pycache in DBUS_FAST_DIR.rglob("__pycache__"):
        shutil.rmtree(pycache, ignore_errors=True)

    # Step 2: patch message_bus.py.
    text = MESSAGE_BUS.read_text()

    # Remove any prior version of the patched handler so we always
    # re-inject the current implementation.  Older versions of this
    # patch assumed ``_path_exports[path]`` was a dict; dbus_fast
    # >= 1.95 stores it as a list, so leaving the old code in place
    # breaks GATT registration with ``\'list\' object has no
    # attribute \'values\'``.
    text = re.sub(
        r"\n    def _focusflow_get_managed_objects\(.*?(?=\n\n    def |\nclass |\Z)",
        "",
        text,
        flags=re.DOTALL,
    )
    text = text.replace(
        "return self._focusflow_get_managed_objects",
        "return self._default_get_managed_objects_handler",
    )

    # Try multiple insertion points to handle version drift.
    for anchor in (
        "    def _default_get_managed_objects_handler(",
        "    def _default_properties_handler(self, msg: Message, send_reply: SendReply) -> None:",
    ):
        if anchor in text:
            text = text.replace(anchor, PATCHED_HANDLER + anchor, 1)
            break
    else:
        print("ERROR: could not find insertion anchor in message_bus.py",
              file=sys.stderr)
        return 1

    # Rewire the dispatch site.
    new_dispatch = (
        "            return self._focusflow_get_managed_objects"
    )
    old_dispatch = (
        "            return self._default_get_managed_objects_handler"
    )
    if new_dispatch in text:
        print("dispatch site already rewired")
    elif old_dispatch in text:
        text = text.replace(old_dispatch, new_dispatch)
    else:
        print("ERROR: could not find dispatch site in message_bus.py",
              file=sys.stderr)
        return 1

    MESSAGE_BUS.write_text(text)
    MARKER.touch()
    print(f"patched {MESSAGE_BUS}")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] in ("--apply", "apply"):
        return apply()
    if argv[1] in ("--restore", "restore"):
        return restore()
    if argv[1] in ("--status", "status"):
        return print_status()
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
