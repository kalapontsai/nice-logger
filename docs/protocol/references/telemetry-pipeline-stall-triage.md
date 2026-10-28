# Diagnosing a stopped MQTT telemetry pipeline

Use this when the web UI shows telemetry that has stopped updating, the MQTT panel
reports connected but no new records arrive, or a dashboard value is frozen.

The pipeline has three independent tiers. A green indicator on one tier says
nothing about the others, so probe all three before naming a cause:

| Tier | Process | Prove it is alive with |
|---|---|---|
| Publisher | `nicelogger_gateway.py` (BLE to MQTT) | process exists AND `last_message_at` advances |
| Broker | `mqtt-mosquitto` (Windows Docker) | `mosquitto_sub -t $SYS/broker/clients/connected` |
| Subscriber | `nice_logger/mqtt_feed.py` (Pi FastAPI) | `/api/mqtt/status` shows `connected: true` |

A subscriber that stays connected while `last_message_at` freezes means the
**publisher** is dead, not the broker or the subscriber.

## Step 1 - Separate publisher death from broker death

Check whether the publisher process exists:

```bash
ssh pi4 'ps -eo pid,etime,cmd | grep -Ei "nicelogger|gateway|uvicorn" | grep -v grep'
```

Check whether the subscriber is connected and when it last heard anything:

```bash
curl -s http://100.102.221.71:8000/api/mqtt/status
```

Interpret the pair:

- `connected: true` plus `last_message_at` older than `REPORT_INTERVAL * 2`
  means the publisher is gone. Go to Step 2.
- `connected: false` means the broker or the network path is down. Go to Step 3.
- `connected: true` plus `last_message_at` recent means the pipeline is fine; look
  elsewhere for the freeze (browser cache, chart rendering, stale row).

Do not conclude that MQTT is broken from a stale timestamp alone, because a
subscriber connected to a broker with a dead publisher looks exactly the same
as a healthy subscriber with a slow device.

## Step 2 - Confirm the publisher has no supervisor

The publisher is often started by hand, which means it survives only until the
next reboot or shell exit:

```bash
ssh pi4 'systemctl list-units --type=service --all --no-pager | grep -Ei "nice|gateway|logger|mqtt"'
ssh pi4 'grep -a -i -n "gateway|nohup" ~/.bash_history | tail -20'
```

If the only trace of the publisher is a `python nicelogger_gateway.py` line in
the shell history and there is no systemd unit for it, the publisher is
unmanaged. Check the remaining supervision surfaces before declaring that:

```bash
ssh pi4 'crontab -l; ls /etc/cron.d/; systemctl --user list-units --no-pager'
ssh pi4 'loginctl show-user kali | grep -i linger'
```

If none of them start the publisher, install a systemd unit for it. Use the
gateway venv explicitly, because the publisher dependencies are not in the
FastAPI service venv:

```ini
[Unit]
Description=NiceLogger BLE to MQTT publisher
After=network-online.target bluetooth.target
Wants=network-online.target bluetooth.target

[Service]
Type=simple
User=kali
WorkingDirectory=/home/kali/nice-logger-pi
Environment="PATH=/home/kali/nice-logger-pi/nicelogger_env/bin:/usr/local/bin:/usr/bin:/bin"
Environment="PYTHONUNBUFFERED=1"
ExecStart=/home/kali/nice-logger-pi/nicelogger_env/bin/python nicelogger_gateway.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

`Restart=always` rather than `on-failure`, because the publisher is expected to
run continuously and must come back after a clean exit as well as a crash.

Verify the unit is live and producing, not merely started:

```bash
ssh pi4 'systemctl is-active nice-logger-gateway.service'
ssh pi4 'journalctl -u nice-logger-gateway.service --no-pager -n 30'
```

The journal must show the MQTT broker connect line and then per-device publish
lines. A unit that is `active` but silent is scanning and publishing nothing.

Also enable linger so a user-scoped publisher survives logout:

```bash
ssh pi4 'loginctl enable-linger kali'
```

## Step 3 - Prove the broker and the network path separately

From the Pi, confirm the broker answers on the MQTT port:

```bash
ssh pi4 'timeout 5 bash -c "cat < /dev/null > /dev/tcp/10.35.32.11/1883" && echo OPEN'
```

From the Windows host, check the broker client counter and log:

```bash
"/mnt/c/Program Files/Docker/docker.exe" logs --tail 20 mqtt-mosquitto
"/mnt/c/Program Files/Docker/docker.exe" exec mqtt-mosquitto \
  mosquitto_sub -h 127.0.0.1 -t '$SYS/broker/clients/connected' -C 1 -W 3
```

A client count of 2 with one subscriber and one worker means the publisher is
not registered at the broker, which confirms a publisher-side failure rather
than a broker failure.

Check container restarts, because a broker restart silently drops every
publisher:

```bash
"/mnt/c/Program Files/Docker/docker.exe" inspect \
  -f '{{.Name}} started={{.State.StartedAt}} restarts={{.RestartCount}}' mqtt-mosquitto
```

## Step 4 - Check the Pi clock before trusting any timestamp

A Pi with no RTC and blocked UDP/123 keeps the time it had at boot, which can
be days stale. Every timestamp in the pipeline then looks wrong, and TLS
clients on the box fail with certificate-not-yet-valid errors that have nothing
to do with certificates.

Compare the Pi clock against an authoritative clock:

```bash
ssh pi4 'date -u; timedatectl'
curl -sI http://10.35.32.11/ | grep -i '^date:'
```

If the Pi is days behind, confirm the NTP failure instead of assuming it:

```bash
ssh pi4 'journalctl -u systemd-timesyncd --no-pager -n 25'
ssh pi4 'timedatectl show -p NTPSynchronized -p TimeUSec'
```

`NTPSynchronized: no` with repeated timed-out-waiting-for-reply lines means
UDP/123 is blocked on that network.

Check whether NTS over TCP/4460 is reachable, because blocked UDP/123 does not
imply blocked NTS:

```bash
ssh pi4 'timeout 6 bash -c "exec 3<>/dev/tcp/time.cloudflare.com/4460" && echo "NTS OPEN"'
```

When NTS is reachable, install `chrony` and point it at NTS servers so the
clock self-corrects after every reboot. When nothing is reachable, set the
clock from the LAN host over HTTP once, and accept that a reboot reintroduces
the skew.

Check the clock whenever the symptom is that timestamps are wrong or that TLS
errors mention the current time, and check it before concluding that a frozen
chart is a data-delivery problem.

## Step 5 - Verify the whole path end to end

Confirm new rows land in MySQL with a current created_at:

```bash
"/mnt/c/Program Files/Docker/docker.exe" exec mysql \
  sh -c 'mysql -uroot -p"$MYSQL_ROOT_PASSWORD" -D mqtt_store -e "
  "SELECT id,topic,created_at FROM mqtt_messages ORDER BY id DESC LIMIT 5;"'
```

Compare created_at with the broker host NOW(). A row whose created_at matches
the broker clock but not the Pi clock proves the publisher is alive and the
clock is the remaining defect.

Confirm the UI reflects the new rows:

```bash
curl -s "http://100.102.221.71:8000/api/mqtt/records?limit=3"
```

A stale `last_message_at` with fresh MySQL rows means the subscriber tier is
the broken one; fresh rows with a stale UI means the web tier is the broken one.

## Note on the mqtt_messages schema

The table stores `topic`, `payload`, `qos`, `retained`, and `created_at`. There
is no `device_id` column and no `timestamp` column; the device fields live
inside `payload` as JSON. Query with `SELECT id, topic, created_at, payload
FROM mqtt_messages` and parse the JSON out of `payload`, rather than assuming
flattened columns exist.