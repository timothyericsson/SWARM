#!/usr/bin/python3
"""Deterministic raw-mode PTY recorder; no network or actual Codex interaction."""

import json
import os
from pathlib import Path
import signal
import sys
import time
import tty


directory = Path(sys.argv[1])
mode = sys.argv[2] if len(sys.argv) > 2 else "record"
if mode != "canonical":
    tty.setraw(0)
os.write(1, b"\x1b[?2004h")
if mode == "stubborn":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    signal.signal(signal.SIGHUP, signal.SIG_IGN)
    child = os.fork()
    if child == 0:
        os.setpgid(0, 0)
        while True:
            time.sleep(10)
    (directory / "descendant").write_text(str(child))
(directory / "ready").write_text(json.dumps({"pid": os.getpid(), "sid": os.getsid(0)}))
if mode == "exit":
    sys.exit(7)
if mode == "stubborn":
    while True:
        time.sleep(10)
with (directory / "input").open("ab", buffering=0) as stream:
    while True:
        data = os.read(0, 4096)
        if not data:
            break
        stream.write(data)
