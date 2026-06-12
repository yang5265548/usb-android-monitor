import os
import unittest
from tempfile import TemporaryDirectory
from unittest.mock import patch

import usb_android_monitor
from usb_android_monitor import (
    acroname_control_for_serial,
    acroname_mapping_control,
    clear_learned_acroname_mappings,
    configured_devices,
    connect_device,
    diagnose_disconnect,
    disconnect_device,
    format_text_log,
    flatten_usb_tree,
    hub_evidence_from_adb_usb_path,
    infer_uhubctl_target_from_usb_path,
    last_actions_snapshot,
    map_acroname_ports,
    normalized_acroname_ports,
    parse_acroname_serial,
    parse_adb_devices,
    record_adb_device_events,
    recent_persistent_logs,
    recovery_plan_for_serial,
    snapshot,
    wait_for_adb_present,
    write_log,
)


class UsbAndroidMonitorTest(unittest.TestCase):
    def setUp(self) -> None:
        with usb_android_monitor.ACTION_LOCK:
            usb_android_monitor.LAST_ACTIONS.clear()
            usb_android_monitor.ACTIVE_ACTIONS.clear()
            usb_android_monitor.ADB_EVENT_STATE.clear()
            usb_android_monitor.ADB_EVENT_LOG_INITIALIZED = False
            usb_android_monitor.LAST_ADB_RAW_OUTPUT = ""
            usb_android_monitor.EVENT_HISTORY.clear()
            usb_android_monitor.MANUAL_DISCONNECT_UNTIL.clear()
            usb_android_monitor.HUB_BACKEND = "acroname"
        with usb_android_monitor.MIRROR_LOCK:
            usb_android_monitor.MIRROR_SCRCPY_PROCESSES.clear()
            usb_android_monitor.MIRROR_SCRCPY_LOG_FILES.clear()
            usb_android_monitor.MIRROR_AUTO_ALL = False
            usb_android_monitor.MIRROR_TARGET_SERIALS.clear()
            usb_android_monitor.MIRROR_DISABLED_SERIALS.clear()
            usb_android_monitor.MIRROR_FAILED_SERIALS.clear()
            usb_android_monitor.MIRROR_LOG_FILE = ""
        usb_android_monitor.LOG_ENABLED = False

    def tearDown(self) -> None:
        usb_android_monitor.LOG_ENABLED = True

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

    def test_persistent_logs_round_trip_jsonl(self) -> None:
        with TemporaryDirectory() as log_dir:
            old_dir = usb_android_monitor.LOG_DIR
            old_latest_initialized = usb_android_monitor.LATEST_LOGS_INITIALIZED
            usb_android_monitor.LOG_DIR = log_dir
            usb_android_monitor.LATEST_LOGS_INITIALIZED = False
            usb_android_monitor.LOG_ENABLED = True
            try:
                write_log("unit_test_event", {"serial": "SERIAL1", "message": "hello"})
                entries = recent_persistent_logs(10)
                text_logs = [
                    os.path.join(root, name)
                    for root, _, names in os.walk(log_dir)
                    for name in names
                    if name.startswith("run-") and name.endswith(".log")
                ]
                json_logs = [
                    os.path.join(root, name)
                    for root, _, names in os.walk(log_dir)
                    for name in names
                    if name.startswith("run-") and name.endswith(".jsonl")
                ]
                latest_jsonl_exists = os.path.exists(os.path.join(log_dir, "latest.jsonl"))
                run_log_is_in_date_dir = os.path.basename(os.path.dirname(text_logs[0])).count("-") == 2
                with open(os.path.join(log_dir, "latest.log"), "r", encoding="utf-8") as handle:
                    text_content = handle.read()
            finally:
                usb_android_monitor.LOG_DIR = old_dir
                usb_android_monitor.LATEST_LOGS_INITIALIZED = old_latest_initialized

        self.assertEqual(entries[0]["event"], "unit_test_event")
        self.assertEqual(entries[0]["serial"], "SERIAL1")
        self.assertEqual(len(text_logs), 1)
        self.assertEqual(len(json_logs), 1)
        self.assertTrue(latest_jsonl_exists)
        self.assertTrue(run_log_is_in_date_dir)
        self.assertIn("unit_test_event", text_content)
        self.assertIn("serial=SERIAL1", text_content)

    def test_format_text_log_is_readable(self) -> None:
        line = format_text_log(
            {
                "ts": "2026-06-04T10:00:00+03:00",
                "event": "hub_port_action_result",
                "backend": "acroname",
                "serial": "SERIAL1",
                "port": 2,
                "ok": False,
                "result_code": 18,
                "message": "port off failed",
            }
        )

        self.assertIn("ERROR", line)
        self.assertIn("hub_port_action_result", line)
        self.assertIn("backend=acroname", line)
        self.assertIn("port=2", line)
        self.assertIn("message=port off failed", line)

    def test_disconnect_diagnosis_uses_recent_manual_action(self) -> None:
        with TemporaryDirectory() as log_dir:
            old_dir = usb_android_monitor.LOG_DIR
            old_latest_initialized = usb_android_monitor.LATEST_LOGS_INITIALIZED
            usb_android_monitor.LOG_DIR = log_dir
            usb_android_monitor.LATEST_LOGS_INITIALIZED = False
            usb_android_monitor.LOG_ENABLED = True
            try:
                write_log("action_started", {"action": "disconnect", "serial": "SERIAL1"})
                diagnose_disconnect(
                    "SERIAL1",
                    ["SERIAL1"],
                    ("device", "2-2.3", "7", True),
                    {"backend": "test-usb-log", "ok": True, "raw": "usb disconnect"},
                )
                entries = recent_persistent_logs(20)
            finally:
                usb_android_monitor.LOG_DIR = old_dir
                usb_android_monitor.LATEST_LOGS_INITIALIZED = old_latest_initialized

        diagnosis = next(entry for entry in entries if entry["event"] == "disconnect_diagnosis")
        self.assertEqual(diagnosis["reason"], "manual_disconnect")
        self.assertEqual(diagnosis["confidence"], "high")
        self.assertIn("recent manual disconnect", diagnosis["message"])

    def test_disconnect_diagnosis_flags_multi_device_loss(self) -> None:
        with TemporaryDirectory() as log_dir:
            old_dir = usb_android_monitor.LOG_DIR
            old_latest_initialized = usb_android_monitor.LATEST_LOGS_INITIALIZED
            usb_android_monitor.LOG_DIR = log_dir
            usb_android_monitor.LATEST_LOGS_INITIALIZED = False
            usb_android_monitor.LOG_ENABLED = True
            try:
                diagnose_disconnect(
                    "SERIAL1",
                    ["SERIAL1", "SERIAL2"],
                    ("device", "", "7", False),
                    {"backend": "test-usb-log", "ok": True, "raw": "hub reset"},
                )
                entries = recent_persistent_logs(20)
            finally:
                usb_android_monitor.LOG_DIR = old_dir
                usb_android_monitor.LATEST_LOGS_INITIALIZED = old_latest_initialized

        diagnosis = next(entry for entry in entries if entry["event"] == "disconnect_diagnosis")
        self.assertEqual(diagnosis["reason"], "hub_or_upstream_disconnect")
        self.assertEqual(diagnosis["confidence"], "high")
        self.assertEqual(diagnosis["affected_serials"], ["SERIAL1", "SERIAL2"])

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
        usb_android_monitor.HUB_BACKEND = "uhubctl"
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

    def test_acroname_control_for_serial_ignores_suspect_state_mapping(self) -> None:
        known = {
            "SERIAL1": {
                "mapping_status": "suspect",
                "acroname_control": {
                    "model": "USBHub3c",
                    "hub_serial": "0xC194E2FB",
                    "port": 4,
                },
            }
        }

        with patch("usb_android_monitor.known_devices_snapshot", return_value=known):
            control = acroname_control_for_serial("SERIAL1", {"devices": {}})

        self.assertEqual(control, {})

    def test_acroname_mapping_defaults_skip_port_zero(self) -> None:
        self.assertEqual(acroname_mapping_control({})["ports"], [1, 2, 3, 4, 5])

    def test_clear_learned_acroname_mappings_keeps_config_source(self) -> None:
        with TemporaryDirectory() as temp_dir:
            old_state = usb_android_monitor.STATE_PATH
            usb_android_monitor.STATE_PATH = os.path.join(temp_dir, "state.json")
            try:
                usb_android_monitor.save_state(
                    {
                        "known_devices": {
                            "AUTO": {
                                "power_state": "off",
                                "mapping_status": "suspect",
                                "mapping_conflict_with": "OTHER",
                                "acroname_control": {"port": 1, "source": "auto-map"},
                            },
                            "CONFIG": {
                                "power_state": "off",
                                "acroname_control": {"port": 2, "source": "config"},
                            },
                        }
                    }
                )
                cleared = clear_learned_acroname_mappings()
                state = usb_android_monitor.load_state()
            finally:
                usb_android_monitor.STATE_PATH = old_state

        self.assertEqual(cleared, 1)
        self.assertNotIn("acroname_control", state["known_devices"]["AUTO"])
        self.assertNotIn("mapping_status", state["known_devices"]["AUTO"])
        self.assertEqual(state["known_devices"]["AUTO"]["power_state"], "on")
        self.assertEqual(state["known_devices"]["CONFIG"]["acroname_control"]["port"], 2)

    def test_normalized_acroname_ports_scans_zero_last(self) -> None:
        self.assertEqual(normalized_acroname_ports([0, 3, 1, 0]), [1, 3, 0])

    def test_acroname_map_aborts_when_failed_action_changes_adb_state(self) -> None:
        with (
            patch("usb_android_monitor.load_config", return_value={"acroname": {"ports": [0]}, "devices": {}}),
            patch("usb_android_monitor.adb_serials", side_effect=[{"A", "B"}, {"A", "B"}, set()]),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                side_effect=[
                    {"ok": False, "status": "failed", "message": "off failed"},
                    {"ok": False, "status": "failed", "message": "on failed"},
                ],
            ),
        ):
            result = map_acroname_ports()

        self.assertFalse(result["ok"])
        self.assertIn("aborted", result["message"])

    def test_acroname_map_clears_learned_mappings_before_probe(self) -> None:
        with (
            patch("usb_android_monitor.load_config", return_value={"acroname": {"ports": [1]}, "devices": {}}),
            patch("usb_android_monitor.adb_serials", return_value={"A"}),
            patch("usb_android_monitor.clear_learned_acroname_mappings", return_value=3) as clear,
            patch("usb_android_monitor.run_acroname_port_action", return_value={"ok": True, "status": "ok", "message": "ok"}),
            patch("usb_android_monitor.wait_for_serials_absent", return_value=set()),
        ):
            result = map_acroname_ports()

        clear.assert_called_once()
        self.assertFalse(result["ok"])
        self.assertIn("cleared learned mappings=3", result["message"])

    def test_acroname_map_aborts_on_ambiguous_port(self) -> None:
        with (
            patch("usb_android_monitor.load_config", return_value={"acroname": {"ports": [1]}, "devices": {}}),
            patch("usb_android_monitor.adb_serials", side_effect=[{"A", "B"}, {"A", "B"}, set(), {"A", "B"}]),
            patch("usb_android_monitor.wait_for_serials_absent", return_value={"A", "B"}),
            patch("usb_android_monitor.wait_for_serials_present", return_value={"A", "B"}),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                side_effect=[
                    {"ok": True, "status": "ok", "message": "off ok"},
                    {"ok": True, "status": "ok", "message": "on ok"},
                ],
            ),
        ):
            result = map_acroname_ports()

        self.assertFalse(result["ok"])
        self.assertIn("multiple ADB serials", result["message"])

    def test_acroname_map_saves_partial_mapping_when_phone_returns_slowly(self) -> None:
        remembered: list[tuple[str, dict[str, object]]] = []

        def remember(serial: str, data: dict[str, object]) -> None:
            remembered.append((serial, data))

        with (
            patch("usb_android_monitor.load_config", return_value={"acroname": {"ports": [1]}, "devices": {}}),
            patch("usb_android_monitor.adb_serials", return_value={"A", "B"}),
            patch("usb_android_monitor.clear_learned_acroname_mappings", return_value=0),
            patch("usb_android_monitor.wait_for_serials_absent", return_value={"A"}),
            patch("usb_android_monitor.wait_for_serials_present", return_value=set()),
            patch("usb_android_monitor.reconnect_device"),
            patch("usb_android_monitor.remember_known_device", side_effect=remember),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                side_effect=[
                    {"ok": True, "status": "ok", "message": "off ok"},
                    {"ok": True, "status": "ok", "message": "on ok"},
                ],
            ),
        ):
            result = map_acroname_ports()

        self.assertTrue(result["ok"])
        self.assertIn("partial mapping", result["message"])
        self.assertIn("partial returns=1", result["message"])
        self.assertEqual(remembered[0][0], "A")
        self.assertEqual(remembered[0][1]["power_state"], "unknown")
        self.assertEqual(remembered[0][1]["mapping_status"], "mapped-needs-return")
        self.assertEqual(remembered[0][1]["acroname_control"]["port"], 1)

    def test_acroname_map_continues_after_slow_return(self) -> None:
        remembered: list[tuple[str, dict[str, object]]] = []

        def remember(serial: str, data: dict[str, object]) -> None:
            remembered.append((serial, data))

        with (
            patch("usb_android_monitor.load_config", return_value={"acroname": {"ports": [1, 2]}, "devices": {}}),
            patch("usb_android_monitor.adb_serials", side_effect=[{"A", "B"}, {"A", "B"}, {"B"}]),
            patch("usb_android_monitor.clear_learned_acroname_mappings", return_value=0),
            patch("usb_android_monitor.wait_for_serials_absent", side_effect=[{"A"}, {"B"}]),
            patch("usb_android_monitor.wait_for_serials_present", side_effect=[set(), {"B"}]),
            patch("usb_android_monitor.reconnect_device"),
            patch("usb_android_monitor.remember_known_device", side_effect=remember),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                side_effect=[
                    {"ok": True, "status": "ok", "message": "port 1 off"},
                    {"ok": True, "status": "ok", "message": "port 1 on"},
                    {"ok": True, "status": "ok", "message": "port 2 off"},
                    {"ok": True, "status": "ok", "message": "port 2 on"},
                ],
            ),
            patch("usb_android_monitor.time.sleep"),
        ):
            result = map_acroname_ports()

        self.assertTrue(result["ok"])
        self.assertEqual([item[0] for item in remembered], ["A", "B"])
        self.assertEqual(remembered[0][1]["mapping_status"], "mapped-needs-return")
        self.assertEqual(remembered[1][1]["mapping_status"], "mapped")
        self.assertIn("partial returns=1", result["message"])

    def test_windows_acroname_without_mapping_does_not_use_android_fallback(self) -> None:
        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.known_devices_snapshot", return_value={}),
            patch("usb_android_monitor.platform.system", return_value="Windows"),
            patch("usb_android_monitor.brainstem_available", return_value=True),
        ):
            result = disconnect_device("SERIAL1")

        self.assertFalse(result["ok"])
        self.assertIn("Refresh Acroname port map", result["message"])
        self.assertNotIn("setFunctions", result["message"])

    def test_acroname_connect_uses_global_discovery_when_serial_reconnect_is_slow(self) -> None:
        control = {"type": "acroname", "model": "USBHub3c", "hub_serial": "0xC194E2FB", "port": 5}
        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.acroname_control_for_serial", return_value=control),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                return_value={"ok": True, "status": "ok", "message": "Acroname USBHub3c port 5 on; result=0"},
            ),
            patch(
                "usb_android_monitor.reconnect_device",
                side_effect=[
                    {"ok": True, "message": "serial reconnect did not find device"},
                    {"ok": True, "message": "global reconnect"},
                ],
            ) as reconnect,
            patch("usb_android_monitor.wait_for_adb_present", side_effect=[(False, "absent"), (True, "device")]),
            patch("usb_android_monitor.forget_disconnected_target") as forget,
        ):
            result = connect_device("SERIAL1")

        self.assertTrue(result["ok"])
        self.assertEqual(reconnect.call_args_list[1].args[0], "")
        self.assertIn("adb returned after global discovery", result["message"])
        forget.assert_called_once_with("SERIAL1")

    def test_acroname_connect_cycles_port_when_adb_stays_absent(self) -> None:
        control = {"type": "acroname", "model": "USBHub3c", "hub_serial": "0xC194E2FB", "port": 5}
        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.acroname_control_for_serial", return_value=control),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                side_effect=[
                    {"ok": True, "status": "ok", "message": "Acroname USBHub3c port 5 on; result=0"},
                    {"ok": True, "status": "ok", "message": "Acroname USBHub3c port 5 cycle; result=0"},
                ],
            ) as port_action,
            patch("usb_android_monitor.reconnect_device", return_value={"ok": True, "message": "reconnect"}),
            patch("usb_android_monitor.wait_for_adb_present", side_effect=[(False, "absent"), (False, "absent"), (True, "device")]),
            patch("usb_android_monitor.time.sleep"),
            patch("usb_android_monitor.forget_disconnected_target") as forget,
        ):
            result = connect_device("SERIAL1")

        self.assertTrue(result["ok"])
        self.assertEqual(port_action.call_args_list[1].args[2], "cycle")
        self.assertIn("adb returned after retry cycle", result["message"])
        forget.assert_called_once_with("SERIAL1")

    def test_linux_connect_prefers_uhubctl_when_acroname_state_is_stale(self) -> None:
        stale_acroname = {"type": "acroname", "model": "USBHub3c", "port": 5}
        uhubctl = {"location": "2-2", "port": "3", "source": "state"}
        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.platform.system", return_value="Linux"),
            patch("usb_android_monitor.hub_backend", return_value="uhubctl"),
            patch("usb_android_monitor.brainstem_available", return_value=False),
            patch("usb_android_monitor.acroname_control_for_serial", return_value=stale_acroname),
            patch("usb_android_monitor.uhubctl_target_for_serial", return_value=uhubctl),
            patch("usb_android_monitor.power_on_linux_hub_port", return_value={"ok": True, "message": "uhubctl on ok"}),
            patch("usb_android_monitor.run_acroname_port_action") as acroname_action,
            patch("usb_android_monitor.reconnect_device", return_value={"ok": True, "message": "reconnect"}),
            patch("usb_android_monitor.wait_for_adb_present", return_value=(True, "device")),
            patch("usb_android_monitor.forget_disconnected_target") as forget,
        ):
            result = connect_device("SERIAL1")

        self.assertTrue(result["ok"])
        self.assertIn("hub port power on via state", result["message"])
        acroname_action.assert_not_called()
        forget.assert_called_once_with("SERIAL1")

    def test_acroname_disconnect_keeps_powered_off_state_when_verified(self) -> None:
        control = {"type": "acroname", "model": "USBHub3c", "hub_serial": "0xC194E2FB", "port": 5}
        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.acroname_control_for_serial", return_value=control),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                return_value={"ok": True, "status": "ok", "message": "Acroname USBHub3c port 5 off"},
            ),
            patch("usb_android_monitor.adb_serials", side_effect=[{"SERIAL1"}, set()]),
            patch("usb_android_monitor.remember_known_device") as remember,
        ):
            result = disconnect_device("SERIAL1")

        self.assertTrue(result["ok"])
        self.assertEqual(remember.call_args.args[0], "SERIAL1")
        self.assertEqual(remember.call_args.args[1]["power_state"], "off")
        self.assertEqual(remember.call_args.args[1]["acroname_control"]["port"], 5)

    def test_acroname_disconnect_trusts_adb_when_hub_reports_error(self) -> None:
        control = {"type": "acroname", "model": "USBHub3c", "hub_serial": "0xC194E2FB", "port": 5}
        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.acroname_control_for_serial", return_value=control),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                return_value={"ok": False, "status": "failed", "message": "Acroname USBHub3c port 5 off; result=18"},
            ),
            patch("usb_android_monitor.adb_serials", side_effect=[{"SERIAL1"}, set()]),
            patch("usb_android_monitor.remember_known_device") as remember,
        ):
            result = disconnect_device("SERIAL1")

        self.assertTrue(result["ok"])
        self.assertIn("warning: hub API reported failure", result["message"])
        self.assertEqual(remember.call_args.args[0], "SERIAL1")
        self.assertEqual(remember.call_args.args[1]["power_state"], "off")
        self.assertEqual(remember.call_args.args[1]["acroname_control"]["port"], 5)

    def test_acroname_disconnect_records_mapping_conflict_for_actual_missing_serial(self) -> None:
        control = {"type": "acroname", "model": "USBHub3c", "hub_serial": "0xC194E2FB", "port": 5}
        remembered: list[tuple[str, dict[str, object]]] = []

        def remember(serial: str, data: dict[str, object]) -> None:
            remembered.append((serial, data))

        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.acroname_control_for_serial", return_value=control),
            patch(
                "usb_android_monitor.run_acroname_port_action",
                return_value={"ok": True, "status": "ok", "message": "Acroname USBHub3c port 5 off"},
            ),
            patch("usb_android_monitor.adb_serials", side_effect=[{"SERIAL1", "SERIAL2"}, {"SERIAL1"}]),
            patch("usb_android_monitor.adb_state_for_serial", return_value="device"),
            patch("usb_android_monitor.remember_known_device", side_effect=remember),
        ):
            result = disconnect_device("SERIAL1")

        self.assertFalse(result["ok"])
        self.assertIn("adb disappeared=['SERIAL2']", result["message"])
        self.assertIn(("SERIAL2", {
            "acroname_control": control,
            "power_state": "off",
            "mapping_status": "learned-from-conflict",
            "mapping_conflict_with": "SERIAL1",
        }), remembered)
        self.assertEqual(remembered[-1][0], "SERIAL1")
        self.assertEqual(remembered[-1][1]["power_state"], "on")
        self.assertEqual(remembered[-1][1]["mapping_status"], "suspect")

    def test_linux_disconnect_prefers_uhubctl_when_acroname_state_is_stale(self) -> None:
        stale_acroname = {"type": "acroname", "model": "USBHub3c", "port": 5}
        uhubctl = {"location": "2-2", "port": "3", "source": "state"}
        with (
            patch("usb_android_monitor.load_config", return_value={"devices": {}}),
            patch("usb_android_monitor.platform.system", return_value="Linux"),
            patch("usb_android_monitor.hub_backend", return_value="uhubctl"),
            patch("usb_android_monitor.brainstem_available", return_value=False),
            patch("usb_android_monitor.acroname_control_for_serial", return_value=stale_acroname),
            patch("usb_android_monitor.uhubctl_target_for_serial", return_value=uhubctl),
            patch("usb_android_monitor.remember_disconnected_target"),
            patch("usb_android_monitor.power_off_linux_hub_port", return_value={"ok": True, "message": "uhubctl off ok"}),
            patch("usb_android_monitor.run_acroname_port_action") as acroname_action,
            patch("usb_android_monitor.wait_for_adb_absent", return_value=(True, "absent")),
        ):
            result = disconnect_device("SERIAL1")

        self.assertTrue(result["ok"])
        self.assertIn("hub port power off requested via state", result["message"])
        acroname_action.assert_not_called()

    def test_configured_devices_rejects_non_dict(self) -> None:
        self.assertEqual(configured_devices({"devices": []}), {})

    def test_hub_backend_can_be_forced_to_uhubctl(self) -> None:
        self.assertEqual(usb_android_monitor.select_hub_backend({"hub_backend": "acthub"}), "uhubctl")
        self.assertEqual(usb_android_monitor.select_hub_backend({"hub_control": {"type": "uhubctl"}}), "uhubctl")

    def test_hub_backend_auto_prefers_uhubctl_when_available(self) -> None:
        with (
            patch("usb_android_monitor.shutil.which", return_value="/usr/bin/uhubctl"),
            patch("usb_android_monitor.brainstem_available", return_value=True),
        ):
            backend = usb_android_monitor.select_hub_backend({"devices": {}})

        self.assertEqual(backend, "uhubctl")

    def test_hub_backend_auto_uses_acroname_without_uhubctl(self) -> None:
        with (
            patch("usb_android_monitor.shutil.which", return_value=None),
            patch("usb_android_monitor.brainstem_available", return_value=True),
        ):
            backend = usb_android_monitor.select_hub_backend({"devices": {}})

        self.assertEqual(backend, "acroname")

    def test_snapshot_lists_known_powered_off_device_as_missing(self) -> None:
        usb_android_monitor.HUB_BACKEND = "uhubctl"
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

    def test_snapshot_does_not_list_known_online_device_as_missing_after_restart(self) -> None:
        known = {
            "SERIAL1": {
                "name": "Rack phone 1",
                "power_state": "on",
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

        self.assertEqual(state["summary"]["configured_missing"], 0)
        self.assertEqual(state["missing_configured_devices"], [])

    def test_snapshot_does_not_mark_manual_disconnect_back_online(self) -> None:
        usb_android_monitor.MANUAL_DISCONNECT_UNTIL["SERIAL1"] = usb_android_monitor.time.time() + 60
        android = [
            {
                "serial": "SERIAL1",
                "state": "device",
                "usb_path": "",
                "transport_id": "9",
                "behind_hub": False,
                "needs_attention": False,
            }
        ]

        with (
            patch("usb_android_monitor.load_config", return_value={"auto_recovery": {"enabled": False}, "devices": {}}),
            patch("usb_android_monitor.get_usb_devices", return_value=([], "test-usb")),
            patch("usb_android_monitor.get_adb_devices", return_value=[]),
            patch("usb_android_monitor.enrich_adb_with_usb", return_value=android),
            patch("usb_android_monitor.forget_disconnected_target") as forget,
            patch("usb_android_monitor.shutil.which", return_value="/usr/bin/adb"),
        ):
            snapshot()

        forget.assert_not_called()

    def test_snapshot_does_not_return_persistent_logs_to_recent_actions(self) -> None:
        with (
            patch("usb_android_monitor.load_config", return_value={"auto_recovery": {"enabled": False}, "devices": {}}),
            patch("usb_android_monitor.get_usb_devices", return_value=([], "test-usb")),
            patch("usb_android_monitor.get_adb_devices", return_value=[]),
            patch("usb_android_monitor.known_devices_snapshot", return_value={}),
            patch("usb_android_monitor.shutil.which", return_value="/usr/bin/adb"),
        ):
            state = snapshot()

        self.assertEqual(state["last_actions"], [])
        self.assertNotIn("persistent_logs", state)

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

    def test_mirror_actions_complete_without_active_queue_entry(self) -> None:
        with patch("usb_android_monitor.start_mirror_script", return_value={"ok": True, "message": "started"}) as start:
            result = usb_android_monitor.run_action_async("start-mirror-script", "")

        start.assert_called_once_with("")
        self.assertEqual(result["message"], "started")
        self.assertEqual(usb_android_monitor.active_actions_snapshot(), [])

    def test_scrcpy_exit_pauses_auto_restart_for_device(self) -> None:
        class ExitedProcess:
            def poll(self) -> int:
                return 1

        with TemporaryDirectory() as temp_dir:
            log_path = os.path.join(temp_dir, "scrcpy_SERIAL1.log")
            with open(log_path, "w", encoding="utf-8") as handle:
                handle.write("ERROR: device disconnected\n")
            with usb_android_monitor.MIRROR_LOCK:
                usb_android_monitor.MIRROR_SCRCPY_PROCESSES["SERIAL1"] = ExitedProcess()  # type: ignore[assignment]
                usb_android_monitor.MIRROR_SCRCPY_LOG_FILES["SERIAL1"] = log_path
                usb_android_monitor.MIRROR_AUTO_ALL = True

            usb_android_monitor.handle_scrcpy_exit(
                "SERIAL1",
                ExitedProcess(),  # type: ignore[arg-type]
                {"SERIAL1"},
                {"SERIAL1"},
            )

        with usb_android_monitor.MIRROR_LOCK:
            self.assertIn("SERIAL1", usb_android_monitor.MIRROR_DISABLED_SERIALS)
            self.assertIn("SERIAL1", usb_android_monitor.MIRROR_FAILED_SERIALS)
            self.assertNotIn("SERIAL1", usb_android_monitor.MIRROR_SCRCPY_PROCESSES)
        self.assertEqual(last_actions_snapshot()[0]["action"], "scrcpy mirror exited")


if __name__ == "__main__":
    unittest.main()
