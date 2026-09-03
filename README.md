# ProxyFleet XUI Sync

`proxyfleet-xui-sync` synchronizes ProxyFleet's managed `TH-<number>`
outbounds with x-ui while avoiding unnecessary x-ui/Xray restarts.

It preserves all non-TH outbounds and routing rules. The configured balancer
(default: `ADMOB-BALANCER`) is created when missing; afterward, only its
selector is synchronized.

## Runtime behavior

| Detected change | Action |
| --- | --- |
| Same tags, only proxy IP/port/credentials changed | Hot-swap changed outbounds through Xray `HandlerService`; no x-ui restart |
| Feed order changed but tag set is identical | No change; tags are normalized in numeric order |
| Tag added/removed, selector changed, or balancer missing | Wait for repeated identical observations, then perform one controlled restart |
| Xray runtime API unavailable during an endpoint-only update | Fall back to the same stability and cooldown guards before restarting |
| Hot update starts but fails | Perform one controlled restart so the database and running Xray stay consistent |

The timer checks every **10 minutes**, with up to 30 seconds of random delay.
With the default `RESTART_STABLE_RUNS=3`, a restart-requiring topology change
must therefore remain identical for roughly 30 minutes. Ordinary endpoint
changes behind fixed tags are applied on the first check without restarting.

Hot updates use Xray's official `api rmo` and `api ado` commands. See the
[Xray command documentation](https://xtls.github.io/document/command.html).

## Safety guarantees

- Validates every downloaded tag, protocol, address and port before writing.
- Requires ProxyFleet's Ready response by default.
- Refuses an empty feed so a bad response cannot remove every managed outbound.
- Sorts `TH-<number>` tags numerically, so feed ordering cannot churn selectors.
- Uses an SQLite snapshot backup that remains consistent in WAL mode.
- Applies database changes in an immediate transaction.
- Restores the backup if a controlled restart fails.
- Uses a process lock so timer and manual runs cannot overlap.
- Rate-limits restarts with stable-observation and cooldown guards.
- Stores restart state atomically with mode `0600`.
- Supports a non-destructive dry-run.

## Fresh installation

Requirements: Linux with systemd, Python 3, Git, x-ui, and its SQLite database.

```bash
git clone https://github.com/exirhub/proxyfleet-xui-sync.git
cd proxyfleet-xui-sync
chmod +x install.sh
sudo ./install.sh
```

Then configure the feed token if the endpoint requires one:

```bash
sudoedit /etc/proxyfleet-xui-sync.env
sudo systemctl start proxyfleet-xui-sync.service
```

The installer creates the environment file only on the first installation.
On later installations it preserves every existing value and appends only
new configuration keys that are missing.

## Update an existing installation

From the existing repository clone:

```bash
cd /path/to/proxyfleet-xui-sync
git pull --ff-only origin main
sudo ./install.sh
```

If the old installation directory is unknown, install a fresh copy and run
the same installer; the current `/etc/proxyfleet-xui-sync.env` is preserved:

```bash
git clone --depth 1 https://github.com/exirhub/proxyfleet-xui-sync.git proxyfleet-xui-sync-update
cd proxyfleet-xui-sync-update
sudo ./install.sh
```

After installing or updating, verify the timer and run one manual sync:

```bash
systemctl status proxyfleet-xui-sync.timer --no-pager
systemctl list-timers proxyfleet-xui-sync.timer
sudo systemctl start proxyfleet-xui-sync.service
journalctl -u proxyfleet-xui-sync.service -n 100 --no-pager -o cat
```

For fixed tags whose proxy endpoint changed, the success log should contain:

```text
Runtime action: Xray API hot update
```

It should not contain an x-ui restart for that run.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROXYFLEET_OUTBOUNDS_URL` | `http://85.237.211.23:8788/outbounds` | Base64 outbounds feed |
| `PROXYFLEET_OUTBOUNDS_TOKEN` | empty | `X-Outbounds-Token` header |
| `XUI_DB` | `/etc/x-ui/x-ui.db` | x-ui SQLite database |
| `XUI_SERVICE` | `x-ui` | systemd service used for controlled restart |
| `XUI_BALANCER_TAG` | `ADMOB-BALANCER` | Managed balancer |
| `MIN_CHANGES` | `1` | Minimum managed diff before applying |
| `REQUIRE_READY` | `true` | Require ProxyFleet Ready response |
| `DOWNLOAD_TIMEOUT` | `25` | Feed timeout in seconds |
| `HOT_RELOAD` | `true` | Enable runtime updates for fixed tags |
| `XRAY_BINARY` | `/usr/local/x-ui/bin/xray-linux-amd64` | Xray executable with API commands |
| `XRAY_RUNTIME_CONFIG` | `/usr/local/x-ui/bin/config.json` | Running config used to discover the API listener |
| `XRAY_API_SERVER` | empty | Explicit API address, such as `127.0.0.1:10085`; empty means auto-discover |
| `XRAY_API_TIMEOUT` | `5` | Runtime API timeout in seconds |
| `RESTART_STABLE_RUNS` | `3` | Identical observations required before a restart |
| `RESTART_COOLDOWN_SECONDS` | `1800` | Minimum time between controlled restarts |
| `STATE_FILE` | `/var/lib/proxyfleet-xui-sync/state.json` | Persistent stability/cooldown state |
| `XUI_RESTART_WAIT_SECONDS` | `4` | Post-restart health-check wait |
| `BACKUP_RETENTION` | `10` | Backups to retain |
| `DRY_RUN` | `false` | Calculate only; do not write, hot-update, or restart |

If x-ui exposes `HandlerService` on a nonstandard API address and automatic
discovery cannot find it, set `XRAY_API_SERVER` explicitly. If the API is not
available, the synchronizer does not restart immediately: it applies the
stability and cooldown gates first.

## Operations

```bash
systemctl status proxyfleet-xui-sync.timer --no-pager
systemctl list-timers proxyfleet-xui-sync.timer
journalctl -u proxyfleet-xui-sync.service -n 100 --no-pager -o cat
sudo systemctl start proxyfleet-xui-sync.service
```

For a dry-run, temporarily set `DRY_RUN=true` in the environment file, start
the service manually, then restore it to `false`.

Backups are stored next to the configured database as
`x-ui.db.backup-YYYYmmdd-HHMMSS`. The newest ten are retained by default.

## Test

The smoke tests use a local feed, temporary SQLite database, fake Xray API,
and fake systemctl executable. They never contact the production endpoint or
restart a real service.

```bash
python3 tests/smoke_test.py
```
