from __future__ import annotations

import http.server
import sys
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from cfgtrack.store import Repo

KEY = bytes(range(32))

IOS = """\
Building configuration...

Current configuration : 1432 bytes
!
! Last configuration change at 10:11:12 UTC Mon Nov 3 2026 by admin
!
version 15.2
hostname sw1
!
enable secret 9 $9$abcdefghijklmn
username admin privilege 15 secret 9 $9$zzzzzzzzzz
!
vlan 10
 name users
!
interface GigabitEthernet0/1
 description uplink
 switchport mode access
 switchport access vlan 10
!
interface GigabitEthernet0/2
 description printer
 shutdown
!
ip access-list extended MGMT
 permit tcp 10.0.0.0 0.0.0.255 any eq 22
 deny ip any any log
!
snmp-server community public123 RO
ntp clock-period 17179869
line vty 0 4
 password 7 0822455D0A16
 login
!
end
"""


@pytest.fixture
def key() -> bytes:
    return KEY


@pytest.fixture
def ios() -> str:
    return IOS


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    r = Repo(tmp_path / "store")
    r.init()
    return r


@pytest.fixture
def http_config() -> Iterator[tuple[str, dict[str, str]]]:
    """A web server that serves a configuration and remembers the Authorization header it was sent."""
    seen: dict[str, str] = {"body": "hostname web1\nend\n", "auth": ""}

    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            seen["auth"] = self.headers.get("Authorization", "")
            if self.path == "/missing":
                self.send_error(404)
                return
            body = (seen["body"] if self.path == "/config" else "x" * 5000).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            pass

    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}", seen
    srv.shutdown()
    srv.server_close()
    t.join(timeout=5)
