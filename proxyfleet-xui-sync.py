#!/usr/bin/env python3

import urllib.request
import urllib.error
import base64
import hashlib
import json
import sqlite3
import shutil
import tempfile
import time
import re
import sys
import subprocess
import os
import stat

# ============================================================
# CONFIG
# ============================================================

URL = os.environ.get(
    "PROXYFLEET_OUTBOUNDS_URL",
    "http://85.237.211.23:8788/outbounds"
)

OUTBOUNDS_TOKEN = os.environ.get(
    "PROXYFLEET_OUTBOUNDS_TOKEN",
    ""
).strip()

DB = os.environ.get(
    "XUI_DB",
    "/etc/x-ui/x-ui.db"
)

BALANCER_TAG = os.environ.get(
    "XUI_BALANCER_TAG",
    "ADMOB-BALANCER"
)

MIN_CHANGES = int(os.environ.get("MIN_CHANGES", "1"))

# false = sync exactly the currently exported Working TH outbounds.
# true  = only sync when ProxyFleet reports Ready=1.
REQUIRE_READY = os.environ.get(
    "REQUIRE_READY",
    "true"
).strip().lower() in ("1", "true", "yes", "on")

DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "25"))
XUI_RESTART_WAIT_SECONDS = int(
    os.environ.get("XUI_RESTART_WAIT_SECONDS", "4")
)

XUI_SERVICE = os.environ.get(
    "XUI_SERVICE",
    "x-ui"
).strip() or "x-ui"

SYSTEMCTL_BINARY = os.environ.get(
    "SYSTEMCTL_BINARY",
    "systemctl"
).strip() or "systemctl"

HOT_RELOAD = os.environ.get(
    "HOT_RELOAD",
    "true"
).strip().lower() in ("1", "true", "yes", "on")

XRAY_BINARY = os.environ.get(
    "XRAY_BINARY",
    "/usr/local/x-ui/bin/xray-linux-amd64"
).strip()

XRAY_RUNTIME_CONFIG = os.environ.get(
    "XRAY_RUNTIME_CONFIG",
    "/usr/local/x-ui/bin/config.json"
).strip()

XRAY_API_SERVER = os.environ.get(
    "XRAY_API_SERVER",
    ""
).strip()

XRAY_API_TIMEOUT = max(
    1,
    int(os.environ.get("XRAY_API_TIMEOUT", "5"))
)

RESTART_STABLE_RUNS = max(
    1,
    int(os.environ.get("RESTART_STABLE_RUNS", "3"))
)

RESTART_COOLDOWN_SECONDS = max(
    0,
    int(os.environ.get("RESTART_COOLDOWN_SECONDS", "1800"))
)

STATE_FILE = os.environ.get(
    "STATE_FILE",
    "/var/lib/proxyfleet-xui-sync/state.json"
).strip()

BACKUP_RETENTION = max(
    1,
    int(os.environ.get("BACKUP_RETENTION", "10"))
)

DRY_RUN = os.environ.get(
    "DRY_RUN",
    "false"
).strip().lower() in ("1", "true", "yes", "on")

TH_TAG_RE = re.compile(r"^TH-(\d+)$")


# ============================================================
# HELPERS
# ============================================================

def is_th_tag(tag):
    return bool(TH_TAG_RE.fullmatch(str(tag or "")))


def th_sort_key(tag):
    match = TH_TAG_RE.fullmatch(str(tag or ""))
    if not match:
        return (1, str(tag))
    return (0, int(match.group(1)))


def load_state():
    default = {
        "restart_candidate": "",
        "restart_candidate_seen": 0,
        "last_restart_at": 0,
    }
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as handle:
            loaded = json.load(handle)
        if not isinstance(loaded, dict):
            return default
        for key in default:
            if key in loaded:
                default[key] = loaded[key]
    except FileNotFoundError:
        pass
    except Exception as exc:
        print(f"WARNING: could not read state file: {exc}")
    return default


def save_state(state):
    directory = os.path.dirname(STATE_FILE) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".state-",
        suffix=".json",
        dir=directory,
        text=True,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, separators=(",", ":"), sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, STATE_FILE)
    finally:
        try:
            os.remove(temporary)
        except FileNotFoundError:
            pass


def change_fingerprint(new_th, new_tags):
    payload = {
        "outbounds": [new_th[tag] for tag in new_tags],
        "selector": new_tags,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def discover_xray_api_server():
    if not HOT_RELOAD:
        raise Exception("HOT_RELOAD is disabled")
    if not XRAY_BINARY or not os.path.isfile(XRAY_BINARY):
        raise Exception(f"Xray binary not found: {XRAY_BINARY}")
    if not os.access(XRAY_BINARY, os.X_OK):
        raise Exception(f"Xray binary is not executable: {XRAY_BINARY}")
    if XRAY_API_SERVER:
        return XRAY_API_SERVER

    try:
        with open(XRAY_RUNTIME_CONFIG, "r", encoding="utf-8") as handle:
            runtime_config = json.load(handle)
    except Exception as exc:
        raise Exception(
            f"could not read Xray runtime config {XRAY_RUNTIME_CONFIG}: {exc}"
        ) from exc

    api = runtime_config.get("api")
    if not isinstance(api, dict):
        raise Exception("Xray runtime API is not configured")
    services = api.get("services", [])
    if "HandlerService" not in services:
        raise Exception("Xray HandlerService is not enabled")
    api_tag = str(api.get("tag", "api"))

    for inbound in runtime_config.get("inbounds", []):
        if str(inbound.get("tag", "")) != api_tag:
            continue
        port = int(inbound.get("port", 0))
        if port <= 0 or port > 65535:
            break
        listen = str(inbound.get("listen", "127.0.0.1")).strip()
        if listen in ("", "0.0.0.0"):
            listen = "127.0.0.1"
        elif listen in ("::", "[::]"):
            listen = "[::1]"
        elif ":" in listen and not listen.startswith("["):
            listen = f"[{listen}]"
        return f"{listen}:{port}"

    raise Exception(f"Xray API inbound with tag {api_tag!r} was not found")


def run_xray_api(*arguments):
    result = subprocess.run(
        [XRAY_BINARY, "api", *arguments],
        check=False,
        timeout=XRAY_API_TIMEOUT + 3,
        text=True,
        capture_output=True,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown error").strip()
        raise Exception(detail[-600:])


def add_runtime_outbound(api_server, outbound):
    descriptor, path = tempfile.mkstemp(
        prefix="proxyfleet-outbound-",
        suffix=".json",
        text=True,
    )
    try:
        document = {"outbounds": [outbound]}
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(document, handle, ensure_ascii=False, separators=(",", ":"))
            handle.write("\n")
        os.chmod(path, 0o600)
        run_xray_api(
            "ado",
            f"--server={api_server}",
            f"--timeout={XRAY_API_TIMEOUT}",
            path,
        )
    finally:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def hot_swap_outbounds(api_server, changed_tags, old_th, new_th):
    for tag in changed_tags:
        print(f"Hot-swapping outbound: {tag}")
        run_xray_api(
            "rmo",
            f"--server={api_server}",
            f"--timeout={XRAY_API_TIMEOUT}",
            tag,
        )
        try:
            add_runtime_outbound(api_server, new_th[tag])
        except Exception:
            # Best-effort runtime rollback. A controlled x-ui restart remains
            # the final consistency fallback if this restoration also fails.
            try:
                add_runtime_outbound(api_server, old_th[tag])
            except Exception as rollback_exc:
                print(f"WARNING: runtime rollback failed for {tag}: {rollback_exc}")
            raise


def proxy_identity(outbound):
    """
    Compare the actual proxy endpoint behind a managed TH tag.
    Supports SOCKS5 and plain HTTP. HTTPS proxy outbounds are not accepted.
    """
    try:
        protocol = str(outbound.get("protocol", "")).strip().lower()
        settings = outbound.get("settings", {})

        if protocol == "http":
            servers = settings.get("servers")
            if isinstance(servers, list) and servers:
                server = servers[0]
                address = str(server.get("address", ""))
                port = int(server.get("port", 0))
                users = server.get("users", [])
                user = ""
                password = ""
                if isinstance(users, list) and users:
                    user = str(users[0].get("user", ""))
                    password = str(users[0].get("pass", ""))
                return (protocol, address, port, user, password)

            # Read compatibility for the bad v1.4.6 direct-Core shape so the
            # first corrective sync can detect and replace those entries.
            address = str(settings.get("address", ""))
            port = int(settings.get("port", 0))
            user = str(settings.get("user", ""))
            password = str(settings.get("pass", ""))
            return (protocol, address, port, user, password)

        if protocol == "socks":
            # Current ProxyFleet legacy-compatible SOCKS5 export:
            # settings.servers[0].{address,port,users}
            servers = settings.get("servers")
            if isinstance(servers, list) and servers:
                server = servers[0]
                address = str(server.get("address", ""))
                port = int(server.get("port", 0))
                users = server.get("users", [])
                user = ""
                password = ""
                if isinstance(users, list) and users:
                    user = str(users[0].get("user", ""))
                    password = str(users[0].get("pass", ""))
                return (protocol, address, port, user, password)

            # Also understand the current native Xray SOCKS outbound shape.
            address = str(settings.get("address", ""))
            port = int(settings.get("port", 0))
            user = str(settings.get("user", ""))
            password = str(settings.get("pass", ""))
            return (protocol, address, port, user, password)

        return None
    except Exception:
        return None


def header_int(headers, name, default=0):
    value = headers.get(name)
    if value is None:
        return default
    try:
        return int(str(value).strip())
    except Exception:
        return default


def header_bool(headers, name, default=False):
    value = headers.get(name)
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def download_proxyfleet_outbounds():
    headers = {
        "Accept": "text/plain",
        "User-Agent": "ProxyFleet-XUI-Sync/4.0",
    }

    if OUTBOUNDS_TOKEN:
        headers["X-Outbounds-Token"] = OUTBOUNDS_TOKEN

    request = urllib.request.Request(
        URL,
        headers=headers,
        method="GET"
    )

    try:
        with urllib.request.urlopen(
            request,
            timeout=DOWNLOAD_TIMEOUT
        ) as response:
            raw = response.read().strip()

            meta = {
                "pool_size": header_int(
                    response.headers, "X-ProxyFleet-Pool-Size", 0
                ),
                "working": header_int(
                    response.headers, "X-ProxyFleet-Working", 0
                ),
                "tag_start": header_int(
                    response.headers, "X-ProxyFleet-Tag-Start", 0
                ),
                "ready": header_bool(
                    response.headers, "X-ProxyFleet-Ready", False
                ),
            }

            # Backward-compatible alternate header names
            if meta["pool_size"] == 0:
                meta["pool_size"] = header_int(
                    response.headers, "Pool-Size", 0
                )
            if meta["working"] == 0:
                meta["working"] = header_int(
                    response.headers, "Working", 0
                )
            if meta["tag_start"] == 0:
                meta["tag_start"] = header_int(
                    response.headers, "Tag-Start", 0
                )
            if not meta["ready"]:
                meta["ready"] = header_bool(
                    response.headers, "Ready", False
                )

    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise Exception(
            f"ProxyFleet HTTP {exc.code}: {body[:300]}"
        )
    except Exception as exc:
        raise Exception(
            f"Could not download ProxyFleet outbounds: {exc}"
        )

    if not raw:
        raise Exception("ProxyFleet returned an empty response")

    # Base64 padding safety
    raw += b"=" * ((4 - len(raw) % 4) % 4)

    try:
        decoded = base64.b64decode(raw, validate=False)
        outbounds = json.loads(decoded)
    except Exception as exc:
        raise Exception(
            f"Could not decode ProxyFleet outbounds: {exc}"
        )

    if not isinstance(outbounds, list):
        raise Exception("Downloaded data is not a JSON array")

    return outbounds, meta


def validate_new_outbounds(outbounds):
    tags = []
    seen = set()

    for outbound in outbounds:
        if not isinstance(outbound, dict):
            raise Exception("Invalid outbound item")

        tag = str(outbound.get("tag", ""))

        if not is_th_tag(tag):
            raise Exception(
                f"ProxyFleet returned invalid managed tag: {tag!r}"
            )

        if tag in seen:
            raise Exception(
                f"ProxyFleet returned duplicate tag: {tag}"
            )

        protocol = str(outbound.get("protocol", "")).strip().lower()
        settings = outbound.get("settings", {})

        if protocol == "socks":
            try:
                # Accept the existing ProxyFleet legacy-compatible SOCKS5
                # shape and the native Xray SOCKS outbound shape.
                servers = settings.get("servers")
                if isinstance(servers, list) and servers:
                    server = servers[0]
                    address = str(server.get("address", "")).strip()
                    port = int(server.get("port", 0))
                else:
                    address = str(settings.get("address", "")).strip()
                    port = int(settings.get("port", 0))
            except Exception:
                raise Exception(
                    f"{tag}: invalid SOCKS5 server structure"
                )

            if not address:
                raise Exception(f"{tag}: empty SOCKS5 address")

            if port <= 0 or port > 65535:
                raise Exception(f"{tag}: invalid SOCKS5 port")

        elif protocol == "http":
            try:
                servers = settings.get("servers")
                if isinstance(servers, list) and servers:
                    server = servers[0]
                    address = str(server.get("address", "")).strip()
                    port = int(server.get("port", 0))
                else:
                    # Backward read compatibility for the v1.4.6 direct-Core
                    # shape. New exports must use settings.servers[0].
                    address = str(settings.get("address", "")).strip()
                    port = int(settings.get("port", 0))
            except Exception:
                raise Exception(
                    f"{tag}: invalid HTTP proxy structure"
                )

            if not address:
                raise Exception(f"{tag}: empty HTTP address")

            if port <= 0 or port > 65535:
                raise Exception(f"{tag}: invalid HTTP port")

        else:
            # This rejects https, socks4 and every other outbound protocol.
            raise Exception(
                f"{tag}: unsupported protocol={protocol!r}; "
                "only SOCKS5 and plain HTTP are allowed"
            )

        tags.append(tag)
        seen.add(tag)

    return sorted(tags, key=th_sort_key)


def find_or_create_balancer(config):
    routing = config.get("routing")
    if not isinstance(routing, dict):
        raise Exception("xrayTemplateConfig.routing not found")

    balancers = routing.get("balancers")
    if balancers is None:
        balancers = []
        routing["balancers"] = balancers
    elif not isinstance(balancers, list):
        raise Exception("xrayTemplateConfig.routing.balancers is not an array")

    for balancer in balancers:
        if str(balancer.get("tag", "")) == BALANCER_TAG:
            return balancer, False

    balancer = {
        "tag": BALANCER_TAG,
        "selector": [],
        "strategy": {"type": "random"},
    }
    balancers.append(balancer)
    return balancer, True


def restart_xui_or_rollback(backup):
    print("")
    print("Restarting x-ui...")

    try:
        subprocess.run(
            [SYSTEMCTL_BINARY, "restart", XUI_SERVICE],
            check=True,
            timeout=30
        )

        time.sleep(XUI_RESTART_WAIT_SECONDS)

        status = subprocess.run(
            [SYSTEMCTL_BINARY, "is-active", "--quiet", XUI_SERVICE]
        )

        if status.returncode != 0:
            raise Exception(
                "x-ui is not active after restart"
            )

    except Exception as exc:
        print("")
        print("ERROR: x-ui restart failed:")
        print(exc)

        print("")
        print("Restoring previous database...")

        subprocess.run(
            [SYSTEMCTL_BINARY, "stop", XUI_SERVICE],
            check=False
        )

        time.sleep(2)

        # Full DB restore: stale WAL/SHM must not remain.
        for suffix in ["-wal", "-shm"]:
            try:
                os.remove(DB + suffix)
            except FileNotFoundError:
                pass

        original = os.stat(DB)
        shutil.copy2(backup, DB)
        os.chown(DB, original.st_uid, original.st_gid)
        os.chmod(DB, stat.S_IMODE(original.st_mode))

        subprocess.run(
            [SYSTEMCTL_BINARY, "start", XUI_SERVICE],
            check=False
        )

        print("Rollback completed.")
        sys.exit(1)


def create_consistent_backup(connection):
    """Create an SQLite snapshot that is safe even when WAL mode is active."""
    backup = f"{DB}.backup-{time.strftime('%Y%m%d-%H%M%S')}"
    destination = sqlite3.connect(backup)
    try:
        connection.backup(destination)
    finally:
        destination.close()

    source_stat = os.stat(DB)
    os.chown(backup, source_stat.st_uid, source_stat.st_gid)
    os.chmod(backup, stat.S_IMODE(source_stat.st_mode))
    return backup


def cleanup_old_backups():
    directory = os.path.dirname(DB) or "."
    prefix = os.path.basename(DB) + ".backup-"
    backups = []

    for name in os.listdir(directory):
        if not name.startswith(prefix):
            continue
        path = os.path.join(directory, name)
        if os.path.isfile(path):
            backups.append(path)

    backups.sort(key=os.path.getmtime, reverse=True)
    for path in backups[BACKUP_RETENTION:]:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


# ============================================================
# DOWNLOAD
# ============================================================

print("Downloading ProxyFleet outbounds...")
print("URL:", URL)

new_outbounds, meta = download_proxyfleet_outbounds()
new_tags = validate_new_outbounds(new_outbounds)

print("")
print("ProxyFleet exported TH outbounds:", len(new_outbounds))

if meta["pool_size"]:
    print("ProxyFleet configured pool:", meta["pool_size"])

if meta["working"]:
    print("ProxyFleet working:", meta["working"])

if meta["tag_start"]:
    print("ProxyFleet tag start:", meta["tag_start"])

print("ProxyFleet ready:", meta["ready"])

if REQUIRE_READY and not meta["ready"]:
    print("")
    print("SKIPPED: ProxyFleet is not Ready.")
    sys.exit(0)

if not new_outbounds:
    # Safety guard: never wipe every TH outbound because of a bad/empty feed.
    raise Exception(
        "ProxyFleet exported zero TH outbounds; refusing destructive sync"
    )


# ============================================================
# READ CURRENT X-UI CONFIG
# ============================================================

con = sqlite3.connect(DB, timeout=30)

row = con.execute(
    "SELECT value FROM settings WHERE key = ?",
    ("xrayTemplateConfig",)
).fetchone()

if not row:
    con.close()
    raise Exception("xrayTemplateConfig setting not found")

try:
    config = json.loads(row[0])
except Exception:
    con.close()
    raise

old_all_outbounds = config.get("outbounds", [])

if not isinstance(old_all_outbounds, list):
    con.close()
    raise Exception("xrayTemplateConfig.outbounds is not an array")


# ============================================================
# CURRENT/NEW TH MAPS
# ============================================================

old_th = {}

for outbound in old_all_outbounds:
    tag = str(outbound.get("tag", ""))
    if is_th_tag(tag):
        old_th[tag] = outbound

new_th = {
    str(outbound.get("tag")): outbound
    for outbound in new_outbounds
}

old_tags = set(old_th)
new_tag_set = set(new_th)

added_tags = sorted(
    new_tag_set - old_tags,
    key=th_sort_key
)

removed_tags = sorted(
    old_tags - new_tag_set,
    key=th_sort_key
)

changed_tags = []

for tag in sorted(
    old_tags & new_tag_set,
    key=th_sort_key
):
    if proxy_identity(old_th[tag]) != proxy_identity(new_th[tag]):
        changed_tags.append(tag)


# ============================================================
# BALANCER DIFF
# ============================================================

balancer, balancer_created = find_or_create_balancer(config)

old_selector = balancer.get("selector", [])

if not isinstance(old_selector, list):
    con.close()
    raise Exception(
        f"{BALANCER_TAG}.selector is not an array"
    )

# Exactly mirror the currently exported TH tags.
# No routing.rules are touched anywhere in this script.
new_selector = new_tags

selector_changed = old_selector != new_selector

difference_count = (
    len(added_tags)
    + len(removed_tags)
    + len(changed_tags)
    + (1 if selector_changed else 0)
)

print("")
print("Existing TH outbounds:", len(old_th))
print("New TH outbounds:", len(new_th))
print("Added TH tags:", len(added_tags))
print("Removed TH tags:", len(removed_tags))
print("Changed TH proxies:", len(changed_tags))
print(
    f"{BALANCER_TAG} balancer:",
    "will be created" if balancer_created else "already exists"
)
print(
    f"{BALANCER_TAG} selector:",
    f"{len(old_selector)} -> {len(new_selector)}"
)

if added_tags:
    print("Added:")
    print(", ".join(added_tags))

if removed_tags:
    print("Removed:")
    print(", ".join(removed_tags))

if changed_tags:
    print("Changed:")
    print(", ".join(changed_tags))

if selector_changed:
    print("Balancer selector will be synchronized.")

if difference_count < MIN_CHANGES:
    if not DRY_RUN:
        state = load_state()
        if state.get("restart_candidate") or state.get("restart_candidate_seen"):
            state["restart_candidate"] = ""
            state["restart_candidate_seen"] = 0
            save_state(state)
    print("")
    print(
        f"SKIPPED: only {difference_count} managed changes "
        f"(minimum required: {MIN_CHANGES})"
    )
    con.close()
    sys.exit(0)

topology_changed = bool(
    balancer_created
    or added_tags
    or removed_tags
    or selector_changed
)

api_server = None
hot_reload_unavailable = ""

if changed_tags and not topology_changed:
    try:
        api_server = discover_xray_api_server()
        print(f"Runtime action: hot update via Xray API at {api_server}")
    except Exception as exc:
        hot_reload_unavailable = str(exc)
        print(f"Runtime action: controlled restart ({hot_reload_unavailable})")

requires_restart = topology_changed or bool(hot_reload_unavailable)
state = load_state()
candidate_fingerprint = change_fingerprint(new_th, new_tags)

# A validated non-empty Ready feed is safe to apply immediately when x-ui has
# no managed TH outbounds yet. Waiting for repeated timer observations here
# leaves a fresh installation without its required outbounds and balancer.
initial_bootstrap = not old_tags and bool(new_tag_set)

if requires_restart:
    if initial_bootstrap:
        candidate_seen = RESTART_STABLE_RUNS
        state["restart_candidate"] = candidate_fingerprint
        state["restart_candidate_seen"] = candidate_seen
        print("Initial bootstrap: applying the first managed TH pool immediately.")
    else:
        if state.get("restart_candidate") == candidate_fingerprint:
            candidate_seen = int(state.get("restart_candidate_seen", 0)) + 1
        else:
            candidate_seen = 1

        state["restart_candidate"] = candidate_fingerprint
        state["restart_candidate_seen"] = candidate_seen

    print(
        "Restart stability:",
        f"{candidate_seen}/{RESTART_STABLE_RUNS} matching runs"
    )

    if DRY_RUN:
        print("")
        print("DRY RUN: no database, runtime or state changes were performed.")
        con.close()
        sys.exit(0)

    if not initial_bootstrap and candidate_seen < RESTART_STABLE_RUNS:
        save_state(state)
        print("")
        print(
            "DEFERRED: restart-requiring change is not stable yet; "
            "the active Xray configuration was left untouched."
        )
        con.close()
        sys.exit(0)

    last_restart_at = int(state.get("last_restart_at", 0) or 0)
    cooldown_remaining = 0 if initial_bootstrap else max(
        0,
        RESTART_COOLDOWN_SECONDS - (int(time.time()) - last_restart_at)
    )
    if cooldown_remaining:
        save_state(state)
        print("")
        print(
            "DEFERRED: restart cooldown is active for another "
            f"{cooldown_remaining} seconds."
        )
        con.close()
        sys.exit(0)

elif DRY_RUN:
    print("")
    print("DRY RUN: no database, runtime or state changes were performed.")
    con.close()
    sys.exit(0)
else:
    # Endpoint-only hot updates never need restart stability state.
    state["restart_candidate"] = ""
    state["restart_candidate_seen"] = 0


# ============================================================
# BACKUP
# ============================================================

backup = create_consistent_backup(con)

print("")
print("Backup created:")
print(backup)


# ============================================================
# BUILD NEW CONFIG
# ============================================================

# Preserve every non-TH outbound exactly as-is.
kept_outbounds = []

for outbound in old_all_outbounds:
    tag = str(outbound.get("tag", ""))
    if not is_th_tag(tag):
        kept_outbounds.append(outbound)

# Replace the entire managed TH section with ProxyFleet's current export.
config["outbounds"] = kept_outbounds + [
    new_th[tag] for tag in new_tags
]

# ONLY create/update ADMOB-BALANCER and its selector.
# routing.rules are intentionally untouched.
balancer["selector"] = new_selector

new_config = json.dumps(
    config,
    separators=(",", ":"),
    ensure_ascii=False
)


# ============================================================
# UPDATE DATABASE
# ============================================================

try:
    con.execute("BEGIN IMMEDIATE")

    result = con.execute(
        "UPDATE settings SET value = ? WHERE key = ?",
        (
            new_config,
            "xrayTemplateConfig"
        )
    )

    if result.rowcount != 1:
        raise Exception(
            "xrayTemplateConfig was not updated"
        )

    con.commit()

except Exception:
    con.rollback()
    con.close()
    raise

con.close()


# ============================================================
# APPLY TO RUNNING XRAY
# ============================================================

print("")
print("DATABASE UPDATE APPLIED")
print("Managed TH outbounds:", len(new_outbounds))
print(
    f"{BALANCER_TAG} selector entries:",
    len(new_selector)
)
print(
    "Routing rules:",
    "UNCHANGED"
)

runtime_action = "none"

if requires_restart:
    restart_xui_or_rollback(backup)
    state["last_restart_at"] = int(time.time())
    state["restart_candidate"] = ""
    state["restart_candidate_seen"] = 0
    runtime_action = "controlled x-ui restart"
else:
    try:
        hot_swap_outbounds(api_server, changed_tags, old_th, new_th)
        runtime_action = "Xray API hot update"
    except Exception as exc:
        # The database already contains the desired state. One restart is the
        # safest way to make runtime and persistence agree after a partial API
        # operation; future restart-requiring changes remain cooldown-limited.
        print("")
        print(f"WARNING: Xray API hot update failed: {exc}")
        print("Falling back to one controlled x-ui restart...")
        restart_xui_or_rollback(backup)
        state["last_restart_at"] = int(time.time())
        runtime_action = "controlled restart after hot-update failure"

save_state(state)
cleanup_old_backups()


# ============================================================
# SUCCESS
# ============================================================

print("")
print("========================================")
print("PROXYFLEET -> X-UI SYNC SUCCESS")
print("========================================")
print("Added TH:", len(added_tags))
print("Removed TH:", len(removed_tags))
print("Changed TH:", len(changed_tags))
print("Loaded TH outbounds:", len(new_outbounds))
print(
    f"{BALANCER_TAG} selector:",
    len(new_selector)
)
print("Routing rules: unchanged")
print("Runtime action:", runtime_action)
