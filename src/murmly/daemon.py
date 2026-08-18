from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import logging
import socket
import threading
import time

from murmly.audio import SoundDeviceRecorder
from murmly.config import MurmlyConfig
from murmly.focus import (
    FocusObserver,
    WindowIdentity,
    create_focus_observer,
    record_target,
    should_deliver,
)
from murmly.integrations import ClipboardPaster
from murmly.overlay import (
    detect_overlay_backend,
    NullOverlayController,
    OverlayController,
    OverlayLifecycle,
    OverlayState,
)
from murmly.silence import SilenceDetector
from murmly.stt import FasterWhisperTranscriber


logger = logging.getLogger(__name__)
MAX_COMMAND_BYTES = 4_096
MAX_COMMAND_WORKERS = 8
COMMAND_TIMEOUT_SECONDS = 2.0
LIVE_WORKER_JOIN_SECONDS = 0.5
MIN_LIVE_TICK_SECONDS = 0.05
SILENCE_TICK_SECONDS = 0.25
MAX_SILENCE_TICK_SECONDS = 1.0
MAX_TICK_FAILURES = 5


@dataclass(slots=True)
class ProcessingResult:
    text: str
    state: str
    delivered: bool = False
    detail: str | None = None


class SpeechSession:
    def __init__(
        self,
        config: MurmlyConfig,
        level_sink=None,
        focus_observer: FocusObserver | None = None,
        partial_sink: Callable[[str], None] | None = None,
        on_silence: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._config = config
        self._recorder = SoundDeviceRecorder(config, level_sink=level_sink)
        self._transcriber = FasterWhisperTranscriber(config)
        self._paster: ClipboardPaster | None = None
        self._focus = focus_observer if focus_observer is not None else create_focus_observer()
        self._partial_sink = partial_sink
        self._on_silence = on_silence
        self._clock = clock
        self._silence: SilenceDetector | None = None
        self._live_stop = threading.Event()
        self._live_threads: list[threading.Thread] = []
        self._delivery_lock = threading.Lock()

    @property
    def focus_observer(self) -> FocusObserver:
        return self._focus

    def start_recording(self) -> None:
        self._recorder.start()
        try:
            self._transcriber.begin_capture()
            self._silence = self._create_silence_detector()
            self._start_live_worker()
        except Exception:
            # The stream is already open. Without this the daemon reports failure
            # and stays IDLE while the microphone stays live, and the next toggle
            # orphans it by overwriting the handle.
            try:
                self._recorder.stop()
            except Exception as stop_error:
                logger.warning("Unable to close capture after a failed start: %s", stop_error)
            raise

    def stop_recording(self) -> bytes:
        # Refuse new passes first: a tick already past its wait would otherwise
        # start a full decode during the join and hold the model lock, delaying
        # the final transcription by a whole pass whose result is discarded.
        self._transcriber.stop_partials()
        self._stop_live_worker()
        return self._recorder.stop()

    def take_segment(self) -> bytes:
        """Close the current segment and start speech tracking over for the next."""
        segment = self._recorder.take_segment()
        if self._silence is not None:
            self._silence.reset()
        return segment

    def capture_delivery_target(self) -> WindowIdentity | None:
        return record_target(self._focus)

    def process_recording(
        self,
        pcm_audio: bytes,
        target: WindowIdentity | None = None,
    ) -> ProcessingResult:
        text = self._transcriber.transcribe_pcm16(pcm_audio, self._recorder.sample_rate_hz)
        if not text:
            return ProcessingResult(text=text, state="DONE")
        # Held across delivery because clipboard restoration runs inside
        # copy_and_paste: without it a restore could overwrite the next
        # segment's transcript before that segment is pasted.
        with self._delivery_lock:
            allowed, reason = should_deliver(self._focus, target, self._config.verify_target)
            if allowed:
                self._ensure_paster().copy_and_paste(text)
                return ProcessingResult(text=text, state="DONE", delivered=True)
            logger.warning("Transcript delivery refused: %s", reason)
            self._ensure_paster().copy(text)
        return ProcessingResult(
            text=text,
            state="DONE",
            delivered=False,
            detail="Transcript copied to the clipboard but not pasted.",
        )

    def _create_silence_detector(self) -> SilenceDetector | None:
        if self._config.auto_transcribe == "off":
            return None
        detector = SilenceDetector(
            self._recorder.sample_rate_hz,
            self._config.channels,
            silence_ms=self._config.auto_transcribe_silence_ms,
            min_speech_ms=self._config.auto_transcribe_min_speech_ms,
        )
        if not detector.available:
            logger.warning("Auto-transcribe disabled for this session: %s", detector.unavailable_reason)
        return detector

    def _start_live_worker(self) -> None:
        wants_partials = self._config.live_transcribe
        wants_silence = self._silence is not None and self._silence.available
        self._live_stop = threading.Event()
        self._live_threads = []
        if wants_silence:
            # Its own thread, not a shared tick: a partial pass takes hundreds of
            # milliseconds on CUDA and seconds on CPU, and running both on one
            # thread would black out silence detection for that whole time --
            # which is the defect that let recordings run through pauses.
            self._spawn_live_thread(
                self._run_silence_loop,
                "murmly-silence",
                self._silence_interval(),
            )
        if wants_partials:
            self._spawn_live_thread(
                self._run_partial_loop,
                "murmly-partial",
                max(self._config.live_interval_ms / 1_000, MIN_LIVE_TICK_SECONDS),
            )

    def _silence_interval(self) -> float:
        """How often to re-measure the trailing window.

        Each tick re-runs the voice activity model over the whole window, so a
        fixed rate would recompute a 30 s window four times a second. Scaling with
        the window keeps the cost flat while staying far below the threshold the
        reading has to detect.
        """
        window = self._silence.window_seconds if self._silence is not None else 0.0
        return min(max(SILENCE_TICK_SECONDS, window / 40), MAX_SILENCE_TICK_SECONDS)

    def _spawn_live_thread(self, loop, name: str, interval: float) -> None:
        thread = threading.Thread(
            target=loop,
            args=(self._live_stop, interval),
            name=name,
            daemon=True,
        )
        try:
            thread.start()
        except RuntimeError as error:
            logger.warning("Live worker %s did not start: %s", name, error)
            return
        self._live_threads.append(thread)

    def _stop_live_worker(self) -> None:
        threads = list(self._live_threads)
        self._live_stop.set()
        for thread in threads:
            if thread is threading.current_thread():
                continue
            thread.join(timeout=LIVE_WORKER_JOIN_SECONDS)
            if thread.is_alive():
                # A pass already inside the engine cannot be interrupted. Its
                # result is discarded by the transcriber, so the final pass
                # proceeds now rather than waiting on it.
                logger.debug("Live worker %s did not exit within the join timeout.", thread.name)
        self._live_threads = [
            thread for thread in threads if thread is threading.current_thread() and thread.is_alive()
        ]

    def _run_silence_loop(self, stop_event: threading.Event, interval: float) -> None:
        self._run_tick_loop(stop_event, interval, self._silence_tick, "silence")

    def _run_partial_loop(self, stop_event: threading.Event, interval: float) -> None:
        self._run_tick_loop(stop_event, interval, self._partial_tick, "partial")

    def _run_tick_loop(self, stop_event: threading.Event, interval: float, tick, label: str) -> None:
        # Per loop, not shared: a healthy loop resetting the counter would hide a
        # neighbour failing every tick, and a broken one could kill a healthy one.
        failures = 0
        next_tick = self._clock() + interval
        while True:
            delay = next_tick - self._clock()
            if stop_event.wait(delay if delay > 0 else MIN_LIVE_TICK_SECONDS):
                return
            try:
                tick()
                failures = 0
            except Exception as error:
                failures += 1
                logger.warning(
                    "Live worker %s tick failed (%d/%d): %s",
                    label,
                    failures,
                    MAX_TICK_FAILURES,
                    error,
                )
                if failures >= MAX_TICK_FAILURES:
                    return
            # Advance against the time the tick finished: a slow tick would
            # otherwise leave the schedule in the past and fire a catch-up burst.
            after = self._clock()
            while next_tick <= after:
                next_tick += interval

    def _silence_tick(self) -> None:
        if self._silence is None or not self._silence.available:
            return
        reading = self._silence.observe(self._recorder.snapshot(self._silence.window_seconds))
        if reading.triggered and self._on_silence is not None:
            self._on_silence()

    def _partial_tick(self) -> None:
        if not self._config.live_transcribe or not self._transcriber.partials_available:
            return
        audio = self._recorder.snapshot(self._config.live_window_seconds)
        text = self._transcriber.transcribe_partial(audio, self._recorder.sample_rate_hz)
        if text is not None and self._partial_sink is not None:
            self._partial_sink(text)

    def _ensure_paster(self) -> ClipboardPaster:
        if self._paster is None:
            self._paster = ClipboardPaster(
                restore_clipboard=self._config.restore_clipboard,
                restore_delay_ms=self._config.restore_clipboard_delay_ms,
            )
        return self._paster


class MurmlyDaemon:
    def __init__(
        self,
        config: MurmlyConfig,
        session: SpeechSession | None = None,
        overlay: OverlayLifecycle | None = None,
    ) -> None:
        self._config = config
        self._overlay = overlay or self._create_overlay(config)
        self._session = session or SpeechSession(
            config,
            level_sink=self._publish_level,
            partial_sink=self._publish_partial,
            on_silence=self._on_silence,
        )
        self._segments: list[str] = []
        self._session_delivered = True
        # Held for the whole produce-and-deliver of one unit of audio, by both the
        # live worker and the toggle path. Mutual exclusion here is what keeps
        # segment order, `_segments`, and `_session_delivered` consistent.
        self._unit_lock = threading.Lock()
        self._segment_thread: threading.Thread | None = None
        self._partial_sink_owner = self._publish_partial
        self._state = "IDLE"
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._server: socket.socket | None = None
        self._worker_slots = threading.BoundedSemaphore(MAX_COMMAND_WORKERS)
        self._connections_lock = threading.Lock()
        self._connections: set[socket.socket] = set()

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def serve_forever(self) -> None:
        self._config.socket_path.parent.mkdir(parents=True, exist_ok=True)
        self._config.socket_path.unlink(missing_ok=True)
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
                self._server = server
                server.bind(str(self._config.socket_path))
                server.listen()
                server.settimeout(0.2)
                while not self._shutdown.is_set():
                    try:
                        connection, _address = server.accept()
                    except socket.timeout:
                        continue
                    except OSError:
                        if self._shutdown.is_set():
                            break
                        raise
                    self._dispatch_connection(connection)
        finally:
            self._server = None
            self._config.socket_path.unlink(missing_ok=True)
            self._close_overlay()

    def shutdown(self) -> None:
        self._shutdown.set()
        server = self._server
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        with self._connections_lock:
            connections = tuple(self._connections)
        for connection in connections:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        self._config.socket_path.unlink(missing_ok=True)
        self._close_overlay()

    def _serve_connection(self, connection: socket.socket) -> None:
        try:
            with connection:
                connection.settimeout(COMMAND_TIMEOUT_SECONDS)
                response = self._handle_connection(connection)
                try:
                    connection.sendall((json.dumps(response) + "\n").encode("utf-8"))
                except (BrokenPipeError, OSError):
                    if not self._shutdown.is_set():
                        raise
        except (json.JSONDecodeError, socket.timeout, UnicodeDecodeError, ValueError):
            pass
        except OSError as error:
            if not self._shutdown.is_set():
                logger.warning("Command connection failed: %s", error)
        finally:
            with self._connections_lock:
                self._connections.discard(connection)
            self._worker_slots.release()

    def _dispatch_connection(self, connection: socket.socket) -> bool:
        if not self._worker_slots.acquire(blocking=False):
            connection.close()
            return False
        started = False
        try:
            with self._connections_lock:
                if self._shutdown.is_set():
                    return False
                self._connections.add(connection)
                thread = threading.Thread(
                    target=self._serve_connection,
                    args=(connection,),
                    name="murmly-command",
                    daemon=True,
                )
                try:
                    thread.start()
                except RuntimeError:
                    self._connections.discard(connection)
                    raise
                started = True
            return True
        except RuntimeError as error:
            logger.warning("Unable to start command worker: %s", error)
            return False
        finally:
            if not started:
                connection.close()
                self._worker_slots.release()

    def _handle_connection(self, connection: socket.socket) -> dict[str, object]:
        payload = bytearray()
        while not payload.endswith(b"\n"):
            chunk = connection.recv(4096)
            if not chunk:
                break
            if len(payload) + len(chunk) > MAX_COMMAND_BYTES:
                return {"ok": False, "error": "Command exceeds the 4096-byte limit."}
            payload.extend(chunk)
        command = json.loads(payload.decode("utf-8") or "{}")
        return self.handle_command(str(command.get("command", "")))

    def handle_command(self, command: str) -> dict[str, object]:
        if command == "status":
            return {"ok": True, "state": self.state}
        if command != "toggle":
            return {"ok": False, "error": f"Unsupported command: {command}"}

        with self._lock:
            if self._state == "IDLE":
                self._segments = []
                self._session_delivered = True
                try:
                    self._session.start_recording()
                except Exception as error:
                    self._publish_error()
                    return {"ok": False, "state": "IDLE", "error": str(error)}
                self._state = "LISTENING"
                state = self._state
                self._publish_state(OverlayState.LISTENING)
                return {"ok": True, "state": state}
            if self._state != "LISTENING":
                return {"ok": False, "state": self._state, "error": "Daemon is busy."}
            self._state = "THINKING"
        # Claimed before stopping capture so an in-flight segment finishes first;
        # otherwise its transcript could paste after this one, out of order.
        with self._unit_lock:
            try:
                pcm_audio = self._session.stop_recording()
            except Exception as error:
                with self._lock:
                    self._state = "IDLE"
                self._publish_error()
                return {"ok": False, "state": "IDLE", "error": str(error)}
            # Published only once capture has stopped: the processing presentation
            # must never be shown while the waveform still represents live input.
            self._publish_state(OverlayState.THINKING)
            target = self._session.capture_delivery_target()
            return self._finish_toggle(pcm_audio, target)

    def _finish_toggle(self, pcm_audio: bytes, target: WindowIdentity | None) -> dict[str, object]:

        try:
            result = self._session.process_recording(pcm_audio, target)
            if result.detail is None:
                self._publish_state(OverlayState.IDLE)
            else:
                self._publish_error()
            return self._session_response(result)
        except Exception as error:
            self._publish_error()
            return {"ok": False, "state": "IDLE", "error": str(error)}
        finally:
            with self._lock:
                self._state = "IDLE"

    def _session_response(self, result: ProcessingResult) -> dict[str, object]:
        """Report the whole session, keeping single-transcript responses identical."""
        with self._lock:
            segments = list(self._segments)
            session_delivered = self._session_delivered
        texts = [text for text in (*segments, result.text) if text]
        if segments:
            # A final segment that produced no text cannot make the session undelivered.
            delivered = session_delivered and (result.delivered or not result.text)
        else:
            delivered = result.delivered
        response: dict[str, object] = {
            "ok": True,
            "state": result.state,
            "text": " ".join(texts),
            "delivered": delivered,
        }
        if len(texts) > 1:
            response["segments"] = len(texts)
        detail = result.detail
        if detail is None and segments and not delivered:
            detail = "Transcript copied to the clipboard but not pasted."
        if detail is not None:
            response["detail"] = detail
        return response

    def _on_silence(self) -> None:
        mode = self._config.auto_transcribe
        if mode == "stop":
            self._begin_auto_stop()
        elif mode == "continuous":
            self._deliver_segment()

    def _claim_listening(self, *, advance: bool) -> bool:
        """Take the state lock without waiting, and only while still listening.

        Never blocks: a toggle already holding the lock is stopping this very
        recording, and it also joins this worker thread. Waiting here would stall
        that toggle for the whole join timeout, so an in-flight toggle simply wins.
        """
        if not self._lock.acquire(blocking=False):
            return False
        try:
            if self._state != "LISTENING":
                return False
            if advance:
                self._state = "THINKING"
            return True
        finally:
            self._lock.release()

    def _begin_auto_stop(self) -> None:
        # Claiming THINKING before releasing the lock means a toggle arriving now
        # is told the daemon is busy instead of starting a second stop.
        if not self._claim_listening(advance=True):
            return
        # Off the live worker thread: stopping capture joins that worker, and a
        # thread cannot join itself.
        self._start_transition(self._finish_auto_stop, "murmly-auto-stop")

    def _finish_auto_stop(self) -> None:
        try:
            pcm_audio = self._session.stop_recording()
            # Published only once capture has stopped, matching the toggle path:
            # the processing presentation must never be shown while the waveform
            # still represents live input.
            self._publish_state(OverlayState.THINKING)
            target = self._session.capture_delivery_target()
            result = self._session.process_recording(pcm_audio, target)
            if result.detail is None:
                self._publish_state(OverlayState.IDLE)
            else:
                self._publish_error()
        except Exception as error:
            logger.warning("Auto-transcribe stop failed: %s", error)
            self._publish_error()
        finally:
            with self._lock:
                self._state = "IDLE"

    def _deliver_segment(self) -> None:
        """Hand the segment to its own thread and return to polling.

        Decode plus paste plus the clipboard restore delay runs for seconds; on
        the silence thread that would black out detection for the whole time and
        swallow the next pause -- the defect the separate thread exists to avoid.
        """
        if self._segment_thread is not None and self._segment_thread.is_alive():
            return
        try:
            thread = threading.Thread(
                target=self._run_segment,
                name="murmly-segment",
                daemon=True,
            )
            thread.start()
        except RuntimeError as error:
            logger.warning("Unable to start segment delivery: %s", error)
            return
        self._segment_thread = thread

    def _run_segment(self) -> None:
        # Non-blocking: a toggle already holding this is ending the recording, so
        # it should win rather than queue behind another segment.
        if not self._unit_lock.acquire(blocking=False):
            return
        try:
            if not self._claim_listening(advance=False):
                return
            segment = self._session.take_segment()
            target = self._session.capture_delivery_target()
            try:
                result = self._session.process_recording(segment, target)
            except Exception as error:
                logger.warning("Segment transcription failed: %s", error)
                self._end_continuous_session()
                return
            if not result.text:
                return
            with self._lock:
                self._segments.append(result.text)
                if not result.delivered:
                    self._session_delivered = False
            # This text has been pasted; leaving it under the indicator would show
            # the user speech that is no longer pending.
            if self._partial_sink_owner is not None:
                self._partial_sink_owner("")
            if not result.delivered:
                # Claimed under the unit lock: releasing first lets a toggle take
                # the session over and deliver normally, leaving the user a
                # success overlay and a failure response for the same session.
                self._end_continuous_session()
        finally:
            self._unit_lock.release()

    def _end_continuous_session(self) -> None:
        if not self._claim_listening(advance=True):
            return
        self._start_transition(self._finish_continuous_session, "murmly-auto-end")

    def _start_transition(self, target, name: str) -> None:
        """Run a state transition off the live worker thread.

        The state is already THINKING by the time this is called, so a thread
        that never starts would leave the daemon wedged there with the microphone
        open and every later toggle answered "busy". Failing back to IDLE keeps
        the daemon usable.
        """
        try:
            threading.Thread(target=target, name=name, daemon=True).start()
        except RuntimeError as error:
            logger.warning("Unable to start %s: %s", name, error)
            try:
                self._session.stop_recording()
            except Exception as stop_error:
                logger.warning("Unable to stop capture after a failed transition: %s", stop_error)
            self._publish_error()
            with self._lock:
                self._state = "IDLE"

    def _finish_continuous_session(self) -> None:
        try:
            # The audio captured since the refused segment closed is discarded:
            # the session is ending because delivery was refused, so delivering
            # more of it would repeat the mistake.
            self._session.stop_recording()
        except Exception as error:
            logger.warning("Ending a continuous session failed: %s", error)
        finally:
            self._publish_error()
            with self._lock:
                self._state = "IDLE"

    @staticmethod
    def _create_overlay(config: MurmlyConfig) -> OverlayLifecycle:
        if not config.overlay_enabled:
            return NullOverlayController()
        backend = detect_overlay_backend()
        if backend is None:
            return NullOverlayController("Overlay requires KDE Plasma on X11 or Wayland.")
        return OverlayController(
            bottom_margin_px=config.overlay_bottom_margin_px,
            reduced_motion=config.overlay_reduced_motion,
            text_size_px=config.overlay_text_size_px,
            transcript_panel=config.live_transcribe,
            backend=backend,
        )

    def _publish_state(self, state: OverlayState) -> None:
        try:
            self._overlay.publish_state(state)
        except Exception as error:
            logger.warning("Overlay state update failed: %s", error)

    def _publish_level(self, level: float) -> None:
        try:
            self._overlay.publish_level(level)
        except Exception as error:
            logger.warning("Overlay level update failed: %s", error)

    def _publish_partial(self, text: str) -> None:
        try:
            self._overlay.publish_partial(text)
        except Exception as error:
            # Deliberately no transcript text in the log line.
            logger.warning("Overlay partial update failed: %s", error)

    def _publish_error(self) -> None:
        try:
            self._overlay.publish_error()
        except Exception as error:
            logger.warning("Overlay error update failed: %s", error)

    def _close_overlay(self) -> None:
        try:
            self._overlay.close()
        except Exception as error:
            logger.warning("Overlay shutdown failed: %s", error)


def send_command(socket_path: str, command: str) -> dict[str, object]:
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
        client.connect(socket_path)
        client.sendall((json.dumps({"command": command}) + "\n").encode("utf-8"))
        payload = b""
        while not payload.endswith(b"\n"):
            chunk = client.recv(4096)
            if not chunk:
                break
            payload += chunk
    if not payload:
        raise RuntimeError("Murmly daemon closed the connection before responding.")
    return json.loads(payload.decode("utf-8"))
