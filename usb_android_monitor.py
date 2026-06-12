#!/usr/bin/env python3
"""Monitor Android phones connected by USB, including hub context when available."""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import platform
import shlex
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
ACTIVE_ACTIONS: dict[str, dict[str, Any]] = {}
ACTION_LOCK = threading.Lock()
ADB_EVENT_STATE: dict[str, tuple[str, str, str, bool]] = {}
ADB_EVENT_LOG_INITIALIZED = False
AUTO_RECONNECT_ENABLED = True
AUTO_RECONNECT_ATTEMPTS: dict[str, float] = {}
MANUAL_DISCONNECT_UNTIL: dict[str, float] = {}
DISCONNECTED_TARGETS: dict[str, dict[str, str]] = {}
CONFIG_PATH = os.environ.get("USB_ANDROID_MONITOR_CONFIG", "usb_android_monitor_config.json")
STATE_PATH = os.environ.get("USB_ANDROID_MONITOR_STATE", "usb_android_monitor_state.json")
LOG_DIR = os.environ.get("USB_ANDROID_MONITOR_LOG_DIR", "logs")
LOG_ENABLED = os.environ.get("USB_ANDROID_MONITOR_LOG_ENABLED", "1") != "0"
LOG_LOCK = threading.Lock()
EVENT_HISTORY: list[dict[str, Any]] = []
LAST_ADB_RAW_OUTPUT = ""
COMMAND_LOG_OUTPUT_LIMIT = 4000
RUN_STARTED_AT = dt.datetime.now().astimezone()
RUN_ID = os.environ.get("USB_ANDROID_MONITOR_RUN_ID", f"{RUN_STARTED_AT:%Y%m%d-%H%M%S}-pid{os.getpid()}")
LATEST_LOGS_INITIALIZED = False
HUB_BACKEND = os.environ.get("USB_ANDROID_MONITOR_HUB_BACKEND", "auto").strip().lower() or "auto"
MIRROR_SCRIPT_PATH = os.environ.get("USB_ANDROID_MONITOR_MIRROR_SCRIPT", "")
MIRROR_SCRCPY_DIR = os.environ.get("USB_ANDROID_MONITOR_SCRCPY_DIR", r"C:\Users\digitaltwin\Desktop\scrcpy-win64-v3.3.4")
MIRROR_RUNTIME_DIR = os.environ.get("USB_ANDROID_MONITOR_MIRROR_RUNTIME_DIR", "mirror_runtime")
DEFAULT_SCRCPY_ARGS = ["--no-audio", "--max-size", "1280", "--max-fps", "30", "--video-bit-rate", "4M"]
MIRROR_MONITOR_THREAD: threading.Thread | None = None
MIRROR_STOP_EVENT = threading.Event()
MIRROR_SCRCPY_PROCESSES: dict[str, subprocess.Popen[str]] = {}
MIRROR_SCRCPY_LOG_FILES: dict[str, str] = {}
MIRROR_AUTO_ALL = False
MIRROR_TARGET_SERIALS: set[str] = set()
MIRROR_DISABLED_SERIALS: set[str] = set()
MIRROR_FAILED_SERIALS: dict[str, str] = {}
MIRROR_LOG_FILE = ""
MIRROR_LOCK = threading.Lock()


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


def record_action(action: str, ok: bool, message: str, serial: str = "", status: str = "") -> dict[str, Any]:
    entry = {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "serial": serial,
        "ok": ok,
        "status": status or ("ok" if ok else "failed"),
        "message": message,
    }
    with ACTION_LOCK:
        LAST_ACTIONS.insert(0, entry)
        del LAST_ACTIONS[30:]
    write_log("action", entry)
    return entry


def now_iso() -> str:
    return dt.datetime.now().astimezone().isoformat(timespec="milliseconds")


def log_file_path(timestamp: dt.datetime | None = None) -> str:
    stamp = timestamp or RUN_STARTED_AT
    return os.path.join(LOG_DIR, f"{stamp:%Y-%m-%d}", f"run-{RUN_ID}.jsonl")


def text_log_file_path(timestamp: dt.datetime | None = None) -> str:
    stamp = timestamp or RUN_STARTED_AT
    return os.path.join(LOG_DIR, f"{stamp:%Y-%m-%d}", f"run-{RUN_ID}.log")


def latest_jsonl_log_path() -> str:
    return os.path.join(LOG_DIR, "latest.jsonl")


def latest_text_log_path() -> str:
    return os.path.join(LOG_DIR, "latest.log")


def initialize_latest_logs() -> None:
    global LATEST_LOGS_INITIALIZED

    if LATEST_LOGS_INITIALIZED:
        return
    os.makedirs(LOG_DIR, exist_ok=True)
    for path in (latest_jsonl_log_path(), latest_text_log_path()):
        try:
            with open(path, "w", encoding="utf-8"):
                pass
        except OSError:
            pass
    LATEST_LOGS_INITIALIZED = True


def truncate_text(value: Any, limit: int = COMMAND_LOG_OUTPUT_LIMIT) -> str:
    if value is None:
        text = ""
    elif isinstance(value, bytes):
        text = value.decode(errors="replace")
    else:
        text = str(value)
    if len(text) <= limit:
        return text
    return text[:limit] + f"...<truncated {len(text) - limit} chars>"


def log_level_for(entry: dict[str, Any]) -> str:
    event = str(entry.get("event") or "")
    if entry.get("ok") is False or "failed" in event or "timeout" in event or "aborted" in str(entry.get("message", "")):
        return "ERROR"
    if entry.get("status") == "failed":
        return "ERROR"
    if event.endswith("_started") or entry.get("status") == "running":
        return "INFO"
    if "changed" in event or entry.get("status") == "event":
        return "EVENT"
    return "INFO"


def compact_log_value(value: Any) -> str:
    if isinstance(value, (list, tuple, set)):
        return "[" + ",".join(str(item) for item in value) + "]"
    if isinstance(value, dict):
        return "{" + ",".join(f"{key}:{value[key]}" for key in sorted(value)) + "}"
    return str(value)


def format_text_log(entry: dict[str, Any]) -> str:
    event = str(entry.get("event") or "-")
    level = log_level_for(entry)
    parts = [
        f"{entry.get('ts', '-')}",
        f"{level:<5}",
        event,
    ]
    for key in (
        "action",
        "serial",
        "target_serial",
        "backend",
        "hub_model",
        "hub_serial",
        "port",
        "ok",
        "status",
        "result_code",
        "duration_ms",
        "reason",
        "confidence",
        "affected_serials",
        "requested_serial",
        "disappeared_serials",
    ):
        if key in entry and entry[key] is not None and entry[key] != "":
            parts.append(f"{key}={compact_log_value(entry[key])}")
    message = entry.get("message")
    if message:
        parts.append(f"message={truncate_text(message, 800)}")
    elif event == "adb_snapshot_changed":
        parts.append("message=ADB device list changed")
    return " | ".join(parts)


def write_log(event: str, fields: dict[str, Any] | None = None) -> None:
    if not LOG_ENABLED:
        return
    entry = {
        "ts": now_iso(),
        "event": event,
        "pid": os.getpid(),
        "platform": platform.system(),
        "run_id": RUN_ID,
    }
    if fields:
        entry.update(fields)
    history_entry = dict(entry)
    history_entry["_mono"] = time.monotonic()
    try:
        with LOG_LOCK:
            EVENT_HISTORY.append(history_entry)
            del EVENT_HISTORY[:-500]
            os.makedirs(LOG_DIR, exist_ok=True)
            os.makedirs(os.path.dirname(log_file_path()), exist_ok=True)
            initialize_latest_logs()
            with open(log_file_path(), "a", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            with open(latest_jsonl_log_path(), "a", encoding="utf-8") as handle:
                json.dump(entry, handle, ensure_ascii=False, sort_keys=True)
                handle.write("\n")
            with open(text_log_file_path(), "a", encoding="utf-8") as handle:
                handle.write(format_text_log(entry))
                handle.write("\n")
            with open(latest_text_log_path(), "a", encoding="utf-8") as handle:
                handle.write(format_text_log(entry))
                handle.write("\n")
    except OSError:
        pass


def recent_log_entries(seconds: float = 30.0) -> list[dict[str, Any]]:
    cutoff = time.monotonic() - seconds
    with LOG_LOCK:
        return [dict(entry) for entry in EVENT_HISTORY if float(entry.get("_mono", 0)) >= cutoff]


def recent_persistent_logs(limit: int = 100) -> list[dict[str, Any]]:
    if not LOG_ENABLED or not os.path.isdir(LOG_DIR):
        return []
    try:
        paths = []
        for root, _, names in os.walk(LOG_DIR):
            for name in names:
                if name.startswith("run-") and name.endswith(".jsonl"):
                    paths.append(os.path.join(root, name))
    except OSError:
        return []
    lines: list[str] = []
    for path in sorted(paths)[-3:]:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                lines.extend(handle.readlines())
        except OSError:
            continue
    entries: list[dict[str, Any]] = []
    for line in lines[-limit:]:
        try:
            parsed = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            entries.append(parsed)
    return list(reversed(entries))


def collect_platform_usb_events(seconds: int = 60) -> dict[str, Any]:
    system = platform.system().lower()
    if system == "windows":
        powershell = shutil.which("powershell") or shutil.which("pwsh")
        if not powershell:
            return {"backend": "windows-eventlog", "ok": False, "message": "PowerShell is not available"}
        script = (
            "$start=(Get-Date).AddSeconds(-%d); "
            "$providers=@('Microsoft-Windows-Kernel-PnP','Microsoft-Windows-UserPnp','Microsoft-Windows-DriverFrameworks-UserMode'); "
            "Get-WinEvent -FilterHashtable @{StartTime=$start; ProviderName=$providers} -ErrorAction SilentlyContinue | "
            "Select-Object -First 30 TimeCreated,ProviderName,Id,LevelDisplayName,Message | ConvertTo-Json -Compress"
        ) % seconds
        result = run_command([powershell, "-NoProfile", "-Command", script], timeout=8)
        output = (result.stdout or result.stderr).strip()
        return {
            "backend": "windows-eventlog",
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "raw": truncate_text(output, 6000),
        }
    if system == "linux":
        if shutil.which("journalctl"):
            result = run_command(["journalctl", "-k", f"--since={seconds} seconds ago", "-n", "80", "--no-pager"], timeout=8)
            output = (result.stdout or result.stderr).strip()
            return {
                "backend": "journalctl-kernel",
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "raw": truncate_text(output, 6000),
            }
        if shutil.which("dmesg"):
            result = run_command(["dmesg", "--ctime", "--level=err,warn,notice,info"], timeout=8)
            output = "\n".join((result.stdout or result.stderr).splitlines()[-80:])
            return {
                "backend": "dmesg",
                "ok": result.returncode == 0,
                "returncode": result.returncode,
                "raw": truncate_text(output, 6000),
            }
    return {"backend": "unsupported", "ok": False, "message": f"no platform USB log collector for {platform.system()}"}


def diagnose_disconnect(
    serial: str,
    affected_serials: list[str],
    previous_signature: tuple[str, str, str, bool],
    system_events: dict[str, Any] | None = None,
) -> None:
    recent = recent_log_entries(30)
    evidence: list[str] = []
    reason = "unknown_external_disconnect"
    confidence = "low"

    has_map = any(entry.get("event") == "acroname_map_port_probe_started" for entry in recent) or any(
        entry.get("action") == "map-acroname" for entry in recent
    )
    has_manual_disconnect = any(
        entry.get("action") == "disconnect" and entry.get("serial") in {serial, ""}
        for entry in recent
    )
    hub_off = [
        entry
        for entry in recent
        if entry.get("event") == "hub_port_action_result" and entry.get("action") == "off"
    ]
    if has_map:
        reason = "auto_map_probe"
        confidence = "high"
        evidence.append("recent Acroname auto-map probe was running")
    elif has_manual_disconnect:
        reason = "manual_disconnect"
        confidence = "high"
        evidence.append("recent manual disconnect action for this serial")
    elif any(entry.get("ok") is True for entry in hub_off):
        reason = "hub_port_power_off"
        confidence = "high"
        evidence.append("recent hub port off action returned ok")
    elif len(affected_serials) > 1:
        reason = "hub_or_upstream_disconnect"
        confidence = "high"
        evidence.append(f"{len(affected_serials)} ADB serials disappeared in the same poll")
    else:
        evidence.append("serial disappeared from ADB without a matching recent software action")

    if system_events:
        backend = system_events.get("backend", "unknown")
        ok = system_events.get("ok")
        evidence.append(f"platform_usb_log backend={backend} ok={ok}")

    write_log(
        "disconnect_diagnosis",
        {
            "serial": serial,
            "affected_serials": affected_serials,
            "previous_state": previous_signature[0],
            "previous_usb_path": previous_signature[1],
            "previous_transport_id": previous_signature[2],
            "reason": reason,
            "confidence": confidence,
            "evidence": evidence,
            "system_events": system_events or {},
            "message": "; ".join(evidence),
        },
    )


def run_action_async(action: str, serial: str) -> dict[str, Any]:
    action_map = {
        "connect": connect_device,
        "reconnect": reconnect_device,
        "recover": recover_device,
        "verify": verify_device,
        "disconnect": disconnect_device,
        "map-acroname": map_acroname_ports,
        "start-mirror-script": start_mirror_script,
        "stop-mirror-script": stop_mirror_script,
        "start-mirror-device": start_mirror_device,
        "stop-mirror-device": stop_mirror_device,
    }
    target = action_map.get(action)
    if target is None:
        return record_action(action or "unknown", False, "unsupported action", serial)
    if action.startswith("start-mirror") or action.startswith("stop-mirror"):
        return target(serial)
    with ACTION_LOCK:
        for active in ACTIVE_ACTIONS.values():
            if active["serial"] == serial:
                return {
                    "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "action": action,
                    "serial": serial,
                    "ok": True,
                    "status": "running",
                    "message": f"{active['action']} is already running for this device",
                }
    action_id = f"{action}:{serial or 'all'}:{time.time()}"
    started = {
        "id": action_id,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "action": action,
        "serial": serial,
        "message": "running",
    }
    with ACTION_LOCK:
        ACTIVE_ACTIONS[action_id] = started
    write_log("action_started", {"action": action, "serial": serial, "action_id": action_id})

    def worker() -> None:
        try:
            target(serial)
        finally:
            with ACTION_LOCK:
                ACTIVE_ACTIONS.pop(action_id, None)

    threading.Thread(target=worker, daemon=True).start()
    return {
        "timestamp": started["timestamp"],
        "action": action,
        "serial": serial,
        "ok": True,
        "status": "running",
        "message": "action started; final result will appear in recent actions",
    }


def active_actions_snapshot() -> list[dict[str, Any]]:
    with ACTION_LOCK:
        return list(ACTIVE_ACTIONS.values())


def last_actions_snapshot() -> list[dict[str, Any]]:
    with ACTION_LOCK:
        return list(LAST_ACTIONS)


def configured_mirror_script_path(config: dict[str, Any]) -> str:
    mirror_config = config.get("mirror", {})
    if isinstance(mirror_config, dict) and mirror_config.get("script_path"):
        return str(mirror_config["script_path"])
    return MIRROR_SCRIPT_PATH


def configured_scrcpy_dir(config: dict[str, Any]) -> str:
    mirror_config = config.get("mirror", {})
    if isinstance(mirror_config, dict) and mirror_config.get("scrcpy_dir"):
        return str(mirror_config["scrcpy_dir"])
    state = load_state()
    state_mirror_config = state.get("mirror", {})
    if isinstance(state_mirror_config, dict) and state_mirror_config.get("scrcpy_dir"):
        return str(state_mirror_config["scrcpy_dir"])
    return MIRROR_SCRCPY_DIR


def configured_scrcpy_args(config: dict[str, Any]) -> list[str]:
    mirror_config = config.get("mirror", {})
    raw_args: Any = DEFAULT_SCRCPY_ARGS
    if isinstance(mirror_config, dict) and "scrcpy_args" in mirror_config:
        raw_args = mirror_config.get("scrcpy_args")
    if raw_args is None:
        return []
    if isinstance(raw_args, str):
        return shlex.split(raw_args)
    if isinstance(raw_args, list):
        return [str(arg) for arg in raw_args if str(arg)]
    return list(DEFAULT_SCRCPY_ARGS)


def remember_mirror_scrcpy_dir(scrcpy_dir: str) -> dict[str, Any]:
    scrcpy_dir = scrcpy_dir.strip().strip('"')
    if not scrcpy_dir:
        return {"ok": False, "message": "scrcpy directory is empty"}
    with ACTION_LOCK:
        state = load_state()
        mirror_config = state.setdefault("mirror", {})
        if not isinstance(mirror_config, dict):
            mirror_config = {}
            state["mirror"] = mirror_config
        mirror_config["scrcpy_dir"] = scrcpy_dir
        mirror_config["updated_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        save_state(state)
    write_log("mirror_scrcpy_dir_saved", {"scrcpy_dir": scrcpy_dir})
    return {"ok": True, "message": "mirror scrcpy directory saved", "scrcpy_dir": scrcpy_dir}


def mirror_process_running() -> bool:
    with MIRROR_LOCK:
        return MIRROR_MONITOR_THREAD is not None and MIRROR_MONITOR_THREAD.is_alive()


def mirror_status_snapshot() -> dict[str, Any]:
    config = load_config()
    scrcpy_dir = configured_scrcpy_dir(config)
    scrcpy_args = configured_scrcpy_args(config)
    adb_command = mirror_adb_command(scrcpy_dir)
    scrcpy_command = mirror_scrcpy_command(scrcpy_dir)
    with MIRROR_LOCK:
        running = MIRROR_MONITOR_THREAD is not None and MIRROR_MONITOR_THREAD.is_alive()
        return {
            "configured": True,
            "built_in": True,
            "running": running,
            "pid": None,
            "script_path": "built-in Python scrcpy monitor",
            "scrcpy_dir": scrcpy_dir,
            "scrcpy_args": scrcpy_args,
            "scrcpy_dir_exists": os.path.isdir(scrcpy_dir),
            "scrcpy_exe_exists": bool(scrcpy_command),
            "adb_exe_exists": bool(adb_command),
            "supported": bool(adb_command and scrcpy_command),
            "active_devices": sorted(MIRROR_SCRCPY_PROCESSES),
            "auto_all": MIRROR_AUTO_ALL,
            "target_serials": sorted(MIRROR_TARGET_SERIALS),
            "disabled_serials": sorted(MIRROR_DISABLED_SERIALS),
            "failed_serials": dict(MIRROR_FAILED_SERIALS),
            "log_file": MIRROR_LOG_FILE,
        }


def mirror_adb_command(scrcpy_dir: str) -> list[str]:
    executable = "adb.exe" if platform.system().lower() == "windows" else "adb"
    configured = os.path.join(scrcpy_dir, executable)
    if scrcpy_dir and os.path.exists(configured):
        return [configured]
    found = shutil.which(executable) or shutil.which("adb")
    return [found] if found else []


def mirror_scrcpy_command(scrcpy_dir: str) -> list[str]:
    executable = "scrcpy.exe" if platform.system().lower() == "windows" else "scrcpy"
    configured = os.path.join(scrcpy_dir, executable)
    if scrcpy_dir and os.path.exists(configured):
        return [configured]
    found = shutil.which(executable) or shutil.which("scrcpy")
    return [found] if found else []


def mirror_log(message: str) -> None:
    global MIRROR_LOG_FILE

    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    write_log("mirror_monitor", {"message": message})
    if not MIRROR_LOG_FILE:
        return
    try:
        with open(MIRROR_LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
            handle.write("\n")
    except OSError:
        pass


def read_log_tail(path: str, max_chars: int = 3000) -> str:
    if not path:
        return ""
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return ""
    return text[-max_chars:].strip()


def mirror_ready_serials(adb_command: list[str]) -> list[str]:
    result = run_command([*adb_command, "devices"], timeout=10)
    if result.returncode != 0:
        mirror_log(f"adb devices failed: {result.stderr.strip() or result.stdout.strip()}")
        return []
    serials: list[str] = []
    for line in result.stdout.splitlines():
        line = line.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        if parts[1] == "device":
            serials.append(parts[0])
        else:
            mirror_log(f"ADB device found but not ready: {parts[0]} status={parts[1]}")
    return serials


def start_scrcpy_for_serial(
    serial: str,
    scrcpy_command: list[str],
    scrcpy_args: list[str],
    session_dir: str,
) -> subprocess.Popen[str] | None:
    log_path = os.path.join(session_dir, f"scrcpy_{serial}.log")
    command = [*scrcpy_command, "-s", serial, *scrcpy_args]
    try:
        log_handle = open(log_path, "a", encoding="utf-8", errors="replace")
        process = subprocess.Popen(
            command,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            text=True,
        )
        log_handle.close()
    except OSError as exc:
        mirror_log(f"Failed to start scrcpy for {serial}: {exc}")
        return None
    with MIRROR_LOCK:
        MIRROR_SCRCPY_LOG_FILES[serial] = log_path
    mirror_log(f"scrcpy started for {serial}, pid={process.pid}, command={shlex.join(command)}, log={log_path}")
    return process


def handle_scrcpy_exit(serial: str, process: subprocess.Popen[str], serials: set[str], wanted_serials: set[str]) -> None:
    return_code = process.poll()
    if return_code is None:
        return
    with MIRROR_LOCK:
        log_path = MIRROR_SCRCPY_LOG_FILES.pop(serial, "")
        MIRROR_SCRCPY_PROCESSES.pop(serial, None)
    tail = read_log_tail(log_path)
    detail = f"scrcpy exited for {serial}, return code={return_code}, log={log_path or '-'}"
    if tail:
        detail = f"{detail}; tail={tail}"
    mirror_log(detail)
    if serial in serials and serial in wanted_serials:
        with MIRROR_LOCK:
            MIRROR_DISABLED_SERIALS.add(serial)
            MIRROR_TARGET_SERIALS.discard(serial)
            MIRROR_FAILED_SERIALS[serial] = detail
        record_action(
            "scrcpy mirror exited",
            False,
            f"scrcpy exited immediately or unexpectedly. Auto restart paused for this device. {detail}",
            serial,
        )


def mirror_monitor_loop(
    adb_command: list[str],
    scrcpy_command: list[str],
    scrcpy_args: list[str],
    interval_seconds: float,
    session_dir: str,
) -> None:
    try:
        run_command([*adb_command, "start-server"], timeout=10)
        mirror_log("scrcpy monitor started")
        while not MIRROR_STOP_EVENT.is_set():
            serials = set(mirror_ready_serials(adb_command))
            with MIRROR_LOCK:
                auto_all = MIRROR_AUTO_ALL
                targets = set(MIRROR_TARGET_SERIALS)
                disabled = set(MIRROR_DISABLED_SERIALS)
            wanted_serials = serials - disabled if auto_all else serials & targets
            with MIRROR_LOCK:
                current_processes = dict(MIRROR_SCRCPY_PROCESSES)
            for serial, process in current_processes.items():
                return_code = process.poll()
                if return_code is not None:
                    handle_scrcpy_exit(serial, process, serials, wanted_serials)
                    continue
                if serial not in wanted_serials:
                    reason = "device disconnected from adb" if serial not in serials else "mirror target disabled"
                    mirror_log(f"{reason}: {serial}; terminating scrcpy")
                    try:
                        process.terminate()
                    except OSError as exc:
                        mirror_log(f"Failed to terminate scrcpy for {serial}: {exc}")
                    with MIRROR_LOCK:
                        MIRROR_SCRCPY_PROCESSES.pop(serial, None)
                        MIRROR_SCRCPY_LOG_FILES.pop(serial, None)
            with MIRROR_LOCK:
                running_after_cleanup = set(MIRROR_SCRCPY_PROCESSES)
                disabled = set(MIRROR_DISABLED_SERIALS)
            wanted_serials = serials - disabled if auto_all else serials & targets
            for serial in sorted(wanted_serials - running_after_cleanup):
                new_process = start_scrcpy_for_serial(serial, scrcpy_command, scrcpy_args, session_dir)
                if new_process is not None:
                    with MIRROR_LOCK:
                        MIRROR_SCRCPY_PROCESSES[serial] = new_process
            MIRROR_STOP_EVENT.wait(interval_seconds)
    finally:
        with MIRROR_LOCK:
            processes = dict(MIRROR_SCRCPY_PROCESSES)
            MIRROR_SCRCPY_PROCESSES.clear()
            MIRROR_SCRCPY_LOG_FILES.clear()
        for serial, process in processes.items():
            if process.poll() is None:
                try:
                    process.terminate()
                    mirror_log(f"Terminated scrcpy for {serial}")
                except OSError:
                    pass
        mirror_log("scrcpy monitor stopped")


def ensure_mirror_monitor_started(config: dict[str, Any]) -> tuple[bool, str]:
    global MIRROR_MONITOR_THREAD, MIRROR_LOG_FILE

    mirror_config = config.get("mirror", {})
    interval_seconds = 5.0
    if isinstance(mirror_config, dict):
        try:
            interval_seconds = float(mirror_config.get("check_interval_seconds", interval_seconds))
        except (TypeError, ValueError):
            interval_seconds = 5.0
    scrcpy_dir = configured_scrcpy_dir(config)
    scrcpy_args = configured_scrcpy_args(config)
    adb_command = mirror_adb_command(scrcpy_dir)
    scrcpy_command = mirror_scrcpy_command(scrcpy_dir)
    if not adb_command:
        return False, "adb was not found for scrcpy monitor"
    if not scrcpy_command:
        return False, "scrcpy was not found; install scrcpy or configure scrcpy_dir"
    with MIRROR_LOCK:
        if MIRROR_MONITOR_THREAD is not None and MIRROR_MONITOR_THREAD.is_alive():
            return True, f"scrcpy monitor already running; devices={sorted(MIRROR_SCRCPY_PROCESSES)}"
        session_dir = os.path.abspath(
            os.path.join(MIRROR_RUNTIME_DIR, f"session-{time.strftime('%Y%m%d-%H%M%S')}")
        )
        os.makedirs(session_dir, exist_ok=True)
        MIRROR_LOG_FILE = os.path.join(session_dir, "scrcpy_monitor.log")
        MIRROR_STOP_EVENT.clear()
        MIRROR_MONITOR_THREAD = threading.Thread(
            target=mirror_monitor_loop,
            args=(adb_command, scrcpy_command, scrcpy_args, interval_seconds, session_dir),
            daemon=True,
        )
        MIRROR_MONITOR_THREAD.start()
    return True, (
        f"scrcpy monitor started; log={MIRROR_LOG_FILE}; adb={adb_command[0]}; "
        f"scrcpy={scrcpy_command[0]}; args={shlex.join(scrcpy_args) if scrcpy_args else '-'}"
    )


def start_mirror_script(_: str = "") -> dict[str, Any]:
    global MIRROR_AUTO_ALL

    config = load_config()
    with MIRROR_LOCK:
        MIRROR_AUTO_ALL = True
        MIRROR_DISABLED_SERIALS.clear()
        MIRROR_FAILED_SERIALS.clear()
    ok, message = ensure_mirror_monitor_started(config)
    if not ok:
        return record_action("start-mirror-script", False, message)
    return record_action(
        "start-mirror-script",
        True,
        message,
        status="running",
    )


def start_mirror_device(serial: str) -> dict[str, Any]:
    if not serial:
        return record_action("start-mirror-device", False, "serial is required")
    config = load_config()
    with MIRROR_LOCK:
        MIRROR_TARGET_SERIALS.add(serial)
        MIRROR_DISABLED_SERIALS.discard(serial)
        MIRROR_FAILED_SERIALS.pop(serial, None)
    ok, message = ensure_mirror_monitor_started(config)
    if not ok:
        return record_action("start-mirror-device", False, message, serial)
    return record_action("start-mirror-device", True, message, serial, status="running")


def terminate_mirror_process(serial: str) -> bool:
    with MIRROR_LOCK:
        process = MIRROR_SCRCPY_PROCESSES.pop(serial, None)
        MIRROR_SCRCPY_LOG_FILES.pop(serial, None)
    if process is None:
        return False
    if process.poll() is None:
        try:
            process.terminate()
        except OSError as exc:
            mirror_log(f"Failed to terminate scrcpy for {serial}: {exc}")
    mirror_log(f"scrcpy stop requested for {serial}")
    return True


def stop_mirror_device(serial: str) -> dict[str, Any]:
    if not serial:
        return record_action("stop-mirror-device", False, "serial is required")
    with MIRROR_LOCK:
        MIRROR_TARGET_SERIALS.discard(serial)
        MIRROR_DISABLED_SERIALS.add(serial)
    stopped = terminate_mirror_process(serial)
    message = "scrcpy stopped for device" if stopped else "device mirror was not running; device disabled for auto mirror"
    return record_action("stop-mirror-device", True, message, serial)


def stop_mirror_script(_: str = "") -> dict[str, Any]:
    global MIRROR_AUTO_ALL, MIRROR_MONITOR_THREAD

    with MIRROR_LOCK:
        MIRROR_AUTO_ALL = False
        MIRROR_TARGET_SERIALS.clear()
        MIRROR_DISABLED_SERIALS.clear()
        MIRROR_FAILED_SERIALS.clear()
        processes = dict(MIRROR_SCRCPY_PROCESSES)
        MIRROR_SCRCPY_PROCESSES.clear()
        MIRROR_SCRCPY_LOG_FILES.clear()
        monitor_thread = MIRROR_MONITOR_THREAD
    for serial, process in processes.items():
        if process.poll() is None:
            try:
                process.terminate()
                mirror_log(f"Terminated scrcpy for {serial}")
            except OSError as exc:
                mirror_log(f"Failed to terminate scrcpy for {serial}: {exc}")
    MIRROR_STOP_EVENT.set()
    if monitor_thread is not None and monitor_thread.is_alive():
        monitor_thread.join(timeout=2)
    with MIRROR_LOCK:
        if MIRROR_MONITOR_THREAD is monitor_thread:
            MIRROR_MONITOR_THREAD = None
    return record_action("stop-mirror-script", True, "all scrcpy mirror processes stopped")


def adb_event_signature(device: dict[str, Any]) -> tuple[str, str, str, bool]:
    return (
        str(device.get("state") or ""),
        str(device.get("usb_path") or ""),
        str(device.get("transport_id") or ""),
        bool(device.get("behind_hub")),
    )


def record_adb_device_events(devices: list[dict[str, Any]]) -> None:
    global ADB_EVENT_LOG_INITIALIZED

    current = {str(device["serial"]): adb_event_signature(device) for device in devices if device.get("serial")}
    events: list[tuple[str, str, str, str]] = []
    disconnected: list[tuple[str, tuple[str, str, str, bool]]] = []
    with ACTION_LOCK:
        previous = dict(ADB_EVENT_STATE)
        if not ADB_EVENT_LOG_INITIALIZED:
            ADB_EVENT_STATE.clear()
            ADB_EVENT_STATE.update(current)
            ADB_EVENT_LOG_INITIALIZED = True
            return
        for serial, signature in current.items():
            if serial not in previous:
                events.append(
                    (
                        "device-connected",
                        serial,
                        "event",
                        f"ADB device appeared: state={signature[0]}, usb={signature[1] or '-'}",
                    )
                )
            elif previous[serial] != signature:
                events.append(
                    (
                        "device-changed",
                        serial,
                        "event",
                        "ADB device changed: "
                        f"state {previous[serial][0]} -> {signature[0]}, "
                        f"usb {previous[serial][1] or '-'} -> {signature[1] or '-'}, "
                        f"transport {previous[serial][2] or '-'} -> {signature[2] or '-'}",
                    )
                )
        for serial, signature in previous.items():
            if serial not in current:
                disconnected.append((serial, signature))
                events.append(
                    (
                        "device-disconnected",
                        serial,
                        "event",
                        f"ADB device disappeared: last state={signature[0]}, usb={signature[1] or '-'}",
                    )
                )
        ADB_EVENT_STATE.clear()
        ADB_EVENT_STATE.update(current)

    for action, serial, status, message in events:
        record_action(action, True, message, serial, status=status)
    if disconnected:
        system_events = collect_platform_usb_events(60)
        write_log("platform_usb_events", system_events)
        affected = [serial for serial, _ in disconnected]
        for serial, signature in disconnected:
            diagnose_disconnect(serial, affected, signature, system_events)


def dashboard_diagnostics() -> list[dict[str, str]]:
    diagnostics: list[dict[str, str]] = []
    with ACTION_LOCK:
        recent = list(LAST_ACTIONS[:8])
    for action in recent:
        message = action.get("message", "")
        if "USB permission problem" in message:
            diagnostics.append(
                {
                    "level": "error",
                    "title": "USB permission is blocking hub control",
                    "message": "Start the service with sudo for a quick test, or add udev rules for uhubctl/libusb.",
                }
            )
            break
    for action in recent:
        message = action.get("message", "")
        if "No controllable hub was found" in message:
            diagnostics.append(
                {
                    "level": "error",
                    "title": "Inferred hub location is not controllable",
                    "message": "Run `sudo uhubctl` and compare the listed locations with the location shown on each phone card.",
                }
            )
            break
    return diagnostics


def run_command(args: list[str], timeout: float = 8.0) -> subprocess.CompletedProcess[str]:
    started = time.perf_counter()
    try:
        result = subprocess.run(args, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        duration_ms = int((time.perf_counter() - started) * 1000)
        write_log(
            "command_timeout",
            {
                "command": args,
                "timeout_seconds": timeout,
                "duration_ms": duration_ms,
                "stdout": truncate_text(exc.stdout or ""),
                "stderr": truncate_text(exc.stderr or ""),
            },
        )
        raise
    duration_ms = int((time.perf_counter() - started) * 1000)
    if result.returncode != 0 or duration_ms > 2000:
        write_log(
            "command_completed",
            {
                "command": args,
                "returncode": result.returncode,
                "duration_ms": duration_ms,
                "stdout": truncate_text(result.stdout),
                "stderr": truncate_text(result.stderr),
            },
        )
    return result


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


def normalize_hub_backend(value: Any) -> str:
    raw = str(value or "").strip().lower()
    aliases = {
        "": "auto",
        "auto": "auto",
        "none": "none",
        "disabled": "none",
        "off": "none",
        "acroname": "acroname",
        "brainstem": "acroname",
        "usbhub3c": "acroname",
        "uhubctl": "uhubctl",
        "acthub": "uhubctl",
        "linux": "uhubctl",
    }
    return aliases.get(raw, "auto")


def configured_hub_backend(config: dict[str, Any]) -> str:
    for key in ("hub_backend", "backend"):
        if key in config:
            return normalize_hub_backend(config.get(key))
    hub_control = config.get("hub_control", {})
    if isinstance(hub_control, dict) and hub_control.get("type"):
        return normalize_hub_backend(hub_control.get("type"))
    return normalize_hub_backend(os.environ.get("USB_ANDROID_MONITOR_HUB_BACKEND", "auto"))


def select_hub_backend(config: dict[str, Any]) -> str:
    requested = configured_hub_backend(config)
    if requested in {"acroname", "uhubctl", "none"}:
        return requested
    if shutil.which("uhubctl"):
        return "uhubctl"
    if brainstem_available():
        return "acroname"
    return "none"


def initialize_hub_backend(config: dict[str, Any] | None = None) -> str:
    global HUB_BACKEND
    selected_config = config or load_config()
    HUB_BACKEND = select_hub_backend(selected_config)
    write_log(
        "hub_backend_selected",
        {
            "hub_backend": HUB_BACKEND,
            "requested": configured_hub_backend(selected_config),
            "uhubctl_available": bool(shutil.which("uhubctl")),
            "brainstem_available": brainstem_available(),
        },
    )
    return HUB_BACKEND


def hub_backend() -> str:
    return HUB_BACKEND


def load_state(path: str = STATE_PATH) -> dict[str, Any]:
    if not os.path.exists(path):
        return {"known_devices": {}}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            state = json.load(handle)
    except (OSError, json.JSONDecodeError):
        return {"known_devices": {}}
    if not isinstance(state, dict):
        return {"known_devices": {}}
    state.setdefault("known_devices", {})
    if not isinstance(state["known_devices"], dict):
        state["known_devices"] = {}
    return state


def save_state(state: dict[str, Any], path: str = STATE_PATH) -> None:
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(state, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp_path, path)
    write_log(
        "state_saved",
        {
            "state_path": path,
            "known_device_count": len(state.get("known_devices", {})) if isinstance(state.get("known_devices"), dict) else 0,
        },
    )


def remember_known_device(serial: str, data: dict[str, Any]) -> None:
    if not serial:
        return
    with ACTION_LOCK:
        state = load_state()
        known = state.setdefault("known_devices", {})
        current = known.get(serial, {})
        current.update(data)
        current["last_seen_at"] = time.strftime("%Y-%m-%d %H:%M:%S")
        known[serial] = current
        save_state(state)


def known_devices_snapshot() -> dict[str, dict[str, Any]]:
    with ACTION_LOCK:
        return dict(load_state().get("known_devices", {}))


def clear_learned_acroname_mappings() -> int:
    with ACTION_LOCK:
        state = load_state()
        known = state.setdefault("known_devices", {})
        cleared = 0
        for device in known.values():
            if not isinstance(device, dict):
                continue
            control = device.get("acroname_control")
            if not isinstance(control, dict):
                continue
            if control.get("source") == "config":
                continue
            device.pop("acroname_control", None)
            device.pop("mapping_status", None)
            device.pop("mapping_conflict_with", None)
            if device.get("power_state") in {"off", "unknown"}:
                device["power_state"] = "on"
            cleared += 1
        if cleared:
            save_state(state)
    if cleared:
        write_log("acroname_mappings_cleared", {"cleared": cleared})
    return cleared


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


def infer_uhubctl_target_from_usb_path(usb_path: str) -> dict[str, str]:
    path_only = usb_path.split(":", 1)[-1] if usb_path else ""
    if "." not in path_only:
        return {}
    location, port = path_only.rsplit(".", 1)
    if not location or not port.isdigit():
        return {}
    return {"location": location, "port": port, "source": f"adb usb path {usb_path}"}


def get_adb_devices() -> list[AdbDevice]:
    global LAST_ADB_RAW_OUTPUT

    if not shutil.which("adb"):
        return []
    result = run_command(["adb", "devices", "-l"], timeout=10)
    if result.returncode != 0:
        record_action("adb devices", False, result.stderr.strip() or result.stdout.strip())
        return []
    if result.stdout != LAST_ADB_RAW_OUTPUT:
        write_log(
            "adb_snapshot_changed",
            {
                "raw": truncate_text(result.stdout),
                "previous_raw": truncate_text(LAST_ADB_RAW_OUTPUT),
            },
        )
        LAST_ADB_RAW_OUTPUT = result.stdout
    return parse_adb_devices(result.stdout)


def get_adb_device(serial: str) -> AdbDevice | None:
    for device in get_adb_devices():
        if device.serial == serial:
            return device
    return None


def uhubctl_target_for_serial(serial: str, config: dict[str, Any]) -> dict[str, str]:
    device_config = configured_devices(config).get(serial, {})
    configured = device_config.get("uhubctl", {})
    if configured.get("enabled", True) and configured.get("location") and configured.get("port"):
        return {
            "location": str(configured["location"]),
            "port": str(configured["port"]),
            "source": "config",
        }
    with ACTION_LOCK:
        remembered = DISCONNECTED_TARGETS.get(serial)
    if remembered:
        return dict(remembered)
    known = known_devices_snapshot().get(serial, {})
    known_target = known.get("uhubctl_target", {})
    if known_target.get("location") and known_target.get("port"):
        return {
            "location": str(known_target["location"]),
            "port": str(known_target["port"]),
            "source": str(known_target.get("source") or "state"),
        }
    adb_device = get_adb_device(serial)
    if not adb_device:
        return {}
    return infer_uhubctl_target_from_usb_path(adb_device.usb_path)


def power_target_for_serial(serial: str, config: dict[str, Any]) -> dict[str, Any]:
    backend = hub_backend()
    if backend == "acroname":
        return acroname_control_for_serial(serial, config)
    if backend == "uhubctl":
        return uhubctl_target_for_serial(serial, config)
    return {}


def remember_disconnected_target(serial: str, target: dict[str, Any]) -> None:
    if not serial or not target.get("location") or not target.get("port"):
        return
    with ACTION_LOCK:
        DISCONNECTED_TARGETS[serial] = {
            "location": str(target["location"]),
            "port": str(target["port"]),
            "source": str(target.get("source") or "remembered"),
        }
    remember_known_device(
        serial,
        {
            "uhubctl_target": {
                "location": str(target["location"]),
                "port": str(target["port"]),
                "source": str(target.get("source") or "remembered"),
            },
            "power_state": "off",
        },
    )


def forget_disconnected_target(serial: str) -> None:
    with ACTION_LOCK:
        DISCONNECTED_TARGETS.pop(serial, None)
    known = known_devices_snapshot().get(serial)
    if known:
        known["power_state"] = "on"
        remember_known_device(serial, known)


def explain_uhubctl_failure(output: str, target: dict[str, Any]) -> str:
    lower = output.lower()
    location = target.get("location", "-")
    if "permission" in lower or "accessing usb" in lower:
        return (
            "USB permission problem. Run the service with sudo for testing, or install udev rules "
            "for uhubctl/libusb access. "
        )
    if "no compatible devices" in lower:
        return (
            f"No controllable hub was found at location {location}. Run `sudo uhubctl` and use one "
            "of the exact locations it lists, or use a hub with per-port power switching. "
        )
    if "not found" in lower:
        return "uhubctl could not find the requested hub or port. Verify the inferred location/port. "
    return ""


def adb_state_for_serial(serial: str) -> str:
    for device in get_adb_devices():
        if device.serial == serial:
            return device.state
    return "absent"


def wait_for_adb_absent(serial: str, timeout_seconds: float = 6.0) -> tuple[bool, str]:
    deadline = time.time() + timeout_seconds
    last_state = adb_state_for_serial(serial)
    while time.time() < deadline:
        last_state = adb_state_for_serial(serial)
        if last_state == "absent":
            return True, last_state
        time.sleep(0.5)
    return False, last_state


def wait_for_adb_present(serial: str, timeout_seconds: float = 25.0) -> tuple[bool, str]:
    deadline = time.time() + timeout_seconds
    last_state = adb_state_for_serial(serial)
    while time.time() < deadline:
        last_state = adb_state_for_serial(serial)
        if last_state != "absent":
            return True, last_state
        time.sleep(1.0)
    return False, last_state


def verify_device(serial: str) -> dict[str, Any]:
    if not serial:
        return record_action("verify", False, "serial is required")
    if not shutil.which("adb"):
        return record_action("verify", False, "adb is not installed or not on PATH", serial)
    state = adb_state_for_serial(serial)
    if state == "absent":
        return record_action("verify", True, "verified disconnected from ADB: serial is absent", serial)
    return record_action(
        "verify",
        False,
        f"still connected according to ADB: serial is present with state={state}",
        serial,
    )


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
        inferred_uhubctl = infer_uhubctl_target_from_usb_path(device.usb_path)
        row["uhubctl_target"] = inferred_uhubctl
        if inferred_uhubctl:
            row["hub_evidence"] = [
                *row["hub_evidence"],
                f"inferred uhubctl target: -l {inferred_uhubctl['location']} -p {inferred_uhubctl['port']}",
            ]
            remember_known_device(
                device.serial,
                {
                    "name": row["model"] or row["product"] or device.serial,
                    "model": row["model"],
                    "product": row["product"],
                    "device": row["device"],
                    "usb_path": row["usb_path"],
                    "uhubctl_target": inferred_uhubctl,
                    "power_state": "on",
                },
            )
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
    steps = ["adb start-server", "adb reconnect"]
    device_config = configured_devices(config).get(serial, {})
    backend = hub_backend()
    if backend == "acroname":
        acroname = acroname_control_for_serial(serial, config)
        if acroname:
            steps.append(f"acroname {acroname.get('action', 'port')} cycle port={acroname['port']}")
    elif backend == "uhubctl":
        uhubctl = uhubctl_target_for_serial(serial, config)
        if uhubctl.get("location") and uhubctl.get("port"):
            steps.append(f"uhubctl cycle location={uhubctl['location']} port={uhubctl['port']}")
    windows_instance_id = device_config.get("windows_instance_id", "")
    if windows_instance_id:
        steps.append(f"pnputil restart-device {windows_instance_id}")
    return steps


def acroname_control_for_device(device_config: dict[str, Any]) -> dict[str, Any]:
    hub_control = device_config.get("hub_control", {})
    acroname = device_config.get("acroname", {})
    if isinstance(hub_control, dict) and hub_control.get("type") == "acroname":
        acroname = hub_control
    if not isinstance(acroname, dict) or not acroname.get("enabled", True):
        return {}
    if "port" not in acroname:
        return {}
    control = dict(acroname)
    control.setdefault("type", "acroname")
    return control


def acroname_control_for_serial(serial: str, config: dict[str, Any]) -> dict[str, Any]:
    configured = acroname_control_for_device(configured_devices(config).get(serial, {}))
    if configured:
        configured.setdefault("source", "config")
        return configured
    known = known_devices_snapshot().get(serial, {})
    if known.get("mapping_status") == "suspect":
        return {}
    known_control = known.get("acroname_control", {})
    if isinstance(known_control, dict) and known_control.get("port") is not None:
        control = dict(known_control)
        control.setdefault("type", "acroname")
        control.setdefault("source", "state")
        return control
    return {}


def acroname_mapping_control(config: dict[str, Any]) -> dict[str, Any]:
    hub_control = config.get("hub_control", {})
    acroname = config.get("acroname", {})
    if isinstance(hub_control, dict) and hub_control.get("type") == "acroname":
        acroname = hub_control
    if not isinstance(acroname, dict):
        acroname = {}
    control = dict(acroname)
    control.setdefault("type", "acroname")
    control.setdefault("model", "USBHub3c")
    control.setdefault("ports", [1, 2, 3, 4, 5])
    return control


def normalized_acroname_ports(raw_ports: Any) -> list[int]:
    ports = sorted({int(port) for port in raw_ports})
    if 0 in ports:
        ports = [port for port in ports if port != 0] + [0]
    return ports


def brainstem_available() -> bool:
    try:
        import brainstem  # noqa: F401
    except ImportError:
        return False
    return True


def acroname_control_for_port(mapping_control: dict[str, Any], port: int) -> dict[str, Any]:
    control = {
        "type": "acroname",
        "model": mapping_control.get("model", "USBHub3c"),
        "port": port,
        "source": "auto-map",
    }
    for key in ("hub_serial", "serial_number", "cycle_delay_seconds"):
        if mapping_control.get(key) not in {None, ""}:
            control[key] = mapping_control[key]
    return control


def parse_acroname_serial(value: Any) -> int | None:
    if value in {None, ""}:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    if text.lower().startswith("0x"):
        return int(text, 16)
    try:
        return int(text, 10)
    except ValueError:
        return int(text, 16)


def acroname_hub_class(brainstem: Any, hub_model: str) -> Any:
    model = hub_model or "USBHub3c"
    if not hasattr(brainstem.stem, model):
        raise RuntimeError(f"unsupported Acroname hub model {model}")
    return getattr(brainstem.stem, model)


def acroname_no_error_value(brainstem: Any) -> Any:
    result = getattr(brainstem, "Result", None)
    if result is not None and hasattr(result, "NO_ERROR"):
        return result.NO_ERROR
    from brainstem.result import Result

    return Result.NO_ERROR


def run_acroname_port_action(serial: str, control: dict[str, Any], action: str) -> dict[str, Any]:
    started = time.perf_counter()
    try:
        port = int(control["port"])
        hub_model = str(control.get("model") or "USBHub3c")
        hub_serial = parse_acroname_serial(control.get("hub_serial") or control.get("serial_number"))
        import brainstem

        no_error = acroname_no_error_value(brainstem)
    except ImportError as exc:
        return record_action(
            f"acroname-port-{action}",
            False,
            f"brainstem Python package is not installed or not on PYTHONPATH: {exc}",
            serial,
        )
    except (KeyError, TypeError, ValueError) as exc:
        return record_action(f"acroname-port-{action}", False, f"invalid Acroname config: {exc}", serial)

    hub = None
    try:
        hub = acroname_hub_class(brainstem, hub_model)()
        if hub_serial is None:
            result = hub.discoverAndConnect(brainstem.link.Spec.USB)
        else:
            result = hub.discoverAndConnect(brainstem.link.Spec.USB, hub_serial)
        if result != no_error:
            return record_action(
                f"acroname-port-{action}",
                False,
                f"could not connect to Acroname {hub_model}; result={result}",
                serial,
            )
        if action == "off":
            err = hub.usb.setPortDisable(port)
        elif action == "on":
            err = hub.usb.setPortEnable(port)
        elif action == "cycle":
            err = hub.usb.setPortDisable(port)
            if err == no_error:
                time.sleep(float(control.get("cycle_delay_seconds", 2)))
                err = hub.usb.setPortEnable(port)
        else:
            return record_action(f"acroname-port-{action}", False, f"unsupported Acroname action {action}", serial)
        ok = err == no_error
        message = f"Acroname {hub_model} port {port} {action}; result={err}"
        write_log(
            "hub_port_action_result",
            {
                "backend": "acroname",
                "serial": serial,
                "hub_model": hub_model,
                "hub_serial": control.get("hub_serial") or control.get("serial_number") or "",
                "port": port,
                "action": action,
                "ok": ok,
                "result_code": str(err),
                "duration_ms": int((time.perf_counter() - started) * 1000),
            },
        )
        return record_action(f"acroname-port-{action}", ok, message, serial)
    except Exception as exc:
        return record_action(f"acroname-port-{action}", False, f"Acroname control failed: {exc}", serial)
    finally:
        if hub is not None:
            try:
                hub.disconnect()
            except Exception:
                pass


def adb_serials() -> set[str]:
    return {device.serial for device in get_adb_devices() if device.serial and device.state != "absent"}


def wait_for_serials_absent(serials: set[str], timeout_seconds: float = 8.0) -> set[str]:
    deadline = time.time() + timeout_seconds
    missing: set[str] = set()
    while time.time() < deadline:
        current = adb_serials()
        missing = serials - current
        if missing:
            return missing
        time.sleep(0.5)
    return missing


def wait_for_serials_missing_from_baseline(
    baseline: set[str],
    timeout_seconds: float = 6.0,
) -> tuple[set[str], set[str]]:
    deadline = time.time() + timeout_seconds
    current = set(baseline)
    missing: set[str] = set()
    while time.time() < deadline:
        current = adb_serials()
        missing = baseline - current
        if missing:
            return missing, current
        time.sleep(0.5)
    return missing, current


def wait_for_serials_present(serials: set[str], timeout_seconds: float = 25.0) -> set[str]:
    deadline = time.time() + timeout_seconds
    present: set[str] = set()
    while time.time() < deadline:
        current = adb_serials()
        present = serials & current
        if present == serials:
            return present
        time.sleep(1.0)
    return present


def map_acroname_ports(_: str = "") -> dict[str, Any]:
    if hub_backend() != "acroname":
        return record_action(
            "map-acroname",
            False,
            f"Acroname mapping is unavailable because active hub backend is {hub_backend()}",
        )
    config = load_config()
    mapping_control = acroname_mapping_control(config)
    ports = normalized_acroname_ports(mapping_control.get("ports", []))
    if not ports:
        return record_action("map-acroname", False, "no Acroname ports configured for mapping")
    baseline = adb_serials()
    if not baseline:
        return record_action("map-acroname", False, "no ADB devices are currently visible; connect phones first")
    cleared_mappings = clear_learned_acroname_mappings()

    mapped: dict[str, int] = {}
    partial_returns = 0
    messages: list[str] = [
        f"cleared learned mappings={cleared_mappings}",
        f"baseline adb devices={len(baseline)}",
        f"ports={ports}",
    ]
    write_log(
        "acroname_map_started",
        {
            "ports": ports,
            "baseline_serials": sorted(baseline),
            "cleared_mappings": cleared_mappings,
            "hub_model": mapping_control.get("model", "USBHub3c"),
            "hub_serial": mapping_control.get("hub_serial") or mapping_control.get("serial_number") or "",
        },
    )

    def remember_acroname_mapping(serial: str, control: dict[str, Any], power_state: str, status: str) -> None:
        learned = dict(control)
        learned["source"] = "auto-map"
        remember_known_device(
            serial,
            {
                "name": serial,
                "acroname_control": learned,
                "power_state": power_state,
                "mapping_status": status,
            },
        )

    for port in ports:
        control = acroname_control_for_port(mapping_control, port)
        before = adb_serials()
        if not before:
            messages.append(f"port {port}: skipped because no ADB devices were visible")
            continue
        write_log("acroname_map_port_probe_started", {"port": port, "before_serials": sorted(before)})
        off = run_acroname_port_action("", control, "off")
        if not off["ok"]:
            messages.append(f"port {port}: off failed: {off['message']}")
            on = run_acroname_port_action("", control, "on")
            current = adb_serials()
            if current != before or not on["ok"]:
                return record_action(
                    "map-acroname",
                    False,
                    "aborted because an Acroname port action failed and device state changed; "
                    f"port={port}; before={sorted(before)}; current={sorted(current)}; "
                    f"off={off['message']}; on={on['message']}",
                )
            continue
        missing = wait_for_serials_absent(before)
        on = run_acroname_port_action("", control, "on")
        if missing and on["ok"]:
            reconnect_device("")
        returned = wait_for_serials_present(missing) if missing else set()
        write_log(
            "acroname_map_port_probe_result",
            {
                "port": port,
                "before_serials": sorted(before),
                "missing_serials": sorted(missing),
                "returned_serials": sorted(returned),
                "off_ok": off["ok"],
                "on_ok": on["ok"],
            },
        )
        if len(missing) > 1:
            return record_action(
                "map-acroname",
                False,
                "aborted because one Acroname port affected multiple ADB serials; "
                f"port={port}; disappeared={sorted(missing)}; on={on['message']}",
            )
        elif missing:
            if not on["ok"]:
                return record_action(
                    "map-acroname",
                    False,
                    "aborted because the mapped port could not be re-enabled; "
                    f"port={port}; disappeared={sorted(missing)}; on={on['message']}",
                )
            for serial in sorted(missing):
                remember_acroname_mapping(
                    serial,
                    control,
                    "on" if serial in returned else "unknown",
                    "mapped" if serial in returned else "mapped-needs-return",
                )
                mapped[serial] = port
            messages.append(
                f"port {port}: mapped {', '.join(sorted(missing))}; "
                f"return={'ok' if returned == missing else 'partial'}; on={on['status']}"
            )
            if returned != missing:
                partial_returns += 1
                messages.append(
                    f"port {port}: saved partial mapping; disappeared={sorted(missing)}; "
                    f"returned={sorted(returned)}; continuing with visible devices"
                )
        else:
            messages.append(f"port {port}: no ADB serial disappeared")
        time.sleep(1.0)
    ok = bool(mapped)
    write_log("acroname_map_completed", {"ok": ok, "mapped": mapped, "ports": ports, "partial_returns": partial_returns})
    return record_action(
        "map-acroname",
        ok,
        f"mapped {len(mapped)} device(s): {mapped}; partial returns={partial_returns}; " + " | ".join(messages),
        status="ok" if ok else "failed",
    )


def power_cycle_linux_hub_port(serial: str, device_config: dict[str, Any]) -> dict[str, Any]:
    uhubctl = device_config.get("uhubctl", device_config)
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
    diagnostic = explain_uhubctl_failure(output, uhubctl)
    message = f"{diagnostic}{output or result.returncode}"
    return record_action("power-cycle", result.returncode == 0, message, serial)


def power_off_linux_hub_port(serial: str, device_config: dict[str, Any]) -> dict[str, Any]:
    uhubctl = device_config.get("uhubctl", device_config)
    if not uhubctl.get("location") or not uhubctl.get("port"):
        return record_action("hub-port-off", False, "missing uhubctl location or port in config", serial)
    if not shutil.which("uhubctl"):
        return record_action("hub-port-off", False, "uhubctl is not installed or not on PATH", serial)
    command = [
        "uhubctl",
        "-l",
        str(uhubctl["location"]),
        "-p",
        str(uhubctl["port"]),
        "-a",
        "off",
    ]
    result = run_command(command, timeout=20)
    output = (result.stdout + result.stderr).strip()
    diagnostic = explain_uhubctl_failure(output, uhubctl)
    message = f"{diagnostic}{output or result.returncode}"
    return record_action("hub-port-off", result.returncode == 0, message, serial)


def power_on_linux_hub_port(serial: str, device_config: dict[str, Any]) -> dict[str, Any]:
    uhubctl = device_config.get("uhubctl", device_config)
    if not uhubctl.get("location") or not uhubctl.get("port"):
        return record_action("hub-port-on", False, "missing uhubctl location or port", serial)
    if not shutil.which("uhubctl"):
        return record_action("hub-port-on", False, "uhubctl is not installed or not on PATH", serial)
    command = [
        "uhubctl",
        "-l",
        str(uhubctl["location"]),
        "-p",
        str(uhubctl["port"]),
        "-a",
        "on",
    ]
    result = run_command(command, timeout=20)
    output = (result.stdout + result.stderr).strip()
    diagnostic = explain_uhubctl_failure(output, uhubctl)
    message = f"{diagnostic}{output or result.returncode}"
    return record_action("hub-port-on", result.returncode == 0, message, serial)


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

    if allow_power_cycle and serial:
        backend = hub_backend()
        if backend == "acroname":
            acroname = acroname_control_for_serial(serial, config)
            if not acroname:
                messages.append("no Acroname target for active backend")
            else:
                result = run_acroname_port_action(serial, acroname, "cycle")
                messages.append(result["message"])
                time.sleep(2)
                messages.append(reconnect_device(serial)["message"])
        elif backend == "uhubctl":
            uhubctl_target = uhubctl_target_for_serial(serial, config)
            if not uhubctl_target:
                messages.append("no uhubctl target for active backend")
            else:
                result = power_cycle_linux_hub_port(serial, uhubctl_target)
                messages.append(f"uhubctl source={uhubctl_target.get('source', '-')}")
                messages.append(result["message"])
                time.sleep(2)
                messages.append(reconnect_device(serial)["message"])
        elif platform.system().lower() == "windows" and device_config.get("windows_instance_id"):
            result = restart_windows_usb_device(serial, device_config)
            messages.append(result["message"])
            time.sleep(2)
            messages.append(reconnect_device(serial)["message"])
    ok = any(action.get("ok") for action in LAST_ACTIONS[:4] if action.get("serial") in {serial, ""})
    return record_action("recover", ok, " | ".join(messages), serial)


def connect_device(serial: str) -> dict[str, Any]:
    if not serial:
        return record_action("connect", False, "serial is required")
    config = load_config()
    messages: list[str] = []
    ok = False
    backend = hub_backend()
    if backend == "uhubctl":
        uhubctl_target = uhubctl_target_for_serial(serial, config)
        if uhubctl_target:
            result = power_on_linux_hub_port(serial, uhubctl_target)
            messages.append(
                f"hub port power on via {uhubctl_target.get('source', '-')}: {result['message']}"
            )
            ok = result["ok"]
            if ok:
                MANUAL_DISCONNECT_UNTIL.pop(serial, None)
                messages.append(reconnect_device(serial)["message"])
                present, state = wait_for_adb_present(serial)
                if present:
                    forget_disconnected_target(serial)
                    return record_action(
                        "connect",
                        True,
                        f"{' | '.join(messages)} | adb returned within wait window; state={state}",
                        serial,
                    )
                return record_action(
                    "connect",
                    False,
                    f"{' | '.join(messages)} | hub port is on, but serial stayed absent from adb after 25s",
                    serial,
                )
        else:
            messages.append("no remembered or configured uhubctl target; only restarting ADB")
    if backend == "acroname":
        acroname = acroname_control_for_serial(serial, config)
        if not acroname:
            messages.append("no Acroname target for active backend")
        else:
            result = run_acroname_port_action(serial, acroname, "on")
            messages.append(f"acroname port power/data on: {result['message']}")
            ok = result["ok"]
            if ok:
                MANUAL_DISCONNECT_UNTIL.pop(serial, None)
                messages.append(reconnect_device(serial)["message"])
                present, state = wait_for_adb_present(serial)
                if present:
                    forget_disconnected_target(serial)
                    return record_action(
                        "connect",
                        True,
                        f"{' | '.join(messages)} | adb returned within wait window; state={state}",
                        serial,
                    )
                messages.append(reconnect_device("")["message"])
                present, state = wait_for_adb_present(serial, timeout_seconds=35.0)
                if present:
                    forget_disconnected_target(serial)
                    return record_action(
                        "connect",
                        True,
                        f"{' | '.join(messages)} | adb returned after global discovery; state={state}",
                        serial,
                    )
                cycle = run_acroname_port_action(serial, acroname, "cycle")
                messages.append(f"acroname retry cycle: {cycle['message']}")
                if cycle["ok"]:
                    time.sleep(float(acroname.get("post_cycle_wait_seconds", 4)))
                    messages.append(reconnect_device("")["message"])
                    present, state = wait_for_adb_present(serial, timeout_seconds=45.0)
                    if present:
                        forget_disconnected_target(serial)
                        return record_action(
                            "connect",
                            True,
                            f"{' | '.join(messages)} | adb returned after retry cycle; state={state}",
                            serial,
                        )
                return record_action(
                    "connect",
                    False,
                    f"{' | '.join(messages)} | Acroname port is on, but serial stayed absent from adb after retry",
                    serial,
                )
    MANUAL_DISCONNECT_UNTIL.pop(serial, None)
    messages.append(reconnect_device(serial)["message"])
    ok, state = wait_for_adb_present(serial, timeout_seconds=12.0)
    if ok:
        forget_disconnected_target(serial)
    return record_action("connect", ok, f"{' | '.join(messages)} | adb state={state}", serial)


def disconnect_device(serial: str) -> dict[str, Any]:
    if not serial:
        return record_action("disconnect", False, "serial is required")
    MANUAL_DISCONNECT_UNTIL[serial] = time.time() + 120

    config = load_config()
    backend = hub_backend()
    acroname = acroname_control_for_serial(serial, config)
    if backend == "acroname" and acroname:
        before_serials = adb_serials()
        result = run_acroname_port_action(serial, acroname, "off")
        missing_serials, current_serials = wait_for_serials_missing_from_baseline(before_serials)
        verified = serial in missing_serials or (serial in before_serials and serial not in current_serials)
        last_state = "absent" if verified else adb_state_for_serial(serial)
        if missing_serials and not verified:
            for missing_serial in sorted(missing_serials):
                remember_known_device(
                    missing_serial,
                    {
                        "acroname_control": dict(acroname),
                        "power_state": "off",
                        "mapping_status": "learned-from-conflict",
                        "mapping_conflict_with": serial,
                    },
                )
            remember_known_device(
                serial,
                {
                    "acroname_control": dict(acroname),
                    "power_state": "on",
                    "mapping_status": "suspect",
                },
            )
            write_log(
                "acroname_mapping_conflict",
                {
                    "serial": serial,
                    "port": acroname.get("port"),
                    "hub_model": acroname.get("model", "USBHub3c"),
                    "hub_serial": acroname.get("hub_serial") or acroname.get("serial_number") or "",
                    "requested_serial": serial,
                    "disappeared_serials": sorted(missing_serials),
                    "current_serials": sorted(current_serials),
                    "message": (
                        "Acroname port action affected different ADB serial(s); "
                        "the stored serial-to-port mapping is stale or wrong"
                    ),
                },
            )
        elif verified:
            remember_known_device(serial, {"acroname_control": dict(acroname), "power_state": "off"})
            if not result["ok"]:
                write_log(
                    "hub_port_effective_disconnect",
                    {
                        "backend": "acroname",
                        "serial": serial,
                        "port": acroname.get("port"),
                        "hub_model": acroname.get("model", "USBHub3c"),
                        "hub_serial": acroname.get("hub_serial") or acroname.get("serial_number") or "",
                        "result_ok": result["ok"],
                        "message": (
                            "Acroname reported a port action error, but ADB verified the target serial disappeared"
                        ),
                    },
                )
        else:
            remember_known_device(
                serial,
                {
                    "acroname_control": dict(acroname),
                    "power_state": "on",
                    "mapping_status": "suspect",
                },
            )
        return record_action(
            "disconnect",
            verified,
            "Acroname port disable requested; adb verification="
            f"{'absent' if verified else 'still ' + last_state}; "
            f"adb disappeared={sorted(missing_serials)}; {result['message']}"
            f"{'; warning: hub API reported failure but ADB verified disconnect' if verified and not result['ok'] else ''}",
            serial,
        )
    if backend == "acroname" and brainstem_available():
        return record_action(
            "disconnect",
            False,
            "No current Acroname port mapping for this serial. Run Refresh Acroname port map first; "
            "the Android-side USB disable fallback is not a reliable computer-side disconnect.",
            serial,
        )
    if backend == "uhubctl":
        uhubctl_target = uhubctl_target_for_serial(serial, config)
        if uhubctl_target:
            remember_disconnected_target(serial, uhubctl_target)
            result = power_off_linux_hub_port(serial, uhubctl_target)
            verified, last_state = wait_for_adb_absent(serial)
            return record_action(
                "disconnect",
                result["ok"] and verified,
                "hub port power off requested "
                f"via {uhubctl_target.get('source', '-')}; adb verification="
                f"{'absent' if verified else 'still ' + last_state}; {result['message']}",
                serial,
            )
        record_action(
            "disconnect-info",
            False,
            "no uhubctl target could be inferred or configured; falling back to Android-side USB data command",
            serial,
        )
    if not shutil.which("adb"):
        return record_action("disconnect", False, "adb is not installed or not on PATH", serial)
    command = ["adb", "-s", serial, "shell", "svc", "usb", "setFunctions", "none"]
    result = run_command(command, timeout=8)
    output = (result.stdout + result.stderr).strip()
    verified, last_state = wait_for_adb_absent(serial)
    if result.returncode == 0:
        if verified:
            return record_action(
                "disconnect",
                True,
                f"Android USB data disable requested and verified absent from adb; command output: {output or '-'}",
                serial,
            )
        return record_action(
            "disconnect",
            False,
            "Android accepted the USB data disable command, but adb still reports "
            f"the device as {last_state}. This phone may ignore svc usb setFunctions none; "
            "configure a uhubctl-controlled hub port for a true computer-side disconnect.",
            serial,
        )
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
        if now < MANUAL_DISCONNECT_UNTIL.get(serial, 0):
            continue
        if device["state"] not in {"offline", "unknown"}:
            continue
        if now - AUTO_RECONNECT_ATTEMPTS.get(serial, 0) < interval_seconds:
            continue
        AUTO_RECONNECT_ATTEMPTS[serial] = now
        threading.Thread(target=recover_device, args=(serial, "adb-state", False), daemon=True).start()

    current_serials = {device["serial"] for device in devices}
    for serial in configured_devices(config):
        if now < MANUAL_DISCONNECT_UNTIL.get(serial, 0):
            continue
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
    record_adb_device_events(android_devices)
    auto_reconnect_if_needed(android_devices, config)
    for device in android_devices:
        device["acroname_control"] = acroname_control_for_serial(device["serial"], config) if hub_backend() == "acroname" else {}
        device["uhubctl_target"] = uhubctl_target_for_serial(device["serial"], config) if hub_backend() == "uhubctl" else {}
        device["power_target"] = power_target_for_serial(device["serial"], config)
    usb_android = [device for device in usb_devices if device.is_android_candidate]
    current_serials = {device["serial"] for device in android_devices}
    now = time.time()
    for serial in current_serials:
        if now < MANUAL_DISCONNECT_UNTIL.get(serial, 0):
            continue
        forget_disconnected_target(serial)
    known_devices = known_devices_snapshot()
    missing_configured = [
        {
            "serial": serial,
            "name": str(device_config.get("name") or serial),
            "recovery_plan": recovery_plan_for_serial(serial, config),
            "last_attempt_at": AUTO_RECONNECT_ATTEMPTS.get(serial, 0),
            "power_target": power_target_for_serial(serial, config),
            "reason": "configured",
        }
        for serial, device_config in configured_devices(config).items()
        if serial not in current_serials
    ]
    for serial, known in known_devices.items():
        if serial in current_serials:
            continue
        if any(device["serial"] == serial for device in missing_configured):
            continue
        power_state = str(known.get("power_state") or "")
        if power_state == "on":
            continue
        target = known.get("acroname_control", {}) if hub_backend() == "acroname" else {}
        if isinstance(target, dict) and target.get("port") is not None:
            missing_configured.append(
                {
                    "serial": serial,
                    "name": str(known.get("name") or known.get("model") or serial),
                    "recovery_plan": [
                        f"acroname on port={target['port']}",
                        "adb reconnect",
                    ],
                    "last_attempt_at": AUTO_RECONNECT_ATTEMPTS.get(serial, 0),
                    "power_target": target,
                    "reason": power_state or "known-missing",
                }
            )
            continue
        target = known.get("uhubctl_target", {}) if hub_backend() == "uhubctl" else {}
        if not isinstance(target, dict) or not target.get("location") or not target.get("port"):
            continue
        missing_configured.append(
            {
                "serial": serial,
                "name": str(known.get("name") or known.get("model") or serial),
                "recovery_plan": [
                    f"uhubctl on location={target['location']} port={target['port']}",
                    "adb reconnect",
                ],
                "last_attempt_at": AUTO_RECONNECT_ATTEMPTS.get(serial, 0),
                "power_target": target,
                "reason": power_state or "known-missing",
            }
        )
    if hub_backend() == "uhubctl":
        with ACTION_LOCK:
            remembered_targets = {
                serial: dict(target)
                for serial, target in DISCONNECTED_TARGETS.items()
                if serial not in current_serials
            }
        for serial, target in remembered_targets.items():
            if not any(device["serial"] == serial for device in missing_configured):
                missing_configured.append(
                    {
                        "serial": serial,
                        "name": serial,
                        "recovery_plan": [
                            f"uhubctl on location={target['location']} port={target['port']}",
                            "adb reconnect",
                        ],
                        "last_attempt_at": AUTO_RECONNECT_ATTEMPTS.get(serial, 0),
                        "power_target": target,
                        "reason": "powered-off",
                    }
                )
    behind_hub_count = sum(1 for device in android_devices if device["behind_hub"]) + sum(
        1 for device in usb_android if device.parent_hubs
    )
    return {
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "platform": platform.system(),
        "usb_backend": usb_backend,
        "hub_backend": hub_backend(),
        "adb_available": bool(shutil.which("adb")),
        "acroname_available": brainstem_available(),
        "auto_reconnect_enabled": AUTO_RECONNECT_ENABLED,
        "config_path": CONFIG_PATH,
        "state_path": STATE_PATH,
        "log_dir": LOG_DIR,
        "run_id": RUN_ID,
        "run_log_path": text_log_file_path(),
        "latest_log_path": latest_text_log_path(),
        "mirror_status": mirror_status_snapshot(),
        "configured_device_count": len(configured_devices(config)),
        "active_actions": active_actions_snapshot(),
        "last_actions": last_actions_snapshot(),
        "diagnostics": dashboard_diagnostics(),
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
        print("Missing / powered-off devices:")
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
  <title>Android USB Device Pool</title>
  <style>
    :root { color-scheme: dark; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; }
    body { margin: 0; background: #0f1419; color: #e8edf2; }
    header { padding: 18px 24px; background: #131a21; border-bottom: 1px solid #26323d; position: sticky; top: 0; z-index: 2; box-shadow: 0 8px 30px rgba(0,0,0,.18); }
    h1 { margin: 0; font-size: 20px; letter-spacing: 0; }
    main { padding: 20px 24px 36px; max-width: 1280px; margin: 0 auto; }
    button { border: 1px solid #344554; background: #18222c; color: #e8edf2; border-radius: 6px; padding: 7px 10px; cursor: pointer; transition: transform .08s ease, background .12s ease, opacity .12s ease, border-color .12s ease; }
    button:hover { background: #22303c; border-color: #4b6377; }
    button:active { transform: translateY(1px); }
    button:disabled { cursor: wait; opacity: .55; }
    .danger { border-color: #8b463e; color: #ffb8ad; }
    .primary { border-color: #3c78aa; color: #bfe1ff; }
    .stats { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr)); gap: 12px; margin-bottom: 14px; }
    .stat, .device, .notice, .diag { background: #151d24; border: 1px solid #2a3742; border-radius: 8px; padding: 14px; }
    .stat b { display: block; font-size: 24px; margin-top: 3px; }
    .stat.good b { color: #44d184; }
    .stat.bad b { color: #ff806f; }
    .section-title { margin: 20px 0 9px; font-size: 16px; }
    .devices { display: grid; gap: 10px; }
    .device.ready { border-left: 5px solid #2fbf72; }
    .device.auth { border-left: 5px solid #4da3ff; }
    .device.warn { border-left: 5px solid #d99432; }
    .device.missing { border-left: 5px solid #d99432; }
    .device.failed { border-left: 5px solid #e05242; }
    .device.running { border-left: 5px solid #4da3ff; animation: pulseBorder 1.2s ease-in-out infinite; }
    .device.hub { border-left: 5px solid #7b8a96; }
    .name { font-weight: 700; margin-bottom: 7px; }
    .meta { color: #b6c3cf; font-size: 13px; line-height: 1.45; overflow-wrap: anywhere; }
    .actions { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 10px; }
    .tag { display: inline-block; padding: 2px 7px; border-radius: 999px; background: #25313b; color: #d7e0e8; margin-left: 8px; font-size: 12px; }
    .tag.ok { background: #193b2a; color: #9df0bf; }
    .tag.failed { background: #4a211d; color: #ffc0b8; }
    .tag.running { background: #173653; color: #bfe1ff; }
    .tag.event { background: #26323b; color: #c8d5df; }
    .hint { margin-top: 8px; padding: 9px 10px; border-radius: 6px; background: #102638; color: #bfe1ff; font-size: 13px; border: 1px solid #24445d; }
    .diag { border-left: 5px solid #e05242; margin-bottom: 10px; }
    .diag-title { font-weight: 700; margin-bottom: 4px; }
    .columns { display: grid; grid-template-columns: minmax(0, 1.25fr) minmax(360px, .75fr); gap: 16px; align-items: start; }
    @media (max-width: 980px) { .columns { grid-template-columns: 1fr; } }
    .spinner { display: inline-block; width: 10px; height: 10px; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: spin .8s linear infinite; margin-right: 6px; vertical-align: -1px; }
    @keyframes spin { to { transform: rotate(360deg); } }
    @keyframes pulseBorder { 0%, 100% { box-shadow: 0 0 0 rgba(43, 118, 183, 0); } 50% { box-shadow: 0 0 0 3px rgba(43, 118, 183, .16); } }
  </style>
</head>
<body>
  <header><h1>Android USB Device Pool</h1><div id="updated" class="meta"></div></header>
  <main>
    <section class="stats" id="stats"></section>
    <section class="notice" id="notice"></section>
    <section id="diagnostics"></section>
    <div class="columns">
      <section>
        <h2 class="section-title">Android Devices</h2>
        <section class="devices" id="android"></section>
        <h2 class="section-title">Missing / Powered Off Devices</h2>
        <section class="devices" id="missing"></section>
        <h2 class="section-title">USB / Hub Context</h2>
        <section class="devices" id="usb"></section>
      </section>
      <section>
        <h2 class="section-title">Running Actions</h2>
        <section class="devices" id="active"></section>
        <h2 class="section-title">Recent Actions</h2>
        <section class="devices" id="actions"></section>
      </section>
    </div>
  </main>
  <script>
    const statsEl = document.querySelector("#stats");
    const androidEl = document.querySelector("#android");
    const usbEl = document.querySelector("#usb");
    const missingEl = document.querySelector("#missing");
    const updatedEl = document.querySelector("#updated");
    const noticeEl = document.querySelector("#notice");
    const actionsEl = document.querySelector("#actions");
    const activeEl = document.querySelector("#active");
    const diagnosticsEl = document.querySelector("#diagnostics");

    function stat(label, value) {
      return `<div class="stat"><span>${label}</span><b>${value}</b></div>`;
    }

    async function doAction(action, serial = "") {
      markButtonBusy(action, serial);
      const response = await fetch("/api/action", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action, serial })
      });
      await response.json();
      await refresh();
      refreshLater(1500);
      refreshLater(3500);
      refreshLater(6500);
    }

    async function configureMirrorPath(startAfterSave = false) {
      const current = window.latestState && window.latestState.mirror_status
        ? window.latestState.mirror_status.scrcpy_dir
        : "";
      const value = window.prompt("Enter the scrcpy-win64 folder path on this Windows PC:", current || "C:\\\\Users\\\\digitaltwin\\\\Desktop\\\\scrcpy-win64-v3.3.4");
      if (!value) {
        return;
      }
      const response = await fetch("/api/mirror-config", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ scrcpy_dir: value })
      });
      const result = await response.json();
      await refresh();
      if (!result.ok) {
        alert(result.message || "Failed to save mirror path");
        return;
      }
      if (startAfterSave) {
        await doAction("start-mirror-script");
      }
    }

    function markButtonBusy(action, serial) {
      const selector = `button[data-action="${action}"][data-serial="${serial}"]`;
      document.querySelectorAll(selector).forEach((button) => {
        button.disabled = true;
        button.innerHTML = `<span class="spinner"></span>${button.textContent}`;
      });
    }

    function isSerialActive(state, serial) {
      return state.active_actions.some((action) => action.serial === serial);
    }

    function androidCard(device, state) {
      const active = isSerialActive(state, device.serial);
      const mirrorRunning = state.mirror_status && state.mirror_status.active_devices && state.mirror_status.active_devices.includes(device.serial);
      const mirrorFailed = state.mirror_status && state.mirror_status.failed_serials ? state.mirror_status.failed_serials[device.serial] : "";
      const cls = active ? "running" : device.state === "unauthorized" ? "auth" : device.needs_attention ? "warn" : "ready";
      const hub = device.behind_hub ? "Behind hub" : "Hub unknown";
      const evidence = device.hub_evidence.length ? `<div class="meta">Hub evidence: ${device.hub_evidence.join("; ")}</div>` : "";
      const mirrorFailure = mirrorFailed ? `<div class="hint">Mirror failed; auto restart paused. ${mirrorFailed}</div>` : "";
      const hubControl = device.acroname_control && device.acroname_control.port !== undefined
        ? `<div class="hint">Hub control target: Acroname ${device.acroname_control.model || "USBHub3c"} port ${device.acroname_control.port} (${device.acroname_control.source || "state"})</div>`
        : device.uhubctl_target && device.uhubctl_target.location
        ? `<div class="hint">Hub control target: uhubctl -l ${device.uhubctl_target.location} -p ${device.uhubctl_target.port} (${device.uhubctl_target.source})</div>`
        : state.hub_backend === "acroname"
        ? `<div class="hint">No current Acroname port mapping. Run Refresh Acroname port map after phones are replugged or moved.</div>`
        : `<div class="hint">No controllable Hub port inferred yet. Disconnect will fall back to Android-side USB data disable.</div>`;
      const disabled = active ? "disabled" : "";
      return `<article class="device ${cls}">
        <div class="name">${device.serial}<span class="tag">${device.state}</span><span class="tag">${hub}</span></div>
        <div class="meta">Model: ${device.model || "-"} · Product: ${device.product || "-"} · Device: ${device.device || "-"}</div>
        <div class="meta">ADB USB path: ${device.usb_path || "-"} · Transport: ${device.transport_id || "-"}</div>
        <div class="meta">${device.status_hint}</div>
        ${evidence}
        ${mirrorFailure}
        ${hubControl}
        <div class="actions">
          <button class="primary" data-action="reconnect" data-serial="${device.serial}" ${disabled} onclick="doAction('reconnect', '${device.serial}')">Reconnect</button>
          <button data-action="recover" data-serial="${device.serial}" ${disabled} onclick="doAction('recover', '${device.serial}')">Recover</button>
          <button data-action="verify" data-serial="${device.serial}" ${disabled} onclick="doAction('verify', '${device.serial}')">Verify</button>
          <button class="danger" data-action="disconnect" data-serial="${device.serial}" ${disabled} onclick="doAction('disconnect', '${device.serial}')">Disconnect and verify</button>
          ${state.mirror_status.supported && !mirrorRunning ? `<button data-action="start-mirror-device" data-serial="${device.serial}" onclick="doAction('start-mirror-device', '${device.serial}')">${mirrorFailed ? "Retry Mirror" : "Start Mirror"}</button>` : ""}
          ${mirrorRunning ? `<button class="danger" data-action="stop-mirror-device" data-serial="${device.serial}" onclick="doAction('stop-mirror-device', '${device.serial}')">Stop Mirror</button>` : ""}
        </div>
      </article>`;
    }

    function missingCard(device, state) {
      const active = isSerialActive(state, device.serial);
      const disabled = active ? "disabled" : "";
      const target = device.power_target && device.power_target.type === "acroname"
        ? `<div class="hint">Power target: Acroname ${device.power_target.model || "USBHub3c"} port ${device.power_target.port} (${device.power_target.source || "state"})</div>`
        : device.power_target && device.power_target.location
        ? `<div class="hint">Power target: uhubctl -l ${device.power_target.location} -p ${device.power_target.port} (${device.power_target.source})</div>`
        : `<div class="hint">No Hub power target is known for this missing device.</div>`;
      return `<article class="device missing">
        <div class="name">${device.name}<span class="tag">${device.serial}</span><span class="tag">${device.reason || "missing"}</span></div>
        <div class="meta">Recovery plan: ${device.recovery_plan.join(" -> ")}</div>
        ${target}
        <div class="actions">
          <button class="primary" data-action="connect" data-serial="${device.serial}" ${disabled} onclick="doAction('connect', '${device.serial}')">Power on / Connect</button>
          <button data-action="verify" data-serial="${device.serial}" ${disabled} onclick="doAction('verify', '${device.serial}')">Verify</button>
          <button class="primary" data-action="recover" data-serial="${device.serial}" ${disabled} onclick="doAction('recover', '${device.serial}')">Recover</button>
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
      if (action.event && !action.action) {
        const status = action.status || (action.ok === false ? "failed" : "event");
        const cls = status === "running" ? "running" : status === "failed" ? "failed" : "ready";
        const title = action.event;
        const serial = action.serial || action.target_serial || "all devices";
        const message = action.message || action.raw || JSON.stringify(action);
        return `<article class="device ${cls}">
          <div class="name">${title}<span class="tag ${status}">${status}</span></div>
          <div class="meta">${action.ts || action.timestamp || "-"} · ${serial}</div>
          <div class="meta">${message}</div>
        </article>`;
      }
      const status = action.status || (action.ok ? "ok" : "failed");
      const cls = status === "running" ? "running" : action.ok ? "ready" : "failed";
      const spinner = status === "running" ? `<span class="spinner"></span>` : "";
      return `<article class="device ${cls}">
        <div class="name">${spinner}${action.action}<span class="tag ${status}">${status}</span></div>
        <div class="meta">${action.timestamp} · ${action.serial || "all devices"}</div>
        <div class="meta">${action.message}</div>
      </article>`;
    }

    function activeCard(action) {
      return `<article class="device running">
        <div class="name"><span class="spinner"></span>${action.action}<span class="tag running">running</span></div>
        <div class="meta">${action.timestamp} · ${action.serial || "all devices"}</div>
        <div class="meta">${action.message}</div>
      </article>`;
    }

    function diagnosticCard(item) {
      return `<article class="diag">
        <div class="diag-title">${item.title}</div>
        <div class="meta">${item.message}</div>
      </article>`;
    }

    async function refresh() {
      const response = await fetch("/api/state");
      const state = await response.json();
      window.latestState = state;
      updatedEl.textContent = `Updated ${state.timestamp} · ${state.platform} · USB backend ${state.usb_backend} · Hub backend ${state.hub_backend} · ADB ${state.adb_available ? "available" : "not installed"}`;
      const mapButton = state.hub_backend === "acroname"
        ? `<button class="primary" data-action="map-acroname" data-serial="" onclick="doAction('map-acroname')">Refresh Acroname port map</button>`
        : "";
      const mirrorButton = state.mirror_status.running
        ? `<button class="danger" data-action="stop-mirror-script" data-serial="" onclick="doAction('stop-mirror-script')">Stop all mirrors</button>`
        : state.mirror_status.supported
        ? `<button class="primary" data-action="start-mirror-script" data-serial="" onclick="doAction('start-mirror-script')">Start all mirrors</button>`
        : state.platform === "Windows"
        ? `<button class="primary" type="button" onclick="configureMirrorPath(false)">Configure mirror path</button>`
        : `<button data-action="start-mirror-script" data-serial="" disabled>Install adb and scrcpy</button>`;
      const mirrorConfigButton = state.platform === "Windows"
        ? `<button type="button" onclick="configureMirrorPath(false)">Change mirror path</button>`
        : "";
      const mirrorArgs = state.mirror_status.scrcpy_args && state.mirror_status.scrcpy_args.length
        ? state.mirror_status.scrcpy_args.join(" ")
        : "-";
      noticeEl.innerHTML = state.adb_available
        ? `Auto recovery is ${state.auto_reconnect_enabled ? "enabled" : "disabled"}. Hub backend: ${state.hub_backend}. Config file: ${state.config_path}. Run log: ${state.latest_log_path}. Mirror launcher: ${state.mirror_status.script_path}. scrcpy: ${state.mirror_status.scrcpy_exe_exists ? "available" : "not found"}. Mirror args: ${mirrorArgs}.<div class="actions">${mapButton}${mirrorButton}${mirrorConfigButton}<button data-action="reconnect" data-serial="" onclick="doAction('reconnect')">Restart ADB discovery</button></div>`
        : `Install Android platform-tools and make sure adb is on PATH before using reconnect controls.`;
      diagnosticsEl.innerHTML = state.diagnostics.length
        ? state.diagnostics.map(diagnosticCard).join("")
        : "";
      statsEl.innerHTML = [
        `<div class="stat ${state.summary.adb_android_devices ? "good" : "bad"}"><span>ADB Android</span><b>${state.summary.adb_android_devices}</b></div>`,
        stat("Behind hub", state.summary.android_behind_hub),
        stat("Missing / powered off", state.summary.configured_missing),
        stat("Needs attention", state.summary.attention),
        stat("USB hubs", state.summary.hubs)
      ].join("");
      activeEl.innerHTML = state.active_actions.length
        ? state.active_actions.map(activeCard).join("")
        : `<article class="device ready"><div class="name">No actions running</div></article>`;
      androidEl.innerHTML = state.android_devices.length
        ? state.android_devices.map((device) => androidCard(device, state)).join("")
        : `<article class="device"><div class="name">No ADB Android devices detected</div><div class="meta">Connect the phone by USB, enable USB debugging, and authorize this computer.</div><div class="actions"><button onclick="doAction('reconnect')">Restart ADB discovery</button></div></article>`;
      missingEl.innerHTML = state.missing_configured_devices.length
        ? state.missing_configured_devices.map((device) => missingCard(device, state)).join("")
        : `<article class="device ready"><div class="name">No missing or powered-off devices</div></article>`;
      usbEl.innerHTML = state.usb_devices.length
        ? state.usb_devices.map(usbCard).join("")
        : `<article class="device"><div class="name">No USB context available</div><div class="meta">Install lsusb on Ubuntu, or run on Windows with PowerShell available.</div></article>`;
      actionsEl.innerHTML = state.last_actions.length
        ? state.last_actions.map(actionCard).join("")
        : `<article class="device"><div class="name">No actions yet in this service run</div><div class="meta">Current readable log: ${state.latest_log_path}</div></article>`;
    }

    function refreshLater(delayMs) {
      window.setTimeout(refresh, delayMs);
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
        if parsed.path == "/api/mirror-config":
            length = int(self.headers.get("Content-Length", "0"))
            payload = json.loads(self.rfile.read(length) or b"{}")
            result = remember_mirror_scrcpy_dir(str(payload.get("scrcpy_dir") or ""))
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if parsed.path != "/api/action":
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        action = payload.get("action", "")
        serial = payload.get("serial", "")
        result = run_action_async(action, serial)
        body = json.dumps(result, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), format % args))


def serve(host: str, port: int) -> None:
    try:
        server = ThreadingHTTPServer((host, port), MonitorHandler)
    except OSError as exc:
        write_log("service_start_failed", {"host": host, "port": port, "error": str(exc)})
        print(f"Could not start USB Android Monitor at http://{host}:{port}: {exc}", file=sys.stderr)
        print("If the port is already in use, stop the old service or start with --port 8766.", file=sys.stderr)
        raise SystemExit(1) from exc
    write_log(
        "service_started",
        {
            "host": host,
            "port": port,
            "config_path": CONFIG_PATH,
            "state_path": STATE_PATH,
            "log_dir": LOG_DIR,
            "run_id": RUN_ID,
            "run_log_path": text_log_file_path(),
            "latest_log_path": latest_text_log_path(),
            "python": sys.version,
            "adb_available": bool(shutil.which("adb")),
            "brainstem_available": brainstem_available(),
            "uhubctl_available": bool(shutil.which("uhubctl")),
            "hub_backend": hub_backend(),
        },
    )
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

    connect_parser = subparsers.add_parser("connect", help="power on a remembered/configured hub port and reconnect")
    connect_parser.add_argument("serial")

    recover_parser = subparsers.add_parser("recover", help="run the recovery ladder for one configured device")
    recover_parser.add_argument("serial")

    verify_parser = subparsers.add_parser("verify", help="verify whether one serial is absent from ADB")
    verify_parser.add_argument("serial")

    disconnect_parser = subparsers.add_parser("disconnect", help="try to disable USB data on one Android device")
    disconnect_parser.add_argument("serial")

    subparsers.add_parser("map-acroname", help="auto-map Acroname ports to visible ADB serials")

    args = parser.parse_args()
    initialize_hub_backend()
    if args.command == "list":
        print_table(snapshot())
    elif args.command == "watch":
        watch(args.interval)
    elif args.command == "serve":
        serve(args.host, args.port)
    elif args.command == "reconnect":
        print(json.dumps(reconnect_device(args.serial), ensure_ascii=False, indent=2))
    elif args.command == "connect":
        print(json.dumps(connect_device(args.serial), ensure_ascii=False, indent=2))
    elif args.command == "recover":
        print(json.dumps(recover_device(args.serial), ensure_ascii=False, indent=2))
    elif args.command == "verify":
        print(json.dumps(verify_device(args.serial), ensure_ascii=False, indent=2))
    elif args.command == "disconnect":
        print(json.dumps(disconnect_device(args.serial), ensure_ascii=False, indent=2))
    elif args.command == "map-acroname":
        print(json.dumps(map_acroname_ports(), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
