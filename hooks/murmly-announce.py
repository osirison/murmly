#!/usr/bin/env python3
"""Announce what a coding agent just finished, out loud, through Murmly.

A Stop hook for Claude Code and GitHub Copilot CLI. Both deliver the same thing
at the end of a turn -- a JSON payload on stdin carrying the finished turn's
message and naming a JSONL transcript -- so one script serves both.

The message is taken from the payload. The transcript is a fallback for an agent
that does not send one, and a lagging fallback: when a turn ends it does not yet
hold that turn's message, so reading it finds the previous turn's.

The announcement is three parts, in this order:

  1. Three rising notes, so a person who is not looking at the terminal knows a
     message is arriving before any words start.
  2. One short sentence naming the agent, the project, and the branch.
  3. What the agent wrote to be heard.

For the third part the agent is asked to mark a passage of its message with a
<voice-note> element, which is announced as it wrote it. A message with no such
element is announced as it always was: an executive summary extracted from its
opening sentences. An element left empty announces nothing at all, which is how
an agent says this turn was not worth interrupting for.

Exits 0 whatever happens. An announcement nobody hears is a small loss; a hook
that fails a turn is not. Every reason to stay quiet is silent by design:

  - speech output disabled, or the daemon too old to know the command
  - another client already holds the session (one is open at a time)
  - the microphone is open, so speaking would be talking over the person
  - an empty voice note, or nothing worth saying in the last message

Set MURMLY_ANNOUNCE_LOG to a path to see which of those it took.

Environment:
  MURMLY_SOCKET           the command socket. Default: $XDG_RUNTIME_DIR/murmly.sock
  MURMLY_ANNOUNCE_LOG     append a line per run explaining what happened
  MURMLY_ANNOUNCE_AGENT   name the agent rather than inferring it
  MURMLY_ANNOUNCE_CHIME   0 to speak without the notes
  MURMLY_ANNOUNCE_FOREGROUND  1 to skip the detach, for testing
"""

from __future__ import annotations

import io
import json
import math
import os
import re
import shutil
import socket
import struct
import subprocess
import sys
import tempfile
import wave

#: `MURMLY_SOCKET` is set on every registration `setup.sh` makes: it resolves
#: the path once, through `murmly doctor` running under the venv (the same
#: `murmly.config.default_socket_path` authority everything else on Linux
#: uses), and `install_hooks.py --socket` bakes the answer into the command it
#: registers. That is the one resolution; this script never repeats it.
#:
#: The literal fallback below is what answers when no such registration was
#: made -- this script run directly, or `install_hooks.py` invoked without
#: `--socket` because no virtual environment existed yet to ask `murmly
#: doctor` (`./setup.sh hooks` before `./setup.sh install`). This script has
#: no import path to `murmly` to resolve it properly at that point: it runs
#: under the system Python with no virtual environment (see `setup.sh`'s
#: `install_announce_hook`), copied out of a checkout that is not required to
#: still exist by the time this runs. `openspec/changes/all-os-distributions/
#: tasks.md` task 2.6 is about this: closed by moving the resolution to
#: install time rather than by ever reaching it from here, which stays
#: impossible for the reason above.
SOCKET_PATH = os.environ.get(
    "MURMLY_SOCKET", f"{os.environ.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')}/murmly.sock"
)
LOG_PATH = os.environ.get("MURMLY_ANNOUNCE_LOG")

# Long enough to be an account of what happened, short enough that nobody waits
# through it. Roughly twenty seconds of speech.
MAX_SUMMARY_CHARACTERS = 400
MAX_SUMMARY_SENTENCES = 4
# Below this an opening sentence is a fragment rather than an outcome.
MIN_OPENER_CHARACTERS = 45
# A marked passage is not shaped by this bound, only stopped by it. The caps
# above exist because an extract stops being informative; a passage written to
# be heard has no such point, so all this is here to do is stop an agent that
# emits a runaway passage from holding the session against the person, who can
# take it back only by pressing a capture hotkey. Roughly a minute of speech.
MAX_VOICE_NOTE_CHARACTERS = 1200
#: What was announced, and so what the second `speak` frame is named. An
#: interruption event names the piece it cut off and, by the speech output
#: specification, may not carry the text itself, so the name is the only place
#: the distinction can appear in a signal.
SOURCE_VOICE_NOTE = "voice_note"
SOURCE_SUMMARY = "summary"
SOURCE_SUPPRESSED = "suppressed"
SOURCE_LABELS = {
    SOURCE_VOICE_NOTE: "voice_note (the agent's own)",
    SOURCE_SUMMARY: "summary (an extract)",
}

HEARD_ALL_TIMEOUT_SECONDS = 90.0
CONNECT_TIMEOUT_SECONDS = 2.0
GIT_TIMEOUT_SECONDS = 1.0

CHIME_RATE_HZ = 48_000
# A rising stack of fourths. It resolves upward, which reads as an arrival
# rather than a warning, and it sits above speech so it is not mistaken for it.
CHIME_NOTES_HZ = (880.00, 1174.66, 1760.00)
CHIME_NOTE_SECONDS = 0.13
CHIME_ATTACK_SECONDS = 0.006
CHIME_PEAK = 0.28
CHIME_PLAYERS = (("pw-play",), ("paplay",), ("aplay", "-q"))


def note(message: str) -> None:
    if not LOG_PATH:
        return
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as handle:
            handle.write(message + "\n")
    except OSError:
        pass


# ------------------------------------------------------------ the payload ---


def payload_field(payload: dict, *names: str) -> str:
    """The first field present under any of `names`.

    Claude Code and Copilot's `Stop` event both send snake_case, and Copilot's
    `agentStop` alias sends the same fields in camelCase. Reading both means the
    hook works whichever event a person wired it to.
    """
    for name in names:
        value = payload.get(name)
        if isinstance(value, str) and value:
            return value
    return ""


# --------------------------------------------------------- the transcript ---


def transcript_rows(transcript_path: str) -> list[dict]:
    try:
        with open(transcript_path, encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []

    rows = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(row, dict):
            rows.append(row)
    return rows


def last_agent_message(rows: list[dict]) -> str:
    """The final user-facing text of the turn, or empty when there is none.

    Two transcript shapes, both JSONL, read back to front:

    Claude Code
        {"type": "assistant", "message": {"content": [{"type": "text", ...}]}}
        A turn can end with text after tool calls, so the blocks are searched
        backwards too.

    Copilot CLI
        {"type": "assistant.message", "data": {"content": "..."}}
        `content` is empty on a message that was only tool calls, which is why
        emptiness is what disqualifies a row rather than the absence of tools.
    """
    for row in reversed(rows):
        kind = row.get("type")

        if kind == "assistant":
            content = (row.get("message") or {}).get("content") or []
            if isinstance(content, str):
                if content.strip():
                    return content
                continue
            for block in reversed(content):
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and (block.get("text") or "").strip():
                    return block["text"]

        elif kind == "assistant.message":
            content = (row.get("data") or {}).get("content")
            if isinstance(content, str) and content.strip():
                return content

    return ""


def finished_turn_message(payload: dict, rows: list[dict]) -> str:
    """The message of the turn that just ended.

    Taken from the payload, which carries it directly, and only from the
    transcript when the payload does not.

    The transcript is a lagging record, which is the whole reason for the
    preference. At the moment a turn ends it has not been written with that
    turn's final message yet, so reading it back to front finds the *previous*
    turn's, and every announcement is one turn late. In a session's first turn
    it finds nothing at all. Neither failure is loud: an extract of the previous
    turn is still plausible English about the same project, and the first turn
    just goes quiet.

    Reading through `payload_field` covers the camelCase alias, which is how
    Copilot's `agentStop` sends the same fields. The fallback covers an agent or
    a version that does not send the message at all -- for those this behaves
    exactly as it did before, rather than not at all.
    """
    handed_over = payload_field(payload, "last_assistant_message", "lastAssistantMessage")
    return handed_over or last_agent_message(rows)


def agent_name(rows: list[dict]) -> str:
    """Which agent produced this transcript, named the way it is spoken."""
    configured = os.environ.get("MURMLY_ANNOUNCE_AGENT", "").strip()
    if configured:
        return configured
    for row in rows:
        if row.get("type") == "assistant.message":
            return "Copilot"
        if row.get("type") == "assistant":
            return "Claude Code"
    return "The agent"


# ------------------------------------------------------------ what to say ---


#: A fenced block, matched exactly as `plain_text` matches it so that what is
#: hidden from the search is what would have been stripped from the speech.
FENCED_BLOCK = re.compile(r"```.*?```", re.DOTALL)
#: A marked passage: an opening element with no attributes, its content, and a
#: closing element. Non-greedy, so two passages are two matches rather than one
#: spanning both.
VOICE_NOTE = re.compile(r"<voice-note>(.*?)</voice-note>", re.IGNORECASE | re.DOTALL)
#: Either half of the element on its own, for the opener that was never closed.
VOICE_NOTE_MARKER = re.compile(r"</?voice-note>", re.IGNORECASE)
#: A sentence terminator that ends a sentence, rather than one inside "e.g.".
#: The same test `executive_summary` splits on.
SENTENCE_END = re.compile(r"[.!?](?=\s|$)")


def plain_text(text: str) -> str:
    """Strip what is noise once spoken.

    Fenced code, tables, inline code, links, headings, list markers, emphasis.
    Tables especially: they read as a wall of pipes.
    """
    text = re.sub(r"```.*?```", " ", text, flags=re.DOTALL)
    text = re.sub(r"^\s*\|.*$", " ", text, flags=re.MULTILINE)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"^\s*\d+\.\s+", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*([^*]+)\*\*", r"\1", text)
    text = re.sub(r"\*([^*]+)\*", r"\1", text)
    return re.sub(r"\s+", " ", text).strip()


def executive_summary(text: str) -> str:
    """Enough of the message to stand on its own, and no more.

    Whole sentences with their terminators, so the voice falls at the end of
    each. A message often opens with a fragment -- "Fixed." -- which says
    nothing alone, so a short opener takes the sentence after it. What is kept
    is the account of the outcome; the detail behind it is what the screen is
    for.
    """
    text = plain_text(text)
    if not text:
        return ""

    sentences = [s.strip() for s in re.findall(r".+?(?:[.!?](?=\s|$)|$)", text) if s.strip()]
    if not sentences:
        return ""

    if len(sentences[0]) < MIN_OPENER_CHARACTERS and len(sentences) > 1:
        sentences[:2] = [f"{sentences[0]} {sentences[1]}"]

    spoken = ""
    for sentence in sentences[:MAX_SUMMARY_SENTENCES]:
        candidate = f"{spoken} {sentence}".strip()
        if spoken and len(candidate) > MAX_SUMMARY_CHARACTERS:
            break
        spoken = candidate

    if len(spoken) > MAX_SUMMARY_CHARACTERS:
        spoken = spoken[:MAX_SUMMARY_CHARACTERS].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return spoken


def voice_notes(text: str) -> list[str]:
    """Every passage the agent marked to be heard, in the order it wrote them.

    Fenced code is removed before the search. Without that, a turn spent
    discussing this convention announces its own example instead of its note,
    which is not a corner case: it is every turn that builds or documents it.
    The cost is that a marked passage cannot contain a fence, which it should
    not anyway -- `plain_text` would drop the fence a moment later.

    A list rather than a joined string, because a message with one empty element
    and a message with no element mean opposite things.
    """
    return VOICE_NOTE.findall(FENCED_BLOCK.sub(" ", text))


def spoken_voice_note(text: str) -> str:
    """A marked passage as the agent wrote it, stopped rather than shaped.

    No sentence count and no joining of a short opener: those are an extract's
    heuristics for where an opening ends, and a passage authored to be heard has
    no such point. The bound is only a stop, so over it the passage ends at the
    last sentence terminator that fits, and with none to end at it falls back to
    the word boundary an over-long extract already uses.
    """
    text = plain_text(text)
    if len(text) <= MAX_VOICE_NOTE_CHARACTERS:
        return text

    head = text[:MAX_VOICE_NOTE_CHARACTERS]
    terminators = [match.end() for match in SENTENCE_END.finditer(head)]
    if terminators:
        return head[: terminators[-1]]
    return head.rsplit(" ", 1)[0].rstrip(",;:") + "."


def announcement(text: str) -> tuple[str, str]:
    """What to announce for this message, and which of the three it is.

    Three outcomes rather than two:

    A marked passage with text in it is announced as written. Several are
    announced as one, in the order they appear -- an agent that wrote two of
    them meant both, and announcing one silently discards what it said.

    A marked passage with nothing in it announces nothing, and does not fall
    back. An agent that wrote the element knows the convention, so an empty one
    is a decision that the turn is not worth interrupting for; extracting
    instead would override the only instruction it gave.

    No element -- or an opener that was never closed, which is a truncated
    message rather than a decision -- is extracted exactly as it was before
    marked passages existed. The stray markers go first: `plain_text` strips
    markdown, not elements, so they would otherwise be read out as words.
    """
    passages = voice_notes(text)
    if passages:
        spoken = spoken_voice_note("\n\n".join(passages))
        return (spoken, SOURCE_VOICE_NOTE) if spoken else ("", SOURCE_SUPPRESSED)
    return executive_summary(VOICE_NOTE_MARKER.sub(" ", text)), SOURCE_SUMMARY


def git_branch(directory: str) -> str:
    if not directory:
        return ""
    try:
        finished = subprocess.run(
            ["git", "-C", directory, "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    if finished.returncode != 0:
        return ""
    branch = finished.stdout.strip()
    return "" if branch in {"", "HEAD"} else branch


def session_sentence(agent: str, directory: str) -> str:
    """One short sentence placing the announcement: who, where, on what."""
    project = os.path.basename(directory.rstrip("/")) if directory else ""
    if not project:
        return f"{agent} has finished."
    branch = git_branch(directory)
    if branch:
        return f"{agent} in {project}, on branch {branch}."
    return f"{agent} in {project}."


# --------------------------------------------------------------- the notes --


def chime_wav() -> bytes:
    """Three rising notes as a WAV, built rather than shipped.

    Each note is a sine with a soft second harmonic for brightness, under a
    short attack and an exponential decay. The envelope is not decoration: a
    raw sine that starts and stops at full amplitude clicks at both ends, and
    three clicks is not an alert, it is a fault.
    """
    frames = bytearray()
    note_frames = int(CHIME_RATE_HZ * CHIME_NOTE_SECONDS)
    attack_frames = max(int(CHIME_RATE_HZ * CHIME_ATTACK_SECONDS), 1)

    for frequency in CHIME_NOTES_HZ:
        for index in range(note_frames):
            seconds = index / CHIME_RATE_HZ
            if index < attack_frames:
                envelope = index / attack_frames
            else:
                envelope = math.exp(-6.0 * (index - attack_frames) / note_frames)
            angle = 2.0 * math.pi * frequency * seconds
            sample = math.sin(angle) + 0.22 * math.sin(2.0 * angle)
            value = int(max(-1.0, min(1.0, sample * CHIME_PEAK * envelope)) * 32_767)
            frames += struct.pack("<h", value)

    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(CHIME_RATE_HZ)
        handle.writeframes(bytes(frames))
    return buffer.getvalue()


def chime_override_path() -> str:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local/share")
    return os.path.join(data_home, "murmly", "announce-chime.wav")


def play_chime() -> str:
    """Play the notes and wait for them to finish. Returns what happened.

    Waited for rather than overlapped: the notes exist to say that words are
    coming, which they cannot do underneath the words.
    """
    if os.environ.get("MURMLY_ANNOUNCE_CHIME", "1") == "0":
        return "chime disabled"

    player = next((p for p in CHIME_PLAYERS if shutil.which(p[0])), None)
    if player is None:
        return "no audio player for the chime"

    override = chime_override_path()
    if os.path.isfile(override):
        return _run_player(player, override, "chime (from " + override + ")")

    try:
        handle = tempfile.NamedTemporaryFile(prefix="murmly-chime-", suffix=".wav", delete=False)
        with handle:
            handle.write(chime_wav())
    except OSError as error:
        return f"chime not written: {error}"
    try:
        return _run_player(player, handle.name, "chime")
    finally:
        try:
            os.unlink(handle.name)
        except OSError:
            pass


def _run_player(player: tuple[str, ...], path: str, label: str) -> str:
    try:
        finished = subprocess.run(
            [*player, path],
            capture_output=True,
            timeout=CHIME_NOTE_SECONDS * len(CHIME_NOTES_HZ) + 5.0,
        )
    except (OSError, subprocess.SubprocessError) as error:
        return f"{label} failed: {error}"
    if finished.returncode != 0:
        return f"{label} failed: {player[0]} exited {finished.returncode}"
    return label


# ------------------------------------------------------------- the speaking --


class Session:
    """One speech session on the command socket."""

    def __init__(self) -> None:
        self._connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._payload = b""

    def __enter__(self) -> "Session":
        return self

    def __exit__(self, *_exception: object) -> None:
        try:
            self._connection.close()
        except OSError:
            pass

    def declare(self) -> str:
        """Open the session, or say why not. Empty means it is open."""
        self._connection.settimeout(CONNECT_TIMEOUT_SECONDS)
        try:
            self._connection.connect(SOCKET_PATH)
        except OSError as error:
            return f"no daemon: {error}"
        try:
            self._connection.sendall(b'{"command": "speech_session"}\n')
        except OSError as error:
            return f"connection lost: {error}"
        answer = self.read(CONNECT_TIMEOUT_SECONDS)
        if answer is None:
            return "no answer to the declaration"
        if not answer.get("ok"):
            return f"refused: {answer.get('code')}"
        return ""

    def read(self, timeout: float) -> dict | None:
        self._connection.settimeout(timeout)
        while b"\n" not in self._payload:
            try:
                chunk = self._connection.recv(4096)
            except (socket.timeout, OSError):
                return None
            if not chunk:
                return None
            self._payload += chunk
        line, _, rest = self._payload.partition(b"\n")
        self._payload = rest
        try:
            return json.loads(line.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None

    def speak(self, name: str, text: str) -> None:
        frame = json.dumps({"command": "speak", "name": name, "text": text}) + "\n"
        self._connection.sendall(frame.encode("utf-8"))

    def end(self) -> None:
        self._connection.sendall(b'{"command": "end"}\n')

    def wait_until_heard(self) -> str:
        """Hold the connection until it has been heard, and say how it ended.

        Closing stops speech and discards the queue, so returning early would
        cut off the announcement this hook exists to make.
        """
        remaining = HEARD_ALL_TIMEOUT_SECONDS
        while remaining > 0:
            frame = self.read(min(1.0, remaining))
            remaining -= 1.0
            if frame is None:
                continue
            event = frame.get("event")
            if event == "heard_all":
                return "spoken"
            if event == "interrupted":
                return "interrupted by the person"
            if event == "failed":
                return f"failed: {frame.get('error')}"
            if event == "shutting_down":
                return "daemon shutting down"
        return "gave up waiting to be heard"


def announce(context: str, spoken: str, source: str) -> str:
    """Chime, then speak, and wait for it. Returns why it stopped.

    The session is declared before the notes sound. Speech is refused for
    several ordinary reasons -- disabled, in use, a capture running -- and a
    chime with no announcement behind it is worse than silence.

    `source` names the second frame. An interruption event names the piece it
    cut off and may not carry the text, so the name is where the difference
    between a passage the agent wrote and an extract of its message shows up.
    """
    with Session() as session:
        refusal = session.declare()
        if refusal:
            return refusal

        note(f"  {play_chime()}")

        try:
            session.speak("context", context)
            session.speak(source, spoken)
            session.end()
        except OSError as error:
            return f"connection lost: {error}"
        return session.wait_until_heard()


# ------------------------------------------------------------------- main ---


def detach() -> bool:
    """Put the work in a process of its own. True in the child.

    Claude Code runs this hook asynchronously; Copilot's documentation does not
    promise that, and a turn that waits out an announcement is worse than one
    that is never announced. `setsid` is what keeps the child alive when the
    agent exits immediately after the turn, as a non-interactive run does.
    """
    if os.environ.get("MURMLY_ANNOUNCE_FOREGROUND") == "1":
        return True
    try:
        if os.fork() > 0:
            return False
    except OSError:
        return True
    try:
        os.setsid()
    except OSError:
        pass
    devnull = os.open(os.devnull, os.O_RDWR)
    for descriptor in (0, 1, 2):
        try:
            os.dup2(devnull, descriptor)
        except OSError:
            pass
    if devnull > 2:
        os.close(devnull)
    return True


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:  # noqa: BLE001 - a malformed payload is not worth a failure
        payload = {}
    if not isinstance(payload, dict):
        payload = {}

    # The transcript is no longer what the announcement is made from, so its
    # absence is empty rows rather than a reason to say nothing. `agent_name`
    # is the only thing still reading it.
    transcript = payload_field(payload, "transcript_path", "transcriptPath")
    rows = transcript_rows(transcript) if transcript else []

    message = finished_turn_message(payload, rows)
    if not message:
        note("no message in the payload and none in the transcript")
        return 0

    spoken, source = announcement(message)
    if source == SOURCE_SUPPRESSED:
        note("suppressed by the agent: an empty voice note")
        return 0
    if not spoken:
        note("nothing worth saying")
        return 0

    agent = agent_name(rows)
    context = session_sentence(agent, payload_field(payload, "cwd"))

    if not detach():
        return 0

    note(f"saying {SOURCE_LABELS[source]}: {context} | {spoken}")
    note(f"  -> {announce(context, spoken, source)}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as error:  # noqa: BLE001 - never fail the turn
        note(f"unexpected: {error}")
        sys.exit(0)
