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


def create_fixture_database(path):
    config = {
        "outbounds": [
            {"tag": "direct", "protocol": "freedom", "settings": {}},
            {
                "tag": "TH-1",
                "protocol": "socks",
                "settings": {
                    "servers": [{"address": "127.0.0.1", "port": 1080}]
                },
            },
        ],
        "routing": {
            "rules": [{"type": "field", "balancerTag": "ADMOB-BALANCER"}],
            "balancers": [
                {"tag": "ADMOB-BALANCER", "selector": ["TH-1"]}
            ],
        },
    }
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute(
        "INSERT INTO settings(key, value) VALUES (?, ?)",
        ("xrayTemplateConfig", json.dumps(config)),
    )
    connection.commit()
    connection.close()


def main():
    outbound = {
        "tag": "TH-1",
        "protocol": "socks",
        "settings": {
            "servers": [
                {
                    "address": "10.0.0.5",
                    "port": 1081,
                    "users": [{"user": "test", "pass": "secret"}],
                }
            ]
        },
    }
    FeedHandler.payload = base64.b64encode(json.dumps([outbound]).encode())

    with tempfile.TemporaryDirectory() as temp_dir:
        database = Path(temp_dir) / "x-ui.db"
        create_fixture_database(database)
        before = database.read_bytes()

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
                    "DRY_RUN": "true",
                    "MIN_CHANGES": "1",
                }
            )
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                env=env,
                text=True,
                capture_output=True,
                timeout=20,
                check=False,
            )
            server.shutdown()

        assert result.returncode == 0, result.stderr
        assert "DRY RUN" in result.stdout
        assert "Changed TH proxies: 1" in result.stdout
        assert database.read_bytes() == before, "dry-run modified the database"

    print("smoke test passed")


if __name__ == "__main__":
    main()

