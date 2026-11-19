# AgentWorld — what is built, and what is left

> **Measured, not remembered.** Every claim here was checked against the worktree on
> **2026-11-19**, at commit `5fcaa83` plus the gap fixes committed with this file. Where a
> plan document disagreed with the code, the code won.
>
> **This file has no date in its name on purpose.** Its predecessor was
> `STATUS-2026-11-17.md`, and seven documents linked to it by that name — so the moment it
> was re-measured, every one of those links pointed at a date that was no longer the
> status. The date lives in the line above instead, where updating it is the same edit as
> updating the content.
>
> This file is the status. The *reasoning* lives in
> [`proposals/2026-11-ecosystem-builder/`](proposals/2026-11-ecosystem-builder/README.md);
> the pre-pivot contract set, [`01`](01-ARCHITECTURE.md)–[`05`](05-ROADMAP.md), is history
> with banners saying so.

---

## 1. One-screen summary

| | | |
|---|---|---|
| **Phase 0** Foundations | ✅ complete | monorepo, compose stack, CI, green from a fresh clone |
| **Phase 1** Tenancy and auth | ✅ complete | Supabase JWT, workspaces, members, roles, API keys, RLS |
| **Phase 2** Core model and protocol | ✅ complete | 30 tables, 26 migrations, event sourcing, replay, outbox. 13 of 14 ledger criteria met, the 14th untestable until Phase 7 |
| **Phase 3** Execution core + island skeleton | 🟡 **Track A mostly done, Track B not started** | Phase 3 is T0–T8 + T13. Open: T7's turn loop, T8's retry decision, T13's second provider, the event bus and WebSocket gateway, and all of the island |
| **Phase 3a** Approvals, MCP tools, dispatchable agents | ⬜ not started | until it lands, consequential actions are refused at publish rather than gated |
| **Phase 4** Vertical slice → M1 | 🟡 **partly built early** | multi-step, delegation, `agent.test` and `execution.cancel` all exist; its real remainder is the island's I2 and the builder prototype |
| **Phases 5–11** | ⬜ not started | real workflows, approvals/tools/integrations, external agents, full workspace, full island, complete builder, hardening |

**The headline, and it has moved since 2026-11-17.** The backend can create, configure,
run, cancel, test and review agents and workflows. **The browser can now do all of that
too** — an agent can be created, edited, archived, restored, tested against a real model
and watched through its executions, and a workspace's credentials and model profiles can
be managed, without `curl`. What the browser still cannot do is see anything change
without a reload: there is no realtime feed, and there is no island.

---

## 2. What is done

### 2.1 Backend — 71 routes, twelve `v1` routers plus health

| Area | Routes | Notes |
|---|---|---|
| Identity | 2 | Supabase Auth client-side; the first `GET /v1/me` materialises the user and reports `needs_onboarding` |
| Workspaces and members | 9 | roles `viewer`→`owner`, one owner per workspace by partial unique index, promotion is a transfer in one transaction |
| Projects | 5 | |
| API keys | 4 | hashed, scoped (`ingest`, `full`), revoke and delete |
| Credentials | 5 | envelope encryption, AAD bound to workspace **and** credential id, rotate, revoke |
| Agents | 15 | list, create, read, patch, archive, restore, `test`, status history, runs, executions; model profiles CRUD |
| Workflows | 14 | connections and grants, workflows, versions, draft `GET`/`PUT`, `validate`, `publish`, `restore`, archive |
| Executions | 7 | run, list, read, cancel, and the step tree |
| Dispatch review | 2 | the queue at `viewer`, `resolve` at `admin` under `dispatch.resolve_review` |
| Ingest | 4 | events (partial success), heartbeat, logs, metrics |
| Self and meta | 2 | |
| Health | 2 | |

**Orchestration.** `advance()` is a pure decision function — no clock, no I/O, asserted
from its own syntax tree — and `services/executions.py` applies its decisions as one
transaction: event, state, outbox row. Dispatch rows are written from exactly one file,
enforced by a containment allow-list split by verb.

**Workers.** `agentworld-worker` and `agentworld-reaper` are real processes with `--once`
and continuous modes, outside the API lifespan. Claiming is `FOR UPDATE SKIP LOCKED` plus
a lease. `side_effect_started_at` is written **before** the outbound call, so an expired
lease can tell "never sent" from "might have landed": the first is retried, the second
becomes `needs_review` and waits for a human.

**Outbound security.** One guarded HTTP client: resolve, validate every resolved address,
pin the connection to a validated one, no redirects followed. Both IP families, the
metadata addresses and names, NAT64 and the short/hex/octal loopback forms are all
refused. `httpx` is confined to `infra/net/` by an import contract with one named Supabase
exemption.

**Event sourcing.** Append-only `events` with per-project `seq`, dedup keys, partitioning,
reducers with out-of-order tolerance, and the two properties that keep it honest:
refolding the stored log reproduces the state, and folding it creates **zero** dispatch
rows.

**Tests and enforcement.** 48 integration and 27 unit test files, 1,246 test functions;
8 import-linter contracts; schema and enum drift tests against a freshly migrated
database; an exhaustive role matrix with a non-vacuity guard; migrations applied from
scratch in CI. Three CI jobs — `api`, `web`, `compose` — and the compose job asserts the
application role cannot bypass RLS.

### 2.2 Web app — 12 pages

| Page | What it does |
|---|---|
| `/` | the one page that handles every session state: setup instructions, sign-in, a refused account, an API that is down, or the workspace |
| `/login` | Supabase sign-in, deep-link preservation, the rejected-token recovery path |
| `/auth/callback`, `/auth/signout` | code exchange; sign-out at the library's global scope, answering 303 |
| `/onboarding` | create the first workspace and project |
| `/w/{ws}` | project list |
| `/w/{ws}/keys` | mint and revoke API keys |
| `/w/{ws}/settings/credentials` | provider credentials: add, rotate, revoke, delete |
| `/w/{ws}/settings/model-profiles` | model profiles: create, re-point, delete |
| `/w/{ws}/p/{p}/agents` | the project's agents, archived included on request |
| `/w/{ws}/p/{p}/agents/new` | create an agent |
| `/w/{ws}/p/{p}/agents/{id}` | one agent: status, heartbeat, **a test panel**, executions, details, archive and restore |
| `/w/{ws}/p/{p}/agents/{id}/edit` | edit an agent |
| `/w/{ws}/p/{p}/executions/{id}` | one execution and what its steps did |

Typed clients for tenancy, agents, credentials, model profiles and executions; a
problem-details-aware fetch wrapper; middleware that forwards the real return path so a
deep link survives sign-in; **577 tests across 27 files** with `tsc --noEmit` and
`eslint .` clean.

### 2.3 Infrastructure

Docker compose stack with Postgres, a health-gated startup, an application role that
cannot bypass RLS, Alembic migrations with a from-scratch CI check, and runner tasks for
every loop a developer needs (`npm run dev`, `test`, `lint`, `typecheck`, `verify`,
`api:worker`, `api:reaper`, `infra:up`).

---

## 3. What is not done

### 3.1 Phase 3, Track A — four items

1. **The event bus and the WebSocket gateway.** `api/ws/__init__.py` is still a one-line
   placeholder. No `POST /v1/projects/{id}/ws-ticket`, no snapshot, no deltas, no cursors,
   no `resume`, no `resync_required`. `EventBusKind` exists in `settings.py` and nothing
   reads it. This is the largest outstanding piece of Phase 3, and the island's statuses
   are specified to come from it.
2. **The OpenAI-compatible provider (T13).** `infra/providers/` holds `anthropic.py` and
   nothing else. The shared conformance suite its acceptance criterion names does not
   exist either; it would be extracted from the Anthropic tests.
3. **T7, the managed-agent turn loop.** One model call per step is what exists;
   `services/dispatch.py` says so itself. No `max_turns` anywhere, no wall clock, and no
   token counts or durations reaching `metric_samples`.
4. **T8's decision layer.** Retries, jittered backoff, dead letters and uncertain outcomes
   exist on the dispatch row, but `RetryStep` is still absent from `advance()` —
   `decisions.py` says it "arrives with the retry policy, in T8" — and
   `workflow_steps.on_failure` remains a column no code reads.

### 3.2 Phase 3, Track B — nothing built

`apps/web/world/` holds a README. Phaser and Zustand are in no `package.json`. The
`world_nodes`, `world_routes`, `world_districts` and `world_layouts` tables exist with no
endpoints in front of them.

**What has changed here since 2026-11-17 is the "minimal pages" half, which is now
closed.** The agent form, the edit page and the execution detail page all exist, so the
sentence that used to stand here — an agent cannot be created or configured without
`curl` — is no longer true.

### 3.3 Everything after

Phase 3a (approvals, MCP tool execution, dispatchable agents), the rest of Phase 4, and
Phases 5–11. Nothing in them has started.

---

## 4. Open gaps and risks

**Reliability**

- **Stalled executions.** Settling or resolving a dispatch commits in one transaction;
  `advance_once` runs in a second, because it decides from committed state. A process that
  dies between them leaves an execution complete, consistent and **stalled**. T6c narrows
  this to "somebody comes back within the sweep interval plus the grace period"; it is not
  closed.
- **`needs_review` is terminal without a human.** A lease that expires mid-flight is never
  retried automatically — by design, because nobody knows whether the effect landed.
  `POST .../dispatches/{id}/resolve` is the only way out, at `admin`, with a mandatory
  note.

**Capability**

- `tool_call` steps cannot be published: the check constraint needs a `tool_id` and there
  is no `tools` table until Phase 6. `advance()`'s tool_call branch is tested and
  unreachable from a published workflow.
- Consequential external actions are **refused at publish**, not gated, until Phase 3a
  replaces the refusal with an approval.

**Security, stated honestly**

- Prompt injection choosing an *allowed* destination is not solved. The guarded client
  stops an agent reaching an internal address; it does not stop a malicious document
  telling an agent to POST to an attacker's public URL. The mitigations are a **gate on
  Phase 6**, including an adversarial test that asserts the exfiltration *succeeds*, so
  the limitation lives in CI rather than in memory.

**Web**

- **No `.tsx` file is under test.** Vitest runs in `node` with no jsdom, so page markup is
  covered by live probes and comments only. Anything whose correctness must be *asserted*
  belongs in a pure `.ts` module — which is why the redirect decisions live in
  `lib/auth/destination.ts` and the agent-test panel's decisions in
  `lib/agents/test-panel.ts`. **This is a standing decision, not a defect to fix in
  passing:** adding jsdom and a Testing Library is a dependency choice with its own cost,
  and it has been declined each time it came up.
- The agent test panel spends real money on a real provider, and its duplicate-submit
  protection is a convenience rather than a guarantee: `POST .../agents/{id}/test` has no
  idempotency key and no rate limit, so two requests are two executions and two charges.

**Closed on 2026-11-19**, and listed only so that a reader of the previous edition does not
go looking: the 403 reported as `api-unreachable`, the raw `"1 field(s) failed validation"`
on a malformed id, the client-side copy of the liveness rule, the untested `outcomeOf`
agent branch, the unasserted bearer token, the `{}` passed as `nextConfig`, and the stale
`.pill--warn` comment. Each now has a test that fails if it comes back.

---

## 5. The roadmap from here

### Next steps, in order

| | Step | Size | Done when |
|---|---|---|---|
| **N1** | The realtime spine: event bus, `ws-ticket`, gateway | ~1 week | two `websocat` sessions on different API processes see identical deltas, and killing one mid-stream loses nothing on reconnect |
| **N2** | OpenAI-compatible provider *(independent — can run in parallel)* | ~2 days | the same workflow runs against both providers with no change outside `infra/providers/`, and the SDK contract still fails on a planted violation |
| **N3** | T7's turn loop and T8's retry decision | ~1 week | a step can take several turns under a budget, and a failure classified as retryable is retried by `advance()` rather than by the dispatch row alone |
| **N4** | Reconcile what remains of Phase 3's ledger | ~½ session | the open Track A boxes are either ticked with a named test or recorded as wrong; Phase 3 can then be *declared* done and mean it |
| **N5** | Island skeleton I1 | ~1 week | the island shows real agents with real statuses, no mock-data path exists, and deleting `world/phaser/` leaves the backend and non-world web suites green. Needs N1 and the missing `world/nodes` endpoints |

**Why this order.** The previous edition's N1 and N2 — the agent pages and the execution
detail page — are done, which is what moved everything up. The realtime spine comes before
the island because the island's statuses are specified to come from live deltas; the other
order builds that feed twice. The provider is independent of all of it.

### After that

| Phase | What it adds | Watch for |
|---|---|---|
| **3a** | approvals, MCP tool execution, dispatchable agents | replaces the publish-time refusal of consequential work with a gate; `RequestApproval` is the decision `advance()` deliberately does not yet emit |
| **4 → M1** | the vertical slice: a described ecosystem proposed, created, run and watched | most of its Track A exists; the remainder is the island's I2 and the builder prototype, and I2 needs I1 |
| **5** | real workflows: branches, joins, retries at policy | |
| **6 → M2** | approvals, tools, integrations | the injection mitigations are a gate on this phase, not a later audit |
| **7 → M3** | external agents and triggers | `DispatchKind.agent_task_offer` and the claim/poll shape already exist |
| **8** | the full workspace | |
| **9 → M4** | the full island | the gate: can the island be deleted without one backend test changing? |
| **10 → M5** | the complete builder — **the MVP** | |
| **11 → M6** | hardening and launch | |

### Three questions still open from the proposal

| | Question | Default | Needed by |
|---|---|---|---|
| O1 | First real integration: Slack or Gmail? | Slack | Phase 6 |
| O2 | MCP as the primary tool mechanism? | Yes | Phase 6 |
| O3 | Polish the plain workspace before the island? | Yes | Phase 8 |

---

## 6. The decision worth making next

Phase 3's gate is *a real end-to-end managed execution, driven by `curl`, with no
frontend*. The web work of the last three days did not move that gate — it made the
results visible, which was its own justification and is now spent.

Everything left before Phase 4 is backend or island:

- To **declare Phase 3 complete**: N2 → N3 → N4, then N1.
- To **reach M1**: N1 (realtime) → N5 (island I1) → Phase 4's I2 and builder prototype.

The two orders share N1 and differ in what they leave open while building on it. There is
no third order that reaches the island sooner, because the island reads the feed that N1
builds.
