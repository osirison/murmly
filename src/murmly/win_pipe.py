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
#
# Exception discipline for this whole section: no `pywintypes.error` may ever
# escape this module. Every function that can raise one either translates it
# into a plain `OSError` (or a subclass every caller already knows --
# `FileNotFoundError`, `BrokenPipeError`, `ConnectionRefusedError`,
# `TimeoutError`) or swallows it because the failure it names does not matter
# to the caller. `daemon.py` is written against `socket.socket`'s own
# vocabulary and must never import `pywintypes` merely to catch what this
# module's calls can raise.
# --------------------------------------------------------------------------


#: Win32 error codes this module translates, as plain integers rather than
#: `winerror.ERROR_*` names. `winerror` is itself part of `pywin32` and so is
#: unavailable on a machine with no `pywin32` installed -- exactly the
#: machine every test in this suite runs on. A translation table built from
#: literals is importable, and testable against a faked Win32 layer, on any
#: platform; the real Windows runtime compares these same numbers against
#: `pywintypes.error.winerror`, which carries the identical value regardless
#: of which name looked it up. Every value below is `winerror.h`'s own and
#: has not changed since Windows NT.
ERROR_FILE_NOT_FOUND = 2  #: `CreateFile`/`WaitNamedPipe`: no pipe of this name exists.
ERROR_ACCESS_DENIED = 5  #: `CreateNamedPipe` with `first_instance=True`: the name is squatted.
ERROR_BROKEN_PIPE = 109  #: `ReadFile`: the peer disconnected -- a socket's zero-length `recv`.
ERROR_PIPE_BUSY = 231  #: `CreateFile`: every instance is taken; `WaitNamedPipe` is the retry.
ERROR_NO_DATA = 232  #: `WriteFile`: the peer is gone -- a socket's `EPIPE`/`BrokenPipeError`.
#: `ReadFile`/`WriteFile`: the instance has no client on it. The peer left, the
#: same event `ERROR_BROKEN_PIPE` reports, but Windows chooses between the two
#: by how far the disconnect had progressed when the call landed rather than by
#: anything the caller can distinguish -- so both mean end of stream to a reader
#: and a gone peer to a writer, and both are translated the same way.
ERROR_PIPE_NOT_CONNECTED = 233
ERROR_PIPE_CONNECTED = 535  #: `ConnectNamedPipe`: a client connected before the call arrived. Success.
ERROR_OPERATION_ABORTED = 995  #: `GetOverlappedResult` after `CancelIo`: genuinely cancelled.
ERROR_IO_INCOMPLETE = 996  #: `GetOverlappedResult(bWait=False)`: still pending. Not a failure.
ERROR_IO_PENDING = 997  #: An overlapped call was queued and has not completed yet.


class NamedPipeIOError(OSError):
    """An `OSError` carrying the Win32 error code that produced it.

    Plain `OSError.winerror` exists only on a genuine Windows CPython build
    -- it is compiled in conditionally, under `sys.platform == 'win32'`, in
    CPython's own `Objects/exceptions.c` -- so it does not exist to read on
    any machine this suite's tests run on. `win32_error_code` carries the
    identical number as an ordinary attribute on every platform instead,
    which is what lets `recv`'s and `sendall`'s translation of
    `ERROR_BROKEN_PIPE` and `ERROR_NO_DATA` (see their own docstrings) be
    exercised and asserted from Linux, against a faked Win32 layer, rather
    than trusted untested until a Windows machine runs it.
    """

    def __init__(self, win32_error_code: int, message: str) -> None:
        super().__init__(message)
        self.win32_error_code = win32_error_code


def _pipe_error_from(error: object) -> NamedPipeIOError:
    """Turn one raised `pywintypes.error` into what this module raises.

    `error.winerror` is the Win32 error code `pywin32` attaches to every
    `pywintypes.error` it raises -- its `.args` are `(winerror, funcname,
    strerror)`, mirrored as same-named attributes for exactly this kind of
    read. `error` is typed `object`, not `pywintypes.error`, so this
    function itself never has to import `pywintypes` -- a test can hand it
    anything exposing the same `.winerror` and `str()` shape.
    """
    return NamedPipeIOError(error.winerror, str(error))


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
        # `win32con` does not export this constant in every `pywin32`
        # release, unlike `FILE_FLAG_OVERLAPPED` above -- the literal is
        # `winbase.h`'s own value, unlikely to ever change now, and `getattr`
        # prefers whatever the installed `pywin32` does define whenever it
        # has it.
        open_mode |= getattr(win32con, "FILE_FLAG_FIRST_PIPE_INSTANCE", 0x00080000)
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


def _wait_for_signal(event: object, timeout_ms: int) -> int:
    """`WaitForSingleObject`, kept to this module's own no-`pywintypes.error`
    rule. The C API reports a real failure through the return value
    (`WAIT_FAILED`), but `pywin32`'s wrapper raises `pywintypes.error`
    instead -- translated here the same way every other call in this module
    is, rather than left to surprise the one caller (`_run_overlapped`) that
    only checks the return value against `WAIT_TIMEOUT`.
    """
    import pywintypes
    import win32event

    try:
        return win32event.WaitForSingleObject(event, timeout_ms)
    except pywintypes.error as error:
        raise _pipe_error_from(error) from error


def _cancel_overlapped(handle: object, overlapped: object) -> None:
    """`CancelIo`, not `CancelIoEx`: confirmed against `pywin32`'s own C
    source (`win32file.i`), which wraps `CancelIo` and has no `CancelIoEx`
    wrapper at all -- the ci3-Windows.log's 14 `AttributeError: module
    'win32file' has no attribute 'CancelIoEx'` failures.

    The two calls differ in what they can reach, not just in name:
    `CancelIoEx` can target one specific `OVERLAPPED` from any thread;
    `CancelIo` cancels *every* pending operation the *calling* thread has
    outstanding on `handle`, and cannot be told about a single operation --
    it takes only the handle, which is why `overlapped` is accepted here but
    never passed on. That coarser scope is still exactly right at every call
    site in this module: `NamedPipeConnection.recv`, `NamedPipeConnection.
    sendall`, and `NamedPipeServer.accept` each issue their one overlapped
    call and, on timeout, cancel it from inside the very same function call
    on the very same thread -- there is never a second overlapped operation
    outstanding on that handle, from that thread, for `CancelIo` to catch by
    mistake. The two directions of a `SpeechSessionConnection` do not share a
    handle to confuse this either: its duplicated write handle
    (`NamedPipeConnection.dup`) is a distinct HANDLE value, read by the
    session's reader thread through the original handle and written by its
    writer thread through the duplicate, so a cancel issued by one thread on
    its own handle can never reach an operation pending on the other.

    Tolerates whatever `pywintypes.error` `CancelIo` raises -- unlike
    `CancelIoEx`, it does not document an "already completed" failure code
    for the no-longer-pending case (there is no specific operation to have
    gone missing), so nothing here needs to distinguish that case from any
    other. The `_collect_overlapped_result` call that always follows this
    one is what reports the actual outcome regardless, whether this cancel
    request did anything, arrived too late to matter, or failed outright.
    """
    import pywintypes
    import win32file

    try:
        win32file.CancelIo(handle)
    except pywintypes.error:
        pass


def _collect_overlapped_result(handle: object, overlapped: object) -> int:
    """The one call site for `GetOverlappedResult`, always with `bWait=True`.

    `bWait=False` is what produced 81 of this change's first 101 Windows CI
    failures, all through `NamedPipeServer.accept` -- `pywintypes.error:
    (996, 'GetOverlappedResult', 'Overlapped I/O event is not in a signaled
    state.')`, `ERROR_IO_INCOMPLETE`, meaning "not finished yet", not a
    failure. `bWait=True` is safe at every one of this function's three call
    sites in `_run_overlapped`: two are reached only once
    `WaitForSingleObject` has already reported the event signalled or the
    wait timed out and `CancelIo` was issued, and the third (`hr == 0` from
    `ReadFile`/`WriteFile`) is a completion that already happened
    synchronously. `True` costs nothing in any of those cases and guards
    against a spurious wakeup in the first one -- the same "not yet complete"
    state the 996 failures above came from, just reached a different way.
    """
    import pywintypes
    import win32file

    try:
        return win32file.GetOverlappedResult(handle, overlapped, True)
    except pywintypes.error as error:
        raise _pipe_error_from(error) from error


def _run_overlapped(handle: object, start, timeout_seconds: float | None) -> int:
    """Run one overlapped I/O call, wait up to `timeout_seconds`, return the
    transfer count.

    `start(overlapped)` issues the operation (`ConnectNamedPipe`, `ReadFile`,
    or `WriteFile`). Both of `pywin32`'s two conventions for reporting an
    outcome are handled, because they differ by function: `ReadFile`/
    `WriteFile` *return* `hr` without raising, for every outcome including
    failure (their own documented convention, needing two Python return
    values -- `hr` and the byte count -- which is why they cannot simply
    raise). `ConnectNamedPipe` is a plain BOOL-returning API with only one
    value to report and no such need, but is *not* a bare "raise on any
    failure" wrapper either: `pywin32`'s own C source for it (`win32pipe.i`)
    reads the underlying call's result itself and raises only for a
    genuinely unexpected failure, explicitly special-casing exactly
    `ERROR_IO_PENDING` and `ERROR_PIPE_CONNECTED` as `PyLong_FromLong(rc)` --
    returned, not raised, the same as `ReadFile`/`WriteFile`'s own pending
    code. Both call shapes are handled below, in both the returned-`hr`
    branch and the raised-`pywintypes.error` branch, since nothing past this
    point needs to know which call reported an outcome or which convention
    it used to report it -- only what the outcome was:

    * `ERROR_IO_PENDING`, returned or raised. The normal case: no client has
      connected yet, or no data has arrived yet. Normalised to one `pending`
      flag below, waited on further down.
    * `ERROR_PIPE_CONNECTED`, returned or raised. `ConnectNamedPipe`-only: a
      client's `CreateFile` landed in the window between `CreateNamedPipe`
      and this call. MSDN's own documented race, and a *success*, not a
      failure -- but critically, per MSDN's own Remarks for this exact
      condition, "the event specified in the OVERLAPPED structure is not set
      to the signaled state": no overlapped operation was ever actually
      queued for `overlapped`, so nothing will ever signal it, and calling
      `GetOverlappedResult` on it can only ever report `ERROR_IO_INCOMPLETE`
      -- or, called with `bWait=True` as this function always does, block
      forever waiting for a signal that will never come. This is exactly the
      996 failure named in `_collect_overlapped_result`'s own docstring
      (`bWait=False`) and its blocking twin (`bWait=True`), and returning `0`
      immediately without calling that function is the fix for both: there
      is nothing further to wait for or collect, because the connection is
      already complete.
    * Anything else raised is a genuine synchronous failure -- translated and
      raised immediately, without calling `GetOverlappedResult`: as with
      `ERROR_PIPE_CONNECTED`, a call that failed outright never queued an
      overlapped operation, so there is no result belonging to `overlapped`
      to collect. `ReadFile`/`WriteFile` report their own failures through
      the returned-`hr` path instead (via `_collect_overlapped_result`
      raising once called on a synchronous `hr == 0`, or via the caller's own
      translation of a raised `pywintypes.error` for a truly synchronous
      failure some Windows versions do raise for), so this branch in
      practice is reached only through `ConnectNamedPipe`'s own convention.

    A returned `hr` that is neither `ERROR_IO_PENDING` nor
    `ERROR_PIPE_CONNECTED` -- `ReadFile`/`WriteFile` completing synchronously
    with `hr == 0` -- is the one case that both *did* queue real overlapped
    I/O and needs `GetOverlappedResult` regardless of not being asked to wait
    for it: MSDN documents that a synchronous data-transfer completion,
    unlike `ConnectNamedPipe`'s `ERROR_PIPE_CONNECTED`, does still signal the
    event, and the transfer count is only available by collecting it.

    A pending operation is waited on with `WaitForSingleObject` up to
    `timeout_seconds`; `FILE_FLAG_OVERLAPPED` and this wait are what let a
    named-pipe `accept`/`recv`/`sendall` honour the same 0.2s shutdown poll
    and command timeouts the UNIX transport gets from `socket.settimeout`. A
    timeout cancels the operation with `CancelIo`, but does not treat the
    cancel itself as the outcome: `CancelIo` only *requests* cancellation
    (MSDN) -- the operation may already have completed, or may complete
    anyway before the request is processed -- so `GetOverlappedResult` is
    still called, with `bWait=True`, and its result decides what actually
    happened. Only `ERROR_OPERATION_ABORTED` (confirming the operation was
    genuinely cancelled) becomes `TimeoutError`; anything else -- a
    connection or a read that snuck in and completed anyway -- is returned as
    real data rather than discarded, which is also why `overlapped` and its
    event are freed only in the `finally` below, never before this collect:
    MSDN requires exactly this wait before either is touched again, since the
    kernel may still write a completion into them until it returns.
    """
    import pywintypes
    import win32event
    import win32file

    overlapped = pywintypes.OVERLAPPED()
    overlapped.hEvent = win32event.CreateEvent(None, True, False, None)
    try:
        try:
            hr = start(overlapped)
        except pywintypes.error as error:
            if error.winerror == ERROR_IO_PENDING:
                pending = True
            elif error.winerror == ERROR_PIPE_CONNECTED:
                return 0
            else:
                raise _pipe_error_from(error) from error
        else:
            # `ConnectNamedPipe`'s own convention (see this function's own
            # docstring, and `pywin32`'s `win32pipe.i`): a *returned* `hr`
            # can be `ERROR_PIPE_CONNECTED` exactly as a *raised* one can be,
            # above -- the same "nothing to collect" outcome, reached by the
            # convention `ConnectNamedPipe` actually uses rather than the one
            # `ReadFile`/`WriteFile` do.
            if hr == ERROR_PIPE_CONNECTED:
                return 0
            pending = hr == ERROR_IO_PENDING

        if not pending:
            return _collect_overlapped_result(handle, overlapped)

        result = _wait_for_signal(overlapped.hEvent, _wait_ms(timeout_seconds))
        if result == win32event.WAIT_TIMEOUT:
            _cancel_overlapped(handle, overlapped)
            try:
                return _collect_overlapped_result(handle, overlapped)
            except NamedPipeIOError as error:
                if error.win32_error_code == ERROR_OPERATION_ABORTED:
                    raise TimeoutError(
                        f"No data arrived within {timeout_seconds:g} seconds."
                        if timeout_seconds is not None
                        else "The operation did not complete."
                    ) from error
                raise
        return _collect_overlapped_result(handle, overlapped)
    finally:
        win32file.CloseHandle(overlapped.hEvent)


class NamedPipeConnection:
    """One accepted client.

    Exposes exactly the subset of `socket.socket`'s interface `daemon.py`'s
    connection handling calls -- `settimeout`, `recv`, `sendall`, `shutdown`,
    `close`, `dup` -- and nothing else, in particular no `getsockopt`: a
    pipe's peer identity comes from its client process token
    (`read_peer_identity_from_pipe`), not a socket option, so it is read
    through a separate function rather than reused through this one's
    interface. Keeping to that subset is what lets `daemon.py`'s
    `_serve_connection`, `_read_request`, `_write_response`, `_refuse`, and
    `SpeechSessionConnection` run unmodified against either transport.
    """

    def __init__(self, handle: object) -> None:
        self._handle = handle
        self._timeout_seconds: float | None = None

    @property
    def handle(self) -> object:
        return self._handle

    def dup(self) -> NamedPipeConnection:
        """A second, independently closable handle onto this same connection.

        `SpeechSessionConnection` (`daemon.py`) is what needs this: one
        handle its reader thread keeps reading on with a short poll timeout,
        and a second, independent handle its writer thread sends on with a
        longer send timeout and closes on its own schedule -- the same
        reason `socket.socket.dup()` exists, and the ci3-Windows.log defect
        this fixes (45 `AttributeError: 'NamedPipeConnection' object has no
        attribute 'dup'`) is exactly that a duck-typed pipe connection did
        not have it.

        `DuplicateHandle` against the current process, for both the source
        and the target, is the Win32 mechanism: it hands back a *new* HANDLE
        value that refers to the same open pipe instance `self._handle`
        does, the way `socket.socket.dup()`'s new file descriptor refers to
        the same open file description as the original. Ownership follows
        from that: the two HANDLE values are now independent references to
        one shared kernel object, so closing either one (`CloseHandle`, in
        `close` below) only drops that reference and leaves the other, and
        the connection it names, open -- never closing the underlying pipe
        instance, and never closing the original from underneath a caller
        still reading or writing through it. `DUPLICATE_SAME_ACCESS` is
        passed so the duplicate carries the same read/write access
        `_open_pipe_client_handle`/`create_named_pipe_server` already
        granted the original, rather than this function having to name it
        again; `bInheritHandle=False` matches every other handle this module
        creates, none of which a child process is meant to inherit.
        """
        import pywintypes
        import win32api
        import win32con

        process = win32api.GetCurrentProcess()
        try:
            duplicated = win32api.DuplicateHandle(
                process,
                self._handle,
                process,
                0,
                False,
                win32con.DUPLICATE_SAME_ACCESS,
            )
        except pywintypes.error as error:
            raise OSError(str(error)) from error
        return NamedPipeConnection(duplicated)

    def settimeout(self, seconds: float | None) -> None:
        self._timeout_seconds = seconds

    def recv(self, size: int) -> bytes:
        import win32file

        buffer = win32file.AllocateReadBuffer(size)

        def start(overlapped: object) -> int:
            hr, _ = win32file.ReadFile(self._handle, buffer, overlapped)
            return hr

        try:
            transferred = _run_overlapped(self._handle, start, self._timeout_seconds)
        except NamedPipeIOError as error:
            if error.win32_error_code in (ERROR_BROKEN_PIPE, ERROR_PIPE_NOT_CONNECTED):
                # `ERROR_BROKEN_PIPE` on `ReadFile` is a named pipe's way of
                # reporting exactly what a UNIX socket reports as a
                # zero-length `recv`: the peer closed its end. Translated
                # here, at the one call site that knows this is a read, so
                # every caller written against `socket.socket` --
                # `daemon._read_request`'s `if not chunk: break`,
                # `send_command`'s own read loop -- sees the same
                # end-of-stream signal from either transport, with no
                # named-pipe-specific branch of its own.
                #
                # `ERROR_PIPE_NOT_CONNECTED` is the same event reported from a
                # slightly later point in the disconnect, and reaching a
                # reader it means the same thing. Leaving it out is what made
                # a session whose client stopped reading hang until the test
                # gave up rather than being disconnected.
                return b""
            raise
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

            try:
                written = _run_overlapped(self._handle, start, self._timeout_seconds)
            except NamedPipeIOError as error:
                if error.win32_error_code in (
                    ERROR_NO_DATA,
                    ERROR_BROKEN_PIPE,
                    ERROR_PIPE_NOT_CONNECTED,
                ):
                    # `ERROR_NO_DATA` ("the pipe is being closed") is
                    # `WriteFile`'s report of what a UNIX socket's `sendall`
                    # reports as `EPIPE`/`BrokenPipeError`: the peer is gone.
                    # `ERROR_BROKEN_PIPE` and `ERROR_PIPE_NOT_CONNECTED` are
                    # documented for the same condition from other points in
                    # the disconnect; all three raise the same
                    # exception type `socket.sendall` itself raises for it,
                    # so `daemon._write_response`'s `except OSError`
                    # (`BrokenPipeError` is one) needs no named-pipe-specific
                    # branch either.
                    raise BrokenPipeError(str(error)) from error
                raise
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
        """Wait for one client, then hand it back with a fresh instance already waiting.

        `ConnectNamedPipe` has three documented outcomes, all reached through
        `_run_overlapped`'s own handling except the third, which belongs here
        because it is specific to this call rather than to overlapped I/O in
        general:

        * The normal case -- no client has connected yet -- is
          `ERROR_IO_PENDING`, waited on the same as any other pending
          overlapped call.
        * A client connected between `CreateNamedPipe` and this call is
          `ERROR_PIPE_CONNECTED`, a success `_run_overlapped` returns from
          directly (see its own docstring for why `GetOverlappedResult` must
          never be called for it).
        * A client connected *and disconnected again* in that same window is
          `ERROR_NO_DATA` -- a third, separately documented race for this
          specific API, distinct from the ordinary write-side meaning
          `NamedPipeConnection.sendall` gives the same code. `handle` is now a
          dead instance no client will ever complete a connection on: closed
          and replaced exactly as a served connection's handle is below, and
          the wait restarts on the fresh instance rather than handing
          `_accept_loop` a connection with nothing on the other end of it.
          The replacement is created *before* the dead instance is closed,
          the same order the ordinary path below already keeps: between the
          two, closing first would leave this pipe name briefly held by no
          instance at all, and the access check that keeps another account
          from squatting the name runs against an *existing* instance's DACL
          -- nothing to check it against is the one gap that check has.
        """
        import pywintypes
        import win32file
        import win32pipe

        while True:
            handle = self._pending

            def start(overlapped: object, handle: object = handle) -> int:
                # The return value matters here and must be passed through,
                # not discarded: `pywin32`'s own C source for
                # `ConnectNamedPipe` (`win32pipe.i`) *returns*
                # `ERROR_IO_PENDING` and `ERROR_PIPE_CONNECTED` as a plain
                # int rather than raising for either -- `_run_overlapped`'s
                # own `hr == ERROR_PIPE_CONNECTED` branch (see its docstring)
                # is what turns that return into the same "nothing to
                # collect" outcome its `except pywintypes.error` clause gives
                # a `pywin32` build that raises for it instead. `handle` is
                # bound as a default argument for the same reason `sendall`'s
                # `chunk` is: it must name *this* iteration's instance even
                # though the enclosing `handle` is reassigned before the
                # loop's next iteration defines a new closure.
                return win32pipe.ConnectNamedPipe(handle, overlapped)

            try:
                _run_overlapped(handle, start, self._timeout_seconds)
            except NamedPipeIOError as error:
                if error.win32_error_code != ERROR_NO_DATA:
                    raise
                self._pending = create_named_pipe_server(self._pipe_name, first_instance=False)
                try:
                    win32file.CloseHandle(handle)
                except pywintypes.error:
                    pass
                continue
            break

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

    `WaitNamedPipe` itself can also fail with `ERROR_FILE_NOT_FOUND`: the
    pipe existed a moment ago (that is what the preceding `ERROR_PIPE_BUSY`
    means), but has since been torn down entirely -- the daemon exited, or
    crashed, in the gap between this call's first `CreateFile` and this wait
    (ci2-Windows.log: `pywintypes.error: (2, 'WaitNamedPipe', ...)` reached
    the generic `ConnectionRefusedError` branch below before this fix).
    "No daemon running" is the accurate report of that -- matching what the
    first `CreateFile` above would itself have raised had the daemon already
    been gone at that point -- not "reached the channel, no one is free to
    serve this connection", so it is mapped to `FileNotFoundError` here too,
    and at the retry `CreateFile` below for the same reason.
    """
    import pywintypes

    try:
        handle = _open_pipe_client_handle(pipe_name)
    except pywintypes.error as error:
        if error.winerror == ERROR_FILE_NOT_FOUND:
            raise FileNotFoundError(str(error)) from error
        if error.winerror != ERROR_PIPE_BUSY:
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
            if wait_error.winerror == ERROR_FILE_NOT_FOUND:
                raise FileNotFoundError(str(wait_error)) from wait_error
            raise ConnectionRefusedError(str(wait_error)) from wait_error
        try:
            handle = _open_pipe_client_handle(pipe_name)
        except pywintypes.error as retry_error:
            if retry_error.winerror == ERROR_FILE_NOT_FOUND:
                raise FileNotFoundError(str(retry_error)) from retry_error
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
