# Reading the recorder configuration (opcode 0x73)

The 0x73 read path is the counterpart of the 0x53 write path. Use this when a client
needs to pull the recorder configuration off a device, when a 0x73 frame comes back
and the fields need to be decoded, or when the write and read directions appear to
disagree.

## Where the code lives

| Function | RVA | Role |
|---|---|---|
| `DeviceManager::QueryConfigure` | `0xE39F80` | sends the 0x73 read request |
| `DeviceManager::QueryAlarmThreshold` | `0xE39FC8` | sends the 0x61 read request |
| `DeviceManager::Query(string)` | `0xE3FE28` | converts the command string to an int type and dispatches |
| `DeviceManager::InternalSendData` | `0xE3FB48` | shared sender |
| `DeviceInfo::ParseQueryConfigure` | `0xE3CAF0` | parses the 0x73 response |
| `DeviceInfo::ParseQueryThreshold` | `0xE3CD3C` | parses the 0x61 response |
| `Utility::GetPureRaw` | `0xE211F4` | slices one field out of the raw response |
| `Utility::ConvertRawToInt16` | `0xE212D8` | slices a field and converts it to an int |
| `Utility::ConvertRawToLong` | `0xE21460` | slices a field and converts it to a long |
| `Utility::GetBitValue` | `0xE21110` | extracts one bit from an int |

## Read the existing dumps before re-disassembling

Annotated Capstone dumps of these functions already exist on disk. Read them first;
re-running the disassembly is wasted work.

```bash
ls /home/bt994846/projects/nice-logger-reverse/rea/
```

| File | Contents |
|---|---|
| `config-named.txt` | annotated disassembly of every function above, in one file |
| `func-dump2.txt` | `ConvertRawToInt16`, `GetConfigureSetCommand`, `ParseQueryConfigure` |
| `func-dump3.txt` | `SendSetConfigure`, `SendSetThreshold`, the generic send path |
| `func-dump4.txt` | the wire-string builder `BleMeterPlugin::DoSendData` |
| `literal-slots.txt` | global slot -> `R_AARCH64_RELATIVE` addend map |
| `query-opcodes.txt` | the literal text behind the runtime command slots |
| `literal-refs.txt` | which slot each function loads, with the literal text |

The dumps carry `CALL DeviceManager::Query` and `LIT '73'` annotations, which is
what makes the call sites interpretable without a decompiler. See
`android-unity-il2cpp-reverse-engineering/references/capstone-annotated-disassembly.md`
for how the annotations are produced.

## The request path

`QueryConfigure` (`0xE39F80`) loads the command string from a runtime global slot
(`0x1EE70A8`), tail-calls `Query`, and passes the string through unchanged. `Query`
(`0xE3FE28`) converts that string to an int with base 16, so the slot literal text
`"73"` means the opcode `0x73` and the slot literal `"61"` means `0x61`.

`Query` then calls `InternalSendData(type, type, mac, cmd)`, so for the query
commands **checkType equals type**: 0x73/0x73 and 0x61/0x61.

`Query` returns false without sending when either guard fails:

- the converted type is zero (`cmp w0, #0`), or
- the device list pointer at `this + 0x50` is null.

When a query appears to do nothing, check the second guard first: a null device list
means the app never selected a device, and no frame is written to FFF1 at all.

## The response field layout

`ParseQueryConfigure` (`0xE3CAF0`) reads the response in this order. `pos` and
`length` are in 2-hex-character units; `length` is the unit count, not characters.

| Step | Call | Result |
|---|---|---|
| 1 | `ConvertRawToInt16(raw, 3, 2)` | packed flags word |
| 2 | `GetBitValue(flags, 7)` | `startingMode` -> `Configure+0x14` |
| 3 | `GetBitValue(flags, 6)` | `stopMode` -> `Configure+0x18` |
| 4 | `GetBitValue(flags, 5)` | `loggingWay` -> `Configure+0x1C` |
| 5 | `GetBitValue(flags, 3)` | `alarmType` -> `Configure+0x10` |
| 6 | `ConvertRawToInt16(raw, 2, 2)` | `samplingRate` -> `Configure+0x20` |
| 7 | `ConvertRawToInt16(raw, 4, 1)` x10 | `startDelay` -> `Configure+0x24` |
| 8 | `ConvertRawToInt16(raw, 5, 1)` x10 | `alarmCumulatedTime` -> `Configure+0x28` |
| 9 | `ConvertRawToLong(raw, 6, 4)` + `0x386D2760` | `loggingStartTime` -> `Configure+0x30` |
| 10 | `ConvertRawToLong(raw, 0xA, 4)` + `0x386D2760` | `loggingStopTime` -> `Configure+0x38` |

The `x10` scale is a signed multiply by 10 written as the magic constant
`0x66666667` (`smull` plus shifts). The epoch offset `0x386D2760` is added to both
timestamps.

## Slicing arithmetic

`GetPureRaw(raw, pos, length)` (`0xE211F4`) computes:

- start character offset = `pos * 3`
- substring length = `length * 3 - 1`
- then `Replace(",", "")` removes the separator

So the raw response is a **comma-separated list of 2-hex-character units**, and
`pos` is a unit index, not a character index. Splitting the response on commas and
treating each piece as a decimal number is wrong twice: the pieces are hex digit
pairs, and a multi-unit field spans several pieces.

## ConvertRawToInt16 / ConvertRawToLong are field extractors

`ConvertRawToInt16(raw, pos, length)` (`0xE212D8`) and
`ConvertRawToLong(raw, pos, length)` (`0xE21460`) take the **whole response**
plus a field position and a unit count. They call `GetPureRaw` internally and then
convert the extracted text with base 16. Passing a pre-sliced string to them
misreads the field by one unit.

## Read and write directions use different widths

The write path (`GetConfigureSetCommand`, `0xE3C460`) writes `samplingRate`,
`startDelay` and `alarmCumulatedTime` as decimal integers, while the read path
reads `startDelay` and `alarmCumulatedTime` as single hex units and then
multiplies by 10. Check the scaling before concluding the two directions disagree.

## Verification

Report the field mapping as verified only when the read direction (parser positions)
and the write direction (builder append order) agree on field order, and when the
opcode itself is confirmed at a call site. A field order that only one direction
supports is a hypothesis.

Still unresolved: the meaning of each numeric value of `alarmType`,
`startingMode`, `stopMode` and `loggingWay` (only the bit positions and widths are
known), the units of `samplingRate` and `startDelay`, the exact threshold (`0x41`)
payload field order, and any live device exchange for either direction.
