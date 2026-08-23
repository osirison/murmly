## 1. The exit boundary

- [x] 1.1 Add a patchable module-level seam in `src/murmly/cli.py` that performs the hard exit, so the daemon branch is testable without the test process leaving with it
- [x] 1.2 Call it in the daemon branch **after** `_run_daemon` returns, passing the status `_run_daemon` determined — not inside `_run_daemon`, which two test classes call in-process expecting a return
- [x] 1.3 Flush `sys.stdout` and `sys.stderr` and run `logging.shutdown()` before leaving, since `os._exit` skips the flushing finalization would have done
- [x] 1.4 Comment the boundary with what it skips and why, naming the PipeWire thread lifetime rather than only the symptom

## 2. Tests

- [x] 2.1 The daemon branch calls the exit seam with the status `_run_daemon` returned, for a clean stop, a startup refusal, and an unhandled error
- [x] 2.2 Every command that is not the daemon returns through the ordinary path and does not reach the seam
- [x] 2.3 Output written before the exit is flushed — assert against a real stream rather than a mock, so a missing flush actually fails
- [x] 2.4 `_run_daemon` still returns its int to a direct caller, keeping `DaemonExitTeardownTests` and `UnhandledFailureTests` working unchanged

## 3. Verification against the crash

- [x] 3.1 Keep the standalone reproduction as a check that does not depend on the suite's exit code: real PortAudio plus a loaded `onnxruntime` plus the teardown unregistered exits 139 without the fix and 0 with it
- [x] 3.2 Row one of the acceptance matrix — stop the daemon with the audio server **alive**, confirm exit 0, no core dump, and no `Failed with result 'core-dump'` in the journal
- [x] 3.3 Row two — stop the daemon with the audio server **already gone**, using the private-PipeWire harness in `docs/agent-notes/portaudio-jack-exit-abort.md`, confirm exit 0 and no core. Issue #11 fixed this row and broke row one; a fix verified against one row repeats that trade
- [x] 3.4 Run the full suite and confirm it still exits 0
- [x] 3.5 Confirm `coredumpctl list` records no new core for the daemon across both rows

## 4. Correcting what is now known wrong

- [x] 4.1 Correct the docstring of `disable_portaudio_exit_teardown` in `src/murmly/audio.py`: "there is nothing else the teardown does that the kernel does not do when the process exits" is true of the host-API disconnect and false of stopping the threads before the interpreter unloads the code under them
- [x] 4.2 Correct the same claim in `docs/agent-notes/portaudio-jack-exit-abort.md`, and record the SIGSEGV that replaced the SIGABRT alongside it so the next reader sees both halves
- [x] 4.3 Record the reproduction recipe in that note — which loaded modules are required, and that streams are irrelevant — since it is not derivable from the crash alone
- [x] 4.4 Close #27 referencing the change, and note that #29 fixed only the test-suite half
