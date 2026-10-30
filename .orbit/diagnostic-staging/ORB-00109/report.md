# ORB-00109 — confirmed lifecycle/recovery defects (unpatched)

## Scope and execution
Production was read only. Actual `manager_app.py` and `adapters.py` were imported with only missing web dependencies stubbed; adapter constructors/model loaders were never invoked. Original worker `main` and `Worker.deactivate` were AST-extracted unchanged and executed with mock protocol/model/CUDA dependencies. `subprocess.Popen`, `run`, and `check_output` were forbidden during test execution; no GPU, live synthesis, service restart, process termination, or voice-parameter change. Bytecode writing disabled. Source SHA256 before/after matches are in evidence.json.

Commands:
```
python3 /home/rugent/tts-cuda-investigation-20261009/lifecycle/test_lifecycle.py
python3 /home/rugent/tts-cuda-investigation-20261009/lifecycle/test_lifecycle.py --require-recovery
```
Final execution: 10 characterization tests PASS (exit 0); same suite with required-recovery assertions has 3 expected failures (exit 1): absent fatal classification, alive poison not replaced, request dispatched while offloading. This is a reproducible defect baseline, NOT a repaired implementation or passing future acceptance suite.

## Confirmed failure modes
1. **Alive/error TTS blocks unrelated retrieval indefinitely.** Actual adapter receives mock `ok:false,state:error` synth response, remains alive. Manager catch only restarts `workers[j.profile]` when dead (manager 60–63). Retrieval switches first call old TTS deactivation (30–37); failed old deactivation escapes outside activation try (39–46), retaining `active_profile=tts,transition=deactivating,state=error`. Repeated retrieval bodies never execute; actual embeddings handler maps the failure to HTTP 503. It checks/restarts the requested retrieval worker, not the failing old TTS owner. See evidence `alive_error_blocks_retrieval`.
2. **Idle cannot recover an alive poisoned owner.** Idle health recovery checks alive only (69–74). Failed idle offload retains TTS ownership and sets transition error (75–81). The test supplies an expired last-use timestamp; no restart occurs. In addition, if the first job fails without recording last_used, its fallback to current time prevents idle expiry altogether (75, last_used updated only at 56).
3. **Same-profile requests bypass health/state checks.** `_activate` immediately returns for matching active_profile (31). Another TTS request reaches synth in error, with no activation/replacement.
4. **Idle lifecycle races with queue execution.** Event barriers place idle inside deactivation after its active_job/queue checks. Queue then dispatches same-profile work while adapter state is offloading and skips activation; idle subsequently clears ownership/state. The fake callback intentionally returns successfully to expose manager mutation, not to claim real synthesis would succeed. Real per-RPC locking (adapters 19) serializes messages, but does NOT make idle's check/offload and manager check/activate/job an atomic lifecycle transaction: a real synth may run after offload and fail not-ready. No claim of simultaneous GPU kernels is made. A separate test shows idle restart proceeds even with active_job populated (crash branch has no busy guard).
5. **Generic CUDA exception leaves worker apparently GPU-ready.** Actual `main` generic exception branch (worker 351–353) logs and sends existing state, and continues accepting the next state operation; mock device-side assertion returns `gpu_ready` and a subsequent state reply succeeds. Actual `Worker.deactivate` sets error if mock move_model fails (214–216). This reproduces the recovery path; no actual CUDA assertion was induced.
6. **Diagnostic loss.** RPC subprocess stderr is DEVNULL (adapters 6); worker traceback logging is therefore discarded through this path. TTS synth failure updates state but not `last` (67), so initial synth-error details are absent from adapter health until another operation updates last. Tests confirm the latter; DEVNULL is direct source evidence, not a spawned process test.
7. **Idle health exceptions are unhandled.** Injected health error escapes `_idle_loop` (69), which would terminate that background thread. This proves an uncovered failure path, NOT that it caused this incident. Current TTS health is cached state plus process poll, not a state RPC; lifecycle races are in state/restart/RPC ownership, not an alleged health RPC absent from this source.

## Original CUDA trigger remains unknown
These tests prove manager recovery and serialization defects, not why the first kernel failed. They use an injected exception message. No incident kernel traceback, tokenizer/model tensor bounds, CUDA launch trace, or original failing input has been established here. Do not attribute it to a seed, voice parameters, text chunk, or cache without separate evidence. Recent Git history could not be checked: `/home/rugent/rtx3050-profile-prototype` is not a Git repository. Python environment lacks fastapi/pydantic/pytest, handled with web-only stubs and standard-library unittest, without installing anything.

## Minimum proposed patch — NOT applied
See proposed-minimum-patch.md. Acceptance after implementation should replace characterization assertions with positive invariants and include late RPC replies, retirement timeout, no duplicate recovery, failed replacement startup, shutdown overlap, and dead-worker controls. Production remains unchanged.

## Artifacts
Local directory: `/home/rugent/tts-cuda-investigation-20261009/lifecycle/`
- test_lifecycle.py
- test-output.txt and red-output.txt
- evidence.json and red-evidence.json (source hashes and snapshots)
- report.md and proposed-minimum-patch.md
