# T6b — the way out of `needs_review`

T6 created a state no code can leave. A dispatch whose lease expired while
`side_effect_started_at` was set is parked in `needs_review`, and that is
deliberate: nobody knows whether the external effect landed, so no worker, no
reaper and no timer may touch it. The design was right and the consequence was
an execution stuck for ever.

T6b is the authorized way out, and nothing more. No row-level history
migration, no notification infrastructure, no retry system. The condition set
for this task was that a row-level history migration had to be *demonstrated*
necessary before being written, and it was not — see §5.

---

## 1. What was built

| File | What it is |
| --- | --- |
| `services/review.py` | `ReviewService.list_needing_review` / `.resolve`, `Resolution`, `ReviewItem` |
| `api/v1/review.py` | `GET …/dispatches`, `POST …/dispatches/{id}/resolve` |
| `domain/policy.py` | `Action.DISPATCH_RESOLVE_REVIEW`, `admin`, in `SENSITIVE_ACTIONS` |
| `repositories/dispatches.py` | `needing_review`, `resolve_review` — an **update**, not an insert |
| `repositories/audit.py` | `count_by_resource`, which is where recovery history comes from |
| `api/deps.py`, `main.py` | the dependency and the router |

No new writer of `dispatches` rows. `repositories/executions.py` is still the
only file in the system that may `INSERT` one, and the containment test still
says so — T6 split its allow-list by verb precisely so that T6a and T6b could
update a dispatch without being allowed to create one:

```python
MAY_CREATE_DISPATCHES = frozenset({"repositories/executions.py"})
MAY_UPDATE_DISPATCHES = MAY_CREATE_DISPATCHES | {"repositories/dispatches.py"}
```

## 2. The split in privilege, which is the design

Seeing that an execution is stuck is `execution.read` at **viewer**. An
operator should be able to look at the queue; hiding it helps nobody.

Deciding what happened is `dispatch.resolve_review` at **admin**, a new action.
The reviewer is not editing a record. They are asserting a fact the system
could not establish — "the provider's dashboard shows the call was served" —
and taking responsibility for it. That is why the note is mandatory, why the
resolving user's identity is recorded, and why the action is in
`SENSITIVE_ACTIONS`.

## 3. Three outcomes, and why `retry` is different

| Outcome | Dispatch becomes | What the person is asserting |
| --- | --- | --- |
| `succeeded` | `SUCCEEDED` | the effect happened; here is the output |
| `failed` | `DEAD` | it did not happen and is not worth another attempt |
| `retry` | `PENDING` | it probably did not happen, and I accept it may happen twice |

`failed` maps to `DEAD`, not to `FAILED`. `FAILED` is the retryable status the
worker uses; a human saying "this is over" must not leave a row a worker can
pick up again.

`retry` requires `acknowledged: true`. It is the one outcome that *acts* on the
assertion rather than recording it, and acting may repeat an effect that
already happened. Refusing without the flag is the system declining to infer
consent from a button press. `Resolution.needs_acknowledgement` is true for
`RETRY` alone.

Nothing here retries automatically. `needs_review` is still outside every
automatic path; `retry` is a person choosing to re-enter the queue, and the
dispatch then goes through the ordinary claim-and-run route with its original
idempotency token — `test_a_retried_dispatch_can_be_claimed_and_run`.

## 4. Resolving exactly once

Two things guard it, at different layers, and the second is the one that
matters under concurrency:

1. The service re-reads the dispatch and refuses with `ConflictError` if it is
   not in `needs_review`. This is what a second human click hits.
2. The repository's `UPDATE` carries `status == NEEDS_REVIEW` in its `WHERE`
   clause and returns nothing if it matched no row. This is what the loser of
   a genuine race hits.

The resolution, the step outcome event and the audit row are one transaction.
A resolution that cannot be audited does not happen.

## 5. Why no migration

The requirement was to preserve attempt history and previous recovery records.
`attempt` is immutable and `claim_count` already counts claims, so what was
missing was only the *recovery* count — and every recovery already writes a
`dispatch.recovered` audit row. `ReviewItem.previous_recoveries` counts those
rows (`count_by_resource`), so the reviewer sees "this has been round three
times" without a new column and without a history table.

`audit_log` is append-only by privilege, not by convention: the application
role has `INSERT` and `SELECT` on it and nothing else. That is a stronger
guarantee than a column I would have had to protect myself.

## 6. The bug the audit trail nearly recorded

`resolve_review` does `UPDATE … RETURNING`, which refreshes the SQLAlchemy
instance in the identity map. The audit payload read `existing.last_error`
*after* that — so `previous_error` held the resolution's own note instead of
the error being resolved. The audit row was quietly describing the present as
the past.

Fixed by capturing the values before the update:

```python
previously = {
    "attempt": existing.attempt,
    "claim_count": existing.claim_count,
    "error": existing.last_error,
    ...
}
```

`test_the_audit_row_names_the_person_and_keeps_the_note` fails if the read
moves back after the update — the `read-after-update` non-vacuity case.

## 7. The tests

Twenty functions, twenty-three cases (the empty-note test is parametrised over
`""`, `"   "`, `"ok"`, `"done"`), in `tests/integration/test_review_api.py`.

**The queue**
- `test_the_queue_shows_what_is_stuck_and_why` — a viewer sees the stuck row,
  its error and its attempt counts.
- `test_an_empty_queue_is_an_empty_list` — nothing stuck is `[]`, not a 404.
- `test_the_count_grows_with_each_recovery` — two trips through the reaper,
  two `dispatch.recovered` rows, both visible. This is §5's claim as a test.

**Authorization**
- `test_a_viewer_may_look_and_may_not_resolve` — the split itself.
- `test_a_member_may_not_resolve_either` — `execution.retry_step` is `member`;
  this is not that, and the nearest existing role is not good enough.
- `test_a_non_member_sees_nothing`.
- Plus the 178-case role matrix, which now covers the new action.

**The three outcomes**
- `test_asserting_it_succeeded_advances_the_execution` — the execution
  finishes, through `ExecutionService`, not a second path.
- `test_asserting_it_failed_fails_the_execution` — and the dispatch is `DEAD`.
- `test_retry_requires_acknowledgement` — refused without the flag.
- `test_a_retried_dispatch_can_be_claimed_and_run` — the retry is real work: a
  worker claims it and runs it with the same idempotency token.

**Resolving once**
- `test_a_second_resolution_is_refused` — the second click is a 409.
- `test_two_concurrent_resolutions_produce_one` — two `ReviewService.resolve`
  calls raced on one event loop, each in its own transaction. One wins, one
  gets `ConflictError`, and there is exactly one audit row.
- `test_a_dispatch_not_awaiting_review_cannot_be_resolved`.

**The budget and the project boundary** — both added during the final review,
because both properties were true by construction and exercised by nothing.
- `test_a_retry_resolution_does_not_grant_another_attempt` — `attempt`,
  `max_attempts` and `claim_count` are unchanged by a resolution. Fails if
  `resolve_review` is given so much as a `max_attempts + 1`.
- `test_the_queue_and_the_resolution_stop_at_the_project_boundary` — the same
  `owner` cannot read or resolve a dispatch through a sibling project. Every
  other case in this file uses one project, where a missing `project_id`
  predicate is invisible; this one resolves through the wrong project's URL
  and requires a 404.

**The record**
- `test_the_note_floor_is_the_services_rule_not_only_the_schemas`.
- `test_a_note_that_says_nothing_is_refused` ×4.
- `test_the_audit_row_names_the_person_and_keeps_the_note` — §6's bug.
- `test_resolving_preserves_every_earlier_recovery_record`.
- `test_a_failed_resolution_leaves_the_dispatch_stuck` — if the audit insert
  fails, the status change rolls back with it.

### Two tests of mine that were not doing their job

Both were found by the non-vacuity pass, not by reading them.

**The concurrency test was not racing.** Its first version fired two
`TestClient` requests through `asyncio.to_thread`. Starlette's test portal
serialises requests onto one loop, so the two never overlapped: the service's
pre-read check caught the second, and removing the status guard from the
`UPDATE` broke nothing. Rewritten to call the service directly, twice,
concurrently — now the guard is the only thing standing between the two.

**The note floor was shadowed.** `ResolveRequest.note` has
`min_length=MIN_NOTE_LENGTH`, so over HTTP a short note is a 422 from pydantic
and the service's own check is never reached. Removing the service check broke
nothing. The floor is now tested at the service, where it is enforced for every
caller rather than only for the one that arrives through the schema.

## 8. Limitations

- **The reviewer is trusted absolutely.** `succeeded` records whatever output
  the request carries, with no evidence that the effect happened. That is
  inherent: if the system could verify it, the dispatch would not be in
  `needs_review`. The mitigation is the audit row and the admin-only action,
  not a check.
- **No notification.** A dispatch can sit in `needs_review` indefinitely and
  nothing tells anyone. Polling the queue is the only discovery mechanism,
  which was the approved scope — "keep polling for now" — but it means the
  time-to-resolution is unbounded in practice.
- **The queue is project-scoped**, and there is no queue-wide view. Both routes
  are addressed by project and filtered on `project_id` in SQL, under the
  workspace's forced RLS. An operator with twenty projects makes twenty
  requests to see everything that is stuck.
- **The dispatch row's own `last_error` is replaced** by the resolution note,
  because that is the column an operator reads and the resolution is the last
  thing that happened. The reason the dispatch was escalated is not lost: it
  is in the `dispatch.recovered` audit row that escalated it, and in this
  resolution's audit row as `previous_error`. Nothing can edit either.
- **`retry` grants no extra attempts.** The retry budget is unchanged, so a
  dispatch already at its ceiling can be resolved as `retry` and then fail
  again immediately. *(When this was written the ceiling was unreachable -
  the Phase 3 acceptance review found the budget was never consumed at all.
  With that fixed, bounded on `claim_count`, this limitation is real rather
  than theoretical. Behaviour as approved; see `15-T6-WORKER.md` §9.)* Whether a human
  resolution should raise the ceiling is a T8 question, not answered here.
- **The advance is a second transaction.** The resolution, the step event and the audit
  row commit together; `advance_once` then runs after that block, because it takes the
  execution's row lock and decides from committed state — it has to see the step as
  finished. So a crash between the two leaves a resolved dispatch whose execution has not
  been advanced, and nothing schedules a retry of the advance. This is **not new in
  T6b**: `DispatchWorker.report` has the same shape for the same reason, and it is
  recorded here only because the review path inherits it rather than fixing it. It is
  **not solved**, and it is tracked as its own task: `18-T6C-ADVANCEMENT-RECOVERY.md`,
  which sets out the gap, rejects folding the advance into one transaction, and
  recommends a reconciliation sweep over a derived condition inside the existing reaper
  process.
- **No undo.** A resolution is final; a mistaken `succeeded` can only be
  followed by a new execution. Reversing one would mean a second audited
  action, which was not in scope.

## 9. Open decisions

- **T8 retry asymmetry** (carried from T6): a blocked destination and an
  unreachable host are indistinguishable in step output by design, but they
  deserve different retry treatment — one will never work, the other might.
- **Does a human `retry` deserve a fresh attempt budget?** See §8.
- **Reaper deployment shape** (carried from T6a): a long-running worker or a
  scheduled `--once` invocation. The code supports both; nothing in the
  repository chooses.
- **`tool_call` steps remain unpublishable** until Phase 6, so no tool
  dispatch can reach this queue yet.

## 10. Non-vacuity

Sixteen protections, removed one at a time, each run against the tests that
are supposed to notice. Every one is now exercised by at least one named test.

| Weakening | What fails |
| --- | --- |
| The `authorize` call becomes `pass` | `test_a_viewer_may_look_and_may_not_resolve`, `test_a_member_may_not_resolve_either` |
| The action's minimum role becomes `member` | `test_a_member_may_not_resolve_either` |
| The `UPDATE`'s `status == NEEDS_REVIEW` predicate | `test_two_concurrent_resolutions_produce_one` |
| The service's status pre-read | `test_a_dispatch_not_awaiting_review_cannot_be_resolved`, `test_a_second_resolution_is_refused` |
| The acknowledgement gate | `test_retry_requires_acknowledgement` |
| The note floor | `test_the_note_floor_is_the_services_rule_not_only_the_schemas` |
| `failed` → `FAILED` instead of `DEAD` | `test_asserting_it_failed_fails_the_execution` |
| Reading `last_error` after the `UPDATE` | `test_the_audit_row_names_the_person_and_keeps_the_note` |
| The step outcome event | `test_asserting_it_succeeded_advances_the_execution`, `test_asserting_it_failed_fails_the_execution` |
| Leaving `side_effect_started_at` set | `test_asserting_it_succeeded_advances_the_execution`, `test_retry_requires_acknowledgement` |
| The audit insert | five tests, including `test_a_failed_resolution_leaves_the_dispatch_stuck` |
| The recovery count | `test_the_count_grows_with_each_recovery`, `test_the_queue_shows_what_is_stuck_and_why` |
| The action's `MINIMUM_ROLE` entry | `test_every_action_has_a_minimum_role`, and 11 matrix cases |
| The action's `SENSITIVE_ACTIONS` entry | `test_resolving_an_uncertain_outcome_is_sensitive` |
| `max_attempts + 1` in `resolve_review` | `test_a_retry_resolution_does_not_grant_another_attempt` |
| The `project_id` predicate on the read behind the 404 | `test_the_queue_and_the_resolution_stop_at_the_project_boundary` |

**Five of those sixteen rows only became true during this pass**, which is the
part of this section worth reading. Two were tests that passed while proving
nothing, described in §7. The other three had no test at all:

**`SENSITIVE_ACTIONS`.** The set is spot-checked for `credential.*` and
`integration.*` only, so removing `dispatch.resolve_review` from it broke
nothing — and that set drives the audit digest and the consent prompts, so the
most consequential human action on an execution would have been missing from
both. `test_resolving_an_uncertain_outcome_is_sensitive` was added for it.
Writing it turned up that `execution.cancel` is not in the set either; that is
left alone and recorded in §9, because it is a Phase 2 call.

**The attempt budget.** `resolve_review` never touches `attempt`,
`max_attempts` or `claim_count`, so "a retry grants no extra attempts" was
true by construction — and nothing would have noticed a later, well-meaning
`attempt + 1` meant to give the human's retry a fair chance. The consequence
of that change is a dispatch that can exceed its own ceiling, which is the one
thing the ceiling is for.

**The project boundary.** Every other case in the file uses a single project,
where a missing `project_id` predicate is invisible, and the resolving person
is `owner` of every project in the workspace — so role alone does not stop
them resolving a sibling project's dispatch by putting its id in a URL. The
new test resolves through the wrong project and requires a 404.

The pattern in all three: a property that is true because of how the code is
shaped rather than because something checks it. Those are the ones that break
quietly later, and they are invisible to a review that only reads the tests
that exist.

### A probe that was reporting nonsense

The case for "the action exists in the matrix the documents quote" renamed the
enum member. The tests import that member by name, so the run ended in a
**collection error** — pytest exit 4 — and the harness, which treats any exit
other than 1 as "nothing failed", reported the protection as untested. It was
neither tested nor untested; nothing had run.

Replaced by two probes that remove what the member is *registered in* rather
than the member itself: its `MINIMUM_ROLE` entry and its `SENSITIVE_ACTIONS`
entry. Both bite. The harness's exit-code reading is still crude, and worth
remembering before trusting a "nothing failed" line from it.
