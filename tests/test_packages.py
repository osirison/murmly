from __future__ import annotations

import unittest

from murmly.packages import (
    PACKAGE_MANAGERS,
    SystemPackages,
    detect_package_manager,
    has_nvidia_gpu,
    system_packages,
    wanted_packages,
)


def which_only(*names: str):
    """A `which` that answers for exactly the named commands."""

    def which(command: str) -> str | None:
        return f"/usr/bin/{command}" if command in names else None

    return which


class DetectPackageManagerTests(unittest.TestCase):
    def test_none_is_found_on_a_machine_with_no_known_manager(self) -> None:
        self.assertIsNone(detect_package_manager(which_only()))

    def test_every_named_manager_is_detected_on_its_own(self) -> None:
        for manager in PACKAGE_MANAGERS:
            with self.subTest(manager=manager):
                self.assertEqual(manager, detect_package_manager(which_only(manager)))

    def test_the_fixed_order_wins_when_more_than_one_is_present(self) -> None:
        # Whatever the real reason two showed up, the same one answers every
        # time rather than depending on `PATH` order.
        self.assertEqual("dnf", detect_package_manager(which_only("apt", "dnf")))


class WantedPackagesTests(unittest.TestCase):
    """Mirrors `setup.sh`'s own `wanted_system_packages()` decision."""

    def test_x11_session_wants_xclip_and_xdotool(self) -> None:
        roles = wanted_packages(wayland=False, plasma=False, speech_output=False)

        self.assertIn("xclip", roles)
        self.assertIn("xdotool", roles)
        self.assertNotIn("wl-clipboard", roles)

    def test_plasma_wayland_wants_layer_shell_and_xdotool_not_wtype(self) -> None:
        roles = wanted_packages(wayland=True, plasma=True, speech_output=False)

        self.assertIn("wl-clipboard", roles)
        self.assertIn("gtk4-layer-shell", roles)
        self.assertIn("xdotool", roles)
        self.assertNotIn("wtype", roles)

    def test_non_plasma_wayland_wants_wtype_not_layer_shell(self) -> None:
        roles = wanted_packages(wayland=True, plasma=False, speech_output=False)

        self.assertIn("wl-clipboard", roles)
        self.assertIn("wtype", roles)
        self.assertNotIn("gtk4-layer-shell", roles)

    def test_speech_output_adds_espeak_ng(self) -> None:
        without = wanted_packages(wayland=False, plasma=False, speech_output=False)
        with_speech = wanted_packages(wayland=False, plasma=False, speech_output=True)

        self.assertNotIn("espeak-ng", without)
        self.assertIn("espeak-ng", with_speech)

    def test_portaudio_is_always_wanted(self) -> None:
        """Task 3.5: capture and playback need it on every session shape."""
        for wayland in (False, True):
            for plasma in (False, True):
                with self.subTest(wayland=wayland, plasma=plasma):
                    self.assertIn(
                        "portaudio",
                        wanted_packages(wayland=wayland, plasma=plasma, speech_output=False),
                    )


class SystemPackagesTests(unittest.TestCase):
    def test_dnf_names_match_setup_shs_own_fedora_list(self) -> None:
        result = system_packages(
            wayland=True, plasma=True, speech_output=True, which=which_only("dnf")
        )

        self.assertEqual("dnf", result.manager)
        self.assertEqual(
            (
                "gtk4",
                "python3-gobject",
                "libX11",
                "libXext",
                "portaudio",
                "wl-clipboard",
                "gtk4-layer-shell",
                "xdotool",
                "espeak-ng",
            ),
            result.names,
        )
        self.assertEqual(
            ("sudo", "dnf", "install", "-y") + result.names,
            result.command,
        )

    def test_apt_names_libportaudio2_for_portaudio(self) -> None:
        """Task 3.5, named explicitly: apt's PortAudio package is libportaudio2."""
        result = system_packages(
            wayland=False, plasma=False, speech_output=False, which=which_only("apt")
        )

        self.assertEqual("apt", result.manager)
        self.assertIn("libportaudio2", result.names)
        self.assertNotIn("portaudio", result.names)

    def test_every_recognised_manager_produces_a_runnable_install_command(self) -> None:
        for manager in PACKAGE_MANAGERS:
            with self.subTest(manager=manager):
                result = system_packages(
                    wayland=False,
                    plasma=False,
                    speech_output=True,
                    which=which_only(manager),
                )

                self.assertEqual("sudo", result.command[0])
                self.assertEqual(manager, result.command[1])
                self.assertTrue(all(name for name in result.names), result.names)
                self.assertTrue(set(result.names).issubset(result.command))

    def test_an_unrecognised_manager_prints_the_role_names_plainly(self) -> None:
        """Task 3.4: what `setup.sh` already does when `dnf` is absent."""
        result = system_packages(
            wayland=False, plasma=False, speech_output=False, which=which_only()
        )

        self.assertIsNone(result.manager)
        self.assertIsNone(result.command)
        self.assertEqual(wanted_packages(wayland=False, plasma=False, speech_output=False), result.names)


class HasNvidiaGpuTests(unittest.TestCase):
    def test_true_when_nvidia_smi_is_on_path(self) -> None:
        self.assertTrue(has_nvidia_gpu(which_only("nvidia-smi")))

    def test_false_when_it_is_not(self) -> None:
        self.assertFalse(has_nvidia_gpu(which_only()))

    def test_nvidia_smi_is_never_executed_only_located(self) -> None:
        calls: list[str] = []

        def which(command: str) -> str | None:
            calls.append(command)
            return "/usr/bin/nvidia-smi" if command == "nvidia-smi" else None

        has_nvidia_gpu(which)

        self.assertEqual(["nvidia-smi"], calls)


if __name__ == "__main__":
    unittest.main()
