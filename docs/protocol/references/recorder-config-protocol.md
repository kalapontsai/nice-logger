# Recorder configuration protocol (0x53 write / 0x73 read)

Static analysis of the native app's configuration command path. Every value below was
read out of the ARM64 code plus Il2CppDumper metadata; none of it is inferred from
documentation or from a guess.

## Where the code lives

| Function | RVA | Role |
|---|---|---|
| `DeviceManager::QueryConfigure` | `0xE39F80` | sends the read request |
| `DeviceManager::QueryAlarmThreshold` | `0xE39FC8` | sends the threshold read request |
| `DeviceManager::Query(string)` | `0xE3FE28` | converts the command string to an int type |
| `DeviceManager::InternalSendData` | `0xE3FB48` | shared sender |
| `DeviceManager::SendSetConfigure` | `0xE3A1F4` | sends the write request |
| `DeviceManager::SendSetThreshold` | `0xE3A240` | sends the threshold write request |
| `DeviceInfo::GetConfigureSetCommand` | `0xE3C460` | builds the `0x53` payload |
| `DeviceInfo::GetThresholdSetCommand` | `0xE3C6E8` | builds the `0x41` payload |
| `DeviceInfo::ParseQueryConfigure` | `0xE3CAF0` | parses the `0x73` response |
| `DeviceInfo::ParseQueryThreshold` | `0xE3CD3C` | parses the threshold response |
| `Utility::GetPureRaw` | `0xE211F4` | extracts one field from the raw string |
| `Utility::ConvertRawToInt16` | `0xE212D8` | field to signed 16-bit int |
| `Utility::ConvertRawToLong` | `0xE21460` | field to signed 64-bit int |
| `Utility::GetBitValue` | `0xE21110` | extracts one bit from an int |
| `BleMeterPlugin::DoSendData` | `0xE1A2FC` | builds the on-wire frame |

Use the annotated disassembler described in
`android-unity-il2cpp-reverse-engineering/references/capstone-annotated-disassembly.md`
to re-read any of these: without method names and relocation-resolved literals the
call sites are uninterpretable.

Annotated dumps of these functions already exist on disk under
`/home/bt994846/projects/nice-logger-reverse/rea/` — read them before re-running any
disassembly. `references/configure-read-path.md` lists which dump file holds which
function, and carries the instruction-level evidence for the `0x73` read path.

## The configuration structure

`DeviceInfo.Configure` (TypeDefIndex 4638) holds the recorder's configuration:

| Field | Offset | Type |
|---|---|---|
| `alarmType` | `0x10` | int |
| `startingMode` | `0x14` | int |
| `stopMode` | `0x18` | int |
| `loggingWay` | `0x1C` | int |
| `samplingRate` | `0x20` | int |
| `startDelay` | `0x24` | int |
| `alarmCumulatedTime` | `0x28` | int |
| `loggingStartTime` | `0x30` | long |
| `loggingStopTime` | `0x38` | long |

`DeviceInfo.Threshold` (TypeDefIndex 4639) holds one alarm threshold:

| Field | Offset | Type |
|---|---|---|
| `threshold` | `0x10` | string |
| `unit` | `0x18` | string |
| `hi` | `0x20` | double |
| `low` | `0x28` | double |
| `enabled` | `0x30` | bool |

The live `Configure` instance is reached through `DeviceInfo.m_Configure` at offset
`0x60`, which is why the config functions all read `this + 0x60` first.

## Raw field format

`Utility::GetPureRaw(raw, pos, length)` slices one field out of the response string:

- fields are **2 hex characters per unit**, each unit followed by a comma
- field `n` therefore starts at character offset `n * 3`
- a field of `length` units occupies `length * 3 - 1` characters (the trailing comma excluded)

`Utility::ConvertRawToInt16(raw, pos, length)` and `Utility::ConvertRawToLong(raw, pos,
length)` take the **whole response** plus a field position and a unit count, call
`GetPureRaw` internally, and then convert the extracted text with base 16 — **the base
is 16, not 10**. Passing a pre-sliced string to them misreads the field by one unit.

This hex-with-comma layout is why a config response cannot be read by splitting on
commas and treating each piece as a decimal number: each piece is a hex digit pair.

## Writing the configuration (opcode 0x53)

`GetConfigureSetCommand` builds the payload by appending to a `StringBuilder`, in this
order:

1. the literal `53`
2. a packed flags word built from the four mode fields
3. `samplingRate`
4. `startDelay`
5. `alarmCumulatedTime`
6. `loggingStartTime`
7. `loggingStopTime`

then the whole string is passed through `Utility::InsertHexSeparate` to become the
comma-separated hex form.

The packed flags word (step 2) is assembled from `alarmType`, `startingMode`,
`stopMode` and `loggingWay`:

- `alarmType` occupies the top bits (`<< 12`)
- `startingMode` occupies the next (`<< 11`)
- `stopMode` occupies the next (`<< 10`)
- `loggingWay` occupies the next (`<< 9`)
- `startDelay > 0` sets bit 8

so the word is `(alarmType << 12) | (startingMode << 11) | (stopMode << 10) |
(loggingWay << 9) | ((startDelay > 0) << 8)`.

The four values are individually single-bit fields in the device's response, which is
why the parser reads them with `Utility::GetBitValue` rather than as plain integers.

## Reading the configuration (opcode 0x73)

`ParseQueryConfigure` reads the response in this order:

1. `raw.Substring(3, 2)` converted with `Convert.ToInt32(s, 16)` gives the packed
   flags word
2. bit 7 of that word -> `startingMode`
3. bit 6 -> `stopMode`
4. bit 5 -> `loggingWay`
5. bit 3 -> `alarmType`
6. field 2 (2 hex units) -> `samplingRate`
7. field 4 (1 hex unit) -> `startDelay`, scaled by 10
8. field 5 (1 hex unit) -> `alarmCumulatedTime`, scaled by 10
9. field 6 (4 hex units) -> `alarmCumulatedTime` upper part / `loggingStartTime`,
   converted with `ConvertRawToLong`
10. field 10 (4 hex units) -> `loggingStopTime`, converted with `ConvertRawToLong`

The two timestamp fields are Unix-style seconds plus a fixed epoch offset of
`0x386d2760`; the parser adds that constant to each raw value before storing it in
`loggingStartTime` / `loggingStopTime`.

Note that the write path and the read path do not use the same scalar widths for
every field: the write path writes `samplingRate`, `startDelay` and
`alarmCumulatedTime` as decimal integers, while the read path reads `startDelay` and
`alarmCumulatedTime` as single hex units and then multiplies by 10. When comparing
the two directions, check the scaling before concluding they disagree.

## Opcodes

| Command | type | checkType | Payload |
|---|---|---|---|
| `SendSetThreshold` | `0x41` | `0x41` | threshold set payload |
| `SendSetConfigure` | `0x53` | `0x53` | config set payload |
| `QueryConfigure` | `0x73` | `0x73` | read request, no payload |
| `QueryAlarmThreshold` | `0x61` | `0x61` | threshold read request |

`0x73` and `0x61` are **not** present as immediates anywhere in the code. They are
runtime strings held in globals `0x1EE70A8` and `0x1EE70B0`; `Query(string)` calls
`ConvertRawToInt16` on the string to turn it into the numeric type. The literal text
is recovered by following each global slot's `R_AARCH64_RELATIVE` relocation to the
`Il2CppString` and reading its UTF-16LE payload.

Do not read the base of these two values as decimal: the conversion is
`Convert.ToInt32(s, 16)`, so `0x73` is the hex string `73` and means 115.

## Wire framing

The payload above is hex-encoded and then wrapped by `BleMeterPlugin::DoSendData`:

```
mac + "/" + hex(type_BE2 + checkType_BE2 + payload)
```

so the full `0x53` write for device `AA:BB:CC:DD:EE:FF` looks like
`AA:BB:CC:DD:EE:FF/00530053<hex payload>`, and the read request is
`AA:BB:CC:DD:EE:FF/00730073`.

## Verification and open items

Verified from the native code and metadata: the opcodes, the field order above, the
2-hex-unit-per-field raw format, the base-16 conversion, the bit positions of the
four mode flags, and the epoch offset on the two timestamps.

Not yet verified:

- the meaning of each numeric value of `alarmType`, `startingMode`, `stopMode` and
  `loggingWay` (only the bit positions and widths are known)
- the units of `samplingRate` and `startDelay`
- the exact threshold (`0x41`) payload field order
- no live device exchange has been captured for either direction

Keep those items labelled as unresolved until a native call site or a captured
exchange confirms them.