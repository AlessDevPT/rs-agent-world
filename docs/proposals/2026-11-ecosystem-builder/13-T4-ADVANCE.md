# T4 — `advance()`, the pure decision function

**Status: implemented.** 46 tests, no fixtures, no database, no network, no clock.

```python
def advance(definition: WorkflowDefinition, state: ExecutionState) -> tuple[Decision, ...]
```

The first task in Phase 3 that is purely domain logic. Everything before it was
infrastructure, where a test needs a socket or a schema; here the cases that actually
produce orchestration bugs — a branch not taken, a join with one arm still running, a step
whose agent was deleted after publish — are cheap to enumerate, and enumerating them
against a live worker would be impossible.

---

## 1. What was built

| | |
|---|---|
| `domain/orchestration/state.py` | `ExecutionState`, `StepState` — what the function is told |
| `domain/orchestration/decisions.py` | the closed union it may answer with |
| `domain/orchestration/advance.py` | the function |

No migration, no service, no caller. T5 is the service that applies decisions
transactionally; `advance` lands first and alone, so the decision logic is tested before
anything can perform a decision.

---

## 2. The four properties, and how each is checked

**Pure.** Asserted from the module's own AST rather than from `sys.modules`: every `import`
and `from … import` node must name the standard library or `agentworld.domain`. Reading
`sys.modules` would miss an import inside a function, and a function-level import is
exactly how a pure function stops being one. The architecture contracts already forbid a
framework or a provider SDK anywhere in `domain/`; this is the narrower claim.

**Deterministic.** `now` is a field, not a call — and that is checked the same way, by
walking the syntax tree for a call to anything named `now`, `utcnow` or `monotonic`. A
second test runs the same state twice with clocks seven months apart and asserts the
decisions are identical; the only place `now` reaches is
`CompleteExecution.decided_at`. Nothing generates an id: `dispatches.id` and
`idempotency_token` come from database defaults, where they are produced once per row
rather than once per call. Iteration follows the author's step order, never a set's.

**Idempotent.** A decision is emitted only for a step whose state does not already reflect
it, so the same state gives the same tuple. A recorded three-state sequence is replayed
twice, decision for decision — which is the property that lets a production incident become
a unit test.

**Total.** Every state produces a decision list, including states that should not exist. A
definition that no longer validates, a step whose kind cannot run, a state naming a step the
definition does not have, a condition that cannot be evaluated: each is a decision, never an
exception. An orchestrator that raises on malformed input leaves the execution exactly as
stuck as the input was malformed.

---

## 3. What this phase can execute

Deliberately narrow. Executable: `condition`, `transform`, `tool_call` whose tool is `read`
or `write`, and `agent_task` on a **managed** agent. That is precisely the "real end-to-end
managed agent execution with explicitly granted, safe capabilities" the scope decision asked
for.

Everything else is refused with a reason, by one predicate — `_not_executable_yet` — which is
the seam Phase 3a and T8 reopen:

| Refused | Arrives with |
|---|---|
| a step that requires approval, or an `approval` step | approvals, Phase 3a |
| a `tool_call` whose tool is `consequential` | approvals, Phase 3a |
| `agent_task` on a `dispatchable` agent | dispatchable agents, Phase 3a |
| a `wait` step | a step configuration shape that can express a duration |

**The consequential refusal is the execution-safety requirement made structural.** No
decision this function returns can carry `side_effect_class = consequential`, so no dispatch
row for consequential work can exist, so no worker can find one. A successful model response
is never authority to act, because the authority to act *is* a dispatch row and `advance`
will not write one. `dispatchable_effects()` exports the constant the function actually uses,
and the test asserts against that rather than against a copy.

It matters that this refusal is at decision time rather than at publish time.
`validate` reports a consequential step with nothing gating it as a **warning**, not an
error — deliberately, because gating it is the approval policy's call and refusing to publish
would be the validator overruling it. So such a workflow *is* publishable, and this refusal
is the only thing between a published workflow and an irreversible side effect.

### `wait` steps: a gap, not a decision

`StepDefinition` has no field that can express a duration or a signal, so a `wait` step
cannot be configured, let alone executed. Rather than inventing a field, it is refused with
a reason that says so. Adding it is a definition change and a migration, and it should be
driven by someone who wants a workflow to wait rather than by this task.

---

## 4. The observed agent

Refused, and for a reason that is **permanent rather than a phase gate**: an observed agent
reports its own activity and is never given work. No dispatch kind can reach one, asserted
over every `StepKind` rather than just `agent_task`.

There are two layers, and the second is currently redundant. `validate` reports
`OBSERVED_AGENT_ASSIGNED` as an **error**, so the gate at the top of `advance` already
refuses any definition naming one, and `_not_executable_yet` can never fire through the
public function today. It is kept and tested directly, because the severity of that finding
is a product decision: `CONSEQUENTIAL_WITHOUT_APPROVAL` is a warning for reasons the
validator's own docstring explains, and if the observed-agent finding were ever softened the
same way, this is what would still refuse to hand an observed agent work.

The same shape appears twice more — `_condition_holds` and `_mapped` both catch a parse
failure that `validate._expressions` has already made impossible. Both are tested directly
and documented as defence in depth rather than left looking load-bearing.

---

## 5. Three decisions deliberately absent

`02-ORCHESTRATION.md` §4 names eight members of the union. Five are here. The other three
are not, and not by oversight:

- **`RequestApproval`** arrives with approvals in Phase 3a. There is no `approvals` table,
  so a decision nothing can apply would be a branch with no behaviour and a test asserting
  the branch exists.
- **`RetryStep`** arrives with the retry policy in T8. Classifying a failure as retryable
  needs the backoff, the jitter, the attempt budget and the consequential-work exception;
  inventing a shape for those now means T8 inheriting a guess.
- **`PauseExecution`** is not an `advance` output at all, on reflection. Pausing is
  something an operator does *to* an execution; `advance` observes a paused execution and
  declines to act. A decision function that could decide to pause would need a reason to,
  and there is none.

`test_every_decision_type_is_reachable` and `test_the_union_has_no_member_without_a_test`
are the pair that keeps that honest: one drives the function until every member has been
emitted, the other reads the union off the type. A member nothing emits fails the second.

---

## 6. Acceptance criteria

| Criterion | Test | |
|---|---|---|
| Unit tests pass with no database and no network | the whole file — no fixtures — plus `test_advance_imports_nothing_it_should_not` | ✅ |
| Called twice on identical state it returns identical decisions | `test_the_same_state_twice_gives_the_same_decisions`, `test_a_recorded_sequence_replays_to_the_same_decisions` | ✅ |
| It refuses to target an `observed` agent — no dispatch kind can reach one | `test_no_dispatch_kind_can_reach_an_observed_agent`, `test_only_a_managed_agent_can_be_given_work` | ✅ |
| A graph that `validate()` rejects is never advanced | `test_a_graph_validate_rejects_is_never_advanced` (4 kinds of invalid graph) | ✅ |

The fourth is checked at *decision* time rather than trusted from publish time, and that is
not belt-and-braces. A version that validated when it was published stops validating when an
agent is deleted or a tool grant is revoked — and the definition handed to `advance` carries
those facts, because the service assembles it from current rows.

---

## 7. Non-vacuity, and the three holes it found

Ten guards weakened. **Three failed no test at all**, and each was a real gap rather than a
redundant guard.

| Weakened | What failed |
|---|---|
| the `validate` gate removed | `test_a_graph_validate_rejects_is_never_advanced` (all 4) + 1 |
| `consequential` added to the dispatchable effects | `test_a_dispatch_can_never_carry_consequential_work`, `test_a_consequential_tool_is_refused_before_any_dispatch_exists` |
| the whole phase gate removed | 3 refusal tests |
| a join taking **any** satisfied edge instead of all | `test_a_join_whose_arm_was_skipped_does_not_run_on_half_its_input` — **added** |
| a not-taken branch left pending instead of skipped | `test_a_branch_not_taken_is_skipped_rather_than_left_pending`, + the reachability guard |
| a failed step no longer failing the execution | `test_a_failed_step_fails_the_execution_with_its_own_reason` + 1 |
| completion output taken from every step, not the leaves | 2 tests |
| the in-flight check removed from settling | `test_a_failure_waits_for_work_that_is_still_in_flight` — **added** |
| a condition that cannot be evaluated raising instead of false | `test_an_unparseable_condition_is_false_at_the_decision_layer` — **added** |

### 7.1 The join test was testing the wrong half

`test_a_join_waits_for_every_arm` asserts the join waits while one arm is still working —
and **"any" does that too**, because an unfinished arm is not terminal and readiness returns
`WAITING` before the all/any question is reached. The distinction only appears once every arm
has *finished* and not all of them succeeded: with "any" the join runs with one input
missing, with "all" it is skipped. That is the case that would corrupt a real execution, and
it had no test.

### 7.2 The in-flight check looked redundant and is not

Removing it changed no outcome, because with nothing else wrong the "all terminal" check
gives the same answer. It differs in exactly one state: something has **failed** and
something else is still **running**. Without the check the execution fails immediately and
the running dispatch reports its outcome into an execution that is already over.

### 7.3 Two `except` clauses are unreachable today

`validate._expressions` parses every step condition, every mapping expression and every edge
condition, and an unparseable one is an error — so the gate at the top of `advance` refuses
the definition before any expression is evaluated. The `try` blocks in `_condition_holds`
and `_mapped` are defence in depth against the validator stopping covering something. They
are now tested directly, which is the difference between defence in depth and a comment.

---

## 8. A flaky test found by running the suite

`test_two_heartbeats_in_one_second_are_one_fact` failed on the full run for T4, in
`test_ingest.py`, which this task does not touch. It is not a regression and it is not a
product defect — it is a test that was only ever true by luck.

The heartbeat dedup key is `<agent>:hb:<integer second>`, which is the documented and
correct behaviour: a worker heartbeating twice within one second is retrying, not reporting
twice. The test posted two real HTTP requests and assumed they would land in the same
second. They were about ninety milliseconds apart, so they straddled a second boundary
roughly nine times in a hundred — and did, on this run, after passing in every run before
it.

A test that passes nine times out of ten is worse than no test. It gets believed when it
fails for a real reason, and it teaches whoever sees it to re-run the suite instead of
reading the failure.

The fix pins the clock, so the test asserts the documented property every time rather than
usually. A second test was added for the other half — a heartbeat a second later *is* a new
fact — because pinning the clock alone would hide a dedup key that ignored the second
entirely: every heartbeat would be a duplicate and the first test would still pass. Dropping
the second from the key fails the new test, which is what makes the pair meaningful. The
pinned test was then run ten times in a row to confirm it no longer depends on timing.

The clock is patched by subclassing `datetime` rather than by substituting a stub with one
method, because `services.ingest` also uses `datetime` for the future-skew check and a bare
stub would break that for reasons unrelated to the test.

---

## 9. Verification pass

Requested after T4 was committed. Everything below was run against the final tree.

### Every path that can create a dispatch row

**There are none.** No statement anywhere in `src/` inserts, updates or deletes a
`dispatches` row — verified by search, and now enforced by
`tests/unit/test_dispatch_containment.py`, whose allow-list of permitted writers is
**empty**. T5 adds `services/executions.py` to that list in the same commit that adds the
writer, so the one-line diff is the moment a reviewer looks at the only code in the system
that can cause an irreversible action.

Four layers stand behind the guarantee, and they are not equally strong:

| Layer | What it stops | Enforced by |
|---|---|---|
| `MutationTarget` has no `dispatches` member | a folded event creating work | a closed vocabulary, checked by a test |
| `DerivedStateRepository._TABLES` is keyed by that vocabulary | the repository writing one if asked | construction |
| `advance()` never returns `side_effect_class = consequential` | consequential work being decided at all | a constant, plus the test that reads it |
| no statement in `src/` targets the table | anything else | the containment test |

**The database is not one of those layers, and that is worth stating.** `agentworld_app`
holds `INSERT`, `UPDATE`, `DELETE` and `SELECT` on `dispatches` — unlike `events`, which is
append-only by privilege. It has to, because T5 and T6 will write and claim rows. Revoking
the grant until then would be a migration that T5 immediately reverses, so the containment
today rests on application code and tests, not on privilege. RLS is enabled and forced on
the table, so a row written in the wrong tenant context would be invisible rather than
harmless — but nothing writes one.

### Reporting into a settled execution

Two independent guards, and the verification found that they overlap:

- the early return for an execution that is not live, and
- the in-flight check in `_settle`.

Removing **either** leaves all three new tests passing. Removing **both** fails all of
them. That is a real finding about the tests rather than about the code: each guard is
individually unnecessary for these cases and jointly load-bearing, so the honest claim is
"the pair holds", not "each holds".

What the guarantee does and does not cover:

- **Covered:** a worker reporting a step outcome after the execution has completed, failed
  or been cancelled produces **no decision**, so no new work starts and the execution is
  not reopened. A step still `PENDING` in a cancelled execution is never dispatched.
- **Covered elsewhere:** a late event cannot move the *execution* backwards — the
  terminal-status guard from T0, with
  `test_a_late_running_does_not_revive_a_finished_execution` in `test_stale_events.py`.
- **Not covered, and deliberate:** the *step's* own status still moves. A step that was
  dispatched before the execution failed will be recorded as succeeded when its worker
  reports, so a failed execution can contain a succeeded step. That is accurate
  bookkeeping — the work really did happen — and refusing it would lose the record. It is
  stated here because "a failed execution with a succeeded step" looks like a bug in a
  listing and is not one.

---

## 10. Open

- **No caller.** T5 is the service that applies decisions in one transaction — event, state
  change, and for `DispatchStep` alone an outbox row. Until then `advance` is a function
  with tests and no production path, which is the intended order.
- **`wait` steps cannot be configured**, §3.
- **The `_settle` fallthrough returns nothing** in one state: nothing ready, nothing in
  flight, something still pending behind a branch whose skips have not been applied yet. The
  next call settles it, and the comment says so. If T5's apply loop ever stops calling
  `advance` again after applying, that becomes a stuck execution — which is a note for T5
  rather than a defect here.
- Carried forward: **D7**, **D11**, **D12**, T1's tombstone name collision, T2's retry
  asymmetry, and T3's unmade live provider call.
