from __future__ import annotations

import os
import socket
import unittest
from unittest.mock import patch

from murmly.desktop import GnomeShortcuts, PlasmaShortcuts
from murmly.focus import X11FocusObserver
from murmly.installer import UserService
from murmly.integrations import WAYLAND_INJECTORS, X11_INJECTORS, choose_clipboard_copy_command
from murmly.overlay import OverlayController
from murmly.platform import (
    BACKEND_REGISTRIES,
    IN_PROCESS_HOTKEY_MECHANISMS,
    PERMISSIONS,
    RUNTIME_GAPS,
    TRANSCRIPTION_CAPABILITY,
    BackendCandidate,
    BackendRegistry,
    Desktop,
    OperatingSystem,
    Permission,
    PermissionState,
    PlatformProfile,
    RuntimeGap,
    _detected_libc,
    hotkey_mechanism_is_in_process,
    operating_system_for,
    resolve_platform,
    runtime_gaps_for,
    transcription_runtime_gap,
)
from murmly.tts import KokoroSynthesizer


def linux_plasma_x11() -> PlatformProfile:
    return PlatformProfile(
        operating_system=OperatingSystem.LINUX,
        architecture="x86_64",
        session_type="x11",
        x11_display=True,
        desktop=Desktop.PLASMA,
    )


def linux_plasma_wayland() -> PlatformProfile:
    return PlatformProfile(
        operating_system=OperatingSystem.LINUX,
        architecture="x86_64",
        session_type="wayland",
        wayland_display=True,
        desktop=Desktop.PLASMA,
    )


def linux_gnome_wayland() -> PlatformProfile:
    return PlatformProfile(
        operating_system=OperatingSystem.LINUX,
        architecture="x86_64",
        session_type="wayland",
        wayland_display=True,
        desktop=Desktop.GNOME,
    )


def linux_xfce_x11() -> PlatformProfile:
    """A desktop with no hotkey backend at all, distinct from `Desktop.OTHER`
    meaning "declared nothing": this one declared something Murmly simply does
    not register hotkeys on."""
    return PlatformProfile(
        operating_system=OperatingSystem.LINUX,
        architecture="x86_64",
        session_type="x11",
        x11_display=True,
        desktop=Desktop.OTHER,
    )


def windows() -> PlatformProfile:
    return PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="x86_64")


def macos() -> PlatformProfile:
    return PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="arm64")


class OperatingSystemMappingTests(unittest.TestCase):
    """The pure sys.platform -> OperatingSystem mapping, without patching sys.platform."""

    def test_linux_platform_strings_map_to_linux(self) -> None:
        self.assertEqual(OperatingSystem.LINUX, operating_system_for("linux"))

    def test_windows_platform_strings_map_to_windows(self) -> None:
        # "win32" is what CPython reports on every 32- and 64-bit Windows build;
        # there is no separate "win64".
        self.assertEqual(OperatingSystem.WINDOWS, operating_system_for("win32"))

    def test_darwin_maps_to_macos(self) -> None:
        self.assertEqual(OperatingSystem.MACOS, operating_system_for("darwin"))

    def test_anything_else_maps_to_other(self) -> None:
        self.assertEqual(OperatingSystem.OTHER, operating_system_for("freebsd13"))


class ResolvePlatformTests(unittest.TestCase):
    """Resolution answers for a supplied environment, not the process's own (18.2)."""

    def test_resolves_the_real_operating_system_and_architecture(self) -> None:
        # This suite runs on Linux; resolving with no environment override
        # must still name the operating system this process is actually on,
        # confirming resolve_platform reads sys.platform rather than a stub.
        profile = resolve_platform({})
        self.assertEqual(OperatingSystem.LINUX, profile.operating_system)
        self.assertTrue(profile.architecture)

    def test_a_supplied_environment_decides_the_session_reading(self) -> None:
        """The answer does not depend on the environment Murmly is actually
        running in (spec.md, "A supplied environment decides the resolution").

        Proved by conflict: the process's real `os.environ` is patched to say
        one thing, and a supplied mapping saying the opposite is handed to
        `resolve_platform` directly. If the process environment leaked in at
        all, this would report GNOME and X11 instead of Plasma and Wayland.
        """
        supplied = {
            "XDG_SESSION_TYPE": "wayland",
            "WAYLAND_DISPLAY": "wayland-0",
            "XDG_CURRENT_DESKTOP": "KDE",
        }

        with patch.dict(
            os.environ,
            {"XDG_SESSION_TYPE": "x11", "DISPLAY": ":0", "XDG_CURRENT_DESKTOP": "GNOME"},
            clear=False,
        ):
            profile = resolve_platform(supplied)

        self.assertEqual("wayland", profile.session_type)
        self.assertTrue(profile.wayland_display)
        self.assertEqual(Desktop.PLASMA, profile.desktop)

    def test_no_supplied_environment_falls_back_to_the_process_environment(self) -> None:
        """The other half of task 1.2: `env=None` reads `os.environ`, not nothing."""
        with patch.dict(
            os.environ,
            {"XDG_SESSION_TYPE": "wayland", "WAYLAND_DISPLAY": "wayland-0", "XDG_CURRENT_DESKTOP": "KDE"},
            clear=False,
        ):
            profile = resolve_platform()

        self.assertEqual("wayland", profile.session_type)
        self.assertTrue(profile.wayland_display)
        self.assertEqual(Desktop.PLASMA, profile.desktop)

    def test_an_empty_environment_reports_no_desktop_or_display(self) -> None:
        profile = resolve_platform({})

        self.assertEqual("", profile.session_type)
        self.assertFalse(profile.wayland_display)
        self.assertFalse(profile.x11_display)
        self.assertEqual(Desktop.OTHER, profile.desktop)

    def test_a_combined_desktop_string_naming_both_prefers_plasma(self) -> None:
        profile = resolve_platform({"XDG_CURRENT_DESKTOP": "KDE:GNOME"})

        self.assertEqual(Desktop.PLASMA, profile.desktop)

    def test_gnome_is_recognised_on_its_own(self) -> None:
        profile = resolve_platform({"XDG_CURRENT_DESKTOP": "GNOME"})

        self.assertEqual(Desktop.GNOME, profile.desktop)


class PlatformProfileConstructionTests(unittest.TestCase):
    """PlatformProfile is a value tests construct directly (18.1)."""

    def test_a_profile_for_a_platform_this_machine_is_not_running_is_constructible(self) -> None:
        profile = windows()

        self.assertEqual(OperatingSystem.WINDOWS, profile.operating_system)
        self.assertFalse(profile.supported)

    def test_supported_reports_only_the_operating_systems_murmly_claims_today(self) -> None:
        self.assertTrue(linux_plasma_x11().supported)
        self.assertFalse(windows().supported)
        self.assertFalse(macos().supported)


class DetectedLibcTests(unittest.TestCase):
    """musl is confirmed, not left unreachable behind a signature not every
    supported interpreter's `libc_ver()` reports (`_detected_libc`, which
    `resolve_platform` calls for every Linux profile and
    `transcription_runtime_gap` depends on to refuse a real musl machine, not
    only one a test constructs directly)."""

    def test_glibc_is_read_straight_from_libc_ver(self) -> None:
        with patch("murmly.platform.stdlib_platform.libc_ver", return_value=("glibc", "2.38")):
            self.assertEqual("glibc", _detected_libc())

    def test_musl_named_by_libc_ver_is_trusted_without_the_filesystem_fallback(self) -> None:
        with (
            patch("murmly.platform.stdlib_platform.libc_ver", return_value=("musl", "1.2.4")),
            patch("murmly.platform._musl_loader_present", return_value=False) as loader_check,
        ):
            self.assertEqual("musl", _detected_libc())
            loader_check.assert_not_called()

    def test_musl_is_confirmed_by_the_loader_path_when_libc_ver_says_nothing(self) -> None:
        """The case an interpreter without the musl signature needs:
        `libc_ver()` reports `("", "")` there, so the dynamic linker's own
        fixed path is what tells `resolve_platform` this machine is musl, not
        glibc-by-default."""
        with (
            patch("murmly.platform.stdlib_platform.libc_ver", return_value=("", "")),
            patch("murmly.platform._musl_loader_present", return_value=True),
        ):
            self.assertEqual("musl", _detected_libc())

    def test_neither_signature_present_reports_unknown_rather_than_a_guess(self) -> None:
        with (
            patch("murmly.platform.stdlib_platform.libc_ver", return_value=("", "")),
            patch("murmly.platform._musl_loader_present", return_value=False),
        ):
            self.assertIsNone(_detected_libc())

    def test_resolve_platform_reports_musl_for_a_musl_machine(self) -> None:
        """End to end: a musl machine's own resolution -- not a profile a test
        built by hand -- is what `transcription_runtime_gap` has to see."""
        with (
            patch("murmly.platform.stdlib_platform.libc_ver", return_value=("", "")),
            patch("murmly.platform._musl_loader_present", return_value=True),
        ):
            profile = resolve_platform({})

        self.assertEqual(OperatingSystem.LINUX, profile.operating_system)
        self.assertEqual("musl", profile.libc)
        gap = transcription_runtime_gap(profile)
        self.assertIsNotNone(gap)
        self.assertIn("musl", gap.characteristic)


class BackendRegistryTests(unittest.TestCase):
    """Every backend registry, exercised for every platform, from this one machine (18.1)."""

    def test_every_registered_concern_is_present(self) -> None:
        self.assertEqual(
            {
                "command_channel",
                "service_management",
                "hotkey_registration",
                "clipboard",
                "paste_injection",
                "focus_observation",
                "overlay",
                "speech_synthesis",
            },
            set(BACKEND_REGISTRIES),
        )

    def test_command_channel_selects_the_unix_socket_on_linux(self) -> None:
        choice = BACKEND_REGISTRIES["command_channel"].select(linux_plasma_x11())

        self.assertEqual("unix-socket", choice.mechanism)
        self.assertTrue(choice.available)
        self.assertIs(socket.AF_UNIX, choice.load())

    def test_command_channel_has_no_backend_on_windows_or_macos(self) -> None:
        for profile in (windows(), macos()):
            with self.subTest(profile=profile.operating_system):
                choice = BACKEND_REGISTRIES["command_channel"].select(profile)

                self.assertFalse(choice.available)
                self.assertIsNone(choice.mechanism)
                self.assertIn(profile.operating_system.value, choice.reason)

    def test_service_management_selects_systemd_on_linux_only(self) -> None:
        choice = BACKEND_REGISTRIES["service_management"].select(linux_plasma_x11())

        self.assertEqual("systemd", choice.mechanism)
        self.assertIs(UserService, choice.load())

        self.assertFalse(BACKEND_REGISTRIES["service_management"].select(windows()).available)

    def test_hotkey_registration_selects_plasma_on_a_plasma_desktop(self) -> None:
        choice = BACKEND_REGISTRIES["hotkey_registration"].select(linux_plasma_wayland())

        self.assertEqual("plasma", choice.mechanism)
        self.assertIs(PlasmaShortcuts, choice.load())

    def test_hotkey_registration_selects_gnome_on_a_gnome_desktop(self) -> None:
        choice = BACKEND_REGISTRIES["hotkey_registration"].select(linux_gnome_wayland())

        self.assertEqual("gnome", choice.mechanism)
        self.assertIs(GnomeShortcuts, choice.load())

    def test_hotkey_registration_names_the_desktops_own_limitation_elsewhere(self) -> None:
        # Task 4.6: distinct from the platform having no backend at all --
        # naming the desktops Murmly does support is what a person on this
        # desktop can act on, unlike "no hotkey backend for linux".
        choice = BACKEND_REGISTRIES["hotkey_registration"].select(linux_xfce_x11())

        self.assertFalse(choice.available)
        self.assertIn("KDE Plasma", choice.reason)
        self.assertIn("GNOME", choice.reason)

    def test_hotkey_registration_names_the_operating_system_on_windows(self) -> None:
        choice = BACKEND_REGISTRIES["hotkey_registration"].select(windows())

        self.assertFalse(choice.available)
        self.assertIn("windows", choice.reason)

    def test_clipboard_and_injection_follow_the_session_display_protocol(self) -> None:
        wayland = BACKEND_REGISTRIES["clipboard"].select(linux_plasma_wayland())
        x11 = BACKEND_REGISTRIES["clipboard"].select(linux_plasma_x11())

        self.assertEqual("wayland", wayland.mechanism)
        self.assertEqual("x11", x11.mechanism)
        self.assertIs(choose_clipboard_copy_command, wayland.load())
        self.assertIs(choose_clipboard_copy_command, x11.load())

        wayland_injection = BACKEND_REGISTRIES["paste_injection"].select(linux_plasma_wayland())
        x11_injection = BACKEND_REGISTRIES["paste_injection"].select(linux_plasma_x11())

        self.assertIs(WAYLAND_INJECTORS, wayland_injection.load())
        self.assertIs(X11_INJECTORS, x11_injection.load())

    def test_clipboard_and_injection_do_not_apply_off_linux(self) -> None:
        self.assertFalse(BACKEND_REGISTRIES["clipboard"].select(macos()).available)
        self.assertFalse(BACKEND_REGISTRIES["paste_injection"].select(macos()).available)

    def test_focus_observation_selects_x11_only_off_wayland(self) -> None:
        choice = BACKEND_REGISTRIES["focus_observation"].select(linux_plasma_x11())

        self.assertEqual("x11", choice.mechanism)
        self.assertIs(X11FocusObserver, choice.load())

        wayland_choice = BACKEND_REGISTRIES["focus_observation"].select(linux_plasma_wayland())
        self.assertFalse(wayland_choice.available)
        self.assertIn("X11", wayland_choice.reason)

    def test_overlay_selects_gtk4_only_with_a_plasma_display(self) -> None:
        choice = BACKEND_REGISTRIES["overlay"].select(linux_plasma_x11())

        self.assertEqual("gtk4", choice.mechanism)
        self.assertIs(OverlayController, choice.load())

    def test_overlay_has_no_backend_on_a_headless_plasma_session(self) -> None:
        """Plasma is confirmed present here; the reason must say the actual
        problem is the missing display, not send someone after KDE Plasma they
        already have (the two reasons this registry can give for Linux are
        required to differ, so a report built on top of this can distinguish
        them the way the spec's "mechanism exists but could not be used"
        scenario requires)."""
        headless = PlatformProfile(
            operating_system=OperatingSystem.LINUX,
            architecture="x86_64",
            desktop=Desktop.PLASMA,
        )
        choice = BACKEND_REGISTRIES["overlay"].select(headless)
        not_plasma_choice = BACKEND_REGISTRIES["overlay"].select(linux_gnome_wayland())

        self.assertFalse(choice.available)
        self.assertIn("display", choice.reason)
        self.assertNotIn("KDE Plasma", choice.reason)
        self.assertNotEqual(not_plasma_choice.reason, choice.reason)

        # 6.2: the mechanism exists here (Plasma is present) and could not be
        # used, so this is the one case in this registry that must name
        # something to fix -- as data (`remedy`), not by re-parsing `reason`.
        self.assertTrue(choice.remedy)
        # Whereas GNOME has no overlay backend at all: nothing exists to name
        # something to install for, so `remedy` must stay empty.
        self.assertEqual((), not_plasma_choice.remedy)

    def test_speech_synthesis_selects_kokoro_on_linux_only(self) -> None:
        choice = BACKEND_REGISTRIES["speech_synthesis"].select(linux_plasma_x11())

        self.assertEqual("kokoro", choice.mechanism)
        self.assertIs(KokoroSynthesizer, choice.load())
        self.assertFalse(BACKEND_REGISTRIES["speech_synthesis"].select(windows()).available)

    def test_every_registry_answers_for_every_constructed_platform_without_raising(self) -> None:
        """The exhaustive sweep across concerns and platforms task 18.1 asks for."""
        for profile in (
            linux_plasma_x11(),
            linux_plasma_wayland(),
            linux_gnome_wayland(),
            windows(),
            macos(),
        ):
            for concern, registry in BACKEND_REGISTRIES.items():
                with self.subTest(concern=concern, operating_system=profile.operating_system):
                    choice = registry.select(profile)
                    if choice.available:
                        self.assertIsNotNone(choice.load())
                    else:
                        self.assertTrue(choice.reason)


class InProcessHotkeyMarkerTests(unittest.TestCase):
    """Task 5.4's design boundary: which mechanisms need `hotkey_record.py`'s
    rebind-at-startup path, rather than the desktop's own persisted state."""

    def test_neither_real_hotkey_mechanism_is_in_process_today(self) -> None:
        self.assertFalse(hotkey_mechanism_is_in_process(linux_plasma_x11()))
        self.assertFalse(hotkey_mechanism_is_in_process(linux_gnome_wayland()))

    def test_the_real_table_is_empty(self) -> None:
        """Nothing populates this until a Windows or macOS backend exists
        (sections 8 and 13) -- an entry here today would claim a mechanism
        this change never built."""
        self.assertEqual(frozenset(), IN_PROCESS_HOTKEY_MECHANISMS)

    def test_an_injected_in_process_mechanism_is_recognised(self) -> None:
        """The branch a future Windows or macOS candidate exercises, proven
        against a constructed registry rather than a stub in production code."""
        fake_registry = BackendRegistry(
            "hotkey registration",
            candidates=(BackendCandidate("windows-hotkey", lambda profile: True, lambda: object()),),
            unavailable_reason=lambda profile: "unreachable",
        )

        self.assertTrue(
            hotkey_mechanism_is_in_process(
                windows(), registry=fake_registry, in_process=frozenset({"windows-hotkey"})
            )
        )

    def test_a_mechanism_outside_the_injected_table_is_not_in_process(self) -> None:
        self.assertFalse(
            hotkey_mechanism_is_in_process(linux_plasma_x11(), in_process=frozenset({"windows-hotkey"}))
        )


class BackendChoiceRemedyTests(unittest.TestCase):
    """6.2: `remedy` is the data that distinguishes "no mechanism" from
    "exists but could not be used", not `reason`'s wording."""

    def test_remedy_defaults_to_empty(self) -> None:
        """A registry that never names anything to install -- every one of
        them except `OVERLAY` -- need not construct an unused remedy."""
        choice = BACKEND_REGISTRIES["command_channel"].select(windows())

        self.assertFalse(choice.available)
        self.assertEqual((), choice.remedy)

    def test_a_registry_can_be_constructed_with_no_remedy_callable_at_all(self) -> None:
        registry = BackendRegistry(
            "test concern",
            candidates=(),
            unavailable_reason=lambda profile: "no mechanism for this test concern",
        )

        self.assertEqual((), registry.select(windows()).remedy)


class PermissionShapeTests(unittest.TestCase):
    """6.4: the reporting shape three future permission checks plug into,
    proved against a constructed `Permission` since none is registered yet."""

    def test_the_real_table_is_empty(self) -> None:
        """Nothing populates this until a permission-gated backend exists
        (Windows privacy settings, section 9; macOS grants, sections 12, 14)
        -- an entry here today would claim a check this change never built."""
        self.assertEqual({}, dict(PERMISSIONS))

    def test_a_platform_offering_no_way_to_ask_answers_undetermined(self) -> None:
        cannot_tell = Permission(
            name="test-permission",
            capability="test capability",
            grant_location="Settings > Test",
            check=lambda profile: PermissionState.UNDETERMINED,
        )

        self.assertEqual(PermissionState.UNDETERMINED, cannot_tell.check(linux_plasma_x11()))

    def test_granted_and_denied_are_distinct_from_undetermined(self) -> None:
        granted = Permission(
            name="test-permission",
            capability="test capability",
            grant_location="Settings > Test",
            check=lambda profile: PermissionState.GRANTED,
        )
        denied = Permission(
            name="test-permission",
            capability="test capability",
            grant_location="Settings > Test",
            check=lambda profile: PermissionState.DENIED,
        )

        self.assertEqual(PermissionState.GRANTED, granted.check(linux_plasma_x11()))
        self.assertEqual(PermissionState.DENIED, denied.check(linux_plasma_x11()))
        self.assertNotEqual(PermissionState.GRANTED, PermissionState.UNDETERMINED)
        self.assertNotEqual(PermissionState.DENIED, PermissionState.UNDETERMINED)


class RuntimeGapTests(unittest.TestCase):
    """The machine-capability check (1.8, 18.4)."""

    def test_musl_linux_has_no_transcription_runtime(self) -> None:
        profile = PlatformProfile(operating_system=OperatingSystem.LINUX, architecture="x86_64", libc="musl")

        gap = transcription_runtime_gap(profile)

        self.assertIsNotNone(gap)
        self.assertEqual("ctranslate2", gap.runtime)
        self.assertIn("musl", gap.characteristic)

    def test_windows_on_arm64_has_no_transcription_runtime(self) -> None:
        profile = PlatformProfile(operating_system=OperatingSystem.WINDOWS, architecture="arm64")

        gap = transcription_runtime_gap(profile)

        self.assertIsNotNone(gap)
        self.assertEqual("ctranslate2", gap.runtime)

    def test_intel_macos_has_no_transcription_runtime(self) -> None:
        profile = PlatformProfile(operating_system=OperatingSystem.MACOS, architecture="x86_64")

        gap = transcription_runtime_gap(profile)

        self.assertIsNotNone(gap)
        self.assertEqual("onnxruntime", gap.runtime)

    def test_an_architecture_alias_is_normalized_before_resolution_reaches_the_table(self) -> None:
        """`platform.machine()` spells this processor `AArch64` or `ARM64` depending on
        the OS; resolution folds either to the one spelling the table matches."""
        with patch("murmly.platform.stdlib_platform.machine", return_value="AArch64"):
            profile = resolve_platform({})

        self.assertEqual("arm64", profile.architecture)

    def test_a_supported_machine_has_no_transcription_gap(self) -> None:
        self.assertIsNone(transcription_runtime_gap(linux_plasma_x11()))
        self.assertIsNone(transcription_runtime_gap(macos()))  # Apple Silicon macOS

    def test_a_gap_outside_transcription_is_reported_without_being_a_transcription_gap(self) -> None:
        """18.4's second half: a machine missing anything else, not just transcription."""
        synthetic_gaps = (
            RuntimeGap(
                runtime="a-fictional-synthesis-runtime",
                characteristic="a fictional architecture",
                capability="speech synthesis",
                matches=lambda profile: profile.operating_system is OperatingSystem.MACOS,
            ),
        )

        self.assertIsNone(transcription_runtime_gap(macos(), synthetic_gaps))
        gaps = runtime_gaps_for(macos(), synthetic_gaps)
        self.assertEqual(1, len(gaps))
        self.assertEqual("speech synthesis", gaps[0].capability)
        self.assertNotEqual(TRANSCRIPTION_CAPABILITY, gaps[0].capability)

    def test_the_real_table_only_ever_names_the_transcription_capability_today(self) -> None:
        """All three documented gaps take out transcription outright (design.md)."""
        self.assertEqual(3, len(RUNTIME_GAPS))
        self.assertTrue(all(gap.capability == TRANSCRIPTION_CAPABILITY for gap in RUNTIME_GAPS))


if __name__ == "__main__":
    unittest.main()
