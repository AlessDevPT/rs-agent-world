# T6c — durable advancement recovery

**Status: implemented, uncommitted and awaiting review.** Raised by the T6b review and
built as T6c with B1 - a reconciliation sweep over derived state, no marker column, and no
cursor. §§1-4 are the design as approved; §7 onwards is what was built and what it proves.

## 1. The gap, stated exactly

Both the worker and the review path do the same two things in two transactions:

```
transaction 1   the dispatch is settled (or resolved), the step's outcome event is
                appended and folded, the audit row is written       -- commits
transaction 2   ExecutionService.advance_once decides what happens next, and
                writes the next step's dispatch rows                -- commits
```

A process that dies between them leaves an execution that is **correct but stalled**:
the step is finished, the record is complete and consistent, and nothing will ever
decide the next step. Nothing retries transaction 2.

| Where | Code | Introduced |
| --- | --- | --- |
| The worker | `DispatchWorker.report` → `advance_once` after the settle block | T6 |
| The review path | `ReviewService.resolve` → `advance_once` after the resolve block | T6b |

`advance_once` is deliberately *not* inside transaction 1: it takes the execution's row
lock and decides from committed state, so the outcome has to be folded and committed
before it can see the step as finished. That is the reason for the split, and it is why
the gap is a design consequence rather than an oversight.

### What makes it survivable today, and what does not

What stops this being data loss: transaction 1 is complete in itself. The dispatch is
settled, the event is folded, the audit row is written. Nothing is half-applied and
nothing is wrong.

What makes it a real defect anyway: there is **no signal** that an advance is owed.
Discovering a stalled execution means a person noticing that one stopped moving. At one
execution a day that is tolerable; at scale it is a silent stall rate.

How likely: the window is one process death between two adjacent transactions — small
per dispatch, and not small across every dispatch the system will ever run. A deploy
that restarts workers mid-flight hits exactly this window, repeatedly, on purpose.

## 2. Option A — one transaction

Fold the advance into transaction 1.

**Against.** `advance_once` would have to decide from uncommitted state read back
through the same session. That is possible, but it changes what the function means: it
currently promises "decide from what is committed", which is what makes it safe to call
from three places (the run route, the worker, the review path) without them knowing about
each other. Changing that contract changes all three.

It also lengthens the transaction to: dispatch update, event append, fold, audit insert,
decision, and the next step's dispatch inserts — while holding the execution row lock.
Two row locks taken in one transaction in an order that differs between the worker and
the reaper is how deadlocks are made, and the current split is part of why there are none.

**For.** It removes the window completely rather than shortening it. No new worker, no
new state, nothing to operate.

**Verdict:** rejected for now. It trades a bounded, detectable stall for an unbounded
class of lock-ordering problems across three callers, and it is the harder change to
verify.

## 3. Option B — a durable signal plus a reconciliation sweep

Write, in transaction 1, something that says "this execution is owed an advance"; have a
sweep pick those up and call `advance_once` until none are owed.

This is the shape the system already has. The reaper is a polling sweeper over a
condition expressed in SQL, it is idempotent, and `advance_once` is **already** idempotent
— deterministic decisions plus the dedup keys that make a double advance produce one
dispatch. Calling it again when it was not needed costs a row lock and a no-op.

Two variants. **Both need a migration** — an earlier draft of this document claimed B1
did not, and that was wrong; see the correction below.

**B1 — derive the condition.** The sweep queries for `running` executions that have no
dispatch in a non-terminal status:

```sql
NOT EXISTS (
    SELECT 1 FROM dispatches d
    WHERE d.execution_id = e.id
      AND d.status IN ('pending', 'leased', 'needs_review')
)
```

No new state: the condition *is* the state, so there is no signal that can be lost in the
same crash it is meant to survive. The cost is the query, and it needs **a new partial
index** on `dispatches (execution_id, status)` to be cheap — the existing indexes are
`(run_after, id) WHERE pending`, `(leased_until) WHERE leased`, `(project_id, status)
WHERE status IN ('dead','needs_review')`, and the unique `(execution_step_id, kind,
attempt)`. None of them can serve a correlation on `execution_id`.

**B2 — an explicit marker.** A column such as `advance_owed_at` on the execution, set in
transaction 1 and cleared by the advance. Trivially cheap to query and unambiguous. It
needs a migration for the column *and* for an index to find the rows that carry it.

### The correction

The earlier draft framed this as "B1 needs no migration, B2 does", and used that as the
deciding argument. Both need one. What actually separates them:

| | B1 | B2 |
| --- | --- | --- |
| Migration | a partial index | a column **and** its index |
| New state | none | one column |
| New invariant | none | every dispatch writer must set it |
| Can be silently wrong | no | yes |
| Can be slow | yes | less so |

**Verdict: still B1, for a better reason than the one first given.** The deciding
argument is not the migration count, it is the invariant: B2 adds a rule that every
present and future writer of a dispatch outcome has to maintain, and a writer that
forgets it reintroduces exactly the gap T6c exists to close — silently, because the
missing marker looks identical to "nothing is owed". B1 adds no rule to forget. An index
can only be slow, and slow is measurable; a marker can be wrong, and wrong is not.

That is the same judgement T6b made in counting recovery history from `audit_log` rather
than adding a column: prefer the derivable fact over the maintained one, and pay for it
in query cost rather than in discipline.

Where it runs: an extra sweep inside the existing reaper process, not a new process. It
shares the loop, the error ceiling, the `--once` mode and the operational story that T6a
already built and documented.

## 3a. What the repository actually says

Checked before writing any code, because four of this document's assumptions were wrong
or imprecise.

**`advance_once` locks by blocking, not by skipping.** `ExecutionRepository.lock` is a
plain `SELECT … FOR UPDATE`. Two reconcilers that pick the same execution serialise: the
second waits for the first to commit, then reads committed state, finds no decisions and
writes nothing. Correct and idempotent, but *blocking* — unlike the reaper, whose
`expire_leases` uses `FOR UPDATE SKIP LOCKED`. T6c does not change this contract. The
sweep calls `advance_once` once per candidate, each in its own transaction, and lets the
row lock do the serialising.

**Terminal and paused executions are already no-ops.** `advance()` opens with
`if not state.status.is_live or state.status is ExecutionStatus.PAUSED: return ()`, and
`advance_once` then returns `Applied((), ())` without writing. So two of the correctness
requirements are properties of existing code rather than of anything T6c adds — which
means their tests have to demonstrate the no-op, not merely pass.

**Candidates are `running` only, not every live status.** `LIVE_EXECUTION_STATUSES` is
`pending, running, waiting_approval, waiting_input, paused`. `paused` is refused inside
`advance()`, but `pending`, `waiting_approval` and `waiting_input` are live and would be
nudged on every cycle. A `pending` execution is one that was created and never started: a
recovery sweep must not be the thing that starts it. A reconciler finishes work; it does
not invent it. `executions_live_idx` is `(project_id, status, created_at)` with a partial
predicate over the live set, so `status = 'running'` still uses it.

**`dispatches.execution_id` is nullable**, so the new index's predicate includes
`execution_id IS NOT NULL` rather than indexing rows that can never satisfy the
correlation. `dispatches.status` is the PostgreSQL enum `dispatch_status`; T6c adds no
values to it and changes no existing meaning.

**There is no reaper cursor to reuse.** `LeaseReaper.sweep` iterates *every* workspace
from `workspaces.live_ids()` each sweep, with a per-workspace `limit`, and holds no
cursor. The rotation is `DispatchWorker._cursor`, and it exists for a different reason:
`run_once` returns after claiming **one** dispatch, so without rotation a permanently
busy workspace would starve the ones after it. A sweep that visits every workspace with a
bounded limit cannot starve anything, so modelled on the reaper it needs no cursor.

## 4. What T6c has to prove

- An execution whose advance is interrupted is advanced by the sweep, exactly once.
- The sweep is idempotent: running it against an execution that needs nothing changes
  nothing and writes no event.
- Two sweeps racing produce one advance, not two (the existing dedup keys, exercised
  rather than assumed).
- The sweep cannot advance an execution whose step is genuinely unfinished, including a
  dispatch in `needs_review` — the state T6 and T6b exist to protect.
- The query's cost is measured against a table with a realistic row count, not asserted.
- A non-vacuity pass: each guard removed, a named test fails.

## 5. Explicitly out of scope

Retry budgets, notification of stuck dispatches, and the T8 retry system. T6c is one
thing: an owed advance always eventually happens.

---

## 6. The iteration decision: no cursor

Approved as part of this task, and worth recording with its reason because the absence of
a cursor looks like an omission.

Each pass visits **every** workspace, processes at most `RECONCILE_BATCH_LIMIT` candidates
within each, and holds no state between passes. The dispatch worker's `_cursor` exists
because `run_once` returns after claiming **one** dispatch, so a permanently busy
workspace could starve the ones after it in the list. A sweep that visits every workspace
cannot starve anything, so it needs no rotation - and `LeaseReaper`, which has the same
shape, has none either.

**The consequence, stated plainly:** the batch limit bounds the work *within* a workspace,
not across a pass. Total sweep duration therefore grows with workspace count. At the
current scale that is irrelevant; at some scale it will not be, and a cursor is the answer
then rather than now. §8 says what to measure so that judgement is made from numbers.

## 7. What was built

| Piece | Where |
| --- | --- |
| `is_unsettled` on `DispatchStatus` | `domain/enums.py` |
| The partial index | migration `0020`, and the model's `__table_args__` |
| The candidate query | `ExecutionRepository.needing_advance` |
| The sweep | `services/reconcile.py` — `AdvanceReconciler`, `ReconcileResult` |
| Scheduling | `workers/reaper.py`, on its own interval inside the existing process |
| Three settings | `reconcile_interval_seconds`, `reconcile_batch_limit`, `reconcile_grace_seconds` |

**One advancement path.** `AdvanceReconciler` calls `ExecutionService.advance_once` and
nothing else. A recovery mechanism with its own decision path would be deciding
differently from the worker by definition, which is the thing T6 was careful about.

**One definition of "in flight".** The query builds its `IN` list from
`DispatchStatus.is_unsettled`; the index's predicate is hand-written SQL in the migration.
Two statements of one fact, so a test holds them together - adding a status to one and not
the other would silently stop the index covering the query, and the only symptom would be
a slow sweep nobody traces back here.

**Candidates are `running` only.** `paused` is already refused inside `advance()`, but
`pending`, `waiting_approval` and `waiting_input` are live and would otherwise be offered
every pass. `pending` is the one that matters: an execution created and never started. A
reconciler finishes work; it must not be the thing that starts it.

**A grace period, not a race.** The normal path settles a dispatch and advances
milliseconds later. Without `reconcile_grace_seconds` the sweep would compete for the
execution's row lock on every ordinary step. The window is measured against
`last_event_at`, the execution's own folded clock, which stops moving when an execution
stalls and does not when it is healthy. No new column.

**Failures are isolated twice.** A workspace whose query raises is counted in
`workspaces_failed` and stepped over; a candidate whose advance raises is counted in
`failures` and its neighbours continue. Nothing is lost either way, because candidacy is
derived rather than consumed: whatever made a row a candidate is still true next pass.
That is also why a crashed sweep is safe - there is no claim to leak and no marker to
leave half-written.

## 8. Operational measurements

What to watch, and what each number means. All of it is already logged.

| Signal | Where | What it tells you |
| --- | --- | --- |
| `candidates` vs `advanced` | `reconcile_swept` | How often the derived condition was a **false positive**. A persistently large gap means the grace period is too short or the condition too loose. Each one costs a row lock and no rows. |
| `advanced` > 0 at all | `reconcile_swept` | How often advances are actually being stranded. Should be near zero in a healthy deployment; a steady rate means processes are dying mid-advance and that is the thing to fix. |
| `failures` | `reconcile_swept` | Candidates whose advance raised. Non-zero means a real bug in advancement, not a recovery problem. |
| `workspaces_failed` | `reconcile_swept` | Reported separately so one unreachable workspace is never filed as healthy work. |
| `workspaces` and pass wall-time | `reconcile_swept` | **The cursor decision.** When a pass stops finishing inside `RECONCILE_INTERVAL_SECONDS`, the per-workspace bound is no longer bounding anything useful and a global cursor earns its place. Until then it would be state nobody needs. |
| `reconcile_execution_gone` | its own line | An execution deleted between the query and the lock. Benign, and deliberately not counted as a failure. |

The one number that is **not** instrumented is time-to-recovery: how long an execution
stayed stalled before a pass picked it up. It is bounded above by
`reconcile_interval_seconds + reconcile_grace_seconds` by construction, and measuring it
properly means recording when the stall began, which is the marker column this design
deliberately does not have. Stated here so nobody looks for the metric and assumes it was
forgotten.

## 9. Limitations

- **Polling, still.** Same choice as T6a: a stalled execution is found by a sweep, not by
  a notification. The upper bound on recovery time is the interval plus the grace period.
- **The gap is narrowed, not closed.** A process that dies between the two transactions
  still strands an execution; T6c guarantees someone comes back for it, not that it never
  happens. Option A in §2 would close it, at the cost of changing `advance_once`'s
  contract for all three of its callers.
- **No cursor.** Pass duration grows with workspace count. See §6 and §8.
- **The grace period delays recovery too.** It cannot distinguish "the worker is about to
  advance this" from "the worker died a moment ago", so it waits out the window in both
  cases.

## 10. The tests

Twenty-four cases in `tests/integration/test_reconcile.py`.

The first one asserts **the bug**: after an interrupted advance the dispatch succeeded, the
step succeeded, and the execution is still `running` with nothing in flight. Without that
test the recovery test proves nothing, because a recovery that never had anything to
recover passes trivially. The window is produced through the real worker with
`advance_once` raising for one call - what a kill between the two transactions looks like
to the database - and every assertion in the file is on rows, not on a mock.

**Recovery**
- `test_the_stall_this_exists_for_is_real` — the bug, asserted.
- `test_an_interrupted_advance_is_finished_by_the_sweep` — the point of T6c.
- `test_a_crash_mid_sweep_loses_nobody` — a sweep that dies counts the failure and the
  next pass still recovers the execution. Candidacy is derived, so there is no claim to
  leak and no marker to leave half-written.

**What it must not touch**
- `test_a_pending_dispatch_is_work_in_flight`, `test_a_leased_dispatch_is_work_in_flight`.
- `test_needs_review_is_never_a_candidate` — the one that matters most.
- `test_only_a_running_execution_is_a_candidate`, over all five live statuses.
- `test_a_finished_execution_is_left_alone`, `test_an_already_advanced_execution_is_harmless`
  — asserted on the **event count**, because a no-op that appends an event is not a no-op.
- `test_the_grace_period_keeps_the_sweep_out_of_the_live_path` — both directions, one
  setting.
- `test_a_sibling_in_flight_does_not_hide_a_stalled_execution` — the `NOT EXISTS` is
  correlated per execution.
- `test_the_query_stays_in_its_scope_when_more_is_visible` — the `workspace_id` predicate
  itself, with a wider visibility set than the sweep ever opens.

**Bounds, passes, workspaces**
- `test_the_batch_limit_bounds_a_workspace_and_the_rest_wait`.
- `test_repeated_passes_drain_everything_eligible` — the liveness claim: a bound may delay
  recovery, never deny it.
- `test_every_workspace_is_visited_in_one_pass` — what replaces the need for a cursor.
- `test_one_broken_workspace_does_not_stop_the_others`.
- `test_concurrent_sweeps_advance_once`.
- `test_the_reconciler_keeps_no_state_between_passes` — the absence of a cursor, asserted
  behaviourally rather than by reading the source.

**The index**
- `test_the_index_exists_and_says_what_the_enum_says` — holds the migration's hand-written
  predicate and `DispatchStatus.is_unsettled` together.
- `test_the_candidate_query_can_use_the_index` — with `enable_seqscan` off. Honest about
  its scope: the test database holds a handful of rows where a sequential scan is genuinely
  cheaper, so a plan taken at face value would say nothing. Disabling seqscan asks the
  narrower question this can answer - *is the index usable for this predicate at all* -
  which catches a predicate that does not match, a column order that cannot serve the
  correlation, or an index that was never created. Throughput at scale is §8's business.

## 11. Non-vacuity

Thirteen protections, each removed in turn. Every one is now exercised by a named test.

| Weakening | What fails |
| --- | --- |
| `needs_review` out of the unsettled set | `test_needs_review_is_never_a_candidate` |
| `pending` out of the unsettled set | `test_a_pending_dispatch_is_work_in_flight` |
| `leased` out of the unsettled set | `test_a_leased_dispatch_is_work_in_flight` |
| The `status = 'running'` filter | `test_only_a_running_execution_is_a_candidate`, 4 cases |
| The grace period | `test_the_grace_period_keeps_the_sweep_out_of_the_live_path` |
| The batch limit | `test_the_batch_limit_bounds_a_workspace_and_the_rest_wait` |
| The `execution_id` correlation | `test_a_sibling_in_flight_does_not_hide_a_stalled_execution` |
| The `workspace_id` predicate | `test_the_query_stays_in_its_scope_when_more_is_visible` |
| Counting every call as progress | `test_concurrent_sweeps_advance_once` |
| Per-workspace error isolation | `test_one_broken_workspace_does_not_stop_the_others` |
| Per-candidate error isolation | `test_a_crash_mid_sweep_loses_nobody` |
| `needs_review` out of the index predicate | `test_the_index_exists_and_says_what_the_enum_says` |
| `execution_id IS NOT NULL` out of the index | `test_the_index_exists_and_says_what_the_enum_says` |

### Four of those thirteen did not hold on the first pass

Two were faults in the harness, and they are worth recording because both produced a
*reassuring* result rather than an obviously broken one.

**An anchor that matched twice.** The batch-limit case anchored on `.limit(limit)`, which
appears in two methods of that file, so the weakening was never applied and the harness
reported it as "nothing failed" - indistinguishable, in the output, from a protection that
genuinely has no test.

**A weakening the test could not see.** The nullable-rows case changed the predicate to
`execution_id IS NOT NULL OR execution_id IS NULL`, which leaves the exact substring the
test greps for still present in the index definition. The test could not have failed
whatever the index did. A weakening has to be visible to the assertion, not merely present
in the file.

The other two were real holes, and both are the same shape: a property that every test
happened to satisfy for a reason unrelated to the property.

**The correlation.** Removing `d.execution_id = e.id` broke nothing, because every test
had one execution per workspace - where "does *any* unsettled dispatch exist?" gives the
same answer as the correct question. In a workspace with traffic it does not: one busy
execution would hide every stalled one behind it, and the reconciler would quietly do
nothing for as long as the workspace stayed busy.

**The workspace predicate.** Removing it broke nothing either, and here the reason is
legitimate: the sweep opens one workspace at a time and `workflow_executions` is under
forced RLS, so RLS alone hides the rest. The predicate is not redundant for a caller that
opens a wider visibility set, and a repository method that silently returns another
tenant's rows under such a caller is the shape of defect this codebase has already been
bitten by twice. The new test opens two workspaces, scopes to one, and asserts the
predicate rather than the RLS.
