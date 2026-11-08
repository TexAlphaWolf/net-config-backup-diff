"""A network device on loopback: a real SSH server (paramiko's server side) that answers like a router would.

Used by the tests and by tools/evaluate.py, so that the SSH code is exercised against a genuine SSH handshake,
genuine host keys and a genuine channel, not against a mock.
"""

from __future__ import annotations

import contextlib
import socket
import threading
import time
from collections.abc import Callable
from typing import Any

import paramiko

from cfgtrack.fetchers import fingerprint


class _Server(paramiko.ServerInterface):  # type: ignore[misc]
    def __init__(self, dev: FakeSshDevice) -> None:
        self.dev = dev
        self.exec_command: str | None = None
        self.shell = threading.Event()
        self.exec = threading.Event()

    def check_auth_password(self, username: str, password: str) -> int:
        self.dev.auth_attempts += 1
        ok = username == self.dev.username and password == self.dev.password
        return paramiko.AUTH_SUCCESSFUL if ok else paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str) -> str:
        return "password"

    def check_channel_request(self, kind: str, chanid: int) -> int:
        return paramiko.OPEN_SUCCEEDED if kind == "session" else paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_pty_request(self, *args: Any) -> bool:
        return True

    def check_channel_shell_request(self, channel: Any) -> bool:
        if not self.dev.allow_shell:
            return False
        self.shell.set()
        return True

    def check_channel_exec_request(self, channel: Any, command: bytes) -> bool:
        if not self.dev.allow_exec:
            return False
        self.exec_command = command.decode()
        self.exec.set()
        return True


class FakeSshDevice:
    def __init__(
        self,
        config: str | Callable[[], str],
        username: str = "backup",
        password: str = "s3cret-pw",
        allow_exec: bool = True,
        allow_shell: bool = True,
        prompt: str = "router#",
        chunk_delay: float = 0.0,
        command: str = "show running-config",
    ) -> None:
        self._config = config
        self.username, self.password = username, password
        self.allow_exec, self.allow_shell = allow_exec, allow_shell
        self.prompt = prompt
        self.chunk_delay = chunk_delay
        self.command = command
        self.exit_status = 0
        self.auth_attempts = 0
        self.key = paramiko.ECDSAKey.generate()
        self.fingerprint = fingerprint(self.key.asbytes())
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(16)
        self.port: int = self.sock.getsockname()[1]
        self.commands_seen: list[str] = []
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._accept = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept.start()

    def text(self) -> str:
        return self._config() if callable(self._config) else self._config

    def close(self) -> None:
        self._stop.set()
        with contextlib.suppress(OSError):  # wake the thread that waits in accept(): closing is not enough everywhere
            socket.create_connection(("127.0.0.1", self.port), timeout=1).close()
        self.sock.close()
        self._accept.join(timeout=5)
        for t in self._threads:
            t.join(timeout=5)

    def __enter__(self) -> FakeSshDevice:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            if self._stop.is_set():
                conn.close()
                return
            t = threading.Thread(target=self._serve, args=(conn,), daemon=True)
            self._threads.append(t)
            t.start()

    def _serve(self, conn: socket.socket) -> None:
        transport = paramiko.Transport(conn)
        transport.add_server_key(self.key)
        server = _Server(self)
        try:
            transport.start_server(server=server)
            chan = transport.accept(10)
            if chan is None:
                return
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline and transport.is_active():
                if server.exec.wait(0.05):
                    self.commands_seen.append(server.exec_command or "")
                    self._send(chan, self.text())
                    chan.send_exit_status(self.exit_status)
                    time.sleep(0.05)
                    return
                if server.shell.wait(0.05):
                    self._shell(chan)
                    return
        except (paramiko.SSHException, EOFError, OSError):
            return
        finally:
            transport.close()

    def _send(self, chan: Any, text: str) -> None:
        data = text.encode()
        step = 4096 if not self.chunk_delay else 700
        for i in range(0, len(data), step):
            chan.sendall(data[i : i + step])
            if self.chunk_delay:
                time.sleep(self.chunk_delay)

    def _shell(self, chan: Any) -> None:
        chan.sendall(f"\r\nWelcome to the lab router\r\n{self.prompt}".encode())
        buf = ""
        chan.settimeout(5)
        while True:
            try:
                ch = chan.recv(1).decode(errors="replace")
            except (TimeoutError, OSError):
                return
            if not ch:
                return
            if ch not in "\r\n":
                buf += ch
                continue
            cmd, buf = buf.strip(), ""
            if not cmd:
                continue
            self.commands_seen.append(cmd)
            chan.sendall(f"{cmd}\r\n".encode())  # the device echoes what it was sent
            if cmd == "exit":
                return
            if cmd == "terminal length 0":
                chan.sendall(self.prompt.encode())
            elif cmd == self.command:
                self._send(chan, self.text().replace("\n", "\r\n") + f"\r\n{self.prompt}")
            else:
                chan.sendall(f"% Invalid input detected at '^' marker.\r\n{self.prompt}".encode())
