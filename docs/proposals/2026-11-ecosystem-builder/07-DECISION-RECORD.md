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

> **This table has not been extended past Phase 1.** Phases 2 and 3 (T0-T6b) are committed
> and their status lives in `04-ROADMAP-AND-MVP.md`, per-task, with a document each
> (`09`-`17`). The totals above are therefore historical: the suite is over 2,300 API tests
> and 8 contracts as of T6b. Bringing §1 up to date is a documentation task of its own and
> is deliberately not done inside a feature change.

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
| **C19** | **A model turn runs against either a workspace provider credential or a user-authorized personal runtime; a runtime is not assumed to be a conventional API** | 4 (seam), 7 (runtime) | 04 §2 |
| — | MCP as the primary tool mechanism | 6 | 01 §4 |

**C19, in a sentence.** C15 says the model provider and the agent runtime are
independent axes. C19 says what that must mean for *authorization and reachability*: some
subscriptions cannot be used through an API at all, and some runtimes are not reachable
from a server. So `ModelProvider.complete()` stays the contract — it names no transport
and no credential — while construction and call direction become variable, and
`ModelProfile.credential_id` stays nullable rather than gaining a NOT NULL it would later
have to lose. Phase 4 owes this nothing but restraint; Phase 7 implements it on
`agent_task_offer`. Deciding it late means migrating a populated table and editing the
single code path every execution runs through.

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

Four. The earlier list's self-approval item is decided (C16), and the island-ordering item
is largely answered by the Phase 4 prototype.

| # | Decision | Recommended resolution | Needed by | Cost of changing later |
|---|----------|----------------------|-----------|----------------------|
| **O1** | First real integration: Slack or Gmail? | **Slack.** The least painful OAuth, and "post to a channel" is a convincing consequential action to gate. Gmail is the better demo and a much worse first OAuth — restricted scopes mean app review, which is calendar time you cannot compress | **Start of Phase 6** | Low. A second integration is 3-5 days once the first OAuth flow exists |
| **O2** | MCP as the primary tool mechanism? | **Yes.** One client turns every MCP server into tools with no AgentWorld code per service. It does bet on an ecosystem outside our control | **Start of Phase 6** | Medium. Reversing it means an adapter per service, which is the work that does not finish |
| **O3** | Plain workspace polished before the island? | **Yes** - Phase 8 then Phase 9, so the product is proved usable without the island. The Phase 4 prototype means this delays *finishing* the island, not seeing it | **Start of Phase 8** | Low. The two phases share no files and can be swapped |
| **O4** | First user-authorized personal runtime, and the entitlement policy it runs under | **Undecided, deliberately.** The architecture is held open by C19 from Phase 4; the choice depends on which runtimes expose an authorization flow usable by a server-side orchestrator, and on whether their terms permit automated use on a user's behalf. Needs a written policy on attribution — whose authorization a turn ran under — before any runtime ships | **Start of Phase 7** | Low for the abstraction, which C19 keeps open; high for the *choice*, since each runtime is its own authorization flow and failure mode |

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

---

## 6. Phase 3 ledger reconciliation (2026-11-20)

Thirteen decisions. P1-P8 were taken after a read-only audit found that Phase 3's scope sentence
(`04-ROADMAP-AND-MVP.md` §2, "this phase is T0–T8 and T13"), its build list, its
acceptance ledger and the T-criteria in `08-PHASE-3-READINESS.md` did not agree with each
other or with the code. **Approved by the product owner on 2026-11-20.** No criterion was
deleted and no phase boundary moved except where a row below says so. **P9 was approved
later the same day**, from the realtime-spine audit that followed: it is the one
architecture decision R8 and R10 could not be implemented without, and it is detailed
after the table. **P10 followed from the audit of P9's implementation feasibility**: five
decisions that audit identified and deliberately did not take, three of them protocol
surface. It is detailed after P9. **P11 followed from the M2 implementation audit**, which
found that P10.1 had named the `definition.*` namespace and the obligation to register its
types without ever naming them - so the first task that needed them could not start.
**P12 followed from the M3 implementation audit**, which found that P11 had settled every
field of a definition event except one the database requires and no decision had named:
`dedup_key`. **Approved by the product owner on 2026-11-25.**
**P13 followed from the audit of the remaining definition writers**, which found that
P11's inventory of "sixteen write operations across four services" was complete for
`services/definitions.py` and not complete for the codebase: publishing a workflow moves
a client-visible column from `services/executions.py`, and no approved producer covered
it. **Approved by the product owner on 2026-11-25.**

| # | Decision | Why | Where it landed |
|---|----------|-----|-----------------|
| **P1** | A human `retry` keeps the **same** dispatch and the **same** `idempotency_token`; the criterion was amended, not the code | The state being resolved is uncertain - the request went out and nothing came back. A new token would convert a *possible* duplicate into a certain one at exactly the receivers capable of suppressing it. The admin's written acknowledgement is the authority for repeating the effect; the stable token is the only mitigation left. An operator certain nothing happened resolves `failed`, not `retry` | `08-…` T8, criterion 8, with both tests named |
| **P2** | **T7 splits into T7a and T7b.** T7a (turn loop, `max_turns`, wall clock, metrics) stays in Phase 3; T7b (grants at call time, `tool.denied`, redaction) moves to Phase 3a with T10 | Three of T7's six criteria need a tool call to exist, and a `tool_call` step cannot be published until a `tools` table does - `workflow_steps.tool_call_needs_tool` requires a `tool_id`. The alternative was marking them "not applicable until T10", which is how a deferred criterion stops being read | `08-…` T7a / T7b; build bullet in `04-…` Phase 3 |
| **P3** | **The four island acceptance boxes stay in Phase 3** | Moving I1 to Phase 4 would close this phase sooner and concentrate both island milestones into one - the opposite of why I1 was pulled early - and the `world/phaser/` box is the G8 gate | `04-…` Phase 3 acceptance, island group |
| **P4** | The realtime acceptance run uses the **`redis`** bus; `inprocess` stays for local dev | R13 - two gateway processes seeing identical frames - is the criterion that proves the bus is a bus, and `inprocess` cannot satisfy it. Both already exist as `EventBusKind` | `04-…` R13 and the event-bus build bullet |
| **P5** | The **step-timeout** criterion moves from T8 to **T11** | For a managed step the guarded client's 30-second total timeout and the lease already bound it; the unbounded case is a dispatchable agent that never reports, which is T11's. `workflow_steps.timeout_seconds` therefore ships unread in Phase 3, and the ledger says so | `08-…` T8 criterion 9 (struck through) and T11 |
| **P6** | `on_failure` implements **`fail` only** in Phase 3; `continue` and `route` move to **Phase 5** | `route` needs branch targets, which arrive with Phase 5's branches and joins. There is nowhere to route to before then | `08-…` T8 criterion 5; `04-…` T8 ledger box |
| **P7** | Two T8 criteria **amended rather than implemented**: backoff "asserted statistically" becomes asserted by bounds over the window; "a consequential dispatch defaults to `max_attempts = 1`" becomes "no dispatch can carry consequential work in this release" | Bounds are deterministic and strictly stronger than a sampled distribution, and are what `tests/unit/test_backoff.py` already asserts. `advance()` emits no consequential decision, so the second criterion presumed something that cannot exist until approvals land; the default moves to Phase 3a with them | `08-…` T8 criteria 3 and 4 |
| **P8** | **No Phase 3 criterion carries into Phase 4** | Four of Phase 4's own acceptance criteria depend on live deltas, so carrying the spine forward would make Phase 4 unclosable as well. Phase 4 begins when Phase 3's ledger is closed | `docs/STATUS.md` §6 |
| **P9** | **The shared per-project `events.seq` is the single cursor authority for both execution and definition deltas**, and a definition change must be recorded durably in the same transaction as the mutation it describes, with publication only after that transaction commits | One ordered cursor domain is what makes `resume` mean the same thing for both kinds of delta; two sequences would put two orderings into a protocol that has one `cursor` field. Durability is the other half: publishing after commit with nothing persisted is at-most-once, and a crash between the commit and the publish loses a delta silently - which `resume` would then never replay, because nothing recorded it. A separate definition cursor is **not** part of the approved design, and carrying the last-seen execution cursor without persisting the change is **not** acceptable, because neither can satisfy exact definition replay | `03-…` §4; `04-…` R8 and R10; `08-…` §5.1 rule 4 |
| **P10** | **The five implementation decisions P9 left open**, taken on the P9 audit's recommendation of Option A - the committed `events` row is itself the durable publication record. In outline: a non-folding `definition.*` namespace, registered and `CONTROL_PLANE`; an explicit tombstone for deletion; definition events kept out of the raw activity feed; database-backed per-project publisher progress; and the existing worker as the publisher's host | Each one is narrow, and three of them are protocol surface rather than code style - the namespace, the tombstone and the feed boundary all change what a client sees, so they are recorded here rather than settled in review. Option A needs no new table for the payload: the event row is already durable, already per-project, and already ordered by the cursor the protocol carries | `03-…` §4; `04-…` R7, R8, R10, R11, R13; `08-…` §5.1 rule 4 |
| **P11** | **The eleven persisted definition event types, and the conventions they follow.** `definition.agent.{created,updated,archived,restored}`, `definition.workflow.{created,updated,archived,restored}` and `definition.connection.{created,updated,deleted}` - each registered with `producer=Producer.CONTROL_PLANE` and `folds_into=()`, subject `SubjectKind.PROJECT`, entity type and uuid in the payload. Model profiles are out of scope for Phase 3 | P10.1 named the namespace and the obligation to register, and the M2 audit found it had never named the types - so M2 could not begin without either inventing protocol surface or stopping. These eleven are what the sixteen definition writes actually produce: `AgentService` and `WorkflowService` have no `delete`, so a deletion type exists for connections alone. And `SubjectKind` has no member for a workflow definition or a connection, while `SubjectKind.AGENT`'s `subject_ref` is the `agent_key` rather than a uuid - so `PROJECT` plus a payload id is what satisfies P10.2 without redefining a convention the reducers already depend on | `03-…` §4; `04-…` R7 and R8; `08-…` §5.1 rule 4 |
| **P12** | **A definition event's `dedup_key` carries the allocated `seq`**: `def:{entity}:{entity_id}:{operation}:{seq}`. An operation that changes nothing appends no event, and a definition event's resolved reference columns are left NULL | `dedup_key` is `NOT NULL` and uniquely indexed per project per monthly partition, so M3 could not write a row without a convention, and P11 had named none. The obvious one - the orchestrator's `exec:{id}:started` shape, without the `seq` - is wrong twice over for an event that recurs: it collides on the second edit of the same entity in a month, turning every later edit into a 409 whose advice ("try again") can never come true; and because the index is on `dedup_key` alone rather than `(type, dedup_key)`, and `append()` never consults `seen()`, a predictable key is **pre-claimable by any agent ingest key** - a heartbeat carrying `def:agent:<uuid>:updated` would block the control plane from ever editing that agent again. `seq` is allocated under the cursor lock before the append, so it is unique per project by construction and unguessable in advance | `04-…` R8; `08-…` §5.1 rule 4 |
| **P13** | **`ExecutionService._publish` is an approved producer of the existing `definition.workflow.updated`**, and the test-harness writer in `services/agent_test.py` is excluded. Publishing a workflow is **one** logical operation emitting **one** event, whatever it writes internally. No new event type: the eleven stand | `WorkflowRepository.publish` moves `workflows.current_version_id` - its own docstring says publishing *is* moving that pointer - and that column is in the API view, populated on the workflow response and deciding each version's `is_current`. So publishing changes the definition a client holds, while `_publish` appended nothing and wrote only an audit row: a client's workflow detail would go stale and `resume` would never replay the change, because nothing recorded it. That is the hole R8 exists to close, and closing it needs no protocol surface - `definition.workflow.updated` already exists and `workflow.updated` is already the frame a client needs in order to refetch. The harness writer is excluded because `WorkflowRepository.list_all` filters `test_for_agent_id IS NULL`, so a harness is not a member of the listing that snapshot and delta keep in sync | `04-…` R8; `08-…` §5.1 rule 4 |

**What these decisions did not change.** The scope sentence, the phase boundaries other
than P2's and P5's single-criterion moves, and every acceptance criterion's substance
except P1's and P7's two, each of which carries its reasoning on the line itself.

**What they revealed, which is the part worth remembering.** The event bus and the
WebSocket gateway had been a build bullet since the phase was written and had **no
acceptance criteria at all** - so the largest open item in Phase 3 was also the only one
that could not be ticked, and nothing in the ledger would ever have failed because of it.
T13 had no box either. R1-R14 and the T7a/T8/T13 boxes exist because of that, and the
lesson is the same one T6d recorded: *"the code exists" and "something runs it" are
different claims, and the acceptance criteria are where the difference shows up* - to
which this adds a third, that a criterion nobody wrote cannot fail.

### P9 in full — the definition-delta cursor

**Approved, not implemented.** Nothing in the repository allocates a cursor for a
definition change, records one durably, or publishes one. `infra/event_bus.py` does not
exist, `api/ws/__init__.py` is a one-line docstring, and the `redis` dependency is
declared in `pyproject.toml` and never imported. This decision fixes the design that R8
and R10 will be measured against; **both criteria stay open** until an implementation and
its acceptance evidence exist. A settled design is not evidence, and neither is approval.

**The mechanism is open, within one constraint.** A lightweight transactional outbox - a
row written in the same transaction as the mutation, claimed and published after commit -
is an allowed implementation, and the obvious one, since `services/executions.py` already
proves the pattern for dispatches. It is **not** a required table design and this decision
creates nothing. What is required is the property: the delta is durable before it is
published, and published only after the transaction that produced it has committed.

**The accepted cost, stated plainly.** Allocating from `ProjectEventCursor` means taking
the same per-project cursor lock `repositories/events.py` takes for ingest, held
`FOR UPDATE` until the transaction commits. **Configuration writes will therefore contend
with event ingestion for a project and may serialise behind a batch.** That is accepted
deliberately, in exchange for one ordered, replayable cursor domain. Two things make the
trade cheaper than it reads: configuration writes are rare beside ingest, and that same
lock is what already makes a project's `seq` gapless and commit-ordered - which is the
property `resume` depends on in the first place.

**What was rejected, and why.** A second per-project sequence for definitions would be
replayable without touching the ingest lock, but it puts two orderings into a protocol
with a single `cursor` field. Carrying the last-seen execution cursor on a definition
delta is cheapest and cannot replay at all, because nothing would persist the change the
cursor claims to point at.

### P10 in full — the five implementation decisions

**Approved architecture. None of it is implemented.** No `definition.*` type is
registered, nothing allocates a cursor for a definition change, no publisher exists,
`infra/event_bus.py` does not exist, and the declared `redis` dependency is still never
imported. R8 and R10 stay open, and nothing below ticks a box.

**P10.1 — the persisted namespace is `definition.*`, registered, and
`CONTROL_PLANE`.** Every persisted definition-event type is declared in the taxonomy with
`producer=Producer.CONTROL_PLANE`, and the ingest service must refuse it as externally
ingestible.

Two findings from the audit make this load-bearing rather than tidy. First, `fold()`
dispatches on the type's **namespace** - `domain/reducers/fold.py` partitions on the first
`.` and looks the prefix up in `_REDUCERS`, which holds `agent`, `task`, `run`, `tool`,
`workflow` and `approval`. The documented *frame* names are `agent.updated` and
`workflow.updated`, and both of those namespaces fold. A persisted event reusing a frame
name would therefore be handed to a reducer, and replay would begin re-creating
definitions - which is exactly what `_agent`'s own docstring says must never happen. A
`definition.*` namespace misses `_REDUCERS` and folds to nothing, structurally. Second,
`services/ingest.py` reads the producer from the type's spec and **defaults an unregistered
type to `Producer.AGENT`, which is ingestible** - so registration is the security control.
An unregistered `definition.*` type could be forged by any ingest key.

**P10.2 — deletion is an explicit tombstone.** The frame carries project identity, entity
type, entity id and the shared sequence, and nothing else. No empty or half-populated
entity is fabricated, and the persisted payload stays identity-and-operation. This is why
R7 ("deltas are whole entities") needed a deletion carve-out: a tombstone is deliberately
not an entity, and the alternative - an entity document with its fields blanked - is
indistinguishable from a real update that cleared them.

**P10.3 — definition events stay out of the raw activity feed.** The frame set has a
`{ "type": "event" }` channel for the activity feed and island animation intents. Under
Option A, definition events are rows in the same `events` table, so they would reach that
channel unless the channel excludes them. It must. The reason is sensitivity, not tidiness:
`instructions`, `labels`, `model_overrides` and `dispatch` are free-form and may hold an
endpoint or a token, which is why the agent detail page withholds all four. Keeping the
persisted payload to identity and operation is the other half of the same protection - the
whole entity reaches an authorized consumer through the snapshot and delta path, read from
the definition table at publish time, and never through activity history.

**P10.4 — publisher progress is database-backed, per project.** Redis is the transport and
never the source of truth for how far publication has got. Recovery reads committed
`events` rows from the recorded position, so a publisher crash or a Redis outage loses
nothing, and ordering stays the shared `events.seq`. This introduces **no second public
cursor**: progress is server-side bookkeeping, not a number any client sees.

**P10.5 — the existing worker hosts the publisher, if it can.** `workers/worker.py`
already has what this needs: an `asyncio.Event` stop flag with signal handlers, an
interruptible idle sleep, and a consecutive-error bound. Publication must not run on a
request-scoped session - `Database` is built with `pool_size=5` and `max_overflow` at twice
that, so a per-connection session would exhaust the pool at the sixteenth viewer, and the
RLS context is transaction-local in any case.

**This one is conditional on purpose.** If the worker's lifecycle turns out not to support
reliable publication - its give-up bound stops the loop after consecutive failures, which
is right for dispatches and may be wrong for a publisher that must keep trying through a
Redis outage - then the concrete limitation is to be written down and the hosting left
**explicitly unresolved**, rather than a third process being invented to avoid saying so.

### What P10 does not claim

Four audit findings survive this decision unchanged, and none of them is closed by it:

- **The reverse lock order between ingestion and orchestration is pre-existing and still
  open.** Ingest takes the project cursor lock and then writes entity rows; the
  orchestrator takes the execution row lock and allocates the cursor lazily at append
  time. That is a deadlock window between an agent reporting a step and `advance_once` on
  the same execution. P9 and P10 do not solve it. What P10 does require is that a
  definition mutation take the cursor lock **first**, matching ingest, so definition writes
  do not join the reverse-order set.
- **Nothing prunes event partitions**, and there is no operational replay-window policy, so
  "beyond the replay window" has no definition today. **R11 stays open.**
- **Redis is in the Compose stack, but the `api` CI job starts Postgres only.** The
  Redis-dependent acceptance tests cannot run there until that job starts it.
- **Publication is at-least-once, and exactly-once is not claimed.** A duplicate publish is
  expected after a crash between publishing and recording progress. Clients stay correct
  because every frame carries a monotonic `seq`: one that has applied `seq = N` discards
  anything at or below it, so a duplicate produces no duplicate logical update while
  ordering stays exact. That is a property the acceptance tests have to assert, not an
  assumption.

### P11 in full — the definition event types

**Approved, not implemented. M2 is not complete.** No type below is registered, nothing
emits one, and R8 and R10 stay open. What P11 settles is the protocol surface M2 needs in
order to be a mechanical change rather than an architectural one.

**1. The eleven types, and the operations that produce them.** Taken from the definition
services as they stand, not invented: `services/definitions.py` holds sixteen write
operations across four services.

| Entity | Types | Produced by |
|---|---|---|
| agent | `definition.agent.created` · `.updated` · `.archived` · `.restored` | `AgentService.create` · `update` · `archive` · `restore` |
| workflow | `definition.workflow.created` · `.updated` · `.archived` · `.restored` | `WorkflowService.create` and `compile_delegation` · `update` and `save_draft` · `archive` · `restore` |
| connection | `definition.connection.created` · `.updated` · `.deleted` | `ConnectionService.create` · `update` · `delete` |

**There is deliberately no `definition.agent.deleted` or `definition.workflow.deleted`.**
Neither service has a `delete`: an agent and a workflow are archived and restored, and the
rows survive. `ConnectionService.delete` is the only hard delete among the three entities
in scope.

**2. Producer and folding.** Every one of the eleven is registered explicitly with
`producer=Producer.CONTROL_PLANE` and `folds_into=()` - recorded and folding into nothing.
None of them enters the `agent`, `workflow`, `task`, `run`, `tool` or `approval` reducers,
because `fold()` dispatches on the namespace and `definition` is not a key in `_REDUCERS`.
**No reducer changes, and no new reducer**: the M2 audit established that the separation is
structural and needs no code.

**The unknown-event fallback is untouched.** An unregistered type still resolves to no
spec, still defaults to `Producer.AGENT`, and is still accepted and stored and folded into
nothing. That is the first compatibility promise and P11 does not narrow it;
`test_an_unknown_type_is_still_accepted` keeps pinning it.

**3. The subject convention: `SubjectKind.PROJECT`, with entity type and uuid in the
payload.** Two reasons, both from the existing protocol rather than from preference.
`SubjectKind` has eight members - `AGENT`, `TASK`, `RUN`, `WORKFLOW_EXECUTION`,
`EXECUTION_STEP`, `APPROVAL`, `TOOL_CALL`, `PROJECT` - and **none of them names a workflow
definition, a connection or a model profile**; `WORKFLOW_EXECUTION` is an execution.
Adding members would be protocol surface of its own. And `SubjectKind.AGENT`'s
`subject_ref` is the **`agent_key`** by a convention the reducers read directly, so using
it would put a key where P10.2 asks for an entity **ID**. `PROJECT` as the subject with the
type and uuid in the payload satisfies P10.2 and **does not redefine
`SubjectKind.AGENT.subject_ref`**, which keeps its existing meaning.

**4. Operations, frames, and what archive means.**

| Persisted | Client frame |
|---|---|
| `definition.agent.created` · `.updated` · `.archived` · `.restored` | `agent.updated` |
| `definition.workflow.created` · `.updated` · `.archived` · `.restored` | `workflow.updated` |
| `definition.connection.created` · `.updated` | `connection.updated` |
| `definition.connection.deleted` | the tombstone of P10.2 |

**Archive is not deletion.** An archived agent is still readable by id - the detail
endpoint asks for archived rows on purpose, and the page renders one with its archive date
- so an archive is an *update* to an entity that remains addressable through its normal
read model, not a removal from it. The same holds for a workflow.

Persisted payloads carry **only the identity and the operation**, per P10.3; the
whole-entity `data` of a client frame is assembled at publish time from the authorized read
model, which is what keeps `instructions`, `labels`, `model_overrides` and `dispatch` out of
an append-only log.

**5. Scope boundary: model profiles are excluded from Phase 3 definition deltas.**
`model_profiles` carries a `workspace_id` and **no `project_id`**, while `events` is
per-project and the cursor is `ProjectEventCursor`. A model-profile change therefore has no
per-project `events.seq` to carry, and three of the sixteen writes
(`ModelProfileService.create`, `update`, `delete`) are outside definition deltas for this
phase.

**A workspace-level cursor or event domain is an open architectural question.** P9 does not
cover it, P11 does not invent one, and nothing here should be read as deciding it. Whoever
needs workspace-scoped deltas - credentials are in the same position - opens that decision
then.

**6. Security boundary.** Registration is the boundary, not metadata. `services/ingest.py`
reads the producer from the type's spec and **defaults an unregistered type to
`Producer.AGENT`, which is ingestible**. So today an ingest key could post
`definition.agent.updated` and have it accepted and stored - the same path that
deliberately accepts `future.namespace_from_a_later_release` in
`test_an_unknown_type_is_still_accepted`. A publisher reading `definition.*` rows would
then publish a forged definition delta. Registering these eleven with `CONTROL_PLANE`
closes that for these eleven **and for nothing else**: unrelated unknown types stay
accepted, by the compatibility promise, unless that is separately decided.

### One contradiction P11 corrected

`03-API-AND-FRONTEND.md` §4 illustrated the tombstone as
`{ "type": "agent.deleted", … }`, added with P10.2 as an example. Under P11 that frame
**cannot occur**: an agent is archived, never deleted, and an agent archive maps to
`agent.updated`. The example now shows `connection.deleted`, the only deletion among the
eleven. Nothing else in the documentation contradicted the type names, the tombstone
representation or the frame mapping.

### P12 in full — the definition event envelope

**Approved, not implemented. M3 has not started.** P11 settled the type, the producer, the
folding, the subject and the payload. The M3 audit found it had not settled `dedup_key`,
which `EventEnvelope` requires as a plain `str` and the `events` table declares `CITEXT NOT
NULL` - so M3 could not append a row without either inventing a convention or stopping.

**1. The key: `def:{entity}:{entity_id}:{operation}:{seq}`.** For the first writer that is
`def:agent:{agent_id}:updated:{seq}`, and the pattern generalises to the other ten so that
M4 repeats a decision rather than taking one. `seq` is the number already allocated from
the shared per-project cursor (P9) under the cursor lock, immediately before the append.

**Why the `seq` is in the key, and not merely decorative.** Uniqueness on `events` is
`UNIQUE (project_id, dedup_key)`, enforced by an index on **each monthly partition** rather
than on the table - the partition key would otherwise have to be part of the constraint,
which would let one key through twice at different timestamps. Two consequences follow, and
the convention exists because of both.

- **A recurring event cannot carry a once-per-entity key.** The existing control-plane
  precedent is `exec:{execution_id}:started` and `exec:{execution_id}:cancelled`, which are
  correct *because* an execution starts once. An agent is edited repeatedly. A key of
  `def:agent:{agent_id}:updated` would insert cleanly the first time and raise `23505` every
  later time within the partition, which `CONFLICT_SQLSTATES` maps to a 409 reading "That
  change collided with a concurrent change. Try again." - advice that would never come true,
  for the rest of the month, for that agent.
- **A predictable key is pre-claimable, and the key space is shared with ingest.** The index
  covers `dedup_key` alone, not `(type, dedup_key)`; and `EventRepository.append` never
  consults `seen()` - only `services/ingest.py` does. So a holder of an agent ingest key
  could post an ordinary `agent.heartbeat` whose `dedup_key` is
  `def:agent:<uuid>:updated` and thereby make that agent permanently uneditable through the
  control plane. The definition *type* is refused to an ingest key by P11's producer
  boundary; the *key* was never reserved. Carrying `seq` closes this, because `seq` is not
  knowable before the write it belongs to.

**2. An operation that changes nothing appends nothing.** `AgentRepository.update` returns
the row unchanged, performing no `UPDATE`, when every field arrives `UNSET`, while
`audit.record` still writes a row. The event log follows the mutation, not the request: an
empty `PATCH` appends no event and consumes no `seq`. The asymmetry is deliberate and is
the existing division of labour - **the audit log records that someone asked, the event log
records that something changed.** A delta describing no change would otherwise reach every
subscriber and cost a cursor position.

**3. A definition event's resolved reference columns stay NULL.** `events.agent_id`,
`task_id`, `run_id`, `execution_id` and `execution_step_id` are left unset rather than
pointing at the entity the payload names. `EventRepository.resolve` matches only the
subject kinds `agent`, `task`, `run` and `workflow_execution`, so for P11's `PROJECT`
subject it already returns an all-`None` result - after four lookups that cannot match.
Passing the empty result directly is therefore equivalent and cheaper. It also keeps
definition rows off `events_agent_idx`, which is the direction P10.3 requires: definition
events stay out of the raw activity feed. An authorized consumer reaches the entity through
the snapshot and delta path, where the whole entity is read from the definition table at
publish time.

### What P12 does not claim

**It does not fix the pre-claimable keys that already exist.** `exec:{execution_id}:started`
and `exec:{execution_id}:cancelled` are reservable today by exactly the mechanism described
above. They are once-per-execution, so the collision cost is bounded differently, but the
exposure is real and pre-dates this decision. **It is not M3's to absorb** - a fix touches
the orchestrator's envelope construction and belongs with whoever decides whether
uniqueness should be scoped `(type, dedup_key)` instead.

**It does not address the partition horizon.** `agentworld_ensure_partitions(2)` is called
only from migration 0011 and from no schedule, so `INSERT INTO events` has no partition
roughly two months past the last migration. That is a Phase 2 defect, not a Phase 3
decision - but M3 raises its severity, because after M3 the symptom is no longer that
telemetry stops: **it is that no agent can be edited.** Whoever owns the deployment runbook
owns this.

**It does not decide deadlock retry.** `40P01` is absent from `CONFLICT_SQLSTATES`, raises
`OperationalError` for which no handler is registered, and so reaches the catch-all as a
500; no retry exists anywhere, and no `lock_timeout` is set. The M3 audit established that
cursor-first ordering introduces no new lock cycle - every existing writer of an `agents`
row either takes the cursor first or never takes it - so M3 does not need this decided.
A retry policy or a lock timeout is cross-cutting across ingest, the orchestrator and the
worker, and deserves its own scope.

### P13 in full — the complete producer inventory

**Implemented, 2026-11-26.** All fourteen producers are wired, tested and committed:
`AgentService.update` with the step this record calls M3 (`deeb86c`), and the other
thirteen with M4, in the four commits ending `d70eb5d`. **R8 and R10 stay unchecked even so**, and the reason is
worth stating precisely, because "the producers are done" is not the same claim as "the
criterion is met". What is implemented is the *durable record* - each change appends its
`events` row inside the transaction that makes the change. What R8 also asks for is
publication after that transaction commits, and what R10 asks for is replay; both belong
to the event bus and the WebSocket gateway, and neither exists.

P11 counted "sixteen write operations across four services" in `services/definitions.py`.
That count is right about that file. The audit of the remaining writers enumerated every
write method on `AgentRepository`, `ConnectionRepository` and `WorkflowRepository` and then
found every caller, which turned up **two writers outside it**. One is excluded with a
reason; the other is the subject of this decision.

**1. The inventory.** Fourteen approved producers, all now implemented: thirteen call
sites in `services/definitions.py` and one in `services/executions.py`. The inventory
itself is unchanged by implementation - no producer was added, removed or retyped.

| Producer | Event type | State |
|---|---|---|
| `AgentService.create` | `definition.agent.created` | **implemented** |
| `AgentService.update` | `definition.agent.updated` | **implemented** |
| `AgentService.archive` | `definition.agent.archived` | **implemented** |
| `AgentService.restore` | `definition.agent.restored` | **implemented** |
| `ConnectionService.create` | `definition.connection.created` | **implemented** |
| `ConnectionService.update` | `definition.connection.updated` | **implemented** |
| `ConnectionService.delete` | `definition.connection.deleted` | **implemented** |
| `WorkflowService.create` | `definition.workflow.created` | **implemented** |
| `WorkflowService.compile_delegation` | `definition.workflow.created` | **implemented**, **no production caller** |
| `WorkflowService.update` | `definition.workflow.updated` | **implemented** |
| `WorkflowService.save_draft` | `definition.workflow.updated` | **implemented** |
| `WorkflowService.archive` | `definition.workflow.archived` | **implemented** |
| `WorkflowService.restore` | `definition.workflow.restored` | **implemented** |
| **`ExecutionService._publish`** | **`definition.workflow.updated`** | **implemented - new in P13** |

The work that remained after M3 was **twelve operations in `services/definitions.py` plus
one in `services/executions.py`** - thirteen call sites, producing the ten types M3 did
not. All thirteen were wired in M4. `ModelProfileService.create`, `update` and `delete`
are not producers at all (P11.5).

**An approved producer is not an implemented one**, and the table says which is which on
purpose. Every earlier revision of this record that said "none of this is implemented" was
true when written and is not true now; the distinction has to be read off this column
rather than off a sentence that ages.

**2. Publishing a workflow emits exactly one event.** `ExecutionService._publish` calls
`WorkflowRepository.publish(...)` and then `next_draft(...)`, and between them they insert
workflow steps and edges, set `published_at` on the version, move
`workflows.current_version_id` and open the next draft. That is **one** act - a publish -
and it emits **one** `definition.workflow.updated`. Counting the internal writes would
publish several deltas for a change a client experiences once, and would spend several
cursor positions doing it.

**3. It stays in one transaction.** Sequence reservation, the publication, the next draft,
the event append and the existing audit write all share `_publish`'s existing transaction,
with the cursor reserved **before** the first definition write, exactly as P9 and M3's
pattern require. Nothing about `_publish` becomes a second transaction, and no publish may
commit with its event unwritten.

**4. The payload is the approved one.** Identity and operation: entity type and entity
uuid, subject `SubjectKind.PROJECT`, `dedup_key` carrying the allocated `seq` (P12). **No
new event type, no workflow snapshot, no graph, no step or edge contents, no credentials,
no instructions, no configuration.** A publish makes a frozen graph live, and that graph is
exactly the kind of free-form content P10.3 keeps out of an append-only log; the whole
entity reaches an authorized consumer at publish time from the read model.

**5. The harness writer is excluded, and here is the precise reason.**
`services/agent_test.py` creates a `workflows` row through
`WorkflowRepository.find_or_create_test` on an agent's first test, authorizing
`WORKFLOW_CREATE` and writing an audit row. It is **not** a definition producer, because
`WorkflowRepository.list_all` filters `test_for_agent_id IS NULL` - so a harness is not a
member of the workflow listing that snapshot and delta keep synchronized, and a delta for
it would describe a change to a collection no client holds.

**The limit of that argument, stated so it is not overstated.** `list_all` is one reader.
`WorkflowService.get` will return a harness by id, and `WorkflowRepository.test_for` reads
one deliberately. The claim is therefore **"no listing a client subscribes to contains a
harness"**, not "no endpoint can return one". That is sufficient for snapshot-and-delta
consistency and is the whole of the exclusion. It is written down because the exclusion is
currently correct by the behaviour of a filter, and a reader who found the gap without
finding the filter would reasonably mistake it for an oversight.

**Amended 2026-11-26: the exclusion follows the harness, not the writer.** As first
written, §5 excluded one *writer* - `WorkflowRepository.find_or_create_test`, called from
`services/agent_test.py` - and said nothing about what happens when that harness is
**published**. `ExecutionService.publish_test_harness` is a second entry point into the
same `_publish` body with the harness refusal lifted, so without this amendment §2 and §3
would read as requiring an event there. They do not. **`publish_test_harness` appends no
`definition.workflow.updated`**, reserves no sequence number and takes no cursor lock, and
that is intentional.

The reason is §5's own, unchanged: `WorkflowRepository.list_all` filters
`test_for_agent_id IS NULL`, so a harness is not a member of the workflow listing that
snapshot and delta are specified to keep synchronized, and a delta for it would describe a
change to a collection no client holds. That argument is about the *entity*, so it reaches
the harness's publication exactly as it reaches its creation. The discriminator in the code
is therefore the row - `workflow.is_test_harness` - and not which entry point was used: an
ordinary `publish` of a client-visible workflow emits, as §2 requires, and a harness does
not, whichever verb reached it.

**What this does not say.** It is not a volume argument, and it would be a weak one: a
harness is published **once**, on an agent's first test, because `AgentTestService._harness`
returns `harness.current_version_id is None` as its publish-first predicate - so a later
test is a run, not a republish, and the only way it recurs is an earlier publish that
failed and left no version pointer. The exclusion rests on the listing and nothing else. Nor is
anything else about a harness excluded: **a harness is still published and still executed**,
it still writes its `workflow.publish` audit row, and `agent.test` depends on all of that.
Only the realtime definition event is withheld. And the producer inventory is untouched -
`ExecutionService._publish` remains the fourteenth approved producer, carrying one
conditional inside it; the writer excluded from the inventory is still `find_or_create_test`,
which is not a producer at all.

**6. `compile_delegation` has no production caller.** Its only caller anywhere is
`tests/integration/test_delegation_compilation.py`; there is no route and no service
reaches it. It stays an approved producer and its event should be wired with the rest -
this is a fact about today's reachability, **not** a reason to omit the wiring, since a
producer left unwired because nothing calls it is a producer that will be missing on the
day something does.

### Implementation constraints P13 records for the next step

These came out of the audit. They are **constraints, not work done**.

- **`AgentService.create` is in scope, and it moves M3's test numbers.**
  `tests/integration/test_definition_events.py` asserts `seq == 1`, `[1, 2]` and that the
  project has no cursor row, every one of them resting on its own stated premise that
  creating an agent appends nothing. Wiring `create` makes the creation take `seq 1`. Those
  assertions must become **relative** - captured after setup - rather than re-baselined to
  new fixed numbers, or the step after this one moves them again.
- **Where the mutation is also the existence check, reserve the cursor first and let a
  not-found roll back.** `AgentService.restore`, `WorkflowService.restore` and
  `ConnectionService.delete` have no prior read to refuse against, so a 404 will briefly
  take the project's cursor lock. That is accepted. **Do not add a read-then-write precheck
  to avoid it**: it costs a query and reintroduces the race the single statement avoids, to
  buy back a lock held for microseconds by a caller who is already authorized.
  `AgentService.archive`, `WorkflowService.archive` and the three `update`s read first and
  so keep M3's property of resolving every refusal before the lock.
- **The no-op guard is preserved for the `update` operations.** All four definition
  repositories share the same early return - `if not values: return await self.get(...)` -
  so P12.2 transfers verbatim to `ConnectionService.update` and `WorkflowService.update`.
  The other operations always write when they succeed and need no guard; adding one there
  would be dead code.
- **`save_draft` always writes**, because `graph` is a required argument. There is no
  autosave or debounce in `apps/web` today and **none is to be introduced here**. A future
  graph editor that autosaved would turn each save into an event and a cursor position, and
  that is that editor's problem to solve, server-side, when it exists.
- **`connection.delete` carries the approved identity-and-operation payload like every
  other type.** It is the only hard delete, so it is the one case where the publisher cannot
  read the entity back - which is exactly why P10.2 asks for a tombstone. **Tombstone
  construction is a client-frame concern and remains later work**; the persisted event is
  not special.
