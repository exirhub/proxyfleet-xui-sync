#!/usr/bin/env python3

import urllib.request
import urllib.error
import base64
import json
import sqlite3
import shutil
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
    "false"
).strip().lower() in ("1", "true", "yes", "on")

DOWNLOAD_TIMEOUT = int(os.environ.get("DOWNLOAD_TIMEOUT", "25"))
XUI_RESTART_WAIT_SECONDS = int(
    os.environ.get("XUI_RESTART_WAIT_SECONDS", "4")
)

XUI_SERVICE = os.environ.get(
    "XUI_SERVICE",
    "x-ui"
).strip() or "x-ui"

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
        "User-Agent": "ProxyFleet-XUI-Sync/3.0",
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
            ["systemctl", "restart", XUI_SERVICE],
            check=True,
            timeout=30
        )

        time.sleep(XUI_RESTART_WAIT_SECONDS)

        status = subprocess.run(
            ["systemctl", "is-active", "--quiet", XUI_SERVICE]
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
            ["systemctl", "stop", XUI_SERVICE],
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
            ["systemctl", "start", XUI_SERVICE],
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
    print("")
    print(
        f"SKIPPED: only {difference_count} managed changes "
        f"(minimum required: {MIN_CHANGES})"
    )
    con.close()
    sys.exit(0)

if DRY_RUN:
    print("")
    print("DRY RUN: no database changes or service restart were performed.")
    con.close()
    sys.exit(0)


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
# RESTART + ROLLBACK SAFETY
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

restart_xui_or_rollback(backup)
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
print("x-ui restarted successfully")
