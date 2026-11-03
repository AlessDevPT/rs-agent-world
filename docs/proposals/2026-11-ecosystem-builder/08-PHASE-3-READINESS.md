# Phase 2 verification and the Phase 3 plan

**Date:** 2026-11-02 · **Head:** `bca9582` · **Branch:** `main`

This is a readiness review, not a summary of the last build. Everything below was
measured against the repository and the running database. Where I could not verify
something, it says so and says why — including one criterion that passes only because
nothing exercises it yet, and one that cannot be checked here at all.

---

## 1. What was actually run

| Check | Command | Result |
|---|---|---|
| Lint | `ruff check .` | clean |
| Format | `ruff format --check .` | 121 files already formatted |
| Types | `mypy src` (strict) | no issues, 73 files |
| Architecture | `lint-imports` | 5 contracts kept, 0 broken; 96 files / 435 deps analysed |
| API tests | `pytest --cov` with `AGENTWORLD_REQUIRE_INTEGRATION=1` | **1847 passed, 0 failed, 0 skipped**, 89% coverage |
| Migrations from scratch | the session fixture drops and recreates `agentworld_test`, then `alembic upgrade head` | passed as part of the suite |
| Migrations onto existing data | `alembic upgrade head` on the populated dev database at `0011` | 0012→0016 applied cleanly |
| Web lint | `npm run lint -w apps/web` | clean |
| Web types | `npm run typecheck -w apps/web` | clean |
| Web tests | `npm run test -w apps/web` | 26 passed (3 files) |
| Web build | `npm run build -w apps/web` | production build succeeded |
| Compose stack | `docker compose up -d --wait` | postgres + redis healthy |
| App role cannot bypass RLS | the `compose` job's SQL assertion | passed |

`0 skipped` matters: with `AGENTWORLD_REQUIRE_INTEGRATION=1` a missing Postgres is a
failure rather than a skip, so no integration test quietly opted out.

The web build initially failed with `EPERM ... .next/trace`. That was a leftover dev
server holding the directory, not a code problem; it builds after stopping it. Several
stale servers from earlier sessions were running (uvicorn on 8000 and 8077, `next dev` on
3000, `next start` on 3077) and the Next ones were stopped.

---

## 2. Phase 2 criteria, one by one

### 2.1 Migrations and database objects — verified

16 migration files, `alembic_version` at `0016`. Against the migrated database:

- **30 tables** (excluding `alembic_version`), **39 partitions**, **23 native enums**,
  **84 functions**.
- **26 tables with RLS enabled, all 26 with `FORCE`**, 26 policies — one per table.
- The four deliberately unprotected tables are `users`, `workspaces`, `workspace_members`
  and `api_keys`. `workspace_members` carries no policy by design: a policy there would
  need the answer it exists to produce.

### 2.2 RLS and tenant isolation — verified

- `test_rls.py` (16 tests), `test_tenant_isolation.py` (5), `test_tenant_pairs.py` (5),
  `test_append_only.py` (14), `test_world_separation.py` (11),
  `test_execution_integrity.py` (13).
- The application connects as `agentworld_app`, which is neither superuser nor
  `BYPASSRLS` — asserted in CI's `compose` job and reproduced here.
- The composite `(workspace_id, project_id) → projects (workspace_id, id)` foreign key
  from migration 0012 is present on every tenant table I checked, `dispatches`,
  `execution_steps` and `workflow_executions` included.
- Isolation tests assert the stronger property: a foreign id and a nonexistent id give
  *identical* responses, so existence does not leak through the status code.

### 2.3 Role-matrix guard — verified, and the fix is real

The guard reads `client.app.openapi()["paths"]`, normalises path parameters, skips
`HEAD`/`OPTIONS`, and **asserts the walk found something** before comparing. It then
requires `app_routes - covered - exempt == ∅`. Thirteen exemptions, each with a written
reason: `/v1/me` and `/v1/self/key` (no role — the caller is the resource), workspace
create/list (you become the owner), the three health/meta routes, and the four ingest
routes (key-scoped, not role-scoped; covered by `test_ingest.py`).

A stale row cannot hide either: a row naming a route that no longer exists would get a
404, and `404 < 400` is false, so the permitted-role branch fails.

### 2.4 The 77-action permission matrix — verified for what exists; 46 actions are forward-declared

- `Action` has **77 members**; `MINIMUM_ROLE` has **77 keys**; no action lacks a role.
  *(T6b made this 78 and 78: `dispatch.resolve_review`. See `17-T6B-REVIEW-API.md`.)*
- **31 actions are referenced outside `policy.py`.** The other **46 are declared ahead of
  their endpoints** — approvals, triggers, credentials, integrations, knowledge, tools,
  world, builder, `WORKFLOW_PUBLISH`, `WORKFLOW_RUN`.

That is the roadmap's intent (declare the matrix once, so adding an endpoint is a lookup
rather than a judgement), but it should be stated plainly: *"the matrix is consistent with
the API"* is a verified claim about 31 actions and an unexercised claim about 46. The
unused rows are reviewed logic, not tested behaviour.

- Conditional elevation works as designed: `ELEVATED_ROLE` raises `CONNECTION_CREATE` and
  `CONNECTION_UPDATE` to `admin` when the connection grants a consequential tool.
- `authorize()` refuses `SystemPrincipal` **first**, before any table lookup, for every
  action — so a scheduled run can perform no control-plane action at all.

### 2.5 Event protocol, reducers and stale events — one real gap (see D2)

- 44 known event types, 6 namespace reducers, `test_protocol_envelope.py` (42 tests),
  `test_reducers.py` (43), `test_ingest.py` (24), `test_reducer_columns.py` (column names
  *and* types checked against the live schema).
- Dedup: a per-event `seen()` check, a within-batch repeat check, and a **UNIQUE
  `(project_id, dedup_key)` index on every partition**. `\d events` does not show it
  because it is per-partition, which is why it is easy to believe it is missing — it is
  not: `events_y2026m11_dedup_idx` is there and unique.
- Out-of-order: `project_event_cursors` allocates `seq` under `FOR UPDATE`.
- **Stale events are only guarded for agents and tasks.** This is D2 below.

### 2.6 Replay cannot cause work — verified, structurally

`MutationTarget` has exactly seven members: `agents`, `agent_status_history`, `tasks`,
`runs`, `run_steps`, `workflow_executions`, `execution_steps`. There is no `dispatches`
member, so no mutation can express a dispatch — the guarantee is a type, not a check.
Backed by two tests: a drift guard asserting the forbidden names never appear as targets,
and an integration test that folds a full workflow trace and asserts `count(*) = 0` on
`dispatches`.

The half that cannot be tested yet is unchanged from the last report: *"replay with
dispatch workers running creates zero dispatch rows"* has no workers to run.

### 2.7 Workflow version immutability — not enforced, and vacuously true (D3)

### 2.8 Dispatch outbox — schema verified; semantics contradictory (D4, D5)

The table is well shaped: `leased_by` / `leased_until`, `idempotency_token`,
`attempt` / `max_attempts`, `run_after`, `side_effect_started_at`, `last_error_code`, and
three partial indexes matching the three access patterns (claimable, expired lease, needs
attention). A check constraint forbids a `leased` row without a lease, and
`UNIQUE (execution_step_id, kind, attempt)` is intended to make a double decision collide.

Transactional consistency is structurally sound: the outbox lives in the same database as
the state it is derived from, so event + state + dispatch commit together or not at all.
No worker exists to test it.

### 2.9 Credentials — schema correct, implementation absent (D10)

The `credentials` table has exactly the envelope shape: `ciphertext`, `nonce`,
`wrapped_key`, `key_id`, `aad_version`, plus `display_hint`, `scopes`, `expires_at`,
`refresh_after`, `revoked_at`, `deleted_at`. It is a separate table from the argon2
verification hashes in `api_keys`, which satisfies *"keep recoverable integration
credentials separate from password hashes and API key verification hashes."*

There is **no crypto code**: no `SecretStore`, no AEAD, no KEK map, no repository, no
service, no endpoint. Nothing in the codebase reads or writes a credential row. The
architecture is intended, not implemented.

### 2.10 Island presentation state is separate — verified

- Four presentation tables (`world_layouts`, `world_districts`, `world_nodes`,
  `world_routes`), all RLS-forced and tenant-isolated.
- **No reducer, service or ingest path writes a `world_*` table.** Confirmed by search;
  the only matches in `domain/` are enum names.
- Tests assert both directions: no presentation table carries execution state, and no
  execution table references a presentation table.
- `test_an_agent_keeps_its_node_when_it_goes_offline_or_is_archived` and
  `test_deleting_a_node_leaves_the_agent_and_its_history_intact` enforce the brief
  directly: moving or deleting an island object changes no execution system, and an
  offline agent stays visible.
- The web side keeps its own boundary: eslint rules forbid Phaser outside `world/`, and
  `world/projector` must stay free of React and Phaser.

---

## 3. Defects and gaps found in this review

Ordered by what they cost, not by when I found them.

### D1 — No event producer authority. Highest priority. Must close before Phase 3 creates executions.

`EventSpec` records a type's subject kinds and the tables it folds into. It records
**nothing about who may produce it.** Ingest checks the key's scope, that it is pinned to a
project, the clock skew, and — only when the event names an agent — the agent allowlist.
There is no event-type allowlist anywhere in the ingest path.

So any holder of an ingest key may post any of the 44 types. Two consequences:

1. **`approval.granted` is ingestible.** The reducer folds it to
   `execution_steps.status = READY`. Phase 3's `advance()` dispatches ready steps. The
   design's claim that *"the gate is the absence of a dispatch, not a flag a worker is
   trusted to check"* protects against a buggy approval UI; it does not protect against a
   forged event, because the forged event makes `advance()` create the dispatch itself.
2. **`workflow.step_succeeded` is ingestible with arbitrary `output`.** A later step reads
   that output through the expression language, so a worker could fabricate another step's
   result.

Today this is **latent, not live**: no publish path exists, so there are no executions and
no steps, and the fold changes zero rows. It becomes exploitable the moment Phase 3 adds
publish and run.

It cannot be fixed with a blanket ban, because the design *requires* a dispatchable agent
to emit `workflow.step_succeeded` (§6 of the orchestration doc). The rule has to be scoped:

- `approval.*` — control plane only. Never ingestible.
- `workflow.execution_*` — orchestrator only. Never ingestible.
- `workflow.step_*` — ingestible only for a step that is *currently dispatched* to an agent
  the key may act as, and only the terminal members (`succeeded`, `failed`), never
  `step_dispatched` or `step_retried`.
- `agent.*`, `task.*`, `run.*`, `tool.*` — ingestible under the existing allowlist.

This is a design gap as well as an implementation one: none of the seven proposal documents
states who may emit what.

### D2 — Stale-event protection covers only agents and tasks

Proven directly against the pure reducer:

```
workflow.step_succeeded  -> Patch target=execution_steps     guard=None
workflow.step_started    -> Patch target=execution_steps     guard=None
agent.paused             -> Patch target=agents              guard=('last_event_at', ...)
```

`last_event_at` exists on `agents` and `tasks` only. `runs`, `run_steps`,
`workflow_executions` and `execution_steps` each carry a mutable `status` folded from
events with **no staleness predicate at all**, so a late `step_started` overwrites a
`step_succeeded`, and a late `execution_running` can revive a finished execution.

In Phase 2 this is a display error. In Phase 3 it is worse: `advance()` reads step status
to decide what to do next, so a reverted status can cause a re-dispatch or a permanent
stall. The guard must extend to these four tables before the orchestrator reads them.

### D3 — Workflow version immutability is unenforced

- No trigger and no rule exists anywhere in the schema (`pg_trigger` and `pg_rules` are
  both empty of user objects).
- `agentworld_app` holds `UPDATE` **and** `DELETE` on `workflow_versions`,
  `workflow_steps` and `workflow_edges`.
- `published_at` is recorded and `WorkflowVersion.is_published` reads it, but nothing
  consults it before a write.
- No test asserts immutability.

The criterion passes today only because nothing publishes. `workflow_executions` →
`workflow_versions` is `ON DELETE RESTRICT`, so a version that has *run* cannot be deleted
— but a published, not-yet-run version can be edited or dropped. Enforcement belongs in the
database, as a trigger refusing `UPDATE`/`DELETE` when `published_at IS NOT NULL`, because
the app role's privileges make a service-layer check advisory.

### D4 — The outbox `attempt` semantics are self-contradictory

Three statements in the design cannot all hold:

1. Claiming does `attempt = attempt + 1` on the claimed row (§3 Claiming).
2. A human retry inserts a **new** dispatch with `attempt + 1` (§8 Dead letters).
3. `UNIQUE (execution_step_id, kind, attempt)` is what makes a double `advance()` collide
   (Phase 3 acceptance criterion).

If claiming mutates `attempt`, the unique key is not stable, so it no longer guards
creation — and a claimed row incremented to `attempt = 1` collides with a human retry row
inserted at `attempt = 1`. Needs resolving before any of it is written. The cleanest fix
keeps `attempt` immutable per row (set at insert, the identity of the attempt) and adds a
separate `claim_count` for lease bookkeeping.

### D5 — The duplicate-dispatch guard does not cover step-less dispatches

`dispatches.execution_step_id` is nullable, and in PostgreSQL NULLs are distinct in a
unique constraint. Any dispatch not tied to a step — a trigger-level or execution-level one
— gets **no** duplicate protection from `dispatches_step_attempt_key`. Phase 2 creates no
such rows; Phase 3 should either keep the column mandatory for every kind or add a second
constraint covering the step-less shape.

### D6 — A concurrent duplicate aborts the whole batch instead of skipping

`seen()` runs before `next_seq()`, so the cursor lock is taken lazily — on the first
*accepted* event. Two simultaneous batches sharing a `dedup_key` both pass `seen()`; the
partition's unique index then raises on insert and, because the batch is one transaction,
the whole request fails rather than reporting one duplicate. The retry succeeds, so it is
self-healing, but it surfaces as a spurious 5xx under concurrency. Taking the cursor lock
once at the start of the transaction — which migration 0015 already says is the intent,
*"a project's ingest is serialised"* — makes it deterministic.

### D7 — Per-month dedup, now quantified

Migration 0011 names this honestly: the dedup index is per-partition, so a retry crossing a
month boundary is accepted twice. What the comment does not say is the cost. Folding is
idempotent **except** `Increment`, which is used in exactly two places —
`runs.step_count` and `runs.tool_call_count`. So a cross-month duplicate double-counts
those two columns and nothing else. A non-partitioned dedup side-table closes it.

### D8 — The dev database was five migrations behind (fixed in this review)

`agentworld` sat at `0011` while head is `0016`: 25 tables, no `project_event_cursors`, no
`world_*`, no `tasks.last_event_at`. The API dev server points at that database, so **no
Phase 2 endpoint could have worked against it** — ingest would have failed on the missing
cursor table. The previous report's "both servers responding" was `/health` answering, not
the Phase 2 surface working. I applied `0012`→`0016`; they went on cleanly over existing
data, which also exercises an upgrade path CI does not cover (CI only ever migrates from
scratch).

### D9 — No approvals table

No `approvals`, `approval_policies`, `triggers` or `integrations` table exists. The
`_approval` reducer moves step status and nothing else, so an approver's identity lives
only in the event payload — in a partitioned table with retention. The design is right
(§7: the requirement frozen at creation, `audit_log` rows for accountability); none of it
is built.

### D10 — No credential crypto (see 2.9)

### D11 — Deleting an agent leaves its island node

Carried over and still true; `test_deleting_an_agent_leaves_its_node_behind` asserts the
current leaky behaviour rather than the desired one. `world_nodes.node_id` is polymorphic,
so no foreign key can clean up. Phase 3 owes a reconciliation path.

### D12 — `.shell__mark` contrast

Still unfixed at 1.85:1 against a 4.5:1 requirement. Cosmetic, one line, unrelated to
Phase 3.

---

## 4. CI — verified green

**Correction to an earlier claim in this document.** It originally said CI had never run.
That was wrong, and it was an inference rather than a measurement. The repository has a
GitHub remote (`AlessDevPT/rs-agent-world`), `main` is fully pushed, and the workflow
triggers on push — so CI has run **18 times**. It is also a *public* repository, which
means runs can be read through the unauthenticated REST API, with no `gh auth login`.

**Run #18 is on `bca9582` — the exact head reviewed here — and its conclusion is
`success`.** Triggered by push, 2026-11-01T23:20:15Z → 23:25:08Z.
<https://github.com/AlessDevPT/rs-agent-world/actions/runs/41361610264>

| Job | Conclusion | Steps |
|---|---|---|
| `API - lint, types, architecture, tests` | **success** | all 12 green: ruff check, ruff format, mypy (strict), Architecture contracts, Migrations apply from scratch, pytest |
| `Web - lint, types, tests, build` | **success** | all 8 green: npm ci, lint, typecheck, test, build |
| `Local stack starts` | **success** | all 6 green, including *"The application role exists and cannot bypass RLS"* |

### The two checks that need log text

Log download returns 403 unauthenticated, so the pytest summary line and the
`lint-imports` output cannot be read directly. Both were established another way.

**Zero skips — proven, not inferred.** The suite contains exactly **one** skip mechanism:
`tests/integration/conftest.py:115`. There is no `skipif`, no `xfail` and no `mark.skip`
anywhere under `tests/`. That one call is guarded two lines above it by
`AGENTWORLD_REQUIRE_INTEGRATION == "1"`, which converts it into `pytest.fail`, and the
workflow sets that variable at job level. So a green pytest step in CI cannot have skipped
anything — there is no path by which it could.

**Five contracts evaluated.** The workflow calls the console script `lint-imports`, not the
`python -m importlinter.cli` form its own comment warns about. Run from a directory with no
configuration, `lint-imports` exits **1** with *"Could not read any configuration"* —
checked directly. So it fails closed: a green step means it read a config and evaluated
contracts. The config at this commit declares five, and prints `Contracts: 5 kept, 0
broken` locally.

### One correction to the manual steps

`gh workflow run ci.yml` would **not** have worked. `ci.yml` has only `push` and
`pull_request` triggers — no `workflow_dispatch` — so there is nothing to dispatch. Adding
`workflow_dispatch:` is worth doing on its own merits, so a run can be triggered without an
empty commit.

Authentication is still needed for log text, for re-running a job, and for anything on a
private repository. No Phase 3 work is blocked on it.

---

## 5. Phase 3 architecture

This preserves the Phase 2 structure and the proposal's design. It introduces no new layer
and rewrites nothing that works.

### 5.1 The four capabilities stay apart

```
  control plane            orchestration          dispatch            execution
  (HTTP, roles)            (pure decide)          (outbox)            (workers)
       |                        |                     |                   |
  publish / run            advance(state)        dispatches row      step_runner
  approve / retry          -> Decision[]         claimed by lease    provider call
       |                        |                     |              tool call
       +----------> services/executions.py <----------+                   |
                    one transaction:                                      |
                    event + state + outbox                                v
                                ^                                   ingest events
                                |                                         |
                                +----------- reducers fold ---------------+
```

Four rules hold the shape:

1. **`advance()` is pure.** `domain/orchestration/advance.py` takes a snapshot of execution
   state plus the frozen version and returns `Decision` values. No I/O, no session, no clock
   it does not receive. It lives in `domain/`, so the existing import contracts already
   forbid it from importing a provider, a driver or FastAPI.
2. **Only `services/executions.py` writes.** It applies decisions in one transaction: the
   event row, the derived state, and the outbox row. There is no other way to create a
   dispatch, and no `Action` for one.
3. **Workers only execute.** A worker claims a dispatch, performs the side effect, and
   reports by writing an event. It never decides what happens next and never writes
   execution state directly — the reducer does that, through the same path ingest uses.
4. **Ingest stays the single fold path.** The island, the REST reads and the WebSocket all
   read state the reducers produced. There is no second execution path for the island or the
   builder: both call the same `POST /v1/workflows/{id}/run`.

### 5.2 Why replay still cannot cause work

The guarantee survives Phase 3 for the same structural reason it holds now. `fold()` returns
`Mutation`, and `MutationTarget` has no `dispatches` member. Workers find work by querying
`dispatches`, never by consuming events. So replaying the log reconstructs state and creates
no outbox row, and a worker has nothing new to claim. The existing drift guard keeps it that
way: add a dispatch target and the test fails by name.

The one new obligation: the replay test must be re-run **with workers running**, which is the
acceptance criterion that could not be met in Phase 2.

### 5.3 New modules

```
infra/secrets/            SecretStore, envelope encryption, KEK map, rotation
infra/net/guarded.py      the only outbound HTTP client
infra/providers/          base.py Protocol, anthropic.py, openai_compatible.py, registry
infra/tools/              MCP client, tool executor
domain/orchestration/     advance.py, decisions.py          (pure, joins validate.py)
domain/approvals/         policy.py — requirement resolution (pure)
services/executions.py    publish, run, apply decisions, retry
services/approvals.py     request, decide, expire
workers/step_runner.py    claim -> execute -> report
workers/dispatch_reaper.py expired leases, step timeouts, approval expiry
api/v1/executions.py      run, read, cancel, retry, provide input
api/v1/approvals.py       list, decide
api/v1/credentials.py     create, rotate, revoke, metadata-only reads
```

Two new import contracts, each verified by planting a violation:

- `services/` and `domain/` may not import `anthropic`, `openai` or any provider SDK.
- `httpx.AsyncClient` may be constructed only inside `infra/net/`.

### 5.4 Execution lifecycle

```
pending -> running -> {succeeded, failed, cancelled}
                   \-> waiting_approval -> running
                   \-> waiting_input    -> running
                   \-> paused           -> running
```

A step: `pending → ready → dispatched → running → {succeeded, failed, skipped}`, with
`waiting_approval` between `ready` and `dispatched`, and `ready` again after a retry. All of
these already exist as enum members; `waiting_approval` was added in migration 0014.

### 5.5 Provider independence

One Protocol in `infra/providers/base.py` with a single surface, `complete(messages, tools,
config)`. Adding a provider adds a file and a registry entry. The second implementation
(OpenAI-compatible) is written **in this phase, last**, specifically to prove the Protocol is
not shaped around Anthropic. Nothing above `infra/providers/` mentions a vendor;
`model_profiles` already stores `provider_slug` and `model_id` as data.

### 5.6 Reliability

| Concern | Mechanism |
|---|---|
| Duplicate dispatch | `UNIQUE (execution_step_id, kind, attempt)` with `attempt` immutable per row (D4) |
| Concurrent claiming | `FOR UPDATE SKIP LOCKED` + a lease |
| Worker crash | `leased_until` expiry; the reaper returns the row to `pending` |
| Uncertain outcome | `side_effect_started_at` set *before* a consequential call; a crash after it means `needs_review`, never an automatic retry |
| Retry | classify first; backoff `ceiling = min(15m, 2s · 2^(claims-1))` with **equal** jitter, `uniform(ceiling/2, ceiling)`; `max_attempts` 3, but **1** for consequential work |
| Dead letter | `status = dead` keeps instruction, error and lineage; always resolves its step by emitting `step_failed`; appears in the project's attention list |
| Timeout | per-step wall clock enforced by the reaper, not by a hung dispatch |
| Idempotency | `idempotency_token` per dispatch, forwarded to providers that accept one |

---

## 6. Ordered tasks and acceptance criteria

**T0 comes first and is not optional.** It closes the Phase 2 defects that Phase 3 would
otherwise build on top of. Everything after it assumes T0 landed.

### T0 — Close the blocking Phase 2 defects

| | |
|---|---|
| Closes | D1, D2, D3, D4, D5, D6 |
| Touches | `domain/protocol/taxonomy.py`, `services/ingest.py`, `domain/reducers/fold.py`, `repositories/events.py`, migrations 0017–0019 |

1. **Producer authority (D1).** `EventSpec` gains a `producers` field. Ingest refuses a type
   the key may not produce, with the index of the offending event.
2. **Staleness (D2).** Migration adds `last_event_at` to `runs`, `run_steps`,
   `workflow_executions`, `execution_steps`; the three reducers gain the guard.
3. **Version immutability (D3).** Migration adds a trigger refusing `UPDATE`/`DELETE` on
   `workflow_versions`, `workflow_steps`, `workflow_edges` when `published_at IS NOT NULL`.
4. **Attempt semantics (D4).** `attempt` immutable per row; add `claim_count`.
5. **Step-less dispatches (D5).** Decide the shape; constrain it.
6. **Ingest serialisation (D6).** Take the cursor lock once, before the dedup loop.

**Acceptance**
- [ ] An ingest key posting `approval.granted` gets 403, and the step's status is unchanged.
- [ ] An ingest key posting `workflow.execution_succeeded` gets 403.
- [ ] An ingest key posting `workflow.step_succeeded` for a step dispatched to an agent it
      may act as is **accepted**; for a step dispatched elsewhere, 403; for a step not
      dispatched at all, 403.
- [ ] A late `step_started` after `step_succeeded` leaves the step `succeeded` — asserted
      per table for all four new guards.
- [ ] `UPDATE workflow_versions SET graph = ...` on a published version is refused **as the
      app role**, not by a service check.
- [ ] The same `UPDATE` on an unpublished draft version still succeeds.
- [ ] Two concurrent batches sharing a `dedup_key` produce one accepted event and one
      reported duplicate — no 5xx.
- [ ] Every new guard is verified non-vacuous by weakening it and watching the test fail.

### T1 — Secrets and envelope encryption

| | |
|---|---|
| Builds | `infra/secrets/`, `services/credentials.py`, `api/v1/credentials.py` |
| Needs | T0 |

AEAD per credential, DEK wrapped by a KEK from `CREDENTIAL_KEKS`, AAD binding the ciphertext
to its workspace and `aad_version`. `Settings` fails fast without KEKs outside local and test.

**Acceptance**
- [ ] A credential row copied into another workspace at the SQL level **cannot be
      decrypted** — the AAD mismatch is the failure, not a service check.
- [ ] KEK rotation re-wraps every DEK with no plaintext leaving the store and **no change to
      any `ciphertext`**.
- [ ] No endpoint returns a secret. `display_hint` is the only hint, and it is derived.
- [ ] A credential appears in no log line, no event payload and no problem document —
      asserted by a test that ingests, errors and greps the captured output.
- [ ] Starting the app in `production` with no `CREDENTIAL_KEKS` fails at boot.

### T2 — The guarded HTTP client

| | |
|---|---|
| Builds | `infra/net/guarded.py` |
| Needs | nothing; lands before any caller exists |

Resolve, validate **every** resolved address, pin the connection to it, no redirects.

**Acceptance**
- [ ] Every range in `06-OUTBOUND-SECURITY.md` §3 is refused, parametrised one case per row,
      **both IP families**, including `::ffff:169.254.169.254` and the NAT64 form.
- [ ] A resolver returning a public address and then `10.0.0.5` still connects to the first:
      pinned, not re-resolved.
- [ ] `Host` and TLS SNI carry the original hostname when connecting by IP, against a real
      TLS test server.
- [ ] A blocked destination, a nonexistent host and a refused connection are
      **indistinguishable** in step output, event payload and problem document.
- [ ] Planting `httpx.AsyncClient(...)` outside `infra/net/` fails the new import contract.

### T3 — Provider interface and Anthropic

**Acceptance**
- [ ] A one-message completion against the real API returns content and token counts.
- [ ] Every provider call goes through the guarded client — asserted by the contract.
- [ ] A 429 with `Retry-After` is classified retryable and honours the header.
- [ ] 401 and 400 are classified **terminal**.
- [ ] Planting `import anthropic` in `services/` fails the new import contract.
- [ ] Nothing outside `infra/providers/` names a vendor — asserted by search.

### T4 — `advance()`, the pure decision function

**Acceptance**
- [ ] Unit tests pass with **no database and no network**.
- [ ] Called twice on identical state it returns identical decisions.
- [ ] It refuses to target an `observed` agent — no dispatch kind can reach one.
- [ ] A graph that `validate()` rejects is never advanced.
- [ ] `advance()` imports nothing from `services`, `repositories` or `infra`.

### T5 — The executions service and publish

**Acceptance**
- [ ] Publishing normalises a draft into `workflow_steps` / `workflow_edges`, sets
      `published_at`, and the version is then immutable (T0's trigger).
- [ ] Publishing an unpublishable draft is refused with the validation findings.
- [ ] `POST /v1/workflows/{id}/run` requires `WORKFLOW_RUN`; the role matrix has a row.
- [ ] A run names its initiator; the XOR constraint and `ON DELETE RESTRICT` are exercised.
- [ ] **A double `advance()` produces one dispatch**; the loser hits the unique constraint.
- [ ] Event, state and outbox row commit together — a forced failure after the event leaves
      none of the three.

### T6 — Outbox claiming and the step runner

**Acceptance**
- [ ] Two workers claiming concurrently never get the same row (`SKIP LOCKED`).
- [ ] A worker killed mid-dispatch: the lease expires and the step **completes exactly
      once**.
- [ ] A worker that needs longer extends its own lease rather than taking a long one.
- [ ] A worker writes no execution state directly — only events.
- [ ] Ten workers against one project produce no duplicated step and no lost step.

### T7 — The managed agent turn

**Acceptance**
- [ ] `POST /v1/workflows/{id}/run` on a one-step workflow calls a real provider, writes a
      run trace, and completes — through `curl`, with no frontend.
- [ ] Tool grants are checked **at call time**: a grant revoked mid-run takes effect on the
      next call.
- [ ] A tool that is not granted produces `tool.denied` and the refusal is fed back to the
      model, not raised.
- [ ] `max_turns` and the wall clock both terminate the loop.
- [ ] Token counts and durations land in `metric_samples` through the existing metric events
      — no new plumbing.
- [ ] Tool arguments and results are redacted in events.

### T8 — Retries, timeouts, dead letters, uncertain outcomes

**Acceptance**
- [ ] A 429 then success: **one step, two attempts, one output**.
- [ ] Each row of the classification table is asserted — retryable retries, terminal does
      not.
- [ ] Backoff is jittered; asserted statistically, not by equality.
- [ ] A consequential dispatch defaults to `max_attempts = 1`.
- [ ] **A dead dispatch always resolves its step** — `step_failed` is emitted and
      `on_failure` applies.
- [ ] A crash after `side_effect_started_at` with no result goes to `needs_review`, never an
      automatic retry.
- [ ] A `needs_review` row appears in the project's attention list.
- [ ] A human retry creates a new dispatch with a **new** `idempotency_token`.
- [ ] A step that is never reported is resolved by its timeout, not by a hung dispatch.

### T9 — Approvals

| | |
|---|---|
| Builds | `approvals`, `approval_policies`, `domain/approvals/policy.py`, `services/approvals.py`, `api/v1/approvals.py`, expiry in the reaper |
| Closes | D9 |

**Acceptance**
- [ ] While an approval is pending **no dispatch row exists** — asserted by count.
- [ ] The requirement is **frozen at creation**: editing the policy afterwards changes
      neither direction for a pending approval.
- [ ] `requires_different_user` is enforced in three places — the service, a check
      constraint, and the absence of a dispatch.
- [ ] A `viewer` sees a pending approval and cannot decide it.
- [ ] Granting produces a dispatch **only** via `advance()`, never from the event alone.
- [ ] Every decision writes an `audit_log` row as well as an event.
- [ ] Expiry is a worker writing `approval.expired`, with the same effect as denial.
- [ ] An ingest key still cannot forge any of it (T0's rule, re-asserted here).

### T10 — MCP-based tool execution

**Acceptance**
- [ ] A tool is invoked over MCP and its result is folded into the run trace.
- [ ] A tool server is reached only through the guarded client.
- [ ] Tool output is treated as **untrusted input**: a tool result carrying an instruction
      does not change what the agent is permitted to do — asserted separately from the
      document case, because tool output is the surface people forget.
- [ ] A consequential tool with no approval does not execute.
- [ ] A tool failure is classified, not swallowed.

### T11 — Dispatchable agents: push and poll

**Acceptance**
- [ ] Push: a `2xx` means **accepted, never done** — the step stays `dispatched` until a
      terminal event arrives or the timeout fires.
- [ ] Push requests are signed exactly as outbound webhooks are, verified against an
      independent implementation.
- [ ] Poll: the agent claims a materialised task through the existing
      `FOR UPDATE SKIP LOCKED` path.
- [ ] No dispatch kind can target an `observed` agent, and `advance()` refuses one.
- [ ] A `dispatchable` or `observed` agent cannot be saved with a `model_profile_id` — the
      **check constraint**, not a service check.

### T12 — Execution reads and reporting

**Acceptance**
- [ ] `GET /v1/executions/{id}` returns the step tree with statuses and timings.
- [ ] Logs and metrics for an execution are retrievable and tenant-isolated.
- [ ] Every new route has a role-matrix row — the guard enforces it.
- [ ] A step's output never contains a secret or a raw provider error.

### T13 — The second provider

**Acceptance**
- [ ] The OpenAI-compatible provider passes the **same** conformance suite as Anthropic.
- [ ] Adding it changed only `infra/providers/` and a registry entry — asserted by diff.
- [ ] Switching a model profile between providers needs no workflow change.

### T14 — Adversarial and integration tests

**Acceptance**
- [ ] **Replay with workers running creates zero dispatch rows** — the Phase 2 criterion
      that could not be met, now met.
- [ ] Tenant isolation asserted on every new table and every new route.
- [ ] A `SystemPrincipal` is refused for every control-plane action, re-asserted after the
      new actions exist.
- [ ] Execution authority comes from the **published version**, never from whoever last
      configured or enabled the trigger — asserted by changing the configurer and showing
      the authority unchanged.
- [ ] Deleting `world/phaser/` leaves the backend suite and the non-world web suite green.
- [ ] A chaos test: kill workers at random points through a 20-step execution; it completes
      exactly once or stops in a named state, never silently.

---

## 7. Decisions I need from you

### Q1 — Scope. Your 12 topics are wider than the designed Phase 3.

MCP tool execution, approvals and integrations are **Phase 6** in
`04-ROADMAP-AND-MVP.md`, deliberately, so that Phase 3 can prove one thing end to end
first. Your list folds them in. Two options:

- **(a) Staged** — Phase 3 = T0–T8 and T13 (execution core, one provider proven by a
  second, retries and recovery). Approvals, MCP and dispatchable agents follow as 3a.
  Earlier working slice, the plan's original shape. **My recommendation.**
- **(b) One large phase** — T0–T14 as a single Phase 3. Everything you listed, roughly
  double the duration, and nothing demonstrable until late.

### Q2 — T0 before anything else?

I would not build the orchestrator on top of D1 and D2. D1 in particular means that the
moment executions exist, an ingest key can open its own approval gate. T0 is a few days.
Confirm, or tell me to proceed and track them as known risks.

### Q3 — Real provider calls in the test suite?

An Anthropic key in CI makes T3 and T7's acceptance criteria genuine but makes the suite
cost money and need network. The alternative is a recorded-cassette suite plus one
manually-run live test. I lean towards **cassettes in CI, one live smoke test run by hand**
before each milestone.

### Q4 — Where does `advance()` run?

The design has the API process call it inside the request for `run`, and the reaper call it
on step completion. The alternative is a dedicated orchestrator process. I lean towards
**in-process for Phase 3** — it is a pure function and the transaction boundary is what
matters, not the host — with a separate process deferred until there is load to justify it.

### Q5 — CI first?

I cannot authenticate `gh`. Running CI once before Phase 3 starts would establish a baseline
on code known to be green locally, so the first failure is attributable. Worth ten minutes
of your time; your call whether now or later.

---

## 8. Summary

Phase 2 is substantially sound. The guarantees that were designed to be structural **are**
structural: replay cannot cause work because the vocabulary cannot express it; append-only
is a privilege, not a convention; tenancy is a composite foreign key; the island cannot
reach into execution state. 1847 tests pass with nothing skipped, and every CI step passes
locally.

Three things are not what the criteria claim. **Immutability is unenforced** and passes only
because nothing publishes. **Staleness protection covers two of six folded tables.** And
**any ingest key may emit any event type**, which becomes an approval bypass the moment
Phase 3 creates executions. None of these is expensive to fix; all three are much cheaper
now than after the orchestrator reads that state.

CI is verified green on this exact commit — run #18, all three jobs, every step — and the
two claims that needed log text were established by other means rather than assumed.
