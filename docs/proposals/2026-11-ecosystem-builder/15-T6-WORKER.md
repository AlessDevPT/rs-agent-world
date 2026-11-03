# T6 — The outbox worker and the step runner

**Status: implemented, uncommitted.** The queued managed agent turn from T5 now actually
runs: a worker claims it, decrypts the credential, calls the provider through the guarded
client, folds the outcome, and advances the execution to completed.

---

## 1. What was built

| | |
|---|---|
| `repositories/dispatches.py` | claim, lease, settle, expire, reclaim — **and no INSERT** |
| `services/dispatch.py` | `StepRunner`, `DispatchWorker`, `LeaseReaper` |
| `repositories/workspaces.py` | `live_ids`, so a worker knows where to look |
| `repositories/definitions.py` | `AgentRepository.by_key`, the lookup an instruction needs |
| `infra/providers/base.py`, `anthropic.py` | a `TIMEOUT` failure member — see §4 |
| `tests/unit/test_dispatch_containment.py` | split by verb: creating vs updating |

No migration. Every column this needed — `leased_by`, `leased_until`,
`side_effect_started_at`, `claim_count`, `idempotency_token`, `run_after` — was built in
Phase 2 and corrected in T0.

```
claim      UPDATE ... WHERE id = (SELECT ... FOR UPDATE SKIP LOCKED LIMIT 1)
perform    the only place an outbound call happens
report     settle the row, append the outcome event, fold it
advance    ExecutionService.advance_once — the same path, never a second one
```

---

## 2. The claim is workspace-scoped, and that was not the first design

The first version claimed across tenants, reasoning that a worker serves all of them. It
returned nothing. Every time.

`dispatches` has row-level security **enabled and forced**, and a session with no visibility
set sees an empty table — migration 0004's deliberate failure mode: *the failure mode of an
unset session variable is total invisibility*. The four tables with no policy are
`workspaces`, `workspace_members`, `users` and `api_keys`, the ones that answer "which
tenant is this?", and the outbox is pointedly not among them.

That is the isolation model working rather than an obstacle to route around. So the worker
enumerates workspaces through `workspaces.live_ids` — unscoped for the same documented
reason the three Phase 1 unscoped repositories are — and claims *within* one. Every
statement against the outbox stays under RLS, and `DispatchClaimRepository` is a
`ScopedRepository` rather than the fourth global one its first draft apologised for being.

**The cost, stated:** one claim attempt per workspace when the queue is empty, starting from
a rotating offset so a permanently busy workspace cannot starve the ones after it. For a few
hundred workspaces that is a cheap poll. Past that it wants a notification rather than a
scan — `LISTEN`/`NOTIFY` on insert, or a small unprotected "work is available here" table —
and that is the obvious next change rather than something to pretend away.

---

## 3. The requirements, and where each one lives

| | Requirement | Where |
|---|---|---|
| 1 | Dispatch creation stays in the apply path | `MAY_CREATE_DISPATCHES` is still one file; the claim repository has no INSERT, verified by planting one |
| 2 | Atomic claiming and leasing | one `UPDATE ... WHERE id = (SELECT … FOR UPDATE SKIP LOCKED LIMIT 1)`, committed before the work starts |
| 3 | No concurrent execution of one dispatch | the claim filters on `status = 'pending'`; a leased row is not claimable |
| 4 | Crashes, lease expiry, recovery | `LeaseReaper.sweep`, the only thing that can move a dead worker's row |
| 5 | Idempotent reporting | three layers — §5 |
| 6 | Uncertain outcomes explicit | `side_effect_started_at` set *before* the call; a timeout is never read as "it did not happen" |
| 7 | No consequential work without an authorized record | `advance()` cannot emit one, **and** the worker refuses one if it finds one |
| 8 | Replayable events vs non-replayable instructions | the worker appends events and folds them; it cannot write a dispatch |
| 9 | Advance through the existing service | `ExecutionService.advance_once`, no second path |
| 10 | Provider behind an adapter | `infra/providers`' registry, by slug from a `model_profiles` row; this module names no vendor |

### Requirement 7 is belt *and* braces, deliberately

`advance()` cannot produce a consequential dispatch, so a row carrying one can only have come
from outside the approved path. The worker refuses it, logs it, and marks it `dead` without
making a call. A worker that trusted the row would be a worker for which the decision-time
guarantee bought nothing — the test writes such a row as the schema owner and asserts no
outbound request is made.

### Requirement 6 is the one with teeth

`side_effect_started_at` is set **before** the outbound call and cleared after it returns. The
reaper reads exactly that column:

- **NULL** — the worker died before calling anything. Safe to retry: back to `pending`.
- **set** — the call was in flight. **Nobody knows whether it landed.** Retrying might do the
  thing twice and failing might not do it at all, so the row becomes `needs_review` and
  nothing — no worker, no timer — retries it.

A timeout from a live worker is the same case and is treated the same way: the request was
sent, nothing came back, and that says nothing about whether the far side served it. The step
keeps its `dispatched` status rather than being told something untrue.

---

## 4. A distinction the provider vocabulary had collapsed

T3's `ProviderFailure` mapped both a 5xx and a timeout to `UNAVAILABLE`. The worker needs
them apart:

- a **5xx** means the far side answered and said it failed — known, and retryable;
- a **timeout** means the request was sent and nothing came back — unknown.

With one name for both, every 500 was escalated as an uncertain outcome, which would have
flooded a human with reviews and retried nothing. `ProviderFailure.TIMEOUT` now exists,
`OutboundFailure.TIMEOUT` maps to it, and `test_every_failure_member_has_a_retryability_answer`
asserts every member has a decision — which is what would have caught the new member being
left out of the retryable set.

This is a change to T3's code, made because T6 found the gap. It is additive: no existing
classification moved.

---

## 5. Idempotent reporting, in three layers

1. **`settle` only moves a row out of `leased`.** A second report finds it settled and
   returns False, changing nothing — including a *contradictory* second report, which the
   test asserts cannot overwrite the first.
2. **The outcome event's dedup key is deterministic** in the step and the attempt. Got past
   the first layer, a duplicate append violates the per-project dedup index and the whole
   transaction rolls back.
3. **`advance()` emits nothing** for a step whose state already reflects the outcome.

A worker whose lease the reaper has already taken back is told so by `settle` returning
False, and must not overwrite what the reaper decided — because the reaper may have decided
`needs_review`.

---

## 6. Tests

19 scenarios, all against a real database and a real socket. The provider is a loopback
server speaking the Messages protocol, so the guarded client, the credential decryption and
the provider adapter are all in the path rather than stubbed out of it.

| Required scenario | Test |
|---|---|
| Concurrent workers claiming the same dispatch | `test_concurrent_workers_cannot_claim_the_same_dispatch`, `test_a_second_worker_sees_the_lease_while_the_first_works` |
| Worker crash after claiming | `test_a_worker_that_crashes_after_claiming_leaves_a_lease_behind` |
| Lease expiration and recovery | `test_an_expired_lease_is_recovered_and_the_work_runs`, `test_a_live_lease_is_left_alone` |
| Duplicate result reporting | `test_reporting_the_same_outcome_twice_changes_nothing`, `test_a_second_outcome_event_for_one_attempt_is_refused` |
| Timeout with uncertain outcome | `test_a_timeout_is_treated_as_uncertain_not_as_a_failure`, `test_a_lease_that_expires_mid_flight_becomes_needs_review`, `test_the_worker_marks_the_call_in_flight_before_making_it` |
| Unauthorized dispatch rejection | `test_a_consequential_dispatch_is_refused_rather_than_performed` |
| Advancement after success and failure | `test_a_worker_performs_the_turn_and_the_execution_completes`, `test_a_failed_result_fails_the_execution_through_the_same_path` |
| No duplicate side effects where the receiver can help | `test_a_retry_carries_the_same_idempotency_token`, `test_the_token_is_the_dispatchs_own` |

Plus: a retryable failure backing off, a backed-off row not being claimable until due, an
empty queue not being an error, and the credential reaching the provider and no row the
worker wrote.

---

## 7. Non-vacuity, and the five things it corrected

Thirteen guards weakened. Eight bit immediately. Five did not, and four of those were real
gaps.

| Weakened | What failed |
|---|---|
| the claim ignoring `status` | `test_a_worker_that_crashes_after_claiming_leaves_a_lease_behind` |
| `settle` accepting any status | `test_reporting_the_same_outcome_twice_changes_nothing` |
| the reaper ignoring `side_effect_started_at` | `test_a_lease_that_expires_mid_flight_becomes_needs_review` |
| the worker performing consequential work | `test_a_consequential_dispatch_is_refused_rather_than_performed` |
| a timeout folded back into `unavailable` | `test_a_timeout_is_treated_as_uncertain_not_as_a_failure` |
| a retryable failure settled as `failed` | 2 tests — **and this was a bug**, §7.1 |
| the idempotency token not forwarded | `test_a_retry_carries_the_same_idempotency_token`, `test_the_token_is_the_dispatchs_own` |
| an INSERT planted in the claim repository | `test_nothing_outside_the_apply_path_creates_a_dispatch` |
| the claim ignoring `run_after` | nothing → **test added** |
| the outcome event's dedup key randomised | nothing → **test added** |
| `TIMEOUT` removed from the retryable set | nothing → **test added** |
| the worker not setting the in-flight marker | nothing → **test added** |
| `FOR UPDATE SKIP LOCKED` → plain `FOR UPDATE` | nothing, **and correctly so** — §7.2 |

### 7.1 A real bug: a retried dispatch nothing would ever claim

`report` settled a retryable failure with `status = outcome.status`, which is `failed`, and a
future `run_after`. The claim query filters on `status = 'pending'`, so the row was parked
forever: a dispatch that looked retried and was not. It settles as `pending` now, with the
failure still recorded in `last_error` and `last_error_code`.

Found by `test_a_retryable_failure_goes_back_to_pending_with_backoff`, which is exactly what
that test was for.

### 7.2 `SKIP LOCKED` is a throughput choice, not a correctness guard

Removing it broke no test, and no test should be written to make it look otherwise. With a
plain `FOR UPDATE` the second worker *blocks* until the first commits, then re-evaluates the
subquery, finds the row no longer `pending`, and returns None — the same outcome, reached
more slowly. What stops two workers performing one dispatch is the `status = 'pending'`
filter, which is tested.

`SKIP LOCKED` is why a pool scales instead of queueing behind its slowest member. That is a
real reason to keep it and not a property a unit test can assert without measuring time,
which is how flaky tests get written.

### 7.3 Three guards were tested only by their own test's setup

The in-flight marker, the backoff filter and the outcome dedup key were each covered only by
a test that arranged the state by hand — injecting the marker, resetting `run_after`, or
being stopped by `settle` before the event layer was reached. Each now has a test that
exercises the worker's own path.

---

## 8. Limitations

- **One dispatch kind has a runner.** `managed_agent_turn`. `tool_call`, `agent_push`,
  `agent_task_offer` and `webhook_emit` are refused as `unsupported_kind`. `tool_call`
  cannot even be published (T5 §3), and the other three are Phase 3a.
- **One message per turn.** No conversation loop, no tool results fed back. That is T7, and
  building a message history here with no caller for it would be guessing at T7's shape.
- **The retry policy is minimal and T8 owns the real one.** A `FAILED` outcome with attempts
  left is retried with jittered exponential backoff; everything else is terminal. A
  provider's `Retry-After` is parsed by the adapter and **not yet honoured** by the worker —
  T8's call, and the provider knows better than the backoff does.
  *(T6a honoured `Retry-After`, clamped to 900 s. And the Phase 3 acceptance review found
  that the paragraph above described something the code did not do — see §9.)*
- **No `workflow.step_retried` event.** A retry puts the row back to `pending` and tells the
  step nothing, because nothing has been *decided* about the step. The event belongs with
  T8's retry decision.
- **The reaper has no scheduler.** `sweep()` does one pass and returns. Nothing calls it on a
  timer yet; a deployment needs that, and so does the worker loop.
- **`needs_review` has no UI and no endpoint.** A dispatch that lands there is visible only
  in the table and in the log line the worker wrote. An operator cannot see or resolve one
  through the API.
- **Polling, not notification.** §2.
- Carried forward: **D7**, **D11**, **D12**, T1's tombstone names, T2's retry asymmetry
  between a blocked destination and an unreachable one, T3's unmade live provider call.

---

## 9. Remaining decisions

1. **Should `Retry-After` override the backoff?** The provider knows when it will serve the
   key again. The argument against is that a hostile or misconfigured provider could park a
   worker's queue for hours.
2. **Who runs the reaper, and how often?** A separate process, a thread in the API, or a
   scheduled task. The lease is 120 s, so a sweep less frequent than that leaves crashed
   work idle for longer than it needs to be.
3. **Does a `needs_review` dispatch need an endpoint before Phase 3a?** It is the one state
   that requires a human and currently has no way to reach one.
4. **Notification instead of polling** — at what scale, and which mechanism.


---

## 9. What the Phase 3 acceptance review found, and the fix

Closing the last unticked Phase 3 criterion - *a 429 then success: one step, two attempts,
one output* - turned up three things about the retry path. None was a safety hole; all three
were the retry policy not being the policy the code and tests described.

### The attempt budget was never consumed

`_will_retry` computed `claimed.attempt + 1 < claimed.max_attempts`. Migration 0019 makes
`attempt` immutable - it is the *identity* of a unit of work, which is what makes
`UNIQUE (execution_step_id, kind, attempt)` guard creation - so that expression was frozen
at `0 + 1 < 3` for the life of a row. Only `claim_count` moves.

Measured, with the provider answering 500 every time:

```
attempt 1: ('pending', 0, 1)      attempt == 0 throughout
attempt 2: ('pending', 0, 2)      claim_count is the thing that moves
attempt 3: ('pending', 0, 3)      max_attempts is 3
attempt 4: ('pending', 0, 4)
attempt 5: ('pending', 0, 5)      five provider calls, still claimable
```

And because `_backoff`'s ceiling was `2.0 * 2**claimed.attempt`, it was frozen at two
seconds as well. So a permanently failing call was re-sent roughly once a second,
indefinitely, with real spend, and the execution stayed `running` for ever because the step
was never told anything. T6c cannot help: there *is* an unsettled dispatch, and the
reconciler is right to leave it alone.

**Fixed** by bounding on `claim_count`, which is what increments, and growing the ceiling
with it. The budget is now exactly `max_attempts` provider calls.

**The consequence worth knowing:** `claim_count` cannot tell "the provider refused" from
"our worker died before calling", so a claim that crashed pre-call also consumes budget. A
crash-looping worker can therefore exhaust a dispatch. That is bounded failure instead of
unbounded looping, and it is visible in the column - but separating the two needs a
dedicated failure counter, a migration, and a decision. Not smuggled in with a fix.

### A terminal failure was retried

`ProviderFailure.is_retryable` has classified `UNAUTHORIZED`, `INVALID_REQUEST` and
`MALFORMED_RESPONSE` as terminal since T3, with a docstring saying retrying a 400 "turns one
caller's mistake into three times the spend". **Nothing consulted it.** `StepRunner` mapped
every failure except `TIMEOUT` to `FAILED`, and `_will_retry` retried any `FAILED`. Measured:
a 400 left the dispatch `pending` with `last_error_code = invalid_request`.

Worse than dead code, because it is *tested* code: `test_providers.py` asserts the full
classification, so the predicate reads like an enforced guarantee. The same is true of
`infra/net/errors.py`'s own `is_retryable`, which is also consulted nowhere.

**Fixed** by carrying the adapter's classification on `Outcome.retryable` and gating
`_will_retry` on it. Defaults to `True`, so an outcome built outside the adapter - the
runner-crash path - keeps being retried, now bounded by the budget.

### A test that documented the opposite of what ran

`test_a_failed_result_fails_the_execution_through_the_same_path` said the 400's terminal
classification is why the worker does not retry - and then set `max_attempts = 1`, with a
comment that this made the classification decisive "rather than the attempt budget". It was
the budget doing all the work; remove the line and the 400 was retried.

The crutch is gone. The test now runs with the full budget, asserts one provider call, and
rests on what its docstring claims.

### Non-vacuity

| Weakening | What fails |
| --- | --- |
| `_will_retry` ignores the classification | `test_a_failed_result_fails_the_execution_through_the_same_path` |
| The adapter stops passing the classification | the same test |
| The budget reads `attempt` again | `test_the_attempt_budget_is_spent_and_then_the_work_is_over` |
| The ceiling reads `attempt` again | `test_the_backoff_ceiling_grows_with_each_attempt` |

### One knock-on, recorded rather than fixed

`17-T6B-REVIEW-API.md` §8 lists, as a limitation, that a dispatch already at its attempt
ceiling can be resolved as `retry` and then fail immediately. That was unreachable before -
the ceiling was never reached - and it is reachable now. The behaviour is as documented and
as approved ("retry grants no extra attempts"); what changed is that it can actually happen.
