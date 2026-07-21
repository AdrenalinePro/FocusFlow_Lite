#!/usr/bin/env bash
# setup_dbus_fast.sh - apply the dbus_fast patches that the FocusFlow
# UNO Q BLE server needs.
#
# Background
# ----------
# Upstream dbus_fast (>= 1.83) has two behaviours that prevent the
# FocusFlow server from registering a GATT application with BlueZ:
#
#  1. The default ``_default_get_managed_objects_handler`` filters
#     exported paths with ``node.startswith(msg.path + "/")``, which
#     excludes the queried root path itself.  BlueZ's
#     ``GattManager1.RegisterApplication`` calls GetManagedObjects on
#     the registered application path and expects that path to be a key
#     in the response.  Without the patch, BlueZ reports
#     "No valid external GATT objects found" and silently drops the
#     application.
#
#  2. The default handler reads property values via the
#     ``@dbus_property`` machinery, which returns empty ``{}`` for our
#     hand-written ``get_properties()`` methods.  BlueZ then complains
#     that the characteristic's ``Service`` / ``UUID`` / ``Flags``
#     properties are missing.
#
# What this script does
# ---------------------
# It renames every Cython ``.so`` wheel file to ``.so.bak`` so the
# pure-Python sources in ``dbus_fast/*.py`` are loaded, then injects a
# ``_focusflow_get_managed_objects`` method into ``message_bus.py`` and
# rewires ``_find_message_handler`` to call it for
# ``ObjectManager.GetManagedObjects``.
#
# Idempotent: re-running this script is a no-op once the marker file
# ``.focusflow-patched`` exists.  ``pip install --upgrade dbus-fast``
# removes the wheel and the .so files come back without the .bak
# suffix; the next run of this script restores the patch.
#
# Use ``./setup_dbus_fast.sh --restore`` to undo (re-enable the Cython
# extensions, drop the patch).  Use ``./setup_dbus_fast.sh --status``
# to print whether the patch is currently applied.

set -euo pipefail

DBUS_FAST_DIR="/home/arduino/.local/lib/python3.13/site-packages/dbus_fast"
MESSAGE_BUS_PY="${DBUS_FAST_DIR}/message_bus.py"
MARKER="${DBUS_FAST_DIR}/.focusflow-patched"

print_status() {
    if [[ -f "$MARKER" ]]; then
        echo "patch:  APPLIED  (marker: $MARKER)"
    else
        echo "patch:  NOT APPLIED"
    fi
    local n=0
    for so in "$DBUS_FAST_DIR"/*.so "$DBUS_FAST_DIR"/_private/*.so "$DBUS_FAST_DIR"/aio/*.so; do
        [[ -f "$so" ]] && n=$((n+1))
    done
    echo "cython .so active: $n  (bak count: $(find "$DBUS_FAST_DIR" -name '*.so.bak' | wc -l))"
}

restore() {
    echo "Restoring: moving .so.bak back to .so and removing the marker."
    for bak in $(find "$DBUS_FAST_DIR" -name "*.so.bak"); do
        original="${bak%.bak}"
        if [[ ! -f "$original" ]]; then
            mv "$bak" "$original"
            echo "  restored $original"
        else
            echo "  skipped $bak (target already exists)"
        fi
    done
    if [[ -f "$MESSAGE_BUS_PY" ]]; then
        # Remove our injected method and rewire
        python3 - <<'PYEOF'
import re, pathlib
p = pathlib.Path("/home/arduino/.local/lib/python3.13/site-packages/dbus_fast/message_bus.py")
text = p.read_text()
text = re.sub(
    r"\n    def _focusflow_get_managed_objects\(self, msg, send_reply\):.*?(?=\n\n    def |\nclass |\Z)",
    "", text, flags=re.DOTALL,
)
text = text.replace(
    "                return self._focusflow_get_managed_objects",
    "                return self._default_get_managed_objects_handler",
)
p.write_text(text)
PYEOF
        echo "  removed patch from message_bus.py"
    fi
    rm -f "$MARKER"
    echo "marker removed"
}

apply_patch() {
    if [[ -f "$MARKER" ]]; then
        echo "Already patched (marker exists).  Use --restore to undo."
        exit 0
    fi

    if [[ ! -d "$DBUS_FAST_DIR" ]]; then
        echo "Cannot find dbus_fast at $DBUS_FAST_DIR." >&2
        echo "If dbus-fast is installed elsewhere, set DBUS_FAST_DIR and re-run." >&2
        exit 1
    fi

    echo "Step 1: move Cython .so files aside..."
    n=0
    # If a .so.bak already exists, the .so is a fresh wheel install
    # and the previous patched one is in .bak -- delete the live .so
    # so Python falls back to the .py source.
    for so in $(find "$DBUS_FAST_DIR" -name "*.so"); do
        bak="${so}.bak"
        if [[ -f "$bak" ]]; then
            rm -f "$so"
            n=$((n+1))
        else
            mv "$so" "$bak"
            n=$((n+1))
        fi
    done
    echo "  deactivated $n .so files (kept as .so.bak)"

    # Clear pycache so Python re-imports from the .py sources
    find "$DBUS_FAST_DIR" -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    find /home/arduino/focusble -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null || true
    echo "  cleared pycache"

    echo "Step 2: patch message_bus.py..."
    python3 - <<'PYEOF'
import re, pathlib
p = pathlib.Path("/home/arduino/.local/lib/python3.13/site-packages/dbus_fast/message_bus.py")
text = p.read_text()
if "_focusflow_get_managed_objects" in text:
    print("  already contains _focusflow_get_managed_objects, skipping injection")
else:
    PATCH = '''
    def _focusflow_get_managed_objects(self, msg, send_reply):
        """Patched ``GetManagedObjects`` that:

        1. Includes the queried root path (the default handler
           excludes it, breaking BlueZ's ``RegisterApplication``).
        2. Reads property values via the interface's
           ``get_properties()`` method instead of the
           ``@dbus_property`` machinery, so our hand-written GATT
           classes expose their state.
        """
        from dbus_fast import Message as _Msg
        result_signature = "a{oa{sa{sv}}}"

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
            def cb(intf, values, _, err):
                if err is None and values:
                    captured.update(values)
            ServiceInterface._get_all_property_values(interface, cb, None)
            return captured

        result = {}
        for node in nodes:
            result[node] = {}
            for interface in self._path_exports[node].values():
                result[node][interface.name] = collect(interface)

        send_reply(_Msg.new_method_return(msg, result_signature, [result]))
'''
    target = "    def _default_properties_handler(self, msg: Message, send_reply: SendReply) -> None:"
    if target not in text:
        raise SystemExit("could not find insertion point in message_bus.py")
    text = text.replace(target, PATCH + "\n\n" + target)
    
    old_dispatch = """            if (
                msg.interface == "org.freedesktop.DBus.ObjectManager"
                and msg.member == "GetManagedObjects"
            ):
                return self._default_get_managed_objects_handler"""
    new_dispatch = """            if (
                msg.interface == "org.freedesktop.DBus.ObjectManager"
                and msg.member == "GetManagedObjects"
            ):
                return self._focusflow_get_managed_objects"""
    if old_dispatch not in text:
        raise SystemExit("could not find dispatch in message_bus.py")
    text = text.replace(old_dispatch, new_dispatch)
    p.write_text(text)
    print("  patched message_bus.py")
PYEOF

    touch "$MARKER"
    echo
    echo "Patch applied successfully."
}

case "${1:-apply}" in
    --restore|restore) restore ;;
    --status|status) print_status ;;
    --help|-h) cat <<EOF
Usage: $0 [--apply | --restore | --status | --help]
  --apply   (default) Apply the dbus_fast patch
  --restore         Restore the Cython .so files and drop the patch
  --status          Show current state
  --help            This message
EOF
        ;;
    *) apply_patch ;;
esac
