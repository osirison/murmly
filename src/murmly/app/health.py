"""What Health shows: plain Python, no Qt.

The report comes from `murmly doctor` itself, run as a separate process, so the
checks exist in one place only. Doctor opens the microphone and the speaker,
loads the transcription model when live transcription is on and starts the
overlay helper; none of that belongs in the app's process, and a hung audio
device must not hang the window. The process gets a time limit, and quitting the
app stops it.

`build_rows` turns the report into rows: a plain sentence for each section, with
the raw keys and values it came from beside it. Anything doctor wrote itself (a
`detail`, a `reason`, a remedy) is shown exactly as written, never reworded.
A section this module has no sentence for still gets a row, so a section added
to doctor later appears without a change here.

Nothing here reads, stores or logs a transcript: doctor does not report one, and
the text offered for copying is doctor's own output, unchanged.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime

# Doctor can load a model and open two audio devices. Generous, because a cold
# machine without a graphics card is slow; bounded, because a hung device is not
# a reason to wait for ever.
DOCTOR_TIMEOUT_S = 120.0

OK = "ok"
PROBLEM = "problem"


# ---------------------------------------------------------------------------
# Running doctor
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DoctorResult:
    """What one run of `murmly doctor` produced.

    `text` is exactly what the command printed, and is what "Copy the full
    report" copies. `report` is that text parsed, `None` when the run failed;
    `error` then says why, in the words the failure came with.
    """

    text: str = ""
    report: dict | None = None
    error: str = ""


def doctor_command() -> list[str]:
    return [sys.executable, "-m", "murmly", "doctor"]


def child_environment() -> dict[str, str]:
    """The environment doctor runs in: ours, with its pipes in UTF-8.

    A Python child writing to a pipe on Windows uses the ANSI code page unless
    told otherwise, and this side decodes UTF-8; the two must agree or any
    accent or dash in doctor's output is garbled.
    """
    return {**os.environ, "PYTHONIOENCODING": "utf-8"}


class DoctorRunner:
    """Runs `murmly doctor` and parses its answer. Blocking: call it from a worker thread."""

    def __init__(
        self,
        command: Callable[[], list[str]] = doctor_command,
        timeout: float = DOCTOR_TIMEOUT_S,
        popen: Callable[..., subprocess.Popen] = subprocess.Popen,
    ) -> None:
        self._command = command
        self._timeout = timeout
        self._popen = popen
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None
        self._cancelled = False

    def run(self) -> DoctorResult:
        kwargs: dict[str, object] = {}
        if sys.platform == "win32":
            # No console window flashing up each time Health opens.
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
        with self._lock:
            if self._cancelled:
                return DoctorResult(error="The app is closing.")
            try:
                process = self._popen(
                    self._command(),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    env=child_environment(),
                    **kwargs,
                )
            except OSError as error:
                return DoctorResult(error=f"murmly doctor could not be started: {error}")
            self._process = process
        try:
            try:
                stdout, stderr = process.communicate(timeout=self._timeout)
            except subprocess.TimeoutExpired:
                process.kill()
                process.communicate()
                return DoctorResult(
                    error=f"murmly doctor did not finish within {self._timeout:g} seconds."
                )
        finally:
            with self._lock:
                self._process = None
        return parse_doctor_output(stdout, stderr, process.returncode)

    def cancel(self) -> None:
        """Stop a run in progress, and refuse to start another. Safe to call twice."""
        with self._lock:
            self._cancelled = True
            process = self._process
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass


def parse_doctor_output(stdout: str, stderr: str, returncode: int | None) -> DoctorResult:
    """A finished run as a result. A failure carries what the command said, verbatim."""
    if returncode not in (0, None):
        said = stderr.strip() or stdout.strip()
        return DoctorResult(error=said or f"murmly doctor stopped with status {returncode}.")
    try:
        report = json.loads(stdout)
    except ValueError:
        report = None
    if not isinstance(report, dict):
        said = stderr.strip()
        return DoctorResult(
            error=said or "murmly doctor printed something that was not a report."
        )
    return DoctorResult(text=stdout, report=report)


# ---------------------------------------------------------------------------
# Rows
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Row:
    """One check: a sentence, the raw values behind it, and how it went.

    `urgent` is a problem that stops dictation or leaves it unsafe (shown with
    the crimson outline). A problem that is not urgent leaves dictation alone
    (amber). `pairs` are the headline keys and values; `every` is every value
    in the sections the row covers. `keys` are the report's top-level keys the
    row stands for.
    """

    title: str
    sentence: str
    status: str = OK
    urgent: bool = False
    pairs: tuple[tuple[str, str], ...] = ()
    every: tuple[tuple[str, str], ...] = ()
    keys: tuple[str, ...] = ()

    def view(self) -> dict[str, object]:
        if self.status == OK:
            tone = "none"
        else:
            tone = "crimson" if self.urgent else "amber"
        return {
            "title": self.title,
            "sentence": self.sentence,
            "status": self.status,
            "tone": tone,
            "raw": " · ".join(f"{key}: {value}" for key, value in self.pairs),
            "pairs": [{"key": key, "value": value} for key, value in self.pairs],
            "every": [{"key": key, "value": value} for key, value in self.every],
        }


def show(value: object) -> str:
    """A value as written: text untouched, everything else as JSON would spell it."""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(", ", ": "))


def leaves(prefix: str, value: object) -> list[tuple[str, str]]:
    """Every value under `prefix` as dotted key and text, text untouched."""
    if isinstance(value, Mapping):
        found: list[tuple[str, str]] = []
        for key, inner in value.items():
            found.extend(leaves(f"{prefix}.{key}" if prefix else str(key), inner))
        return found
    if isinstance(value, list) and value and all(isinstance(item, (Mapping, list)) for item in value):
        found = []
        for index, inner in enumerate(value):
            found.extend(leaves(f"{prefix}[{index}]", inner))
        return found
    if (
        prefix.endswith("remedy")
        and isinstance(value, list)
        and value
        and all(isinstance(item, str) for item in value)
    ):
        # A list of sentences to follow: each as written, not one JSON string.
        return [(f"{prefix}[{index}]", item) for index, item in enumerate(value)]
    return [(prefix, show(value))]


def _section(report: Mapping, key: str) -> Mapping:
    value = report.get(key)
    return value if isinstance(value, Mapping) else {}


def _pairs(prefix: str, section: Mapping, *names: str) -> list[tuple[str, str]]:
    """The named keys of a section that it carries, with a `prefix.` on each."""
    found: list[tuple[str, str]] = []
    for name in names:
        if name in section:
            found.extend(leaves(f"{prefix}.{name}" if prefix else name, section[name]))
    return found


def _every(report: Mapping, keys: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    found: list[tuple[str, str]] = []
    for key in keys:
        if key in report:
            found.extend(leaves(key, report[key]))
    return tuple(found)


def _text(value: object) -> str:
    return value if isinstance(value, str) else ""


class _Verdict:
    """The first problem a row finds wins the sentence; every pair is kept."""

    def __init__(self) -> None:
        self.status = OK
        self.urgent = False
        self.sentence = ""
        self.pairs: list[tuple[str, str]] = []

    def problem(self, sentence: str, *, urgent: bool = False) -> None:
        if self.status == OK:
            self.status = PROBLEM
            self.urgent = urgent
            self.sentence = sentence
        elif urgent and not self.urgent:
            # A worse problem found later takes the sentence.
            self.urgent = True
            self.sentence = sentence

    def add(self, pairs: list[tuple[str, str]]) -> None:
        for pair in pairs:
            if pair not in self.pairs:
                self.pairs.append(pair)

    def row(self, title: str, ok_sentence: str, report: Mapping, keys: tuple[str, ...]) -> Row:
        # A key that says nothing (null) is left out of the headline, as long as
        # something is left; every value, null or not, is still under `every`.
        shown = [pair for pair in self.pairs if pair[1] != "null"] or self.pairs[:1]
        return Row(
            title=title,
            sentence=self.sentence if self.status == PROBLEM else ok_sentence,
            status=self.status,
            urgent=self.urgent,
            pairs=tuple(shown),
            every=_every(report, keys),
            keys=keys,
        )


def _concern(
    report: Mapping, verdict: _Verdict, name: str, phrase: str, *, urgent: bool
) -> None:
    """Fold one platform concern into the row it belongs to.

    An unavailable concern is a problem of its own even when the row's own
    section says nothing: it is what the desktop cannot do, with doctor's
    reason beside it.
    """
    concern = _section(_section(_section(report, "platform"), "concerns"), name)
    if not concern:
        return
    base = f"platform.concerns.{name}"
    if concern.get("available") is False:
        verdict.problem(f"This desktop offers no way to {phrase}.", urgent=urgent)
        verdict.add(_pairs(base, concern, "mechanism", "available", "reason", "remedy"))


def _chord(text: object) -> str:
    return " + ".join(part for part in str(text).split("+") if part)


# -- the rows -----------------------------------------------------------------


def _your_key(report: Mapping) -> list[Row]:
    installation = _section(report, "installation")
    verdict = _Verdict()
    key = installation.get("hotkey")
    held = installation.get("hotkey_held")
    verdict.add(_pairs("installation", installation, "hotkey", "hotkey_held"))
    if not key:
        verdict.problem("No key is set for dictating yet.", urgent=True)
    elif held is not True:
        verdict.problem(
            f"{_chord(key)} is set, but your desktop is not delivering it to murmly.",
            urgent=True,
        )
    _concern(report, verdict, "hotkey_registration", "register a dictation key", urgent=True)
    ok = f"{_chord(key)} belongs to murmly, and your desktop delivers it." if key else ""
    return [verdict.row("Your key", ok, report, ("installation",))]


def _background_service(report: Mapping) -> list[Row]:
    installation = _section(report, "installation")
    socket = _section(report, "command_socket")
    verdict = _Verdict()
    verdict.add(_pairs("installation", installation, "installed", "service_active", "detail"))
    verdict.add(_pairs("command_socket", socket, "path_private", "detail", "peer_identity_detail"))
    if installation.get("installed") is not True:
        verdict.problem("Murmly is not installed as a background service.", urgent=True)
    elif installation.get("service_active") is not True:
        verdict.problem("The background service is installed but not running.", urgent=True)
    if socket.get("path_private") is False:
        verdict.problem(
            "The command socket can be reached by other accounts on this computer.", urgent=True
        )
    _concern(report, verdict, "service_management", "run a background service", urgent=True)
    _concern(report, verdict, "command_channel", "talk to the background service", urgent=True)
    return [
        verdict.row(
            "Background service",
            "Running, and its command socket belongs to you alone.",
            report,
            ("installation", "command_socket"),
        )
    ]


def _microphone(report: Mapping) -> list[Row]:
    microphone = _section(report, "microphone")
    verdict = _Verdict()
    verdict.add(
        _pairs(
            "microphone", microphone, "device_present", "available", "detail", "device_detail"
        )
    )
    permission = microphone.get("permission")
    if isinstance(permission, Mapping):
        verdict.add(_pairs("microphone.permission", permission, "state", "grant_location", "detail"))
    if microphone.get("available") is not True:
        if microphone.get("device_present") is False:
            sentence = "No microphone was found."
        elif isinstance(permission, Mapping) and permission.get("state") == "denied":
            sentence = "Murmly is not allowed to use the microphone."
        else:
            sentence = "Murmly could not check the microphone."
        verdict.problem(sentence, urgent=True)
    elif microphone.get("detail"):
        verdict.problem("A microphone is present, with a caveat.")
    return [
        verdict.row(
            "Microphone", "A microphone is present and murmly may use it.", report, ("microphone",)
        )
    ]


def _typing(report: Mapping) -> list[Row]:
    paste = _section(report, "paste_injection")
    clipboard = report.get("clipboard_command")
    verdict = _Verdict()
    verdict.add(_pairs("", report, "clipboard_command"))
    verdict.add(
        _pairs("paste_injection", paste, "method", "available", "reason", "remedy", "advisory")
    )
    if isinstance(clipboard, str) and clipboard.startswith("unavailable"):
        verdict.problem("Murmly cannot reach the clipboard.", urgent=True)
    if paste.get("available") is False:
        verdict.problem("Murmly cannot paste your words into a window.", urgent=True)
    elif paste.get("advisory"):
        verdict.problem("Pasting works, with a caveat.")
    _concern(report, verdict, "clipboard", "use the clipboard", urgent=True)
    _concern(report, verdict, "paste_injection", "paste into a window", urgent=True)
    method = paste.get("method")
    ok = (
        f"Murmly puts your words on the clipboard and pastes them with {method}."
        if method
        else "Murmly puts your words on the clipboard and pastes them."
    )
    return [verdict.row("Typing your words", ok, report, ("clipboard_command", "paste_injection"))]


def _window_check(report: Mapping) -> list[Row]:
    delivery = _section(report, "delivery")
    verdict = _Verdict()
    verdict.add(
        _pairs(
            "delivery",
            delivery,
            "verification_supported",
            "verification_enabled",
            "restore_clipboard",
            "detail",
        )
    )
    if delivery.get("verification_supported") is False:
        verdict.problem(
            "Murmly cannot check which window has focus here, so it types without checking."
        )
    _concern(report, verdict, "focus_observation", "see which window has focus", urgent=False)
    if delivery.get("verification_supported") is True and delivery.get("verification_enabled") is False:
        ok = "Checking the window first is turned off. Murmly types where focus is."
    else:
        ok = "Murmly checks the window first, and puts your clipboard back."
    if delivery.get("restore_clipboard") is False and "Murmly checks" in ok:
        ok = "Murmly checks the window first. Your clipboard is not put back."
    return [verdict.row("Checking the window first", ok, report, ("delivery",))]


_DEVICES = {"cuda": "the graphics card", "cpu": "the processor"}


def _transcription(report: Mapping) -> list[Row]:
    keys = (
        "model_profile",
        "model_name",
        "model_cache_path",
        "model_cache_detail",
        "device",
        "compute_type",
        "runtime_device",
        "runtime_detail",
        "runtime_compute_type",
        "beam_size",
        "vad_filter",
    )
    verdict = _Verdict()
    verdict.add(
        _pairs(
            "",
            report,
            "model_profile",
            "model_name",
            "runtime_device",
            "runtime_compute_type",
            "runtime_detail",
            "model_cache_path",
            "model_cache_detail",
        )
    )
    if report.get("runtime_detail"):
        verdict.problem("Murmly could not work out how to run transcription.", urgent=True)
    if report.get("model_cache_detail"):
        verdict.problem("Murmly could not find where downloaded models are kept.")
    profile = _text(report.get("model_profile")).capitalize()
    where = _DEVICES.get(_text(report.get("runtime_device")), "a device it picks when it starts")
    ok = f"{profile} accuracy, on {where}." if profile else f"Transcription runs on {where}."
    return [verdict.row("Transcription", ok, report, keys)]


def _memory(report: Mapping) -> list[Row]:
    keys = (
        "model_resident",
        "model_resident_detail",
        "unload_after_idle_s",
        "system_memory_returnable",
        "system_memory_returnable_detail",
    )
    verdict = _Verdict()
    verdict.add(_pairs("", report, *keys))
    resident = report.get("model_resident")
    idle = report.get("unload_after_idle_s")
    if report.get("model_resident_detail"):
        verdict.problem("Murmly could not ask the service whether the model is loaded.")
    if report.get("system_memory_returnable_detail"):
        verdict.problem("Memory freed by murmly may not go back to the system.")
    if resident is True:
        ok = "The transcription model is loaded and ready."
    elif resident is False:
        ok = "The transcription model is not loaded. It loads when you start speaking."
    else:
        ok = "Murmly does not know whether the transcription model is loaded."
    if isinstance(idle, (int, float)) and not isinstance(idle, bool):
        ok += " It is kept loaded." if idle == 0 else f" It is given back after {idle:g} seconds idle."
    return [verdict.row("Memory", ok, report, keys)]


_FINISHING = {
    "off": "You press the keys again to finish.",
    "stop": "It finishes when you stop talking.",
    "continuous": "It types as you pause.",
}


def _live(report: Mapping) -> list[Row]:
    live = _section(report, "live_transcription")
    verdict = _Verdict()
    verdict.add(
        _pairs(
            "live_transcription",
            live,
            "live_transcribe",
            "auto_transcribe",
            "silence_detection_available",
            "silence_detection_detail",
            "partial_pass_keeps_pace",
            "partial_pass_ceiling_ms",
            "auto_transcribe_rejected_value",
        )
    )
    auto = live.get("auto_transcribe")
    if live.get("auto_transcribe_rejected_value") is not None:
        verdict.problem("The finishing setting was not understood, so a default is used.")
    if auto not in (None, "off") and live.get("silence_detection_available") is False:
        verdict.problem("Murmly cannot tell when you stop talking.")
    if live.get("live_transcribe") is True:
        if live.get("partial_pass_keeps_pace") is False:
            verdict.problem("Live text takes longer to update than it is asked to.")
            verdict.add(_pairs("live_transcription", live, "partial_pass_detail"))
        elif live.get("partial_pass_ceiling_ms") is None and live.get("partial_pass_detail"):
            verdict.problem("Murmly could not measure how quickly live text updates.")
            verdict.add(_pairs("live_transcription", live, "partial_pass_detail"))
    live_text = "Live text is on" if live.get("live_transcribe") else "Live text is off"
    ok = f"{live_text}. {_FINISHING.get(auto, '')}".strip()
    return [verdict.row("Live text and finishing", ok, report, ("live_transcription",))]


_VOICES = {
    "off": "Everyone's voice is typed.",
    "mine-only": "Only your voice is typed.",
    "label-everyone": "Everyone's voice is typed, each marked by name.",
}


def _voices(report: Mapping) -> list[Row]:
    speakers = _section(report, "speaker_recognition")
    verdict = _Verdict()
    mode = speakers.get("mode")
    verdict.add(
        _pairs(
            "speaker_recognition",
            speakers,
            "mode",
            "available",
            "detail",
            "owner_enrolled",
            "enrolled_count",
            "model_checksum_matches",
            "mode_rejected_value",
            "match_threshold_rejected_value",
        )
    )
    if speakers.get("mode_rejected_value") is not None or speakers.get("match_threshold_rejected_value") is not None:
        verdict.problem("A voice setting was not understood, so a default is used.")
    # Off is a choice, not a fault: its `available: false` is not a problem.
    if mode not in (None, "off"):
        if speakers.get("available") is False:
            verdict.problem(
                "Speaker recognition cannot run, so everything is typed whoever speaks."
            )
        elif speakers.get("detail"):
            verdict.problem("Speaker recognition works, with a caveat.")
    ok = _VOICES.get(mode, "Voice recognition is set up.")
    if mode == "mine-only" and speakers.get("owner"):
        ok = f"Only your voice is typed. {speakers['owner']} is enrolled as you."
    return [verdict.row("Voices", ok, report, ("speaker_recognition",))]


def _indicator(report: Mapping) -> list[Row]:
    overlay = _section(report, "overlay")
    verdict = _Verdict()
    verdict.add(_pairs("overlay", overlay, "enabled", "available", "backend", "detail"))
    if overlay.get("enabled") is True and overlay.get("available") is False:
        verdict.problem("The on-screen indicator cannot be shown on this desktop.")
    _concern(report, verdict, "overlay", "draw the on-screen indicator", urgent=False)
    if overlay.get("enabled") is False:
        ok = "The on-screen indicator is turned off."
    else:
        ok = "The on-screen indicator appears while you dictate."
    return [verdict.row("On-screen indicator", ok, report, ("overlay",))]


def _speech(report: Mapping) -> list[Row]:
    speech = _section(report, "speech_output")
    verdict = _Verdict()
    verdict.add(
        _pairs(
            "speech_output",
            speech,
            "enabled",
            "available",
            "detail",
            "voice",
            "device_in_use",
            "device_detail",
            "output_device_detail",
            "playback_dropouts",
            "playback_starvations",
        )
    )
    if speech.get("enabled") is True:
        if speech.get("available") is False:
            verdict.problem("Murmly cannot speak aloud. Dictation is unaffected.")
        elif speech.get("device_detail"):
            verdict.problem("Speech works, but not on the processor it was asked for.")
        elif speech.get("output_device_detail"):
            verdict.problem("Speech works, with a note about the output device.")
        else:
            dropouts = speech.get("playback_dropouts")
            if isinstance(dropouts, int) and not isinstance(dropouts, bool) and dropouts > 0:
                verdict.problem("Speech has been cutting out.")
    _concern(report, verdict, "speech_synthesis", "speak aloud", urgent=False)
    if speech.get("enabled") is False:
        ok = "Speech is turned off."
    else:
        voice = _text(speech.get("voice"))
        ok = f"Murmly can speak aloud, in the {voice} voice." if voice else "Murmly can speak aloud."
    return [verdict.row("Speech", ok, report, ("speech_output",))]


_SYSTEMS = {"linux": "Linux", "windows": "Windows", "macos": "macOS"}


def _computer(report: Mapping) -> list[Row]:
    platform = _section(report, "platform")
    verdict = _Verdict()
    verdict.add(_pairs("", report, "session", "session_detail"))
    verdict.add(_pairs("platform", platform, "operating_system", "desktop", "supported"))
    verdict.add(_pairs("", report, "config_path", "socket_path"))
    if platform.get("supported") is False:
        verdict.problem("Murmly does not support this computer.", urgent=True)
    if report.get("session_detail"):
        verdict.problem("Murmly could not tell what kind of session this is.")
    permissions = _section(platform, "permissions")
    for name, entry in permissions.items():
        if not isinstance(entry, Mapping):
            continue
        state = entry.get("state")
        if state == "granted":
            continue
        verdict.add(_pairs(f"platform.permissions.{name}", entry, "state", "grant_location", "detail"))
        verdict.problem(
            f"Murmly has not been given the {name.replace('_', ' ')} permission."
            if state == "denied"
            else f"Murmly cannot tell whether it has the {name.replace('_', ' ')} permission.",
            urgent=state == "denied",
        )
    for name, entry in _section(platform, "environment").items():
        if not isinstance(entry, Mapping) or entry.get("satisfied") is True:
            continue
        verdict.add(
            _pairs(f"platform.environment.{name}", entry, "description", "satisfied", "remedy", "detail")
        )
        verdict.problem(f"This computer is missing something murmly needs: {name.replace('_', ' ')}.")
    system = _SYSTEMS.get(_text(platform.get("operating_system")), "")
    desktop = _text(platform.get("desktop"))
    session = _text(report.get("session"))
    parts = (
        system,
        f"{desktop.capitalize()} desktop" if desktop and desktop != "unknown" else "",
        f"{session.capitalize()} session" if session else "",
    )
    where = ", ".join(part for part in parts if part)
    ok = f"{where}." if where else "Murmly found this computer."
    return [
        verdict.row(
            "This computer",
            ok,
            report,
            ("session", "session_detail", "config_path", "socket_path", "platform"),
        )
    ]


_HANDLERS: tuple[tuple[tuple[str, ...], Callable[[Mapping], list[Row]]], ...] = (
    (("installation",), _your_key),
    (("installation", "command_socket"), _background_service),
    (("microphone",), _microphone),
    (("clipboard_command", "paste_injection"), _typing),
    (("delivery",), _window_check),
    (
        (
            "model_profile",
            "model_name",
            "model_cache_path",
            "model_cache_detail",
            "device",
            "compute_type",
            "runtime_device",
            "runtime_detail",
            "runtime_compute_type",
            "beam_size",
            "vad_filter",
        ),
        _transcription,
    ),
    (
        (
            "model_resident",
            "model_resident_detail",
            "unload_after_idle_s",
            "system_memory_returnable",
            "system_memory_returnable_detail",
        ),
        _memory,
    ),
    (("live_transcription",), _live),
    (("speaker_recognition",), _voices),
    (("overlay",), _indicator),
    (("speech_output",), _speech),
    (("session", "session_detail", "config_path", "socket_path", "platform"), _computer),
)


def _title(key: str) -> str:
    text = key.replace("_", " ").strip()
    return text[:1].upper() + text[1:]


def _fallback(report: Mapping, key: str) -> Row:
    return Row(
        title=_title(key),
        sentence="Reported by murmly doctor; shown as it is.",
        pairs=tuple(leaves(key, report[key])),
        every=tuple(leaves(key, report[key])),
        keys=(key,),
    )


def build_rows(report: Mapping) -> list[Row]:
    """A row for every section of the report, in the order a person would check them.

    A row for a section this module has no sentence for says so plainly and
    shows its values; a sentence that cannot be built falls back the same way,
    so nothing in the report goes missing.
    """
    rows: list[Row] = []
    covered: set[str] = set()
    for keys, build in _HANDLERS:
        present = [key for key in keys if key in report]
        if not present:
            continue
        try:
            rows.extend(build(report))
        except Exception:  # noqa: BLE001 - a surprise in one section must not hide the others
            continue
        covered.update(present)
    for key in report:
        if key not in covered:
            rows.append(_fallback(report, str(key)))
    return rows


def tray_row(found: bool | None) -> Row | None:
    """The app's own entry about the system tray, which doctor knows nothing about.

    `None`: the app has not looked, and says nothing. The tray code reports what
    it found. This row is the app's, and is never part of the copied report.
    """
    if found is None:
        return None
    if found:
        return Row(
            title="System tray",
            sentence="Murmly's icon is in your system tray.",
            pairs=(("tray.found", "true"),),
            every=(("tray.found", "true"),),
        )
    return Row(
        title="System tray",
        sentence="No system tray was found, so murmly runs from its window only.",
        status=PROBLEM,
        pairs=(("tray.found", "false"),),
        every=(("tray.found", "false"),),
    )


# ---------------------------------------------------------------------------
# The view
# ---------------------------------------------------------------------------

EMPTY = "empty"
CHECKING = "checking"
READY = "ready"
FAILED = "failed"


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def build_view(
    result: DoctorResult | None,
    *,
    checking: bool,
    checked_at: datetime | None,
    tray_found: bool | None = None,
) -> dict[str, object]:
    """Everything QML shows on Health, as plain values."""
    rows: list[Row] = []
    if result is not None and result.report is not None:
        rows = build_rows(result.report)
        tray = tray_row(tray_found)
        if tray is not None:
            rows.append(tray)
    when = f"checked at {checked_at:%H:%M}" if checked_at is not None else ""

    if result is None:
        state = CHECKING if checking else EMPTY
        headline = "Checking this computer"
        body = "Murmly is running its checks. This can take a few seconds."
    elif result.report is None:
        state = FAILED if not checking else CHECKING
        headline = "Murmly could not run its checks"
        body = result.error
    else:
        state = CHECKING if checking else READY
        urgent = sum(1 for row in rows if row.status == PROBLEM and row.urgent)
        notes = sum(1 for row in rows if row.status == PROBLEM and not row.urgent)
        if urgent:
            headline = "Something needs you before dictation works"
            body = f"What murmly found on this computer, {when}. {_plural(urgent, 'check')} marked in red need you."
        elif notes:
            headline = "Dictation works, with something to look at"
            body = f"What murmly found on this computer, {when}. {_plural(notes, 'note')} below, and none affects dictation."
        else:
            headline = "Dictation works end to end"
            body = f"What murmly found on this computer, {when}. Every check passed."
        if state == CHECKING:
            body = "Checking again. " + body
    return {
        "state": state,
        "headline": headline,
        "body": body,
        "error": result.error if result is not None and result.report is None else "",
        "rows": [row.view() for row in rows],
        "canCopy": result is not None and result.report is not None,
        "checking": checking,
        "checkedAt": when,
    }


@dataclass
class HealthModel:
    """Fetches the report. Blocking: it runs on the controller's worker thread."""

    runner: DoctorRunner = field(default_factory=DoctorRunner)

    def check(self) -> DoctorResult:
        return self.runner.run()

    def cancel(self) -> None:
        self.runner.cancel()
