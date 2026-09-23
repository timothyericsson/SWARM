#!/usr/bin/python3
"""Local app-server protocol fixture. It never connects to a network."""

import json
import os
from pathlib import Path
import signal
import sys
import time


mode = os.environ.get("SWARM_TEST_USAGE_MODE", "normal")
folder = Path(os.environ["SWARM_TEST_USAGE_DIRECTORY"])
(folder / "pid").write_text(str(os.getpid()))
(folder / "argv").write_text(json.dumps(sys.argv[1:]))


def receive():
    raw = sys.stdin.readline()
    if not raw:
        sys.exit(0)
    message = json.loads(raw)
    with (folder / "requests").open("a") as stream:
        stream.write(json.dumps(message) + "\n")
    return message


def send(message):
    print(json.dumps(message), flush=True)


def hang():
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    while True:
        time.sleep(0.1)


initialize = receive()
assert initialize["method"] == "initialize"
assert initialize["params"]["clientInfo"]["name"] == "swarm"
if mode == "initialize_timeout":
    hang()
if mode == "initialize_error":
    send({"id": 1, "error": {"code": -1, "message": "secret-token-for-test"}})
    hang()
if mode == "invalid_json":
    print("private invalid output", flush=True)
    hang()
if mode == "oversized_line":
    sys.stdout.write("x" * (300 * 1024))
    sys.stdout.flush()
    hang()
if mode == "output_flood":
    for index in range(1800):
        send({"method": "notification", "params": {"text": "x" * 1024}})
    hang()
if mode == "close":
    sys.exit(0)
send({"method": "account/updated", "params": {"authMode": "chatgpt"}})
send({"id": 999, "result": {"wrong": True}})
send({"id": True, "result": {"wrong": True}})
send({"id": 1, "result": {"userAgent": "fake"}})
assert receive()["method"] == "initialized"
request = receive()
assert request == {"id": 2, "method": "account/rateLimits/read"}
if mode == "usage_timeout":
    hang()
if mode == "usage_error":
    send({"id": 2, "error": {"message": "secret-token-for-test"}})
    hang()
if mode == "close_after_initialize":
    sys.exit(0)
send({"method": "account/rateLimits/updated", "params": {"rateLimits": {"primary": {"usedPercent": 99}}}})
send({"id": 2, "method": "server/request", "params": {}})
result = {
    "rateLimitsByLimitId": {
        "codex": {
            "limitId": "codex",
            "primary": {"usedPercent": 25.5, "windowDurationMins": 300, "resetsAt": 1900000000},
            "secondary": {"usedPercent": 70, "windowDurationMins": 10080, "resetsAt": 1900600000},
        }
    }
}
payload = json.dumps({"id": 2, "result": result}) + "\n"
if mode == "partial_lines":
    for position in range(0, len(payload), 7):
        sys.stdout.write(payload[position:position + 7])
        sys.stdout.flush()
        time.sleep(0.001)
else:
    sys.stdout.write(payload)
    sys.stdout.flush()
# Keep the helper alive to verify that fetch_usage closes stdin and reaps it.
receive()
