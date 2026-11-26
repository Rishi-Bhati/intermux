"""JSON v1 line-delimited IPC test transport helpers."""

from __future__ import annotations

import json
import socket
import subprocess
from typing import Any, Dict, Optional


def call_stdio(binary_path: str, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    req = json.dumps({"v": 1, "action": action, "params": params or {}}) + "\n"
    proc = subprocess.run(
        [binary_path, "stdio"],
        input=req,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    return json.loads(proc.stdout.strip().splitlines()[-1])


class LoopbackAuthIPCClient:
    def __init__(self, host: str, port: int, token: str) -> None:
        self.sock = socket.create_connection((host, port), timeout=10)
        self.fh = self.sock.makefile("r", encoding="utf-8")
        self.sock.sendall((json.dumps({"v": 1, "auth": token}) + "\n").encode("utf-8"))
        auth_resp = json.loads(self.fh.readline())
        if not auth_resp.get("ok"):
            raise RuntimeError(f"IPC auth failed: {auth_resp}")

    def call(self, action: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        line = json.dumps({"v": 1, "action": action, "params": params or {}}) + "\n"
        self.sock.sendall(line.encode("utf-8"))
        return json.loads(self.fh.readline())

    def close(self) -> None:
        try:
            self.fh.close()
        finally:
            self.sock.close()
