import unittest

from usb_android_monitor import (
    configured_devices,
    flatten_usb_tree,
    hub_evidence_from_adb_usb_path,
    parse_adb_devices,
    recovery_plan_for_serial,
)


class UsbAndroidMonitorTest(unittest.TestCase):
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

    def test_configured_devices_rejects_non_dict(self) -> None:
        self.assertEqual(configured_devices({"devices": []}), {})


if __name__ == "__main__":
    unittest.main()
