"""A private command-channel address and client this test's daemon can serve
on whichever transport the host actually has (task 7's Windows named pipe,
alongside the UNIX socket every other platform already had).

Shared by every test that stands up a real `MurmlyDaemon` and talks to it
in-process, rather than through the CLI's own `send_command` (which already
dispatches on the address's shape): `test_speech_session.py`'s harness needs a
long-lived, multi-frame connection `send_command` does not offer, so it needs
this module's `connect_command_channel` for the same dispatch.

Not named `test_*`, matching `fakes.py` and `module_stubs.py`: it carries no
tests of its own, and `unittest discover` must not try to run it as one.
"""

from __future__ import annotations

import os
import socket
import uuid
from pathlib import Path

from murmly.platform import OperatingSystem, resolve_platform
from murmly.win_pipe import connect_named_pipe_client, is_pipe_name


def command_channel_address(temp_dir: str) -> Path:
    """A private address a `MurmlyDaemon` on *this* host can actually bind.

    A path under `temp_dir` everywhere `MurmlyDaemon` serves a UNIX socket.
    On Windows, where it instead serves a named pipe (`_uses_named_pipe`), a
    pipe name unique to this process and this call: the pipe namespace is
    global, not confined to a directory the way a socket file is, so two
    tests running back to back -- or two runs of the suite on the same
    machine -- must never collide on the same name the way they safely can
    on the same `temp_dir`.

    Decided from `resolve_platform()`, the same call `MurmlyDaemon.__init__`
    itself defaults to when a test passes no `profile=` -- so this always
    agrees with the transport the daemon it addresses actually picks.
    """
    if resolve_platform().operating_system is OperatingSystem.WINDOWS:
        return Path(f"\\\\.\\pipe\\murmly-test-{os.getpid()}-{uuid.uuid4().hex}")
    return Path(temp_dir) / "murmly.sock"


def connect_command_channel(address: Path, timeout: float) -> object:
    """A connected client for `address`, over whichever transport its shape names.

    Exposes exactly the `socket.socket` subset both transports already agree
    on -- `settimeout`, `recv`, `sendall`, `close` -- see `win_pipe.
    NamedPipeConnection`'s own docstring for why that subset is what
    `daemon.py` was written to call on either one.
    """
    path = str(address)
    if is_pipe_name(path):
        return connect_named_pipe_client(path, timeout)
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.settimeout(timeout)
    client.connect(path)
    return client
