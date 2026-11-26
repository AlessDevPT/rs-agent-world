# M4 — the publish producer: review

> **A review of one change, written 2026-11-26 and kept as the verification record for
> it.** The change it reviews is `d70eb5d`, which wired `ExecutionService._publish` and
> **completed all fourteen producers of P13's inventory**. The decision it asked for was
> taken and is now recorded in P13 §5 and R8 (`b07a6d5`), so the section below headed as
> open is kept for its reasoning, not as a question. CI was green on both commits.
>
> This is a point-in-time document. Where it disagrees with
> [07-DECISION-RECORD.md](07-DECISION-RECORD.md), that record wins.

## The decision this change needed — since taken

**P13 section 5 did not cover publishing a harness, and the implementation had to
choose.** As section 5 stood before the amendment, it excluded the harness *writer* -
`services/agent_test.py` creating a `workflows` row through `find_or_create_test` - and
said nothing about `publish_test_harness`, which
`agent.test` calls conditionally: normally once per agent, on its first test, since
`AgentTestService._harness` uses `current_version_id is None` as the publish-first
predicate, and again only if an earlier publish failed and left no pointer. Meanwhile
section 2 described `_publish` emitting one event with no condition attached, and section
3 says "no publish may commit with its event unwritten".

So the record pointed two ways for one case. The publish **skips the event for a
harness**, because:

- Section 5's stated *reason* transfers intact: `WorkflowRepository.list_all` filters
  `test_for_agent_id IS NULL`, so a harness is in no listing a client subscribes to, and a
  delta for one "would describe a change to a collection no client holds" - which is as
  true of publishing one as of creating one.
- **Corrected 2026-11-26:** an earlier version of this review argued that emitting would
  put a *stream* of `definition.workflow.updated` for invisible workflows into every
  project that tests an agent. That was wrong, and it followed from the frequency error
  above: the real volume is at most one event per agent. **The exclusion rests on the
  listing argument alone**, which is P13 section 5's own reason and is unaffected.
- An unemitted event can be added later; an appended one cannot be retracted.

**Resolved.** The exclusion was kept, and `b07a6d5` amended P13 section 5 to say so: the
exclusion follows the harness rather than the writer, and so reaches a harness's
publication as it reaches its creation. That amendment also records what is *not*
excluded - a harness is still published, still executed and still audited - and carves the
case out of sections 2 and 3 without editing them. Had the decision gone the other way,
the change would have been deleting one conditional, and mutation 2 below shows exactly one
test pins it.

The discriminator is `workflow.is_test_harness`, the row, not the `refuse_harness` argument
- that flag says which entry point was used, while the question is what the workflow is.
They agree whenever `refuse_harness` is true, since `not_a_test_harness` has already raised.

## Diff

| Path | Change |
|---|---|
| `apps/api/src/agentworld/services/executions.py` | the import, one reservation, one conditional append, inside `_publish` |
| `apps/api/tests/integration/test_definition_events.py` | 8 new tests, 52 → 60 |

`services/definitions.py` is **untouched**, so no committed producer moved. The nine
test-file deletions are six lines of module docstring, one widened import and the two
comment lines that said the fourteenth producer was not in this file. **Zero assertions
removed**; inventory against `e635b39` is 52 → 60 with none lost.

## Placement

Reservation sits after every refusal - the 404, the harness check, `not_archived`, the
missing draft and `_validated` - and after the pure step and edge assembly, immediately
before `workflows.publish(...)`. The append is after `next_draft`, before the audit write,
carrying `workflow_id`.

All 14 producers verified from the AST across both modules:
`def < reserve < write < append < audit`, one reservation and one append each.

## Lock order, checked for these tables

The publish writes more than any other producer: inserts into `workflow_steps` and
`workflow_edges`, an UPDATE of `workflow_versions.published_at`, an UPDATE of
`workflows.current_version_id`.

Read from the live database rather than inferred:

- Unique indexes on `workflows` are `(id)`, `(project_id, slug)` and a **partial** one on
  `test_for_agent_id`. `current_version_id` is in none of them.
- Unique indexes on `workflow_versions` are `(id)`, `(workflow_id, version)`,
  `(workflow_id, id)` and a partial `(workflow_id) WHERE published_at IS NULL`.
  `published_at` appears only in that **predicate**, never as an indexed column.

So both UPDATEs are non-key updates taking `FOR NO KEY UPDATE`, which does **not** conflict
with the `FOR KEY SHARE` that starting a run holds on the workflow row - inbound FKs are
`workflow_executions.workflow_id -> workflows.id` and
`workflow_executions.version_fkey -> workflow_versions`. A run reaches these tables in the
opposite order from the publish, and the two modes are compatible, so there is no cycle.

Worth recording: **no definition producer performs a key update on `workflows`.** `slug`
*is* in a unique index, and `WorkflowService.update` deliberately has no field for it.

## Mutation testing

Applied to `executions.py` alone, focused file run, source restored byte-identical.

| Mutation | Tests killed |
|---|---|
| reserve *after* the publish writes | 1 — `test_the_cursor_lock_is_taken_before_the_first_step_row_is_frozen` |
| drop the harness condition | 1 — `test_publishing_a_harness_writes_no_event_and_takes_no_lock` |
| event names the frozen version instead of the workflow | 2 — the shape test and `test_the_publish_event_carries_no_graph_and_not_the_version` |

The second is the one that matters: it shows the harness condition is load-bearing and
pinned, so if that decision is ever reversed, exactly one test has to change and it will
say so.

## The sweep over existing assertions

Fourteen test files publish. Checked for assertions that span a publish:

- No test outside `test_definition_events.py` asserts an absolute `seq`; every assertion in
  that file is relative to the `baseline` fixture.
- `test_agent_activity.py` and `test_workflow_writes.py` publish but make no event-table
  assertion. The activity-feed file is a free regression check: if P12.3's NULL reference
  columns were wrong, a definition event would reach `events_agent_idx` and break it.
- `tests/unit/test_acceptance_script.py` queries `FROM events` against a **stubbed**
  connection and asserts the query's shape; its `{"execution.started": 2}` comes from the
  stub, so real events cannot reach it.
- Event counts elsewhere filter on `execution_id`, which is NULL for every definition
  event, or assert `> 0`.

No existing test needed changing.

## Validation

| Item | Result |
|---|---|
| Focused definition-event tests | **60 passed** (52 preserved + 8 new), 0 skipped |
| Full API suite | **3147 passed**, 0 failed, 0 skipped, 1:09:56 (3139 at `e635b39` + 8 new) |
| Ruff check / format | All checks passed |
| Mypy | Success, no issues in 106 source files |
| Import contracts | 8 kept, 0 broken |

The full suite is **entirely green this time** - no assertion failures and no
`ConnectionResetError`/WinError 64. The two that failed on the Workflow group's run
(`test_dispatch_worker`'s wall-clock race and `test_reaper`'s Postgres transport drop)
did not recur, which is the second independent sign they were environmental rather than
caused by the definition-event work.

## Non-blocking concerns

1. **The P13 gap above** - the only item that needs a decision.
2. **The two helpers are imported across modules while underscore-private.**
   `executions.py` already imported four public names from `definitions.py`, so no contract
   broke and ruff does not flag it, but the underscore now understates their role. Renaming
   them public would touch 13 committed call sites, which is not worth it inside a
   one-producer change; worth a separate tidy-up.
3. **R8's wording in `04-ROADMAP-AND-MVP.md` was stale** - it said the inventory was
   "Partly implemented, and this box stays open." **Reconciled in `b07a6d5`**, along with
   P13's own status and the same claim in `08-PHASE-3-READINESS.md`. R8 and R10 stay open,
   and all fourteen R criteria stay unchecked, because the durable record is only half of
   R8: publication after commit and replay need `infra/event_bus.py` and the WebSocket
   gateway, neither of which exists.
4. **`save_draft`'s unreachable branch** and **`40P01` unhandled** carry over from the
   Workflow group, unchanged.
5. **`_assert_shape`'s parameter is still named `agent_id`** while carrying workflow,
   connection and agent ids.
6. **The harness test flips `test_for_agent_id` with owner SQL** rather than compiling a
   real harness, which would need a model profile, a credential and a compiled graph to
   reach the same one-column state. The partial unique index means this can only ever make
   one harness per agent.
