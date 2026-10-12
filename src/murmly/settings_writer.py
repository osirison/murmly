from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os
import stat
import tempfile
import tomllib

import tomlkit
from tomlkit.items import InlineTable, Table

from murmly.config import default_config_path, validate_setting

#: How many times a write starts over because the file changed under it. A hand
#: edit landing inside the few milliseconds of one write is rare; three in a row
#: means something else is writing the file, and that is reported rather than
#: raced.
MAX_WRITE_ATTEMPTS = 3

#: A file's modification time and size, or None for a file that does not exist.
#: Both, because a coarse clock can give an edit the same modification time.
FileStamp = tuple[int, int] | None


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


def _stamp(path: Path) -> FileStamp:
    try:
        info = path.stat()
    except FileNotFoundError:
        return None
    return (info.st_mtime_ns, info.st_size)


class SettingsWriter:
    """Sets one key at a time in the configuration file Murmly is using.

    Only that key's value changes: comments, key order and every other value are
    kept by `tomlkit`. The value is checked with `murmly.config`'s own rules
    first, so nothing is written that the daemon would set aside. A file that
    does not parse is never written, and neither is a document read before the
    file changed: the fresh file is read again and the key applied to that.
    """

    def __init__(self, path: str | Path | None = None, env: dict[str, str] | None = None) -> None:
        self.path = Path(path) if path else default_config_path(env)
        self._read_stamp: FileStamp = None
        self._has_read = False

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
        table_name, _, name = key.partition(".")

        file_changed = self._has_read and _stamp(self.path) != self._read_stamp
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
            if _stamp(self.path) != stamp:
                file_changed = True
                continue
            self._replace(updated, existed=stamp is not None)
            self._read_stamp = _stamp(self.path)
            self._has_read = True
            return WriteResult(self.path, file_changed=file_changed, created=stamp is None)
        raise SettingsFileError(f"{self.path} kept changing while {key} was being written.")

    def _read_text(self) -> tuple[FileStamp, str]:
        try:
            stamp = _stamp(self.path)
            if stamp is None:
                return None, ""
            return stamp, self.path.read_bytes().decode("utf-8")
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
        """Write beside the file and swap it in, so a reader never sees half a file."""
        directory = self.path.parent
        directory.mkdir(parents=True, exist_ok=True)
        mode = stat.S_IMODE(self.path.stat().st_mode) if existed else 0o644
        descriptor, temporary = tempfile.mkstemp(
            dir=directory, prefix=f".{self.path.name}.", suffix=".tmp"
        )
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(text.encode("utf-8"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, mode)
            os.replace(temporary, self.path)
        except OSError as error:
            Path(temporary).unlink(missing_ok=True)
            raise SettingsFileError(f"Unable to write {self.path}: {error}") from error
