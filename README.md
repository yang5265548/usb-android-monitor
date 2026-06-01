# USB Android Monitor

Local monitor for Android phones connected by USB, focused on Win11 and Ubuntu environments
where phones are usually connected through a USB hub.

The monitor uses `adb devices -l` as the source of truth for Android phone state, then adds
USB/hub context from the operating system where available.

## Supported Targets

- Ubuntu: `adb` plus optional `lsusb` for USB/hub context.
- Win11: `adb` plus PowerShell PnP enumeration for USB context.
- macOS: still supported for development through `system_profiler`.

## Requirements

Install Android platform-tools and make sure `adb` is on `PATH`.

Ubuntu:

```sh
sudo apt install android-tools-adb usbutils
```

Win11:

1. Install Android SDK Platform-Tools.
2. Add the platform-tools folder to `PATH`.
3. Enable USB debugging on the phone.
4. Authorize the computer on the phone when prompted.

## Recovery Model

Remote phone labs usually fail in two different ways:

- ADB state failure: the phone still exists on USB, but `adb devices` shows `offline`,
  `unknown`, or no usable transport. The app can usually recover this with ADB reconnects.
- USB bus disappearance: the phone disappears from the operating system. At this point ADB
  cannot address the phone anymore, so the app needs a configured Hub port and a tool that can
  power-cycle or restart that port.

For Ubuntu, use a Hub supported by `uhubctl` if you need software-controlled unplug/replug.
For Win11, the app can attempt a `pnputil /restart-device` for a configured USB instance id,
but true per-port power cycling depends on the Hub hardware and driver.

For Acroname hubs on Win11, install the BrainStem Python package and use the dashboard's
`Auto-map Acroname ports` action. It briefly disables each Acroname port, watches which ADB
serial disappears, re-enables the port, and saves the learned serial-to-port mapping in
`usb_android_monitor_state.json`. The BrainStem port number is zero-based. For example, if
discovery reports `serial:C194E2FB`, configure `hub_serial` as `0xC194E2FB` once at the hub level.
The default scan list skips port 0 because on USBHub3c it is often the upstream/control path.
If you explicitly add port 0 to the configured `ports` list, the mapper scans it last and aborts
if that port drops multiple ADB devices or prevents the hub from reconnecting.

On Linux, the app can infer a likely `uhubctl` target from ADB USB paths. For example,
`usb:1-2.2` maps to `uhubctl -l 1-2 -p 2`, and `usb:2-2.3` maps to
`uhubctl -l 2-2 -p 3`. The dashboard shows this inferred command on each phone card.
If the Hub supports per-port power switching, `Disconnect and verify` uses that target before
falling back to the less reliable Android-side USB data command.

If the dashboard reports `USB permission problem`, first test by running the server with `sudo`.
If it reports `No controllable hub was found`, run `sudo uhubctl` and check whether locations
such as `1-2` or `2-2` are listed. If they are not listed, the current Hub is visible in USB
topology but cannot be power-cycled by `uhubctl`.

After a successful `Disconnect and verify`, the device moves to the missing list with a
`Power on / Connect` button. `Restart ADB discovery` only restarts ADB; it does not power a Hub
port back on. After powering a Hub port back on, the app waits up to 25 seconds for the serial to
return in ADB before marking the connect action as failed.

The monitor also stores learned phone-to-Hub mappings in `usb_android_monitor_state.json`. This
matters after a real power-off: once the phone is gone from ADB, the app cannot rediscover its
Hub port from the phone itself. If the state file exists, restarting the service still shows the
powered-off phone in the missing list and can run the remembered `uhubctl` or Acroname power-on
operation.

Copy the example config and fill in your real device serials:

```sh
cp usb_android_monitor_config.example.json usb_android_monitor_config.json
```

Example:

```json
{
  "auto_recovery": {
    "enabled": true,
    "cooldown_seconds": 60,
    "power_cycle_missing": false
  },
  "acroname": {
    "model": "USBHub3c",
    "hub_serial": "0xC194E2FB",
    "ports": [1, 2, 3, 4, 5]
  },
  "devices": {
    "R58N123456": {
      "name": "Pixel 6 rack slot 1",
      "uhubctl": {
        "enabled": true,
        "location": "1-1",
        "port": "2"
      },
      "windows_instance_id": "USB\\VID_18D1&PID_4EE7\\R58N123456"
    }
  }
}
```

Set `power_cycle_missing` to `true` only after verifying that the configured Hub port is correct,
because power cycling the wrong port can disconnect another phone.

## Quick Start

```sh
python3 usb_android_monitor.py list
python3 usb_android_monitor.py watch
python3 usb_android_monitor.py serve
```

Open the dashboard at <http://127.0.0.1:8765> after running `serve`.

## Commands

- `list`: print the current USB and ADB state once.
- `watch`: keep polling and print Android connect/disconnect/change events.
- `serve`: start a local HTTP dashboard and JSON API with reconnect controls.
- `reconnect --serial SERIAL`: restart the ADB transport for one phone.
- `reconnect`: restart ADB discovery for all phones.
- `map-acroname`: auto-map Acroname ports to currently visible ADB serials. This briefly
  disconnects each Acroname port, so run it only when phones are idle.
- `connect SERIAL`: power on a remembered/configured Hub port, then restart ADB discovery.
- `recover SERIAL`: run the full recovery ladder for a configured phone.
- `verify SERIAL`: verify whether the serial is absent from `adb devices`.
- `disconnect SERIAL`: disconnect and verify. If the device has a configured `uhubctl` Hub port
  on Linux, this powers off that port and verifies the serial disappears from ADB. Otherwise it
  falls back to `adb shell svc usb setFunctions none` and reports failure if ADB still sees the
  phone afterward.

Install BrainStem on Win11:

```powershell
python -m pip install --user --upgrade brainstem
```

## Notes

- Dashboard actions are asynchronous. A `*-queued` entry means the request was accepted; the
  final `disconnect`, `recover`, `reconnect`, or `verify` entry appears when the background check
  completes.
- The recent log also records ADB device events detected by polling, including
  `device-connected`, `device-disconnected`, and `device-changed`. The first dashboard refresh
  only establishes a baseline; later automatic disconnects and reconnects are logged.
- If the USB cable or hub is physically disconnected, software cannot force the device back.
  It can only restart ADB discovery, reset the operating system's USB device, or power-cycle a
  supported Hub port.
- The dashboard's automatic reconnect attempts recover common ADB states such as `offline`.
- Manual disconnect is verified against `adb devices` after the command. If the log says the
  device is still visible, the command did not create a real disconnect even if Android accepted
  it. For reliable computer-side disconnect/reconnect in a remote lab, configure `uhubctl` and
  use a Hub that supports per-port power switching.
- The phone UI may not show anything when USB data is disabled. Treat the computer-side evidence
  as authoritative: `adb devices -l` should no longer list that serial, and the dashboard should
  show one fewer ADB Android device.
- On Ubuntu, `adb devices -l` often includes a USB path such as `1-1.2`; the dot usually means
  the phone is behind a downstream hub.
- On Win11, exact hub ancestry depends on the USB driver stack. The app reports PnP USB context,
  and ADB remains the authority for Android connection state.
