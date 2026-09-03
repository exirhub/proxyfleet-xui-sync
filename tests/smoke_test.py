#!/usr/bin/env python3

import base64
import http.server
import json
import os
from pathlib import Path
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "proxyfleet-xui-sync.py"


class FeedHandler(http.server.BaseHTTPRequestHandler):
    payload = b""

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.send_header("X-ProxyFleet-Working", "1")
        self.send_header("X-ProxyFleet-Ready", "true")
        self.end_headers()
        self.wfile.write(self.payload)

    def log_message(self, *_args):
        return


def outbound(tag, address, port):
    return {
        "tag": tag,
        "protocol": "socks",
        "settings": {
            "servers": [
                {
                    "address": address,
                    "port": port,
                    "users": [{"user": "test", "pass": "secret"}],
                }
            ]
        },
    }


def create_fixture_database(path, managed, selector=None):
    config = {
        "outbounds": [
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            *managed,
        ],
        "routing": {
            "rules": [{"type": "field", "balancerTag": "ADMOB-BALANCER"}],
        },
    }
    if selector is not None:
        config["routing"]["balancers"] = [
            {
                "tag": "ADMOB-BALANCER",
                "selector": selector,
                "strategy": {"type": "random"},
            }
        ]

    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)",
        ("xrayTemplateConfig", json.dumps(config)),
    )
    connection.commit()
    connection.close()


def read_config(path):
    connection = sqlite3.connect(path)
    try:
        value = connection.execute(
            "SELECT value FROM settings WHERE key = 'xrayTemplateConfig'"
        ).fetchone()[0]
        return json.loads(value)
    finally:
        connection.close()


def write_fake_command(path, log_variable):
    path.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, sys\n"
        f"with open(os.environ[{log_variable!r}], 'a', encoding='utf-8') as f:\n"
        "    f.write(json.dumps(sys.argv[1:]) + '\\n')\n",
        encoding="utf-8",
    )
    path.chmod(0o755)


def invoke(feed, database, temp_dir, **overrides):
    FeedHandler.payload = base64.b64encode(json.dumps(feed).encode())
    temp = Path(temp_dir)
    xray = temp / "fake-xray"
    systemctl = temp / "fake-systemctl"
    xray_log = temp / "xray.log"
    systemctl_log = temp / "systemctl.log"
    runtime = temp / "config.json"

    write_fake_command(xray, "FAKE_XRAY_LOG")
    write_fake_command(systemctl, "FAKE_SYSTEMCTL_LOG")
    runtime.write_text(
        json.dumps(
            {
                "api": {"tag": "api", "services": ["HandlerService"]},
                "inbounds": [{"tag": "api", "listen": "127.0.0.1", "port": 10085}],
            }
        ),
        encoding="utf-8",
    )

    with socketserver.TCPServer(("127.0.0.1", 0), FeedHandler) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        env = os.environ.copy()
        env.update(
            {
                "PROXYFLEET_OUTBOUNDS_URL": (
                    f"http://127.0.0.1:{server.server_address[1]}/outbounds"
                ),
                "XUI_DB": str(database),
                "STATE_FILE": str(temp / "state.json"),
                "XRAY_BINARY": str(xray),
                "XRAY_RUNTIME_CONFIG": str(runtime),
                "SYSTEMCTL_BINARY": str(systemctl),
                "FAKE_XRAY_LOG": str(xray_log),
                "FAKE_SYSTEMCTL_LOG": str(systemctl_log),
                "MIN_CHANGES": "1",
                "REQUIRE_READY": "true",
                "XUI_RESTART_WAIT_SECONDS": "0",
                "RESTART_COOLDOWN_SECONDS": "0",
            }
        )
        env.update({key: str(value) for key, value in overrides.items()})
        result = subprocess.run(
            [sys.executable, str(SCRIPT)],
            env=env,
            text=True,
            capture_output=True,
            timeout=20,
            check=False,
        )
        server.shutdown()

    return result, xray_log, systemctl_log


def logged_calls(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_dry_run():
    with tempfile.TemporaryDirectory() as temp_dir:
        database = Path(temp_dir) / "x-ui.db"
        create_fixture_database(
            database,
            [outbound("TH-1", "127.0.0.1", 1080)],
            selector=["TH-1"],
        )
        before = database.read_bytes()
        result, xray_log, systemctl_log = invoke(
            [outbound("TH-1", "10.0.0.5", 1081)],
            database,
            temp_dir,
            DRY_RUN="true",
        )
        assert result.returncode == 0, result.stderr
        assert "DRY RUN" in result.stdout
        assert database.read_bytes() == before
        assert not (Path(temp_dir) / "state.json").exists()
        assert logged_calls(xray_log) == []
        assert logged_calls(systemctl_log) == []


def test_fixed_tag_is_hot_swapped_without_restart():
    with tempfile.TemporaryDirectory() as temp_dir:
        database = Path(temp_dir) / "x-ui.db"
        create_fixture_database(
            database,
            [outbound("TH-1", "127.0.0.1", 1080)],
            selector=["TH-1"],
        )
        result, xray_log, systemctl_log = invoke(
            [outbound("TH-1", "10.0.0.5", 1081)],
            database,
            temp_dir,
        )
        assert result.returncode == 0, result.stderr
        assert "Runtime action: Xray API hot update" in result.stdout
        assert [call[0] for call in logged_calls(xray_log)] == ["api", "api"]
        assert logged_calls(xray_log)[0][1] == "rmo"
        assert logged_calls(xray_log)[1][1] == "ado"
        assert logged_calls(systemctl_log) == []
        config = read_config(database)
        managed = {item["tag"]: item for item in config["outbounds"]}
        server = managed["TH-1"]["settings"]["servers"][0]
        assert (server["address"], server["port"]) == ("10.0.0.5", 1081)


def test_feed_order_does_not_change_selector():
    with tempfile.TemporaryDirectory() as temp_dir:
        database = Path(temp_dir) / "x-ui.db"
        th_1 = outbound("TH-1", "10.0.0.1", 1080)
        th_2 = outbound("TH-2", "10.0.0.2", 1080)
        create_fixture_database(database, [th_1, th_2], selector=["TH-1", "TH-2"])
        state_file = Path(temp_dir) / "state.json"
        state_file.write_text(
            json.dumps(
                {
                    "restart_candidate": "stale-candidate",
                    "restart_candidate_seen": 2,
                    "last_restart_at": 123,
                }
            ),
            encoding="utf-8",
        )
        before = database.read_bytes()
        result, xray_log, systemctl_log = invoke(
            [th_2, th_1], database, temp_dir
        )
        assert result.returncode == 0, result.stderr
        assert "only 0 managed changes" in result.stdout
        assert database.read_bytes() == before
        assert logged_calls(xray_log) == []
        assert logged_calls(systemctl_log) == []
        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["restart_candidate"] == ""
        assert state["restart_candidate_seen"] == 0
        assert state["last_restart_at"] == 123


def test_topology_change_requires_stable_observations():
    with tempfile.TemporaryDirectory() as temp_dir:
        database = Path(temp_dir) / "x-ui.db"
        th_1 = outbound("TH-1", "10.0.0.1", 1080)
        th_2 = outbound("TH-2", "10.0.0.2", 1080)
        create_fixture_database(database, [th_1], selector=["TH-1"])
        before = database.read_bytes()

        first, _, first_systemctl_log = invoke(
            [th_1, th_2], database, temp_dir, RESTART_STABLE_RUNS="2"
        )
        assert first.returncode == 0, first.stderr
        assert "DEFERRED" in first.stdout
        assert database.read_bytes() == before
        assert logged_calls(first_systemctl_log) == []

        second, _, second_systemctl_log = invoke(
            [th_1, th_2], database, temp_dir, RESTART_STABLE_RUNS="2"
        )
        assert second.returncode == 0, second.stderr
        assert "Runtime action: controlled x-ui restart" in second.stdout
        calls = logged_calls(second_systemctl_log)
        assert calls[0] == ["restart", "x-ui"]
        assert calls[1] == ["is-active", "--quiet", "x-ui"]
        config = read_config(database)
        selector = config["routing"]["balancers"][0]["selector"]
        assert selector == ["TH-1", "TH-2"]


def main():
    tests = [
        test_dry_run,
        test_fixed_tag_is_hot_swapped_without_restart,
        test_feed_order_does_not_change_selector,
        test_topology_change_requires_stable_observations,
    ]
    for test in tests:
        test()
        print(f"passed: {test.__name__}")
    print("all smoke tests passed")


if __name__ == "__main__":
    main()
