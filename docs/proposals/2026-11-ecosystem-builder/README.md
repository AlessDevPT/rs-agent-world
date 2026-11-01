# Proposal: AgentWorld as a visual AI ecosystem builder

> 2026-11-01. **Nothing here is implemented.** `docs/01`–`05` still describe the previous
> product direction and remain the contract the code was written against. On approval
> these documents are merged into that set and this directory is removed.

## Read in this order

| Document | Deliverables | What it settles |
|----------|-------------|-----------------|
| [00-GAP-ANALYSIS.md](00-GAP-ANALYSIS.md) | 1, 2, 3 | What exists, the 23 decisions that stay, the 11 that change, and what each change costs in built code |
| [01-DOMAIN-AND-PROTOCOL.md](01-DOMAIN-AND-PROTOCOL.md) | 4, 5 | 15 new tables with SQL, the nine already-designed ones, and the event envelope with a worked end-to-end trace |
| [02-ORCHESTRATION.md](02-ORCHESTRATION.md) | 6 | How work is decided and performed without breaking replay |
| [03-API-AND-FRONTEND.md](03-API-AND-FRONTEND.md) | 7, 8, 9 | Endpoints, module layout, the workspace, Build Mode and Live Mode |
| [04-ROADMAP-AND-MVP.md](04-ROADMAP-AND-MVP.md) | 10, 11 | Nine remaining phases, the MVP, the initial scope, the deferrals |

## The three findings that shape everything

**1. The protocol has a version number and zero consumers.** No `events` table, no SDK
package, no git tag, no integrator. Every expensive part of a protocol change costs
nothing today and would cost all of it after Phase 2 as previously planned. This is the
last moment it can be reshaped for free.

**2. No Phase 0 or Phase 1 code needs to be discarded.** Everything built is tenancy,
identity, authorization and isolation, and none of it takes a position on what an agent is
or on who decides what happens next. The pivot lands on Phase 2 onward, which is unbuilt.
The changes that do reach existing code are additive: new enums, new policy actions, new
tables joining `RLS_PROTECTED_TABLES`, one new required setting.

**3. The one thing that must be designed before any table is added.** An event log you
replay and an instruction queue you must not replay cannot be the same table. If
dispatching work were a reducer's side effect, then replaying the log to rebuild state
would send the emails again — and the replay-equivalence test, which exists to prove the
pipeline honest, would be the thing that sent them. The resolution is a transactional
outbox: one transaction writes the event (replayed), the state change (rebuilt by replay)
and the dispatch row (never replayed). It is in
[02-ORCHESTRATION.md §2](02-ORCHESTRATION.md), and it is the proposal everything else
depends on.

## The four capabilities, kept apart

The brief asks that these not be confused. Each gets a module boundary rather than an
intention:

| Capability | Question | Where |
|-----------|----------|-------|
| Observability | What happened? | `events` + pure reducers |
| Orchestration | What should happen next? | `domain/orchestration/advance.py` — pure, no I/O, no provider |
| Execution | Who performs it, how? | `workers/` + `infra/providers/` + `infra/tools/` |
| Control | Pause, stop, approve | control-plane services, audited and role-gated |

## Open questions for the approval conversation

1. **Provider for the MVP.** Phase 3 assumes Anthropic first, with an OpenAI-compatible
   second implementation behind the same Protocol. Fine, or should the second one land in
   Phase 3 too?
2. **First real integration.** Slack is proposed because its OAuth is the least painful
   and "post to a channel" is a convincing consequential action to gate. Gmail is the more
   compelling demo and a much worse first OAuth experience.
3. **MCP as the primary tool mechanism.** This is the highest-leverage choice in the
   proposal — one client turns every MCP server into tools with no AgentWorld code per
   service. It also bets on an ecosystem. The alternative is a bespoke adapter per service,
   which does not finish.
4. **Phase 7 before Phase 8.** The plain workspace ships before the island, so the product
   is proved usable without it. That delays the most distinctive feature by three weeks.
5. **Knowledge retrieval in v1** is PostgreSQL full-text, not pgvector, behind a
   `knowledge_search` tool so it can be swapped. Acceptable for v1?
6. **Timeline.** Roughly 20–26 weeks of focused single-developer work to the MVP at the end
   of Phase 9. If that is too long, the honest lever is §4's scope table rather than the
   phase list.
