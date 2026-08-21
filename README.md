# ProxyFleet XUI Sync

`proxyfleet-xui-sync` safely synchronizes the managed `TH-<number>`
outbounds exported by ProxyFleet into x-ui's `xrayTemplateConfig`.

It preserves every non-TH outbound and every routing rule. The only routing
value it changes is the selector of the configured balancer (by default
`ADMOB-BALANCER`).

## Safety guarantees

- Validates every downloaded tag, protocol, address and port before writing.
- Refuses an empty feed so a bad response cannot remove every TH outbound.
- Uses an SQLite snapshot backup that remains consistent in WAL mode.
- Applies the database update in an immediate transaction.
- Restores the backup automatically when x-ui fails to restart.
- Uses a process lock so timer runs and manual runs cannot overlap.
- Keeps a configurable number of timestamped backups.
- Supports a non-destructive dry-run.

## Install

Requirements: Linux with systemd, Python 3, x-ui and its SQLite database.

```bash
chmod +x install.sh
sudo ./install.sh
```

The installer creates `/etc/proxyfleet-xui-sync.env` only when it does not
already exist. Put the feed token there when the endpoint requires one:

```bash
sudoedit /etc/proxyfleet-xui-sync.env
sudo systemctl restart proxyfleet-xui-sync.service
```

The timer runs 90 seconds after boot and then roughly every two minutes. It
does not launch a second copy while the previous run is still active.

## Operations

```bash
systemctl status proxyfleet-xui-sync.timer
systemctl list-timers proxyfleet-xui-sync.timer
journalctl -u proxyfleet-xui-sync.service -n 100 --no-pager
systemctl start proxyfleet-xui-sync.service
```

For a dry-run, temporarily set `DRY_RUN=true` in the environment file and
start the service manually. Set it back to `false` afterward.

Backups are stored next to the configured database as
`x-ui.db.backup-YYYYmmdd-HHMMSS`. The newest ten are retained by default.

## Configuration

| Variable | Default | Purpose |
| --- | --- | --- |
| `PROXYFLEET_OUTBOUNDS_URL` | `http://85.237.211.23:8788/outbounds` | Base64 outbounds feed |
| `PROXYFLEET_OUTBOUNDS_TOKEN` | empty | `X-Outbounds-Token` header |
| `XUI_DB` | `/etc/x-ui/x-ui.db` | x-ui SQLite database |
| `XUI_SERVICE` | `x-ui` | systemd service to restart |
| `XUI_BALANCER_TAG` | `ADMOB-BALANCER` | managed balancer |
| `MIN_CHANGES` | `1` | minimum diff before applying |
| `REQUIRE_READY` | `false` | require ProxyFleet Ready header |
| `DOWNLOAD_TIMEOUT` | `25` | feed timeout in seconds |
| `XUI_RESTART_WAIT_SECONDS` | `4` | post-restart health wait |
| `BACKUP_RETENTION` | `10` | backups to retain |
| `DRY_RUN` | `false` | calculate only; do not write/restart |

## Test

The smoke test starts a local feed and uses a temporary SQLite database. It
never contacts the production endpoint or systemd.

```bash
python3 tests/smoke_test.py
```

