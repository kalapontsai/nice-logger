---
name: nice-logger-88361-protocol
description: Work on AZ Instrument 88361 Nice Logger BLE protocol recovery and client work on the Raspberry Pi 4 in Python (FastAPI/Bleak) or C# (.NET 8 / Tmds.DBus to BlueZ). Use when diagnosing scan/connect/notify behavior, mapping opcodes from the Unity IL2CPP app, recovering the recorder configuration payload (0x53 write / 0x73 read), maintaining the deployed FastAPI/Bleak bridge, debugging a stopped MQTT telemetry pipeline, mapping which backend a UI URL reaches, or building a cross-language notify reader. Covers verified BLE identifiers, native-app handshake, Bleak 3.x compatibility, evidence-based opcode mapping, the configuration protocol, deployment topology, the C# / Tmds.DBus port path, and safe recovery; does not authorize blind destructive probing.
---

# Nice Logger 88361 protocol

## Scope and evidence rule

This is the protocol-specific companion to `android-unity-il2cpp-reverse-engineering` and `raspberry-pi-4-imager-deploy`. Keep facts, static findings, and hypotheses separate. Do not publish an opcode map from guesses; label unresolved values as hypotheses until a native call-site or captured device exchange confirms them.

## Verified device and GATT identity

- Device observed as `NiceLogger`, address `3C:E4:B0:81:0E:81` during the verified run (address may change on other hardware).
- Manufacturer data company ID: `0x5002`; this was observed in the advertisement and used to rank 88361 candidates.
- Service: `0000fff0-0000-1000-8000-00805f9b34fb`
- Write characteristic: `0000fff1-0000-1000-8000-00805f9b34fb`
- Notify characteristic: `0000fff2-0000-1000-8000-00805f9b34fb`
- The Pi 4 run verified service discovery and properties: FFF1 = write, FFF2 = notify.

## Native-app connection sequence

The recovered DeviceManager flow and the manual agree on the important order:

1. Scan and choose the device.
2. Connect, discover services, and enable FFF2 notifications.
3. Send device time.
4. Query configuration.
5. Query alarm thresholds.
6. Only then present the configured device UI.

The manual states that real-time values are sent to the app every five seconds, and the LCD updates every five seconds. Therefore, a connected client receiving no RX is **not** evidence that the device is event-only. First verify the post-connect handshake, notification subscription, and parser; only then investigate opcodes.

## Bleak 3.x requirements

Use these APIs for the Pi 4 runtime (Bleak 3.0.2 was verified):

- `BleakScanner.discover(return_adv=True)` returns a dict of address to `(BLEDevice, AdvertisementData)` on the tested runtime. `BLEDevice.rssi` is absent; read `AdvertisementData.rssi`.
- Construct the client with pairing enabled: `BleakClient(address, pair=True, timeout=20)`. `pair=True` belongs to the constructor, not `connect()`.
- Let normal service discovery run; do not pass an empty service filter that results in zero services.
- Confirm FFF0/FFF1/FFF2 discovery before calling `start_notify` or writing.
- Use `write_gatt_char`, not the removed `write_characteristic`.
- Record every TX and RX packet with timestamp and raw hex before adding a parser.

Completion checks for a connection diagnosis:

- `systemctl is-active nice-logger-pi.service` is active.
- `/api/health` reports the selected address.
- Service discovery logs FFF1/write and FFF2/notify.
- A notification callback is confirmed independently of whether the payload parser recognizes it.

## RX observability in the FastAPI app

When the goal is to study unknown telemetry, add observability before adding a decoder:

1. In the Bleak notify callback, create one raw packet entry containing local timestamp, direction `RX`, characteristic UUID, byte length, and complete HEX.
2. Append it to the bounded in-memory packet log and return the entry from the logger helper.
3. Schedule a `state._emit("packet", entry)` on the API event loop; do not call WebSocket code directly from the Bleak callback thread.
4. Include a recent packet window in the WebSocket snapshot and render both snapshot packets and live `packet` events in the UI. Keep REST polling as a fallback.
5. Log the same raw RX at INFO level in journald when field capture is needed; this makes the evidence survive UI refreshes.

Deploy verification is separate from source verification: run `py_compile` first, copy only the changed files to `/home/kali/nice-logger-pi`, then restart `nice-logger-pi.service` and confirm the new process is running. A non-interactive SSH session may not have sudo rights; if restart is not completed, report the source as updated but the running service as unverified.

Completion check: a synthetic or real notify produces one packet-log entry, one WebSocket packet event, and one visible UI row with the same timestamp/HEX; the service restart is explicitly verified.

## Static opcode findings

These values were obtained by disassembling ARM64 functions in `libil2cpp.so` using the RVAs from Il2CppDumper 6.7.46 and Capstone. Each row was confirmed by reading the `mov w1, #TYPE; mov w2, #CHECKTYPE; bl 0xE3FB48` epilogue directly. They are verified for the named native functions, not a complete command map:

| Native function | type | checkType | Notes |
|---|---|---|---|
| `SendSetThreshold` | `0x41` | `0x41` | configures alarm thresholds |
| `SendSetDeviceTime` | `0x43` | `0x43` | pushes current device time |
| `SendSetID` | `0x49` | `0x49` | sets device ID/SN |
| `SendSetUnitAndPassword` | `0x50` | `0x50` | unit + password write |
| `SendSetConfigure` | `0x53` | `0x53` | general configuration |
| `SendLoggerState` | `0x58` | `0x58` | **start / stop logging** — type and checkType are both `0x58`; the start vs stop payload differs (static strings at VA `0x1ee70f8` / `0x1ee7100`, BSS-resolved at runtime), not the opcode |
| `ReadOut` | `0x64` | `0x0000` then `0xFFFF` | two back-to-back writes, the second with `checkType=0xFFFF` |

### Query opcodes recovered by relocation following

`QUERY_CONFIGURE` is `0x73` and `QUERY_ALARM_THRESHOLD` is `0x61`. Neither appears as an immediate in the code: `QueryConfigure` (`0xe39f80`) and `QueryAlarmThreshold` (`0xe39fc8`) load their command strings from runtime globals `0x1ee70a8` / `0x1ee70b0`, then `Query(string)` (`0xe3fe28`) converts the string to an int with `Convert.ToInt32(s, 16)`.

The literal text is recoverable statically: each global slot carries an `R_AARCH64_RELATIVE` relocation whose addend points at the real `Il2CppString`, so following the relocation yields the text `73` / `61`. The conversion base is **16**, so `0x73` is 115 decimal, not 115 hex-as-text.

### Read the existing dumps before re-disassembling

Annotated Capstone dumps of the query, parse and wire-builder functions already exist on disk under `/home/bt994846/projects/nice-logger-reverse/rea/` (`config-named.txt`, `func-dump*.txt`, `literal-slots.txt`, `query-opcodes.txt`, `literal-refs.txt`). List that directory and read the dumps before running any disassembly; re-deriving the same functions is wasted work. The per-file contents are listed in `references/configure-read-path.md`.

A query that appears to do nothing is usually the null device-list guard, not a bad opcode: `DeviceManager::Query` (`0xE3FE28`) returns without writing anything when `this + 0x50` is null, and it passes `checkType == type` for the query commands (0x73/0x73, 0x61/0x61).

`references/recorder-config-protocol.md` carries the recovered configuration protocol: the `0x53` write payload order, the `0x73` response field order, the four mode-flag bit positions, the 2-hex-unit-per-field raw format, and the timestamp epoch offset.

Still unresolved and must not be described as verified: READ_LIVE wire opcode; the exact `START_DATA` / `STOP_DATA` static payload strings (BSS pointers resolve at runtime, not from the static dump); and the FFF2 notify frame byte layout.

### Verified wire format (2026-10-26)

The write side is **not** a bare opcode byte. The native app builds:

```
mac + "/" + hex(type + checkType + payload)
```

`type` and `checkType` are each 2 bytes big-endian in the hex body; `payload` follows. Example for `SendLoggerState`:

```
AA:BB:CC:DD:EE:FF/00580058
```

Evidence:

- `InternalSendData(int type, int checkType, string mac, string data)` (RVA `0xe3fb48`) builds a `SendInfo` and dispatches on `type`: `cmp w8, #0x64` (READOUT) and `cmp w8, #0x43` (device time) select special paths; everything else takes the generic path at `0xe3e88c`, which calls the string builder at `0xe1a2fc` with the mac and the data string.
- The `/` split happens in that builder; a single-byte write to FFF1 is the anti-pattern.
- `SendInfo` field offsets read from the disassembly: `type` @`0x10`, `checkType` @`0x14`, `data` @`0x18`, `mac` @`0x20`, `state` @`0x28`. REA confirms the same field set: `type`, `checkType`, `data`, `mac`, `state`, `waitFrameToDequeue`, `startTime`, `TIME_WAIT`.

`ReadOut` therefore emits two frames: `type=0x64, checkType=0x0000` then `type=0x64, checkType=0xFFFF`.

### PC client implementation (pc-app/nice_logger)

The PC app mirrors the verified opcodes. `nice_logger/opcodes.py` is the single source of truth for opcode values and the frame builder; `ble_client.py` imports it. Do not reintroduce guessed opcodes.

```python
from nice_logger.opcodes import Opcode, build_wire_string
build_wire_string("AA:BB:CC:DD:EE:FF", 0x58, 0x58)   # 'AA:BB:CC:DD:EE:FF/00580058'
```

Implemented endpoints and their opcodes:

| Endpoint | Opcode | Status |
|---|---|---|
| `POST /api/device/time` | `0x43` | verified |
| `POST /api/device/id` | `0x49` | verified |
| `POST /api/device/unit_password` | `0x50` | verified |
| `POST /api/config` (threshold / configure) | `0x41` / `0x53` | verified |
| `POST /api/log/start`, `/api/log/stop` | `0x58` | verified |
| `POST /api/log/readout` | `0x64` x2 | verified |
| `GET /api/config` | `0x73` (QUERY_CONFIGURE) | opcode verified, **not implemented** |
| `GET /api/live` | READ_LIVE | **not implemented** |

Commands whose wire opcode is unresolved must be refused explicitly, never guessed. `read_live` in `ble_client.py` logs a warning and returns `False`; the API returns `ok: false` plus `unavailable_reason`. `get_config` must be implemented: the 0x73 opcode is verified, so refusing it as "opcode unknown" is wrong. The 0x73 response field layout is in `references/configure-read-path.md`.

When a client-side opcode enum disagrees with the table above, trust the table: it was read out of the binary call sites. Enum comments in a client are hand-written and drift. Re-derive from the call site before changing a recovered value.

Bleak 3.x note: use `write_gatt_char(WRITE_UUID, data, response=True)`. `write_characteristic` was removed in this runtime. The wire string is UTF-8 encoded and written as-is.

### REA route (reproducible, verified 2026-10-26)

The opcode table above was re-derived independently through the `REA` (Reverse Engineer Anything) MCP server plus Capstone, with no Ghidra/Hopper/IDA installed. The result matched the Il2CppDumper-derived table row for row, so the numbers are confirmed by two independent toolchains.

Route A — metadata layer via REA (needs no analysis engine):

```bash
# metadata version 31 here; REA accepts 24, 27, 29, 31, 39
npx -y rea-agents@6.8.0 inspect-il2cpp-metadata <global-metadata.dat> \
  --binary <libil2cpp.so> \
  --detail summary \
  --evidence_output <out>/il2cpp-evidence.json
```

The evidence bundle carries `types`, `methods`, `method_addresses`, `fields`, `field_types`, `parameters`, `parameter_types`, `return_types`, `string_literals`. On this build: metadata v31, 5182 types, 33131 methods, all 33131 method addresses linked (28633 `code`, 4498 `no_body`, 0 `unknown`).

Type indices in the bundle are array positions into `types`; `methods[i].declaring_type` is such an index, not a name. Resolve names before filtering:

- `DeviceManager` = type 4644, `DeviceInfo` = 4641, `DeviceInfoUtility` = 4642, `SendInfo` = 4668.
- `InternalSendData` (the shared sender) = RVA `0xe3fb48`, signature `(type:int, checkType:int, mac:string, data:string) -> void`.

Route B — instruction layer via Capstone (REA `instructions` needs an analysis engine and fails without one):

Read the `mov w1, #TYPE` / `mov w2, #CHECKTYPE` immediates that precede each `bl 0xe3fb48`:

| Function | RVA | type | checkType | call site |
|---|---|---|---|---|
| `SendSetDeviceTime` | `0xe3a18c` | `0x43` | `0x43` | `0xe3a1c8` / `0xe3a1d8` |
| `SendSetThreshold` | `0xe3a240` | `0x41` | `0x41` | `0xe3a268` / `0xe3a26c` |
| `SendSetConfigure` | `0xe3a1f4` | `0x53` | `0x53` | `0xe3a21c` / `0xe3a220` |
| `SendSetID` | `0xe3a28c` | `0x49` | `0x49` | `0xe3a2b8` / `0xe3a2bc` |
| `SendSetUnitAndPassword` | `0xe3a2d4` | `0x50` | `0x50` | `0xe3a300` / `0xe3a304` |
| `SendLoggerState` | `0xe3a5f8` | `0x58` | `0x58` | `0xe3a658` / `0xe3a65c` |
| `ReadOut` | `0xe3a31c` | `0x64` | `0x0000` then `0xFFFF` | `0xe3a3a8` (w2=wzr), `0xe3a3d0` |

Every verified row has type == checkType. `ReadOut` is the only exception: the first write uses `checkType=0`, the second `checkType=0xFFFF`.

Notes and traps:

- `SendInfo` fields confirmed by REA: `type`, `checkType`, `data`, `mac`, `state`, `waitFrameToDequeue`, `startTime`, `TIME_WAIT`.
- To scan for `bl` targets in an ELF, iterate executable PT_LOAD segments and decode the 26-bit signed immediate of the `BL` encoding; plain `grep` on bytes will not find them.
- **Not every sender uses `bl`.** `SendSetDeviceTime` (`0xe3a1e4`), `SendSetID` (`0xe3a2c4`) and `SendSetUnitAndPassword` (`0xe3a30c`) reach `InternalSendData` by a **tail call** (`b #0xe3fb48`). A scanner that only matches `bl` silently loses three of the seven opcodes. Match both `bl` and `b`.
- `checkType == 0` is emitted as `mov w2, wzr`, not `mov w2, #0`. A scanner that only parses `#`-immediates reports the `ReadOut` first write as unknown.
- Derive each function's extent from the REA `method_addresses` **sorted by address** (each function ends where the next begins). `methods[]` is ordered by metadata index, not address, so sorting that array gives wrong lengths.
- The operand immediates are not always adjacent to the call; walk **backwards** from the `bl`/`b` and take the most recent `w1`/`w2` assignment.
- `Il2CppString` layout in the static dump is `{klass, monitor, length:int32, chars}`; the string bytes start at `+0x10` and are UTF-16LE. Reading the length at `+0` instead yields garbage.
- Runtime-resolved globals read as 0 in a static dump, but their values are still recoverable by following the slot's `R_AARCH64_RELATIVE` relocation to the `Il2CppString`. `QueryConfigure` (`0xe39f80`) and `QueryAlarmThreshold` (`0xe39fc8`) load their command strings from globals `0x1ee70a8` / `0x1ee70b0`, then tail-call `Query(string)` at `0xe3fe28`, which converts the string to an int at `0xe212d8` and passes it as type. The relocation addends yield `73` and `61`, and the conversion base is 16, so `QUERY_CONFIGURE` = `0x73` and `QUERY_ALARM_THRESHOLD` = `0x61`. Treat a runtime global as unresolved only after checking its relocation.

## FFF2 notify payload shape

The FFF2 notify callback `OnReceivedDataFromDeviceSend(string mac, string raw)` receives a **comma-separated ASCII string**, not raw bytes. The native `ParseData` (RVA `0xE1B9D0`) splits on `,` (single-char `0x2C`) into at least 30 fields. Confirmed field index assignments from the disassembled branch table:

| Index | Field | Decoding |
|---|---|---|
| 5 | hex blob (battery + status) | high/low nibble pair |
| `0x0F` (15) | T1 temperature | `raw / 10.0` if `raw <= 0x7F9A`, else `0x7FFF - raw` |
| `0x11` (17) | humidity | large-value path uses `0x7FFFFF - raw` |
| `0x14` (20) | battery indicator | `raw / 10.0` |
| `0x15` (21) | humidity (alt) | `raw / 10.0` |
| `0x19` (25) | T2 temperature | same as T1 |
| `0x1B` (27) | T3 temperature | same as T1 |
| `0x1D` (29) | T4 temperature | same as T1 |

Two implications:

- A 20-30 °C ordinary signed-tenths value would land at `0x00C8-0x012C` LE (`C8 00` to `2C 01`). It is not enough to look for `7F xx` bytes in a notify frame and call them temperature; the `0x7F9A`/`0x7F9B` thresholds are sentinel regions, not normal data.
- Treat any Python client that writes a single byte to FFF1 as broken. The wire format on the write side is `mac + "/" + hex(type + checkType + payload)` (the `/` split happens in `_doSendData` at RVA `0xE1A47C`). Just sending `0x58` to FFF1 is the single-byte-opcode anti-pattern.

## Advertisement temperature decoding evidence

Static ARM64 disassembly of DeviceInfo.GetTemperature (RVA 0xE3D928) established a real decoder branch for the model/parameter path that uses offset 0x10: read a signed 16-bit value, return value/10 for ordinary values at or below 0x7F9A, and handle 0x7F9B as a special sentinel/branch. This is parser evidence, not proof that every raw manufacturer-data byte pair is a temperature.

For the observed advertisement 01 49 4E 69 63 65 6C 6F 67 01 A0 00 03 08 7F FD 7F 9B 00 00 0A 00 02 64, do not interpret 7F FD or 7F 9B as 20--30 C. The raw advertisement has not yet been mapped to the parser input string/offset convention, and these values are in the decoder special/high-value region. A 20--30 C ordinary signed-tenths value would be approximately 0x00C8--0x012C (little-endian bytes C8 00--2C 01), but absence of such bytes is not by itself proof that the advertisement lacks temperature.

Decision rule: preserve raw advertisement bytes, capture the exact string passed into the native parser or a real FFF2 notification, then correlate multiple samples against the disassembled offset and sentinel branches before adding a temperature parser.

## Safe investigation decision rule

For a passive dashboard that discovers multiple devices and collects per-device values, follow `references/multi-device-passive-monitoring.md` before changing scan, listener scheduling, notification parsing, or UI state. Keep one backend-owned monitor lifecycle and serialize links when the peripheral only supports one active connection.

Do **not** scan `0x00-0xFF` blindly against a real logger. The observed run showed that probing and repeated debug connections could clear pairing, produce BlueZ Page Timeout, and require device reset, pair removal, hci0 reset, and cooldown before recovery. Prefer, in order:

1. Reproduce the native-app handshake and passively capture FFF2 notifications.
2. Disassemble the caller of the named native wrapper with Ghidra/Capstone.
3. Use a narrowly scoped, reversible live test only after the user explicitly accepts the device-side risk.

After a Page Timeout or stale pairing state: stop probes, disconnect the client, remove the device from BlueZ, reset/power-cycle the logger if needed, bring hci0 back up after rfkill recovery, wait for cooldown, and verify advertisement before reconnecting. Do not immediately retry a batch.

## Windows-native backend (present, not the serving backend)

The Windows host has a native BLE backend tree, but its WinRT HCI link does not come
up, so it is **not** the backend that serves the UI. The serving backend is the Pi.

### Architecture

```
Browser -> Apache (:80) -> reverse proxy -> Pi FastAPI (:8000) -> BlueZ BLE -> 88361
```

- Apache proxy target: the Pi's LAN IP `10.35.102.143:8000`. Read `ProxyPass` from
  the live Apache config before claiming a topology; it does not point at the
  Windows backend. See `references/deployment-topology-and-url-mapping.md`.
- The Windows backend tree exists at `D:\docker\nicelogger-backend\`
  (Python 3.14 venv + FastAPI + bleak 3.0.2) for the WinRT port work, but
  `10.35.32.11:8000` is not listening, so nothing reaches it.
- When a container must reach the Windows host, use the host LAN IP
  (`10.35.32.11`), **not** `host.docker.internal` (that hostname does not resolve
  inside the container) and **not** the Docker gateway `172.30.0.1` (not routable to
  the host from this bridge network).
- `docker cp` must use a Windows-side source path (`D:\...`); a WSL UNC path
  (`\\wsl.localhost\...`) fails with Access Denied.

### bleak does not work on Windows for this device

bleak 3.x on the WinRT backend returns only the **cached** GATT services from the Windows
Bluetooth stack. On Windows 11 with the Realtek adapter the cache holds only standard
services (1800/1801/180A) - never FFF0/FFF1/FFF2 - so `write_gatt_char` / `start_notify`
are unusable.

Use `nice_logger/winrt_ble.py`, which calls `Windows.Devices.Bluetooth` directly:

- `BluetoothLEDevice.from_bluetooth_address_async` (takes an **int** MAC, not a string)
- `GattSession.from_device_id_async` then `maintain_connection = True` - without this,
  characteristic reads return Unreachable
- `get_gatt_services_for_uuid_async` / `get_characteristics_for_uuid_async` (take a
  `uuid.UUID` object, not a string)
- `write_client_characteristic_configuration_descriptor_async(NOTIFY)` to enable notify
  (returns status=0 on success); this is the API that works - writing the CCCD
  descriptor directly returns Access Denied
- `write_value_async(data, GattWriteOption.WRITE_WITH_RESPONSE)` - FFF1 has
  `characteristic_properties == 8` (WRITE only); WRITE_WITHOUT_RESPONSE is rejected

WinRT binding traps confirmed in this runtime:

- `TypedEventHandler` is only a type alias; register a plain callable
  (`characteristic.add_value_changed(handler)`).
- The property is `characteristic_properties`, not `properties`.
- `GattValueChangeStatus` does not exist in the `winrt` package.
- The descriptor method is `get_descriptors_for_uuid_async`; its `write_value_async`
  takes only the value (no option argument).
- `GattSession` exposes `session_status`, not `status`.

### The write path uses the wire string, on every platform

`nice_logger/ble_client.py` must build the native wire string before writing:

```
mac + "/" + hex(type + checkType + payload)     # UTF-8 encoded, written as-is
```

This applies to the Windows backend too. A backend that writes raw bytes such as
`[0x58, 0x58]` is the single-byte-opcode anti-pattern and will not be understood by
the device.

### Observed Windows link limitation (2026-10-27)

Discovery succeeds (FFF0 service, FFF1 props=8, FFF2 props=16, CCCD status=0), but the
underlying HCI link does not come up: `read_value_async` returns status 2/3
(InvalidParameter/Unreachable) and `write_value_async` returns Access Denied
(-2147024891). Retries do not help. This is a Windows Bluetooth-stack limitation, not a
protocol bug - the failure happens before any command is sent.

Candidate causes, in order:

1. Another BLE host still holds the link (88361 allows only one).
2. `BluetoothUserService_<suffix>` is Stopped; starting it needs SYSTEM rights, which a
   non-elevated session does not have.
3. Realtek adapter incompatibility; the Pi + BlueZ path remains the fallback.

### PowerShell `Start-Process` does not return

`Start-Process` spawns a child whose handle keeps the PowerShell pipeline open, so the
wrapper appears to hang and the agent reports `SIGTERM` / `overall-timeout` even though the
server started correctly. Do not read that as a failed deployment.

Pattern that avoids the confusion:

1. Kill the old process in one command.
2. Start the new process in a **separate** command that does not wait on it
   (background it, or redirect output to a log file).
3. Verify from a **third** command (`Get-NetTCPConnection -LocalPort 8000` plus
   `Invoke-WebRequest http://127.0.0.1:8000/api/health`).

Also check for duplicate servers before assuming a failure: two uvicorn processes may
compete for port 8000, in which case only one owns the listening socket.

## Deployment boundary

The deployed target is the separate project at `/home/bt994846/projects/nice-logger-pi`; the decompile worktree is research input, not the deployed Git repository. Verify the target repository status before editing or committing. For headless Pi SSH, use the existing `pi4` alias and the Raspberry Pi deployment skill’s sudo/systemd procedure. Never copy credentials into this skill or commit askpass files.

## C# / .NET 8 alternative (read-only notify path on Linux)

When the user wants a C# / .NET 8 client (verified target: Raspberry Pi running .NET 8 runtime), the project lives at `/home/bt994846/projects/nice-logger-cs/` and the protocol logic is a direct port of this skill's Python section (opcodes, decode rules, packet shape) into C# static classes.

**Do not use InTheHand.BluetoothLE for Linux targets.** Verified by inspecting the nupkg `lib/` tree of both 1.1.0 and 4.0.45 (latest as of 2026-10-19): the supported targets are `net462`, `net9.0`, `net9.0-android35.0`, `net9.0-ios18.0`, `net9.0-maccatalyst18.0`, `net9.0-windows10.0.19041`, `netstandard2.0`. There is no Linux RID, so the package references Windows-only `*.winmd` files and the build fails with `NETSDK1130` even on a clean `net8.0` target.

The verified path is `Tmds.DBus` 0.95.x (netstandard2.0, works on `net8.0`):

- `dotnet add package Tmds.DBus`
- Hand-define the BlueZ D-Bus interfaces in C# (`IBluezAdapter1`, `IBluezDevice1`, `IBluezGattService1`, `IBluezGattCharacteristic1`, `IDBusObjectManager`, `IDBusProperties`) — the surface is small, do not pull in a code generator for it.
- `Connection(Address.System)` for the system bus, then `CreateProxy<T>(busName, objectPath)`.
- Discover the target device by walking `IDBusObjectManager.GetManagedObjectsAsync()` and matching `org.bluez.Device1.Address`. **A direct MAC-based lookup API is not part of BlueZ D-Bus** — the device must already be in the BlueZ object tree (run `bluetoothctl scan on` on the host first to populate it).
- Subscribe to FFF2 by watching `org.freedesktop.DBus.Properties.PropertiesChanged` on the characteristic's object path; the `Value` property carries the raw bytes when a notify arrives.
- Write FFF1 with `IBluezGattCharacteristic1.WriteValueAsync(data, new Dictionary<string, object>())`; pass options as a plain `IDictionary<string, object>`, not the older D-Bus signature dict wrapper.
- Use `await using` for the `NiceLoggerClient` (it implements `IAsyncDisposable`); `using` alone will not compile when the type is `IAsyncDisposable` only.

Build gotchas observed while getting the project to compile: `IDBusObjectManager.WatchInterfacesAddedAsync` / `WatchInterfacesRemovedAsync` reference `DBusInterfaceAddedEventArgs` types that are not exported by `Tmds.DBus` 0.95.1 — omit those two method signatures from the interface; the on-demand `GetManagedObjectsAsync` walk is enough. `Connection` does not implement `IAsyncDisposable`; call `Dispose()` (synchronous) on the connection or wrap it in your own async-disposable holder.

Cross-check the project structure (verified 2026-10-19):

```
nice-logger-cs/
├── NiceLoggerReader.csproj      # <PackageReference Include="Tmds.DBus" Version="0.95.1" />
├── Program.cs                    # CLI 入口
├── Protocol/  (Cmd.cs, Packet.cs)
├── Decoding/  (Decoders.cs)
├── Models/    (Records.cs)
├── Ble/       (BlueZInterfaces.cs, NiceLoggerClient.cs)
└── README.md
```

Completion checks for a C# notify reader:

- `dotnet build -c Release` succeeds with zero errors and at most the unused-event warning for `OnDisconnected` (kept for the future disconnect hook).
- The binary links against `Tmds.DBus.dll`, not `InTheHand.*`.
- On the Pi, run as a user that can talk to the system D-Bus (default `kali` user is fine); the device is already in the BlueZ object tree from a prior `bluetoothctl scan on`.

## Known completion state

A successful deployment check is not the same as protocol completion. The service can be systemd-active, paired, connected, and able to write a packet while still receiving no RX. Report those states separately, and do not claim live temperature support until a real FFF2 notification has been captured and decoded.

## REA deep-analysis provider

Provider install, the `rea decompile-all` CLI route, the memory limits, and the
snapshot flag are in `references/rea-provider-setup-and-findings.md`. REA 6.8.0
accepts Ghidra 12.1.x only and needs JDK 21; the CLI is the practical route for
bulk decompilation because the MCP client deadline is shorter than a first-time
Ghidra auto-analysis of a 33 MB ARM64 binary.

### Notify path (verified by static analysis, 2026-10-27)

The native app's notify chain, recovered from `dump.cs` RVAs and the JADX output
of `com.innomind.blemeter.BleMeter`:

```
Android BLE callback (com.etek.ble.service.AbstractBLeService)
  onCharacteristicChanged(FFF2)
    -> receiveData(..., mode=2)
      -> onBTData(mac, bytes)
        -> BleMeter.onBTData: str2 = mac + "|," + ParseData(bytes)
          -> UnitySendMessage("BleMeterPlugin#", "OnWriteDataOk", str2)
            -> C# BleMeterPlugin.OnWriteDataOk(string address_returndata)   RVA 0xE194C8
              -> DeviceManager.OnReceivedDataFromDeviceSend(mac, raw)    RVA 0xE3EF64
                -> ParseData(raw)                                         RVA 0xE1B9D0
```

Key facts:

- The Java layer converts bytes to a **comma-separated uppercase hex string**
  before handing data to Unity. The C# side therefore never sees raw bytes.
- `BleMeterPlugin.OnWriteDataOk` receives `"<mac>|,<hex,hex,...>"` and splits on
  the `|` marker to recover the address before dispatching.
- Notification enablement happens entirely in Java: `AbstractBLeService.ReadData`
  checks `properties | 16` on FFF2 and calls `setCharacteristicNotification(...,
  true)`, which writes `ENABLE_NOTIFICATION_VALUE` to the CCCD. **The C# layer
  never writes the CCCD itself** — a PC client must do it explicitly.
- `AbstractBLeService.onServicesDiscovered` picks FFF0, reads FFF1 properties
  (4 = WRITE_WITHOUT_RESPONSE, 8 = WRITE), sleeps 500 ms, then calls `ReadData`.

### REA provider install and CLI route

Install paths, the CLI route, and the collected findings are in
`references/rea-provider-setup-and-findings.md` and in the REA route section above.

**Validated findings from prior toolchains** (JADX + Capstone + nRF Sniffer):

| Finding | Evidence | Status |
|---|---|---|
| Notify chain RVAs | Il2CppDumper 6.7.46 + dump.cs + JADX | Verified |
| `BleMeterPlugin.OnWriteDataOk` entry | RVA 0xE194C8 | Verified |
| `DeviceManager.OnReceivedDataFromDeviceSend` handler | RVA 0xE3EF64 | Verified |
| `ParseData` decoder | RVA 0xE1B9D0 | Verified |
| CCCD write by Java only | `AbstractBLeService.ReadData` | Verified |
| First notify ~10.8s after subscribe | nRF Sniffer capture | Verified |
| Notify period ~55 seconds | nRF Sniffer (multiple cycles) | Verified |
| FFF2 payload = comma-separated ASCII hex | `ParseData` splits on `,` | Verified |
| Temperature fields: idx 15/25/27/29 (T1–T4) | `ParseData` branch table | Verified |
| Humidity fields: idx 17/21 | `ParseData` branch table | Verified |
| Battery field: idx 20 | `ParseData` branch table | Verified |
| Sentinel thresholds 0x7F9A / 0x7F9B | `ParseData` branch logic | Verified |

**Pi passive probe gap** (2026-10-27):
- `passive_listen.py` ran 20s probe window, starting subscription at ~T+0.
- First device notification begins at ~T+10.8s (nRF log).
- Probe **missed the entire first notification cycle** (disconnected at T+28s).
- Device sends 0x73 (14B) then 0x61 (14B) as paired frames every ~55s.
- **Conclusion**: Pi probe window too short. Next test must hold subscription
  ≥30s after `start_notify` to capture the background broadcast.

**Windows WinRT limitation** (2026-10-27):
- Discovery succeeds (FFF0/FFF1/FFF2 visible, CCCD write status=0).
- `read_value_async` / `write_value_async` return Unreachable / Access Denied.
- Root cause: Windows Bluetooth stack HCI link not established (another host?
  `BluetoothUserService` stopped? Realtek adapter?).
- **Fallback remains Pi + BlueZ path** for reliable notify stream.

### Open verification steps

1. **nRF52840 + Wireshark** — capture the native Android app's 0x58 START_LOGGING
   command wire bytes to confirm the frame on a real channel.
2. **Second logger on Pi** — replicate the passive listen to confirm the periodic
   notify is device behaviour, not unit-specific.

## Pi web UI + MQTT telemetry pipeline (verified 2026-10-27)

Repo: `github.com/kalapontsai/nice-logger` (commit `c4d4c58`).
Deployed on Pi 4 `kali@100.102.221.71` → `/home/kali/nice-logger-pi`,
systemd unit `nice-logger-pi.service` (FastAPI on :8000).

### The wire-format bug (most important finding of this round)

The previously deployed client wrote **raw bytes** `[type][checkType][payload]`
to FFF1. That is the known anti-pattern — the native app sends an **ASCII string**:

```
mac + "/" + hex(type_BE2 + checkType_BE2 + payload)
```

| Command | Wire string |
|---|---|
| `SendLoggerState` | `AA:BB:CC:DD:EE:FF/00580058` |
| `SendSetDeviceTime` | `3C:E4:B0:81:18:48/00430043` |
| `ReadOut` phase 1 | `3C:E4:B0:81:18:48/00640000` |
| `ReadOut` phase 2 | `3C:E4:B0:81:18:48/0064ffff` |

`type`/`checkType` are each **2 bytes big-endian** in the hex body. The MAC is the
target device address, so a client cannot build this string without knowing which
device it is talking to. Verified reference implementation:
`pc-app/nice_logger/opcodes.py` → `build_wire_string()`.

Corollary: a client that only writes a bare opcode byte is broken, and a client
that writes the correct frame but omits the MAC prefix is *also* broken.

### MQTT topology (the Pi is self-sufficient)

```
88361 BLE broadcast (mfg 0x5002)
  └─▶ nicelogger_gateway.py  (Pi)  ──▶ MQTT sensor/nicelogger/<device_id>
                                             │
                                             ├─▶ nice_logger/mqtt_feed.py (Pi FastAPI)
                                             │      └─▶ /api/mqtt/records + WS "mqtt" event
                                             │             └─▶ web UI MQTT panel
                                             └─▶ Windows mqtt-worker ──▶ MySQL mqtt_messages
```

- Broker is **Windows** Docker container `mqtt-mosquitto` on `10.35.32.11:1883`.
  The WSL-local `docker ps` shows a *different, unrelated* `mosquitto` container in
  `Created` state — a red herring. Use `docker.exe` (Docker Desktop) for
  `D:\docker` deployments.
- Pi FastAPI env overrides: `NICELOGGER_MQTT_BROKER` / `_PORT` / `_TOPIC` / `_HISTORY`.
- Endpoints: `GET /api/mqtt/status`, `GET /api/mqtt/records?limit=N`.
- Gateway publish interval: the Pi deployment uses `REPORT_INTERVAL = 60.0`
  (one message per minute per device).

### Trap: a short subscribe window looks like "MQTT is broken"

With `REPORT_INTERVAL = 60`, a 20 s subscribe window receives **nothing**, which
reads as a dead pipeline. Subscribe for ≥90 s before concluding the broker or the
gateway is at fault. A decisive round-trip test (publish a probe topic and
subscribe to `sensor/nicelogger/#` in the same window) separates the cases fast.
This cost a full diagnostic cycle — check the publish interval first.

### The publisher must run under a supervisor, not a shell

`nicelogger_gateway.py` is the only component that reads BLE advertisements, so
when it dies the whole pipeline freezes while the subscriber still reports
`connected: true`. A stale `last_message_at` with a connected subscriber means the
publisher is dead, not the broker.

Give the publisher its own systemd unit with `Restart=always` and run it from
`nicelogger_env`, because `paho-mqtt` is not installed in the FastAPI service
venv. Verify the journal shows per-device publish lines, not just that the unit
is `active`.

### Trap: a gateway that logs "publishes" but publishes nothing (verified 2026-10-28)

`nicelogger_gateway.py` used to call `client.connect()` exactly once at startup and
swallow the exception. After a Pi reboot where Wi-Fi was not up yet, the initial
connect failed with `[Errno 101] Network is unreachable`; every later `publish()`
was then silently dropped while the journal still printed
`-> MQTT (sensor/nicelogger/logger_01)` once a minute. Dashboard symptom:
`/nicelogger-history` showed `資料過期` although the gateway looked healthy.

Fixed in commit `61b8b7f` and deployed to the Pi: `connect_async()` +
`loop_start()` so paho retries in the background, the `publish()` rc is checked
and logged (`已送出` / `等待重連（尚未送出）` / `發送失敗（rc=…）`), and a watchdog
logs every connect/disconnect transition.

Rules that follow from it:

- Never trust a journal line ending in `-> MQTT`; look for the `已送出` marker
  and for a live TCP session (`ss -tnp | grep 1883`).
- `publish()` on a disconnected paho client returns an error code instead of
  raising, so the rc must be inspected.
- When the dashboard goes stale, compare the newest `mqtt_messages.created_at`
  with the gateway journal before blaming the broker or the worker.

For the full tier-by-tier procedure (publisher vs broker vs subscriber, clock
checks, MySQL verification) use `references/telemetry-pipeline-stall-triage.md`.

### Apache reverse proxy (sub-path mount)

`D:\docker\apache\nicelogger-proxy.conf` → `/nicelogger/` → `http://10.35.102.143:8000/`
(the Pi, **not** the Windows backend, whose WinRT HCI link fails).

- The WebSocket `ProxyPass` must be listed **before** the general `/nicelogger/`
  rule, otherwise the HTTP rule captures it.
- The web UI therefore runs under a sub-path, so it must not hardcode `/api/...`.
  It computes `API_BASE` from `location.pathname` and builds every URL through
  `api(path)`. A UI that hardcodes absolute paths returns 404 under the proxy —
  this is the "multi-environment frontend" failure mode.
- Verified through the proxy: UI 200, REST 200, `/static/chart.umd.min.js` 200,
  WebSocket upgrade **HTTP 101**.
- The Pi answers on two addresses, so a URL can be correct and still look wrong: the
  Tailscale IP `100.102.221.71` and the LAN IP `10.35.102.143` are the same host, and
  the proxy target uses the LAN one. Use `references/deployment-topology-and-url-mapping.md`
  to re-derive which host a URL reaches instead of assuming.

### Pi operational notes

- `sudo` on the Pi needs a password, so a bare `sudo systemctl restart` over
  non-interactive SSH fails with "Interactive authentication required". Supply
  it with the `SUDO_ASKPASS` + `DISPLAY=` + `setsid` triple from
  `raspberry-pi-4-imager-deploy` §E, or use `sudo -S` with the password piped
  in for a single command. To avoid needing sudo at all for the FastAPI unit,
  `kill -9 $(systemctl show -p MainPID --value nice-logger-pi.service)` makes
  systemd bring it back (≈5 s) because the unit has `Restart=on-failure`.
- The service venv is `.venv`; `nicelogger_env` is a **separate** venv used by
  the gateway. `paho-mqtt` lives only in the latter until installed explicitly,
  so a publisher unit must point `ExecStart` at `nicelogger_env`.
- **The Pi has no RTC and its clock drifts.** UDP/123 is blocked on this network,
  so `systemd-timesyncd` never completes a sync and the clock keeps the time it
  had at boot, which can be days stale. Every pipeline timestamp then looks
  wrong and TLS clients on the box fail with not-yet-valid certificate errors
  that have nothing to do with certificates. Check `timedatectl` against a LAN
  host `Date:` header whenever timestamps look wrong, and prefer NTS over
  TCP/4460 (reachable) over UDP/123 (blocked) for the fix.
- The Pi LAN address is DHCP and changed on 2026-10-28: `10.35.102.143` →
  `10.35.31.13` (wlan0). Reach the Pi over Tailscale `100.102.221.71` (stable);
  a LAN-IP SSH command or URL fails silently after a reboot. `ssh pi4` uses the
  Tailscale name from `~/.ssh/config`; fall back to
  `ssh -i ~/.ssh/id_nice-logger-pi4 kali@<current-lan-ip>`.
- `/etc/systemd/system/nice-logger-pi.service` (FastAPI :8000) went missing and
  `systemctl` reported `not-found`; recreate it with
  `ExecStart=/home/kali/nice-logger-pi/.venv/bin/python -m uvicorn nice_logger.api:app --host 0.0.0.0 --port 8000`.
  `StartLimitIntervalSec` / `StartLimitBurst` belong in `[Unit]`, not
  `[Service]` (the old file had them in `[Service]`; systemd ignores them there).
- `mqtt_user` has `SELECT, INSERT, DELETE, CREATE, INDEX, ALTER` on `mqtt_store`
  but **no UPDATE**, so the MQTT settings web UI cannot persist topic changes
  and direct SQL `UPDATE` is denied. Change `mqtt_settings.topics` with the
  MySQL container's root account, then restart the `mqtt-worker` container.
- `mqtt-worker` topic validation rejects `+` / `#` wildcards, so subscribe to
  explicit topics (`sensor/nicelogger/logger_01`, `.../logger_02`).

### Verified end state

`logger_01` 24.17 °C / RSSI -50 / battery 100 % / 3 log points;
`logger_02` 24.22 °C / RSSI -45 / battery 100 %. Both persisted to MySQL via the
Windows worker and rendered in the Pi web UI MQTT panel.

