"""The enrolled voices on disk: `<data dir>/voiceprints.json`.

Only embeddings are stored, never audio and never a timestamp. The file is
private to the account that runs Murmly: created with mode 0600, and any
directory Murmly creates for it with 0700. On Windows neither mode applies and
the file inherits the per-user ACL of `%LOCALAPPDATA%`; that this excludes other
standard accounts is an assumption until task 1.7 runs `icacls` there.

A damaged or unreadable file never raises into the daemon. It reads as no voices
with a `detail` that `murmly doctor` can show.
"""

from __future__ import annotations

from collections.abc import Sequence
import json
import logging
import os
from pathlib import Path
import threading
import uuid

from murmly.speakers import (
    EMBEDDING_DIMENSION,
    SPEAKER_MODEL_SHA256,
    Voice,
    l2_normalised,
    same_name,
)


logger = logging.getLogger(__name__)

VOICEPRINTS_FILENAME = "voiceprints.json"
FORMAT_VERSION = 1


class VoiceprintStore:
    """Reads, caches and rewrites the voiceprint file.

    `refresh()` is what the daemon calls when a capture starts: one `stat`, and a
    re-read only when `(st_mtime_ns, st_size)` moved. A missing file is no voices.

    The installed model's identity is taken to be `model_sha256`, which defaults
    to the pinned `SPEAKER_MODEL_SHA256`, rather than hashing the 26 MB file
    here. `setup.sh` and `bootstrap.ps1` refuse a file that does not match the
    pin, and `murmly doctor` hashes the file on its own; hashing it at every
    capture would put a read of the whole model in front of the first word.
    """

    def __init__(
        self,
        directory: Path,
        *,
        model_sha256: str = SPEAKER_MODEL_SHA256,
        dimension: int = EMBEDDING_DIMENSION,
    ) -> None:
        self._directory = Path(directory)
        self._path = self._directory / VOICEPRINTS_FILENAME
        self._model_sha256 = model_sha256
        self._dimension = dimension
        self._lock = threading.RLock()
        self._signature: tuple[int, int] | None = None
        self._loaded = False
        self._voices: list[Voice] = []
        self._needs_reenrolment = False
        self._detail: str | None = None

    @property
    def path(self) -> Path:
        return self._path

    @property
    def voices(self) -> list[Voice]:
        """The voices as of the last `refresh()` or write. A copy."""
        return list(self._voices)

    @property
    def names(self) -> list[str]:
        return [voice.name for voice in self._voices]

    @property
    def needs_reenrolment(self) -> bool:
        """The file holds voiceprints made by a different model, now ignored."""
        return self._needs_reenrolment

    @property
    def detail(self) -> str | None:
        """Why the file could not be used, or None. Never carries a vector."""
        return self._detail

    def refresh(self) -> bool:
        """Re-read the file if it changed. Reports whether the voices were re-read."""
        with self._lock:
            signature = self._stat()
            if self._loaded and signature == self._signature:
                return False
            self._read(signature)
            return True

    def add_or_replace(self, name: str, embedding: Sequence[float]) -> bool:
        """Store a voice under `name`, replacing one with the same name in any case.

        Reports whether it replaced one. The embedding is stored unit length.
        The file is read again first, so a voice another process stored since
        `refresh()` is kept. Voiceprints made by a different model are dropped by
        this write: one file holds one model's voices, and those were already
        ignored.
        """
        unit = [float(value) for value in l2_normalised(embedding)]
        if len(unit) != self._dimension:
            raise ValueError(f"a voiceprint has {self._dimension} values, not {len(unit)}")
        with self._lock:
            self._read(self._stat())
            kept = [voice for voice in self._voices if not same_name(voice.name, name)]
            replaced = len(kept) != len(self._voices)
            kept.append(Voice(name, tuple(unit)))
            self._write(kept)
            return replaced

    def remove(self, name: str) -> bool:
        """Remove one voice. Reports whether it was enrolled.

        The remaining voices are written back exactly as read. Removing the last
        one deletes the file.
        """
        with self._lock:
            self._read(self._stat())
            kept = [voice for voice in self._voices if not same_name(voice.name, name)]
            if len(kept) == len(self._voices):
                return False
            self._write(kept)
            return True

    def remove_all(self) -> bool:
        """Delete the file, whatever it holds. Reports whether one existed."""
        with self._lock:
            existed = self._delete()
            self._read(self._stat())
            return existed

    # -- reading ----------------------------------------------------------------

    def _stat(self) -> tuple[int, int] | None:
        try:
            result = os.stat(self._path)
        except OSError:
            return None
        return (result.st_mtime_ns, result.st_size)

    def _read(self, signature: tuple[int, int] | None) -> None:
        self._loaded = True
        self._signature = signature
        self._voices = []
        self._needs_reenrolment = False
        self._detail = None
        if signature is None:
            return
        try:
            with open(self._path, encoding="utf-8") as handle:
                payload = json.load(handle)
            voices, model_sha256 = self._parse(payload)
        except (OSError, ValueError) as error:
            # No path or value in the message: it is shown by `doctor`, and the
            # parse errors below name only what was wrong.
            self._detail = f"the voiceprint file cannot be used: {error}"
            logger.warning("Voiceprints ignored: %s", self._detail)
            return
        if model_sha256 != self._model_sha256:
            self._needs_reenrolment = True
            self._detail = "the voiceprints were made with a different speaker model"
            logger.warning("Voiceprints ignored: %s; enrol again.", self._detail)
            return
        self._voices = voices

    def _parse(self, payload: object) -> tuple[list[Voice], str]:
        if not isinstance(payload, dict) or payload.get("version") != FORMAT_VERSION:
            raise ValueError("unsupported format")
        model_sha256 = payload.get("model_sha256")
        voices = payload.get("voices")
        if not isinstance(model_sha256, str) or not isinstance(voices, list):
            raise ValueError("unsupported format")
        if model_sha256 != self._model_sha256:
            # Another model's entries may not even have this model's dimension,
            # and are ignored either way, so they are not checked.
            return [], model_sha256
        parsed: list[Voice] = []
        for entry in voices:
            name = entry.get("name") if isinstance(entry, dict) else None
            values = entry.get("embedding") if isinstance(entry, dict) else None
            if (
                not isinstance(name, str)
                or not isinstance(values, list)
                or payload.get("dimension") != self._dimension
                or len(values) != self._dimension
                or not all(
                    isinstance(value, (int, float)) and not isinstance(value, bool)
                    for value in values
                )
            ):
                raise ValueError("a voice entry is malformed")
            parsed.append(Voice(name, tuple(float(value) for value in values)))
        return parsed, model_sha256

    # -- writing ----------------------------------------------------------------

    def _write(self, voices: list[Voice]) -> None:
        if not voices:
            self._delete()
            self._read(self._stat())
            return
        payload = {
            "version": FORMAT_VERSION,
            "model_sha256": self._model_sha256,
            "dimension": self._dimension,
            "voices": [{"name": v.name, "embedding": list(v.embedding)} for v in voices],
        }
        self._make_directory()
        temporary = self._directory / f".{VOICEPRINTS_FILENAME}.{uuid.uuid4().hex}.tmp"
        # 0600 at creation, so the vectors are never readable by another account,
        # not even between the create and a later chmod.
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self._path)
        except BaseException:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        self._read(self._stat())

    def _delete(self) -> bool:
        try:
            os.unlink(self._path)
        except FileNotFoundError:
            return False
        return True

    def _make_directory(self) -> None:
        """Create the data directory and any missing parent with mode 0700.

        `os.makedirs(mode=...)` applies the mode to the last directory only, and
        a directory Murmly creates is Murmly's to keep private.
        """
        missing: list[Path] = []
        directory = self._directory
        while not directory.exists():
            missing.append(directory)
            if directory.parent == directory:
                break
            directory = directory.parent
        for directory in reversed(missing):
            os.mkdir(directory, 0o700)
            # The mode given to mkdir is filtered through the umask.
            os.chmod(directory, 0o700)
