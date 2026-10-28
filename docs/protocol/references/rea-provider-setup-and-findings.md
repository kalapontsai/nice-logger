# REA deep-analysis provider setup and findings

REA 6.8.0 accepts **Ghidra 12.1.x only** (`SUPPORTED_GHIDRA_VERSION = 12.1.4`).
Ghidra 12.2 is rejected as `unsupported`. The provider also requires **JDK 21**.

Installed on this host:

- `GHIDRA_INSTALL_DIR=/home/bt994846/.local/opt/ghidra_12.1.4_PUBLIC`
- `JAVA_HOME=/home/bt994846/.local/opt/jdk-21.0.13+9` (Temurin 21.0.13+9)

Both came from the official NSA / Adoptium release pages and were verified against
the published SHA-256 digests. The system default Java was left untouched.

Config lives in `mcp.servers.rea.env`; `openclaw mcp reload` disposes the cached
runtime so the next agent build spawns REA with the new environment. **A REA MCP
child process started before the install keeps the old environment** — check its
start time (`ps -eo lstart,args | grep 'rea mcp'`) before concluding the provider
is still missing.

## CLI is the practical route for bulk decompilation

The MCP client deadline is shorter than a first-time Ghidra auto-analysis of a
33 MB ARM64 binary, so `procedure_pseudo_code` over MCP can time out. Use the CLI
instead, where the exec timeout is under our control:

```bash
export GHIDRA_INSTALL_DIR=/home/bt994846/.local/opt/ghidra_12.1.4_PUBLIC
export JAVA_HOME=/home/bt994846/.local/opt/jdk-21.0.13+9
export PATH="$JAVA_HOME/bin:$PATH"
rea decompile-all <libil2cpp.so> --provider ghidra \
  --output <dir> --layout address \
  --address-start 0xE18000 --address-end 0xE40000
```

`--output` must not already exist (or pass `--resume` for a matching export).
`--snapshot <file>` loads and updates a reusable analysis snapshot so later
single-function calls do not repeat the full auto-analysis.

Memory: this host has ~7.8 GB total, so Ghidra runs with `-Xmx2G` and swap is
required. **Run one Ghidra instance at a time.**

## Notify path (verified by static analysis, 2026-10-27)

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

## REA deep-analysis provider findings (2026-10-27)

REA 6.8.0 + Ghidra 12.1.4 + Temurin JDK 21 installed at:
- `GHIDRA_INSTALL_DIR=/home/bt994846/.local/opt/ghidra_12.1.4_PUBLIC`
- `JAVA_HOME=/home/bt994846/.local/opt/jdk-21.0.13+9`

Both from official NSA / Adoptium releases with SHA-256 verification. Config in
`mcp.servers.rea.env`; `openclaw mcp reload` applied. Existing Codex MCP child
processes keep the old environment until restarted.

**Bulk decompilation via CLI** (MCP client deadline < first-time Ghidra auto-analysis):
```bash
export GHIDRA_INSTALL_DIR=/home/bt994846/.local/opt/ghidra_12.1.4_PUBLIC
export JAVA_HOME=/home/bt994846/.local/opt/jdk-21.0.13+9
export PATH="$JAVA_HOME/bin:$PATH"
rea decompile-all <libil2cpp.so> --provider ghidra \  --output <dir> --layout address \
  --address-start 0xE18000 --address-end 0xE40000
```
Use `--snapshot <file>` to reuse analysis. Memory: ~7.8 GB total, Ghidra `-Xmx2G`,
swap required. **Run one Ghidra instance at a time.**

**Decompilation attempt result**: Single `decompile-all` job launched on 2026-10-27
(covering RVAs 0xE18000–0xE40000, the notify chain region) terminated with
`Ghidra startup deadline elapsed` before producing output. Host memory ~2 GB free +
5.9 GB swap. The auto-analysis phase did not complete within the exec timeout.

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

## Next verification steps (pending)

1. **Pi extended passive listen** — hold FFF2 subscription ≥30s after connect,
  capture 0x73/0x61 frames, decode with `ParseData` rules.
2. **nRF52840 + Wireshark** — capture native Android app 0x58 START_LOGGING
   command wire bytes (MAC + "/" + hex payload format confirmed).
3. **Second logger (18:48) on Pi** — replicate passive listen to confirm
   periodic notify is device behaviour, not unit-specific.
4. **C# notify reader** — `/home/bt994846/projects/nice-logger-cs/` using
   `Tmds.DBus` 0.95.1; subscribe PropertiesChanged on FFF2, decode per `ParseData`.
5. **UI at http://10.35.32.11/nicelogger** — auto-scan 10s, filter 88361,
   list per-logger decoded values (temp/humidity/battery).

