# Decision record

> Proposal, 2026-11-01, revision 3. Refinement item 6.

Four statuses, in increasing order of uncertainty:

| Status | Meaning | Section |
|--------|---------|---------|
| **Implemented** | In the repository, tested, committed. Not up for re-decision | §1 |
| **Approved, not implemented** | Agreed and specified; the roadmap builds it | §2 |
| **Deferred to v1.1 or later** | A real feature, consciously postponed, with its earliest release named | §3 |
| **Still open** | Needs a decision, with a recommended resolution and the phase that forces it | §4 |

Then §5: the architectural risks that remain unresolved, which is the part worth reading
twice.

A decision's status here is the single source of truth for it. Where a numbered decision
(`K*`, `C*`) appears in `00-GAP-ANALYSIS.md`, that document explains *why*; this one records
*where it stands*.

## 1. Implemented and in the repository

Shipped in Phases 0 and 1, verified, committed. Nothing in this table is up for
re-decision; the pivot does not touch any of it.

| # | Decision | Evidence |
|---|----------|----------|
| K1 | Two auth planes, separate principal *types* | `domain/principals.py`; 56-cell plane test |
| K2 | Supabase JWKS verification: local, fixed algorithm list, rotation cooldown | `infra/auth/supabase.py`; 28 tests incl. algorithm confusion |
| K3 | API keys: Argon2id + pepper, prefix lookup, unrecoverable, environment-labelled | `infra/hashing.py`, `domain/keys.py`; every text column searched for the plaintext |
| K4 | `authorize(principal, action, resource)` — one function, one table | `domain/policy.py`; 15 actions, matrix total over the enum |
| K5 | No membership ⇒ 404, not 403 | `test_tenant_isolation.py`, 31 cases |
| K6 | `TenantScope` required to construct a scoped repository | `repositories/base.py` |
| K7 | RLS on a non-superuser role, keyed on the visibility set, with the vacuity guard | migrations `0004`/`0005`; `test_rls.py`, the guard first |
| K8 | Append-only as a **privilege** | `audit_log` granted `SELECT, INSERT` only |
| K9 | Audit row in the same transaction as the mutation | `services/audit.py`; rollback test |
| K19 | RFC 9457 problem details, request id in logs, audit and `instance` | `api/errors.py` |
| K22 | Hand-written migrations plus a drift test | `test_schema.py`, `compare_metadata` |
| K23 | Frontend/backend independence | eslint boundary; `apps/web` imports nothing from `apps/api` |
| — | Phase 1 web app: Supabase sign-in, workspace switcher, projects, keys | 26 vitest tests; production build all-dynamic |

Totals as committed: **444 API tests, 26 web tests, 5 import-linter contracts, mypy strict
clean.**

### Corrections found while implementing, not while designing

Both were found by a test written against a claim this proposal makes, which is the
argument for writing those tests at all rather than reviewing for them.

| Found | What the design said | What was true | Fix |
|-------|---------------------|---------------|-----|
| Tenant scope was enforced per column, not per pair | `workspace_id` and `project_id` both identify the tenant | Two single-column foreign keys check existence, never agreement, and RLS reads `workspace_id` alone — so the application role could write a row naming its own workspace and **another tenant's project** | Migration 0012: `UNIQUE (workspace_id, id)` on `projects` and a composite key on all 16 tenant-scoped tables. A structural test covers tables not yet written |
| RLS was assumed to cover a partitioned table | the tenant policy protects `events` | A partition inherits neither privileges nor policies, so `SELECT * FROM events_y2026m11` returned **every tenant's** events | Migration 0011: the application role holds no privilege on any partition, asserted over every partition |

Neither leaked data — no release has shipped, and the first is a write channel rather
than a read. Both would have been cross-tenant defects in production, and both were
mistakes in this document's own reasoning rather than in its implementation.

Three tables are knowingly left outside the first fix: `events`, `logs` and
`metric_samples` carry no foreign key to `projects` at all, because they outlive what
they describe. On those the pair is enforced by ingest, their only writer, and a test
asserts the gap rather than leaving a reader to assume otherwise.

## 2. Approved for implementation

Agreed across the original brief and two refinements. These are what the roadmap builds.

### Core architecture

| # | Decision | Phase | Specified in |
|---|----------|-------|--------------|
| C1 | The protocol gains `workflow.*` and `approval.*`; envelope gets `subject` + `correlation` | 2 | 01 §9 |
| C2 | Configuration changes are **not** events — audited and versioned instead | 2 | 01 §1 |
| C7 | A pure `advance(definition, state) -> Decision[]` orchestrator | 3 | 02 §4 |
| — | **Transactional outbox:** event + state + dispatch in one transaction; dispatch never replayed | 3 | 02 §2 |
| — | Protocol promise 7: folding the log causes no work | 2 | 01 §9 |
| K10–K14 | Event sourcing, replay equivalence, `dedup_key` idempotency, client `occurred_at`, forward-compatible unknowns, caller-chosen `agent_key` | 2 | 01 §9 |
| C5 | Tasks become the interface to a dispatchable agent, not the top-level concept | 2 | 00 §4 |
| C6 | 16 new tables for workflows, connections, tools, triggers, knowledge, approvals, policies, credentials | 2–6 | 01 |
| — | Workflow definitions **versioned**; executions pinned to a version | 5 | 01 §3 |
| — | Workflow **edges** and ecosystem **connections** are separate concepts | 2 | 01 §3 |

### Agents and provider independence

| # | Decision | Phase | Specified in |
|---|----------|-------|--------------|
| C3 | `interaction_mode ∈ {managed, dispatchable, observed}` | 2 | 01 §2 |
| C4 | Real control for `managed`; capability-gated commands for `dispatchable`; none for `observed` | 3, 7 | 00 §4 |
| **C15** | **Four independent axes: model provider, agent runtime, tool provider, communication protocol** | 2–3 | 01 §2 |
| — | `model_profile_id` only valid for `managed`, by check constraint | 2 | 01 §2 |
| K15 | Provider SDKs confined to `infra/providers/`, by import-linter contract | 3 | 03 §2 |
| K16 | Adapters translate payloads and never touch persistence | 7 | 00 §3 |
| — | MCP as the primary tool mechanism | 6 | 01 §4 |

### Security

| # | Decision | Phase | Specified in |
|---|----------|-------|--------------|
| C8 | `credentials` under envelope encryption, separate from the key hasher | 3 | 01 §6 |
| — | AAD binds ciphertext to `workspace_id` + `credential_id` | 3 | 01 §6 |
| — | KEK rotation re-wraps DEKs with no plaintext access | 3 | 01 §6 |
| — | Four redaction channels; the canary test | 6 | 01 §6 |
| — | Revocation fails the affected step loudly; deletion leaves a tombstone | 6 | 01 §6 |
| C14 | A 72-action permission matrix with nine actions that deliberately do not exist | 2 onward | 05 |
| **C16** | **Configurable approval policies; consequential work needs a second person by default** | 6 | 01 §7 |
| — | Approval requirements frozen at creation; enforced in three independent layers | 6 | 01 §7, 02 §7 |
| — | **Immutable execution initiator**; a system-initiated run borrows no user's authority | 2 | 01 §7 |
| — | `SystemPrincipal` as the third principal type, refused every control-plane action | 2 | 01 §7 |
| — | Authorization checked independently of, and before, approval policy | 6 | 01 §7 |

**Withdrawn from revision 3:** attributing a scheduled execution to whoever last enabled
its trigger. It would have borrowed a user's authority through a mutable row and let it go
stale after a demotion or a departure, and it silently disqualified that user from
approving. Replaced by the immutable initiator above. The residual it leaves — an admin who
can enable a trigger can schedule a workflow and then approve its steps — is bounded,
documented in `01-DOMAIN-AND-PROTOCOL.md` §7, and has a v1.1 candidate.
| **C17** | **Resolve-validate-pin outbound HTTP; no redirects by default; egress policy at the environment** | 3, 7 | 06 |
| — | SSRF: both IP families, IPv4-mapped and NAT64, metadata names, normalised errors | 3 | 06 §3 |

### Execution and reliability

| # | Decision | Phase | Specified in |
|---|----------|-------|--------------|
| — | Dispatch state machine with `dead` and `needs_review` | 5 | 02 §3 |
| — | Error classification: terminal errors are not retried | 5 | 02 §8 |
| — | Dead letters always resolve their step; the no-stuck-executions invariant | 5 | 02 §8 |
| — | `needs_review` for ambiguous consequential work, never auto-retried | 5 | 02 §8 |
| — | Lease-based crash recovery, three idempotency layers | 3 | 02 §3 |
| **C18** | **A `KnowledgeRetriever` Protocol; full-text in v1, vector later with no contract change** | 6 | 01 §5 |

### Visual and conversational

| # | Decision | Phase | Specified in |
|---|----------|-------|--------------|
| K17 | The island is a projection; `project()` pure and Phaser-free | 3 | 03 §5 |
| C9 | Two projections plus a pure `interpret(gesture) -> Intent[]` | 3 | 03 §5 |
| C12 | Visual state in four tables of its own, holding no execution state | 2 | 01 §10 |
| — | A district is a visual region inside a project, not a second tenancy boundary | 2 | 01 §10 |
| C13 | Early island prototype (I1, I2) and early builder prototype, both on final architecture | 3–4 | 03 §5, §7.1 |
| — | Island legibility: six questions answerable with no click, tested on five strangers | 4, 9 | 03 §5 |
| C10 | An object-management workspace, usable entirely without the island | 8 | 03 §6 |
| — | Simple/Advanced on every form; a copy lint on the Simple-mode vocabulary | 8 | 03 §6 |
| — | Builder: propose then apply, one transaction, nothing published or enabled | 4 | 03 §7 |
| — | A "team" compiles to a real `workflow_version` — one execution engine, always | 4 | 03 §7.1 |
| C11 | Three parallel tracks; demonstrable product at M1, about week eight | — | 04 §1 |

### Decided rather than asked

| Decision | Rationale |
|----------|-----------|
| Anthropic in Phase 3, OpenAI-compatible as that phase's **last task** | The second implementation is the only real test that the Protocol is not vendor-shaped, and it is cheapest while the first is fresh |
| PostgreSQL full-text for v1 retrieval | Three fewer moving parts for a gain nobody can measure at first-user volumes — and now behind `KnowledgeRetriever`, so the swap is contract-free |
| `approval_policy.update` is `admin` | AgentWorld has no per-project owner role; `admin` is the nearest to the brief's "project owners". Per-project roles are deferred |

## 3. Deferred until after v1

Each is a real feature, listed so it is a decision rather than an oversight. Full table with
reasons in `04-ROADMAP-AND-MVP.md` §9.

| Deferred | Earliest |
|----------|---------|
| Loops and map-over-collection in workflows | v1.1 |
| Sub-workflows | v1.1 |
| Approval groups, quorum, escalation chains, delegation, approval-of-approval | v1.1 |
| Per-project roles (a real "project owner") | v1.1 |
| Per-service integration adapters: Gmail, Drive, Notion, GitHub, Discord | v1.1, demand-ordered |
| pgvector and hybrid retrieval | v1.1 |
| Cost and token analytics | v1.1 |
| Pending invitations for users who have not signed up | v1.1 |
| Conversational debugging of a running ecosystem | v1.2 |
| Templates and sharing between workspaces | v1.2 |
| Multi-project island | v1.2 |
| OpenTelemetry trace import | v1.2 |
| Alerting rules and notifications | v1.2 |
| Mobile-native app | v2 |
| SSO/SAML, SCIM | on demand |
| Self-serve billing | pre-GA |
| Agent-to-agent negotiation beyond `delegate` | v2 |

**Withdrawn from the original deferred list:** agent-to-agent collaboration and workflow
execution. They were v2 on the grounds that they change the protocol from observational to
transactional. That was correct, and it is now the product.

## 4. Open questions

Three. The earlier list's self-approval item is decided (C16), and the island-ordering item
is largely answered by the Phase 4 prototype.

| # | Decision | Recommended resolution | Needed by | Cost of changing later |
|---|----------|----------------------|-----------|----------------------|
| **O1** | First real integration: Slack or Gmail? | **Slack.** The least painful OAuth, and "post to a channel" is a convincing consequential action to gate. Gmail is the better demo and a much worse first OAuth — restricted scopes mean app review, which is calendar time you cannot compress | **Start of Phase 6** | Low. A second integration is 3-5 days once the first OAuth flow exists |
| **O2** | MCP as the primary tool mechanism? | **Yes.** One client turns every MCP server into tools with no AgentWorld code per service. It does bet on an ecosystem outside our control | **Start of Phase 6** | Medium. Reversing it means an adapter per service, which is the work that does not finish |
| **O3** | Plain workspace polished before the island? | **Yes** - Phase 8 then Phase 9, so the product is proved usable without the island. The Phase 4 prototype means this delays *finishing* the island, not seeing it | **Start of Phase 8** | Low. The two phases share no files and can be swapped |

**None blocks Phase 2**, which is why implementation can begin now. Each is listed against
the phase that forces it, so none has to be decided earlier than it must be.

## 5. Unresolved architectural risks

The honest list: things the design does not solve, only bounds. Each names what would make
it worse and what would have to change to actually fix it.

### R1 — Prompt injection choosing an allowed destination

**The gap.** `06-OUTBOUND-SECURITY.md` stops an agent reaching an *internal* address. It
does not stop a malicious document in a knowledge source telling an agent to `POST` that
knowledge to `https://attacker.test/?data=…`, because that destination is legitimately
allowed. Tool grants, `consequential` classification and approvals are the mitigations, and
they are real but partial: `http_request` is classified `write`, not `consequential`, so by
default it is not gated — and classifying every outbound request as consequential would
make the product unusable.

**What makes it worse:** granting `http_request` and a knowledge source to the same agent,
which is a combination the builder could propose innocently.

**What v1 does protect against**, specifically:

- An agent reaching an internal, loopback, link-local, reserved or metadata destination —
  `06-OUTBOUND-SECURITY.md`, with an automated suite.
- An agent calling a tool it was not granted — checked at call time, so a revocation takes
  effect mid-run.
- A `consequential` tool used without an approval where policy requires one — and the gate
  is the absence of a dispatch, not a flag.
- The capability being invisible: the egress summary on every agent surface says, in plain
  words, that an agent can send data out and where.
- The event being untraceable afterwards: `Provenance` on retrieved passages and
  `context_sources` on `tool.invoked` make "which document was in scope when this agent
  posted our customer list" a query rather than an investigation.
- The builder quietly granting it: `http_request` is excluded from builder-proposed plans
  unless the user asked for web access in their own words.

**What v1 does not protect against:**

- An agent that has legitimately been granted `http_request` sending data it can read to
  any allowed public destination, at the instruction of retrieved content. The destination
  is permitted, the tool is granted, the request is honest. **Nothing in v1 blocks this.**
- `http_request` is classified `write`, not `consequential`, so it is not gated by default.
  Classifying every outbound request as consequential would mean an approval prompt per web
  fetch, which no user would tolerate and which would train them to click through the ones
  that matter.

**AgentWorld is not injection-proof and must not be described as such** — not in the
product, the documentation or any marketing. The accurate claim is narrower and still worth
making: *an agent can only do what it has been granted, you can see what it has been
granted, and you can find out afterwards what was in scope when it acted.*

**What would fix it**, both explicit v1.1 candidates: per-tool-grant destination
allow-lists, and an approval policy for tool calls whose arguments trace to retrieved
content — which `Provenance` and `context_sources` make possible but do not implement.

**Brought forward to Phase 6** from Phase 11: the adversarial test suite — including a case
asserting the exfiltration **succeeds**, so the limitation is encoded in CI rather than
remembered — and the review of the complete path from model output and retrieved content to
tool execution. Phase 11 keeps a second review against the full tool catalogue.

**Severity: high.** Still the risk I would most want a second opinion on before launch.

### R2 — At-least-once delivery against receivers that do not dedupe

**The gap.** `needs_review` escalates ambiguous consequential work to a human instead of
guessing, which is right. But the human is being asked a question they often cannot answer
either — *did the Slack message send?* — and Slack offers no idempotency key to settle it.

**What makes it worse:** a flaky network between AgentWorld and a popular integration,
producing `needs_review` often enough that users start clicking "retry" reflexively, which
is exactly the duplicate-send the status exists to prevent.

**What would fix it:** nothing general. Per-integration reconciliation — "search the channel
for a message matching this idempotency token before retrying" — is possible for some APIs
and not others, and it is per-integration work. v1 ships the escalation and the honest
message.

**Severity: medium**, and it rises with usage.

### R3 — The orchestrator is a single writer per execution

**The gap.** `advance()` holds `FOR UPDATE` on the execution row for the whole
decide-and-apply transaction. That is correct and simple, and it means one execution's
throughput is bounded by one transaction at a time. A workflow fanning out to 200 parallel
steps decides them in one call, which is fine; a workflow whose steps complete at 100/second
serialises on that row.

**What makes it worse:** long `advance()` durations, which is why decision latency has a
p95 alert at 250ms.

**What would fix it:** partitioning the decision by sub-graph, or an actor per execution.
Both are substantial, neither is needed at v1 scale, and the measurement that would justify
one arrives in Phase 11's load test.

**Severity: low for v1**, structural if it is ever wrong.

### R4 — The expression language will be asked to grow

**The gap.** `condition` and `input_mapping` use a deliberately small evaluator: references,
comparison, boolean operators, a documented function list, no calls beyond it. The first
real user will want something it cannot express — a date comparison, a regex, a string
split. Each addition is a step toward a sandbox, and a sandbox in a multi-tenant product is
a vulnerability with a release schedule.

**What makes it worse:** adding functions reactively, one support request at a time, without
re-fuzzing.

**What would fix it:** deciding now that the answer to "the expression cannot do X" is a
`transform` step calling a **tool**, which runs in the ordinary sandboxed, gated, audited
path — not a richer expression language. That is the policy; the risk is that it is
inconvenient enough to erode.

**Severity: medium**, and it is a discipline risk rather than a technical one.

### R5 — Two views of one model drifting apart

**The gap.** The island and the plain workspace are two presentations of the same domain.
Every new object kind needs both. The plain pages are where the work gets done, so the
island will lag, and a lagging island quietly stops being trustworthy — the failure mode
the legibility test exists to catch, but that test runs at Phase 4 and Phase 9, not
continuously.

**What makes it worse:** adding an object kind without a `building_kind`, so it exists in
the product and not on the island.

**What would fix it:** a test asserting every `node_kind` has a projector rule and every
domain object kind has a `node_kind` — the same coverage-guard shape as
`test_the_matrix_covers_every_mutating_route`. Worth adding in Phase 4 and cheap; noted here
because it is not yet in the acceptance criteria.

**Severity: low individually, high cumulatively.**

### R6 — The builder's blast radius grows with its capabilities

**The gap.** The Phase 4 prototype can create agents and connections, and its caps and
permission bounds are sized for that. Phase 10 adds workflows, triggers, integrations,
knowledge and grants. The same object cap and rate limit then govern a much more
consequential set of writes, and `requires_consent` is the main brake.

**What makes it worse:** a user accepting a consent prompt they did not read, which is what
users do.

**What would fix it:** a dry-run cost and permission summary the user sees *before* the
consent prompt, and a "nothing consequential in this plan" badge so the exceptional case
stands out instead of every plan looking alike. Not in the current Phase 10 scope; a
candidate for it.

**Severity: medium.**

### What is *not* on this list, and why

- **Provider lock-in** — closed by C15 and two import-linter contracts, each verified by
  planting a violation.
- **Replay causing side effects** — closed by the outbox and asserted by protocol promise 7.
- **The island becoming the source of truth** — closed by `interpret()` being pure and
  `world/dispatch/` being the only layer that may fetch.
- **Tenant isolation** — two independent layers, built and tested, with a vacuity guard that
  runs first.
- **Secrets in logs or events** — four channels and a canary test across every table, log,
  event, frame and metric label.

Those were risks in revision 1. They are now mechanisms, which is the distinction this
document exists to make.
