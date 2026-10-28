# Multi-device passive BLE monitoring

Use this procedure when a browser dashboard should discover several BLE sensors and keep their readings current without exposing device-control actions.

## 1. Confirm what each radio path actually observes

1. Separate advertisement scanning from connected GATT notifications because they are different data sources.
2. On Windows with Bleak 3.x, call `BleakScanner.discover(timeout=window, return_adv=True)` and consume its `{address: (BLEDevice, AdvertisementData)}` mapping because `BLEDevice.details.advertisement_data` is not the portable WinRT metadata path.
3. Read RSSI, local name, service UUIDs, and manufacturer bytes from `AdvertisementData`; identify company `0x5002` from the `manufacturer_data` dictionary key rather than assuming its bytes are prefixed into the value.
4. Filter candidates using verified manufacturer company ID `0x5002` or a service UUID because local names alone are not a reliable model identifier.
5. Merge each scan window into a map keyed by normalized address and update `last_seen`; expire absent candidates only after a deliberate TTL because a periodic scan window can miss one logger while another advertises.
6. Store raw advertisement and notification with address, timestamp, RSSI or characteristic UUID, and complete raw bytes before decoding because parser evidence must be reproducible.
7. Add a temperature decoder only after the manufacturer-data or notification layout and field offsets are verified against real samples; never label arbitrary advertisement candidates as live values.

## 2. Choose the listener topology

1. Confirm whether the peripheral and adapter support multiple simultaneous GATT links before designing concurrency because one active link may be the device or platform limit.
2. If only one link is reliable, use one backend-owned monitor task and a single connection at a time; keep per-address readings in a map keyed by normalized Bluetooth address.
3. Keep one discovered device connected when it is the only candidate; rotate across a snapshot of multiple candidates only when more than one is present.
4. Use bounded listen windows and a per-address retry cooldown because repeatedly reconnecting a failing peripheral can clear pairing or trigger BLE timeouts.
5. Never send a start, query, reset, or guessed opcode in a passive monitor; subscribe to verified notification characteristics only.

## 3. Make monitor lifecycle idempotent

1. Expose an idempotent POST /api/monitor/start that creates a background task only if none is running because browser refreshes and multiple tabs must not start duplicate scanners.
2. Expose GET /api/monitor with running state, current address, last scan time, last error, filtered devices, and each device's own latest reading because the UI needs a single state snapshot.
3. Run advertisement discovery on a fixed backend cadence and preserve candidates in a map keyed by normalized address because periodic advertising or scan response packets can be missed by a single window.
4. Treat advertisement detection and GATT telemetry as separate states because increasing scan frequency cannot receive FFF2 notifications without a connected GATT client.
5. Serialize GATT connections through one backend-owned monitor task because multiple browser clients must not compete for the adapter; keep a sole logger connected continuously and rotate only when multiple candidates require sampling.
6. Use a listen window longer than the documented notification period plus connection and subscription time because short round-robin windows can repeatedly disconnect before the next periodic notify.
7. Do not set `active_address` or display “listening” until connect and FFF2 notify subscription both succeed because a candidate being attempted is not a live connection. Track candidate, connection state, notify-subscription state, last RX time, last RX payload, and connection error separately.
8. On Windows WinRT, do not verify an FFF2 notify-only characteristic by requiring `read_value_async` to succeed because a notify characteristic may not support reads; verify service/characteristic discovery, CCCD subscription status, and then the actual ValueChanged callback independently.
9. Follow the verified native-app post-connect sequence when notification capture requires it, but do not guess unresolved query opcodes or send writes during passive monitoring; capture the native exchange or leave the device in a clear “subscribed, no RX yet” state.
10. Test whether the target adapter supports scanning while a GATT link is active; if not, serialize scan and listen phases because concurrent WinRT scanning can interfere with connected notification reception.
11. Cancel scanner and listener tasks and close the active client during application shutdown because platform callbacks otherwise outlive the server lifecycle.
12. Import every module dependency used in the background loop and test one full scan iteration because a NameError can terminate monitoring while the HTTP process stays healthy.
13. Broadcast monitor state over WebSocket and retain REST status polling as a fallback because reconnecting browsers still need the latest snapshot. For a UI mounted at `/nicelogger/`, map REST to `/nicelogger/api/...` and WebSocket to `/nicelogger/ws` (strip the `/api` suffix before adding `/ws`) because appending `/ws` to the API base routes to a nonexistent `/nicelogger/api/ws`.

## 4. Associate and preserve readings correctly

1. Parse notifications with a strict shape check and retain the original raw value because malformed or unknown frames must not become plausible-looking temperatures.
2. Update only the reading keyed by the active connection's address because a notification callback must not overwrite every device card.
3. Preserve a device's last good reading across later scans that contain no sample because a scan result is not a telemetry update.
4. Include a per-device sample timestamp and show stale/never-received states because the UI must distinguish an old value from a live one.
5. Render only filtered sensor candidates and escape names/addresses as text because advertising data is untrusted input.

## 5. Verify without hardware writes

1. Unit-test verified decoder inputs, malformed frame rejection, sentinel values, and address-to-reading association using captured or synthetic passive frames; do not invoke any BLE write method.
2. Test the monitor scheduler with mocked scanner/client objects: empty discovery, one device, multiple devices, failed connection cooldown, cancellation, and repeated start requests.
3. Check that the API reports one monitor task after repeated POST /api/monitor/start requests and that a synthetic notification changes only the matching device's sample.
4. Check the UI with a mocked `/api/monitor` response containing one valid sensor and one unrelated device; assert only the sensor card renders and values/time match the device address. Verify computed REST and WebSocket URLs for the actual mount path, including `/nicelogger/api` and `/nicelogger/ws`.
5. Deploy only after syntax/unit/UI checks pass; verify one backend scan cycle updates `last_scan_at`, the `/api/monitor` device list retains candidates across windows, the served HTML has the expected marker, and the WebSocket upgrade reaches `/ws`. Report separately whether a real device notification and decoded temperature were received.

## 6. User-facing boundary

Show an explicit waiting-for-data or last-seen state until a real notification has been decoded. Do not promise simultaneous live values when the monitor serializes GATT links; state that each device is sampled during its own listen window.