# Proposal: AgentWorld as a visual AI ecosystem builder

> **Revision 4, 2026-11-01** — approved; Phase 2 implementation has begun. **Nothing here is implemented.**
> `docs/01`–`05` still describe the previous product direction and remain the contract the
> code was written against. On approval these documents are merged into that set and this
> directory is removed.

**Where every decision stands — implemented, approved, deferred, open — is in
[07-DECISION-RECORD.md](07-DECISION-RECORD.md). Start there if you want status rather
than reasoning.** Its §5 lists the six architectural risks the design bounds rather than
solves.

## Read in this order

| Document | Deliverables | What it settles |
|----------|-------------|-----------------|
| [00-GAP-ANALYSIS.md](00-GAP-ANALYSIS.md) | 1, 2, 3 | What exists, the 23 decisions that stay, the 18 that change, and what each costs in built code |
| [01-DOMAIN-AND-PROTOCOL.md](01-DOMAIN-AND-PROTOCOL.md) | 4, 5 | The four provider-independence axes, 16 new tables with SQL, credentials, approval policies, retrieval, visual state, and the event envelope with a worked trace |
| [02-ORCHESTRATION.md](02-ORCHESTRATION.md) | 6 | How work is decided and performed without breaking replay, and what happens when it fails |
| [03-API-AND-FRONTEND.md](03-API-AND-FRONTEND.md) | 7, 8, 9 | Endpoints, module layout, the workspace, Build and Live Mode, island legibility, and both prototypes |
| [04-ROADMAP-AND-MVP.md](04-ROADMAP-AND-MVP.md) | 10, 11 | Three parallel tracks, ten phases, per-phase acceptance criteria, the MVP, the scope, the deferrals |
| [05-PERMISSIONS.md](05-PERMISSIONS.md) | ref. 1 §5 | The permission matrix, the actions that deliberately do not exist, and what keeps 77 rules honest |
| [06-OUTBOUND-SECURITY.md](06-OUTBOUND-SECURITY.md) | ref. 2 §2 | The outbound security boundary: what AgentWorld guarantees, what the deployment must provide, what neither can prevent |
| [07-DECISION-RECORD.md](07-DECISION-RECORD.md) | ref. 2 §6 | Status of every decision, the three remaining questions, and six unresolved risks |

## What revision 3 changed

| Refinement item | Where it landed |
|---|---|
| 1. Approval policies, no unrestricted self-approval | [01 §7](01-DOMAIN-AND-PROTOCOL.md), [02 §7](02-ORCHESTRATION.md), [05](05-PERMISSIONS.md); decision C16 |
| 2. Stronger outbound network security | [06-OUTBOUND-SECURITY.md](06-OUTBOUND-SECURITY.md), new; decision C17 |
| 3. Provider independence as four separate axes | [01 §2](01-DOMAIN-AND-PROTOCOL.md); decision C15 |
| 4. Knowledge retrieval abstraction | [01 §5](01-DOMAIN-AND-PROTOCOL.md); decision C18 |
| 5. Island legibility acceptance criteria | [03 §5](03-API-AND-FRONTEND.md), Phases 4 and 9 |
| 6. Final decision record | [07-DECISION-RECORD.md](07-DECISION-RECORD.md), new |

Revision 2's changes — the early island and builder prototypes, visual state as its own
table set, the credential and outbox specifications, and the permission matrix — are in the
same table in that document's own history and summarised in
[07 §2](07-DECISION-RECORD.md).

## The five things that most shape the proposal

**1. The protocol has a version number and zero consumers.** No `events` table, no SDK
package, no git tag, no integrator. Every expensive part of a protocol change costs nothing
today and would cost all of it after Phase 2 as previously planned. This is the last moment
it can be reshaped for free.

**2. No Phase 0 or Phase 1 code needs to be discarded.** Everything built is tenancy,
identity, authorization and isolation, and none of it takes a position on what an agent is
or on who decides what happens next. 23 existing decisions are unchanged; the 18 that change
all land additively.

**3. An event log you replay and an instruction queue you must not replay cannot be the
same table.** If dispatching work were a reducer's side effect, replaying the log to rebuild
state would send the emails again — and the replay-equivalence test, which exists to prove
the pipeline honest, would be the thing that sent them. The resolution is a transactional
outbox, with protocol promise 7 asserting that folding the log causes no work.

**4. A URL deny-list is the wrong primitive for outbound security.** It validates a *name*
and then connects to an *address*, which DNS rebinding defeats with one second of TTL. The
proposal resolves, validates every resolved address, and pins the connection to a validated
one — so there is no second resolution to poison. It also catches what careful deny-lists
miss: `::ffff:169.254.169.254` is the cloud metadata endpoint written as IPv6.

**5. Provider independence is four statements, not one.** Model provider, agent runtime,
tool provider and communication protocol are orthogonal. A customer running LangGraph over
Gemini is dispatchable and monitorable **without AgentWorld supporting Gemini at all**,
because AgentWorld never calls their model. Collapsing those axes would make "support
externally hosted agents" require "support every provider".

## The early vertical slice — M1, about week eight

> A user types *"I want a researcher that finds competitor news and a writer that turns it
> into a short brief."* AgentWorld proposes two agents and the connection between them,
> explains what it will create, and creates nothing yet. The user confirms. Two buildings
> appear on the island with a road between them. The user drags one to a better spot and
> opens it to read its instructions. They press Run. The researcher's building lights up,
> its status goes to `working`, the road carries the handoff, the writer runs, and the
> result is on screen — with every status change arriving over the WebSocket from a real
> folded event.

If that demo is unconvincing, week eight is the cheapest moment to change direction: the
domain model is written and almost nothing is built on top of it.

## Timeline

**24–30 weeks to the MVP at M5**, against 20–26 for the sequential plan the first revision
proposed. The four-week difference is accounted for line by line in
[04 §1](04-ROADMAP-AND-MVP.md) and buys a demonstrable product at week eight instead of
week twenty. The honest lever on the timeline is the scope table in
[04 §7](04-ROADMAP-AND-MVP.md), not the phase list.

## Three questions still open

Full form, with the cost of changing each later, in
[07 §4](07-DECISION-RECORD.md). None blocks starting Phase 2.

| | Question | Default |
|---|----------|---------|
| **O1** | First real integration: Slack or Gmail? | Slack — easiest OAuth, convincing gated action. Needed by Phase 6 |
| **O2** | MCP as the primary tool mechanism? | Yes — one client, every MCP server. Needed by Phase 6 |
| **O3** | Plain workspace polished before the island? | Yes — the Phase 4 prototype means this delays finishing the island, not seeing it. Needed by Phase 8 |

## The risk worth a second opinion

**R1, prompt injection choosing an allowed destination.** The outbound security work stops
an agent reaching an *internal* address. It does not stop a malicious document in a
knowledge source telling an agent to `POST` that knowledge to an attacker's *public* URL,
because that destination is legitimately allowed. Tool grants, `consequential`
classification and approvals are real but partial mitigations — `http_request` is classified
`write`, not `consequential`, so by default it is not gated, and classifying every outbound
request as consequential would make the product unusable.

**AgentWorld is not injection-proof and must not be described as such.** The accurate
claim is narrower and still worth making: *an agent can only do what it has been granted,
you can see what it has been granted, and you can find out afterwards what was in scope
when it acted.*

The mitigations are a **gate on Phase 6**, not a Phase 11 audit item: `http_request`
excluded from builder-proposed plans by default, an egress summary on every agent surface
derived from its grants, provenance on retrieved passages and `context_sources` on every
tool call, a review of the complete path from model output and retrieved content to tool
execution, and an adversarial test suite — including a case that asserts the exfiltration
**succeeds**, so the limitation is encoded in CI rather than remembered.

The actual fixes — per-tool-grant destination allow-lists, and an approval policy for tool
calls whose arguments trace to retrieved content — are explicit v1.1 candidates. What v1
does and does not protect against is enumerated in [07 §5](07-DECISION-RECORD.md).
