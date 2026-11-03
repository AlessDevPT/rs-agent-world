# Gap analysis: monitoring platform → ecosystem builder

> Proposal, 2026-11-01, revised after the refinement brief. Nothing here is implemented.
> On approval these documents are merged into `docs/01`–`05`, which still describe the
> previous product direction and remain the contract the code was written against until
> then.

Deliverables 1–3 of the revision brief: what exists, what survives, what has to change.
The decision list in §4 runs to C18; C12–C14 came from the first refinement and C15–C18
from the second, each marked. `07-DECISION-RECORD.md` records where every decision
*stands* — implemented, approved, deferred or open — and this document explains why.

## 1. What the repository actually contains

Read from the repository rather than from the roadmap, because the two have diverged in
AgentWorld's favour.

| Area | Documented | Implemented | Notes |
|------|-----------|-------------|-------|
| Tenancy: users, workspaces, members, projects, api_keys, audit_log | yes | **yes** | 6 tables, migrations `0001`–`0005` |
| Two auth planes, Supabase JWKS, Argon2id keys | yes | **yes** | 444 tests |
| RLS + non-superuser app role + append-only grants | yes | **yes** | enforced and proved non-vacuous |
| `authorize()` policy table, 404-vs-403 rule | yes | **yes** | 15 actions |
| Control plane: `/v1/me`, workspaces, projects, api-keys, `/v1/self/key` | yes | **yes** | 14 OpenAPI paths |
| Web: Supabase sign-in, workspace switcher, projects, keys | yes | **yes** | |
| Agents, tasks, runs, run_steps | yes | **no** | tables do not exist |
| `events` / `logs` / `metric_samples`, partitioning | yes | **no** | |
| Canonical event envelope, reducers | yes | **no** | only `PROTOCOL_VERSION = "1.0"` exists |
| Commands, integrations, webhooks, adapters | yes | **no** | `adapters/` is an empty package |
| WebSocket gateway, event bus | yes | **no** | `api/ws/` is an empty package |
| Island, `world_layouts`, `world_placements`, projector | yes | **no** | `world/` holds a README |
| SDKs | yes | **no** | no `packages/` directory |

### The fact that makes this revision cheap

**The protocol has a version number and zero consumers.** There is no `events` table, no
SDK package, no git tag, no release, and no integrator. `PROTOCOL_VERSION = "1.0"` is a
constant in one file and a field in one JSON response.

Every expensive part of a protocol change — migrating stored events, versioning
reducers, supporting two envelope shapes, re-releasing SDKs, breaking integrators — costs
nothing today and would cost all of it after Phase 2 as originally planned. **This is the
last moment the protocol can be reshaped for free.** The revision arriving now rather
than in three weeks is worth more than anything else in this document.

### What the pivot does *not* touch

Everything implemented is tenancy, identity, authorization and isolation. None of it
takes a position on what an agent *is* or on who decides what happens next. A workflow
execution needs a tenant, a role check and an audit row exactly as a project does.

**No Phase 0 or Phase 1 code needs to be discarded or rewritten.** The changes that do
land on it are additive and listed in §4.

## 2. The gap, stated precisely

The brief's sentence that reorders the architecture is: *"Approvals must be real
execution gates, not just visual notifications."* The same demand is implicit in
executable workflows, in tool calls and in pause/resume. All of it requires AgentWorld to
**decide and perform work**, and the current design is built on the opposite premise.

`docs/01-ARCHITECTURE.md` §3 is explicit about it:

> 2. **Events are facts about the past.**
> 5. **State is derived, never directly writable by agents.**

and §3.4 of the API spec:

> the platform cannot force a third-party process to obey, so it never pretends the
> command succeeded until the agent says so.

That is a well-reasoned design for an **observability** product: work happens elsewhere,
AgentWorld watches. The new product needs four separable capabilities, and the brief is
right to insist they not be confused:

| Capability | Question it answers | Exists today |
|-----------|---------------------|--------------|
| **Observability** | What happened? | designed in full, built in none |
| **Orchestration** | What should happen next? | **absent — no concept of a decision** |
| **Execution** | Who performs the work, and how? | **absent — AgentWorld runs nothing** |
| **Control** | Pause, stop, approve, modify | designed as *advisory* commands only |

The gap is not "more entities". It is that the system has no notion of **intent** — a
record of work that *should* happen — and no component whose job is to produce it.

### The trap this creates, and why it must be solved before any table is added

The obvious move is to put commands in the event log: a `step.dispatch_requested` event,
folded by a reducer that calls the agent. That breaks the one guarantee the brief asks to
preserve:

> Preserve event replay and idempotency guarantees where applicable.

Replay means re-folding the log. If folding an event performs work, then replay **sends
the emails again**. An event log you can replay and an instruction queue you must not
replay cannot be the same table, and discovering that after `events` is partitioned and
populated is expensive.

The resolution is in `02-ORCHESTRATION.md` §2 and is the single most important proposal
in this revision: events record facts and are replayed; a separate non-replayed
dispatch outbox carries intent; both are written in one transaction. Replay reproduces
state, never side effects.

### Second trap: "connection" means two different things

The brief lists connections as first-class — agent to agent, workflow to workflow, agent
to tool — and separately asks for workflows with "sequences of actions", "conditions and
branching" and "passing information between steps". Those are not the same relationship:

- A **workflow edge** is control flow *inside one workflow version*: step B runs after
  step A, with this condition, taking that part of A's output. It is versioned with the
  workflow and meaningless outside it.
- A **connection** is a standing route or grant *between components*: this agent may use
  that tool, this agent may delegate to that agent, this workflow's completion triggers
  that one.

One table for both produces either edges that cannot be versioned or grants that have to
be re-declared in every workflow. They are modelled separately in
`01-DOMAIN-AND-PROTOCOL.md` §3, and the island draws both as paths, which is where the
conflation comes from.

### Third trap: credentials are not API keys

The existing key design — Argon2id hash of a peppered secret, unrecoverable even by the
server — is right, and the Phase 1 test that searches every text column for the plaintext
proves it holds. It is also **exactly the wrong shape for an integration**: you cannot
call Gmail with a hash.

`integrations.secret_hash` in the current schema is for verifying *inbound* HMAC
signatures. Outbound credentials — OAuth tokens, third-party API keys — must be
*recoverable by the server and by nothing else*, which is a different mechanism
(envelope encryption, rotation, a `SecretStore` interface) with different failure modes.
Conflating them yields either unusable integrations or recoverable AgentWorld keys. Both
appear in `01-DOMAIN-AND-PROTOCOL.md` §6, kept apart.

### Fourth: "create an agent" has no meaning yet

The documented `agents` table describes something that connected from outside: `agent_key`
chosen by the caller, `runtime` as free-form descriptive JSON the core "never branches
on", status driven by heartbeats. There is no instruction text, no model, no tool grant,
no owner of execution.

A beginner creating an agent through conversation needs AgentWorld to hold its
instructions, pick its provider, grant it tools and *run it*. That is a different kind of
object sharing one table with the first kind — and the brief is right that the difference
must be explicit rather than papered over:

> Do not assume every external agent can be controlled. Distinguish between agents that
> can only report activity and agents that expose execution/control capabilities.

Proposed as `agent.interaction_mode` with three values, in
`01-DOMAIN-AND-PROTOCOL.md` §2.

## 3. Architecture decisions that stay exactly as they are

These are load-bearing, independent of the pivot, and re-deciding them would cost weeks
for nothing. Listed because "preserve good architectural decisions" needs to name them.

| # | Decision | Why it survives the pivot |
|---|----------|---------------------------|
| K1 | Two auth planes, separate principal *types* | An orchestrator dispatching work still needs "is this a human or a machine" to be a type error, not a field check |
| K2 | Supabase JWKS verification, fixed algorithm list, local, with rotation cooldown | Unrelated to what the product does |
| K3 | API keys: Argon2id + pepper, prefix lookup, unrecoverable, environment-labelled | Still exactly right for AgentWorld's own credentials |
| K4 | `authorize(principal, action, resource)`: one function, one table | Grows rows for workflows, approvals, integrations. The shape is what makes 60 new permissions reviewable |
| K5 | No membership ⇒ 404, not 403 | Applies unchanged to every new object |
| K6 | `TenantScope` required to construct a scoped repository | The main defence as the table count triples |
| K7 | RLS on a non-superuser role, keyed on the caller's visibility set, with the vacuity guard | Every new tenant table joins `RLS_PROTECTED_TABLES`. The guard is worth more as the surface grows |
| K8 | Append-only as a *privilege*, not a convention | Extends to `events`, `logs`, `approvals` decisions |
| K9 | Audit row in the same transaction as the mutation | An ecosystem builder that writes 20 objects from one sentence makes this more important, not less |
| K10 | Event sourcing: append-only log, pure reducers, replay equivalence | **Kept, and the reason the orchestration design works.** See `02-ORCHESTRATION.md` §2 |
| K11 | `dedup_key` idempotency, `(project_id, dedup_key)` unique | Retried dispatches and at-least-once delivery need it more than telemetry did |
| K12 | Client `occurred_at`, server `received_at`, skew recorded not corrected | Unchanged |
| K13 | Unknown event types and payload fields stored, not rejected | Unchanged, and now also how a connected agent of a future kind reports |
| K14 | `agent_key` as caller-chosen stable identity | Still right for external agents; managed agents get a server-assigned one |
| K15 | Provider SDKs forbidden in `domain/`, enforced by import-linter | **Non-negotiable now that AgentWorld calls providers.** Extended in `03-API-AND-FRONTEND.md` §2 |
| K16 | Adapters translate payloads and never touch persistence | Unchanged |
| K17 | The island is a projection; `project()` is pure and Phaser-free | **Kept, and extended rather than replaced.** See `03-API-AND-FRONTEND.md` §5 |
| K18 | Presentation tables touched by no reducer, adapter or SDK | **Unchanged, and strengthened.** Two tables become four, none holding execution state (C12) |
| K19 | RFC 9457 problem details everywhere, request id in logs, audit and `instance` | Unchanged |
| K20 | Cursor pagination; no offsets on a moving stream | Unchanged |
| K21 | Protocol compatibility promises 1–6, including "a headless integration never opening the web app stays fully functional" | **Unchanged and now harder-won.** Promise 6 is what stops the builder becoming mandatory |
| K22 | Hand-written migrations + a drift test against a migrated database | Essential at 25 tables |
| K23 | Frontend/backend independence; the web app may not import from `apps/api` | Unchanged |

## 4. Architecture decisions that must change

Each one states what changes, why, and what it costs in already-built code.

### C1 — The protocol gains an orchestration half

**Now:** seven observational namespaces (`agent`, `task`, `run`, `tool`, `log`, `metric`,
`custom`), all reporting what an external process already did.

**Change:** add `workflow.*`, `approval.*` and `connection`-less definition
notifications; generalise the envelope's identity fields from
`agent_key`/`task_ref`/`run_ref` to a typed `subject` plus a `correlation` block.

**Why:** an execution step, an approval and a workflow execution are all subjects an
event can be about, and bolting each onto a flat agent-shaped envelope produces six
optional foreign keys and no way to add a seventh.

**Cost to built code:** `domain/protocol/version.py` only. `PROTOCOL_VERSION` stays
`"1.0"` — nothing consumed 1.0, so revising it in place is honest and a bump to `"2.0"`
would imply someone had the first one.

### C2 — Configuration changes leave the replayable event log

**Now:** implied that everything interesting is an event.

**Change:** `events` holds **execution facts only**. Creating an agent, editing a
workflow, enabling a connection are control-plane mutations: recorded in `audit_log`,
versioned in `workflow_versions`, and published to the realtime bus as state deltas —
but never appended to `events`.

**Why:** replay must reproduce execution state from a log folded against a *definition
snapshot*. If definition changes are interleaved in the same log, replay depends on
config ordering and the equivalence test becomes meaningless. This also keeps the
partitioned, 90-day-retention table free of rows that must live forever.

**Cost to built code:** none. It constrains what Phase 2 writes.

### C3 — `agents` gains an interaction mode and a definition

**Now:** one implicit kind of agent — external, observed, descriptive `runtime` JSON.

**Change:** `interaction_mode ∈ {managed, dispatchable, observed}`, plus definition
columns (role, instructions, model config reference, tool and knowledge grants) that are
user-editable while `status` stays reducer-only.

**Why:** the product's main promise is creating an agent, and honesty requires refusing
to wire an `observed` agent into an executable step rather than drawing a line and hoping.

**Cost to built code:** none — `agents` does not exist yet. The existing documented split
("`PATCH /v1/agents/{id}` is **not** `status`") already anticipates it.

### C4 — Commands stop being the only control mechanism

**Now:** `POST /v1/agents/{id}/commands`, advisory, honoured only if the agent advertised
the capability.

**Change:** keep it exactly as-is for `dispatchable` and `observed` agents — it is the
honest answer there. Add real control for `managed` agents, where AgentWorld owns the
runner and can actually pause, resume, cancel and step.

**Why:** "pause when the underlying runtime supports these commands" is the brief's own
qualifier. Two mechanisms, each honest about its reach, rather than one that over- or
under-promises.

**Cost to built code:** none.

### C5 — Tasks are demoted from top-level concept to agent interface

**Now:** `tasks` is the central unit of work; users create tasks for agents to claim.

**Change:** a `workflow_execution` is the top-level unit. A `task` becomes what an
execution step *materialises* when it needs work done by a dispatchable agent — the
wire-level interface to an external worker, with its claim, heartbeat and
`FOR UPDATE SKIP LOCKED` machinery intact.

**Why:** it preserves the whole designed task pipeline and gives it a purpose in the new
product instead of a competing one. Standalone tasks remain possible for users who want a
work queue without a workflow.

**Cost to built code:** none. `task_status` may need an added member; `ALTER TYPE ... ADD
VALUE` handles it and the drift test will demand the migration.

### C6 — Workflows, connections, tools, triggers, knowledge, approvals and credentials become domain objects

**Now:** absent. `task_dependencies` is the nearest thing and expresses only "after".

**Change:** 16 genuinely new tables — `model_profiles`, `workflows`,
`workflow_versions`, `workflow_steps`, `workflow_edges`, `connections`, `tools`,
`credentials`, `knowledge_sources`, `knowledge_documents`, `triggers`,
`workflow_executions`, `execution_steps`, `approvals`, `approval_policies`,
`dispatches` — plus new
columns on `agents` and `integrations`. The nine tables the previous design already
specified (`agents`, `tasks`, `runs`, `run_steps`, `events`, `logs`,
`metric_samples`, `integrations`, `world_*`) are built as designed, with the
additions in `01-DOMAIN-AND-PROTOCOL.md`. Phased across Phases 2–8.

**Why:** the brief requires each as first-class, and a workflow that is only a visual
diagram is the specific failure it rules out.

**Cost to built code:** additive migrations from `0006`. Each new tenant table joins
`RLS_PROTECTED_TABLES`, which is a one-line change per table in a pattern that already
exists and is tested.

### C7 — An orchestrator and a dispatch outbox are new components

**Now:** `services/ingest.py` was to be the only writer of state, driven entirely by
inbound events.

**Change:** add `domain/orchestration/` (pure: `advance(definition, state) -> Decision[]`)
and a `dispatches` outbox with worker processes.

**Why:** something has to decide what happens next, and it must be unit-testable without
a database and without a provider — the same property that makes the reducers testable.

**Cost to built code:** none. It adds a layer; the layering contract already permits it.

### C8 — A secret store, separate from the key hasher

**Now:** `integrations.secret_hash`, for inbound HMAC verification only.

**Change:** a `credentials` table of envelope-encrypted material behind a `SecretStore`
interface, with OAuth refresh. Never returned by any endpoint; referenced only by id.

**Why:** outbound integrations need usable secrets. §2 above on why this must not reuse
the Argon2id path.

**Cost to built code:** none. `infra/hashing.py` is untouched and keeps its current job.

### C9 — The island gains a second, equally pure projection and a gesture path

**Now:** `project(domain, layout) -> WorldState`, one-way, read-only.

**Change:** two projections (`projectBuild`, `projectLive`) and a pure
`interpret(gesture, context) -> Intent[]`. Phaser raises gestures and never calls an API;
a thin dispatcher turns Intents into ordinary control-plane calls.

**Why:** Build Mode has to create real objects without the island becoming the source of
truth. Keeping `interpret` pure means the gesture-to-intent mapping is unit-tested with
no canvas, and deleting `world/phaser/` still leaves a working product.

**Cost to built code:** `world/` holds a README and two eslint rules. The rules stay; a
third scoped rule is added for the gesture layer.

### C10 — The frontend grows an object-management workspace, not a dashboard

**Now:** Phase 5 was "a usable dashboard: projects, agents, tasks, activity, keys".

**Change:** an editor for the ecosystem — create and configure agents, workflows,
connections, integrations, knowledge, triggers — with simple and advanced modes
throughout, usable entirely without the island.

**Why:** the brief requires both that beginners never meet LLM infrastructure and that
the island never be mandatory. That is two presentations of one configuration model, which
is a frontend concern and not a second API.

**Cost to built code:** the Phase 1 web app is the shell this sits inside. Additive.

### C11 — The roadmap reorders into three parallel tracks

**Now:** all protocol, then all control plane, then all realtime, then UI, then island.
Nothing demonstrable until Phase 6.

**Change:** a foundation track, a visual track and a builder track running in parallel,
with early prototypes of both differentiators and a full end-to-end demonstration at the
end of Phase 4 — about week eight.

**Why:** the product's claim is "agents collaborate and produce a result". A horizontal
build cannot show that until almost everything exists, cannot discover that the
orchestration model is wrong until it is expensive to change, and — as the refinement
brief puts it — spends months on backend infrastructure without testing the actual user
experience.

**Cost:** roughly four weeks of calendar time, accounted for line by line in
`04-ROADMAP-AND-MVP.md` §1. **Cost to built code:** none.

### C12 — Visual state becomes its own persisted model  *(refinement brief, item 6)*

**Now:** `world_placements`, keyed on `agent_id`, holding a tile and a sprite choice.

**Change:** four tables — `world_layouts`, `world_districts`, `world_nodes`,
`world_routes` — holding position, size, appearance, grouping and visual routing for every
kind of object, and **no column that mirrors execution state**.

**Why:** two concrete failures. If position lived on `agents`, dragging a building would be
a domain write needing the permission that edits an agent and appearing in the audit trail
as a configuration change. And if a node's existence were derived from execution state, the
island would empty itself exactly when something went wrong — whereas a shuttered building
is the information the user needs.

A district also stops being a project. The original proposal mapped the brief's "projects or
districts" onto `projects` alone; a district is now a visual region *within* one project's
island, so an ecosystem can be grouped visually without creating a second tenancy boundary.

**Cost to built code:** none — the table does not exist yet. It widens
`docs/02-DATABASE.md` §6 from two tables to four.

### C13 — Both differentiators get early prototypes on final architecture  *(items 1, 2, 7)*

**Now:** island at Phase 8, conversational builder at Phase 9.

**Change:** an island prototype at Phases 3–4 rendering real backend objects, and a
capability-limited builder prototype at Phase 4, both on the architecture the complete
versions use. The complete versions stay late.

**Why:** they are the product's differentiators and the plan must not leave them untested
until week twenty. The constraint that makes this safe rather than wasteful is that neither
prototype gets its own architecture: the island is built on the real
`projector` / `interpreter` / `dispatch` split from its first commit and never has a mock
data path, and the builder uses the final plan schema with a `BuilderCapability` enum
restricting only *which object kinds* it may propose. Phase 10's acceptance criteria
include the Phase 4 builder tests passing unmodified.

**Cost to built code:** none. Costs calendar time, accounted for in
`04-ROADMAP-AND-MVP.md` §1.

### C14 — A permission matrix, not a count  *(item 5)*

**Now:** this document's first draft said "approximately 45 new authorization rules".

**Change:** a worked matrix of 62 new actions (77 total; **78 implemented** — T6b added
`dispatch.resolve_review`, see `17-T6B-REVIEW-API.md`) with scope, required role and a
sensitivity flag per action — plus, more usefully, the nine actions that deliberately **do
not exist**, the three whose required role is conditional, and the five that are `admin`
rather than `member` with the reason for each. In `05-PERMISSIONS.md`.

**Why:** the count went *up* because least privilege means splitting actions rather than
merging them. `integration.read` is a `member`; `integration.read_config`, which exposes
endpoints and scopes, is an `admin`. And the matrix surfaced one genuine vulnerability that
a flat list would have hidden: `agents.dispatch.url` is a URL AgentWorld makes outbound
requests to, which is a server-side request forgery vector, so `agent.configure` is `admin`
and the URL is checked against a deny-list at request time rather than only at save time.

**Cost to built code:** additive rows in the existing `MINIMUM_ROLE` table. The two Phase 1
tests that keep it honest — `test_every_action_has_a_minimum_role` and
`test_the_matrix_covers_every_mutating_route` — generalise without modification.

### C15 — Provider independence is four axes, not one  *(refinement 2, item 3)*

**Now:** one `infra/providers/` directory and an implicit assumption that an agent's
framework and its model provider are one choice.

**Change:** four orthogonal axes, stated and enforced — **model provider** (who serves the
LLM), **agent runtime** (who executes the loop), **tool provider** (who implements a
capability), **communication protocol** (how work is exchanged).

**Why:** the assumption is load-bearing in the wrong direction. A customer running LangGraph
over Gemini must be dispatchable and monitorable **without AgentWorld supporting Gemini at
all** — AgentWorld never calls their model. Collapsing the axes would make "support
externally hosted agents" require "support every provider", which is the difference between
a shipping v1 and an endless one. The schema says so too: `model_profile_id` is valid only
for a `managed` agent, by check constraint.

**Cost to built code:** none. It renames nothing and adds one constraint.

### C16 — Approval policies, with self-approval as a decision  *(refinement 2, item 1)*

**Now:** `approvals.required_role` only. Revision 2 noted, as an open question, that v1
permitted self-approval.

**Change:** an `approval_policies` row per project, resolved by a pure
`resolve_requirement` taking the strictest of project policy, step config, tool default and
connection. Defaults: `read`/`write` work may be self-approved; **`consequential` work needs
a different person**. Two hard floors are not configurable at all — an approver's role may
never fall below the role required to perform the underlying action, and a policy produces a
requirement rather than a capability.

**Why:** an unrestricted self-approval default makes the gate theatre for a single-member
workspace, which is most of them at first. The design also closes a loophole the obvious
implementation leaves: attribution follows whoever last *enabled the trigger*, so scheduling
a workflow is not a way around a gate you could not pass by hand.

**Cost to built code:** none. One new table, one new permission pair, and
`member_role` reused rather than a new role invented.

### C17 — Outbound HTTP is resolve-validate-pin, not a deny-list  *(refinement 2, item 2)*

**Now:** revision 2 said `dispatch.url` was checked against a deny-list of private ranges.

**Change:** a single guarded client that resolves the host, validates **every** resolved
address, and connects to a validated address with the original `Host` and SNI — so there is
no second resolution to poison. No redirects by default. Normalised errors. Network-level
egress policy at the execution environment as the boundary that holds if the application
layer has a bug. In `06-OUTBOUND-SECURITY.md`.

**Why:** a deny-list checks a *name* and then connects to an *address*, which DNS rebinding
defeats in one second of TTL. It also misses IPv4-mapped IPv6 (`::ffff:169.254.169.254` is
the metadata endpoint), NAT64, redirects, and non-`http` schemes. Each is a known technique,
not a hypothetical.

**Cost to built code:** none, and it adds a sixth import-linter contract confining
`httpx.AsyncClient` to `infra/net/` — with `infra/auth/supabase.py` named as the one
exemption, since its URL is operator configuration and it runs before any tenant context
exists.

### C18 — Knowledge retrieval is an interface  *(refinement 2, item 4)*

**Now:** "full-text in v1, pgvector later behind the same tool".

**Change:** a `KnowledgeRetriever` Protocol with explicit `RetrievalQuery`, `Passage` and
`RetrievalResult` types, and `index` / `forget` alongside `search` so a vector backend's
re-index pipeline has somewhere to live.

**Why:** "behind the same tool" was a sentence, not a contract. The specific thing it was
missing: `score` means different things in BM25 and cosine similarity, so the interface now
states that scores are comparable only within one result set — which forbids the
`score > 0.8` workflow condition that would otherwise break on the swap.

**Cost to built code:** none.

## 5. Two decisions this revision deliberately does *not* make

**The old roadmap's "agent-to-agent coordination → v2" deferral is withdrawn**, as the
brief instructs. It was deferred on the grounds that it "changes the protocol from
observational to transactional" — which was correct, and is now the point.

**No universal execution engine.** The brief warns against it and the warning is right.
What is proposed is a single-tenant, single-process orchestrator over a Postgres outbox,
with sequence, parallel, condition, approval and retry. No loops, no map-over-collection,
no sub-workflows in v1 — see `04-ROADMAP-AND-MVP.md` §4. Those are the features that turn
an orchestrator into a workflow language, and each can be added behind the same
`advance()` signature once something real runs on it.
