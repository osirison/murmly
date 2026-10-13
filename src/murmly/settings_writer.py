from __future__ import annotations

from dataclasses import dataclass
import hashlib
from pathlib import Path
import os
import stat
import sys
import tempfile
import time
import tomllib
from collections.abc import Callable
from typing import TypeVar

import tomlkit
from tomlkit.items import InlineTable, Table

from murmly.config import SettingValueError, default_config_path, validate_setting

#: How many times a write starts over because the file changed under it. A hand
#: edit landing inside the few milliseconds of one write is rare; three in a row
#: means something else is writing the file, and that is reported rather than
#: raced.
MAX_WRITE_ATTEMPTS = 3

#: Windows refuses to open or replace a file while another program has it open
#: (antivirus, the search indexer, a backup tool, an editor), and says so with
#: PermissionError, usually for a few milliseconds. These are the waits between
#: tries, about one second in all, before it is reported. Other platforms do not
#: retry: there a PermissionError is a permission.
TRANSIENT_DELAYS_S = (0.02, 0.05, 0.1, 0.2, 0.3, 0.3)

T = TypeVar("T")

#: A file's modification time, size and a hash of its content, or None for a file
#: that does not exist. The content is part of it because a coarse clock (Windows
#: updates a file's time in steps of several milliseconds) can give an edit the same
#: modification time, and an edit can keep the size; only the content tells
#: those apart. A settings file is a few kilobytes, so reading it is cheap.
FileStamp = tuple[int, int, str] | None


class SettingsFileError(RuntimeError):
    """The file cannot be written; the file is as it was."""


class ConfigParseError(SettingsFileError):
    """The file does not parse. The message is the parser's own, unaltered."""


@dataclass(frozen=True, slots=True)
class WriteResult:
    path: Path
    #: The file had changed on disk since it was last read, so the document that
    #: was read was not the one written over. The caller says so.
    file_changed: bool
    #: The file did not exist and this write created it.
    created: bool


def file_stamp(path: Path) -> FileStamp:
    """The file's stamp now; two stamps are equal only when nothing about it differs."""
    try:
        info = path.stat()
        data = path.read_bytes()
    except FileNotFoundError:
        return None
    return (info.st_mtime_ns, info.st_size, hashlib.sha256(data).hexdigest())


class SettingsWriter:
    """Sets one key at a time in the configuration file Murmly is using.

    Only that key's value changes: comments, key order and every other value are
    kept by `tomlkit`. The value is checked with `murmly.config`'s own rules
    first, so nothing is written that the daemon would set aside. A file that
    does not parse is never written, and neither is a document read before the
    file changed: the fresh file is read again and the key applied to that.
    """

    def __init__(
        self,
        path: str | Path | None = None,
        env: dict[str, str] | None = None,
        *,
        replace: Callable[[str | Path, str | Path], None] | None = None,
        sleep: Callable[[float], None] = time.sleep,
        retry_permission_errors: bool | None = None,
    ) -> None:
        """`replace`, `sleep` and `retry_permission_errors` are injectable so a test can
        make the file system refuse; the last defaults to on for Windows only."""
        self.path = Path(path) if path else default_config_path(env)
        self._read_stamp: FileStamp = None
        self._has_read = False
        # Looked up at the time of the call, so patching os.replace reaches it.
        self._replace_file = replace or (lambda source, destination: os.replace(source, destination))
        self._sleep = sleep
        self._retry = sys.platform == "win32" if retry_permission_errors is None else retry_permission_errors

    def retrying(self, action: Callable[[], T]) -> T:
        """Run `action`, trying again over about a second while it raises PermissionError.

        Only where permission errors are transient (Windows). The last error is raised.
        """
        for delay in (*TRANSIENT_DELAYS_S, None):
            try:
                return action()
            except PermissionError:
                if not self._retry or delay is None:
                    raise
                self._sleep(delay)
        raise AssertionError("unreachable")  # pragma: no cover

    def stamp(self) -> FileStamp:
        """The file's stamp now, trying again while another program has it open."""
        return self.retrying(lambda: file_stamp(self.path))

    def read(self) -> dict[str, object]:
        """The file's parsed content, remembering its stamp for the next `set`.

        Raises `ConfigParseError` when the file does not parse. A file that does
        not exist reads as empty.
        """
        stamp, text = self._read_text()
        data = self._parse(text)
        self._read_stamp = stamp
        self._has_read = True
        return data

    def set(self, key: str, value: object) -> WriteResult:
        validate_setting(key, value)
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise SettingValueError(f"{key} must be text that can be written as UTF-8.") from error
        table_name, _, name = key.partition(".")

        file_changed = self._has_read and self._stamp_now() != self._read_stamp
        for _ in range(MAX_WRITE_ATTEMPTS):
            stamp, text = self._read_text()
            self._parse(text)
            document = tomlkit.parse(text)
            self._apply(document, table_name, name, value)
            updated = tomlkit.dumps(document)
            # Whatever tomlkit produced must read back as the value asked for,
            # or the file is left alone rather than trusted to a round trip.
            written = tomllib.loads(updated).get(table_name, {})
            if not isinstance(written, dict) or written.get(name) != value:
                raise SettingsFileError(f"{key} could not be written to {self.path} unchanged.")
            if self._stamp_now() != stamp:
                file_changed = True
                continue
            self._replace(updated, existed=stamp is not None)
            self._read_stamp = self._stamp_now()
            self._has_read = True
            return WriteResult(self.path, file_changed=file_changed, created=stamp is None)
        raise SettingsFileError(f"{self.path} kept changing while {key} was being written.")

    def _stamp_now(self) -> FileStamp:
        try:
            return file_stamp(self.path)
        except OSError as error:
            raise SettingsFileError(f"Unable to read {self.path}: {error}") from error

    def _read_text(self) -> tuple[FileStamp, str]:
        """The file's text and the stamp of exactly the bytes that text came from."""
        try:
            try:
                info, data = self.retrying(lambda: (self.path.stat(), self.path.read_bytes()))
            except FileNotFoundError:
                return None, ""
            stamp = (info.st_mtime_ns, info.st_size, hashlib.sha256(data).hexdigest())
            return stamp, data.decode("utf-8")
        except (OSError, UnicodeDecodeError) as error:
            raise SettingsFileError(f"Unable to read {self.path}: {error}") from error

    def _parse(self, text: str) -> dict[str, object]:
        try:
            return tomllib.loads(text)
        except tomllib.TOMLDecodeError as error:
            raise ConfigParseError(str(error)) from error

    def _apply(self, document: tomlkit.TOMLDocument, table_name: str, name: str, value: object) -> None:
        if table_name not in document:
            document[table_name] = tomlkit.table()
        table = document[table_name]
        if not isinstance(table, (Table, InlineTable)):
            raise SettingsFileError(f"[{table_name}] in {self.path} is not a table.")
        table[name] = value

    def _replace(self, text: str, *, existed: bool) -> None:
        """Write beside the file and swap it in, so a reader never sees half a file.

        A symbolic link is written through: the temporary file goes next to the
        link's final target and replaces that, so the link stays and the target
        has the new content. A link to a missing file creates the file there.
        """
        temporary: str | None = None
        try:
            target = Path(os.path.realpath(self.path))
            if not target.parent.is_dir() and self.path.is_symlink():
                raise SettingsFileError(
                    f"{self.path} is a link to {target}, and the folder {target.parent} does not exist."
                )
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = stat.S_IMODE(target.stat().st_mode) if existed else 0o644
            descriptor, temporary = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            self.retrying(lambda: self._replace_file(temporary, target))
            temporary = None
        except OSError as error:
            if isinstance(error, PermissionError):
                hint = (
                    " Another program may have it open."
                    if self._retry
                    else " Murmly is not allowed to write the file or its folder."
                )
            else:
                hint = ""
            raise SettingsFileError(f"Unable to write {self.path}: {error}.{hint}") from error
        finally:
            if temporary is not None:
                Path(temporary).unlink(missing_ok=True)
