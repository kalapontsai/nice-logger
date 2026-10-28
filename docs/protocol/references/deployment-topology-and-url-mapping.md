# Deployment topology and URL mapping

Use this when the user hands over two URLs and asks which service each one reaches,
or when a deployment claim in this skill disagrees with what a URL actually serves.

## Step 1 - Prove the two URLs serve one backend

Compare the response bodies byte for byte, not by eye:

```bash
curl -sS -m 10 http://<url-a>/ -o /tmp/a.html
curl -sS -m 10 http://<url-b>/ -o /tmp/b.html
md5sum /tmp/a.html /tmp/b.html
wc -c /tmp/a.html /tmp/b.html
```

Identical md5 and byte count mean the same origin renders the page, so any difference
between the two URLs is only the entry path and the proxy hop. A proxy that strips
the prefix still returns the upstream body unchanged.

When the bodies are not identical, diff them before concluding the URLs are
different services; a health-check counter or a timestamp in the page changes the
hash on every request.

## Step 2 - Read the real proxy target from the live config

Do not trust a remembered topology. Read `ProxyPass` out of the config the running
container actually loaded:

```bash
D=$(echo /mnt/c/Program*/Docker*/Docker/resources/bin/docker.exe)
"$D" exec apache2 sh -c 'grep -n "ProxyPass" /etc/apache2/conf-enabled/*.conf'
```

The file under `conf-enabled/` is a symlink into `conf-available/`, so this shows
the config the server booted with. Reading only the source file under
`/mnt/d/docker/apache/` shows intent, not the loaded state.

An Apache container name that `docker ps` does not list means the Windows-side
daemon is answering while the WSL-native daemon is not; use `docker.exe` for the
Windows tree (see `docker-compose-service-scaffold`).

## Step 3 - Resolve every host to one machine

A host can answer on several addresses. `tailscale status` shows the node name and
the direct endpoint, which is how a Tailscale IP and a LAN IP are tied to the same
device:

```bash
tailscale status
# 100.102.221.71  nice-logger  linux  active; direct 10.35.102.143:41641
```

Then confirm the third address serves the same body:

```bash
curl -sS -m 8 http://<lan-ip>:8000/ -o /tmp/c.html && md5sum /tmp/c.html
```

Three URLs with one md5 is the proof that a Tailscale IP, a LAN IP, and a
proxied sub-path are one service.

## Step 4 - Compare live API state, not just the page

Two pages can be byte-identical while a backend is stale. Compare a live state
endpoint on each entry point:

```bash
for base in http://<direct-base> http://<proxied-base>; do
  echo "## $base"
  for p in /api/health /api/live /api/mqtt/status /api/config; do
    echo "-- $p"; curl -sS -m 6 "$base$p" | head -c 500; echo
  done
done
```

Fields that must match for a shared backend: broker host and port, subscription
topic, connected flag, device list, and `last_message_at`. Equal values prove the
proxy reaches the same process, not a replica.

## Step 5 - Check for a second backend that is not serving

A host can hold a backend tree that nothing routes to. Test the port directly:

```bash
timeout 3 bash -c "echo > /dev/tcp/<host>/8000" && echo OPEN || echo CLOSED
```

A closed port on the host that owns the Apache container proves the proxy cannot be
pointing there, which settles a contradiction between a remembered architecture and
the live config.

## Step 6 - Check what the proxy serves instead of a static file

A sub-path that returns the upstream's HTML means the page is proxied; a sub-path
that returns a file on disk means a static copy is shadowing the backend. Compare
the served size against both candidates:

```bash
curl -sS -m 6 -o /dev/null -w 'code=%{http_code} size=%{size_download}\n' http://<host>/<subpath>/
ls -l /mnt/d/docker/apache/html/<subpath>/
```

When the served size differs from the on-disk file, the backend is serving the page
and the on-disk copy is a leftover.

## Step 7 - Verify the WebSocket hop separately

A REST 200 does not prove the WebSocket route works, and a reverse proxy that
lists a general path rule before the WebSocket rule silently breaks the upgrade:

```bash
curl -sS -m 6 -o /dev/null -w 'code=%{http_code}\n' \
  -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" -H "Sec-WebSocket-Version: 13" \
  http://<host>/<subpath>/ws
```

A `101` is the pass condition. A timeout after the headers is a failed
configuration, not a slow server.

## Step 8 - Check the upstream clock before trusting upstream timestamps

A host without an RTC that never syncs serves stale `Date:` headers, so every
timestamp it produces is wrong while the data still flows. Compare the upstream
`Date:` header with a host whose clock is known good:

```bash
curl -sS -m 6 -D- -o /dev/null http://<direct-base>/ | grep -i '^date:'
curl -sS -m 6 -D- -o /dev/null http://<proxy-host>/ | grep -i '^date:'
```

A gap of days means the upstream clock is stale. Confirm the data is still moving by
watching a monotonic field such as `last_message_at` advance between two calls; a
frozen value is a stall, a stale clock is not.

## Reporting shape

Give the mapping as a table of entry point, terminating host, service, and hop
mechanism, followed by the chain:

```
Browser -> Apache (:80) -> reverse proxy -> Pi FastAPI (:8000) -> BlueZ BLE -> 88361
```

List the evidence as the checks that were actually run (matching md5, the
`ProxyPass` line, the port test), because the mapping is only useful if the next
session can re-derive it.
