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

Ten decisions. P1-P8 were taken after a read-only audit found that Phase 3's scope sentence
(`04-ROADMAP-AND-MVP.md` §2, "this phase is T0–T8 and T13"), its build list, its
acceptance ledger and the T-criteria in `08-PHASE-3-READINESS.md` did not agree with each
other or with the code. **Approved by the product owner on 2026-11-20.** No criterion was
deleted and no phase boundary moved except where a row below says so. **P9 was approved
later the same day**, from the realtime-spine audit that followed: it is the one
architecture decision R8 and R10 could not be implemented without, and it is detailed
after the table. **P10 followed from the audit of P9's implementation feasibility**: five
decisions that audit identified and deliberately did not take, three of them protocol
surface. It is detailed after P9.

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
