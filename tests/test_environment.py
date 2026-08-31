from __future__ import annotations

from pathlib import Path
import subprocess
import unittest

from murmly.environment import (
    EnvironmentSyncError,
    ONNXRUNTIME_GPU_VERSION,
    SyncPlan,
    current_extras,
    install_system_packages,
    installed_package,
    make_confirm,
    refuse_before_sync,
    refuse_or_warn_environment_preconditions,
    resolve_extras,
    swap_in_gpu_onnxruntime,
    sync_arguments,
    sync_environment,
    wants_speech_output,
)
from murmly.platform import OperatingSystem, PlatformProfile


PROJECT = Path("/tmp/murmly-environment-tests-fake-project")


def profile(
    operating_system: OperatingSystem = OperatingSystem.LINUX,
    architecture: str = "x86_64",
    libc: str | None = "glibc",
) -> PlatformProfile:
    return PlatformProfile(operating_system=operating_system, architecture=architecture, libc=libc)


class FakeRunCommand:
    """Records every command it was asked to run and answers per-prefix.

    `answers` maps a leading tuple of argv to the `returncode` a matching call
    should get back; a call matching no configured prefix succeeds (0), which
    is what most of `uv`'s own commands do when nothing is testing their
    failure path specifically.
    """

    def __init__(self, answers: dict[tuple[str, ...], int] | None = None) -> None:
        self.answers = answers or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        for prefix, returncode in self.answers.items():
            if tuple(argv[: len(prefix)]) == prefix:
                return subprocess.CompletedProcess(argv, returncode)
        return subprocess.CompletedProcess(argv, 0)


class RefuseBeforeSyncTests(unittest.TestCase):
    def test_none_on_a_supported_machine_with_a_runtime(self) -> None:
        self.assertIsNone(refuse_before_sync(profile()))

    def test_names_the_platform_and_what_is_supported_on_macos(self) -> None:
        message = refuse_before_sync(profile(operating_system=OperatingSystem.MACOS, libc=None))
        assert message is not None
        self.assertIn("macos", message)
        self.assertIn("linux", message)

    def test_names_the_runtime_and_characteristic_for_a_musl_machine(self) -> None:
        message = refuse_before_sync(profile(libc="musl"))
        assert message is not None
        self.assertIn("ctranslate2", message)
        self.assertIn("musl", message)
        self.assertNotIn("Library", message)  # never the runtime's own load error

    def test_refuses_before_any_command_would_have_run(self) -> None:
        # The property task 16.3 actually asks for: nothing is run yet by the
        # time the refusal is decided. `refuse_before_sync` never takes a
        # `run_command` at all, so a machine this refuses never reaches one.
        self.assertTrue(refuse_before_sync(profile(libc="musl")))


class RefuseOrWarnEnvironmentPreconditionsTests(unittest.TestCase):
    def test_linux_has_nothing_to_check(self) -> None:
        messages: list[str] = []
        self.assertIsNone(refuse_or_warn_environment_preconditions(profile(), messages.append))
        self.assertEqual([], messages)

    def test_windows_with_unreadable_registry_warns_and_proceeds(self) -> None:
        # On this test runner (Linux), the real registry reader raises, which
        # `environment_preconditions_for` already coerces to `satisfied: None`
        # -- neither precondition can be refused on that silence (11.5/11.6's
        # own "never claim a denial from silence either" rule).
        messages: list[str] = []
        result = refuse_or_warn_environment_preconditions(
            profile(operating_system=OperatingSystem.WINDOWS, libc=None), messages.append
        )
        self.assertIsNone(result)
        self.assertTrue(any("long" in message for message in messages))
        self.assertTrue(any("Developer Mode" in message for message in messages))

    def test_refuses_when_long_paths_are_off(self) -> None:
        # `_windows_long_paths_enabled`'s default registry reader is bound at
        # definition time, so nothing patchable on this Linux runner can make
        # a *real* Windows read answer `False` here -- a constructed table is
        # what actually exercises the refusal branch (11.5's own checkbox).
        from murmly.platform import EnvironmentPrecondition, WINDOWS_DEVELOPER_MODE, WINDOWS_LONG_PATHS

        table = {
            WINDOWS_LONG_PATHS: EnvironmentPrecondition(
                name=WINDOWS_LONG_PATHS,
                description="long paths are needed",
                remedy="enable Win32 long paths",
                check=lambda _profile: False,
            ),
        }
        messages: list[str] = []
        result = refuse_or_warn_environment_preconditions(
            profile(operating_system=OperatingSystem.WINDOWS, libc=None), messages.append, table
        )
        assert result is not None
        self.assertIn("long paths are needed", result)
        self.assertIn("enable Win32 long paths", result)
        self.assertEqual([], messages)  # a refusal, not also a warning

    def test_warns_but_proceeds_when_only_developer_mode_is_off(self) -> None:
        from murmly.platform import EnvironmentPrecondition, WINDOWS_DEVELOPER_MODE, WINDOWS_LONG_PATHS

        table = {
            WINDOWS_LONG_PATHS: EnvironmentPrecondition(
                name=WINDOWS_LONG_PATHS, description="long paths are needed", remedy="enable them",
                check=lambda _profile: True,
            ),
            WINDOWS_DEVELOPER_MODE: EnvironmentPrecondition(
                name=WINDOWS_DEVELOPER_MODE, description="doubles the model cache", remedy="enable Developer Mode",
                check=lambda _profile: False,
            ),
        }
        messages: list[str] = []
        result = refuse_or_warn_environment_preconditions(
            profile(operating_system=OperatingSystem.WINDOWS, libc=None), messages.append, table
        )
        self.assertIsNone(result)
        self.assertTrue(any("doubles the model cache" in message for message in messages))

    def test_proceeds_silently_when_everything_is_satisfied(self) -> None:
        from murmly.platform import EnvironmentPrecondition, WINDOWS_DEVELOPER_MODE, WINDOWS_LONG_PATHS

        table = {
            WINDOWS_LONG_PATHS: EnvironmentPrecondition(
                name=WINDOWS_LONG_PATHS, description="d", remedy="r", check=lambda _profile: True,
            ),
            WINDOWS_DEVELOPER_MODE: EnvironmentPrecondition(
                name=WINDOWS_DEVELOPER_MODE, description="d", remedy="r", check=lambda _profile: True,
            ),
        }
        messages: list[str] = []
        result = refuse_or_warn_environment_preconditions(
            profile(operating_system=OperatingSystem.WINDOWS, libc=None), messages.append, table
        )
        self.assertIsNone(result)
        self.assertEqual([], messages)


class InstallSystemPackagesTests(unittest.TestCase):
    def test_no_recognised_manager_prints_plainly_and_runs_nothing(self) -> None:
        announced: list[str] = []
        runner = FakeRunCommand()
        install_system_packages(
            profile(),
            speech_output=False,
            confirm=lambda _prompt: True,
            announce=announced.append,
            which=lambda _name: None,
            run_command=runner,
        )
        self.assertEqual([], runner.calls)
        self.assertTrue(any("No recognised package manager" in message for message in announced))

    def test_a_recognised_manager_is_offered_and_run_on_confirmation(self) -> None:
        # `rpm -q` answering non-zero is a machine with none of them installed.
        runner = FakeRunCommand({("rpm", "-q"): 1})
        install_system_packages(
            profile(),
            speech_output=True,
            confirm=lambda _prompt: True,
            announce=lambda _message: None,
            which=lambda name: "/usr/bin/dnf" if name == "dnf" else None,
            run_command=runner,
        )
        installs = [call for call in runner.calls if call[:1] == ["sudo"]]
        self.assertEqual(1, len(installs))
        self.assertEqual(["sudo", "dnf", "install", "-y"], installs[0][:4])
        self.assertIn("espeak-ng", installs[0])

    def test_declining_the_offer_runs_nothing(self) -> None:
        runner = FakeRunCommand({("dpkg-query", "-W"): 1})
        install_system_packages(
            profile(),
            speech_output=False,
            confirm=lambda _prompt: False,
            announce=lambda _message: None,
            which=lambda name: "/usr/bin/apt" if name == "apt" else None,
            run_command=runner,
        )
        self.assertEqual([], [call for call in runner.calls if call[:1] == ["sudo"]])

    def test_a_machine_that_already_has_them_is_asked_for_nothing(self) -> None:
        """What `setup.sh` did against `rpm -q`, and what generalising the
        step past `dnf` briefly lost.

        Without this, every `install` and `upgrade` offers the whole list --
        and `--yes` runs it, so an upgrade that needed no `sudo` at all
        acquires a `sudo` prompt and a package manager invocation. The
        `confirm` here fails the test if it is ever reached, because being
        asked at all is the defect.
        """
        # Every query exits 0: the default. Nothing is missing.
        runner = FakeRunCommand()
        announced: list[str] = []

        install_system_packages(
            profile(),
            speech_output=True,
            confirm=lambda prompt: self.fail(f"should not have been asked: {prompt}"),
            announce=announced.append,
            which=lambda name: "/usr/bin/dnf" if name == "dnf" else None,
            run_command=runner,
        )

        self.assertEqual([], [call for call in runner.calls if call[:1] == ["sudo"]])
        self.assertTrue(any("already installed" in message for message in announced))

    def test_only_the_absent_packages_are_offered(self) -> None:
        """The offer names what is missing, not everything a session wants."""
        runner = FakeRunCommand()

        def answer(argv, **kwargs):
            runner.calls.append(list(argv))
            if tuple(argv[:2]) == ("rpm", "-q"):
                # Only espeak-ng is absent.
                return subprocess.CompletedProcess(argv, 0 if argv[-1] != "espeak-ng" else 1)
            return subprocess.CompletedProcess(argv, 0)

        install_system_packages(
            profile(),
            speech_output=True,
            confirm=lambda _prompt: True,
            announce=lambda _message: None,
            which=lambda name: "/usr/bin/dnf" if name == "dnf" else None,
            run_command=answer,
        )

        [install] = [call for call in runner.calls if call[:1] == ["sudo"]]
        self.assertEqual(["sudo", "dnf", "install", "-y", "espeak-ng"], install)


class InstalledPackageAndExtrasTests(unittest.TestCase):
    def test_installed_package_is_true_only_on_a_zero_exit(self) -> None:
        present = FakeRunCommand()
        absent = FakeRunCommand({("uv", "pip", "show"): 1})
        self.assertTrue(installed_package(PROJECT, "kokoro-onnx", present))
        self.assertFalse(installed_package(PROJECT, "kokoro-onnx", absent))

    def test_current_extras_reports_cuda_only_when_cublas_is_present(self) -> None:
        self.assertEqual((), current_extras(PROJECT, FakeRunCommand({("uv", "pip", "show"): 1})))
        self.assertEqual(("cuda",), current_extras(PROJECT, FakeRunCommand()))


class WantsSpeechOutputTests(unittest.TestCase):
    def test_explicit_flags_win_outright(self) -> None:
        self.assertTrue(wants_speech_output("yes", venv_exists=True, kokoro_installed=False))
        self.assertFalse(wants_speech_output("no", venv_exists=False, kokoro_installed=True))

    def test_auto_keeps_a_deliberate_opt_out(self) -> None:
        self.assertFalse(wants_speech_output("auto", venv_exists=True, kokoro_installed=False))

    def test_auto_defaults_to_on_for_a_fresh_or_already_opted_in_machine(self) -> None:
        self.assertTrue(wants_speech_output("auto", venv_exists=False, kokoro_installed=False))
        self.assertTrue(wants_speech_output("auto", venv_exists=True, kokoro_installed=True))


class ResolveExtrasTests(unittest.TestCase):
    def test_no_cuda_removes_it_even_if_already_installed(self) -> None:
        result = resolve_extras(
            current=("cuda",), want_cuda="no", has_nvidia_driver=True,
            confirm=lambda _p: True, announce=lambda _m: None,
        )
        self.assertEqual((), result)

    def test_cuda_flag_adds_it_even_without_a_driver(self) -> None:
        result = resolve_extras(
            current=(), want_cuda="yes", has_nvidia_driver=False,
            confirm=lambda _p: False, announce=lambda _m: None,
        )
        self.assertEqual(("cuda",), result)

    def test_auto_keeps_an_installed_extra_without_asking(self) -> None:
        asked = []
        result = resolve_extras(
            current=("cuda",), want_cuda="auto", has_nvidia_driver=True,
            confirm=lambda p: asked.append(p) or True, announce=lambda _m: None,
        )
        self.assertEqual(("cuda",), result)
        self.assertEqual([], asked)

    def test_auto_offers_it_once_when_a_driver_is_found_and_not_yet_installed(self) -> None:
        announced: list[str] = []
        result = resolve_extras(
            current=(), want_cuda="auto", has_nvidia_driver=True,
            confirm=lambda _p: True, announce=announced.append,
        )
        self.assertEqual(("cuda",), result)
        self.assertTrue(any("NVIDIA driver" in message for message in announced))

    def test_auto_declines_leaves_it_out(self) -> None:
        result = resolve_extras(
            current=(), want_cuda="auto", has_nvidia_driver=True,
            confirm=lambda _p: False, announce=lambda _m: None,
        )
        self.assertEqual((), result)

    def test_auto_with_no_driver_never_asks(self) -> None:
        asked = []
        result = resolve_extras(
            current=(), want_cuda="auto", has_nvidia_driver=False,
            confirm=lambda p: asked.append(p) or True, announce=lambda _m: None,
        )
        self.assertEqual((), result)
        self.assertEqual([], asked)


class SyncArgumentsTests(unittest.TestCase):
    def test_no_extras_no_tts_flag(self) -> None:
        self.assertEqual((), sync_arguments(SyncPlan(extras=(), include_tts=True)))

    def test_cuda_extra_is_named(self) -> None:
        self.assertEqual(("--extra", "cuda"), sync_arguments(SyncPlan(extras=("cuda",), include_tts=True)))

    def test_tts_left_out_adds_no_group(self) -> None:
        self.assertEqual(("--no-group", "tts"), sync_arguments(SyncPlan(extras=(), include_tts=False)))


class SwapInGpuOnnxruntimeTests(unittest.TestCase):
    def test_uninstalls_then_reinstalls_with_the_pinned_version(self) -> None:
        runner = FakeRunCommand()
        swap_in_gpu_onnxruntime(PROJECT, runner, lambda _m: None, "installing it")
        self.assertEqual(2, len(runner.calls))
        self.assertEqual(["uv", "pip", "uninstall", "--project", str(PROJECT), "onnxruntime"], runner.calls[0])
        install_call = runner.calls[1]
        self.assertIn("--reinstall", install_call)
        self.assertIn(f"onnxruntime-gpu=={ONNXRUNTIME_GPU_VERSION}", install_call)

    def test_uninstall_failing_does_not_stop_the_reinstall(self) -> None:
        runner = FakeRunCommand({("uv", "pip", "uninstall"): 1})
        swap_in_gpu_onnxruntime(PROJECT, runner, lambda _m: None, "installing it")
        self.assertEqual(2, len(runner.calls))

    def test_a_failing_reinstall_raises(self) -> None:
        runner = FakeRunCommand({("uv", "pip", "install"): 1})
        with self.assertRaises(EnvironmentSyncError):
            swap_in_gpu_onnxruntime(PROJECT, runner, lambda _m: None, "installing it")


class SyncEnvironmentTests(unittest.TestCase):
    """The composed 16.1 flow, driven end to end against a fake `uv`."""

    def test_carries_forward_an_already_installed_extra_without_prompting(self) -> None:
        # cuda already present: `uv pip show` for nvidia-cublas-cu12 succeeds.
        runner = FakeRunCommand()
        announced: list[str] = []
        asked: list[str] = []
        plan = sync_environment(
            PROJECT,
            want_cuda="auto",
            want_tts="auto",
            confirm=lambda p: asked.append(p) or True,
            announce=announced.append,
            run_command=runner,
            which=lambda _c: None,
        )
        self.assertEqual(("cuda",), plan.extras)
        self.assertEqual([], asked)  # never asked: it was already there
        sync_call = next(call for call in runner.calls if call[:2] == ["uv", "sync"])
        self.assertIn("--extra", sync_call)
        self.assertIn("cuda", sync_call)

    def test_opting_out_of_tts_before_is_preserved_on_upgrade(self) -> None:
        # `.venv` exists and kokoro-onnx is not installed: a deliberate opt-out.
        answers = {("uv", "pip", "show", "--project", str(PROJECT), "kokoro-onnx"): 1}
        runner = FakeRunCommand(answers)
        plan = sync_environment(
            PROJECT,
            want_cuda="no",
            want_tts="auto",
            confirm=lambda _p: True,
            announce=lambda _m: None,
            run_command=runner,
            which=lambda _c: None,
            venv_exists=lambda _project_dir: True,
        )

        self.assertFalse(plan.include_tts)
        sync_call = next(call for call in runner.calls if call[:2] == ["uv", "sync"])
        self.assertIn("--no-group", sync_call)
        self.assertIn("tts", sync_call)

    def test_explicit_tts_flag_overrides_a_previous_opt_out(self) -> None:
        answers = {("uv", "pip", "show", "--project", str(PROJECT), "kokoro-onnx"): 1}
        runner = FakeRunCommand(answers)
        plan = sync_environment(
            PROJECT,
            want_cuda="no",
            want_tts="yes",
            confirm=lambda _p: True,
            announce=lambda _m: None,
            run_command=runner,
            which=lambda _c: None,
        )
        self.assertTrue(plan.include_tts)

    def test_restores_the_gpu_swap_when_it_was_installed_before(self) -> None:
        answers = {("uv", "pip", "show", "--project", str(PROJECT), "onnxruntime-gpu"): 0}
        runner = FakeRunCommand(answers)
        announced: list[str] = []
        sync_environment(
            PROJECT,
            want_cuda="yes",
            want_tts="auto",
            confirm=lambda _p: True,
            announce=announced.append,
            run_command=runner,
            which=lambda _c: None,
        )
        self.assertTrue(any("--reinstall" in " ".join(call) for call in runner.calls))
        self.assertTrue(any("restoring it" in message for message in announced))

    def test_a_failing_sync_raises_and_never_attempts_the_swap(self) -> None:
        runner = FakeRunCommand({("uv", "sync",): 1})
        with self.assertRaises(EnvironmentSyncError):
            sync_environment(
                PROJECT,
                want_cuda="yes",
                want_tts="auto",
                confirm=lambda _p: True,
                announce=lambda _m: None,
                run_command=runner,
                which=lambda _c: None,
            )
        self.assertFalse(any("reinstall" in " ".join(call) for call in runner.calls))

    def test_gpu_build_present_without_cuda_extra_warns_rather_than_restoring(self) -> None:
        answers = {
            ("uv", "pip", "show", "--project", str(PROJECT), "onnxruntime-gpu"): 0,
            ("uv", "pip", "show", "--project", str(PROJECT), "nvidia-cublas-cu12"): 1,
        }
        runner = FakeRunCommand(answers)
        announced: list[str] = []
        sync_environment(
            PROJECT,
            want_cuda="no",
            want_tts="auto",
            confirm=lambda _p: True,
            announce=announced.append,
            run_command=runner,
            which=lambda _c: None,
        )
        self.assertFalse(any(call[:3] == ["uv", "pip", "install"] for call in runner.calls))
        self.assertTrue(any("was not restored" in message for message in announced))


class MakeConfirmTests(unittest.TestCase):
    """Task 16.6: decline, never assume, with nothing attached to answer."""

    def test_assume_yes_answers_true_without_reading_anything(self) -> None:
        confirm = make_confirm(True, announce=lambda _m: None, isatty=lambda: (_ for _ in ()).throw(AssertionError))
        self.assertTrue(confirm("Proceed?"))

    def test_no_terminal_and_no_yes_declines(self) -> None:
        confirm = make_confirm(False, announce=lambda _m: None, isatty=lambda: False)
        self.assertFalse(confirm("Proceed?"))

    def test_no_terminal_warns_what_would_make_it_proceed(self) -> None:
        announced: list[str] = []
        confirm = make_confirm(False, announce=announced.append, isatty=lambda: False)
        confirm("Proceed?")
        self.assertTrue(any("--yes" in message for message in announced))

    def test_a_terminal_reads_an_actual_answer(self) -> None:
        confirm = make_confirm(False, announce=lambda _m: None, isatty=lambda: True, read_line=lambda _p: "y")
        self.assertTrue(confirm("Proceed?"))

    def test_a_terminal_declining_returns_false(self) -> None:
        confirm = make_confirm(False, announce=lambda _m: None, isatty=lambda: True, read_line=lambda _p: "n")
        self.assertFalse(confirm("Proceed?"))

    def test_end_of_input_on_a_terminal_declines_rather_than_raising(self) -> None:
        def raise_eof(_prompt: str) -> str:
            raise EOFError

        confirm = make_confirm(False, announce=lambda _m: None, isatty=lambda: True, read_line=raise_eof)
        self.assertFalse(confirm("Proceed?"))


if __name__ == "__main__":
    unittest.main()
