# The permission matrix

> Proposal, 2026-11-01. Deliverable 5 of the refinement brief.

The earlier proposal said "approximately 45 new authorization rules". Worked through
properly it comes to **62 new actions, 77 in total** — counted from the tables in §2, not estimated. The number went up because least
privilege means splitting actions, not merging them — and the useful parts of this
document are not the count but the four tables after the matrix: which actions
deliberately **do not exist**, which ones **change role depending on what is being
granted**, which are **admin rather than member and why**, and what keeps 77 rules
honest as they grow.

## 1. Principles

1. **One function, one table.** `authorize(principal, action, resource)` keeps its Phase 1
   shape. 77 rows in one dict is reviewable in a sitting; 77 checks scattered across
   routes is not.
2. **Least privilege, by splitting.** Where one action would bundle a harmless capability
   with a dangerous one, it becomes two. `integration.read` is a `member`;
   `integration.read_config`, which exposes endpoints and scopes, is an `admin`.
3. **The default role is the weakest that makes the product usable.** A beginner building
   their first ecosystem is a `member` and must not need an admin for anything in the happy
   path. Everything a `member` cannot do is something that spends money unexpectedly,
   widens a permission, acts autonomously, or reveals a secret.
4. **Reading configuration is not reading data.** A `viewer` sees what the ecosystem *is*
   and what it *did*. Seeing how a credential or integration is wired is separate.
5. **Granting a capability is at least as privileged as using it.** A `member` may call a
   tool an agent already holds; granting an agent a `consequential` tool is an `admin`.
6. **Visual actions carry no operational privilege.** `world.update` is held by a `member`
   and grants nothing else, which is what makes dragging a building safe
   (`01-DOMAIN-AND-PROTOCOL.md` §10).
7. **Sensitive is a property of the action, recorded in the table.** The `Sensitive` column
   is not decoration: it drives which actions appear in the audit digest, which require a
   re-authentication prompt in the UI, and which the conversational builder must obtain
   explicit consent for.

Roles are unchanged from Phase 1: `viewer < member < admin < owner`, and the 404-not-403
rule for a non-member stands for every action here.

## 2. The matrix

`Scope` is the tenancy level the action is authorized against. `Sensitive` means the action
exposes or modifies secrets, permissions, spend or externally-visible behaviour.

### Tenancy — built in Phase 1, unchanged

| Object | Action | Scope | Role | Sensitive |
|--------|--------|-------|------|-----------|
| workspace | `workspace.read` | workspace | viewer | |
| workspace | `workspace.update` | workspace | admin | |
| workspace | `workspace.delete` | workspace | owner | yes |
| member | `member.read` | workspace | viewer | |
| member | `member.invite` | workspace | admin | yes |
| member | `member.update_role` | workspace | admin | yes |
| member | `member.remove` | workspace | admin, or self | yes |
| project | `project.read` | workspace | viewer | |
| project | `project.create` | workspace | member | |
| project | `project.update` | project | member | |
| project | `project.delete` | project | admin | yes |
| api_key | `api_key.read` | workspace | admin | yes |
| api_key | `api_key.create` | workspace | admin | yes |
| api_key | `api_key.revoke` | workspace | admin | yes |
| api_key | `api_key.delete` | workspace | admin | yes |

### Agents and models

| Object | Action | Scope | Role | Sensitive |
|--------|--------|-------|------|-----------|
| agent | `agent.read` | project | viewer | |
| agent | `agent.create` | project | member | |
| agent | `agent.update` | project | member | | |
| agent | `agent.configure` | project | **admin** | **yes** |
| agent | `agent.archive` | project | admin | |
| agent | `agent.pause` | project | member | |
| agent | `agent.test` | project | member | **yes** |
| agent | `agent.read_runs` | project | viewer | |
| model_profile | `model_profile.read` | workspace | viewer | |
| model_profile | `model_profile.create` | workspace | **admin** | **yes** |
| model_profile | `model_profile.update` | workspace | **admin** | **yes** |
| model_profile | `model_profile.delete` | workspace | admin | |

`agent.update` is name, role, description, instructions and labels — everything a builder
edits. `agent.configure` is the narrow, dangerous remainder: `model_profile_id`,
`model_overrides` and `dispatch`. It is `admin` for one specific reason stated in §5.

`agent.test` spends money and may invoke tools, so a `member` holds it and it is marked
sensitive — tool grants and approval gates still apply inside a test run.

### Workflows and executions

| Object | Action | Scope | Role | Sensitive |
|--------|--------|-------|------|-----------|
| workflow | `workflow.read` | project | viewer | |
| workflow | `workflow.create` | project | member | |
| workflow | `workflow.update_draft` | project | member | |
| workflow | `workflow.validate` | project | member | |
| workflow | `workflow.publish` | project | member | yes |
| workflow | `workflow.archive` | project | admin | |
| workflow | `workflow.run` | project | member | yes |
| execution | `execution.read` | project | viewer | |
| execution | `execution.cancel` | project | member | |
| execution | `execution.pause_resume` | project | member | |
| execution | `execution.retry_step` | project | member | yes |
| execution | `execution.provide_input` | project | member | |
| approval | `approval.read` | project | viewer | |
| approval | `approval.decide` | project | **per-approval `required_role`**, default member, **and not the attributed user when the approval says so** | **yes** |
| approval_policy | `approval_policy.read` | project | viewer | |
| approval_policy | `approval_policy.update` | project | **admin** | **yes** |

`workflow.publish` and `workflow.run` are `member` because a workflow is only as dangerous
as the grants its steps already have, and those grants were authorized separately. Running
it autonomously on a schedule is a different matter — see `trigger.enable`.

`approval.decide` is the only action whose required role is **data, not code**: it comes
from `approvals.required_role`, frozen when the approval was created by resolving the
project's `approval_policies` row against the step, tool and connection. A workflow that
sends money can require `admin` approval while one that posts to Slack requires `member`,
without inventing a role. The approval may additionally require that the decider is not the
user the execution is attributed to — see `01-DOMAIN-AND-PROTOCOL.md` §7.

`approval_policy.update` is `admin`. AgentWorld has no per-project owner role, so `admin` is
the nearest thing to the brief's "project owners"; per-project roles are a deferred feature
(`04-ROADMAP-AND-MVP.md` §9). An `admin` can loosen the policy for low-risk work, which is
audited and within what an admin can already do — but **cannot loosen it past the two hard
floors**, because those are not in the table: an approver's role may never fall below the
role required to perform the underlying action, and a policy produces a *requirement*, never
a capability. `authorize(principal, APPROVAL_DECIDE, …)` runs first and independently, so a
policy naming `viewer` would still be refused by this matrix.

### Connections, tools and providers

| Object | Action | Scope | Role | Sensitive |
|--------|--------|-------|------|-----------|
| connection | `connection.read` | project | viewer | |
| connection | `connection.create` | project | member → **admin** when granting a `consequential` tool | **yes** |
| connection | `connection.update` | project | member → **admin**, same rule | **yes** |
| connection | `connection.delete` | project | member | |
| tool | `tool.read` | project | viewer | |
| tool | `tool.try` | project | member | **yes** |
| tool | `tool.refresh` | project | admin | |
| provider | `provider.read` | workspace | viewer | |

### Integrations and credentials

| Object | Action | Scope | Role | Sensitive |
|--------|--------|-------|------|-----------|
| integration | `integration.read` | project | member | |
| integration | `integration.read_config` | project | **admin** | **yes** |
| integration | `integration.create` | project | admin | yes |
| integration | `integration.update` | project | admin | yes |
| integration | `integration.delete` | project | admin | yes |
| integration | `integration.connect` | project | admin | **yes** |
| integration | `integration.test` | project | admin | yes |
| credential | `credential.read_metadata` | workspace | **admin** | **yes** |
| credential | `credential.create` | workspace | admin | **yes** |
| credential | `credential.rotate` | workspace | admin | **yes** |
| credential | `credential.revoke` | workspace | admin | **yes** |
| credential | `credential.delete` | workspace | admin | **yes** |

`integration.read` is `member`, not `viewer`: the *existence* of a Slack connection is
operational information a member needs, and a viewer has no reason to enumerate which
external systems a company has wired up. `read_config` — endpoints, scopes, which
credential — is `admin`.

Every credential action is `admin` and sensitive. There is no `viewer` row and no
`member` row, and §3 explains the one that does not exist at all.

### Knowledge and triggers

| Object | Action | Scope | Role | Sensitive |
|--------|--------|-------|------|-----------|
| knowledge_source | `knowledge.read` | project | viewer | |
| knowledge_source | `knowledge.create` | project | member | |
| knowledge_source | `knowledge.upload` | project | member | |
| knowledge_source | `knowledge.delete` | project | member | |
| trigger | `trigger.read` | project | viewer | |
| trigger | `trigger.create` | project | member | |
| trigger | `trigger.update` | project | member | |
| trigger | `trigger.delete` | project | member | |
| trigger | `trigger.enable` | project | **admin** | **yes** |
| trigger | `trigger.rotate_token` | project | **admin** | **yes** |

### World and builder

| Object | Action | Scope | Role | Sensitive |
|--------|--------|-------|------|-----------|
| world | `world.read` | project | viewer | |
| world | `world.update` | project | member | |
| builder | `builder.use` | project | member | |
| builder | `builder.apply` | project | member, **and every action in the plan** | **yes** |

`world.update` covers position, size, appearance, district and route — the whole of
`01-DOMAIN-AND-PROTOCOL.md` §10 — and grants nothing operational. That is the payoff of
keeping visual state in its own tables: dragging a building needs the weakest role that can
change anything at all, and a `viewer` watching an execution cannot rearrange the island
out from under the person building it.

`builder.apply` is the composite: see §4.

## 3. Actions that deliberately do not exist

More useful than the matrix, because an absent action cannot be granted by mistake.

| Not an action | Why | What replaces it |
|---------------|-----|------------------|
| `credential.read_secret` | The whole point. There is no role that can read credential material — not owner, not a platform operator | Rotate it, or read `display_hint` |
| `agent.set_status` | State is derived. An agent's status comes from folded events, so a human cannot assert "this agent is working" | `agent.pause`, or emit an event |
| `execution.edit` | An execution is a record of what happened. Editing it would make the audit trail fiction | Cancel it and run a new one |
| `event.delete`, `event.update` | `events` is append-only *by privilege*: the application role holds `SELECT, INSERT` on the three partitioned **parents** and no privilege at all on any partition — see the note below | Retention drops whole partitions |
| `audit_log.delete`, `audit_log.update` | Same, and already enforced in Phase 1 | Nothing |
| `step.force_succeed` | Marking a step done that never ran would let a workflow report a result it never produced | Retry it, or fail it and branch |
| `approval.decide_on_behalf_of` | An approval is accountability. Delegation needs a model of who may act for whom | A second approver — deferred to v1.1 |
| `approval_policy.override_floor` | The two hard floors in `01` §7 are not configurable at all. A policy that could reach under them would make every other row here advisory | Nothing. Change the underlying permission instead |
| `world.update_execution_state` | The presentation tables have no execution columns to update | Nothing |
| `dispatch.create` | Only `advance()` creates dispatches, inside its transaction. A human-creatable dispatch would bypass every gate | `execution.retry_step` |

That last row is worth dwelling on: it is what makes the approval gate structural. If an
API could create a dispatch, approvals would be advisory again.

**One correction from implementing migration 0011.** A partition inherits neither its
parent's privileges nor its parent's row-level security policy, and the default
privileges set in migration 0005 hand every newly created table `SELECT, INSERT`. So
revoking the write verbs on `events` alone would have left `UPDATE events_y2026m11`
open — and, worse, `SELECT * FROM events_y2026m11` would have returned **every tenant's**
events, because the tenant policy on the parent does not apply to a query that names a
partition. Verified rather than assumed: a partition needs no privileges of its own to be
used through its parent, so `agentworld_ensure_partitions` revokes `ALL` on every
partition it creates. Tenant isolation on these three tables therefore rests on one
invariant — *the application role holds no privilege on any partition* — which
`test_append_only.py` asserts over every partition, so a later grant fails a test rather
than leaking data.

## 4. Conditional and composite rules

Three actions whose required role is not a constant. Each is a function in
`domain/policy.py` beside the table, not a special case inside a route.

**`connection.create` / `connection.update`** — `member`, except when the connection is
`grant_tool` and the target tool's `side_effect` is `consequential`, when it is `admin`.
Granting an agent the ability to send email is privilege escalation by proxy: without this
rule a `member` could give an agent a capability the `member` was never trusted with
directly.

**`approval.decide`** — read from `approvals.required_role`, with `member` as the default
and the workspace's own floor applied. The authoring of the requirement is
`workflow.update_draft` (a `member`), which is deliberate: deciding *that* something needs
admin approval is ordinary authoring; *being* that approver is not.

**`builder.apply`** — the applying user must hold `builder.apply` **and** every action the
plan performs. A `member` applying a plan that creates an integration is refused **at plan
time**, with the reason, rather than failing halfway through the apply transaction. The
builder has no privilege of its own; it is a client of the same services a human uses, so
it cannot be a privilege-escalation path.

## 5. Why these five are admin and not member

Each one is where a `member` could otherwise cause something the role does not imply.

**`agent.configure`** — because it includes `dispatch.url`, a URL AgentWorld will make
outbound HTTP requests to. That is a server-side request forgery vector: a `member` could
point an agent at `http://169.254.169.254/` or an internal service and read the response
through the step output. The URL is additionally validated against a deny-list of loopback,
link-local and private ranges (configurable for self-hosters who legitimately dispatch
inside their own network), and the deny-list is enforced at request time, not only at save
time, so a DNS record that changes later does not slip through.

**`model_profile.create` / `.update`** — because a profile names a credential and sets the
model, and therefore sets what every agent using it spends. A `member` choosing between
"Fast" and "Careful" is picking from profiles an admin approved.

**`trigger.enable`** — because enabling a schedule is the moment the system starts acting
without anyone present. Authoring the trigger is a `member`'s job; turning it on is the
decision that it should run unattended, and that is the single most consequential toggle in
the product.

**`trigger.rotate_token`** — the token *is* a credential, and the plane it authenticates
can start a workflow.

**`integration.read_config` / `credential.read_metadata`** — not because the data is
secret, but because together they map which external systems hold which credentials with
which scopes. That is the reconnaissance step before an attack, and a `viewer` has no need
of it.

## 6. What keeps 77 rules honest

> **Implementation note, T6b.** The matrix is **79** actions in the code. The 78th,
> `dispatch.resolve_review` at `admin`, was not in this document: T6 created a state that
> only a person can leave — a dispatch whose outbound call may or may not have happened —
> and that needed an action of its own rather than a reuse of `execution.retry_step`,
> which is `member`. Every "77" below counts what this proposal designed, and
> `test_the_matrix_is_the_size_the_proposal_says_it_is` asserts both numbers separately so
> the two cannot drift apart silently.
>
> **Implementation note, task 3 stage B.** The 79th is `workflow.update` at `member`: a
> workflow's name and description. `03-API-AND-FRONTEND.md` designed
> `PATCH /v1/workflows/{id}` and the table above enumerated no action for it, so the
> endpoint had none to ask for and did not exist. It is distinct from
> `workflow.update_draft` because renaming a workflow changes what it is called and
> nothing about what it would do.
>
> **Implementation note, task 3 stage C.** The 80th and 81st are `agent.restore` and
> `workflow.restore`, both at `admin`, mirroring the two archives. This document designed
> archiving as one-way, and the gap showed up as two compiler remedies — "Restore the
> agent, then test it" — naming an action no endpoint could perform. Restoring is `admin`
> because a restored managed agent can be dispatched again, and separate per entity
> because the two archives are. Workspace and project restoration remain unimplemented.
>
> **Implementation note, task 3 stage D.** The 82nd is `agent.read_history` at `viewer`:
> an agent's status transitions. This document designed `agent.read_runs` for
> `GET /v1/agents/{id}/status-history` and `…/runs` together, and the two are different
> tables with different producers - history is folded from what an agent says about
> itself, runs carry what it did - so one action for both would mean nobody could be
> granted one without the other.

Phase 1 already built the two mechanisms, and they generalise without change:

**`test_every_action_has_a_minimum_role`** — `authorize()` indexes `MINIMUM_ROLE[action]`,
so an action added to the enum and forgotten in the table raises `KeyError` at runtime: a
500, not a 403. The test asserts the table is total over the enum.

**`test_the_matrix_covers_every_mutating_route`** — walks the live FastAPI route table and
fails if any route has no row in the HTTP role matrix, with a small explicit exemption list.
A new endpoint merged without a permission decision does not pass CI. This is why 62 new
actions is maintainable: forgetting one is a build failure rather than a review miss.

Both extend to the new mechanisms:

**Tenant isolation coverage** — `test_a_foreign_id_is_404_on_every_endpoint` is driven by a
table of `(method, path, body)`. A coverage guard in the same shape as the role-matrix one
fails if a route taking a resource id is absent from it. Every new entity therefore gets
its isolation test by construction, not by remembering.

**The sensitive flag is checked, not just recorded** — a test asserts every action marked
sensitive appears in the audit digest configuration, and that every action the builder can
perform which is marked sensitive has a `requires_consent` code.

**The matrix is transcribed from this document, not derived from the code** — as in Phase 1,
where the role table in `docs/03-API.md` and `MINIMUM_ROLE` are independently written and
then tested against each other. A test that read the table it was testing would prove
nothing.

## 7. Acceptance criteria

- `MINIMUM_ROLE` is total over `Action`; the pure matrix test covers every action × every
  role and asserts both outcomes occur.
- Every route is present in the HTTP role matrix or its exemption list; the coverage guard
  fails otherwise.
- Every endpoint taking a resource id returns **404** for a foreign id, and the owner can
  perform the same call successfully, so the 404s are not explained by broken routes.
- Every new tenant table is in `RLS_PROTECTED_TABLES`, and `test_rls.py` asserts the
  database agrees. The role asserting it still cannot bypass RLS — the Phase 1 vacuity
  guard runs first.
- A `member` can complete the whole happy path: create agents, build and publish a
  workflow, run it, decide a `member`-level approval, arrange the island.
- A `member` is refused, with a reason naming the required role, on: `agent.configure`,
  `model_profile.create`, `trigger.enable`, `integration.create`, every credential action,
  and `connection.create` granting a `consequential` tool.
- A `viewer` can read and watch everything in a project and mutate nothing, including the
  island layout.
- No action exists for any row in §3. Asserted by name against the `Action` enum, so adding
  one later is a deliberate, visible change.
- `builder.apply` with a plan exceeding the caller's permissions fails at plan time, names
  the action that exceeded them, and creates nothing.
- An `agent.configure` setting `dispatch.url` to a blocked destination is refused at save
  time **and** at request time, for every range in `06-OUTBOUND-SECURITY.md` §3.
- A `member` cannot decide an approval whose `required_role` is `admin`, and **cannot decide
  one attributed to themselves when `requires_different_user` is set** — refused by the
  service and, independently, by the check constraint on `approvals`.
- An `admin` editing `approval_policies` cannot produce a requirement below either hard
  floor, and an edit that would strand a published workflow is refused with the
  `approval_unsatisfiable` finding.
- A workflow whose approval requirement cannot be satisfied by anyone in the workspace
  cannot be published.
