# T6c — durable advancement recovery

**Status: not implemented. Design and options only.** Raised by the T6b review; nothing
in this document is built, and nothing in T6b depends on it.

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

Two variants:

**B1 — derive the condition, no migration.** The sweep queries for executions that are
not in a terminal status and whose steps are all settled. No new column, nothing to keep
in step with reality, and no way for the signal itself to be lost — the condition *is*
the state. The cost is the query: it must be index-friendly enough to run every few
seconds over a growing table, and "all steps settled" has to be expressible without a
sequential scan.

**B2 — an explicit marker.** A column such as `advance_owed_at` on the execution, set in
transaction 1 and cleared by the advance. Trivially cheap to query and unambiguous. The
cost is a migration, a second piece of state that can disagree with the first, and the
discipline that **every** writer sets it — a writer that forgets reintroduces exactly the
gap this closes, silently.

**Verdict: B1, falling back to B2 only if the query cannot be made cheap.** B1 has no
signal to lose, which is the property that matters: a recovery mechanism whose own signal
can go missing in the same crash is not a recovery mechanism. T6b's decision to count
recovery history from `audit_log` rather than add a column was the same judgement.

Where it runs: an extra sweep inside the existing reaper process, not a new process. It
shares the loop, the error ceiling, the `--once` mode and the operational story that T6a
already built and documented.

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
