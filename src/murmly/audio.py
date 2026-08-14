from __future__ import annotations

import time

from murmly.config import MurmlyConfig


class SoundDeviceRecorder:
    def __init__(self, config: MurmlyConfig) -> None:
        self._config = config
        self._stream = None
        self._buffer = bytearray()

    def start(self) -> None:
        try:
            import sounddevice as sd
        except ModuleNotFoundError as error:
            raise RuntimeError(
                "sounddevice is required for microphone capture. Install it before starting murmly."
            ) from error

        self._buffer = bytearray()

        def callback(indata: bytes, frames: int, time_info: object, status: object) -> None:
            del frames, time_info
            if status:
                raise RuntimeError(f"Audio capture error: {status}")
            self._buffer.extend(bytes(indata))

        self._stream = sd.RawInputStream(
            samplerate=self._config.sample_rate_hz,
            channels=self._config.channels,
            dtype="int16",
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> bytes:
        if self._stream is None:
            return b""
        self._stream.stop()
        self._stream.close()
        self._stream = None
        return bytes(self._buffer)

    def record_for_seconds(self, seconds: float) -> bytes:
        self.start()
        time.sleep(seconds)
        return self.stop()
