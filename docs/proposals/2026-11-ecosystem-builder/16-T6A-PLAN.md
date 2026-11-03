# T6a — Operational hardening of the dispatch worker

> Design and test plan. **Nothing implemented yet.** Produced after inspecting the T0–T6
> code and measuring the behaviour that matters, so the proposal below is "the smallest safe
> change" rather than a list of everything that could be built.

---

## 1. What the inspection found

Four of the seven priorities need less work than they look like, and one needs more.

| Priority | State today | Change needed |
|---|---|---|
| 1. Automatic reaper scheduling | **nothing runs it.** `workers/` is an empty package; the API lifespan starts no task; `sweep()` is called only by tests | a runner — §3 |
| 2. Multiple schedulers safe | **already safe, measured** — §2 | a test, not a mechanism |
| 3. No repeated uncertain side effects | already implemented and tested (`needs_review`, never retried) | one more test — §6 |
| 4. Attempt history and auditable recovery | **lost.** `reclaim` overwrites `last_error`/`last_error_code` and nothing records a recovery anywhere | an audit row — §4 |
| 5. `Retry-After` behaviour | **parsed and ignored.** `ProviderError.retry_after_seconds` is populated by the adapter and read by nothing | honour it, clamped — §5 |
| 6. `needs_review` lifecycle | reachable, and resolvable by nobody: no endpoint, no action in the matrix, no listing | design in §7; implementation is a decision for you |
| 7. Keep polling | polling, per workspace, rotating offset | no change |

### 1.1 Measured, not reasoned

Four concurrent `LeaseReaper.sweep()` calls against one expired lease, through the
application role against the development database:

```
reaper 0: (1, 0)      <- returned_to_pending, escalated
reaper 1: (0, 0)
reaper 2: (0, 0)
reaper 3: (0, 0)
final row: status=pending leased_by=None claim_count=0 code=lease_expired
```

Exactly one recovery. Two mechanisms already present produce that:

- `expire_leases` selects with `FOR UPDATE SKIP LOCKED`, so a second reaper does not see a
  row the first has locked — it skips it rather than blocking;
- `reclaim` is guarded on `status = 'leased' AND leased_until < now()`, so even a reaper
  that somehow reached the same row after the first committed changes nothing.

Both are inside one transaction per workspace, so the lock is held until the recovery
commits. **Priority 2 therefore needs no new mechanism** — it needs a test that fails if
either guard is removed, which the non-vacuity pass will confirm.

The probe created its own workspace and deleted it; the development database is back to one
workspace, one project, zero dispatches.

---

## 2. Priority 2: a test, and explicitly not a lock

An advisory lock (`pg_try_advisory_lock`) around the sweep would stop N reapers *scanning*
at once. That is efficiency, not safety, and it buys nothing until the scan is expensive —
at which point the right answer is notification rather than a lock on a scan nobody needs to
run. Proposing **no lock**, and saying so in the document so the absence is a decision
rather than an omission.

---

## 3. Priority 1: how the reaper gets run

Three shapes, and the trade is deployment coupling against operational simplicity.

| Option | For | Against |
|---|---|---|
| **(a) API lifespan task** | zero deployment change; works locally with no extra process | the reaper competes with request handling for the connection pool, dies in a rolling deploy mid-sweep, and runs once per replica — safe per §1.1, but N times the scanning |
| **(b) A worker process** — `python -m agentworld.workers.reaper` | one job, its own pool, restartable independently; the shape T6's `DispatchWorker` already wants | a deployment unit that does not exist yet |
| **(c) External scheduler** calling a one-shot command | no long-lived process; the platform owns the schedule and the retries | needs cron/k8s CronJob configured; a missed tick is invisible unless the platform alerts |

**Proposal: (b) with a one-shot mode, which is also (c).** One module, two entry points:

```
python -m agentworld.workers.reaper            # loop, sleeping between sweeps
python -m agentworld.workers.reaper --once     # one sweep, exits non-zero on error
```

The loop is what a long-running deployment uses; `--once` is what a CronJob calls. The same
code path, so the cron deployment is not a second implementation.

**Not the API lifespan.** A reaper inside a request-serving process is the option that looks
free and is not: it ties recovery latency to a deploy, and it makes the connection-pool
sizing of the API depend on how many workspaces exist. If local development wants one
process, `--once` from a terminal is one command.

### 3.1 Settings

| Setting | Default | Why |
|---|---|---|
| `REAPER_INTERVAL_SECONDS` | `30` | a quarter of the 120 s lease, so a crashed worker's row moves within about a quarter of the window rather than at the end of it |
| `REAPER_BATCH_LIMIT` | `100` | already the `expire_leases` default; promoted so a backlog can be drained faster without a code change |
| `REAPER_MAX_CONSECUTIVE_ERRORS` | `5` | the loop exits non-zero rather than spinning forever against a database that is refusing it, so a supervisor restarts it and the failure is visible |

All three bounded by `Field(ge=…, le=…)`, like the existing `ingest_*` settings, so a
misconfigured interval fails at boot.

### 3.2 Operational requirements, stated

- **The reaper must run somewhere.** With nothing running it, a crashed worker's dispatch
  stays `leased` forever and its execution never progresses. That is the current state and
  it is the single most important thing this task fixes.
- **One or many is fine.** §1.1. Running two is wasteful, not unsafe.
- **It needs the application role**, not the owner: it reads and updates `dispatches` under
  RLS, per workspace, exactly as the worker does.
- **Interval versus lease.** An interval longer than the lease means a window in which a
  crashed worker's row is expired and nothing has noticed. The default is deliberately well
  under.
- **A missed sweep is not corruption**, only delay: the guards are on the row, not on the
  schedule.

---

## 4. Priority 4: recovery becomes auditable, with no migration

Today `reclaim` writes `last_error = <reason>` and `last_error_code = 'lease_expired'`,
**overwriting** whatever the previous attempt recorded. After two recoveries there is no
trace of the first, and no record anywhere that a recovery happened at all.

**Proposal: one `audit_log` row per recovery.** The table already exists, is workspace
-scoped, needs no project, and — this is the part that makes it the right home — the
application role holds `INSERT, SELECT` and nothing else on it. A recovery record cannot be
edited or deleted by the process that wrote it.

```
actor_type    'system'
action        'dispatch.recovered'
resource_type 'dispatch'
resource_id   <dispatch id>
metadata      {from: 'leased', to: 'pending'|'needs_review'|'dead',
               reason, attempt, claim_count, side_effect_in_flight,
               previous_error, previous_error_code, leased_by}
```

`previous_error` is what preserves the history `reclaim` currently destroys. `attempt` and
`claim_count` are both carried because they answer different questions — which unit of work
this was, and how many times a worker has held it.

Written in the same transaction as the `reclaim`, so a recovery that rolls back leaves no
claim that it happened.

**Why not an event?** `events` is the replayable log, and a reaper recovery is not a fact
about the agent ecosystem — it is a fact about this system's own recovery machinery.
`workflow.step_retried` exists and is deliberately unemitted (T6 §8), because nothing has
been *decided* about the step: the step is still `dispatched` and still waiting. Emitting it
here would record a decision nobody took.

**Why not a new column or table?** A `recoveries` table or a jsonb history column both need
a migration, and `audit_log` already is the append-only record of "something happened to
this resource". The cost is that a recovery is in the audit trail rather than on the row,
so finding one is a query rather than a column read — acceptable for something that should
be rare.

---

## 5. Priority 5: `Retry-After`, defined

Currently the adapter parses it into `ProviderError.retry_after_seconds` and the worker
ignores it entirely, using jittered exponential backoff. Defining the behaviour:

**When the provider sends a usable `Retry-After`, honour it, clamped.**

```
delay = min(max(retry_after, 0), RETRY_AFTER_MAX_SECONDS)     # 900 s, 15 minutes
run_after = now + delay
```

with these constraints:

1. **Only the delta-seconds form.** The HTTP-date form is parsed by nobody — T3 decided
   that, because turning a date into a wait means trusting this host's clock against the
   provider's. An unparseable header means "use your own backoff", which is a safe answer.
2. **Clamped at 15 minutes**, the same ceiling the jittered backoff already uses. A provider
   that sends `Retry-After: 86400` — hostile, misconfigured, or simply honest about a long
   outage — cannot park a tenant's queue for a day. The clamp is the answer to the concern
   raised when this was listed as an open decision.
3. **It does not grant extra attempts.** The retry budget is `max_attempts` and
   `Retry-After` changes only *when* the next attempt happens, never *whether*. A dispatch
   out of attempts is terminal whatever the header says.
4. **It does not extend a lease.** The row is back to `pending` with no lease; the delay is
   `run_after`, which the claim query already filters on.
5. **Never for a `needs_review` row.** An uncertain outcome is not retried at all, so there
   is nothing for a delay to schedule.
6. **No jitter when the header is honoured.** The provider named a time; adding noise to it
   is second-guessing a number we asked for. Jitter stays on the fallback backoff, where
   several workers are guessing the same number and need to not converge.

**This is not T8's retry policy.** It is one clamped number replacing one guessed number, in
the function that already computes `run_after`. T8 still owns classification, the attempt
budget per side-effect class, dead-lettering and the consequential exception.

---

## 6. Priority 3: the test that is missing

The uncertain-outcome path is implemented and covered, but one property is asserted only
once: that a `needs_review` row is **never** returned to `pending`. Adding a test that
sweeps repeatedly — ten times — and asserts the row never moves, because "nothing retries
it" is the whole claim and once is not a demonstration of never.

---

## 7. Priority 6: the `needs_review` lifecycle, and the minimum API

### What can reach it

| Path | How |
|---|---|
| a lease expires with `side_effect_started_at` set | `LeaseReaper` — the worker died mid-call |
| the provider call times out | `StepRunner`, which classifies a timeout as uncertain |

In both cases the **dispatch** is `needs_review` and the **step** is still `dispatched`, so
the execution is `running` and will never finish. That is correct — nobody knows what
happened — and it means a `needs_review` row is a stuck execution until a human acts.

### What resolution has to mean

A person resolving one is **asserting what happened to an external effect this system cannot
observe**. There are exactly three answers:

| Outcome | Effect |
|---|---|
| **it did happen** | the step succeeds with the output the human supplies (or none); the execution advances |
| **it did not happen** | the step fails, or the dispatch returns to `pending` for one more attempt |
| **leave it** | the dispatch stays `needs_review`; the execution stays stuck, deliberately |

The second is the dangerous one: "it did not happen" authorises a retry of something that
may have happened. That is a human taking responsibility, which is exactly what this state
is for — but it means the action is not a read and not a routine mutation.

### Minimum API

```
GET  /v1/projects/{id}/dispatches?status=needs_review     EXECUTION_READ (viewer)
POST /v1/projects/{id}/dispatches/{id}/resolve            NEW ACTION (admin)
     {"outcome": "succeeded" | "failed" | "retry", "note": "...", "output": {...}}
```

Three things this needs that do not exist:

1. **A new `Action`** — `dispatch.resolve_review`. The matrix has 77 actions and
   `test_policy.py` asserts that number exhaustively, so adding one touches the matrix, its
   test, and the role-matrix route table. `EXECUTION_RETRY_STEP` (member) is the closest
   existing action and is **not** the same thing: retrying a step whose outcome is known is
   routine, while asserting the outcome of an unobservable external effect is not.
   Recommending **admin**, on the same reasoning that makes every credential action admin.
2. **An audit row** with the human's note and the outcome they asserted, which is the whole
   evidentiary point.
3. **A route onto `DispatchReadRepository`**, which exists and has no caller.

### Recommendation

**Design it now, implement it separately.** It is the only piece here that adds a
permission, an endpoint and a role-matrix row, and it is the piece whose shape most deserves
your opinion — particularly whether `retry` belongs in the same endpoint as the two
assertions, and whether admin is the right floor. The other five priorities are internal and
can land without it.

If you want it in this task, the plan above is what I would build.

---

## 8. Test plan

### New tests

| # | Test | Fails when |
|---|---|---|
| 1 | four concurrent sweeps recover one expired lease exactly once | `SKIP LOCKED` or the `reclaim` guard is removed |
| 2 | two sweeps in sequence: the second finds nothing | `reclaim`'s `leased_until < now()` predicate is removed |
| 3 | ten sweeps never move a `needs_review` row | the reaper stops branching on `side_effect_started_at` |
| 4 | a recovery writes one `dispatch.recovered` audit row carrying the previous error | the audit write is removed |
| 5 | two recoveries of one row leave two audit rows with different `claim_count` | the history is overwritten rather than appended |
| 6 | a rolled-back recovery leaves no audit row | the audit write moves outside the transaction |
| 7 | `Retry-After: 42` schedules `run_after ≈ now + 42 s`, not the backoff | the header is ignored |
| 8 | `Retry-After: 86400` is clamped to 900 s | the clamp is removed |
| 9 | `Retry-After` does not grant an extra attempt past `max_attempts` | the budget check is bypassed |
| 10 | an unparseable `Retry-After` falls back to jittered backoff | the fallback is removed |
| 11 | the loop runs a sweep per interval and exits after N consecutive errors | the error ceiling is removed |
| 12 | `--once` exits 0 having swept, and non-zero on a database failure | the exit code is not propagated |

### Failure scenarios covered

- **Worker crashes before calling** → `pending`, audit row, retried. (3, 4)
- **Worker crashes mid-call** → `needs_review`, audit row, never retried. (3, 4)
- **Provider times out** → `needs_review`. Already covered; extended by (3).
- **Two reapers, one row** → one recovery. (1)
- **A slow worker whose lease expired, reporting after a reaper recovered the row** →
  `settle` returns False and the worker does not overwrite the reaper's decision. Already
  covered in T6.
- **Database unavailable to the reaper** → the loop retries, then exits non-zero after the
  ceiling so a supervisor restarts it. (11)
- **A provider demanding a 24-hour wait** → clamped. (8)
- **Out of attempts and expired** → `dead`, with an audit row. (4)

### Concurrency behaviour, stated

- Reapers do not block each other: `SKIP LOCKED` skips, it does not wait.
- A reaper and a worker contend only on one row, and the loser is a no-op — the worker's
  `settle` requires `status = 'leased'`, the reaper's `reclaim` requires `leased_until <
  now()`.
- A reaper and `advance_once` never contend: the reaper touches `dispatches`, not
  `workflow_executions`, and takes no execution lock.
- Everything stays per workspace, so one tenant's backlog cannot delay another's recovery
  beyond one batch.

### Non-vacuity

Each of the twelve above will be weakened in turn, as in every task since T0, and the
results reported — including any guard that turns out to be untested or redundant.

---

## 9. What this task will not do

- No notification infrastructure. Polling, per priority 7.
- No advisory lock. §2.
- No second dispatch writer; the reaper only updates rows the apply path created.
- No new dispatch kinds, no conversation loop, no T8 retry classification.
- No change to the uncertain-outcome rule: a timeout is never read as proof the external
  action failed.
- No weakening of authorization: the reaper performs no control-plane action, and
  `SystemPrincipal` is refused every member of the matrix by `authorize()`.

## 10. Decisions — approved 2026-11-02

All four answered. Recorded here because the plan is what the implementation follows.

**1. Reaper deployment: a dedicated worker process.** Continuous mode and `--once`, and
**not** in the API lifespan. **No advisory lock** — the existing database concurrency
protections (`FOR UPDATE SKIP LOCKED` plus the guarded `reclaim`) are what §1.1 measured and
are what this preserves.

**2. The `needs_review` API is not in T6a.** It becomes **T6b**, after T6a is complete and
reviewed.

**3. T6b's human-resolution policy, settled in advance** so T6a leaves room for it:

- a new action, `dispatch.resolve_review`, **admin only**;
- one endpoint supporting `succeeded`, `failed` and `retry`;
- a **non-empty audit note** is required, and the resolving user's identity is recorded;
- `retry` requires **explicit acknowledgement that the external effect may already have
  happened** — the assertion is the authorisation;
- duplicate or conflicting resolutions are prevented;
- the resolution and its audit record are applied **transactionally**;
- attempt history and previous recovery records are **preserved**, not overwritten.

The last point is what makes §4 of this plan a prerequisite rather than a nicety: T6b has to
build on a recovery history that exists.

**4. `Retry-After`: the 900-second maximum is approved.** Honour valid delta-seconds clamped
to 900; invalid or unsupported values fall back to the existing jittered backoff; no extra
attempts and no lease extension; and `needs_review` is never retried automatically.

### Scope of T6a, restated

Priorities 1, 2, 3, 4, 5 and 7. Priority 6 is designed in §7 and implemented in T6b. No
T6b, T7 or T8 work in this task.

---

# T6a — implementation results

**Status: implemented, uncommitted.** 2264 tests pass with 0 skipped; ruff, ruff format,
mypy strict (98 files) and all 8 import contracts clean.

## Files changed

| File | |
|---|---|
| `workers/reaper.py` | **new.** The process: `run_forever`, `sweep_once`, `--once`, signal handling |
| `settings.py` | `REAPER_INTERVAL_SECONDS` (30), `REAPER_BATCH_LIMIT` (100), `REAPER_MAX_CONSECUTIVE_ERRORS` (5), all bounded |
| `repositories/dispatches.py` | `ExpiredLease`, carrying the previous error out before `reclaim` overwrites it |
| `services/dispatch.py` | the audit row per recovery, `_recovery_for`, `Retry-After` honoured and clamped |
| `tests/integration/test_reaper.py` | **new.** The twelve planned tests plus six more |
| `tests/integration/test_dispatch_worker.py` | the fake provider can send response headers |
| `tests/unit/test_settings.py` | the reaper bounds |

No migration. `audit_log` already existed, is workspace-scoped, and the application role
holds `INSERT, SELECT` on it and nothing else — so a recovery record cannot be edited or
deleted by the process that wrote it.

## What each test exercises, and what fails without it

Thirteen protections weakened in turn. Ten bite; one is redundant by construction and now
has a direct test; one is a throughput choice with no correctness test, deliberately.

| Protection removed | Test that failed |
|---|---|
| the `side_effect_in_flight` branch | `test_ten_sweeps_never_move_a_needs_review_row` |
| the audit row | 8 tests, every one that counts recoveries |
| `previous_error` in the metadata | `test_a_recovery_is_recorded_with_the_error_it_overwrites` |
| `claim_count` in the metadata | `test_two_recoveries_leave_two_records` |
| `Retry-After` honoured at all | `test_retry_after_is_honoured_instead_of_the_backoff`, `..._is_clamped` |
| the 900-second clamp | `test_an_outrageous_retry_after_is_clamped` |
| the attempt budget in `_will_retry` | `test_retry_after_grants_no_extra_attempt` |
| the loop's error ceiling | `test_the_loop_gives_up_after_consecutive_failures` |
| `--once` returning 1 on failure | `test_once_mode_exits_non_zero_when_the_sweep_fails` |
| the `Field(ge=…, le=…)` bounds | `test_the_reaper_settings_are_bounded` — **test added**, §R1 |
| `reclaim`'s expiry guard | `test_reclaim_refuses_a_lease_that_has_not_expired` — **test added**, §R2 |
| `FOR UPDATE SKIP LOCKED` | nothing, **and correctly so** — §R3 |

### R1. The settings bounds were decoration

Removing `Field(ge=1, le=3600)` broke nothing: the settings existed and nothing asserted
their range. An interval of 0 would spin and one of a day would leave crashed work idle for
a day, and either would have passed. Six parametrised cases now cover all three settings at
both ends.

### R2. `reclaim`'s expiry guard is unreachable through the reaper

`expire_leases` filters on `status = 'leased' AND leased_until < now()`, so by the time a
second sweep could call `reclaim`, its own SELECT has already returned nothing. The guard
matters only for two reapers that both got past the SELECT on one row — which `SKIP LOCKED`
prevents.

It is kept as defence in depth against the layer above it changing, and tested directly:
`reclaim` called at a live lease returns False and leaves the row `leased`. That fails when
the predicate is removed, which is the difference between defence in depth and a comment.

### R3. `SKIP LOCKED` has no correctness test, deliberately

Replacing it with a plain `FOR UPDATE` breaks nothing, and no test should be written to
suggest otherwise. The second reaper blocks, waits for the first to commit, re-evaluates its
SELECT, finds the row no longer `leased` and returns nothing — the same outcome, reached
more slowly. Correctness comes from the status-and-expiry filter.

`SKIP LOCKED` is why several reapers do not queue behind each other. That is a throughput
property, and asserting it means measuring time, which is how flaky tests are written. Same
finding as T6 §7.2, for the same reason.

## A test that hung instead of failing

The first non-vacuity sweep ran for two and a half hours and produced nothing. The cause was
case 10: removing the loop's error ceiling is exactly what makes `run_forever` return, so
without it the loop swept forever and `test_the_loop_gives_up_after_consecutive_failures`
awaited it unbounded, with no pytest timeout configured.

A test whose protection being removed makes it **hang** rather than **fail** cannot report.
It now awaits inside `asyncio.wait_for(..., timeout=30)` and calls `pytest.fail` on timeout.

Worse, killing the stuck process skipped the harness's `finally`, leaving `if False:` where
the ceiling belongs in `workers/reaper.py` — an untracked file, so `git status` could not
have shown it. Found by grepping the four touched files for weakening markers afterwards,
which is now the step that follows any killed sweep.

### It happened a second time, in the fix for a different problem

`test_the_worker_marks_the_call_in_flight_before_making_it` originally slept 200 ms and
then read the dispatch row, which assumed the claim, the credential decryption and the
connection all finished inside that window. On a full run that took 27 minutes instead of
17 they did not, and the test failed for reasons that had nothing to do with what it
tests. It was rewritten around a handshake: the fake provider sets `arrived` when the
request reaches it and then waits on `hold`, so the row is read while the call is
genuinely in flight and there is no window to lose.

That fix reintroduced the hang. The release of `hold` sat *after* the assertions, so a
failing assertion left the provider's handler waiting on it for ever and the test hung in
teardown — the exact defect described above, reached from the opposite direction. The
release is now in a `finally`, with a bounded wait on the worker task.

Confirmed the way it should have been the first time: with the marker write removed, the
test now **fails** in about a minute, with its own message — "the reaper could not tell
this from a crash before the call". Killing that run also left the weakening in
`services/dispatch.py`, as before; the grep found it.

## Operational deployment requirements

- **The reaper must run.** Without it a crashed worker's dispatch stays `leased` for ever.
  Either `python -m agentworld.workers.reaper` as a long-running process, or `--once` from
  cron or a Kubernetes CronJob.
- **Interval under the lease.** 30 s against a 120 s lease. Longer than the lease leaves a
  window where a row is expired and nothing has noticed — delay, not corruption, since every
  guard is on the row rather than on the schedule.
- **The application role**, not the owner: it reads and updates `dispatches` under RLS, per
  workspace, exactly as the worker does.
- **Several instances are safe, measured.** Four concurrent sweeps against one expired lease
  produce exactly one recovery and exactly one audit row. Running two is wasteful, not
  unsafe, and there is deliberately no advisory lock.
- **Exit codes**: 0 on a successful sweep, 1 when `--once` fails or the loop exceeds its
  consecutive-error ceiling. A supervisor restarting the process is visible; a process
  quietly retrying for ever is not.
- **SIGINT and SIGTERM** finish the sweep in progress and return. On Windows
  `add_signal_handler` is unimplemented, so a developer gets `KeyboardInterrupt` instead —
  the same outcome by a different route.

## Limitations

- **`needs_review` is still reachable by no API.** T6b, as agreed. A dispatch that lands
  there is visible in the table and in the log line the reaper wrote, and resolvable only by
  direct SQL.
- **The recovery history is in `audit_log`, not on the row.** Finding one is a query rather
  than a column read. That is the price of needing no migration, and T6b's "preserve previous
  recovery records" requirement reads from there.
- **No scheduler is configured**, only a process that can be scheduled. Choosing between the
  long-running and cron shapes is a deployment decision.
- **`Retry-After` is honoured only for the delay**, never for the attempt budget. T8 still
  owns classification, dead-lettering and the consequential exception.
- **One dispatch kind still has a runner**, and `tool_call` still cannot be published.
- Carried forward: **D7**, **D11**, **D12**, T1's tombstone names, T2's retry asymmetry,
  T3's unmade live provider call.

## Remaining decisions

1. **Which deployment shape to configure** — long-running process or CronJob. The code
   supports both; nothing in the repository chooses one.
2. **Does the recovery history need to be on the row** for T6b, or is `audit_log` enough?
   Putting it on the row is a migration.
