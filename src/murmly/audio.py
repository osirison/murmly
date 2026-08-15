from __future__ import annotations

import time
from typing import Any

from murmly.config import MurmlyConfig


class SoundDeviceRecorder:
    def __init__(self, config: MurmlyConfig) -> None:
        self._config = config
        self._stream = None
        self._buffer = bytearray()
        self._sample_rate_hz = config.sample_rate_hz

    @property
    def sample_rate_hz(self) -> int:
        return self._sample_rate_hz

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

        failures: list[str] = []
        for device in self._candidate_devices(sd):
            for sample_rate_hz in self._candidate_sample_rates(sd, device):
                try:
                    sd.check_input_settings(
                        device=device,
                        channels=self._config.channels,
                        dtype="int16",
                        samplerate=sample_rate_hz,
                    )
                except (sd.PortAudioError, ValueError) as error:
                    failures.append(f"device={device!r}, rate={sample_rate_hz}: {error}")
                    continue

                stream = None
                try:
                    stream = sd.RawInputStream(
                        device=device,
                        samplerate=sample_rate_hz,
                        channels=self._config.channels,
                        dtype="int16",
                        callback=callback,
                    )
                    stream.start()
                except (sd.PortAudioError, ValueError) as error:
                    if stream is not None:
                        stream.close()
                    failures.append(f"device={device!r}, rate={sample_rate_hz}: {error}")
                    continue

                self._stream = stream
                self._sample_rate_hz = sample_rate_hz
                return

        details = "; ".join(failures) or "No input devices were available."
        raise RuntimeError(f"Unable to open a microphone input. {details}")

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

    def _candidate_devices(self, sounddevice: Any) -> list[int | None]:
        virtual_device_names = {"default", "pipewire", "pulse", "sysdefault"}
        candidates: list[int | None] = []
        try:
            default_device = sounddevice.query_devices(kind="input")
        except (TypeError, ValueError):
            default_device = None

        if default_device is not None:
            candidates.append(None)

        for index, device in enumerate(sounddevice.query_devices()):
            if int(device["max_input_channels"]) < self._config.channels:
                continue
            if str(device["name"]).casefold() in virtual_device_names:
                continue
            candidates.append(index)
        return candidates

    def _candidate_sample_rates(self, sounddevice: Any, device: int | None) -> list[int]:
        sample_rates = [self._config.sample_rate_hz]
        try:
            if device is None:
                properties = sounddevice.query_devices(kind="input")
            else:
                properties = sounddevice.query_devices(device)
            native_sample_rate_hz = int(properties["default_samplerate"])
        except (KeyError, TypeError, ValueError):
            return sample_rates

        if native_sample_rate_hz > 0 and native_sample_rate_hz not in sample_rates:
            sample_rates.append(native_sample_rate_hz)
        return sample_rates
