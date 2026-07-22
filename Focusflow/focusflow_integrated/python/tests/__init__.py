"""Integration tests for the FocusFlow BLE server subclass.

These tests run on a developer workstation (no BlueZ, no Arduino, no
real BLE peripherals).  They construct a :class:`FocusFlowBLEServer`
with mocked ``WristbandController`` and ``TFTBridge`` and exercise the
new ``decision_update`` path plus the existing ``rest_command`` path
to confirm the vibration policy and TFT behaviour described in:

* ``../../README.md`` (user requirement #3)
* ``../../../UNO_Q_BLE_DECISION_PROTOCOL.md`` (BLE supplement)

The focus is end-to-end logic, not D-Bus plumbing; the latter is
covered by the upstream ``linux_ble_test.py`` and runs on the UNO Q.
"""
