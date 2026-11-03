# T6d — the process that performs the work

**Status: implemented, uncommitted and awaiting review.**

## 1. What was missing, and how it stayed missing

T6 built `DispatchWorker.run_once` as *one unit of work* rather than a `while True`, and
said so in its docstring: "Not a daemon … the process that owns the loop - a worker entry
point, a test, or an API request in development - decides the cadence." That was the right
shape. The worker entry point was then never written.

So until T6d:

- `DispatchWorker.run_once` had **no caller in `src/`** at all.
- `workers/` contained only `reaper.py`.
- There were no console scripts, no compose service, no runner task and no API lifespan
  hook for either process.
- A deployment could publish a workflow, start an execution and write a dispatch row that
  **nothing would ever claim**.

Everything T0-T6c built - claiming, leases, the guarded client, credential decryption, the
provider adapter, settling, the reaper, the reconciler - was therefore exercised only by
the test suite.

**Why it was easy to miss.** Phase 3's deliverables list named `workers/step_runner.py`
and `workers/dispatch_reaper.py`, and both of those *concerns* were implemented, just as
classes in `services/dispatch.py` rather than as processes. Marking the bullet done was
defensible file-by-file and wrong deployment-wise, and the roadmap entry said "the only
process is `workers/reaper.py`" - true, and quietly skipping the fact that no process
performed anything. The acceptance criterion that would have caught it,
*"`POST /v1/workflows/{id}/run` … calls a real provider … through `curl`, with no
frontend"*, was correctly left unticked the whole time. It was the only thing still
pointing at this.

## 2. What was built

| Piece | Where |
| --- | --- |
| The loop | `workers/worker.py` — `run_forever`, `work_once`, `WorkResult`, `main` |
| Shared signal handling | `workers/_signals.py` — `install_stop_handlers`, now used by both processes |
| Three settings | `worker_idle_sleep_seconds`, `worker_lease_seconds`, `worker_max_consecutive_errors` |
| Console scripts | `agentworld-worker`, `agentworld-reaper` in `pyproject.toml` |
| Runner tasks | `scripts/api.py worker` / `reaper`, and `npm run api:worker` / `api:reaper` |

Nothing in `services/` changed. The process is a loop around the existing `run_once` and
adds no decision of its own.

## 3. The decisions

**Not in the API lifespan**, for T6a's reasons and one more. A worker inside a
request-serving process competes for the connection pool, dies mid-work in a rolling
deploy and runs once per replica — and here dying mid-work can mean dying *mid-call*,
where a lease expiring with `side_effect_started_at` set costs a human a decision.

**Sleeps only when idle.** A claim that finds work loops straight back; a claim that finds
nothing waits `WORKER_IDLE_SLEEP_SECONDS` (default 2s). A backlog therefore drains at the
speed of the work rather than the speed of the poll. The cost of a shorter interval is one
claim query **per live workspace** per poll, because the outbox is under forced row-level
security and claiming must be workspace-scoped — so the interval is a real operational
knob, not a formality. Polling remains T6a's deliberate choice; notification
infrastructure is still premature.

**Stop by event, never by cancellation.** `SIGINT`/`SIGTERM` set an event the loop checks
*between* dispatches. Cancelling mid-dispatch would abandon a held lease, and for a call
already in flight the reaper could only escalate it to `needs_review`. The handler
installation is best-effort because `add_signal_handler` is unimplemented on Windows,
where `KeyboardInterrupt` reaches `main` and becomes exit 0 instead.

**One client per process.** `GuardedClient` holds a connection pool and its own docstring
asks for one instance per process. Built once, closed in a `finally`.

**An error ceiling, like the reaper's.** `WORKER_MAX_CONSECUTIVE_ERRORS` consecutive
failed turns exits 1 rather than spinning against a database that is refusing it. A
supervisor restarting a process is visible; a process quietly retrying for ever is not.

**Signal handling moved to a shared module.** Two processes wanting identical behaviour is
the case `infra/net`'s docstring makes for one HTTP client: one place to review and one
place that can be wrong. `reaper.py` lost its private copy.

**No compose service.** `infra/compose/docker-compose.yml` runs Postgres and Redis only —
the API itself is not containerised there, it runs through `scripts/api.py dev`. Adding
worker containers to a compose file that does not run the application would describe a
deployment shape that does not exist. The entry points and runner tasks match how
everything else in this repository is started.

## 4. Tests

Nine in `tests/integration/test_worker_process.py`. The service beneath is covered by
`test_dispatch_worker.py`; these are about the things that only exist once there is a
process.

- `test_the_process_performs_a_queued_dispatch` — the gap, closed: run the route, run the
  process, the execution completes.
- `test_an_empty_queue_is_not_a_failure`, `test_once_mode_exits_zero_with_nothing_to_do`,
  `test_once_mode_exits_one_when_a_turn_raises` — the exit codes a supervisor reads.
- `test_the_loop_drains_a_backlog_without_waiting_for_its_poll` — two dispatches with the
  idle sleep set to **thirty seconds**, both finished inside the timeout. A loop that
  slept between dispatches could not pass this.
- `test_the_loop_stops_when_asked` — the stop event captured and set, asserting exit 0.
  Signalling would pass on CI and do nothing on Windows; cancelling would test the thing
  this loop exists to avoid.
- `test_the_loop_gives_up_after_consecutive_failures` — bounded by `wait_for`, so removing
  the ceiling **fails** instead of hanging the suite. That lesson cost two and a half
  hours on T6a and is not being learned twice.
- `test_the_lease_comes_from_settings` — observed while the provider holds the request
  open, the only moment a lease is visible.
- `test_a_second_worker_finds_nothing_while_the_first_holds_the_lease` — two processes,
  each with its own pool and client, which is the deployment shape.

`GuardedClient` is patched to the fake provider's policy throughout, because the
production default blocks loopback — correctly, which is exactly why the patch is needed
rather than a production seam.

## 5. Limitations

- **No `allowed_networks` setting.** The process uses `EgressPolicy()`'s defaults, so a
  deployment that must reach a provider on its own network has nowhere to configure it.
  Noted rather than invented.
- **Polling, with a per-workspace cost.** The idle poll is one claim query per live
  workspace. At many workspaces and a short interval that is real load, and the honest
  answer at that scale is notification rather than a shorter sleep.
- **One dispatch at a time per process.** Concurrency is more processes, not more tasks
  inside one. Simple and safe; it does mean a single slow provider call blocks that
  process.
- **Nothing supervises either process in this repository.** The entry points exist and the
  deployment that runs them does not. `18-T6C-ADVANCEMENT-RECOVERY.md` §8 lists the
  signals worth watching and still has no consumer.
