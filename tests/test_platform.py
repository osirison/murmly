from __future__ import annotations

import os
import socket
import sys
import threading
import time
import types
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
    WINDOWS_MICROPHONE_PERMISSION,
    _detected_libc,
    _windows_microphone_permission_check,
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


#: The literal answer this test's own assertion needs, independent of
#: `operating_system_for` -- the very function `test_resolves_the_real_
#: operating_system_and_architecture` exists to check resolves `sys.platform`
#: through. Deriving the expectation from that same function would make the
#: assertion true by construction on every host, never wrong, which is not a
#: test of the resolution at all.
_REAL_HOST_OPERATING_SYSTEM = {
    "linux": OperatingSystem.LINUX,
    "win32": OperatingSystem.WINDOWS,
    "darwin": OperatingSystem.MACOS,
}


class ResolvePlatformTests(unittest.TestCase):
    """Resolution answers for a supplied environment, not the process's own (18.2)."""

    def test_resolves_the_real_operating_system_and_architecture(self) -> None:
        # Runs on every host this suite's CI matrix has (Linux, Windows,
        # macOS): resolving with no environment override must still name
        # *this* process's own operating system, confirming resolve_platform
        # reads sys.platform rather than a stub -- whichever one that is.
        expected = _REAL_HOST_OPERATING_SYSTEM.get(sys.platform)
        if expected is None:
            self.skipTest(f"no expected operating system on file for sys.platform {sys.platform!r}")

        profile = resolve_platform({})

        self.assertEqual(expected, profile.operating_system)
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
        built by hand -- is what `transcription_runtime_gap` has to see.

        `sys.platform` pinned to Linux, not left to the real host's own:
        `resolve_platform` only ever asks `_detected_libc` at all when the
        resolved operating system is Linux (musl is a Linux/libc concept, not
        a Windows or macOS one), so this stays the Linux scenario it is on
        every runner, rather than silently resolving Windows or macOS and
        never reaching the two patches below at all.
        """
        with (
            patch("sys.platform", "linux"),
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
        # `getattr(..., 1)`, matching `_load_unix_socket_family` itself: the
        # loader reads *this* interpreter's own `socket` module regardless of
        # which profile was asked for, and Windows' has no `AF_UNIX`
        # attribute for a bare reference here to find either.
        self.assertIs(getattr(socket, "AF_UNIX", 1), choice.load())

    def test_command_channel_has_no_backend_on_macos(self) -> None:
        # Not Windows too (task 7.1): a named-pipe transport now exists there.
        # macOS keeps the UNIX socket eventually (design.md's "The command
        # channel"; task 13.1), but that backend does not exist yet.
        choice = BACKEND_REGISTRIES["command_channel"].select(macos())

        self.assertFalse(choice.available)
        self.assertIsNone(choice.mechanism)
        self.assertIn(macos().operating_system.value, choice.reason)

    def test_command_channel_selects_the_named_pipe_on_windows(self) -> None:
        choice = BACKEND_REGISTRIES["command_channel"].select(windows())

        self.assertEqual("named-pipe", choice.mechanism)
        self.assertTrue(choice.available)
        # Proves the loader -- and therefore `murmly.win_pipe` -- imports
        # cleanly from this Linux machine, where `pywin32` is not installed at
        # all: constructing the class itself touches no `pywin32` name, only
        # calling its methods would.
        from murmly.win_pipe import NamedPipeServer

        self.assertIs(NamedPipeServer, choice.load())

    def test_service_management_selects_systemd_on_linux(self) -> None:
        choice = BACKEND_REGISTRIES["service_management"].select(linux_plasma_x11())

        self.assertEqual("systemd", choice.mechanism)
        self.assertIs(UserService, choice.load())

    def test_service_management_selects_task_scheduler_on_windows(self) -> None:
        choice = BACKEND_REGISTRIES["service_management"].select(windows())

        self.assertEqual("task-scheduler", choice.mechanism)
        # Proves the loader -- and therefore `murmly.installer.WindowsUserService`
        # -- imports cleanly from this Linux machine: constructing the class
        # itself touches no `subprocess` call to `schtasks`, only calling its
        # methods would.
        from murmly.installer import WindowsUserService

        self.assertIs(WindowsUserService, choice.load())

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

    def test_hotkey_registration_selects_the_in_process_backend_on_windows(self) -> None:
        choice = BACKEND_REGISTRIES["hotkey_registration"].select(windows())

        self.assertEqual("windows-hotkey", choice.mechanism)
        self.assertTrue(choice.available)
        # Proves the loader -- and therefore `murmly.win_hotkey` -- imports
        # cleanly from this Linux machine, where no Win32 name is touched
        # until one of the class's own methods runs.
        from murmly.win_hotkey import WindowsHotkeyRegistrar

        self.assertIs(WindowsHotkeyRegistrar, choice.load())

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

    def test_clipboard_and_injection_select_native_win32_on_windows(self) -> None:
        """Task 9.1/9.2: the Win32 API, not a Linux clipboard command."""
        from murmly.win_clipboard import SEND_INPUT_METHOD, WindowsClipboardPaster

        clipboard = BACKEND_REGISTRIES["clipboard"].select(windows())
        injection = BACKEND_REGISTRIES["paste_injection"].select(windows())

        self.assertEqual("windows", clipboard.mechanism)
        self.assertTrue(clipboard.available)
        # Proves the loader -- and therefore `murmly.win_clipboard` -- imports
        # cleanly from this Linux machine, where no `ctypes.windll` name is
        # touched until one of the class's own methods runs.
        self.assertIs(WindowsClipboardPaster, clipboard.load())

        self.assertEqual("windows", injection.mechanism)
        self.assertTrue(injection.available)
        self.assertEqual(SEND_INPUT_METHOD, injection.load())

    def test_focus_observation_selects_x11_only_off_wayland(self) -> None:
        choice = BACKEND_REGISTRIES["focus_observation"].select(linux_plasma_x11())

        self.assertEqual("x11", choice.mechanism)
        self.assertIs(X11FocusObserver, choice.load())

        wayland_choice = BACKEND_REGISTRIES["focus_observation"].select(linux_plasma_wayland())
        self.assertFalse(wayland_choice.available)
        self.assertIn("X11", wayland_choice.reason)

    def test_focus_observation_selects_windows(self) -> None:
        """Task 9.4: `GetForegroundWindow` and friends, needing no permission."""
        from murmly.win_focus import WindowsFocusObserver

        choice = BACKEND_REGISTRIES["focus_observation"].select(windows())

        self.assertEqual("windows", choice.mechanism)
        self.assertTrue(choice.available)
        self.assertIs(WindowsFocusObserver, choice.load())

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

    def test_desktop_hotkey_mechanisms_are_not_in_process(self) -> None:
        self.assertFalse(hotkey_mechanism_is_in_process(linux_plasma_x11()))
        self.assertFalse(hotkey_mechanism_is_in_process(linux_gnome_wayland()))

    def test_windows_hotkey_mechanism_is_in_process(self) -> None:
        """Section 8's own backend: unlike Plasma and GNOME, Windows'
        `RegisterHotKey` binds to the daemon's own thread, so
        `hotkey_record.py`'s rebind-at-startup path is what a fresh session
        needs (task 5.4's design boundary, now populated)."""
        self.assertTrue(hotkey_mechanism_is_in_process(windows()))

    def test_the_real_table_names_exactly_the_windows_mechanism(self) -> None:
        """Nothing populates a macOS entry until section 13's backend exists
        -- an entry here today would claim a mechanism this change never
        built."""
        self.assertEqual(frozenset({"windows-hotkey"}), IN_PROCESS_HOTKEY_MECHANISMS)

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
        choice = BACKEND_REGISTRIES["command_channel"].select(macos())

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
    """6.4: the reporting shape, exercised against the real `windows-microphone`
    entry (task 9.5) and a constructed `Permission` for the two future macOS
    grants (sections 12, 14) that are not registered yet."""

    def test_the_real_table_has_only_the_windows_microphone_permission(self) -> None:
        """Section 9 populates the first entry -- Windows' microphone privacy
        setting (task 9.5). macOS's microphone, Accessibility, and Input
        Monitoring grants (sections 12, 14) do not exist yet, so this must
        name exactly one entry, not claim a check this change never built."""
        self.assertEqual({"windows-microphone"}, set(PERMISSIONS))

    def test_the_windows_microphone_permission_applies_only_to_windows(self) -> None:
        """`applies` is what keeps a Linux (or macOS) report from growing a
        `permissions` entry for a grant that platform does not gate anything
        behind -- `platform_diagnostics` filters on it before running `check`
        at all."""
        permission = PERMISSIONS["windows-microphone"]

        self.assertTrue(permission.applies(windows()))
        self.assertFalse(permission.applies(linux_plasma_x11()))
        self.assertFalse(permission.applies(macos()))

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


class WindowsMicrophonePermissionTests(unittest.TestCase):
    """Task 9.5: the tri-state rule over an injected registry reader -- any
    readable "Deny" is DENIED, both keys readable and neither "Deny" is
    GRANTED, and anything left unreadable is UNDETERMINED, never GRANTED. The
    exact registry path is documented in `_windows_microphone_permission_check`'s
    docstring and is unconfirmed on a real Windows machine; what is proven
    here is the rule this function applies to whatever that seam answers."""

    def test_a_readable_deny_is_denied(self) -> None:
        state = _windows_microphone_permission_check(
            windows(), read_registry_value=lambda key, name: "Deny"
        )

        self.assertEqual(PermissionState.DENIED, state)

    def test_the_master_toggle_denying_is_enough_even_if_the_per_app_key_is_absent(self) -> None:
        """A person can flip the master "Microphone access" toggle off without
        touching the `NonPackaged` desktop-apps toggle at all -- checking only
        the per-app key would read that as no denial and report `GRANTED`,
        which is exactly what the `platform-support` spec forbids."""

        def read(key_path: str, _name: str) -> str | None:
            return "Deny" if not key_path.endswith("NonPackaged") else None

        state = _windows_microphone_permission_check(windows(), read_registry_value=read)

        self.assertEqual(PermissionState.DENIED, state)

    def test_a_readable_allow_is_granted(self) -> None:
        state = _windows_microphone_permission_check(
            windows(), read_registry_value=lambda key, name: "Allow"
        )

        self.assertEqual(PermissionState.GRANTED, state)

    def test_nothing_readable_is_undetermined_not_granted(self) -> None:
        """Neither consent-store key can be read -- the platform offers no
        way to tell whether the grant was given, so this must answer
        `UNDETERMINED`, never `GRANTED` (the `platform-support` spec's "A
        permission whose state cannot be read")."""
        state = _windows_microphone_permission_check(
            windows(), read_registry_value=lambda key, name: None
        )

        self.assertEqual(PermissionState.UNDETERMINED, state)

    def test_one_key_unreadable_is_undetermined_even_though_the_other_reads_allow(self) -> None:
        """The master toggle reading "Allow" does not vouch for a
        `NonPackaged` key this cannot read at all -- an unreadable key might
        be hiding a denial, so this stays `UNDETERMINED` rather than trusting
        the one key that did answer."""

        def read(key_path: str, _name: str) -> str | None:
            return None if key_path.endswith("NonPackaged") else "Allow"

        state = _windows_microphone_permission_check(windows(), read_registry_value=read)

        self.assertEqual(PermissionState.UNDETERMINED, state)

    def test_denial_is_checked_case_insensitively(self) -> None:
        state = _windows_microphone_permission_check(
            windows(), read_registry_value=lambda key, name: "deny"
        )

        self.assertEqual(PermissionState.DENIED, state)

    def test_registered_under_the_real_table_with_the_check_wired_in(self) -> None:
        permission = PERMISSIONS[WINDOWS_MICROPHONE_PERMISSION]

        self.assertIs(_windows_microphone_permission_check, permission.check)


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


class NamedPipeShapeTests(unittest.TestCase):
    """Task 7.5's string check, exercised from any platform: `is_pipe_name`
    decides which of the two wrong channel-shape combinations a configured
    value is, independently of which operating system is asking."""

    def test_the_configured_default_pipe_name_is_recognised(self) -> None:
        from pathlib import Path

        from murmly.win_pipe import is_pipe_name

        # The exact round trip `MurmlyConfig.socket_path` and `daemon.py` put
        # the configured value through: `Path(WINDOWS_PIPE_NAME)`, then
        # `str(...)`. `pathlib.PurePosixPath` -- what `Path` resolves to on
        # this machine -- treats backslashes as ordinary name characters
        # rather than separators, so the string survives the round trip
        # unchanged, which is what makes this assertion meaningful here.
        from murmly.config import WINDOWS_PIPE_NAME

        self.assertEqual(WINDOWS_PIPE_NAME, str(Path(WINDOWS_PIPE_NAME)))
        self.assertTrue(is_pipe_name(str(Path(WINDOWS_PIPE_NAME))))

    def test_case_is_ignored_because_the_pipe_namespace_ignores_it(self) -> None:
        from murmly.win_pipe import is_pipe_name

        self.assertTrue(is_pipe_name(r"\\.\PIPE\murmly"))
        self.assertTrue(is_pipe_name(r"\\.\Pipe\Murmly"))

    def test_a_filesystem_path_is_not_a_pipe_name(self) -> None:
        from murmly.win_pipe import is_pipe_name

        self.assertFalse(is_pipe_name("/run/user/1000/murmly.sock"))
        self.assertFalse(is_pipe_name(r"C:\Users\person\AppData\murmly.sock"))


class NamedPipeDacShapeTests(unittest.TestCase):
    """Task 7.2's DACL, the half of it a machine without `pywin32` can check:
    the pure function describing what the descriptor must contain, kept
    separate in `win_pipe.py` from the `win32security` calls that build the
    real thing. See `WindowsPipeSecurityDescriptorIntegrationTests` below for
    the other half."""

    def test_the_dacl_names_exactly_one_entry(self) -> None:
        from murmly.win_pipe import owner_only_dacl_entries

        sentinel = object()

        entries = owner_only_dacl_entries(sentinel)

        self.assertEqual(1, len(entries))

    def test_the_one_entry_names_the_given_sid_and_nothing_else(self) -> None:
        from murmly.win_pipe import owner_only_dacl_entries

        sentinel = object()

        (entry,) = owner_only_dacl_entries(sentinel)

        self.assertIs(sentinel, entry.sid)

    def test_the_one_entry_grants_full_control_not_read_and_write_alone(self) -> None:
        """The access mask that avoids the classic named-pipe DACL defect:
        read/write alone denies `FILE_CREATE_PIPE_INSTANCE` (0x00000004) to
        the pipe's own creator, wedging the server the moment a second client
        tries to connect. `GENERIC_ALL` is `0x10000000` -- see win32
        `WinNT.h`."""
        from murmly.win_pipe import GENERIC_ALL, owner_only_dacl_entries

        (entry,) = owner_only_dacl_entries(object())

        self.assertEqual(0x10000000, GENERIC_ALL)
        self.assertEqual(GENERIC_ALL, entry.access_mask)

    def test_two_different_sids_produce_two_different_single_entry_dacls(self) -> None:
        """Not a fixed descriptor reused regardless of argument -- each
        account's own DACL names that account and no other."""
        from murmly.win_pipe import owner_only_dacl_entries

        left, right = object(), object()

        (left_entry,) = owner_only_dacl_entries(left)
        (right_entry,) = owner_only_dacl_entries(right)

        self.assertIs(left, left_entry.sid)
        self.assertIs(right, right_entry.sid)
        self.assertIsNot(left_entry.sid, right_entry.sid)


class WindowsPipeSecurityDescriptorIntegrationTests(unittest.TestCase):
    """18.6: read the real DACL back off a real pipe and assert it names only
    the creating user's SID. Needs `pywin32` and a Windows kernel, neither of
    which this machine has, so every test here skips itself immediately --
    the `X11RuntimeIntegrationTests` pattern (`tests/test_focus.py`) applied
    to the wrong-operating-system case (18.18) rather than a missing display.
    This class has never executed anywhere this suite has run: the exact
    `win32security.GetSecurityInfo` return shape used below is written from
    the documented Win32 API and common `pywin32` usage, not from having run
    it, and a Windows machine is the first real check of it. See this
    session's return value for what that means for 18.6's checkbox."""

    def setUp(self) -> None:
        if sys.platform != "win32":
            self.skipTest("A Windows kernel is required to create a named pipe")

    def test_the_dacl_names_only_the_creating_users_sid(self) -> None:
        import win32security

        from murmly.win_pipe import NamedPipeServer, current_user_sid_string

        pipe_name = r"\\.\pipe\murmly-test-dacl-readback"
        server = NamedPipeServer(pipe_name)
        try:
            # Read back by the *handle* Murmly already holds
            # (`SE_KERNEL_OBJECT`), not by reopening the name
            # (`GetNamedSecurityInfo`, `SE_FILE_OBJECT`): reopening the pipe's
            # name is itself a client `CreateFile` against the one instance
            # this server has waiting to accept a connection, and would
            # consume that connection rather than merely inspect it.
            # `GetSecurityInfo` returns a single `PySECURITY_DESCRIPTOR`, not
            # the 5-tuple its C signature's out-parameters might suggest --
            # confirmed by this suite's own first real Windows CI run
            # (`TypeError: cannot unpack non-iterable PySECURITY_DESCRIPTOR
            # object`, ci2-Windows.log). The DACL is read off it with its own
            # `GetSecurityDescriptorDacl` method, matching `pywin32`'s usage
            # for `GetNamedSecurityInfo` elsewhere.
            descriptor = win32security.GetSecurityInfo(
                server.handle,
                win32security.SE_KERNEL_OBJECT,
                win32security.DACL_SECURITY_INFORMATION,
            )
            dacl = descriptor.GetSecurityDescriptorDacl()

            self.assertIsNotNone(dacl)
            self.assertEqual(1, dacl.GetAceCount())
            # `PyACL.GetAce` returns `((ace_type, ace_flags), mask, sid)` --
            # a 3-tuple whose first element is itself a 2-tuple, not four
            # flat values -- per `pywin32`'s own documented shape for this
            # call.
            (_ace_type, _ace_flags), _mask, sid = dacl.GetAce(0)
            self.assertEqual(current_user_sid_string(), win32security.ConvertSidToStringSid(sid))
        finally:
            server.close()


class _FakeWin32Error(Exception):
    """Stand-in for `pywintypes.error`: carries `.winerror`/`.funcname`/
    `.strerror` the same way the real exception does (its `.args` are
    exactly `(winerror, funcname, strerror)`), without needing `pywin32`
    installed to construct one. Every real Win32 call site in `win_pipe.py`
    reads only `.winerror` and `str(error)`, both of which this provides.
    """

    def __init__(self, winerror: int, funcname: str = "", strerror: str = "") -> None:
        self.winerror = winerror
        self.funcname = funcname
        self.strerror = strerror
        super().__init__(winerror, funcname, strerror)


class _FakeOverlapped:
    """Stand-in for `pywintypes.OVERLAPPED`: the one attribute `win_pipe.py`
    ever sets or reads on it (`hEvent`) as a plain, uninspected value."""

    def __init__(self) -> None:
        self.hEvent = None


def _install_fake_win32(
    *,
    connect_named_pipe=None,
    read_file=None,
    write_file=None,
    create_file=None,
    wait_named_pipe=None,
    wait_for_single_object=None,
    get_overlapped_result=None,
    cancel_io=None,
    close_handle=None,
    get_current_process=None,
    duplicate_handle=None,
    disconnect_named_pipe=None,
    flush_file_buffers=None,
):
    """A minimal Win32 layer, faked at exactly the boundary `win_pipe.py`
    imports across.

    Every `import win32...`/`import pywintypes` in `win_pipe.py` is
    function-local (see that module's own docstring for why), which is what
    makes this possible at all: patching `sys.modules` before a call is
    enough to make `win_pipe.py`'s real, unmodified code run against these
    fakes, on a machine with no `pywin32` installed -- the logic under test
    is `win_pipe.py`'s own state machine and error translation, not a
    reimplementation of it.

    Every keyword defaults to a no-op or an immediate success, so a test
    only has to say what it cares about (`get_overlapped_result=...` to
    control one call's outcome, say) rather than script every function this
    module might call along the way. Returns a `unittest.mock.patch.dict`
    context manager -- entries this process never had (nothing here is
    installed at all) are removed again on exit, never left to leak into
    `NamedPipeShapeTests`' own proof that `murmly.win_pipe` imports cleanly
    with no `pywin32` present.
    """
    fake_pywintypes = types.SimpleNamespace(error=_FakeWin32Error, OVERLAPPED=_FakeOverlapped)
    fake_win32event = types.SimpleNamespace(
        CreateEvent=lambda *a, **k: object(),
        WaitForSingleObject=wait_for_single_object or (lambda *a, **k: 0),
        WAIT_TIMEOUT=258,
        WAIT_OBJECT_0=0,
        INFINITE=0xFFFFFFFF,
    )
    fake_win32file = types.SimpleNamespace(
        GetOverlappedResult=get_overlapped_result or (lambda *a, **k: 0),
        CancelIo=cancel_io or (lambda *a, **k: None),
        CloseHandle=close_handle or (lambda *a, **k: None),
        ReadFile=read_file,
        WriteFile=write_file,
        AllocateReadBuffer=lambda size: bytearray(size),
        CreateFile=create_file,
        FlushFileBuffers=flush_file_buffers or (lambda *a, **k: None),
    )
    fake_win32pipe = types.SimpleNamespace(
        ConnectNamedPipe=connect_named_pipe,
        WaitNamedPipe=wait_named_pipe,
        DisconnectNamedPipe=disconnect_named_pipe or (lambda *a, **k: None),
    )
    fake_win32con = types.SimpleNamespace(
        GENERIC_READ=0x80000000,
        GENERIC_WRITE=0x40000000,
        OPEN_EXISTING=3,
        FILE_FLAG_OVERLAPPED=0x40000000,
        DUPLICATE_SAME_ACCESS=2,
    )
    fake_win32api = types.SimpleNamespace(
        GetCurrentProcess=get_current_process or (lambda: object()),
        DuplicateHandle=duplicate_handle or (lambda *a, **k: object()),
    )
    return patch.dict(
        sys.modules,
        {
            "pywintypes": fake_pywintypes,
            "win32event": fake_win32event,
            "win32file": fake_win32file,
            "win32pipe": fake_win32pipe,
            "win32con": fake_win32con,
            "win32api": fake_win32api,
        },
    )


class NamedPipeIOErrorTests(unittest.TestCase):
    """The error-code translation table `_run_overlapped` and its callers
    build on, tested with no Win32 layer at all -- `_pipe_error_from` reads
    only `.winerror` and `str()` off whatever it is given (see its own
    docstring)."""

    def test_carries_the_win32_error_code_as_a_plain_attribute(self) -> None:
        from murmly.win_pipe import NamedPipeIOError

        error = NamedPipeIOError(109, "The pipe has been ended.")

        self.assertEqual(109, error.win32_error_code)
        self.assertIsInstance(error, OSError)

    def test_pipe_error_from_reads_the_winerror_code_and_message(self) -> None:
        from murmly.win_pipe import _pipe_error_from

        source = _FakeWin32Error(232, "WriteFile", "The pipe is being closed.")

        translated = _pipe_error_from(source)

        self.assertEqual(232, translated.win32_error_code)
        self.assertEqual(str(source), str(translated))


class RunOverlappedTests(unittest.TestCase):
    """Task's item 1 (`GetOverlappedResult(bWait=False)` on a not-yet-complete
    operation, ci2-Windows.log's 81 `(996, 'GetOverlappedResult', ...)`
    failures) and the `CancelIo`/timeout race it names as its own defect,
    against a faked Win32 layer standing in for the real one no machine
    running this suite has.
    """

    def test_pipe_connected_returns_immediately_without_collecting_a_result(self) -> None:
        """The regression test for the 996 bug: `ERROR_PIPE_CONNECTED` means
        no overlapped operation was ever queued (MSDN: the OVERLAPPED's event
        is never signalled for this case), so `GetOverlappedResult` must
        never be called for it -- calling it anyway, unconditionally, is
        exactly what produced every one of the 81 failures."""
        from murmly.win_pipe import ERROR_PIPE_CONNECTED, _run_overlapped

        collected = []

        def get_overlapped_result(*args: object) -> int:
            collected.append(args)
            return 999

        def start(overlapped: object) -> int:
            raise _FakeWin32Error(ERROR_PIPE_CONNECTED, "ConnectNamedPipe", "connected")

        with _install_fake_win32(get_overlapped_result=get_overlapped_result):
            result = _run_overlapped(object(), start, 1.0)

        self.assertEqual(0, result)
        self.assertEqual([], collected)

    def test_pipe_connected_returned_without_raising_also_returns_immediately(self) -> None:
        """`ConnectNamedPipe`'s own convention, confirmed against `pywin32`'s
        own C source for it (`win32pipe.i`): it *returns* `ERROR_IO_PENDING`
        and `ERROR_PIPE_CONNECTED` as a plain int, raising only for a
        genuinely unexpected failure -- it does not raise for either of the
        two codes the test above exercises via a raised
        `pywintypes.error`. Without this branch, a returned
        `ERROR_PIPE_CONNECTED` falls through to `pending = hr ==
        ERROR_IO_PENDING` (`False`) and then straight into
        `_collect_overlapped_result`'s `bWait=True`, which blocks forever:
        MSDN's own Remarks for this condition are that the OVERLAPPED's event
        is never signalled, so nothing will ever wake that wait."""
        from murmly.win_pipe import ERROR_PIPE_CONNECTED, _run_overlapped

        collected = []

        def get_overlapped_result(*args: object) -> int:
            collected.append(args)
            return 999

        def start(overlapped: object) -> int:
            return ERROR_PIPE_CONNECTED

        with _install_fake_win32(get_overlapped_result=get_overlapped_result):
            result = _run_overlapped(object(), start, 1.0)

        self.assertEqual(0, result)
        self.assertEqual([], collected)

    def test_a_synchronous_success_still_collects_the_transfer_count(self) -> None:
        """`ReadFile`/`WriteFile` completing synchronously (`hr == 0`) is the
        one case that *did* queue real overlapped I/O and does signal the
        event (MSDN) -- unlike `ERROR_PIPE_CONNECTED` above, collecting the
        result here is both safe and the only way to learn the transfer
        count."""
        from murmly.win_pipe import _run_overlapped

        def start(overlapped: object) -> int:
            return 0

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            self.assertTrue(wait)
            return 42

        with _install_fake_win32(get_overlapped_result=get_overlapped_result):
            result = _run_overlapped(object(), start, 1.0)

        self.assertEqual(42, result)

    def test_a_pending_operation_raised_by_start_waits_then_collects(self) -> None:
        """`ConnectNamedPipe`'s own convention: pending is signalled by
        raising `ERROR_IO_PENDING` rather than by returning it."""
        from murmly.win_pipe import ERROR_IO_PENDING, _run_overlapped

        waited = []

        def start(overlapped: object) -> int:
            raise _FakeWin32Error(ERROR_IO_PENDING, "ConnectNamedPipe", "pending")

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            waited.append(timeout_ms)
            return 0

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            return 7

        with _install_fake_win32(
            wait_for_single_object=wait_for_single_object,
            get_overlapped_result=get_overlapped_result,
        ):
            result = _run_overlapped(object(), start, 2.5)

        self.assertEqual(7, result)
        self.assertEqual([2500], waited)

    def test_a_pending_operation_returned_without_raising_is_also_waited_on(self) -> None:
        """`ReadFile`/`WriteFile`'s own convention for the same condition:
        pending is *returned* as `hr` without raising. Both conventions
        normalise to the same wait, since a test on Linux cannot confirm
        which one a given `pywin32` release actually uses for which call."""
        from murmly.win_pipe import ERROR_IO_PENDING, _run_overlapped

        def start(overlapped: object) -> int:
            return ERROR_IO_PENDING

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            return 0

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            return 3

        with _install_fake_win32(
            wait_for_single_object=wait_for_single_object,
            get_overlapped_result=get_overlapped_result,
        ):
            result = _run_overlapped(object(), start, 1.0)

        self.assertEqual(3, result)

    def test_a_timeout_cancels_and_raises_timeouterror_on_confirmed_abort(self) -> None:
        from murmly.win_pipe import ERROR_IO_PENDING, ERROR_OPERATION_ABORTED, _run_overlapped

        cancelled = []

        def start(overlapped: object) -> int:
            raise _FakeWin32Error(ERROR_IO_PENDING)

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            return 258  # WAIT_TIMEOUT

        def cancel_io(handle: object) -> None:
            cancelled.append(True)

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            raise _FakeWin32Error(ERROR_OPERATION_ABORTED, "GetOverlappedResult", "aborted")

        with _install_fake_win32(
            wait_for_single_object=wait_for_single_object,
            cancel_io=cancel_io,
            get_overlapped_result=get_overlapped_result,
        ):
            with self.assertRaises(TimeoutError):
                _run_overlapped(object(), start, 0.2)

        self.assertEqual([True], cancelled)

    def test_a_timeout_that_races_a_completion_returns_the_completed_result(self) -> None:
        """The task's own "a cancel that races completion is its own
        defect": `CancelIo` only requests cancellation (MSDN) -- the
        operation may complete anyway before the request lands. When that
        happens, `GetOverlappedResult` reports the real outcome instead of
        `ERROR_OPERATION_ABORTED`, and that outcome -- not a manufactured
        `TimeoutError` -- is what this call must return."""
        from murmly.win_pipe import ERROR_IO_PENDING, _run_overlapped

        def start(overlapped: object) -> int:
            raise _FakeWin32Error(ERROR_IO_PENDING)

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            return 258  # WAIT_TIMEOUT

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            return 5  # the operation actually completed before the cancel landed

        with _install_fake_win32(
            wait_for_single_object=wait_for_single_object,
            get_overlapped_result=get_overlapped_result,
        ):
            result = _run_overlapped(object(), start, 0.2)

        self.assertEqual(5, result)

    def test_cancelio_raising_an_error_is_tolerated(self) -> None:
        """Unlike `CancelIoEx`, `pywin32`'s `CancelIo` wrapper is a bare
        `BOOLAPI` (`win32file.i`: `BOOLAPI CancelIo(PyHANDLE handle);`) with
        no operation-specific "already completed" failure code of its own to
        single out -- so `_cancel_overlapped` tolerates whatever
        `pywintypes.error` it raises, for whatever reason, exactly the way it
        tolerated `CancelIoEx`'s `ERROR_NOT_FOUND` before this fix. Not this
        function's problem either way: the `GetOverlappedResult` call that
        always follows still reports the real outcome."""
        from murmly.win_pipe import ERROR_IO_PENDING, _run_overlapped

        def start(overlapped: object) -> int:
            raise _FakeWin32Error(ERROR_IO_PENDING)

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            return 258

        def cancel_io(handle: object) -> None:
            raise _FakeWin32Error(6, "CancelIo", "invalid handle")

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            return 9

        with _install_fake_win32(
            wait_for_single_object=wait_for_single_object,
            cancel_io=cancel_io,
            get_overlapped_result=get_overlapped_result,
        ):
            result = _run_overlapped(object(), start, 0.2)

        self.assertEqual(9, result)

    def test_cancelio_is_called_with_only_the_handle(self) -> None:
        """`CancelIo`'s C signature takes one argument -- confirmed against
        `pywin32`'s own source for it (`win32file.i`) -- unlike `CancelIoEx`,
        which also takes the specific `OVERLAPPED` to target. Passing the
        `overlapped` this module still threads through `_cancel_overlapped`
        (kept so its one call site in `_run_overlapped` needs no special
        case) would be a `TypeError` against the real wrapper; nothing here
        can raise that on a faked one; this test is the state-machine's own
        proof, so a real Windows run is not the first place a wrong arity
        would surface."""
        from murmly.win_pipe import ERROR_IO_PENDING, _run_overlapped

        cancel_calls = []

        def start(overlapped: object) -> int:
            raise _FakeWin32Error(ERROR_IO_PENDING)

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            return 258

        def cancel_io(*args: object) -> None:
            cancel_calls.append(args)

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            return 9

        handle = object()
        with _install_fake_win32(
            wait_for_single_object=wait_for_single_object,
            cancel_io=cancel_io,
            get_overlapped_result=get_overlapped_result,
        ):
            _run_overlapped(handle, start, 0.2)

        self.assertEqual([(handle,)], cancel_calls)

    def test_a_genuine_synchronous_failure_is_translated_without_collecting_a_result(
        self,
    ) -> None:
        """A real, immediate failure (not `ERROR_IO_PENDING`, not
        `ERROR_PIPE_CONNECTED`) never queued an overlapped operation either --
        `GetOverlappedResult` must not be called for it any more than for
        `ERROR_PIPE_CONNECTED`, and the original error is the outcome."""
        from murmly.win_pipe import ERROR_ACCESS_DENIED, NamedPipeIOError, _run_overlapped

        collected = []

        def start(overlapped: object) -> int:
            raise _FakeWin32Error(ERROR_ACCESS_DENIED, "ReadFile", "denied")

        def get_overlapped_result(*args: object) -> int:
            collected.append(args)
            return 0

        with _install_fake_win32(get_overlapped_result=get_overlapped_result):
            with self.assertRaises(NamedPipeIOError) as failure:
                _run_overlapped(object(), start, 1.0)

        self.assertEqual(ERROR_ACCESS_DENIED, failure.exception.win32_error_code)
        self.assertEqual([], collected)

    def test_wait_for_single_object_failure_is_translated(self) -> None:
        """`WaitForSingleObject`'s C API reports a real failure through its
        return value (`WAIT_FAILED`); `pywin32`'s wrapper raises instead --
        translated the same way every other call in this module is, rather
        than left to escape as the raw Win32 exception type."""
        from murmly.win_pipe import ERROR_IO_PENDING, NamedPipeIOError, _run_overlapped

        def start(overlapped: object) -> int:
            raise _FakeWin32Error(ERROR_IO_PENDING)

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            raise _FakeWin32Error(6, "WaitForSingleObject", "invalid handle")

        with _install_fake_win32(wait_for_single_object=wait_for_single_object):
            with self.assertRaises(NamedPipeIOError) as failure:
                _run_overlapped(object(), start, 1.0)

        self.assertEqual(6, failure.exception.win32_error_code)


class NamedPipeConnectionTranslationTests(unittest.TestCase):
    """Task item 4: `ERROR_BROKEN_PIPE` on read and `ERROR_NO_DATA` on write
    are what a named pipe reports for what a UNIX socket reports as a
    zero-length `recv` and a `BrokenPipeError` `sendall` -- translated so
    `daemon.py`'s code, written once against `socket.socket`, needs no
    named-pipe-specific branch."""

    def test_recv_translates_broken_pipe_to_empty_bytes(self) -> None:
        from murmly.win_pipe import ERROR_BROKEN_PIPE, NamedPipeConnection

        def read_file(handle: object, buffer: object, overlapped: object) -> tuple[int, object]:
            raise _FakeWin32Error(ERROR_BROKEN_PIPE, "ReadFile", "The pipe has been ended.")

        connection = NamedPipeConnection(object())
        with _install_fake_win32(read_file=read_file):
            self.assertEqual(b"", connection.recv(4096))

    def test_recv_translates_pipe_not_connected_to_empty_bytes(self) -> None:
        """The same disconnect, reported from a later point in it.

        Windows chooses between `ERROR_BROKEN_PIPE` and
        `ERROR_PIPE_NOT_CONNECTED` by how far the peer's departure had
        progressed when the read landed, which is not something the caller can
        arrange or predict. Handling only the first left a speech session whose
        client stopped reading waiting to be disconnected until the test timed
        out, on Windows CI, with nothing wrong on Linux.
        """
        from murmly.win_pipe import ERROR_PIPE_NOT_CONNECTED, NamedPipeConnection

        def read_file(handle: object, buffer: object, overlapped: object) -> tuple[int, object]:
            raise _FakeWin32Error(
                ERROR_PIPE_NOT_CONNECTED, "ReadFile", "No process is on the other end of the pipe."
            )

        connection = NamedPipeConnection(object())
        with _install_fake_win32(read_file=read_file):
            self.assertEqual(b"", connection.recv(4096))

    def test_recv_propagates_other_errors(self) -> None:
        from murmly.win_pipe import NamedPipeIOError, NamedPipeConnection

        def read_file(handle: object, buffer: object, overlapped: object) -> tuple[int, object]:
            raise _FakeWin32Error(5, "ReadFile", "denied")

        connection = NamedPipeConnection(object())
        with _install_fake_win32(read_file=read_file):
            with self.assertRaises(NamedPipeIOError):
                connection.recv(4096)

    def test_recv_returns_the_transferred_slice_of_the_buffer(self) -> None:
        from murmly.win_pipe import NamedPipeConnection

        def read_file(handle: object, buffer: bytearray, overlapped: object) -> tuple[int, object]:
            buffer[:5] = b"hello"
            return 0, buffer

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            return 5

        connection = NamedPipeConnection(object())
        with _install_fake_win32(read_file=read_file, get_overlapped_result=get_overlapped_result):
            self.assertEqual(b"hello", connection.recv(64))

    def test_sendall_translates_no_data_to_brokenpipeerror(self) -> None:
        from murmly.win_pipe import ERROR_NO_DATA, NamedPipeConnection

        def write_file(handle: object, data: bytes, overlapped: object) -> tuple[int, int]:
            raise _FakeWin32Error(ERROR_NO_DATA, "WriteFile", "The pipe is being closed.")

        connection = NamedPipeConnection(object())
        with _install_fake_win32(write_file=write_file):
            with self.assertRaises(BrokenPipeError):
                connection.sendall(b"hello")

    def test_sendall_translates_broken_pipe_to_brokenpipeerror(self) -> None:
        from murmly.win_pipe import ERROR_BROKEN_PIPE, NamedPipeConnection

        def write_file(handle: object, data: bytes, overlapped: object) -> tuple[int, int]:
            raise _FakeWin32Error(ERROR_BROKEN_PIPE, "WriteFile", "The pipe has been ended.")

        connection = NamedPipeConnection(object())
        with _install_fake_win32(write_file=write_file):
            with self.assertRaises(BrokenPipeError):
                connection.sendall(b"hello")

    def test_sendall_translates_pipe_not_connected_to_brokenpipeerror(self) -> None:
        from murmly.win_pipe import ERROR_PIPE_NOT_CONNECTED, NamedPipeConnection

        def write_file(handle: object, data: bytes, overlapped: object) -> tuple[int, int]:
            raise _FakeWin32Error(
                ERROR_PIPE_NOT_CONNECTED, "WriteFile", "No process is on the other end of the pipe."
            )

        connection = NamedPipeConnection(object())
        with _install_fake_win32(write_file=write_file):
            with self.assertRaises(BrokenPipeError):
                connection.sendall(b"hello")

    def test_sendall_propagates_other_errors(self) -> None:
        from murmly.win_pipe import NamedPipeIOError, NamedPipeConnection

        def write_file(handle: object, data: bytes, overlapped: object) -> tuple[int, int]:
            raise _FakeWin32Error(5, "WriteFile", "denied")

        connection = NamedPipeConnection(object())
        with _install_fake_win32(write_file=write_file):
            with self.assertRaises(NamedPipeIOError):
                connection.sendall(b"hello")


class NamedPipeConnectionDupTests(unittest.TestCase):
    """Task item 1: 45 `AttributeError: 'NamedPipeConnection' object has no
    attribute 'dup'` -- `SpeechSessionConnection` (`daemon.py`) needs a
    second, independently closable handle onto the same connection, the way
    `socket.socket.dup()` already gives the UNIX transport."""

    def test_dup_duplicates_against_the_current_process_on_both_sides(self) -> None:
        from murmly.win_pipe import NamedPipeConnection

        process = object()
        original_handle = object()
        calls = []

        def get_current_process() -> object:
            return process

        def duplicate_handle(*args: object) -> object:
            calls.append(args)
            return object()

        connection = NamedPipeConnection(original_handle)
        with _install_fake_win32(
            get_current_process=get_current_process, duplicate_handle=duplicate_handle
        ):
            connection.dup()

        # `(hSourceProcess, hSource, hTargetProcess, desiredAccess,
        # bInheritHandle, options)` -- `pywin32`'s own parameter order for
        # `DuplicateHandle` (`win32apimodule.cpp`). Both process arguments
        # are this same process, both ends of one call, and `options`
        # carries `DUPLICATE_SAME_ACCESS` so the duplicate needs no access
        # mask of its own.
        self.assertEqual([(process, original_handle, process, 0, False, 2)], calls)

    def test_dup_returns_a_distinct_connection_wrapping_the_duplicated_handle(self) -> None:
        from murmly.win_pipe import NamedPipeConnection

        duplicated_handle = object()

        connection = NamedPipeConnection(object())
        with _install_fake_win32(duplicate_handle=lambda *a, **k: duplicated_handle):
            duplicate = connection.dup()

        self.assertIsInstance(duplicate, NamedPipeConnection)
        self.assertIsNot(duplicate, connection)
        self.assertIs(duplicated_handle, duplicate.handle)

    def test_closing_the_duplicate_does_not_touch_the_original(self) -> None:
        """The whole reason `dup()` exists for `SpeechSessionConnection`: a
        writer thread that closes its own handle on its own schedule must
        never close the handle the reader thread is still using -- proven
        here by driving both handles' `close()` through the same faked
        `CloseHandle` and checking only the duplicate's handle was ever
        passed to it."""
        from murmly.win_pipe import NamedPipeConnection

        original_handle = object()
        duplicated_handle = object()
        closed = []

        def close_handle(handle: object) -> None:
            closed.append(handle)

        connection = NamedPipeConnection(original_handle)
        with _install_fake_win32(
            duplicate_handle=lambda *a, **k: duplicated_handle, close_handle=close_handle
        ):
            duplicate = connection.dup()
            duplicate.close()

        self.assertEqual([duplicated_handle], closed)

    def test_dup_translates_a_failure_into_oserror(self) -> None:
        from murmly.win_pipe import NamedPipeConnection

        def duplicate_handle(*args: object) -> object:
            raise _FakeWin32Error(6, "DuplicateHandle", "invalid handle")

        connection = NamedPipeConnection(object())
        with _install_fake_win32(duplicate_handle=duplicate_handle):
            with self.assertRaises(OSError):
                connection.dup()


class NamedPipeConnectionShutdownTests(unittest.TestCase):
    """`shutdown()` must never discard a frame a still-reading client would
    otherwise get, and must never hang past its own bound for a client that
    has stopped reading -- the two Windows-only failures task item 20
    exists to fix (`test_shutdown_stops_speech_and_tells_the_session` and
    `test_a_session_that_stops_reading_is_disconnected_rather_than_stalling_
    playback` in `tests/test_speech_session.py`). `DisconnectNamedPipe`'s
    own remarks say it discards unread data; `FlushFileBuffers`'s own remarks
    say it does not return until the client has read everything -- so
    whether the flush actually finished in time is what must decide whether
    disconnecting is safe."""

    def test_shutdown_disconnects_once_the_flush_completes(self) -> None:
        """A flush that finishes promptly (a client still reading, or
        nothing left to protect) means disconnecting cannot lose anything."""
        from murmly.win_pipe import NamedPipeConnection

        disconnect_calls = []

        def disconnect_named_pipe(handle: object) -> None:
            disconnect_calls.append(handle)

        handle = object()
        connection = NamedPipeConnection(handle)
        with _install_fake_win32(disconnect_named_pipe=disconnect_named_pipe):
            connection.shutdown(socket.SHUT_RDWR)

        self.assertEqual([handle], disconnect_calls)

    def test_shutdown_flushes_before_disconnecting(self) -> None:
        from murmly.win_pipe import NamedPipeConnection

        order = []

        def flush_file_buffers(handle: object) -> None:
            order.append("flush")

        def disconnect_named_pipe(handle: object) -> None:
            order.append("disconnect")

        connection = NamedPipeConnection(object())
        with _install_fake_win32(
            flush_file_buffers=flush_file_buffers, disconnect_named_pipe=disconnect_named_pipe
        ):
            connection.shutdown(socket.SHUT_RDWR)

        self.assertEqual(["flush", "disconnect"], order)

    def test_shutdown_skips_disconnect_when_the_flush_does_not_finish_in_time(
        self,
    ) -> None:
        """A client that has stopped reading -- exactly what
        `SpeechSessionConnection.send`'s own backlog check disconnects a
        session for -- never lets `FlushFileBuffers` return. Skipping
        `DisconnectNamedPipe` here is what keeps its buffered bytes readable
        by anyone still holding the client handle; disconnecting anyway would
        discard them for no reason -- the client is already gone from
        Murmly's own point of view."""
        from murmly.win_pipe import PIPE_FLUSH_TIMEOUT_SECONDS, NamedPipeConnection

        never_returns = threading.Event()
        disconnect_calls = []

        def flush_file_buffers(handle: object) -> None:
            never_returns.wait()  # a client that will never read this out

        def disconnect_named_pipe(handle: object) -> None:
            disconnect_calls.append(handle)

        connection = NamedPipeConnection(object())
        started = time.monotonic()
        with _install_fake_win32(
            flush_file_buffers=flush_file_buffers, disconnect_named_pipe=disconnect_named_pipe
        ):
            connection.shutdown(socket.SHUT_RDWR)
        elapsed = time.monotonic() - started

        self.assertEqual([], disconnect_calls, "disconnect must not run over unflushed data")
        self.assertLess(
            elapsed,
            PIPE_FLUSH_TIMEOUT_SECONDS + 2.0,
            "shutdown must not hang past its own flush bound",
        )
        never_returns.set()  # let the leaked flush thread stop waiting

    def test_shutdown_disconnects_when_the_flush_itself_fails(self) -> None:
        """A flush that raises -- the pipe already broken or disconnected --
        has nothing left to protect, so `shutdown` falls back to the
        unconditional `DisconnectNamedPipe` this replaced."""
        from murmly.win_pipe import NamedPipeConnection

        disconnect_calls = []

        def flush_file_buffers(handle: object) -> None:
            raise _FakeWin32Error(232, "FlushFileBuffers", "the pipe is being closed")

        def disconnect_named_pipe(handle: object) -> None:
            disconnect_calls.append(handle)

        handle = object()
        connection = NamedPipeConnection(handle)
        with _install_fake_win32(
            flush_file_buffers=flush_file_buffers, disconnect_named_pipe=disconnect_named_pipe
        ):
            connection.shutdown(socket.SHUT_RDWR)

        self.assertEqual([handle], disconnect_calls)


class NamedPipeServerAcceptTests(unittest.TestCase):
    """Task item 3's `ERROR_NO_DATA` race, and the `ERROR_PIPE_CONNECTED`
    race's effect on `accept()` specifically: a state-machine test against a
    faked `create_named_pipe_server` and a faked `ConnectNamedPipe`."""

    def test_accept_returns_a_connection_when_pipe_connected_races_ahead(self) -> None:
        from murmly.win_pipe import ERROR_PIPE_CONNECTED, NamedPipeServer

        instances = iter(["first", "second"])

        def fake_create(pipe_name: str, *, first_instance: bool) -> str:
            return next(instances)

        def connect_named_pipe(handle: object, overlapped: object) -> None:
            raise _FakeWin32Error(ERROR_PIPE_CONNECTED, "ConnectNamedPipe", "connected")

        waited = []

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            waited.append(timeout_ms)
            return 0

        with patch("murmly.win_pipe.create_named_pipe_server", side_effect=fake_create), \
                _install_fake_win32(
                    connect_named_pipe=connect_named_pipe,
                    wait_for_single_object=wait_for_single_object,
                ):
            server = NamedPipeServer(r"\\.\pipe\murmly-test")
            connection, address = server.accept()

        self.assertEqual("first", connection.handle)
        self.assertEqual((r"\\.\pipe\murmly-test", 0), address)
        # The exact behaviour the 996 bug's fix depends on: a race this
        # common never has to wait on anything at all.
        self.assertEqual([], waited)

    def test_accept_returns_a_connection_when_connect_named_pipe_returns_connected(
        self,
    ) -> None:
        """The same race as the test above, reached through `pywin32`'s
        actual convention for `ConnectNamedPipe` -- a *returned* `hr`, not a
        raised `pywintypes.error` (confirmed against `pywin32`'s own
        `win32pipe.i`). `accept`'s own `start` closure must pass that return
        value through to `_run_overlapped` rather than discarding it: a
        `start` that always answers `0` regardless of what `ConnectNamedPipe`
        actually returned would misread this outcome as a *synchronous*
        completion instead -- `pending = hr == ERROR_IO_PENDING` is `False`
        either way a discarded return reads as `0` -- and call
        `GetOverlappedResult(bWait=True)` on an OVERLAPPED MSDN documents as
        never signalled for this condition, which on the real API blocks
        forever rather than returning the `999` this fake would hand back
        harmlessly if called. Asserting `collected == []` is what makes that
        distinction observable here without a fake that can actually hang."""
        from murmly.win_pipe import ERROR_PIPE_CONNECTED, NamedPipeServer

        instances = iter(["first", "second"])

        def fake_create(pipe_name: str, *, first_instance: bool) -> str:
            return next(instances)

        def connect_named_pipe(handle: object, overlapped: object) -> int:
            return ERROR_PIPE_CONNECTED

        waited = []

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            waited.append(timeout_ms)
            return 0

        collected = []

        def get_overlapped_result(*args: object) -> int:
            collected.append(args)
            return 999

        with patch("murmly.win_pipe.create_named_pipe_server", side_effect=fake_create), \
                _install_fake_win32(
                    connect_named_pipe=connect_named_pipe,
                    wait_for_single_object=wait_for_single_object,
                    get_overlapped_result=get_overlapped_result,
                ):
            server = NamedPipeServer(r"\\.\pipe\murmly-test")
            connection, address = server.accept()

        self.assertEqual("first", connection.handle)
        self.assertEqual((r"\\.\pipe\murmly-test", 0), address)
        self.assertEqual([], waited)
        self.assertEqual([], collected)

    def test_accept_waits_when_connect_named_pipe_returns_pending(self) -> None:
        """`ConnectNamedPipe` returning `ERROR_IO_PENDING` (no client waiting
        yet) rather than raising it -- the ordinary case, per `pywin32`'s own
        convention for this call (see the test above). `accept`'s `start`
        closure must pass this return value through too, or every ordinary
        accept would be misread as already complete rather than waited on."""
        from murmly.win_pipe import ERROR_IO_PENDING, NamedPipeServer

        instances = iter(["first", "second"])

        def fake_create(pipe_name: str, *, first_instance: bool) -> str:
            return next(instances)

        def connect_named_pipe(handle: object, overlapped: object) -> int:
            return ERROR_IO_PENDING

        waited = []

        def wait_for_single_object(event: object, timeout_ms: int) -> int:
            waited.append(timeout_ms)
            return 0

        def get_overlapped_result(handle: object, overlapped: object, wait: bool) -> int:
            self.assertTrue(wait)
            return 0

        with patch("murmly.win_pipe.create_named_pipe_server", side_effect=fake_create), \
                _install_fake_win32(
                    connect_named_pipe=connect_named_pipe,
                    wait_for_single_object=wait_for_single_object,
                    get_overlapped_result=get_overlapped_result,
                ):
            server = NamedPipeServer(r"\\.\pipe\murmly-test")
            connection, _address = server.accept()

        self.assertEqual("first", connection.handle)
        self.assertEqual(1, len(waited))

    def test_accept_retries_after_error_no_data(self) -> None:
        from murmly.win_pipe import ERROR_NO_DATA, ERROR_PIPE_CONNECTED, NamedPipeServer

        instances = iter(["first", "second", "third"])
        # Shared with `close_handle` below: the one record of which of the
        # two happened first, each time a dead instance is replaced. Order
        # matters -- see `accept`'s own docstring on this race -- so this
        # tracks interleaving, not merely that both eventually happened.
        order = []

        def fake_create(pipe_name: str, *, first_instance: bool) -> str:
            order.append("create")
            return next(instances)

        connect_calls = []

        def connect_named_pipe(handle: object, overlapped: object) -> None:
            connect_calls.append(handle)
            if handle == "first":
                # A client connected and disconnected again before this
                # `ConnectNamedPipe` reached the kernel -- the third
                # documented race for this call.
                raise _FakeWin32Error(ERROR_NO_DATA, "ConnectNamedPipe", "no data")
            raise _FakeWin32Error(ERROR_PIPE_CONNECTED, "ConnectNamedPipe", "connected")

        # `close_handle` also receives `_run_overlapped`'s own per-call
        # cleanup of `overlapped.hEvent` (an opaque sentinel object, never a
        # string) -- filtered out here so this only tracks the pipe-instance
        # closes `accept()` itself is responsible for.
        closed = []

        def close_handle(handle: object) -> None:
            if isinstance(handle, str):
                closed.append(handle)
                order.append("close")

        with patch("murmly.win_pipe.create_named_pipe_server", side_effect=fake_create), \
                _install_fake_win32(
                    connect_named_pipe=connect_named_pipe,
                    close_handle=close_handle,
                ):
            server = NamedPipeServer(r"\\.\pipe\murmly-test")
            connection, _address = server.accept()

        self.assertEqual(["first", "second"], connect_calls)
        self.assertEqual(["first"], closed)
        self.assertEqual("second", connection.handle)
        # The replacement for the dead "first" instance is created before
        # "first" itself is closed -- never the reverse, which would leave
        # the pipe name briefly held by no instance at all (see `accept`'s
        # own docstring for why that window matters).
        self.assertEqual(["create", "create", "close", "create"], order)


class ConnectNamedPipeClientTests(unittest.TestCase):
    """Task item 4 plus the client-side fix: `WaitNamedPipe` failing with
    `ERROR_FILE_NOT_FOUND` (the pipe was there a moment ago, per the
    preceding `ERROR_PIPE_BUSY`, and is now gone entirely) is
    `FileNotFoundError`, not the generic `ConnectionRefusedError`
    (ci2-Windows.log: `pywintypes.error: (2, 'WaitNamedPipe', ...)`)."""

    def test_no_pipe_at_all_is_file_not_found(self) -> None:
        from murmly.win_pipe import ERROR_FILE_NOT_FOUND, connect_named_pipe_client

        def create_file(*args: object, **kwargs: object) -> object:
            raise _FakeWin32Error(ERROR_FILE_NOT_FOUND, "CreateFile", "not found")

        with _install_fake_win32(create_file=create_file):
            with self.assertRaises(FileNotFoundError):
                connect_named_pipe_client(r"\\.\pipe\murmly-test", 1.0)

    def test_busy_then_wait_then_success(self) -> None:
        from murmly.win_pipe import ERROR_PIPE_BUSY, NamedPipeConnection, connect_named_pipe_client

        calls = {"n": 0}

        def create_file(*args: object, **kwargs: object) -> object:
            calls["n"] += 1
            if calls["n"] == 1:
                raise _FakeWin32Error(ERROR_PIPE_BUSY, "CreateFile", "busy")
            return "handle"

        waited = []

        def wait_named_pipe(pipe_name: str, timeout_ms: int) -> None:
            waited.append((pipe_name, timeout_ms))

        with _install_fake_win32(create_file=create_file, wait_named_pipe=wait_named_pipe):
            connection = connect_named_pipe_client(r"\\.\pipe\murmly-test", 2.5)

        self.assertIsInstance(connection, NamedPipeConnection)
        self.assertEqual("handle", connection.handle)
        self.assertEqual([(r"\\.\pipe\murmly-test", 2500)], waited)

    def test_busy_then_wait_fails_with_file_not_found(self) -> None:
        from murmly.win_pipe import ERROR_FILE_NOT_FOUND, ERROR_PIPE_BUSY, connect_named_pipe_client

        def create_file(*args: object, **kwargs: object) -> object:
            raise _FakeWin32Error(ERROR_PIPE_BUSY, "CreateFile", "busy")

        def wait_named_pipe(pipe_name: str, timeout_ms: int) -> None:
            raise _FakeWin32Error(ERROR_FILE_NOT_FOUND, "WaitNamedPipe", "gone")

        with _install_fake_win32(create_file=create_file, wait_named_pipe=wait_named_pipe):
            with self.assertRaises(FileNotFoundError):
                connect_named_pipe_client(r"\\.\pipe\murmly-test", 1.0)

    def test_busy_then_wait_fails_otherwise_is_connection_refused(self) -> None:
        from murmly.win_pipe import ERROR_PIPE_BUSY, connect_named_pipe_client

        def create_file(*args: object, **kwargs: object) -> object:
            raise _FakeWin32Error(ERROR_PIPE_BUSY, "CreateFile", "busy")

        def wait_named_pipe(pipe_name: str, timeout_ms: int) -> None:
            # ERROR_SEM_TIMEOUT (121): `WaitNamedPipe`'s own real timeout
            # code, distinct from `ERROR_FILE_NOT_FOUND` above.
            raise _FakeWin32Error(121, "WaitNamedPipe", "timeout")

        with _install_fake_win32(create_file=create_file, wait_named_pipe=wait_named_pipe):
            with self.assertRaises(ConnectionRefusedError):
                connect_named_pipe_client(r"\\.\pipe\murmly-test", 1.0)


if __name__ == "__main__":
    unittest.main()
