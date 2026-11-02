# T5 — The executions service and publish

**Status: implemented.** The first task in Phase 3 that can cause work: it adds the only
statement in the system that writes a `dispatches` row.

A workflow can now be published, run, and advanced — and the first managed agent turn
exists as a pending dispatch waiting for the worker T6 builds.

---

## 1. What was built

| | |
|---|---|
| `repositories/executions.py` | `ExecutionRepository`, and `DispatchRepository.create` — **the one dispatch writer** |
| `repositories/definitions.py` | `publish`, `published_version`, `next_draft` on `WorkflowRepository` |
| `services/executions.py` | publish, start, and the decide-and-apply transaction |
| `api/v1/executions.py` | publish, run, and two reads |
| `services/definitions.py` | `facts_and_effects` and `definition_for_execution`, extracted so validate and publish and run ask the same questions |
| `domain/errors.py` | `WorkflowNotPublishableError`, carrying findings to the response |
| `api/errors.py` | renders those findings into the problem document's `errors` array |

No migration. Every table and constraint this needed was built in Phase 2 and corrected in
T0.

---

## 2. The shape of an apply

```
BEGIN
  SELECT ... FROM workflow_executions WHERE id = :id FOR UPDATE
  decisions = advance(definition, state)        -- pure, no I/O
  for each decision:
      INSERT INTO events          (the decision, as a fact)      -- replayed
      UPDATE execution_steps/...  (fold the decision into state)  -- rebuilt
      INSERT INTO dispatches      (the intent, as work to do)     -- NEVER replayed
COMMIT
```

`advance` runs in-process, as Q4 decided. It imports no database, no provider and no
framework, so calling it inside the transaction costs a function call and leaves one code
path rather than two.

`advance_once` **takes no principal**, and that is deliberate. The authority for a running
execution was decided when it started and is recorded immutably on the row; a parameter here
would be a way to change whose authority a running execution uses. The reaper and the step
runner will call it with no user at all.

### The allow-list changed

`tests/unit/test_dispatch_containment.py` had an empty allow-list. It now names one file:

```python
MAY_WRITE_DISPATCHES = frozenset({"repositories/executions.py"})
```

That one-line diff is the point of keeping the list. Claiming is T6's and will be a
different repository, because one that could both create and claim is the natural place for
someone to add "create and immediately run".

---

## 3. A blocking limitation found while building publish

**A `tool_call` step cannot be published in this release.**

`workflow_steps` carries a `tool_call_needs_tool` check constraint — `kind <> 'tool_call' OR
tool_id IS NOT NULL` — and there is **no `tools` table**. It arrives in Phase 6; the column
is a bare uuid until then, with a comment saying so. So there is no row for a `tool_call`
step to name, and three ways to respond:

- invent a uuid from the tool name, which a later foreign key will not recognise;
- point `tool_id` at the granting connection, which is a different thing wearing the same
  column;
- refuse to publish it, and say why.

The third. `PUBLISHABLE_KINDS` is `{agent_task, condition, transform}` and a draft containing
anything else is refused with a 422 naming the kind.

**The consequence is worth stating plainly.** `advance()`'s `tool_call` branch is tested and
unreachable from a published workflow. The milestone is unaffected — it is a *managed agent*
execution, and a managed agent's own tool use is a different mechanism (its model asks, the
step runner decides) that arrives with T7 and Phase 3a. But anyone reading
`13-T4-ADVANCE.md` would reasonably assume a `tool_call` step can run today, and it cannot.

---

## 4. Acceptance criteria

| Criterion | Test | |
|---|---|---|
| Publishing normalises into `workflow_steps`/`workflow_edges`, sets `published_at`, and the version is then immutable | `test_publishing_normalises_the_draft_and_freezes_it` | ✅ |
| Publishing an unpublishable draft is refused with the findings | `test_an_unpublishable_draft_is_refused_with_its_findings`, `test_a_refused_publish_leaves_nothing_behind` | ✅ |
| `POST .../run` requires `WORKFLOW_RUN`; the role matrix has a row | 4 new rows × 4 roles in `test_role_matrix.py` (170 cases) | ✅ |
| A run names its initiator; the XOR constraint and `ON DELETE RESTRICT` are exercised | `test_running_creates_an_execution_naming_its_initiator`, `test_an_initiating_user_cannot_be_deleted_from_under_the_record` | ✅ |
| A double `advance()` produces one dispatch | `test_a_double_advance_produces_one_dispatch` | ✅ |
| Event, state and outbox commit together — a forced failure after the event leaves none of the three | `test_the_three_writes_commit_together` | ✅ |

### Publish order is forced by T0's trigger

Migration 0018 refuses an INSERT into `workflow_steps` or `workflow_edges` when the version
already has `published_at` set — *including the publish's own inserts*. So the graph goes in
first and `published_at` goes on last. Reversing the two lines breaks five tests, which is
how the non-vacuity pass confirmed the ordering is load-bearing rather than incidental.

---

## 5. Non-vacuity, and the three things it corrected

Six guards weakened. Three bit immediately; three did not, and each was a real problem.

| Weakened | What failed |
|---|---|
| publish setting `published_at` before writing the graph | 5 tests, starting with `test_publishing_normalises_the_draft_and_freezes_it` |
| publish ignoring `report.publishable` | `test_an_unpublishable_draft_is_refused_with_its_findings`, `test_a_refused_publish_leaves_nothing_behind` |
| `tool_call` added to `PUBLISHABLE_KINDS` | `test_a_tool_call_step_cannot_be_published_yet` |
| a missing step row ignored instead of raising | `test_the_three_writes_commit_together` |
| the execution row lock removed | `test_a_double_advance_produces_one_dispatch` — **after** §5.2 |
| `set_status` removed | nothing — **it was dead code**, §5.1 |

### 5.1 `set_status` was a second writer of a field that already had one

`advance_once` moved the execution from `pending` to `running` when it produced its first
dispatch. Removing that failed no test, because `start` appends
`workflow.execution_started` and folding it already sets `running` — the reducer's
`_EXECUTION_STATUS` maps it.

So the method was dead, and worse than dead: a second writer of a field that already had
one. `StepStatus`'s own docstring makes the rule — *one writer per field, because two is how
the two disagree* — and this was a quiet violation of it. Both the method and its caller are
gone.

### 5.2 The double-advance test did not race

The first version advanced an execution the run had already dispatched. Both rounds saw a
`dispatched` step, decided nothing, and the assertion passed **without either guard being
touched**.

The step is now put back to `pending`, and its first dispatch and event removed, so both
rounds genuinely decide. What happens then is the interesting part:

- **Both succeed, and only one decides.** The row lock serialises them, so the second waits,
  re-reads a step that is now `dispatched`, and decides nothing. No collision is needed, and
  that is the healthy path.
- **Remove the lock and the loser dies on the event dedup index.** Verified: one dispatch,
  one `step_dispatched` event, and an `IntegrityError` on
  `events_y2026m11_dedup_idx` for the other. The deterministic dedup key —
  `exec:<id>:<step>:dispatched:<attempt>` — is the first backstop.
- **The unique constraint is the second.** `UNIQUE (execution_step_id, kind, attempt)` is
  what catches it if the dedup key ever stops being deterministic, and the "exactly one
  dispatch" assertion is what notices.

Three layers, each separately demonstrated.

### 5.3 The atomicity test was testing the wrong failure

The first version reset a dispatched step and advanced again, expecting the forced failure at
the dispatch insert. The failure came from the **event** append instead — the dedup key was
already in `events`, so the key refused it before the step lookup was reached. The guard
working as documented meant the code under test was never exercised.

The patch is now applied *before* the run, so the event inside the failing transaction is a
new one. The result: `execution_started` survives, `step_dispatched` does not, no dispatch
row exists, and the step is still `pending`.

`execution_started` surviving is deliberate rather than a leak. `start` commits the execution
in its own transaction before advancing, so a graph that fails on its first decision leaves
an execution a user can look at instead of a 500 and nothing.

---

## 6. Open

- **No worker.** The dispatch sits `pending` and nothing claims it. T6 is the step runner,
  and until then the end-to-end managed agent execution exists up to the point of work
  being queued, not performed.
- **`tool_call` cannot be published**, §3.
- **One published version at a time.** `published_version` returns the newest, and a run
  always uses it. Running a specific older version is a feature with its own questions
  about which is current; `RunRequest` forbids a `version_id` rather than accepting one it
  would ignore.
- **`_settle`'s fallthrough.** T4 noted that `advance` can return nothing while a step is
  still pending behind an unapplied skip, and that the next call settles it. This service
  applies one round per call and does not loop, so the caller — T6's worker, after a
  dispatch reports — is what calls it again. A loop here would be the obvious fix and the
  wrong one: it would hide a decision function that never converges.
- Carried forward: **D7**, **D11**, **D12**, T1's tombstone names, T2's retry asymmetry,
  T3's unmade live provider call.
