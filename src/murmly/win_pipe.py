"""Windows' command channel: a named pipe with an explicit owner-only DACL.

CPython exposes no `socket.AF_UNIX` on Windows. Windows has supported it at the
Winsock level since build 17063, but `python/cpython#77589` is still open, and
the current implementation attempt, `python/cpython#137420`, targets 3.16 -- so
it is unavailable for every Python version this project supports. This module
is the replacement `design.md`'s "The command channel" describes: a named pipe
created through `win32pipe.CreateNamedPipe` with a security descriptor built by
`win32security`, whose DACL names only the SID of the account that created it.

Deliberately not `multiprocessing.connection`. Its Windows pipes are created
with a NULL security descriptor -- the OS default DACL, which grants `Everyone`
read access -- and its actual protection is an application-layer HMAC challenge
keyed by `authkey`, not an OS access check. The command channel starts and
stops the microphone; a shared secret in a process's memory is a different
guarantee from one the kernel enforces, and the requirement (`command-interface`
spec, "The command socket is reachable only by the account that owns it") is
the kernel one.

Every `pywin32` name is imported inside the function that uses it, never at
module level, so this module stays importable on Linux -- `pywin32` is
markered `sys_platform == 'win32'` in `pyproject.toml` and is not installed
there at all. That is also why the module is organised in two halves:

* The first half -- `is_pipe_name`, `GENERIC_ALL`, `OwnerOnlyAce`,
  `owner_only_dacl_entries` -- is pure Python, describes the DACL's *shape* as
  plain data, and is exercised by the test suite on every platform.
* The second half turns that data into a real Win32 security descriptor, pipe,
  and connection, and is exercised only on Windows -- there is no way to create
  a named pipe, or read a DACL back off one, from Linux. `tests/test_daemon.py`
  and `tests/test_platform.py` say plainly, per test, which half they cover.
"""

from __future__ import annotations

from dataclasses import dataclass
import logging


logger = logging.getLogger(__name__)

#: The Windows named-pipe namespace prefix. Matched case-insensitively, because
#: the pipe namespace itself is: `\\.\PIPE\x` and `\\.\pipe\x` name the same
#: pipe. `config.WINDOWS_PIPE_NAME` is `\\.\pipe\murmly`, spelled lower case,
#: but a person's own configuration is not obliged to match that spelling.
PIPE_NAME_PREFIX = "\\\\.\\pipe\\"


def is_pipe_name(value: str) -> bool:
    """Whether `value` is shaped like a Windows named-pipe name.

    A pure string check, independent of which operating system is actually
    running right now. That independence is what task 7.5 needs: a configured
    channel that is a filesystem path on Windows, and one that is a pipe name
    on Linux, are both wrong, and recognising either shape is the same
    question asked of the same string, regardless of which platform is asking
    it. Testable on Linux with `is_pipe_name(str(Path(WINDOWS_PIPE_NAME)))` --
    `pathlib.PurePosixPath` treats backslashes as ordinary name characters
    rather than separators, so that round trip reproduces exactly what the
    daemon will actually call this with once `MurmlyConfig.socket_path` holds
    the configured value.
    """
    return value.casefold().startswith(PIPE_NAME_PREFIX.casefold())


#: The access mask Murmly's pipe DACL grants its own SID -- full control, not
#: read and write alone. A second concurrent client's `CreateFile`, and
#: Murmly's own next `CreateNamedPipe` call for the following client, are both
#: access checks against the *first* instance's security descriptor requiring
#: `FILE_CREATE_PIPE_INSTANCE` (0x00000004), which a read/write-only mask does
#: not carry -- a DACL granting only read and write wedges the server the
#: moment a second client tries to connect. Naming only one SID already grants
#: it everything that SID could get by any other route, so full control here
#: costs nothing a read/write mask would have saved.
GENERIC_ALL = 0x10000000


@dataclass(frozen=True, slots=True)
class OwnerOnlyAce:
    """One access-control entry: `access_mask` for exactly one SID.

    `sid` is opaque here -- whatever the platform's own SID representation is,
    down to a plain sentinel a test supplies -- so `owner_only_dacl_entries`
    stays a pure function callable from a machine that has never touched
    `win32security`.
    """

    sid: object
    access_mask: int = GENERIC_ALL


def owner_only_dacl_entries(sid: object) -> tuple[OwnerOnlyAce, ...]:
    """The ACL Murmly's pipe DACL must contain: one entry, naming `sid` alone.

    Kept separate from `_security_attributes_for_owner_only_pipe` below so the
    descriptor's *shape* -- exactly one entry, for exactly this SID, granting
    full control and naming no one else -- is asserted by a test running on any
    platform, independently of whether that platform can build the real
    security descriptor the shape describes.
    """
    return (OwnerOnlyAce(sid=sid),)


# --------------------------------------------------------------------------
# Windows-only from here down. Every name below imports `pywin32` from inside
# its own body; none of it can be exercised except on Windows, and none of it
# is imported until a caller resolved to the Windows platform reaches it.
# --------------------------------------------------------------------------


def _current_user_sid() -> object:
    """The SID of the account this process runs as, read from its own token.

    Never `win32security.LookupAccountName` from a username: a name is not
    guaranteed unique or stable the way the token's own SID is, and the SID is
    what the pipe's DACL and the peer-identity comparison both have to agree
    on naming.
    """
    import win32api
    import win32security

    token = win32security.OpenProcessToken(
        win32api.GetCurrentProcess(), win32security.TOKEN_QUERY
    )
    sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
    return sid


def current_user_sid_string() -> object:
    """The SID this process's own token carries, as the string form Windows uses.

    What a peer's identity (`read_peer_identity_from_pipe`) is compared
    against -- the role `os.getuid()` plays for the UNIX transport (see
    `daemon.local_account_identity`). Returned as a string, not the opaque SID
    object `_current_user_sid` hands back, because the peer's identity is read
    from a different process's token entirely and the two objects are not the
    same Python object even when they name the same account -- only their
    string forms are equal.
    """
    import win32security

    return win32security.ConvertSidToStringSid(_current_user_sid())


def _security_attributes_for_owner_only_pipe() -> object:
    """A `SECURITY_ATTRIBUTES` whose DACL grants only this process's own SID.

    Two ways this is wrong are both silent, which is exactly why this
    function -- not a literal at each `CreateNamedPipe` call -- is the one
    place it happens:

    * A DACL never set (a NULL security descriptor) is the OS default --
      `Everyone` read -- which is the precise defect `multiprocessing.
      connection` carries and this module exists to avoid.
    * An *empty but present* DACL denies everyone, including the server's own
      next `CreateNamedPipe` call for the following client.

    `SetSecurityDescriptorDacl(True, acl, False)` is what tells Windows the
    DACL is present and not defaulted, with `acl` holding exactly the one
    entry `owner_only_dacl_entries` describes.
    """
    import win32security

    (entry,) = owner_only_dacl_entries(_current_user_sid())
    acl = win32security.ACL()
    acl.AddAccessAllowedAce(win32security.ACL_REVISION, entry.access_mask, entry.sid)
    descriptor = win32security.SECURITY_DESCRIPTOR()
    descriptor.SetSecurityDescriptorDacl(True, acl, False)
    attributes = win32security.SECURITY_ATTRIBUTES()
    attributes.SECURITY_DESCRIPTOR = descriptor
    attributes.bInheritHandle = False
    return attributes


#: Matches `MAX_SPEECH_FRAME_BYTES` in `daemon.py`: large enough that an
#: ordinary command or streamed speech frame never spans two `ReadFile` calls
#: worth thinking about differently from a socket's own buffering.
PIPE_BUFFER_BYTES = 65_536


def create_named_pipe_server(pipe_name: str, *, first_instance: bool) -> object:
    """One instance of the named pipe, created with the owner-only DACL.

    `first_instance` sets `FILE_FLAG_FIRST_PIPE_INSTANCE`. The *first* call
    Murmly makes for a given pipe name must carry it, because that flag is
    what turns "another process already holds this name" into a refused
    `CreateNamedPipe` call (`ERROR_ACCESS_DENIED`) instead of a second server
    silently sharing a name with whatever created the existing instance. That
    refusal is task 7.5's "a configured channel name that cannot be created
    privately": `daemon._serve_named_pipe` reports it as a startup refusal
    naming the reason, never retries without the flag, and never falls through
    to joining the pre-existing instance. Every later instance -- created as
    each client is accepted and the server prepares to accept the next one --
    omits the flag and reuses the same DACL.

    Byte-mode, not message-mode: the command protocol is newline-delimited
    JSON read in arbitrary-sized chunks until a `\\n` is seen
    (`daemon._read_request`), the same assumption a `SOCK_STREAM` UNIX socket
    satisfies. Message-mode would deliver one message per `ReadFile` matching
    what a single `WriteFile` sent, which is a different contract from the one
    the rest of `daemon.py` is written against.
    """
    import pywintypes
    import win32con
    import win32pipe

    open_mode = win32pipe.PIPE_ACCESS_DUPLEX | win32con.FILE_FLAG_OVERLAPPED
    if first_instance:
        open_mode |= win32con.FILE_FLAG_FIRST_PIPE_INSTANCE
    pipe_mode = win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT
    try:
        return win32pipe.CreateNamedPipe(
            pipe_name,
            open_mode,
            pipe_mode,
            win32pipe.PIPE_UNLIMITED_INSTANCES,
            PIPE_BUFFER_BYTES,
            PIPE_BUFFER_BYTES,
            0,
            _security_attributes_for_owner_only_pipe(),
        )
    except pywintypes.error as error:
        # Reraised as a plain OSError so `daemon.py` never has to import
        # `pywintypes` itself to catch what this module's calls can raise --
        # the same reason every other translation in this module exists. This
        # is also where a squatted pipe name (`ERROR_ACCESS_DENIED` on the
        # `first_instance=True` call) surfaces to `daemon._serve_named_pipe`.
        raise OSError(str(error)) from error


def _wait_ms(timeout_seconds: float | None) -> int:
    import win32event

    if timeout_seconds is None:
        return win32event.INFINITE
    return max(0, int(timeout_seconds * 1000))


def _run_overlapped(handle: object, start, timeout_seconds: float | None) -> int:
    """Run one overlapped I/O call, wait up to `timeout_seconds`, return the
    transfer count.

    `start(overlapped)` issues the operation (`ConnectNamedPipe`, `ReadFile`,
    or `WriteFile`) and returns its own `hr` for the caller to inspect;
    `ERROR_IO_PENDING` is the only outcome this function waits on -- an
    operation that completes synchronously is not waited on again, since
    `GetOverlappedResult`'s wait flag is left `False` either way. A timeout
    cancels the pending operation with `CancelIoEx` before raising, so nothing
    is left running against a handle the caller may now close or reuse.

    `FILE_FLAG_OVERLAPPED` and this wait loop are what let a named-pipe
    `accept`/`recv`/`sendall` honour the same 0.2s shutdown poll and command
    timeouts the UNIX transport gets from `socket.settimeout`. None of it runs
    except on Windows, and none of it is exercised by this suite; see the
    module docstring.
    """
    import pywintypes
    import win32event
    import win32file
    import winerror

    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        try:
            hr = start(overlapped)
        except pywintypes.error as error:
            if error.winerror == winerror.ERROR_IO_PENDING:
                hr = winerror.ERROR_IO_PENDING
            elif error.winerror == winerror.ERROR_PIPE_CONNECTED:
                # A client connected between `CreateNamedPipe` and
                # `ConnectNamedPipe` -- already connected, nothing to wait for.
                hr = 0
            else:
                raise OSError(str(error)) from error
        if hr == winerror.ERROR_IO_PENDING:
            result = win32event.WaitForSingleObject(overlapped.hEvent, _wait_ms(timeout_seconds))
            if result == win32event.WAIT_TIMEOUT:
                win32file.CancelIoEx(handle, overlapped)
                raise TimeoutError(
                    f"No data arrived within {timeout_seconds:g} seconds."
                    if timeout_seconds is not None
                    else "The operation did not complete."
                )
        try:
            return win32file.GetOverlappedResult(handle, overlapped, False)
        except pywintypes.error as error:
            raise OSError(str(error)) from error
    finally:
        win32file.CloseHandle(overlapped.hEvent)


class NamedPipeConnection:
    """One accepted client.

    Exposes exactly the subset of `socket.socket`'s interface `daemon.py`'s
    connection handling calls -- `settimeout`, `recv`, `sendall`, `shutdown`,
    `close` -- and nothing else, in particular no `getsockopt`: a pipe's peer
    identity comes from its client process token
    (`read_peer_identity_from_pipe`), not a socket option, so it is read
    through a separate function rather than reused through this one's
    interface. Keeping to that subset is what lets `daemon.py`'s
    `_serve_connection`, `_read_request`, `_write_response`, and `_refuse` run
    unmodified against either transport.
    """

    def __init__(self, handle: object) -> None:
        self._handle = handle
        self._timeout_seconds: float | None = None

    @property
    def handle(self) -> object:
        return self._handle

    def settimeout(self, seconds: float | None) -> None:
        self._timeout_seconds = seconds

    def recv(self, size: int) -> bytes:
        import win32file

        buffer = win32file.AllocateReadBuffer(size)

        def start(overlapped: object) -> int:
            hr, _ = win32file.ReadFile(self._handle, buffer, overlapped)
            return hr

        transferred = _run_overlapped(self._handle, start, self._timeout_seconds)
        return bytes(buffer[:transferred])

    def sendall(self, data: bytes) -> None:
        import win32file

        remaining = bytes(data)
        while remaining:

            def start(overlapped: object, chunk: bytes = remaining) -> int:
                # `chunk` defaults to `remaining` at the point this closure is
                # defined, each time round the loop -- not read from the
                # enclosing scope when `start` is called -- so it names this
                # iteration's bytes even though `remaining` is reassigned
                # below before the loop's next iteration defines a new one.
                hr, _ = win32file.WriteFile(self._handle, chunk, overlapped)
                return hr

            written = _run_overlapped(self._handle, start, self._timeout_seconds)
            if written <= 0:
                raise OSError("The named pipe accepted no bytes.")
            remaining = remaining[written:]

    def shutdown(self, _how: int) -> None:
        import pywintypes
        import win32pipe

        try:
            win32pipe.DisconnectNamedPipe(self._handle)
        except pywintypes.error as error:
            raise OSError(str(error)) from error

    def close(self) -> None:
        import pywintypes
        import win32file

        try:
            win32file.CloseHandle(self._handle)
        except (OSError, pywintypes.error):
            # `pywintypes.error` does not subclass `OSError` -- every other
            # Win32 call in this module is wrapped precisely because of that,
            # and a bare `except OSError` here would let it through uncaught
            # into `daemon.shutdown()`'s own `except OSError: pass` around
            # this call, breaking the shutdown it was meant to no-op through.
            pass


class NamedPipeServer:
    """Windows' command channel: one waiting pipe instance at a time.

    `settimeout`/`accept`/`close` are the subset `daemon.py`'s accept loop
    calls, matching `socket.socket`'s own names so that loop is written once
    and shared by both transports (see `daemon.MurmlyDaemon._accept_loop`).

    One instance is kept open and waiting for a connection at all times, not
    created fresh inside `accept`: creating it lazily on each poll would mean
    a client's `CreateFile` could arrive in the gap between polls and be
    refused with nothing listening, which a UNIX socket's own backlog never
    does either. `first_instance=True` on construction is what makes a
    pipe-name already held by another process (task 7.5's "cannot be created
    privately") surface as this constructor raising, before anything else
    starts.
    """

    def __init__(self, pipe_name: str) -> None:
        self._pipe_name = pipe_name
        self._timeout_seconds: float | None = None
        self._pending = create_named_pipe_server(pipe_name, first_instance=True)

    @property
    def handle(self) -> object:
        """The pipe instance currently waiting for a connection.

        Exposed for `GetSecurityInfo(handle, ...)` DACL read-backs (task 18.6):
        reading the descriptor by *name* instead -- `GetNamedSecurityInfo`,
        which opens a fresh handle with its own `CreateFile` -- would itself be
        a client connecting to this waiting instance, consuming the very
        connection the test means only to inspect.
        """
        return self._pending

    def settimeout(self, seconds: float | None) -> None:
        self._timeout_seconds = seconds

    def accept(self) -> tuple[NamedPipeConnection, tuple[str, int]]:
        import win32pipe

        handle = self._pending

        def start(overlapped: object) -> int:
            # `ConnectNamedPipe` signals overlapped-pending, or an
            # already-connected client, by raising rather than by returning an
            # `hr` -- `_run_overlapped`'s own `except pywintypes.error` clause
            # is what turns either of those into the wait-or-proceed decision
            # every other `start` function here gets for free.
            win32pipe.ConnectNamedPipe(handle, overlapped)
            return 0

        _run_overlapped(handle, start, self._timeout_seconds)
        # A connection landed on `handle`. Prepared before returning it, so the
        # channel is never left with nothing waiting between one accepted
        # connection and the next -- the same reason the instance above is
        # created eagerly in `__init__` rather than from inside this method.
        self._pending = create_named_pipe_server(self._pipe_name, first_instance=False)
        return NamedPipeConnection(handle), (self._pipe_name, 0)

    def close(self) -> None:
        import pywintypes
        import win32file

        try:
            win32file.CloseHandle(self._pending)
        except (OSError, pywintypes.error):
            # See `NamedPipeConnection.close` -- `pywintypes.error` does not
            # subclass `OSError`, so a bare `except OSError` here would not
            # actually make this close a no-op the way the rest of this
            # module's callers assume it is.
            pass

    def __enter__(self) -> NamedPipeServer:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def _open_pipe_client_handle(pipe_name: str) -> object:
    import win32con
    import win32file

    return win32file.CreateFile(
        pipe_name,
        win32con.GENERIC_READ | win32con.GENERIC_WRITE,
        0,
        None,
        win32con.OPEN_EXISTING,
        win32con.FILE_FLAG_OVERLAPPED,
        None,
    )


def connect_named_pipe_client(pipe_name: str, timeout_seconds: float) -> NamedPipeConnection:
    """Open the client end of the pipe -- `daemon.send_command`'s Windows branch.

    `WaitNamedPipe` is what makes a pipe whose every instance is momentarily
    taken (`ERROR_PIPE_BUSY`) behave like connecting to a UNIX socket whose
    listen backlog is momentarily full, rather than failing outright the
    instant no instance happens to be free right now. Exceptions are mapped
    onto the same vocabulary `socket.connect` already raises for a UNIX
    socket, so `daemon.send_command`'s callers do not need a Windows-specific
    branch of their own: no pipe of this name at all is `FileNotFoundError`,
    exactly as connecting to a path nothing created is; every instance busy
    for the whole wait is `ConnectionRefusedError`, the same code a UNIX
    listener with no free backlog slot would not actually raise, but the
    closest existing meaning of "reached the channel, no one is free to serve
    this connection."
    """
    import pywintypes
    import winerror

    try:
        handle = _open_pipe_client_handle(pipe_name)
    except pywintypes.error as error:
        if error.winerror == winerror.ERROR_FILE_NOT_FOUND:
            raise FileNotFoundError(str(error)) from error
        if error.winerror != winerror.ERROR_PIPE_BUSY:
            raise OSError(str(error)) from error
        import win32pipe

        try:
            # Floored at 1, never 0: Windows reads a 0 millisecond count here
            # as `NMPWAIT_USE_DEFAULT_WAIT`, which waits the pipe's *default*
            # timeout rather than not waiting at all -- a caller that asked
            # for the shortest possible wait would get a longer one instead,
            # the opposite of what `connect_timeout` means.
            win32pipe.WaitNamedPipe(pipe_name, max(1, int(timeout_seconds * 1000)))
        except pywintypes.error as wait_error:
            raise ConnectionRefusedError(str(wait_error)) from wait_error
        try:
            handle = _open_pipe_client_handle(pipe_name)
        except pywintypes.error as retry_error:
            raise OSError(str(retry_error)) from retry_error
    connection = NamedPipeConnection(handle)
    connection.settimeout(timeout_seconds)
    return connection


def read_peer_identity_from_pipe(connection: NamedPipeConnection) -> object | None:
    """The SID, as a string, of the account behind this pipe connection.

    Read from the pipe's client *process* token (task 7.4) -- `win32pipe.
    GetNamedPipeClientProcessId` names the process, `OpenProcess` and its own
    token are what say which account it runs as -- rather than by
    impersonating the client with `ImpersonateNamedPipeClient`. The pipe's
    DACL, built by `create_named_pipe_server`, is the primary control: no
    other account can ever obtain a handle to connect with in the first place,
    the same posture `SO_PEERCRED` takes layered over an already-0600 UNIX
    socket. This check is a second, independent confirmation of the same fact
    the DACL already enforced, not a substitute for it.

    There is a PID-reuse race between `GetNamedPipeClientProcessId` and
    `OpenProcess`: the connecting process could exit and its PID be recycled
    by an unrelated process before this runs. That window exists for the same
    reason any PID-based check has one, and is accepted here for the same
    reason it is accepted elsewhere -- the DACL, not this call, is what
    actually keeps another account from connecting at all.
    """
    import pywintypes
    import win32api
    import win32con
    import win32pipe
    import win32security

    try:
        pid = win32pipe.GetNamedPipeClientProcessId(connection.handle)
    except pywintypes.error as error:
        logger.warning("Unable to read the client process id of a named-pipe connection: %s", error)
        return None
    try:
        process = win32api.OpenProcess(win32con.PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    except pywintypes.error as error:
        logger.warning("Unable to open the peer process of a named-pipe connection: %s", error)
        return None
    try:
        token = win32security.OpenProcessToken(process, win32security.TOKEN_QUERY)
        sid, _attributes = win32security.GetTokenInformation(token, win32security.TokenUser)
        return win32security.ConvertSidToStringSid(sid)
    except pywintypes.error as error:
        logger.warning("Unable to read the peer identity of a named-pipe connection: %s", error)
        return None
    finally:
        win32api.CloseHandle(process)
