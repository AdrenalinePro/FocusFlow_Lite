"""Shared serial-line framing for the FocusFlow protocol (v1.0 wire format).

Reuses the JSON envelope from ``ble/windows_ble_protocol.py`` so no
message field changes.  The only addition is a newline character (``\n``)
appended to each encoded JSON payload to delimit frames on the serial
byte stream.

Why ``\n``: the compact JSON produced by ``encode_message`` contains no
embedded newlines (``separators=(",", ":")``), so ``readline()`` is safe.
"""

from __future__ import annotations

import asyncio
from typing import Optional

try:
    import serial  # pyserial
except ImportError:  # pragma: no cover — Linux side may use stdlib only
    serial = None  # type: ignore[assignment]

BAUDRATE = 115200
"""Standard FocusFlow serial rate.  ~14 KB/s, well above the ~720 B/s
peak throughput."""

FRAME_DELIMITER = b"\n"
"""Single byte appended to each JSON payload so the receiver can split
the byte stream into complete messages."""


def encode_frame(payload: bytes) -> bytes:
    """Wrap one encoded JSON payload for the serial wire."""
    return payload + FRAME_DELIMITER


def _require_serial() -> None:
    if serial is None:
        raise RuntimeError(
            "pyserial is required for serial transport.  "
            "Install it with: pip install pyserial"
        )


class SerialTransport:
    """Async-safe serial-line transport backed by pyserial.

    Reads are buffered (the OS serial driver already buffers incoming
    bytes); each ``read_message`` call returns the next complete JSON
    line or raises ``EOFError`` when the port disappears.
    """

    def __init__(
        self,
        port: str,
        baudrate: int = BAUDRATE,
        timeout: float = 0.2,
    ) -> None:
        _require_serial()
        self._serial: serial.Serial = serial.Serial(
            port=port,
            baudrate=baudrate,
            timeout=timeout,
            write_timeout=timeout,
        )
        # Opening a COM port on Windows toggles DTR/RTS, which resets the
        # Arduino UNO R4.  During the reset the USB-serial bridge may emit
        # a few garbage bytes.  Purge them before the read loop starts so
        # they won't be mistaken for JSON messages.
        self._serial.reset_input_buffer()
        self._buffer = bytearray()
        self._closed = False

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    async def write(self, data: bytes) -> None:
        """Thread-safe non-blocking write."""
        if self._closed:
            return
        await asyncio.to_thread(self._serial.write, data)

    async def read_message(self) -> bytes:
        """Return the next newline-delimited message (without the trailing ``\\n``).

        Raises ``EOFError`` when the port is closed or the USB cable is
        unplugged.
        """
        while True:
            if self._closed:
                raise EOFError("serial port closed")
            idx = self._buffer.find(b"\n")
            if idx >= 0:
                line = bytes(self._buffer[:idx])
                del self._buffer[: idx + 1]
                return line
            # Refill the buffer from the OS serial driver.
            chunk = await asyncio.to_thread(self._read_chunk)
            if chunk is None:
                raise EOFError("serial port disappeared (cable unplugged?)")
            if chunk:
                self._buffer.extend(chunk)

    def close(self) -> None:
        self._closed = True
        try:
            self._serial.close()
        except Exception:
            pass

    @property
    def is_open(self) -> bool:
        return not self._closed and self._serial.is_open

    # ------------------------------------------------------------------
    # internal
    # ------------------------------------------------------------------

    def _read_chunk(self) -> Optional[bytes]:
        """Read whatever bytes are available from the OS buffer.

        Returns ``None`` when the port is gone (USB unplugged, etc.).
        """
        try:
            if not self._serial.is_open:
                return None
            waiting = self._serial.in_waiting or 1
            return self._serial.read(waiting)
        except (OSError, serial.SerialException):
            return None


async def auto_resolve_port(preferred: Optional[str] = None) -> str:
    """Pick a serial port.

    *If* ``preferred`` is given and can be opened, return it immediately.
    Otherwise scan the available ports for a plausible Arduino UNO R4
    (VID=0x2341) and return the first match.  Falls back to the
    preferred value when nothing is found, so the caller can surface a
    clear error.
    """
    _require_serial()
    from serial.tools.list_ports import comports

    if preferred is not None:
        # Quick liveness check — don't keep the port open.
        try:
            probe = serial.Serial(preferred, baudrate=BAUDRATE, timeout=0.1)
            probe.close()
            return preferred
        except (OSError, serial.SerialException):
            pass  # Fall through to auto-detect.

    for candidate in comports():
        if candidate.vid == 0x2341:  # Arduino VID
            return candidate.device
    return preferred or "COM3"
