# T0 — closing the blocking defects before orchestration

**Approved:** Q1 staged (T0–T8, T13), Q2 T0 mandatory, Q3 deterministic provider tests,
Q4 `advance()` in-process, Q5 CI first.

**CI status:** verified green on `bca9582` — run #18, all three jobs, every step. See
`08-PHASE-3-READINESS.md` §4. Nothing is blocked.

T0 touches six things. None of them is a rewrite: five are additive (a field, four
columns, two triggers, one column, one lock move) and one is a reordering inside an
existing transaction. No Phase 2 component is restructured.

Order matters — D2 before D1, because D1's tests need the step-status guard to assert the
right thing, and D6 before D1, because both change the same loop in `ingest.ingest()`.

---

## Work item D2 — stale-event protection across every folded table

### What is wrong

`last_event_at` exists on `agents` and `tasks` only. `runs`, `run_steps`,
`workflow_executions` and `execution_steps` are folded with `Patch(selector, fields)` and
no `not_before`, so a late event overwrites a newer one. Proven against the pure reducer:
an `execution_steps` patch comes back `guard=None`.

### Mechanism

Two guards, not one. A timestamp guard alone is too weak here, for a reason worth stating:
`not_before` compares with `<=`, so two events carrying the *same* `occurred_at` both
apply and the last one wins. A producer that stamps a whole batch with one timestamp could
therefore still revert a terminal step. Time ordering is the wrong invariant for a status
that is supposed to be final.

So:

1. **Timestamp guard** — migration `0017` adds `last_event_at timestamptz NULL` to `runs`,
   `run_steps`, `workflow_executions`, `execution_steps`. Nullable with **no default**,
   for the reason migration 0016 already records: a default of `now()` makes the guard
   refuse every event about a row created through the API.
   `_run`, `_workflow` and `_approval` gain `fresh = ("last_event_at",
   envelope.occurred_at)` and carry `last_event_at` in the patched fields, exactly as
   `_agent` and `_task` already do.

2. **Monotonicity guard** — `Patch` gains one optional field:

   ```python
   #: Refuse the patch when `column` already holds one of these values.
   #: A terminal step must not be reopened by a late or duplicate event.
   unless: tuple[str, frozenset[str]] | None = None
   ```

   Applied in `DerivedStateRepository._patch` alongside the existing predicate:
   `where = sa.and_(where, column.notin_(values))`. `StepStatus.is_terminal` and
   `_TERMINAL_STEP_STATUSES` already exist, so the set is not new vocabulary.

   Every step patch in `_workflow` and `_approval` carries
   `unless=("status", TERMINAL_STEP_STATUSES)` — **except `step_retried`**, which
   legitimately reopens a step and must not carry it. That exception is the thing a
   reviewer should look at hardest, and it gets its own test.

### Tests

`tests/unit/test_reducers.py`
- `test_every_execution_patch_carries_a_staleness_guard` — parametrised over all
  `workflow.*` and `approval.*` types; asserts `not_before` is set. A table-driven guard,
  so a new event type added without one fails.
- `test_terminal_steps_are_protected_except_by_retry` — `step_retried` has no `unless`;
  every other step patch has one.

`tests/integration/test_ingest.py`
- `test_a_late_step_started_does_not_revert_a_succeeded_step`
- `test_a_late_execution_running_does_not_revive_a_finished_execution`
- `test_a_same_timestamp_event_cannot_reopen_a_terminal_step` — the tie case the
  timestamp guard alone does not cover.
- `test_a_retry_event_may_reopen_a_terminal_step` — the exception, asserted positively.

### Non-vacuity

Each guard is removed one at a time and the corresponding test must fail by name. The
`0016` episode is the precedent: a guard that refuses everything looks identical to one
that works, so each test also asserts the **in-order** case still applies.

---

## Work item D6 — deterministic ingest serialisation

### What is wrong

`seen()` runs before `next_seq()`, so the cursor row lock is taken lazily — on the first
*accepted* event, and never at all for an all-duplicate batch. Two concurrent batches
sharing a `dedup_key` both pass `seen()`; the partition's unique index then raises on
insert, and because the batch is one transaction the whole request fails instead of
reporting one duplicate.

### Mechanism

`EventRepository.lock_project_cursor()` — the upsert and `SELECT ... FOR UPDATE` that
`next_seq()` does today, without the increment. `IngestService.ingest()` calls it once,
immediately after opening the transaction and before the loop. `next_seq()` keeps its own
lock acquisition (it is idempotent within a transaction and the repository stays correct
on its own), so nothing else that calls it changes behaviour.

This is what migration 0015 already says the design is — *"a project's ingest is
serialised"*. The lock was simply taken too late.

### Tests

`tests/integration/test_ingest.py`
- `test_two_concurrent_batches_with_one_dedup_key_report_a_duplicate` — two real
  concurrent transactions; one accepted, one reported duplicate, **no 5xx**.
- `test_an_all_duplicate_batch_still_serialises` — the case that took no lock at all.

### Non-vacuity

Reverting the lock to its lazy position must make the first test fail. If it does not, the
test is not actually concurrent and is worthless — so the test asserts both transactions
were open simultaneously before either committed.

---

## Work item D1 — event producer authority

### What is wrong

`EventSpec` says what an event is about and which tables it folds into. It says **nothing
about who may produce it**, and ingest has no event-type check. So any ingest key may post
any of the 44 types, including `approval.granted` (folds a step to `READY`, which
`advance()` will dispatch) and `workflow.step_succeeded` with arbitrary `output` that a
later step reads through the expression language.

### Mechanism

A closed vocabulary in `domain/protocol/taxonomy.py`, in the same spirit as `Condition` in
`policy.py` — four values, each a fact about who may originate an event:

```python
class Producer(StrEnum):
    #: Any ingest key, subject to the existing agent allowlist.
    AGENT = "agent"
    #: An ingest key, but only for a step currently dispatched to an agent
    #: that key may act as. Required by orchestration §6: a dispatchable
    #: agent reports its own step outcome.
    AGENT_FOR_DISPATCHED_STEP = "agent_for_dispatched_step"
    #: Written only by services/executions.py, inside the transaction that
    #: also writes the state and the outbox row. Never ingestible.
    ORCHESTRATOR = "orchestrator"
    #: Written only by a role-gated control-plane service. Never ingestible.
    CONTROL_PLANE = "control_plane"
```

`EventSpec` gains `producer: Producer = Producer.AGENT`. Assignment:

| Types | Producer |
|---|---|
| `agent.*`, `task.*`, `run.*`, `tool.*`, `log.emitted`, `metric.recorded` | `AGENT` |
| `workflow.step_succeeded`, `workflow.step_failed` | `AGENT_FOR_DISPATCHED_STEP` |
| every other `workflow.*` | `ORCHESTRATOR` |
| `approval.*` | `CONTROL_PLANE` |
| unknown types and `custom.*` | `AGENT` (no spec; folds into nothing) |

The default for an unknown type is deliberate and preserves the first compatibility
promise: an unrecognised type is still accepted and stored. It is safe because a type with
no spec folds into nothing, so it cannot move state.

**Enforcement, in two places because the two checks need different things:**

- `ORCHESTRATOR` and `CONTROL_PLANE` are refused in `IngestService._validated` — a pure
  check on the type alone, raising `PermissionDeniedError` with the event's `index`.
- `AGENT_FOR_DISPATCHED_STEP` needs a lookup, so it happens inside the transaction, before
  `append`. New repository method:

  ```python
  async def dispatched_step_agent(self, step_ref: str) -> str | None:
      """The agent_key of the step named by `step_ref`, when that step is
      currently dispatched or running. None when the step does not exist,
      is not in a dispatchable state, or has no agent."""
  ```

  The key must satisfy `may_act_as(that agent_key)`. A step that does not exist, is not
  dispatched, or belongs to another agent is a 403 — not a 404, consistent with the
  existing allowlist refusal, which already reasons that pretending otherwise sends an
  integrator hunting for a correct name.

### Why not a blanket ban

Orchestration §6 *requires* a dispatchable external agent to report
`workflow.step_succeeded`. Banning the namespace outright would remove the feature. The
rule has to be scoped to the step that was actually handed to that agent, which is why
`AGENT_FOR_DISPATCHED_STEP` exists rather than a boolean.

### Tests

`tests/unit/test_protocol_envelope.py`
- `test_every_known_event_declares_a_producer` — totality over `KNOWN_EVENTS`; a new type
  with no explicit producer and a folding target fails.
- `test_no_approval_or_orchestrator_event_is_agent_producible` — the security property as
  a table assertion, so it cannot drift silently.

`tests/integration/test_ingest.py`
- `test_an_ingest_key_cannot_grant_an_approval` — posts `approval.granted`; expects 403
  **and** asserts the step's status is unchanged, because a 403 that still folded would be
  the real failure.
- `test_an_ingest_key_cannot_report_an_execution_outcome` — `workflow.execution_completed`.
- `test_an_ingest_key_cannot_dispatch_a_step` — `workflow.step_dispatched`.
- `test_an_agent_may_report_its_own_dispatched_step` — the positive case: a step seeded as
  `dispatched` to that agent; accepted and folded.
- `test_an_agent_cannot_report_another_agents_step` — same step dispatched elsewhere; 403.
- `test_an_agent_cannot_report_a_step_that_was_never_dispatched` — step `pending`; 403.
- `test_an_unknown_type_is_still_accepted` — the compatibility promise, re-asserted.

### Non-vacuity

Widening `approval.granted` to `AGENT` must make
`test_an_ingest_key_cannot_grant_an_approval` fail. The positive test
(`test_an_agent_may_report_its_own_dispatched_step`) is what proves the rule is not simply
refusing everything — the failure mode that D8's blind guard and `0016`'s first staleness
attempt both had.

---

## Work item D3 — database-enforced immutability of published versions

### What is wrong

No trigger or rule exists anywhere in the schema. `agentworld_app` holds `UPDATE` **and**
`DELETE` on `workflow_versions`, `workflow_steps`, `workflow_edges`. `published_at` is
recorded and read by `WorkflowVersion.is_published`, but nothing consults it before a
write. Nothing tests it. It passes today only because nothing publishes.

### Mechanism

Migration `0018` adds one trigger function and three triggers.

```sql
CREATE FUNCTION agentworld_refuse_published_change() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'workflow version is published and cannot be changed'
    USING ERRCODE = 'restrict_violation';
END $$;
```

- On `workflow_versions`: `BEFORE UPDATE ... WHEN (OLD.published_at IS NOT NULL)`.
  The publishing update itself passes, because at that moment `OLD.published_at IS NULL`.
- On `workflow_steps` and `workflow_edges`: `BEFORE INSERT OR UPDATE`, refusing when the
  parent version is published. A row-level lookup per row is acceptable — publishes are
  rare, and normalisation happens before `published_at` is set.

**A constraint this places on T5.** Publish must insert `workflow_steps` and
`workflow_edges` **first**, then set `published_at`. The reverse order makes the trigger
refuse the publish's own inserts. This is written here so T5 does not rediscover it.

### The deliberate limit: UPDATE, not DELETE

The trigger does **not** cover `DELETE`, and that is a decision rather than an oversight.
`workflow_versions` is reachable by `ON DELETE CASCADE` from `projects` and `workspaces`.
A trigger that raised on delete would make any hard delete of a workspace fail partway —
turning an administrative operation into a half-applied one.

What covers deletion instead:

- `workflow_executions.workflow_version_id` is already `ON DELETE RESTRICT`, so a version
  that has **ever run** cannot be deleted at all.
- The application never hard-deletes a project: `ProjectRepository` has `archive()` and no
  `delete()`, and `projects.archived_at` is the real mechanism.

So the residual hole is exactly this: a published version that has never run can be
deleted by a direct `DELETE` or an administrative cascade. Editing it — the thing that
would silently change what a running execution means — is refused. **This is Q6 below.**

### Tests

`tests/integration/test_workflow_graph.py`
- `test_a_published_version_cannot_be_edited` — `UPDATE ... SET graph` **as the app role**,
  expecting the exception. Not through a service, because the point is that the privilege
  itself is now constrained.
- `test_an_unpublished_draft_version_can_still_be_edited` — the non-vacuity pair.
- `test_a_step_cannot_be_added_to_a_published_version`
- `test_publishing_itself_is_permitted` — the `OLD.published_at IS NULL` path.
- `test_a_version_that_has_run_cannot_be_deleted` — the existing `RESTRICT`, asserted so
  the pair of mechanisms is documented together.

---

## Work item D4 — immutable attempt identity, separate claim count

### What is wrong

Three statements in the design cannot all hold: claiming does `attempt = attempt + 1`; a
human retry inserts a new row at `attempt + 1`; and `UNIQUE (execution_step_id, kind,
attempt)` is what makes a double `advance()` collide. If claiming mutates `attempt`, the
unique key is not stable, so it no longer guards creation — and a claimed row incremented
to `attempt = 1` collides with a retry row inserted at `attempt = 1`.

### Mechanism

`attempt` becomes the **identity of the attempt**, set at insert and never updated.
Lease bookkeeping moves to a new column.

Migration `0019`:
- `ALTER TABLE dispatches ADD COLUMN claim_count integer NOT NULL DEFAULT 0`, with
  `CHECK (claim_count >= 0)`.
- A `BEFORE UPDATE` trigger on `dispatches` refusing any statement that changes `attempt`.
  Discipline in the claiming SQL is not enough: the whole point of the unique constraint
  is that it holds when a worker is wrong.

Claiming then does `claim_count = claim_count + 1` and leaves `attempt` alone. A retry
inserts a new row with `attempt + 1` and a **new** `idempotency_token`, as §8 requires.

### Tests

`tests/integration/test_execution_integrity.py`
- `test_attempt_cannot_be_updated` — the trigger, as the app role.
- `test_claiming_increments_claim_count_and_not_attempt`
- `test_a_retry_row_does_not_collide_with_a_claimed_row` — the exact collision D4
  predicts; it must not occur.
- The existing `test_one_dispatch_per_step_attempt` must still pass unchanged, which is
  what shows the unique key still guards creation.

---

## Work item D5 — duplicate protection for step-less dispatches

### What is wrong

`dispatches.execution_step_id` is nullable, and PostgreSQL treats NULLs as distinct in a
unique constraint. Any dispatch not tied to a step gets **no** protection from
`dispatches_step_attempt_key`.

### Mechanism

Make it `NOT NULL`. Every dispatch kind Phase 3 defines belongs to a step — a managed
agent turn, a tool call, an agent push, a task offer. There are no rows today, so the
migration is free, and `NOT NULL` is the strongest available statement of the invariant.

Migration `0019` (same migration as D4; they are one change to one table):
`ALTER TABLE dispatches ALTER COLUMN execution_step_id SET NOT NULL`.

If a later phase genuinely needs an execution-level dispatch, it adds a partial unique
index for that shape at the time, with the case in front of it. Loosening the column now
for a use that does not exist is how the hole got there.

### Tests

- `test_a_dispatch_requires_a_step` — insert with NULL; expect the not-null violation.
- The existing dispatch tests must pass unchanged.

---

## Commit sequence

Six reviewable commits, each with its tests green before the next starts:

1. `0017` + reducer guards + `Patch.unless` — **D2**
2. cursor lock moved to transaction start — **D6**
3. `Producer` + `EventSpec.producer` + ingest enforcement — **D1**
4. `0018` immutability triggers — **D3**
5. `0019` `claim_count`, immutable `attempt`, `execution_step_id NOT NULL` — **D4, D5**
6. docs: roadmap and `02-ORCHESTRATION.md` §3 corrected for the attempt/claim split, and
   `01-DOMAIN-AND-PROTOCOL.md` gains the producer table

After each: `ruff`, `mypy`, `lint-imports`, and the affected test files. The full suite and
a push before T1.

Per the standing instruction, every new guard is also verified by weakening it and watching
the matching test fail by name — recorded in the commit message, not just claimed here.

---

## Scope note on the execution-safety requirement

> *Until approvals are implemented in Phase 3a, consequential external actions must not be
> executable.*

T0 does not implement this, but it is worth recording where it lands and why it is nearly
free. `side_effect` is already an enum on `dispatches`, and `advance()` (T4) is the only
thing that creates a dispatch. The requirement becomes one refusal in `advance()`: a
decision whose `side_effect_class` is `consequential` is not emitted at all while approvals
do not exist. That is the same shape as the approval gate itself — the absence of a
dispatch row rather than a flag a worker checks — so Phase 3a replaces a refusal with a
gate rather than restructuring anything.

It gets its acceptance criterion in T4: *a workflow granting a consequential tool cannot be
run at all, and says so with a validation finding, rather than running and stopping
mid-way.* The `GRANTS_CONSEQUENTIAL_TOOL` condition and `approval_floor()` already exist in
`policy.py` to express it.

---

## Q6 — one new decision

**Should a published-but-never-run workflow version be deletable?**

The immutability trigger covers `UPDATE` and the step/edge tables. Covering `DELETE` as
well would make a hard delete of a workspace or project fail partway through its cascade,
which is worse than the hole it closes — and the application never hard-deletes a project
anyway, it archives.

- **(a) Leave DELETE uncovered (my recommendation).** Editing is refused; a version that
  has run is already undeletable via `RESTRICT`; deletion of a published, never-run version
  stays possible for administrative cascades. The residual hole is recorded here and in the
  migration.
- **(b) Cover DELETE too**, and accept that hard-deleting a workspace with published
  workflows becomes a multi-step operation requiring an explicit purge path.

Everything else in T0 follows the decisions you have already approved; this is the only
point where I had to choose, and the choice is visible in the schema rather than in a
comment.

---

## What actually happened

Recorded here rather than in a commit message, because two of these changed the plan above
rather than following it.

### Q6 was decided as (a)

`DELETE` is left uncovered, for the reason the plan gives: a trigger raising on delete
would make a hard delete of a workspace fail partway through its cascade. Migration 0018
states the residual hole in its own docstring — a published version that has never run can
still be removed by a direct `DELETE` or an administrative cascade — and the two mechanisms
that cover the rest (`ON DELETE RESTRICT` from `workflow_executions`, and the fact that
`ProjectRepository` archives rather than deletes) are asserted together in
`test_a_version_that_has_run_cannot_be_deleted`. Say so if you want (b) instead; it is a
migration, not a redesign.

### The timestamp guard was not independently exercised, and the non-vacuity check is what found it

Weakening `not_before` made **no test fail**. The monotonicity guard was quietly covering
every case the suite had, because every out-of-order case written so far happened to
involve a settled status. A guard nothing tests is a guard that can be removed by accident,
so two more tests were added for the cases only the timestamp guard can catch — both
statuses live, so `unless` has nothing to say:

- `test_a_late_event_cannot_move_a_step_backwards_between_live_statuses` — a replayed
  `step_dispatched` arriving after `step_started`.
- `test_a_late_pause_cannot_override_a_newer_resume`.

Both now fail when `not_before` is weakened, and `test_a_same_timestamp_event_cannot_reopen_a_settled_step`
fails when `unless` is weakened. The two guards are independent and each is checked.

### D1 broke a legitimate test, correctly

`test_folding_a_workflow_trace_creates_no_dispatches` — the live half of promise 7 — posted
`workflow.execution_started` and `workflow.step_dispatched` through the **ingest endpoint**,
which D1 now refuses with a 403. The test's property is still exactly right; the layer was
wrong. It now appends, folds and applies in one transaction, which is the path
`services/executions.py` will take, so it asserts the property where the orchestrator will
actually live. The thing it used to assert incidentally — that an API key can write those
events — is the defect.

Two test fixtures also needed reordering: `test_execution_integrity.py` published its
versions before inserting their steps, which migration 0018 now refuses. That is the
ordering constraint T5 inherits, and the fixture was the first thing to find it.

### Non-vacuity, as run

Each guard was weakened in turn and the matching test had to fail by name.

| Weakened | Tests that failed |
|---|---|
| `Patch.not_before` predicate | the two live-status ordering tests |
| `Patch.unless` predicate | `test_a_same_timestamp_event_cannot_reopen_a_settled_step` |
| cursor lock back to lazy | the concurrent-duplicate test, with `UniqueViolationError` on `events_y2026m11_dedup_idx` — the race is real, not theoretical |
| `approval.granted` → `Producer.AGENT` | `test_an_ingest_key_cannot_grant_an_approval` plus two taxonomy totality tests |
| version-immutability trigger | `test_a_published_version_cannot_be_edited`, `..._cannot_be_unpublished` |
| `attempt` trigger, `execution_step_id NOT NULL` | `test_attempt_cannot_be_updated`, `test_a_dispatch_requires_a_step` |

### Still open

Nothing in T0. The items carried forward from the readiness review and **not** addressed
here, because they are not blockers for orchestration: D7 (per-month dedup, which
double-counts `runs.step_count` and `runs.tool_call_count` on a cross-month retry), D11
(deleting an agent leaves its island node), D12 (the `.shell__mark` contrast bug).
