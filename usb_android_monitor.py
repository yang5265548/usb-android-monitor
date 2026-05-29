#!/usr/bin/env python3
"""Monitor Android phones connected by USB, including hub context when available."""

from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlparse


ANDROID_VENDOR_IDS = {
    "0x04e8": "Samsung",
    "0x05c6": "Qualcomm",
    "0x0bb4": "HTC",
    "0x0e8d": "MediaTek",
    "0x12d1": "Huawei",
    "0x18d1": "Google/Android",
    "0x19d2": "ZTE",
    "0x22b8": "Motorola",
    "0x2717": "Xiaomi",
    "0x2a70": "OnePlus",
    "0x2d95": "Nothing",
    "0x2e04": "HMD/Nokia",
}

ANDROID_TEXT_MARKERS = (
    "android",
    "adb",
    "mtp",
    "ptp",
    "samsung",
    "pixel",
    "google",
    "xiaomi",
    "redmi",
    "oneplus",
    "oppo",
    "vivo",
    "huawei",
    "honor",
    "realme",
    "motorola",
    "nothing",
    "nokia",
)

HUB_TEXT_MARKERS = ("hub", "multiport", "dock", "adapter")

LAST_ACTIONS: list[dict[str, Any]] = []
AUTO_RECONNECT_ENABLED = True
AUTO_RECONNECT_ATTEMPTS: dict[str, float] = {}
CONFIG_PATH = os.environ.get("USB_ANDROID_MONITOR_CONFIG", "usb_android_monitor_config.json")


@dataclass(frozen=True)
class UsbDevice:
    key: str
    name: str
    vendor_id: str
    vendor_name: str
    product_id: str
    manufacturer: str
    serial: str
    location_id: str
    bcd_device: str
    path: list[str]
    parent_hubs: list[str]
    is_hub: bool
    is_android_candidate: bool
    detection_reasons: list[str]


@dataclass(frozen=True)
class AdbDevice:
    serial: str
    state: str
    model: str
    device: str
    product: str
    transport_id: str
    usb_path: str
    behind_hub: bool
    hub_evidence: list[str]
    raw: str


def record_action(action: str, ok: bool, message: str, serial: str = "") -> dict[str, Any]:
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "serial": serial,
        "ok": ok,
        "message": message,
    }
    LAST_ACTIONS.insert(0, entry)
    del LAST_ACTIONS[20:]
    return entry


def run_command(args: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)


def run_json_command(args: list[str]) -> dict[str, Any]:
    result = run_command(args)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or f"command failed: {' '.join(args)}")
    return json.loads(result.stdout)


def load_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"auto_recovery": {"enabled": True, "power_cycle_missing": False}, "devices": {}}
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    config.setdefault("auto_recovery", {})
    config.setdefault("devices", {})
    config["auto_recovery"].setdefault("enabled", True)
    config["auto_recovery"].setdefault("power_cycle_missing", False)
    config["auto_recovery"].setdefault("cooldown_seconds", 60)
    return config


def configured_devices(config: dict[str, Any]) -> dict[str, dict[str, Any]]:
    devices = config.get("devices", {})
    return devices if isinstance(devices, dict) else {}


def split_vendor(raw: str) -> tuple[str, str]:
    if not raw:
        return "", ""
    first = raw.split()[0].strip()
    if "(" in raw and ")" in raw:
        return first, raw[raw.find("(") + 1 : raw.rfind(")")].strip()
    return first, ""


def text_blob(item: dict[str, Any]) -> str:
    fields = [
        item.get("_name", ""),
        item.get("name", ""),
        item.get("manufacturer", ""),
        item.get("vendor_id", ""),
        item.get("product_id", ""),
        item.get("serial_num", ""),
        item.get("serial", ""),
    ]
    return " ".join(str(field).lower() for field in fields if field)


def is_hub(item: dict[str, Any]) -> bool:
    blob = text_blob(item)
    return any(marker in blob for marker in HUB_TEXT_MARKERS) or bool(item.get("_items"))


def android_reasons(item: dict[str, Any], vendor_id: str) -> list[str]:
    reasons: list[str] = []
    if vendor_id in ANDROID_VENDOR_IDS:
        reasons.append(f"known Android vendor id {vendor_id} ({ANDROID_VENDOR_IDS[vendor_id]})")
    blob = text_blob(item)
    for marker in ANDROID_TEXT_MARKERS:
        if marker in blob:
            reasons.append(f"USB metadata contains '{marker}'")
            break
    return reasons


def flatten_usb_tree(data: dict[str, Any]) -> list[UsbDevice]:
    """Flatten macOS system_profiler-style USB trees."""
    devices: list[UsbDevice] = []

    def walk(items: list[dict[str, Any]], path: list[str], hubs: list[str]) -> None:
        for item in items:
            name = str(item.get("_name") or item.get("name") or "Unknown USB Device")
            current_path = [*path, name]
            vendor_id, vendor_name = split_vendor(str(item.get("vendor_id", "")))
            hub = is_hub(item)
            reasons = android_reasons(item, vendor_id)
            location_id = str(item.get("location_id", ""))
            serial = str(item.get("serial_num") or item.get("serial") or "")
            product_id = str(item.get("product_id", ""))
            key_parts = [location_id, vendor_id, product_id, serial, "/".join(current_path)]
            key = "|".join(part for part in key_parts if part)
            devices.append(
                UsbDevice(
                    key=key,
                    name=name,
                    vendor_id=vendor_id,
                    vendor_name=vendor_name,
                    product_id=product_id,
                    manufacturer=str(item.get("manufacturer", "")),
                    serial=serial,
                    location_id=location_id,
                    bcd_device=str(item.get("bcd_device", "")),
                    path=current_path,
                    parent_hubs=hubs,
                    is_hub=hub,
                    is_android_candidate=bool(reasons) and not hub,
                    detection_reasons=reasons,
                )
            )
            child_hubs = [*hubs, name] if hub else hubs
            children = item.get("_items", [])
            if isinstance(children, list):
                walk(children, current_path, child_hubs)

    roots = data.get("SPUSBDataType", [])
    if isinstance(roots, list):
        walk(roots, [], [])
    return devices


def parse_adb_devices(output: str) -> list[AdbDevice]:
    devices: list[AdbDevice] = []
    for line in output.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        parts = line.split()
        details: dict[str, str] = {}
        for part in parts[2:]:
            if ":" in part:
                key, value = part.split(":", 1)
                details[key] = value
        serial = parts[0]
        state = parts[1] if len(parts) > 1 else "unknown"
        usb_path = details.get("usb", "")
        hub_evidence = hub_evidence_from_adb_usb_path(usb_path)
        devices.append(
            AdbDevice(
                serial=serial,
                state=state,
                model=details.get("model", ""),
                device=details.get("device", ""),
                product=details.get("product", ""),
                transport_id=details.get("transport_id", ""),
                usb_path=usb_path,
                behind_hub=bool(hub_evidence),
                hub_evidence=hub_evidence,
                raw=line,
            )
        )
    return devices


def hub_evidence_from_adb_usb_path(usb_path: str) -> list[str]:
    if not usb_path:
        return []
    evidence = [f"adb usb path: {usb_path}"]
    path_only = usb_path.split(":", 1)[0]
    if "." in path_only:
        evidence.append("Linux USB port path contains a dot, which usually means a downstream hub")
    return evidence if "." in path_only else []


def get_adb_devices() -> list[AdbDevice]:
    if not shutil.which("adb"):
        return []
    result = run_command(["adb", "devices", "-l"], timeout=10)
    if result.returncode != 0:
        record_action("adb devices", False, result.stderr.strip() or result.stdout.strip())
        return []
    return parse_adb_devices(result.stdout)


def get_macos_usb_devices() -> list[UsbDevice]:
    if not shutil.which("system_profiler"):
        return []
    return flatten_usb_tree(run_json_command(["system_profiler", "SPUSBDataType", "-json"]))


def get_linux_usb_devices() -> list[UsbDevice]:
    if not shutil.which("lsusb"):
        return []
    result = run_command(["lsusb", "-t"], timeout=5)
    if result.returncode != 0:
        return []
    devices: list[UsbDevice] = []
    stack: list[tuple[int, str, bool]] = []
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        depth = max(0, (len(line) - len(line.lstrip(" "))) // 4)
        while stack and stack[-1][0] >= depth:
            stack.pop()
        name = stripped.replace("|__", "").strip()
        hub = "hub" in name.lower()
        hubs = [entry[1] for entry in stack if entry[2]]
        devices.append(
            UsbDevice(
                key=name,
                name=name,
                vendor_id="",
                vendor_name="",
                product_id="",
                manufacturer="",
                serial="",
                location_id="",
                bcd_device="",
                path=[entry[1] for entry in stack] + [name],
                parent_hubs=hubs,
                is_hub=hub,
                is_android_candidate=False,
                detection_reasons=[],
            )
        )
        stack.append((depth, name, hub))
    return devices


def get_windows_usb_devices() -> list[UsbDevice]:
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        return []
    script = (
        "Get-PnpDevice -PresentOnly | "
        "Where-Object { $_.InstanceId -like 'USB*' } | "
        "Select-Object FriendlyName,InstanceId,Class,Manufacturer | "
        "ConvertTo-Json -Compress"
    )
    result = run_command([powershell, "-NoProfile", "-Command", script], timeout=10)
    if result.returncode != 0 or not result.stdout.strip():
        return []
    try:
        raw = json.loads(result.stdout)
    except json.JSONDecodeError:
        return []
    items = raw if isinstance(raw, list) else [raw]
    devices: list[UsbDevice] = []
    for item in items:
        name = str(item.get("FriendlyName") or item.get("InstanceId") or "USB device")
        instance_id = str(item.get("InstanceId") or "")
        lower_name = name.lower()
        hub = any(marker in lower_name for marker in HUB_TEXT_MARKERS)
        vendor_id = ""
        product_id = ""
        for segment in instance_id.split("\\"):
            upper = segment.upper()
            if upper.startswith("VID_"):
                vendor_id = "0x" + upper[4:8].lower()
            if upper.startswith("PID_"):
                product_id = "0x" + upper[4:8].lower()
        reasons = android_reasons({"_name": name, "vendor_id": vendor_id}, vendor_id)
        devices.append(
            UsbDevice(
                key=instance_id or name,
                name=name,
                vendor_id=vendor_id,
                vendor_name=ANDROID_VENDOR_IDS.get(vendor_id, ""),
                product_id=product_id,
                manufacturer=str(item.get("Manufacturer") or ""),
                serial=instance_id.split("\\")[-1] if "\\" in instance_id else "",
                location_id=instance_id,
                bcd_device="",
                path=[name],
                parent_hubs=[],
                is_hub=hub,
                is_android_candidate=bool(reasons) and not hub,
                detection_reasons=reasons,
            )
        )
    return devices


def get_usb_devices() -> tuple[list[UsbDevice], str]:
    system = platform.system().lower()
    try:
        if system == "darwin":
            return get_macos_usb_devices(), "macos-system-profiler"
        if system == "linux":
            return get_linux_usb_devices(), "linux-lsusb"
        if system == "windows":
            return get_windows_usb_devices(), "windows-pnp"
    except Exception as exc:
        record_action("usb scan", False, str(exc))
    return [], "unsupported"


def enrich_adb_with_usb(adb_devices: list[AdbDevice], usb_devices: list[UsbDevice]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    for device in adb_devices:
        row = asdict(device)
        matches = [
            usb
            for usb in usb_devices
            if device.serial and device.serial.lower() in (usb.serial + " " + usb.location_id + " " + usb.key).lower()
        ]
        if matches:
            match = matches[0]
            row["system_usb_match"] = asdict(match)
            if match.parent_hubs:
                row["behind_hub"] = True
                row["hub_evidence"] = [*row["hub_evidence"], f"system USB parent hubs: {' > '.join(match.parent_hubs)}"]
        else:
            row["system_usb_match"] = None
        row["needs_attention"] = row["state"] in {"offline", "unauthorized", "unknown"}
        return_hint = "ready"
        if row["state"] == "unauthorized":
            return_hint = "unlock phone and accept USB debugging authorization"
        elif row["state"] == "offline":
            return_hint = "ADB session is offline; reconnect can restart the ADB transport"
        row["status_hint"] = return_hint
        enriched.append(row)
    return enriched


def reconnect_device(serial: str = "") -> dict[str, Any]:
    if not shutil.which("adb"):
        return record_action("reconnect", False, "adb is not installed or not on PATH", serial)
    commands = [["adb", "start-server"]]
    if serial:
        commands.append(["adb", "-s", serial, "reconnect"])
    else:
        commands.extend([["adb", "reconnect", "offline"], ["adb", "reconnect", "device"]])
    messages: list[str] = []
    ok = True
    for command in commands:
        result = run_command(command, timeout=12)
        output = (result.stdout + result.stderr).strip()
        messages.append(f"{' '.join(command)} -> {output or result.returncode}")
        ok = ok and result.returncode == 0
    return record_action("reconnect", ok, " | ".join(messages), serial)


def recovery_plan_for_serial(serial: str, config: dict[str, Any]) -> list[str]:
    device_config = configured_devices(config).get(serial, {})
    steps = ["adb start-server", "adb reconnect"]
    uhubctl = device_config.get("uhubctl", {})
    if uhubctl.get("enabled", True) and uhubctl.get("location") and uhubctl.get("port"):
        steps.append(f"uhubctl cycle location={uhubctl['location']} port={uhubctl['port']}")
    windows_instance_id = device_config.get("windows_instance_id", "")
    if windows_instance_id:
        steps.append(f"pnputil restart-device {windows_instance_id}")
    return steps


def power_cycle_linux_hub_port(serial: str, device_config: dict[str, Any]) -> dict[str, Any]:
    uhubctl = device_config.get("uhubctl", {})
    if not uhubctl.get("location") or not uhubctl.get("port"):
        return record_action("power-cycle", False, "missing uhubctl location or port in config", serial)
    if not shutil.which("uhubctl"):
        return record_action("power-cycle", False, "uhubctl is not installed or not on PATH", serial)
    command = [
        "uhubctl",
        "-l",
        str(uhubctl["location"]),
        "-p",
        str(uhubctl["port"]),
        "-a",
        "cycle",
    ]
    result = run_command(command, timeout=20)
    output = (result.stdout + result.stderr).strip()
    return record_action("power-cycle", result.returncode == 0, output or str(result.returncode), serial)


def restart_windows_usb_device(serial: str, device_config: dict[str, Any]) -> dict[str, Any]:
    instance_id = device_config.get("windows_instance_id", "")
    if not instance_id:
        return record_action("windows-restart", False, "missing windows_instance_id in config", serial)
    if not shutil.which("pnputil"):
        return record_action("windows-restart", False, "pnputil is not available", serial)
    result = run_command(["pnputil", "/restart-device", instance_id], timeout=20)
    output = (result.stdout + result.stderr).strip()
    return record_action("windows-restart", result.returncode == 0, output or str(result.returncode), serial)


def recover_device(serial: str = "", reason: str = "manual", allow_power_cycle: bool = True) -> dict[str, Any]:
    config = load_config()
    device_config = configured_devices(config).get(serial, {})
    messages = [f"reason={reason}"]
    first = reconnect_device(serial)
    messages.append(first["message"])

    system = platform.system().lower()
    if allow_power_cycle and serial and device_config:
        if system == "linux" and device_config.get("uhubctl"):
            result = power_cycle_linux_hub_port(serial, device_config)
            messages.append(result["message"])
            time.sleep(2)
            messages.append(reconnect_device(serial)["message"])
        elif system == "windows" and device_config.get("windows_instance_id"):
            result = restart_windows_usb_device(serial, device_config)
            messages.append(result["message"])
            time.sleep(2)
            messages.append(reconnect_device(serial)["message"])
    ok = any(action.get("ok") for action in LAST_ACTIONS[:4] if action.get("serial") in {serial, ""})
    return record_action("recover", ok, " | ".join(messages), serial)


def disconnect_device(serial: str) -> dict[str, Any]:
    if not serial:
        return record_action("disconnect", False, "serial is required")
    if not shutil.which("adb"):
        return record_action("disconnect", False, "adb is not installed or not on PATH", serial)
    command = ["adb", "-s", serial, "shell", "svc", "usb", "setFunctions", "none"]
    result = run_command(command, timeout=8)
    output = (result.stdout + result.stderr).strip()
    if result.returncode == 0:
        return record_action("disconnect", True, output or "USB data functions disabled on device", serial)
    fallback = run_command(["adb", "kill-server"], timeout=8)
    fallback_output = (fallback.stdout + fallback.stderr).strip()
    return record_action(
        "disconnect",
        False,
        f"device-level USB disconnect failed: {output or result.returncode}; fallback killed adb server: {fallback_output or fallback.returncode}",
        serial,
    )


def auto_reconnect_if_needed(devices: list[dict[str, Any]], config: dict[str, Any]) -> None:
    if not AUTO_RECONNECT_ENABLED:
        return
    auto_config = config.get("auto_recovery", {})
    if not auto_config.get("enabled", True):
        return
    interval_seconds = int(auto_config.get("cooldown_seconds", 60))
    power_cycle_missing = bool(auto_config.get("power_cycle_missing", False))
    now = time.time()
    for device in devices:
        serial = device["serial"]
        if device["state"] not in {"offline", "unknown"}:
            continue
        if now - AUTO_RECONNECT_ATTEMPTS.get(serial, 0) < interval_seconds:
            continue
        AUTO_RECONNECT_ATTEMPTS[serial] = now
        threading.Thread(target=recover_device, args=(serial, "adb-state", False), daemon=True).start()

    current_serials = {device["serial"] for device in devices}
    for serial in configured_devices(config):
        if serial in current_serials:
            continue
        if now - AUTO_RECONNECT_ATTEMPTS.get(serial, 0) < interval_seconds:
            continue
        AUTO_RECONNECT_ATTEMPTS[serial] = now
        threading.Thread(
            target=recover_device,
            args=(serial, "configured-device-missing", power_cycle_missing),
            daemon=True,
        ).start()


def snapshot() -> dict[str, Any]:
    config = load_config()
    usb_devices, usb_backend = get_usb_devices()
    adb_devices = get_adb_devices()
    android_devices = enrich_adb_with_usb(adb_devices, usb_devices)
    auto_reconnect_if_needed(android_devices, config)
    usb_android = [device for device in usb_devices if device.is_android_candidate]
    current_serials = {device["serial"] for device in android_devices}
    missing_configured = [
        {
            "serial": serial,
            "name": str(device_config.get("name") or serial),
            "recovery_plan": recovery_plan_for_serial(serial, config),
            "last_attempt_at": AUTO_RECONNECT_ATTEMPTS.get(serial, 0),
        }
        for serial, device_config in configured_devices(config).items()
        if serial not in current_serials
    ]
    behind_hub_count = sum(1 for device in android_devices if device["behind_hub"]) + sum(
        1 for device in usb_android if device.parent_hubs
    )
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": platform.system(),
        "usb_backend": usb_backend,
        "adb_available": bool(shutil.which("adb")),
        "auto_reconnect_enabled": AUTO_RECONNECT_ENABLED,
        "config_path": CONFIG_PATH,
        "configured_device_count": len(configured_devices(config)),
        "last_actions": LAST_ACTIONS,
        "summary": {
            "total_usb_devices": len(usb_devices),
            "hubs": sum(1 for device in usb_devices if device.is_hub),
            "adb_android_devices": len(android_devices),
            "usb_android_candidates": len(usb_android),
            "android_behind_hub": behind_hub_count,
            "configured_missing": len(missing_configured),
            "attention": sum(1 for device in android_devices if device["needs_attention"]),
        },
        "usb_devices": [asdict(device) for device in usb_devices],
        "android_devices": android_devices,
        "missing_configured_devices": missing_configured,
        "usb_android_candidates": [asdict(device) for device in usb_android],
    }


def print_table(state: dict[str, Any]) -> None:
    summary = state["summary"]
    print(f"Time: {state['timestamp']} ({state['platform']}, {state['usb_backend']})")
    print(f"ADB available: {'yes' if state['adb_available'] else 'no'}")
    print(
        "ADB Android devices: {adb_android_devices}, USB Android candidates: "
        "{usb_android_candidates}, behind hub: {android_behind_hub}, "
        "configured missing: {configured_missing}, attention: {attention}".format(**summary)
    )
    print()
    if state["android_devices"]:
        print("Android devices:")
        for device in state["android_devices"]:
            hub = "yes" if device["behind_hub"] else "unknown/no"
            print(
                f"  [{device['state']}] {device['serial']} model={device['model'] or '-'} "
                f"product={device['product'] or '-'} usb={device['usb_path'] or '-'} behind_hub={hub}"
            )
            if device["hub_evidence"]:
                print(f"    evidence: {'; '.join(device['hub_evidence'])}")
            if device["status_hint"] != "ready":
                print(f"    hint: {device['status_hint']}")
    else:
        print("Android devices: none")
    if state["missing_configured_devices"]:
        print()
        print("Configured devices missing from ADB:")
        for device in state["missing_configured_devices"]:
            print(f"  {device['serial']} ({device['name']})")
            print(f"    recovery: {' -> '.join(device['recovery_plan'])}")
    print()
    print("USB devices:")
    for device in state["usb_devices"]:
        marker = "ANDROID" if device["is_android_candidate"] else "HUB" if device["is_hub"] else "USB"
        print(f"  [{marker}] {' > '.join(device['path'])}")


def adb_signature(device: dict[str, Any]) -> tuple[str, str, str, bool]:
    return (device["serial"], device["state"], device["usb_path"], bool(device["behind_hub"]))


def watch(interval: float) -> None:
    previous: dict[str, tuple[str, str, str, bool]] = {}
    print("Watching Android USB devices. Press Ctrl-C to stop.")
    while True:
        state = snapshot()
        current = {device["serial"]: adb_signature(device) for device in state["android_devices"]}
        added = [serial for serial in current if serial not in previous]
        removed = [serial for serial in previous if serial not in current]
        changed = [serial for serial in current if serial in previous and current[serial] != previous[serial]]
        if added or removed or changed:
            print(f"\n{state['timestamp']}")
            for serial in added:
                print(f"+ connected: {serial} state={current[serial][1]} usb={current[serial][2] or '-'}")
            for serial in removed:
                print(f"- disconnected: {serial}")
            for serial in changed:
                print(f"* changed: {serial} {previous[serial]} -> {current[serial]}")
        previous = current
        time.sleep(interval)


INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>USB Android Monitor</title>
  <style>
    :root { color-scheme: light dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #f4f6f7; color: #162027; }
    header { padding: 16px 22px; background: #ffffff; border-bottom: 1px solid #d9e0e5; }
    h1 { margin: 0; font-size: 20px; }
    main { padding: 18px 22px; max-width: 1180px; margin: 0 auto; }
    button { border: 1px solid #b9c4cc; background: #ffffff; color: inherit; border-radius: 6px; padding: 7px 10px; cursor: pointer; }
    button:hover { background: #eef3f5; }
    .danger { border-color: #cc9d9d; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 10px; margin-bottom: 14px; }
    .stat, .device, .notice { background: #ffffff; border: 1px solid #d9e0e5; border-radius: 8px; padding: 13px; }
    .stat b { display: block; font-size: 24px; margin-top: 3px; }
    .section-title { margin: 20px 0 9px; font-size: 16px; }
    .devices { display: grid; gap: 10px; }
    .device.ready { border-left: 5px solid #16834a; }
    .device.warn { border-left: 5px solid #c47a21; }
    .device.hub { border-left: 5px solid #5b6b7a; }
    .name { font-weight: 700; margin-bottom: 7px; }
    .meta { color: #53616d; font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .tag { display: inline-block; padding: 2px 7px; border-radius: 999px; background: #e8eef1; margin-left: 8px; font-size: 12px; }
    @media (prefers-color-scheme: dark) {
      body { background: #101519; color: #e7ecef; }
      header, .stat, .device, .notice, button { background: #171d22; border-color: #2c363f; }
      button:hover { background: #202932; }
      .meta { color: #a8b3bc; }
      .tag { background: #28323a; }
    }
  </style>
</head>
<body>
  <header><h1>USB Android Monitor</h1><div id="updated" class="meta"></div></header>
  <main>
    <section class="stats" id="stats"></section>
    <section class="notice" id="notice"></section>
    <h2 class="section-title">Android Devices</h2>
    <section class="devices" id="android"></section>
    <h2 class="section-title">Configured Devices Missing From ADB</h2>
    <section class="devices" id="missing"></section>
    <h2 class="section-title">USB / Hub Context</h2>
    <section class="devices" id="usb"></section>
    <h2 class="section-title">Recent Actions</h2>
    <section class="devices" id="actions"></section>
  </main>
  <script>
    const statsEl = document.querySelector("#stats");
    const androidEl = document.querySelector("#android");
    const usbEl = document.querySelector("#usb");
    const missingEl = document.querySelector("#missing");
    const updatedEl = document.querySelector("#updated");
    const noticeEl = document.querySelector("#notice");
    const actionsEl = document.querySelector("#actions");

    function stat(label, value) {
      return `<div class="stat"><span>${label}</span><b>${value}</b></div>`;
    }

    async function doAction(action, serial = "") {
      const response = await fetch("/api/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, serial })
      });
      await response.json();
      await refresh();
    }

    function androidCard(device) {
      const cls = device.needs_attention ? "warn" : "ready";
      const hub = device.behind_hub ? "Behind hub" : "Hub unknown";
      const evidence = device.hub_evidence.length ? `<div class="meta">Hub evidence: ${device.hub_evidence.join("; ")}</div>` : "";
      return `<article class="device ${cls}">
        <div class="name">${device.serial}<span class="tag">${device.state}</span><span class="tag">${hub}</span></div>
        <div class="meta">Model: ${device.model || "-"} · Product: ${device.product || "-"} · Device: ${device.device || "-"}</div>
        <div class="meta">ADB USB path: ${device.usb_path || "-"} · Transport: ${device.transport_id || "-"}</div>
        <div class="meta">${device.status_hint}</div>
        ${evidence}
        <div class="actions">
          <button onclick="doAction('reconnect', '${device.serial}')">Reconnect</button>
          <button onclick="doAction('recover', '${device.serial}')">Recover</button>
          <button class="danger" onclick="doAction('disconnect', '${device.serial}')">Disconnect USB data</button>
        </div>
      </article>`;
    }

    function missingCard(device) {
      return `<article class="device warn">
        <div class="name">${device.name}<span class="tag">${device.serial}</span></div>
        <div class="meta">Recovery plan: ${device.recovery_plan.join(" -> ")}</div>
        <div class="actions">
          <button onclick="doAction('recover', '${device.serial}')">Recover</button>
        </div>
      </article>`;
    }

    function usbCard(device) {
      const cls = device.is_hub ? "hub" : "";
      const tag = device.is_hub ? "Hub" : device.is_android_candidate ? "Android candidate" : "USB";
      const via = device.parent_hubs.length ? `<div class="meta">Via hub: ${device.parent_hubs.join(" > ")}</div>` : "";
      return `<article class="device ${cls}">
        <div class="name">${device.name}<span class="tag">${tag}</span></div>
        <div class="meta">${device.path.join(" > ")}</div>
        <div class="meta">Vendor: ${device.vendor_id || "-"} · Product: ${device.product_id || "-"} · Serial: ${device.serial || "-"}</div>
        ${via}
      </article>`;
    }

    function actionCard(action) {
      return `<article class="device ${action.ok ? "ready" : "warn"}">
        <div class="name">${action.action}<span class="tag">${action.ok ? "ok" : "failed"}</span></div>
        <div class="meta">${action.timestamp} · ${action.serial || "all devices"}</div>
        <div class="meta">${action.message}</div>
      </article>`;
    }

    async function refresh() {
      const response = await fetch("/api/state");
      const state = await response.json();
      updatedEl.textContent = `Updated ${state.timestamp} · ${state.platform} · USB backend ${state.usb_backend} · ADB ${state.adb_available ? "available" : "not installed"}`;
      noticeEl.innerHTML = state.adb_available
        ? `Auto recovery is ${state.auto_reconnect_enabled ? "enabled" : "disabled"}. Configured devices: ${state.configured_device_count}. Config file: ${state.config_path}.`
        : `Install Android platform-tools and make sure adb is on PATH before using reconnect controls.`;
      statsEl.innerHTML = [
        stat("ADB Android", state.summary.adb_android_devices),
        stat("Behind hub", state.summary.android_behind_hub),
        stat("Configured missing", state.summary.configured_missing),
        stat("Needs attention", state.summary.attention),
        stat("USB hubs", state.summary.hubs)
      ].join("");
      androidEl.innerHTML = state.android_devices.length
        ? state.android_devices.map(androidCard).join("")
        : `<article class="device"><div class="name">No ADB Android devices detected</div><div class="meta">Connect the phone by USB, enable USB debugging, and authorize this computer.</div><div class="actions"><button onclick="doAction('reconnect')">Restart ADB discovery</button></div></article>`;
      missingEl.innerHTML = state.missing_configured_devices.length
        ? state.missing_configured_devices.map(missingCard).join("")
        : `<article class="device ready"><div class="name">No configured devices are missing</div></article>`;
      usbEl.innerHTML = state.usb_devices.length
        ? state.usb_devices.map(usbCard).join("")
        : `<article class="device"><div class="name">No USB context available</div><div class="meta">Install lsusb on Ubuntu, or run on Windows with PowerShell available.</div></article>`;
      actionsEl.innerHTML = state.last_actions.length
        ? state.last_actions.map(actionCard).join("")
        : `<article class="device"><div class="name">No actions yet</div></article>`;
    }

    refresh();
    setInterval(refresh, 2000);
  </script>
</body>
</html>
"""


class MonitorHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/state":
            body = json.dumps(snapshot(), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path in {"/", "/index.html"}:
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path != "/api/action":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        action = payload.get("action", "")
        serial = payload.get("serial", "")
        if action == "reconnect":
            result = reconnect_device(serial)
        elif action == "recover":
            result = recover_device(serial)
        elif action == "disconnect":
            result = disconnect_device(serial)
        else:
            result = record_action(action or "unknown", False, "unsupported action", serial)
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def serve(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), MonitorHandler)
    print(f"USB Android Monitor running at http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping.")
    finally:
        server.server_close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="print the current USB and ADB state once")

    watch_parser = subparsers.add_parser("watch", help="print Android connect/disconnect/change events")
    watch_parser.add_argument("--interval", type=float, default=2.0, help="poll interval in seconds")

    serve_parser = subparsers.add_parser("serve", help="start the local web dashboard")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=8765)

    reconnect_parser = subparsers.add_parser("reconnect", help="restart ADB discovery or reconnect one device")
    reconnect_parser.add_argument("--serial", default="")

    recover_parser = subparsers.add_parser("recover", help="run the recovery ladder for one configured device")
    recover_parser.add_argument("serial")

    disconnect_parser = subparsers.add_parser("disconnect", help="try to disable USB data on one Android device")
    disconnect_parser.add_argument("serial")

    args = parser.parse_args()
    if args.command == "list":
        print_table(snapshot())
    elif args.command == "watch":
        watch(args.interval)
    elif args.command == "serve":
        serve(args.host, args.port)
    elif args.command == "reconnect":
        print(json.dumps(reconnect_device(args.serial), ensure_ascii=False, indent=2))
    elif args.command == "recover":
        print(json.dumps(recover_device(args.serial), ensure_ascii=False, indent=2))
    elif args.command == "disconnect":
        print(json.dumps(disconnect_device(args.serial), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
