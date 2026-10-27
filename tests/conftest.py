"""Isolated smoke tests: no outbound connections and no production configuration."""

import ipaddress
import os
import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT.parent))
os.environ["ASTRBOT_ROOT"] = str(ROOT / ".dev/direct-smoke-runtime")
os.environ["ASTRBOT_TEST_MODE"] = "true"
os.environ["TESTING"] = "true"
sys.dont_write_bytecode = True


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex

    def local_only(method):
        def guarded(sock, address):
            # Windows creates asyncio's internal wakeup pipe via loopback TCP.
            try:
                if isinstance(address, tuple) and ipaddress.ip_address(address[0]).is_loopback:
                    return method(sock, address)
            except ValueError:
                pass
            raise AssertionError("Smoke tests must not open an outbound connection")

        return guarded

    monkeypatch.setattr(socket.socket, "connect", local_only(real_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", local_only(real_connect_ex))
