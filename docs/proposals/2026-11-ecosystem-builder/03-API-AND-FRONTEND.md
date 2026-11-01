# API, backend modules, workspace and world

> Proposal, 2026-11-01. Deliverables 7, 8 and 9.

## 1. Planes — one addition

| Plane | Prefix | Credential | Audience |
|-------|--------|-----------|----------|
| Control | `/v1/workspaces`, `/v1/projects`, `/v1/agents`, `/v1/workflows`, `/v1/executions`, `/v1/approvals`, `/v1/connections`, `/v1/tools`, `/v1/integrations`, `/v1/knowledge`, `/v1/triggers`, `/v1/credentials`, `/v1/world`, `/v1/builder` | Supabase JWT | web app, operator, CI |
| Ingest | `/v1/ingest/*`, `/v1/self/*` | `X-AgentWorld-Key` | monitored and dispatchable agents |
| Adapter inbound | `/v1/hooks/{integration_id}` | adapter-defined, usually HMAC | third-party systems |
| **Trigger inbound** | **`/v1/triggers/{id}/fire/{token}`** | **the token in the path** | **anything that can POST** |
| Realtime | `/v1/ws/projects/{project_id}` | short-lived ticket | web app |

The trigger plane is new and is deliberately its own: a webhook trigger has to be callable
by a system that will never hold an AgentWorld credential — a GitHub webhook, a Zapier
step, a cron service. The token is 32 bytes of `secrets.token_urlsafe`, is the only
credential, is rotatable, is rate-limited per trigger, and grants exactly one thing:
*start this one workflow*. It cannot read anything. Putting it on the ingest plane would
either weaken the ingest key's meaning or require one key per webhook.

K1 still holds: each plane resolves to one principal type and the type checker refuses the
other. `TriggerPrincipal` joins `UserPrincipal` and `KeyPrincipal` as a third frozen class
with exactly one capability.

## 2. Backend modules

```
src/agentworld/
├── domain/                        ← no framework, no provider, no I/O
│   ├── entities.py                + Agent, Workflow, WorkflowVersion, Step, Edge,
│   │                                Connection, Tool, Integration, Credential (no secret
│   │                                field), KnowledgeSource, Trigger, Execution,
│   │                                ExecutionStep, Approval
│   ├── enums.py                   + 8 new enums
│   ├── policy.py                  + 57 new actions, same table (05-PERMISSIONS)
│   ├── protocol/
│   │   ├── envelope.py            NEW  subject + correlation
│   │   ├── types.py               NEW  the registry and payload models
│   │   └── version.py             exists
│   ├── reducers/
│   │   ├── agent.py  task.py  run.py           as designed
│   │   ├── workflow.py            NEW  workflow.* -> executions, execution_steps
│   │   └── approval.py            NEW  approval.* -> approvals, execution_steps
│   ├── orchestration/
│   │   ├── advance.py             NEW  the pure decision function
│   │   ├── decisions.py           NEW  the Decision union
│   │   ├── readiness.py           NEW  edge satisfaction and condition evaluation
│   │   └── validate.py            NEW  can this graph run at all
│   ├── expressions/               NEW  the small evaluator (02-ORCHESTRATION §4)
│   └── keys.py  slugs.py  policy.py  principals.py  errors.py     exist
│
├── services/                      orchestrate; no SQL, no provider SDK
│   ├── agents.py  workflows.py  connections.py  tools.py  integrations.py
│   ├── knowledge.py  triggers.py  approvals.py  credentials.py      all NEW
│   ├── executions.py              NEW  create, apply decisions, cancel, resume
│   ├── ingest.py                  as designed: validate → dedupe → append → fold → publish
│   └── builder.py                 NEW  the conversational ecosystem builder
│
├── repositories/                  the only layer that speaks SQL
│   └── + one module per new aggregate; each scoped, each needing a TenantScope
│
├── infra/
│   ├── providers/                 NEW  base.py (Protocol), anthropic.py,
│   │                                   openai_compatible.py, registry.py
│   ├── tools/                     NEW  base.py, builtin/, mcp_client.py, executor.py
│   ├── secrets/                   NEW  base.py (SecretStore), envelope.py
│   ├── db.py  event_bus.py  redis.py  auth/  hashing.py  logging.py   exist or as designed
│
├── workers/                       step_runner, dispatch_sender, dispatch_reaper,
│                                  scheduler, timeout_sweeper + the designed ones
└── api/
    ├── v1/                        + agents, workflows, executions, approvals,
    │                                connections, tools, integrations, knowledge,
    │                                triggers, credentials, builder, world
    └── ws/                        gateway, frames
```

### Two new import-linter contracts

The existing five stay. These are added because the pivot creates exactly the failure modes
they prevent:

```toml
[[tool.importlinter.contracts]]
# AgentWorld now calls LLM providers, which is precisely when "provider-
# independent" stops being free. Only infra/providers may import an SDK;
# domain/ is already covered by the G1 contract, and services/ is where the
# first convenient `import anthropic` would otherwise appear.
name = "Only infra.providers imports a model provider SDK"
type = "forbidden"
source_modules = ["agentworld.services", "agentworld.repositories",
                  "agentworld.api", "agentworld.workers"]
forbidden_modules = ["anthropic", "openai", "google.generativeai",
                     "mistralai", "cohere", "litellm"]
allow_indirect_imports = true

[[tool.importlinter.contracts]]
# The orchestrator decides; it never performs. An import of a provider, an
# HTTP client or the secret store from the decision layer is the exact shape
# of making `advance` untestable and replay unsafe.
name = "Orchestration decides and never performs"
type = "forbidden"
source_modules = ["agentworld.domain.orchestration", "agentworld.domain.expressions"]
forbidden_modules = ["httpx", "sqlalchemy", "redis",
                     "agentworld.infra", "agentworld.services",
                     "agentworld.repositories"]
```

Both are verified the way Phase 0's were: plant the violation, watch it fail, remove it.

## 3. Control-plane endpoints

Conventions unchanged — RFC 9457 problems, cursor pagination, `Idempotency-Key`, 404 for
invisible, roles from one `authorize()` table.

### Agents

| Method | Path | Role |
|--------|------|------|
| `GET` `POST` | `/v1/projects/{id}/agents` | viewer+ / member+ |
| `GET` `PATCH` `DELETE` | `/v1/agents/{id}` | viewer+ / member+ / admin+ |
| `POST` | `/v1/agents/{id}/test` | member+ |
| `POST` | `/v1/agents/{id}/pause` `…/resume` | member+ |
| `POST` | `/v1/agents/{id}/commands` | member+ |
| `GET` | `/v1/agents/{id}/status-history` `…/runs` | viewer+ |

`PATCH` accepts the definition half and never `status` — the existing rule, now with more
to protect. `POST /…/test` runs the agent once against a sample input without a workflow:
the single most useful affordance when building one, and it is a real execution producing
real events, not a preview.

`pause`/`resume` is honest per mode: a `managed` agent really stops being dispatched; a
`dispatchable` one gets a `pause` command if it advertised the capability and a `409`
naming the mode if it did not; an `observed` one gets a `409` always.

### Workflows and executions

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` `POST` | `/v1/projects/{id}/workflows` | viewer+ / member+ | |
| `GET` `PATCH` `DELETE` | `/v1/workflows/{id}` | viewer+ / member+ / admin+ | |
| `GET` | `/v1/workflows/{id}/versions` | viewer+ | |
| `PUT` | `/v1/workflows/{id}/draft` | member+ | the whole graph, atomically |
| `POST` | `/v1/workflows/{id}/validate` | member+ | **see below** |
| `POST` | `/v1/workflows/{id}/publish` | member+ | freezes a version |
| `POST` | `/v1/workflows/{id}/run` | member+ | manual trigger; `Idempotency-Key` honoured |
| `GET` | `/v1/workflows/{id}/executions` | viewer+ | |
| `GET` | `/v1/executions/{id}` | viewer+ | with steps and timeline |
| `POST` | `/v1/executions/{id}/cancel` `…/pause` `…/resume` | member+ | |
| `POST` | `/v1/executions/{id}/steps/{key}/retry` | member+ | |
| `POST` | `/v1/executions/{id}/input` | member+ | answers `waiting_input` |

`POST /validate` is the endpoint the whole product leans on, and it returns structured
findings rather than a boolean:

```jsonc
{ "publishable": false, "findings": [
  { "severity": "error", "code": "observed_agent_assigned",
    "pointer": "/steps/research",
    "detail": "“Legacy crawler” only reports activity and cannot be given work.",
    "remedy": "Pick a managed agent, or connect this one as dispatchable." },
  { "severity": "error", "code": "tool_not_granted",
    "pointer": "/steps/post/tool_id",
    "detail": "“Writer” has no connection granting send_slack_message.",
    "remedy": "Add a tool grant, or let the builder add it." },
  { "severity": "warning", "code": "consequential_without_approval",
    "pointer": "/steps/post",
    "detail": "This step sends a message and nothing gates it." },
  { "severity": "error", "code": "unreachable_step", "pointer": "/steps/cleanup" },
  { "severity": "error", "code": "cycle", "pointer": "/edges/4" }
] }
```

`detail` is written for a beginner and `remedy` says what to do. This is where "do not
treat a visual line as proof two components can communicate" is actually enforced: a graph
that looks connected and cannot run says so, with a pointer the UI highlights and the
island flashes. `validate` is also what the conversational builder calls before proposing
anything, so the builder cannot produce an unrunnable ecosystem and claim success.

### Approvals

| Method | Path | Role |
|--------|------|------|
| `GET` | `/v1/projects/{id}/approvals?status=pending` | viewer+ |
| `GET` | `/v1/approvals/{id}` | viewer+ |
| `POST` | `/v1/approvals/{id}/decide` | per `approval.required_role` |

One endpoint for grant and deny, `{"decision":"granted","reason":"..."}`, because they are
one decision and two endpoints invite a UI that wires the wrong one.

### Connections, tools, integrations, knowledge, triggers, credentials

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `GET` `POST` | `/v1/projects/{id}/connections` | viewer+ / member+ | |
| `DELETE` `PATCH` | `/v1/connections/{id}` | member+ | |
| `GET` | `/v1/projects/{id}/tools` | viewer+ | builtin + discovered + integration |
| `POST` | `/v1/tools/{id}/try` | member+ | run once with given args; respects approval |
| `GET` | `/v1/providers` | viewer+ | registry: slug, models, config schema |
| `GET` `POST` | `/v1/projects/{id}/integrations` | admin+ | |
| `GET` | `/v1/integrations/{id}/oauth/start` | admin+ | returns the consent URL |
| `GET` | `/v1/integrations/oauth/callback` | — | state-signed; no session needed |
| `POST` | `/v1/integrations/{id}/refresh-tools` | admin+ | re-discover MCP tools |
| `POST` | `/v1/integrations/{id}/test` | admin+ | as designed; persists nothing |
| `GET` `POST` | `/v1/projects/{id}/knowledge` | viewer+ / member+ | |
| `POST` | `/v1/knowledge/{id}/documents` | member+ | upload; extraction is async |
| `GET` `POST` | `/v1/projects/{id}/triggers` | viewer+ / member+ | |
| `POST` | `/v1/triggers/{id}/rotate-token` | admin+ | |
| `GET` `POST` | `/v1/workspaces/{ws}/credentials` | admin+ | **never returns secret material** |
| `DELETE` | `/v1/credentials/{id}` | admin+ | refused while referenced |

`GET /v1/credentials` returns `display_hint`, `scopes`, `expires_at`, `last_used_at` — what
an operator needs to recognise and audit a credential, and nothing that could be replayed.

### The builder

| Method | Path | Role | Notes |
|--------|------|------|-------|
| `POST` | `/v1/projects/{id}/builder/sessions` | member+ | |
| `POST` | `/v1/builder/sessions/{id}/messages` | member+ | streams; returns a **plan**, creates nothing |
| `POST` | `/v1/builder/sessions/{id}/apply` | member+ | applies a named plan, atomically |

The split is the whole design and §6 explains it.

### Project overview, revised

The dashboard's single call shifts from agent-and-task counts to ecosystem health:

```jsonc
{ "project": { "id": "…", "name": "Market Intel" },
  "counts": { "agents": { "working": 2, "idle": 3, "error": 0, "offline": 1 },
              "workflows": { "published": 4, "draft": 1 },
              "executions_24h": { "completed": 28, "failed": 2, "running": 1 },
              "approvals_pending": 1,
              "integrations": { "connected": 2, "error": 0 } },
  "attention": [ { "kind": "approval", "id": "appr-21c8",
                   "title": "Post the competitor brief to #market-intel?" } ],
  "throughput_24h": [ { "hour": "2026-11-04T07:00:00Z", "completed": 3, "failed": 0 } ],
  "cursor": 448912 }
```

`attention` is the field the product is organised around: the island's "requires
attention" state, the nav badge and the Live Mode bell all read it, so there is one answer
to "what needs me" rather than three that can disagree.

## 4. Realtime

Frames extend the designed set rather than replacing it. Snapshot, then deltas carrying a
monotonic per-project cursor; `resume`, `resync_required`, ping/pong unchanged.

```jsonc
{ "type": "snapshot", "cursor": 448912, "data": {
    "agents": [], "workflows": [], "connections": [], "integrations": [],
    "executions": [], "approvals": [], "world": { "layout": {}, "placements": [] } } }

// definition deltas — config changes, which are NOT in the event log
{ "type": "agent.updated",       "cursor": 448913, "data": {} }
{ "type": "workflow.updated",    "cursor": 448914, "data": {} }
{ "type": "connection.updated",  "cursor": 448915, "data": {} }

// execution deltas — folded from the event log
{ "type": "execution.updated",   "cursor": 448916, "data": {} }
{ "type": "step.updated",        "cursor": 448917, "data": {} }
{ "type": "approval.updated",    "cursor": 448918, "data": {} }

// the raw event, for the activity feed and for island animation intents
{ "type": "event",               "cursor": 448919, "data": {} }
```

Deltas stay whole-entity snapshots, not patches, so an ordering bug cannot produce a
half-applied entity. Channels (`agents`, `workflows`, `executions`, `approvals`, `events`,
`world`) let Build Mode subscribe to definitions and Live Mode add executions and events,
rather than every tab receiving everything.

## 5. The world: Build Mode and Live Mode

### The contract, extended not replaced

K17 stays: the island is a projection and `project()` is pure and Phaser-free. Build Mode
needs the island to *create* things, which is exactly where a design turns the island into
the source of truth. It does not have to.

```ts
// Two projections. Both pure, both unit-tested with no canvas.
projectBuild(definition: DefinitionState, layout: LayoutConfig): WorldState
projectLive(definition: DefinitionState, execution: ExecutionState,
            recent: readonly Event[], layout: LayoutConfig): WorldState

// New, and the reason Build Mode does not break the contract: a gesture
// becomes a described intention, with no knowledge of HTTP.
interpret(gesture: Gesture, context: WorldContext): Intent[]
```

```
world/
  types.ts          WorldState, WorldEntity, AnimationIntent, Gesture, Intent
  projector/        projectBuild, projectLive, rules tables     (pure, no React, no Phaser)
  interpreter/      interpret()                                  (pure, no React, no Phaser)
  dispatch/         Intent -> control-plane call                 (the ONLY layer that fetches)
  phaser/           scene, tilemap, sprites, camera              (quarantined)
  bridge/           React <-> Phaser mount, one store subscription
```

Phaser raises gestures and never calls an API. `interpret` is pure, so "dragging a
building from the plaza onto a tile creates an agent there" is a unit test, not a manual
click-through. A third eslint boundary covers `world/interpreter/**` with the same
restrictions as the projector; `world/dispatch/**` is the one directory permitted to import
`@/lib/api`.

**Deleting `world/phaser/` still leaves a working product**, which remains a test: the
backend suite and the non-world web suite stay green with it removed from the build.

### Build Mode

The island as an editor over the definition graph. No animation, no execution state.

| Gesture | Intent | Becomes |
|---------|--------|---------|
| drag a building from the palette onto a tile | `CreateAgent` | `POST /v1/projects/{id}/agents` + a placement |
| drag an existing building | `MovePlacement` | `PUT /v1/projects/{id}/world/placements/{node}` |
| drag from one building to another | `CreateConnection` | `POST /v1/connections`, kind inferred from the pair |
| drag onto a factory | `AssignStep` | `PUT /v1/workflows/{id}/draft` |
| click a building | `OpenInspector` | opens the panel — no call |
| drop a portal | `CreateIntegration` | starts the guided connection flow |
| drop a signal tower | `CreateTrigger` | opens the trigger form |
| delete | `DeleteNode` | `DELETE`, with a confirmation naming what depends on it |

Two honesty rules, both enforced by `validate`:

- A line drawn between two things that cannot talk is drawn **broken** — dashed, amber,
  with the `validate` finding as its tooltip — rather than refused silently or accepted
  falsely. The user sees that they tried, and why it does not work.
- A workflow with `publishable: false` shows its factory unlit, and the start button says
  what is missing. Build Mode never implies something will run when it will not.

### Live Mode

The island as a projection of execution state. Read-only except for intervention.

| Domain fact | Island expression |
|-------------|-------------------|
| `agent.status = working` | character at its building, work animation, chimney smoke |
| `agent.status = idle` / `offline` / `error` / `paused` | idles outside / shuttered / red glow, slumped / asleep |
| `execution.status = running` | the workflow's factory lit, conveyor moving |
| `workflow.step_dispatched` | a crate leaves the factory towards the assignee's building |
| `workflow.step_started` | character walks in, work animation |
| `run.step_finished` | one progress notch on the building's banner |
| `workflow.step_succeeded` | a resource token flows along the connection to the next step |
| `workflow.step_failed` | smoke puff, red flash, the crate stops on the road |
| **`approval.requested`** | **the town hall rings; a queue forms; the branch visibly stalls** |
| `approval.granted` | the gate opens and the crate moves on |
| `tool.invoked` | a beam from the building to the integration portal |
| `execution.status = waiting_input` | a question mark over the factory |

Interventions available from Live Mode, each one an ordinary API call: approve or deny from
the town hall, retry a failed step from the stopped crate, cancel an execution, answer a
`waiting_input` prompt.

Every animation above is keyed to a row in `events`. The projector takes `recent: Event[]`
and emits `AnimationIntent`s; it has no timer and invents nothing. A Playwright test
ingests one `workflow.step_dispatched` and asserts the scene receives a `walk_to` for that
entity — the same test the previous roadmap specified, against the new event.

### The layout is its own persisted model

`world_placements`, keyed on `agent_id`, becomes four tables — `world_layouts`,
`world_districts`, `world_nodes`, `world_routes` — specified in
`01-DOMAIN-AND-PROTOCOL.md` §10. They hold position, size, appearance, grouping and visual
routing, and **no column that mirrors execution state**.

What that buys the frontend:

- **Dragging a building is a `world.update` call and nothing else.** No event, no audit row,
  no agent mutation, and the weakest role that can change anything at all
  (`05-PERMISSIONS.md` §2). A `viewer` watching an execution cannot rearrange the island
  under the person building it.
- **An offline, archived or broken agent keeps its node and stays visible.** The shuttered
  building *is* the information. An island that empties itself when things go wrong would be
  useless at exactly the moment it matters.
- **Rerouting a road changes no grant.** `world_routes.waypoints` is how a line is drawn;
  `connections` is whether the relationship exists.
- **Status arrives at render time**, as `projectLive`'s second argument, never from the
  layout tables.

Autolayout runs on creation so the island is never empty, and a manual placement sets
`auto_assigned = false` and is then never overwritten — the existing rule, unchanged.

### Island API

| Method | Path | Role |
|--------|------|------|
| `GET` | `/v1/projects/{id}/world` | viewer+ |
| `PUT` | `/v1/projects/{id}/world/layout` | member+ |
| `PUT` | `/v1/projects/{id}/world/nodes/{kind}/{node_id}` | member+ |
| `DELETE` | `/v1/projects/{id}/world/nodes/{kind}/{node_id}` | member+ |
| `PUT` | `/v1/projects/{id}/world/routes/{kind}/{route_id}` | member+ |
| `GET` `POST` `PATCH` `DELETE` | `/v1/projects/{id}/world/districts` | member+ |
| `POST` | `/v1/projects/{id}/world/autolayout` | member+ |

`DELETE` on a node removes the placement, not the agent. The confirmation dialog says so,
because "delete" on a building is the single most likely place for a user to destroy
something they meant to tidy away.

### The prototype milestone, and what it deliberately omits

The island ships early — Phases 3 and 4 in `04-ROADMAP-AND-MVP.md` — on the **real**
`projector/` + `interpreter/` + `dispatch/` architecture, so the prototype is extended
rather than replaced. Build Mode and Live Mode exist as distinct concepts from the first
commit even while each does little.

**Milestone I1 (Phase 3) — the island renders the real backend**

| Capability | Shape at I1 |
|-----------|-------------|
| Display actual backend objects | agents from `GET /v1/projects/{id}/agents`, placed from `world_nodes` |
| Agents as buildings and characters | one building sprite per `building_kind`, one character per `character_kind`; small sets |
| Select an object | click → `OpenInspector` intent → the same panel the agents page uses |
| Open configuration panels | the real agent form in Simple mode, not an island-specific one |
| Display actual agent statuses | a tint and a badge per status, from live WebSocket deltas |
| Build / Live distinction | a toggle. Build allows select and move; Live is read-only and shows status |

**Milestone I2 (Phase 4) — the island becomes a workspace**

| Capability | Shape at I2 |
|-----------|-------------|
| Add objects | drag from a palette → `CreateAgent` → the ordinary `POST` |
| Move objects | drag → `MovePlacement` → `PUT …/world/nodes/…` |
| Display connections | straight or waypointed routes from `connections`, drawn per `world_routes` |
| Create connections | drag building to building → `CreateConnection`, kind inferred |
| Live step progress | the executing step's building is lit; its name is on the banner |
| Broken connections | drawn dashed and amber with the `validate` finding as the tooltip |

**Explicitly not at I1 or I2**, and the omission is the point: walk cycles, resource
tokens flowing along roads, chimney smoke, crates, the town-hall bell, camera easing,
object pooling, districts, custom themes, sound. Those are Phase 9. The prototype exists to
prove the **interaction model** — that a user can understand and manipulate an ecosystem
spatially, and that real backend state drives it — not to look finished. Building the
animation system first would mean tuning easing curves before knowing whether anyone can
find the configure button.

One consequence worth stating: at I1 and I2 the island is **not** the recommended way to do
anything. The plain pages do everything it does and more. That is deliberate — it keeps the
island optional while it is still unproven, and it means the Phase 8 workspace is not
racing to catch up with it.

## 6. The web workspace

**Usable entirely without the island**, which is a product requirement and a test. Every
object has a conventional list, detail and form; the island is a second view of the same
data, and a user who never opens it can build and run everything.

```
/w/[workspace]/p/[project]
  ├── overview            counts, attention queue, recent executions
  ├── world               the island — Build / Live toggle
  ├── agents              list → detail: definition, tools, knowledge, runs, history
  ├── workflows           list → editor: graph (DAG canvas) + step inspector + validate
  ├── executions          list → timeline: steps, inputs, outputs, events, retry
  ├── approvals           the pending queue, decide inline
  ├── integrations        connect, OAuth, discovered tools, test
  ├── knowledge           sources, documents, index status
  ├── triggers            schedules, webhook URLs, event subscriptions
  └── settings            members, API keys, credentials, model profiles, world theme
```

Note that the workflow editor is a plain DAG canvas, not the island. The island is for
understanding and watching an ecosystem; a 14-step workflow is edited more comfortably on
a left-to-right graph. Build Mode places and connects *components*; the workflow editor
arranges *steps*. Forcing one to do both makes both worse.

### Simple and advanced modes

Every form has both, and the split is declared per field in one schema rather than decided
per form:

| | Simple | Advanced |
|---|--------|----------|
| Agent | name, what it does (one line), what it should do (prose), a "Fast / Careful" choice, tool checkboxes | full instructions, model profile, parameter overrides, `max_turns`, timeout, dispatch config, labels |
| Workflow step | "who does this", "what do they get", "needs approval?" | `input_mapping` expressions, `max_attempts`, timeout, `on_failure`, conditions |
| Integration | a guided connect button and a plain-language scope list | endpoints, headers, HMAC, discovered schemas |
| Trigger | "every weekday morning" | cron, timezone, overlap policy |

Two rules that keep this from becoming two products:

- **Simple mode never writes a value Advanced cannot show.** "Every weekday morning" is
  stored as `0 7 * * 1-5`, and Advanced shows that string. No hidden state, no setting that
  only one mode can express, so a user who graduates loses nothing.
- **Simple mode's defaults are safe, not clever.** `requires_approval` defaults on for any
  `consequential` tool. A beginner's first workflow asks before it sends.

The word "LLM" does not appear in Simple mode. Neither does "token", "temperature",
"prompt", "schema" or "webhook". That is a copy rule with a lint check on the
simple-mode string catalogue, because it is the first thing that erodes.

## 7. The conversational ecosystem builder

A *writer* of the domain model, which is why the complete version comes after the model
settles — built before that, it is rewritten with every change to it.

But it is also the product's clearest differentiator, so a **deliberately limited
prototype ships in Phase 4**, on the final architecture rather than a temporary one. §7.1
sets out how the capability is narrowed without the design being narrowed.

### Propose, then apply — never one step

```
POST /v1/builder/sessions/{id}/messages
   "I want an agent that researches competitors every morning, sends findings to a
    writer, and asks me to approve the article before publishing."

-> streams back a PLAN, and creates nothing:

{ "plan_id": "plan-5d2c",
  "summary": "Two agents, one four-step workflow, a weekday 07:00 schedule, and an
              approval before anything is posted.",
  "reuses": [ { "kind": "integration", "id": "…", "name": "Slack (workspace)",
                "why": "already connected, and has the channel you mentioned" } ],
  "creates": [
    { "kind": "agent", "name": "Competitor researcher", "mode": "managed",
      "model_profile": "Careful reasoning", "tools": ["web_search"] },
    { "kind": "agent", "name": "Brief writer", "mode": "managed",
      "model_profile": "Careful reasoning", "tools": [] },
    { "kind": "workflow", "name": "Competitor brief", "steps": [
        { "key": "research", "kind": "agent_task", "agent": "Competitor researcher" },
        { "key": "write",    "kind": "agent_task", "agent": "Brief writer",
          "input_from": "research" },
        { "key": "approve",  "kind": "approval",
          "title": "Post the competitor brief?" },
        { "key": "post",     "kind": "tool_call", "tool": "send_slack_message" } ] },
    { "kind": "connection", "kind_detail": "grant_tool",
      "from": "Competitor researcher", "to": "web_search" },
    { "kind": "trigger", "kind_detail": "schedule", "cron": "0 7 * * 1-5",
      "timezone": "Europe/Lisbon" } ],
  "requires_consent": [
    { "code": "consequential_tool",
      "detail": "“Brief writer” will be able to post to Slack. Every post is gated by
                 the approval step above." } ],
  "validation": { "publishable": true, "findings": [] },
  "estimated_cost_note": "Roughly 20k tokens per run on the chosen profile." }
```

The user sees it as a diagram on the island and as a list, edits it conversationally
("make it 6am", "use the fast model for research") or by hand, and only then:

```
POST /v1/builder/sessions/{id}/apply  { "plan_id": "plan-5d2c" }
```

### What the builder may and may not do

| | |
|---|---|
| Writes through | the ordinary service layer — the same `authorize()`, the same audit rows, the same validation |
| Reuses | existing agents, tools, integrations and knowledge where compatible, and says which and why |
| Creates in | **one transaction.** A half-built ecosystem is worse than none |
| Always leaves | workflows **unpublished** and triggers **disabled**. Applying a plan never starts anything |
| May never | create or modify a credential, widen an existing grant, enable a trigger, publish a workflow, or grant a `consequential` tool without an explicit `requires_consent` the user accepted |
| May never | exceed the user's own role. A `member` applying a plan cannot create an integration, and the plan says so instead of failing at apply |
| Is bounded by | a per-plan object cap and a per-session rate limit, so one sentence cannot create 400 agents |

`validation.publishable` comes from the same `POST /workflows/{id}/validate` the UI calls,
run against the proposed graph before the plan is shown. The builder therefore cannot
propose an ecosystem that will not run and call it done.

### 7.1 The early prototype, and how it extends rather than gets replaced

The prototype is the full design with a **smaller tool surface**. Nothing about the plan
schema, the propose/apply split, the transaction boundary or the permission model is
provisional.

```python
# domain/builder/capabilities.py — pure, and the only thing that changes per phase.
class BuilderCapability(StrEnum):
    CREATE_AGENT      = "create_agent"        # Phase 4
    CREATE_CONNECTION = "create_connection"   # Phase 4
    CREATE_TEAM       = "create_team"         # Phase 4
    CREATE_WORKFLOW   = "create_workflow"     # Phase 10
    CREATE_TRIGGER    = "create_trigger"      # Phase 10
    CREATE_INTEGRATION = "create_integration" # Phase 10
    CREATE_KNOWLEDGE  = "create_knowledge"    # Phase 10
    MODIFY_EXISTING   = "modify_existing"     # Phase 10
    GRANT_TOOL        = "grant_tool"          # Phase 10
```

The enabled set is configuration. The model is handed tool definitions for exactly the
enabled capabilities, and a plan containing anything else is rejected by the plan validator
before a user ever sees it — so the prototype cannot half-propose a workflow and fail at
apply.

**What the prototype can do (Phase 4)**

| Capability | Example |
|-----------|---------|
| Create agents from natural language | *"a researcher and a writer"* → two `managed` agents with roles, instructions and a default model profile |
| Create simple connections | a `delegate` connection from researcher to writer |
| Create a basic multi-agent team | the two above, plus a two-step sequential workflow compiled from the delegation — see below |
| Present a structured proposal | the same `plan` object the complete builder returns, rendered as a list and previewed on the island |
| Confirm or reject | `POST …/apply` with the `plan_id`, or discard. Nothing is created until then |
| Show the result in the workspace | the applied objects appear on the island and in the agents list, via the ordinary realtime deltas |

**`create_team` compiles to a real workflow.** This is the decision that keeps the
prototype from becoming throwaway architecture: a "team" is not a new entity and not a
special execution path. The builder emits a `workflow_version` with one `agent_task` step
per member, wired in the order the delegation implies, and it runs on the same orchestrator
as everything else. There is exactly one execution engine in the product at every point in
its life.

**What the prototype cannot do**, and says so in plain language when asked: workflows with
branches or approvals, integrations, triggers, knowledge sources, tool grants, or modifying
anything that already exists. Asked for one, it answers what it can build now and what has
to be done by hand — rather than attempting it and producing something that fails
`validate`.

**What does not change between the prototype and the complete version**

- the plan schema, including `reuses`, `creates`, `requires_consent` and `validation`
- propose-then-apply, with nothing created on propose
- one transaction on apply; a half-built ecosystem is worse than none
- workflows left unpublished and triggers left disabled
- `builder.apply` bounded by the caller's own permissions, refused at plan time
- per-plan object caps and per-session rate limits
- `services/builder.py` as a client of the ordinary services, with no privileged path

So Phase 10 adds capabilities to an enum and tool definitions to a prompt. It does not
revisit the architecture, which is the whole reason the prototype is worth shipping early.

### Architecturally

`services/builder.py` is a *client of the control plane's own services*, not a privileged
path into the repositories. It uses a model through `infra/providers` with the domain
schemas as its tool definitions — `create_agent`, `create_workflow`, `connect`,
`add_trigger` — so the thing constraining it is the same validation that constrains a
human. It never touches Phaser or `WorldState`: it emits domain objects, and the island
shows them because `projectBuild` projects them. The brief's *"It should not directly
manipulate Phaser objects as the source of truth"* is satisfied by the builder having no
access to them at all.
