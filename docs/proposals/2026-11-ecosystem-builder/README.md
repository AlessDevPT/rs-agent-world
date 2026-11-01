# Proposal: AgentWorld as a visual AI ecosystem builder

> Revision 2, 2026-11-01, incorporating the refinement brief. **Nothing here is
> implemented.** `docs/01`–`05` still describe the previous product direction and remain
> the contract the code was written against. On approval these documents are merged into
> that set and this directory is removed.

## Read in this order

| Document | Deliverables | What it settles |
|----------|-------------|-----------------|
| [00-GAP-ANALYSIS.md](00-GAP-ANALYSIS.md) | 1, 2, 3 | What exists, the 23 decisions that stay, the 14 that change, and what each change costs in built code |
| [01-DOMAIN-AND-PROTOCOL.md](01-DOMAIN-AND-PROTOCOL.md) | 4, 5 | 15 new tables with SQL, the credential mechanism, the visual-state model, and the event envelope with a worked end-to-end trace |
| [02-ORCHESTRATION.md](02-ORCHESTRATION.md) | 6 | How work is decided and performed without breaking replay, and what happens when it fails |
| [03-API-AND-FRONTEND.md](03-API-AND-FRONTEND.md) | 7, 8, 9 | Endpoints, module layout, the workspace, Build and Live Mode, and both prototypes |
| [04-ROADMAP-AND-MVP.md](04-ROADMAP-AND-MVP.md) | 10, 11 | Three parallel tracks, ten phases, per-phase acceptance criteria, the MVP, the scope, the deferrals |
| [05-PERMISSIONS.md](05-PERMISSIONS.md) | refinement 5 | The permission matrix, the actions that deliberately do not exist, and what keeps 72 rules honest |

## What revision 2 changed

| Refinement item | Where it landed |
|---|---|
| 1. Early conversational builder prototype | [03 §7.1](03-API-AND-FRONTEND.md), Phase 4 in [04 §2](04-ROADMAP-AND-MVP.md) |
| 2. Early visual island prototype | [03 §5](03-API-AND-FRONTEND.md) milestones I1 and I2, Phases 3–4 |
| 3. Credential and integration security | [01 §6](01-DOMAIN-AND-PROTOCOL.md), rewritten in full |
| 4. Transactional outbox reliability | [02 §3 and §8](02-ORCHESTRATION.md), rewritten in full |
| 5. Authorization review | [05-PERMISSIONS.md](05-PERMISSIONS.md), new |
| 6. Visual state separate from execution state | [01 §10](01-DOMAIN-AND-PROTOCOL.md), new; decision C12 |
| 7. Roadmap balancing three objectives | [04 §1–§4](04-ROADMAP-AND-MVP.md), rewritten |
| 8. Revised deliverables | [04](04-ROADMAP-AND-MVP.md) §2 milestones, §3 dependencies, §5 MVP, §6 assumptions, §8 security, §9 deferrals |

## The trade-off the refinement introduces

**Roughly four weeks.** 24–30 weeks to the MVP, against 20–26 for the sequential plan.
Accounted for line by line in [04 §1](04-ROADMAP-AND-MVP.md): the island and the builder
are each touched twice (+2.5 w), minimal web pages are needed in Phase 3 rather than Phase
8 (+1 w), and one developer alternating between Postgres and Phaser pays a context-switch
tax (+0.5 w).

**What it buys is a demonstrable product at about week eight instead of week twenty**, and
with it three risks move from "discovered late" to "discovered early": whether a beginner
can understand an ecosystem spatially at all, whether the domain model is shaped the way a
natural-language request decomposes, and whether the island and the plain pages can stay in
sync as two views of one model.

**What makes it safe rather than wasteful** is that neither prototype gets its own
architecture. The island is built on the real `projector` / `interpreter` / `dispatch`
split from its first commit and **never has a mock data path** — it renders real backend
objects or nothing. The builder uses the final plan schema, propose/apply split, transaction
boundary and permission model, with a `BuilderCapability` enum restricting only which object
kinds it may propose; Phase 10's acceptance criteria include the Phase 4 builder tests
passing unmodified.

Two things were considered and rejected: starting the island in Phase 2, where there would
be nothing real to render and it would be built against a mock and then rebuilt; and pulling
the complete builder forward, which would write workflows, triggers and integrations that do
not exist until Phase 7.

## The three findings that shape the proposal

**1. The protocol has a version number and zero consumers.** No `events` table, no SDK
package, no git tag, no integrator. Every expensive part of a protocol change costs nothing
today and would cost all of it after Phase 2 as previously planned. This is the last moment
it can be reshaped for free.

**2. No Phase 0 or Phase 1 code needs to be discarded.** Everything built is tenancy,
identity, authorization and isolation, and none of it takes a position on what an agent is
or on who decides what happens next. 23 existing decisions are unchanged; the 14 that change
all land additively — new enums, new policy rows, new tables joining `RLS_PROTECTED_TABLES`,
one new required setting.

**3. An event log you replay and an instruction queue you must not replay cannot be the
same table.** If dispatching work were a reducer's side effect, replaying the log to rebuild
state would send the emails again — and the replay-equivalence test, which exists to prove
the pipeline honest, would be the thing that sent them. The resolution is a transactional
outbox: one transaction writes the event (replayed), the state change (rebuilt by replay)
and the dispatch row (never replayed), with protocol promise 7 asserting that folding the
log causes no work.

## The four capabilities, kept apart

| Capability | Question | Where |
|-----------|----------|-------|
| Observability | What happened? | `events` + pure reducers |
| Orchestration | What should happen next? | `domain/orchestration/advance.py` — pure, no I/O, no provider |
| Execution | Who performs it, how? | `workers/` + `infra/providers/` + `infra/tools/` |
| Control | Pause, stop, approve | control-plane services, audited and role-gated |

## The early vertical slice — M1, about week eight

> A user types *"I want a researcher that finds competitor news and a writer that turns it
> into a short brief."* AgentWorld proposes two agents and the connection between them,
> explains what it will create, and creates nothing yet. The user confirms. Two buildings
> appear on the island with a road between them. The user drags one to a better spot and
> opens it to read its instructions. They press Run. The researcher's building lights up,
> its status goes to `working`, the road carries the handoff, the writer runs, and the
> result is on screen — with every status change arriving over the WebSocket from a real
> folded event.

Acceptance criteria for it are in [04 §2, Phase 4](04-ROADMAP-AND-MVP.md). If that demo is
unconvincing, week eight is the cheapest moment to change direction — the domain model is
written and almost nothing is built on top of it.

## Decisions taken rather than asked about

Two of the original six open questions were engineering sequencing rather than product
calls, so they are decided here and flagged for override:

**Provider count in Phase 3** — Anthropic only to prove the orchestration loop, then the
OpenAI-compatible implementation as the **last task of Phase 3**. Writing the second
implementation is the only real test that the `ModelProvider` Protocol is not shaped around
one vendor, and discovering otherwise after Phase 6 depends on it is expensive. Roughly two
days, while the first one is still fresh.

**Knowledge retrieval in v1** — PostgreSQL full-text, behind the `knowledge_search` tool.
pgvector needs an embedding provider, a chunking pipeline and a re-index job: three moving
parts for a quality gain nobody can measure at first-user document volumes. Because it sits
behind a tool the agent calls, swapping it later changes no contract.

## Still open

1. **First real integration.** Slack is proposed: the least painful OAuth, and "post to a
   channel" is a convincing consequential action to gate. Gmail is the more compelling demo
   and a much worse first OAuth — restricted scopes mean app review, which is calendar time.
2. **MCP as the primary tool mechanism.** The highest-leverage choice in the proposal: one
   client turns every MCP server into tools with no AgentWorld code per service. It also
   bets on an ecosystem outside our control. The alternative is a bespoke adapter per
   service, which does not finish.
3. **Phase 8 before Phase 9** — the plain workspace ships before the island's polish, so the
   product is proved usable without it. The island prototype at Phase 4 means this no longer
   delays *seeing* the island, only finishing it.
4. **Self-approval is permitted in v1.** The author of a workflow can approve its own steps;
   four-eyes is deferred to v1.1. Fine for a first release, or a gap worth closing now?
5. **Timeline.** 24–30 weeks to M5. The honest lever is the scope table in
   [04 §7](04-ROADMAP-AND-MVP.md), not the phase list — compressing phases produces the same
   work with less testing, and the two things this product cannot afford to ship untested are
   the approval gate and tenant isolation.
