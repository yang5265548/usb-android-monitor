import unittest
from unittest.mock import patch

import usb_android_monitor
from usb_android_monitor import (
    acroname_control_for_serial,
    acroname_mapping_control,
    configured_devices,
    disconnect_device,
    flatten_usb_tree,
    hub_evidence_from_adb_usb_path,
    infer_uhubctl_target_from_usb_path,
    last_actions_snapshot,
    parse_acroname_serial,
    parse_adb_devices,
    record_adb_device_events,
    recovery_plan_for_serial,
    snapshot,
    wait_for_adb_present,
)


class UsbAndroidMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        with usb_android_monitor.ACTION_LOCK:
            usb_android_monitor.LAST_ACTIONS.clear()
            usb_android_monitor.ACTIVE_ACTIONS.clear()
            usb_android_monitor.ADB_EVENT_STATE.clear()
            usb_android_monitor.ADB_EVENT_LOG_INITIALIZED = False

    def test_detects_android_phone_behind_hub(self) -> None:
        state = {
            "SPUSBDataType": [
                {
                    "_name": "USB31Bus",
                    "_items": [
                        {
                            "_name": "USB2.1 Hub",
                            "vendor_id": "0x05e3  (Genesys Logic, Inc.)",
                            "product_id": "0x0610",
                            "location_id": "0x01100000",
                            "_items": [
                                {
                                    "_name": "Pixel 8",
                                    "manufacturer": "Google",
                                    "vendor_id": "0x18d1  (Google Inc.)",
                                    "product_id": "0x4ee7",
                                    "serial_num": "ABC123",
                                    "location_id": "0x01130000",
                                }
                            ],
                        }
                    ],
                }
            ]
        }

        devices = flatten_usb_tree(state)
        android = [device for device in devices if device.is_android_candidate]

        self.assertEqual(len(android), 1)
        self.assertEqual(android[0].name, "Pixel 8")
        self.assertEqual(android[0].vendor_id, "0x18d1")
        self.assertEqual(android[0].parent_hubs, ["USB31Bus", "USB2.1 Hub"])

    def test_non_android_usb_device_is_not_candidate(self) -> None:
        state = {
            "SPUSBDataType": [
                {
                    "_name": "USB31Bus",
                    "_items": [
                        {
                            "_name": "USB Receiver",
                            "manufacturer": "Logitech",
                            "vendor_id": "0x046d  (Logitech Inc.)",
                            "product_id": "0xc52f",
                        }
                    ],
                }
            ]
        }

        devices = flatten_usb_tree(state)

        self.assertFalse(any(device.is_android_candidate for device in devices))

    def test_parse_adb_devices_marks_linux_hub_path(self) -> None:
        output = """List of devices attached
R58N123456 device usb:1-1.2 product:oriole model:Pixel_6 device:oriole transport_id:5
"""

        devices = parse_adb_devices(output)

        self.assertEqual(len(devices), 1)
        self.assertEqual(devices[0].serial, "R58N123456")
        self.assertEqual(devices[0].model, "Pixel_6")
        self.assertTrue(devices[0].behind_hub)

    def test_plain_linux_usb_path_is_not_enough_for_hub(self) -> None:
        self.assertEqual(hub_evidence_from_adb_usb_path("1-1"), [])

    def test_infers_uhubctl_target_from_linux_usb_path(self) -> None:
        self.assertEqual(
            infer_uhubctl_target_from_usb_path("1-2.2"),
            {"location": "1-2", "port": "2", "source": "adb usb path 1-2.2"},
        )
        self.assertEqual(
            infer_uhubctl_target_from_usb_path("2-2.3"),
            {"location": "2-2", "port": "3", "source": "adb usb path 2-2.3"},
        )

    def test_recovery_plan_includes_configured_hub_port(self) -> None:
        config = {
            "devices": {
                "R58N123456": {
                    "uhubctl": {"location": "1-1", "port": "2"},
                    "windows_instance_id": "USB\\VID_18D1&PID_4EE7\\R58N123456",
                }
            }
        }

        plan = recovery_plan_for_serial("R58N123456", config)

        self.assertIn("adb start-server", plan)
        self.assertIn("adb reconnect", plan)
        self.assertIn("uhubctl cycle location=1-1 port=2", plan)
        self.assertIn("pnputil restart-device USB\\VID_18D1&PID_4EE7\\R58N123456", plan)

    def test_recovery_plan_includes_acroname_port(self) -> None:
        config = {
            "devices": {
                "R58N123456": {
                    "hub_control": {
                        "type": "acroname",
                        "model": "USBHub3c",
                        "hub_serial": "0xC194E2FB",
                        "port": 0,
                    }
                }
            }
        }

        plan = recovery_plan_for_serial("R58N123456", config)

        self.assertIn("acroname port cycle port=0", plan)

    def test_parse_acroname_serial_accepts_hex_and_decimal(self) -> None:
        self.assertEqual(parse_acroname_serial("0xC194E2FB"), 3247760123)
        self.assertEqual(parse_acroname_serial("C194E2FB"), 3247760123)
        self.assertEqual(parse_acroname_serial(123), 123)

    def test_acroname_control_for_serial_uses_state_mapping(self) -> None:
        known = {
            "SERIAL1": {
                "acroname_control": {
                    "model": "USBHub3c",
                    "hub_serial": "0xC194E2FB",
                    "port": 4,
                }
            }
        }

        with patch("usb_android_monitor.known_devices_snapshot", return_value=known):
            control = acroname_control_for_serial("SERIAL1", {"devices": {}})

        self.assertEqual(control["type"], "acroname")
        self.assertEqual(control["port"], 4)
        self.assertEqual(control["source"], "state")

    def test_acroname_mapping_defaults_include_port_zero(self) -> None:
        self.assertEqual(acroname_mapping_control({})["ports"], [0, 1, 2, 3, 4, 5])

    def test_windows_acroname_without_mapping_does_not_use_android_fallback(self) -> None:
        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.known_devices_snapshot", return_value={}),
            patch("usb_android_monitor.platform.system", return_value="Windows"),
            patch("usb_android_monitor.brainstem_available", return_value=True),
        ):
            result = disconnect_device("SERIAL1")

        self.assertFalse(result["ok"])
        self.assertIn("Auto-map Acroname ports", result["message"])
        self.assertNotIn("setFunctions", result["message"])

    def test_configured_devices_rejects_non_dict(self) -> None:
        self.assertEqual(configured_devices({"devices": []}), {})

    def test_snapshot_lists_known_powered_off_device_as_missing(self) -> None:
        known = {
            "SERIAL1": {
                "name": "Rack phone 1",
                "power_state": "off",
                "uhubctl_target": {
                    "location": "2-2",
                    "port": "3",
                    "source": "state",
                },
            }
        }

        with (
            patch("usb_android_monitor.load_config", return_value={"auto_recovery": {"enabled": False}, "devices": {}}),
            patch("usb_android_monitor.get_usb_devices", return_value=([], "test-usb")),
            patch("usb_android_monitor.get_adb_devices", return_value=[]),
            patch("usb_android_monitor.known_devices_snapshot", return_value=known),
            patch("usb_android_monitor.shutil.which", return_value="/usr/bin/adb"),
        ):
            state = snapshot()

        self.assertEqual(state["summary"]["configured_missing"], 1)
        self.assertEqual(state["missing_configured_devices"][0]["serial"], "SERIAL1")
        self.assertEqual(state["missing_configured_devices"][0]["power_target"]["location"], "2-2")
        self.assertIn("uhubctl on location=2-2 port=3", state["missing_configured_devices"][0]["recovery_plan"])

    def test_snapshot_lists_known_acroname_device_as_missing(self) -> None:
        known = {
            "SERIAL1": {
                "name": "Rack phone 1",
                "power_state": "off",
                "acroname_control": {
                    "type": "acroname",
                    "model": "USBHub3c",
                    "hub_serial": "0xC194E2FB",
                    "port": 2,
                    "source": "auto-map",
                },
            }
        }

        with (
            patch("usb_android_monitor.load_config", return_value={"auto_recovery": {"enabled": False}, "devices": {}}),
            patch("usb_android_monitor.get_usb_devices", return_value=([], "test-usb")),
            patch("usb_android_monitor.get_adb_devices", return_value=[]),
            patch("usb_android_monitor.known_devices_snapshot", return_value=known),
            patch("usb_android_monitor.shutil.which", return_value="/usr/bin/adb"),
        ):
            state = snapshot()

        self.assertEqual(state["summary"]["configured_missing"], 1)
        self.assertEqual(state["missing_configured_devices"][0]["power_target"]["type"], "acroname")
        self.assertEqual(state["missing_configured_devices"][0]["power_target"]["port"], 2)
        self.assertIn("acroname on port=2", state["missing_configured_devices"][0]["recovery_plan"])

    def test_wait_for_adb_present_polls_until_serial_returns(self) -> None:
        with (
            patch("usb_android_monitor.adb_state_for_serial", side_effect=["absent", "absent", "device"]),
            patch("usb_android_monitor.time.sleep"),
        ):
            present, state = wait_for_adb_present("SERIAL1", timeout_seconds=5)

        self.assertTrue(present)
        self.assertEqual(state, "device")

    def test_adb_event_log_records_disconnect_and_reconnect(self) -> None:
        online = [
            {
                "serial": "SERIAL1",
                "state": "device",
                "usb_path": "2-2.3",
                "transport_id": "7",
                "behind_hub": True,
            }
        ]

        record_adb_device_events(online)
        self.assertEqual(last_actions_snapshot(), [])

        record_adb_device_events([])
        actions = last_actions_snapshot()
        self.assertEqual(actions[0]["action"], "device-disconnected")
        self.assertEqual(actions[0]["serial"], "SERIAL1")

        record_adb_device_events(online)
        actions = last_actions_snapshot()
        self.assertEqual(actions[0]["action"], "device-connected")
        self.assertEqual(actions[0]["serial"], "SERIAL1")


if __name__ == "__main__":
    unittest.main()
