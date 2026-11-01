# Revised domain model and event protocol

> Proposal, 2026-11-01. Deliverables 4 and 5.

## 1. Shape of the model

Four groups, and the boundaries between them are the design.

```
TENANCY            unchanged, built in Phase 1
  users, workspaces, workspace_members, projects, api_keys, audit_log

DEFINITION         what the user builds. Edited through the control plane.
  agents, model_profiles, workflows, workflow_versions, workflow_steps,
  workflow_edges, connections, tools, integrations, credentials,
  knowledge_sources, knowledge_documents, triggers

EXECUTION          what actually ran. Written by reducers and the orchestrator.
  workflow_executions, execution_steps, approvals, dispatches,
  tasks, runs, run_steps, agent_status_history,
  events, logs, metric_samples          (append-only, partitioned)

PRESENTATION       how it looks. Touched by no reducer, adapter or SDK.
  world_layouts, world_placements
```

| Group | Written by | Replayable | Retention |
|-------|-----------|------------|-----------|
| Tenancy | control plane | n/a | forever |
| **Definition** | **control plane only** | **no — versioned instead** | forever |
| Execution (current state) | **reducers and the orchestrator only** | rebuilt by replay | forever |
| Execution (append-only) | ingest | **is the replay source** | 30–90 days, partitioned |
| Presentation | the UI | no | forever |

The row that matters most is Definition. **A definition change is never an event.**
It is an audited control-plane mutation, versioned where it affects running work, and
published to the realtime bus as a state delta. `00-GAP-ANALYSIS.md` §4 C2 explains why
putting it in the replayable log destroys replay equivalence.

## 2. Agents

One table, two halves, and the split is enforced by which layer may write each column.

```sql
CREATE TYPE agent_interaction_mode AS ENUM ('managed', 'dispatchable', 'observed');

CREATE TABLE agents (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id   uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id     uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_key      citext NOT NULL,
  interaction_mode agent_interaction_mode NOT NULL,

  -- ---- definition: user-editable, control plane only -------------------
  display_name   text NOT NULL,
  role           text,              -- "Competitor researcher". One line, shown everywhere.
  description    text,
  instructions   text,              -- the system prompt for a managed agent
  model_profile_id uuid REFERENCES model_profiles(id) ON DELETE RESTRICT,
  model_overrides jsonb NOT NULL DEFAULT '{}'::jsonb,
  kind           text,
  labels         jsonb NOT NULL DEFAULT '{}'::jsonb,

  -- ---- declared interface ----------------------------------------------
  -- What this agent can be asked to do. For `managed`, AgentWorld sets it.
  -- For `dispatchable`, the agent advertises it and the API refuses commands
  -- and step assignments outside it, rather than issuing them hopefully.
  capabilities   text[] NOT NULL DEFAULT '{}',
  dispatch       jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- {"mode":"poll"} | {"mode":"push","url":"...","credential_id":"..."}
  runtime        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- descriptive only

  -- ---- runtime state: reducers only ------------------------------------
  status         agent_status NOT NULL DEFAULT 'provisioning',
  status_reason  text,
  current_task_id uuid,
  last_heartbeat_at timestamptz,
  last_event_at  timestamptz,
  heartbeat_ttl_seconds int NOT NULL DEFAULT 60,

  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  registered_at  timestamptz NOT NULL DEFAULT now(),
  archived_at    timestamptz,
  UNIQUE (project_id, agent_key)
);
```

### The three modes, and what each honestly supports

| | `managed` | `dispatchable` | `observed` |
|---|---|---|---|
| Who runs the loop | **AgentWorld** | the customer's process | the customer's process |
| Instructions live in | `agents.instructions` | their code | their code |
| Can be given a workflow step | yes | yes | **no** |
| Pause / resume / cancel | **really** | only if advertised | no |
| Tools | granted in AgentWorld, executed by AgentWorld | their own | their own |
| Appears on the island | yes | yes | yes |
| Created by the conversational builder | yes | — | — |

`observed` is the entire premise of the previous design, preserved: an agent that only
reports. It gets a building, a status and a history, and **cannot be assigned an
execution step**. That is a validation rule in `domain/orchestration/validate.py`, not a
UI nicety — attempting it is a `422` naming the mode, and the builder will not propose it.

`agent_status` gains `paused`:
```sql
ALTER TYPE agent_status ADD VALUE 'paused';  -- irreversible; see migration 0002's note
```

### Model profiles — where provider independence is kept

```sql
CREATE TABLE model_profiles (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  name          text NOT NULL,            -- "Fast drafting", "Careful reasoning"
  provider_slug text NOT NULL,            -- resolved against the provider registry
  model_id      text NOT NULL,
  parameters    jsonb NOT NULL DEFAULT '{}'::jsonb,  -- validated by the provider plugin
  credential_id uuid REFERENCES credentials(id) ON DELETE RESTRICT,
  is_default    boolean NOT NULL DEFAULT false,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workspace_id, name)
);
CREATE UNIQUE INDEX one_default_model_profile
  ON model_profiles (workspace_id) WHERE is_default;
```

A beginner never sees this table: they pick "Fast" or "Careful" from profiles the
workspace already has, and `model_overrides` on the agent stays empty. An advanced user
edits the profile or overrides per agent. `provider_slug` and `parameters` are opaque
strings and JSON to the domain layer — the provider plugin in
`infra/providers/<slug>.py` owns their meaning, which is how `domain/` keeps importing no
provider SDK (K15).

The partial unique index is the same trick as `workspace_single_owner`: "exactly one
default" becomes a database guarantee rather than a service-layer intention.

## 3. Workflows, edges and connections — the two kinds of line

### Workflow definitions are versioned

```sql
CREATE TABLE workflows (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  slug          citext NOT NULL,
  name          text NOT NULL,
  description   text,
  current_version_id uuid,          -- FK added after workflow_versions
  enabled       boolean NOT NULL DEFAULT false,
  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  archived_at   timestamptz,
  UNIQUE (project_id, slug)
);

CREATE TABLE workflow_versions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_id   uuid NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  version       int  NOT NULL,
  -- The whole graph, frozen. Steps and edges below are the queryable
  -- projection of it; this is the authority an execution is bound to.
  graph         jsonb NOT NULL,
  input_schema  jsonb NOT NULL DEFAULT '{}'::jsonb,
  published_at  timestamptz,
  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (workflow_id, version)
);
```

**Why versions.** An execution references a `workflow_version_id`, never a workflow. A
user editing a workflow while it runs must not change what the running copy means —
otherwise a step can complete against a definition that no longer has that step, and
replay has nothing stable to fold against. Editing produces a new draft version;
publishing moves `current_version_id`; running executions finish on the version they
started with.

```sql
CREATE TYPE step_kind AS ENUM (
  'agent_task',    -- hand work to an agent (managed or dispatchable)
  'tool_call',     -- call one tool directly, no agent
  'approval',      -- stop and wait for a human
  'condition',     -- branch on an expression over prior outputs
  'transform',     -- pure reshaping of data between steps
  'wait'           -- for a duration or an external signal
);

CREATE TABLE workflow_steps (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_version_id uuid NOT NULL REFERENCES workflow_versions(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  step_key      citext NOT NULL,       -- stable within the version, e.g. "research"
  name          text NOT NULL,
  kind          step_kind NOT NULL,
  agent_id      uuid REFERENCES agents(id) ON DELETE RESTRICT,
  tool_id       uuid REFERENCES tools(id) ON DELETE RESTRICT,
  config        jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- How this step's input is built from the execution input and prior step
  -- outputs. Declarative on purpose: an expression language the orchestrator
  -- evaluates, not code it executes.
  input_mapping jsonb NOT NULL DEFAULT '{}'::jsonb,
  max_attempts  int NOT NULL DEFAULT 1,
  timeout_seconds int,
  on_failure    text NOT NULL DEFAULT 'fail',  -- 'fail' | 'continue' | 'route'
  UNIQUE (workflow_version_id, step_key),
  CONSTRAINT agent_task_needs_agent
    CHECK (kind <> 'agent_task' OR agent_id IS NOT NULL),
  CONSTRAINT tool_call_needs_tool
    CHECK (kind <> 'tool_call'  OR tool_id  IS NOT NULL)
);

CREATE TABLE workflow_edges (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workflow_version_id uuid NOT NULL REFERENCES workflow_versions(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  from_step_id  uuid REFERENCES workflow_steps(id) ON DELETE CASCADE,  -- NULL = entry
  to_step_id    uuid NOT NULL REFERENCES workflow_steps(id) ON DELETE CASCADE,
  condition     jsonb,                -- NULL = unconditional
  label         text,                 -- "approved" / "rejected", drawn on the island
  UNIQUE (workflow_version_id, from_step_id, to_step_id, label)
);
```

### Connections are standing grants between components

```sql
CREATE TYPE connection_kind AS ENUM (
  'grant_tool',       -- agent  -> tool            : may call it
  'grant_knowledge',  -- agent  -> knowledge_source: may search it
  'delegate',         -- agent  -> agent           : may hand work over
  'trigger',          -- workflow -> workflow      : completion starts the other
  'publish'           -- component -> integration  : may send through it
);

CREATE TABLE connections (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  kind          connection_kind NOT NULL,
  source_kind   text NOT NULL,        -- 'agent' | 'workflow' | 'tool' | ...
  source_id     uuid NOT NULL,
  target_kind   text NOT NULL,
  target_id     uuid NOT NULL,
  config        jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- A connection may itself require approval: "this agent may send email,
  -- but every send is gated". The gate lives with the grant, not with the UI.
  requires_approval boolean NOT NULL DEFAULT false,
  enabled       boolean NOT NULL DEFAULT true,
  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, kind, source_kind, source_id, target_kind, target_id)
);
```

Polymorphic references rather than five pairs of nullable foreign keys: the set of
connectable kinds grows with the product, and the island draws them uniformly. The cost
is that the database cannot enforce referential integrity on `source_id`/`target_id`, so
the service layer validates both ends exist in the same project, and a weekly job reports
orphans. That trade is stated here because it is the one place in this model where a
foreign key is given up.

**A connection is a capability, not a line.** `grant_tool` is what makes a tool callable
by an agent. The orchestrator checks it before dispatching a `tool_call`; a managed
agent's available tool list is computed from it. The brief's *"Do not treat a visual line
as proof that two components can actually communicate"* is satisfied by the line being
read *from* the grant rather than the grant being inferred from the line.

## 4. Tools, integrations and triggers

```sql
CREATE TYPE tool_source AS ENUM ('builtin', 'mcp', 'integration');
CREATE TYPE side_effect AS ENUM ('read', 'write', 'consequential');

CREATE TABLE tools (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  source        tool_source NOT NULL,
  integration_id uuid REFERENCES integrations(id) ON DELETE CASCADE,
  name          citext NOT NULL,        -- as the model sees it: "send_slack_message"
  display_name  text NOT NULL,
  description   text NOT NULL,          -- the model reads this; it is functional, not decoration
  input_schema  jsonb NOT NULL,         -- JSON Schema, handed to the provider as a tool definition
  output_schema jsonb,
  -- 'read' may run freely; 'write' is logged prominently; 'consequential'
  -- defaults to requiring approval. The default is per tool, the decision is
  -- per grant.
  side_effect   side_effect NOT NULL DEFAULT 'write',
  requires_approval_default boolean NOT NULL DEFAULT false,
  enabled       boolean NOT NULL DEFAULT true,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, name)
);
```

**MCP is a first-class tool source, and it is the highest-leverage choice in this
document.** One MCP client implementation turns every MCP server into a set of tools with
no AgentWorld code per service. `source = 'mcp'` rows are *discovered* from a connected
server and refreshed, rather than hand-written. Writing a bespoke adapter per service
(Gmail, Drive, Notion, …) is the alternative, and it does not finish.

`integrations` extends the existing design: `adapter_slug` keeps its meaning for inbound
normalisation, and gains a `credential_id` and a `direction`.

```sql
CREATE TYPE trigger_kind AS ENUM (
  'manual', 'schedule', 'webhook', 'integration_event', 'workflow_completed'
);

CREATE TABLE triggers (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_id   uuid NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  kind          trigger_kind NOT NULL,
  config        jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- {"cron":"0 7 * * 1-5","timezone":"Europe/Lisbon"}
  -- {"integration_id":"...","event":"message.channels"}
  webhook_token text,                  -- for kind='webhook'; the URL's unguessable half
  enabled       boolean NOT NULL DEFAULT true,
  last_fired_at timestamptz,
  next_fire_at  timestamptz,           -- maintained by the scheduler for 'schedule'
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, workflow_id, kind, webhook_token)
);
CREATE INDEX triggers_due_idx ON triggers (next_fire_at)
  WHERE enabled AND kind = 'schedule';
```

A partial index on due triggers, for the same reason `agents_heartbeat_watch` has one: the
scheduler must not scan the table every tick.

## 5. Knowledge

```sql
CREATE TABLE knowledge_sources (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name          text NOT NULL,
  kind          text NOT NULL,         -- 'upload' | 'integration_folder' | 'url' | 'database'
  integration_id uuid REFERENCES integrations(id) ON DELETE SET NULL,
  config        jsonb NOT NULL DEFAULT '{}'::jsonb,
  document_count int NOT NULL DEFAULT 0,
  last_indexed_at timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE knowledge_documents (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  source_id     uuid NOT NULL REFERENCES knowledge_sources(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  external_ref  text,
  title         text NOT NULL,
  content       text NOT NULL,
  content_hash  text NOT NULL,         -- skip re-embedding unchanged documents
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  indexed_at    timestamptz,
  UNIQUE (source_id, content_hash)
);
```

Chunks and embeddings are deliberately **not** in v1's schema. Retrieval in v1 is a
`knowledge_search` builtin tool over PostgreSQL full-text search, which needs no vector
extension, no embedding provider and no re-index pipeline, and is adequate for the
document volumes a first user has. `pgvector` and a chunk table are a Phase-9 change
behind the same tool interface — the agent calls `knowledge_search`, and what is behind
it is not part of the contract.

## 6. Credentials — the separate mechanism

```sql
CREATE TYPE credential_kind AS ENUM ('api_key', 'oauth2', 'basic', 'custom');

CREATE TABLE credentials (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid REFERENCES projects(id) ON DELETE CASCADE,
  name          text NOT NULL,
  kind          credential_kind NOT NULL,
  provider_slug text NOT NULL,
  -- Envelope-encrypted. `ciphertext` is AES-256-GCM over the secret material
  -- under a per-row data key; `wrapped_key` is that data key encrypted by the
  -- master key named in `key_id`. Rotating the master re-wraps the data keys
  -- without touching, or decrypting, the secrets themselves.
  ciphertext    bytea NOT NULL,
  nonce         bytea NOT NULL,
  wrapped_key   bytea NOT NULL,
  key_id        text  NOT NULL,
  -- Readable facts, so the UI can show something useful without decrypting.
  display_hint  text,                  -- "xoxb-…4f2a", "ada@example.com"
  scopes        text[],
  expires_at    timestamptz,
  refresh_after timestamptz,
  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz,
  revoked_at    timestamptz,
  UNIQUE (workspace_id, provider_slug, name)
);
```

Five rules, each one a thing that otherwise goes wrong:

1. **No endpoint ever returns `ciphertext`, `nonce` or `wrapped_key`.** As with `ApiKey`,
   the domain entity has no field for them, so there is nowhere for a response model or a
   log line to pick them up. Only `infra/secrets/` sees them.
2. **Decryption is by `credential_id` at the point of use**, inside the tool or provider
   call, never earlier. A credential is not loaded into an execution's context.
3. **Credentials are never in events, step inputs or step outputs.** Tool schemas declare
   `secret_fields`; the executor redacts them before the result is persisted or
   published. A test asserts a known secret never appears in `events`, `logs`,
   `execution_steps` or `dispatches` — the same shape as the Phase 1 test that searched
   every text column for a key plaintext.
4. **`api_keys` is untouched.** AgentWorld's own credentials stay Argon2id-hashed and
   unrecoverable. Two mechanisms, because one must be verifiable and the other usable.
5. **The master key comes from the environment** (`CREDENTIAL_MASTER_KEY`), and
   `Settings` refuses to boot without it outside local and test — the pattern
   `API_KEY_PEPPER` already uses. `SecretStore` is an interface, so KMS or Vault is a
   deployment choice.

## 7. Execution state

```sql
CREATE TYPE execution_status AS ENUM (
  'pending', 'running', 'waiting_approval', 'waiting_input',
  'paused', 'completed', 'failed', 'cancelled'
);
CREATE TYPE step_status AS ENUM (
  'pending', 'ready', 'dispatched', 'running',
  'succeeded', 'failed', 'skipped', 'cancelled'
);

CREATE TABLE workflow_executions (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_id   uuid NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
  -- The frozen version, not the workflow: editing a definition must not
  -- change what a running copy means.
  workflow_version_id uuid NOT NULL REFERENCES workflow_versions(id) ON DELETE RESTRICT,
  status        execution_status NOT NULL DEFAULT 'pending',
  trigger_id    uuid REFERENCES triggers(id) ON DELETE SET NULL,
  triggered_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  idempotency_key citext,          -- a trigger firing twice yields one execution
  input         jsonb NOT NULL DEFAULT '{}'::jsonb,
  output        jsonb,
  error         jsonb,
  started_at    timestamptz,
  finished_at   timestamptz,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, idempotency_key)
);
CREATE INDEX executions_live_idx ON workflow_executions (project_id, status, created_at DESC)
  WHERE status IN ('pending','running','waiting_approval','waiting_input','paused');

CREATE TABLE execution_steps (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  execution_id  uuid NOT NULL REFERENCES workflow_executions(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  workflow_step_id uuid NOT NULL REFERENCES workflow_steps(id) ON DELETE RESTRICT,
  step_key      citext NOT NULL,       -- denormalised, so a timeline needs no join
  status        step_status NOT NULL DEFAULT 'pending',
  attempt       int NOT NULL DEFAULT 0,
  agent_id      uuid REFERENCES agents(id) ON DELETE SET NULL,
  task_id       uuid REFERENCES tasks(id) ON DELETE SET NULL,
  input         jsonb,
  output        jsonb,
  error         jsonb,
  started_at    timestamptz,
  finished_at   timestamptz,
  UNIQUE (execution_id, workflow_step_id, attempt)
);

CREATE TYPE approval_decision AS ENUM ('pending', 'granted', 'denied', 'expired');

CREATE TABLE approvals (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  execution_id  uuid NOT NULL REFERENCES workflow_executions(id) ON DELETE CASCADE,
  execution_step_id uuid NOT NULL REFERENCES execution_steps(id) ON DELETE CASCADE,
  decision      approval_decision NOT NULL DEFAULT 'pending',
  title         text NOT NULL,         -- "Send this email to 412 recipients?"
  summary       text,
  payload       jsonb NOT NULL DEFAULT '{}'::jsonb,  -- redacted; what is being approved
  required_role member_role NOT NULL DEFAULT 'member',
  requested_at  timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz,
  decided_at    timestamptz,
  decided_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  decision_reason text,
  UNIQUE (execution_step_id)
);
CREATE INDEX approvals_pending_idx ON approvals (project_id, requested_at)
  WHERE decision = 'pending';
```

An approval is a row that must exist and be `granted` before a dispatch exists. There is
no code path from `waiting_approval` to a dispatched step that does not pass through it.
That is what makes it a gate rather than a notification, and it is structural rather than
remembered.

`dispatches` — the outbox — is specified in `02-ORCHESTRATION.md` §3, since it is part of
the orchestration mechanism rather than of the domain.

`tasks`, `runs`, `run_steps`, `agent_status_history`, `events`, `logs` and
`metric_samples` keep the shapes in `docs/02-DATABASE.md` §5, with one addition each on
`tasks` and `runs`: a nullable `execution_step_id`, so a task materialised by a workflow
knows where it came from and a standalone task has nothing there.

## 8. Entity relationships

```
workspace ─┬─ projects ─┬─ agents ──────────┬─ agent_status_history
           │            │     │             └─ runs ─ run_steps
           │            │     ├─ (model_profiles, workspace-level)
           │            │     └─ connections ─┬─ tools ─ integrations ─ credentials
           │            │                     └─ knowledge_sources ─ knowledge_documents
           │            │
           │            ├─ workflows ─ workflow_versions ─┬─ workflow_steps
           │            │                                 └─ workflow_edges
           │            │        │
           │            │        └─ workflow_executions ─┬─ execution_steps ─┬─ approvals
           │            │                                │                   ├─ tasks
           │            │                                │                   └─ dispatches
           │            │                                └─ (events, logs)
           │            ├─ triggers ──> workflows
           │            └─ world_layouts, world_placements      [presentation]
           ├─ api_keys
           ├─ credentials
           └─ audit_log
```

Every table under `projects` carries `workspace_id` *and* `project_id`, as the existing
convention requires, so a tenant filter is always a single-table predicate and a missing
one is visible in review. All of them join `RLS_PROTECTED_TABLES`.

## 9. The event protocol

### Envelope

```jsonc
{
  "dedup_key": "exec-9f2a:step-research:1",
  "type": "workflow.step_succeeded",
  "protocol_version": "1.0",
  "occurred_at": "2026-11-04T07:00:11.204Z",

  // What this event is about. Replaces the flat agent_key/task_ref/run_ref
  // trio: an execution, a step and an approval are all subjects, and bolting
  // each onto an agent-shaped envelope yields six optional ids and no room
  // for a seventh.
  "subject": { "kind": "execution_step", "ref": "exec-9f2a:research" },

  // Everything else this event relates to. Optional, and the reducers use it
  // to resolve rows without the subject having to be a composite.
  "correlation": {
    "agent_key": "researcher",
    "execution_ref": "exec-9f2a",
    "workflow_slug": "competitor-brief",
    "task_ref": null,
    "run_ref": "run-3f9a"
  },

  "severity": "info",
  "labels": { "region": "eu-west-1" },
  "payload": { "output_summary": "6 competitors, 14 sources", "tokens": 8421 }
}
```

`subject.kind ∈ {agent, task, run, workflow_execution, execution_step, approval,
tool_call, project}`. Everything else from the previous envelope is unchanged, including
the rules that matter: `dedup_key` unique per project, client `occurred_at` against server
`received_at`, unknown types and fields stored rather than rejected, and no provider or UI
concept anywhere in it.

### Taxonomy

| Namespace | Types | Folds into | New? |
|-----------|-------|-----------|------|
| `agent` | `registered`, `heartbeat`, `status_changed`, `capability_changed`, `paused`, `resumed`, `deregistered` | `agents` | +3 |
| `task` | `created`, `assigned`, `started`, `progress`, `blocked`, `unblocked`, `completed`, `failed`, `cancelled` | `tasks` | — |
| `run` | `started`, `step_started`, `step_finished`, `finished`, `failed` | `runs`, `run_steps` | — |
| `tool` | `invoked`, `returned`, `failed`, `denied` | `runs` counters, `events` | +1 |
| **`workflow`** | `execution_started`, `step_ready`, `step_dispatched`, `step_started`, `step_succeeded`, `step_failed`, `step_skipped`, `step_retried`, `execution_completed`, `execution_failed`, `execution_cancelled`, `execution_paused`, `execution_resumed` | `workflow_executions`, `execution_steps` | **all** |
| **`approval`** | `requested`, `granted`, `denied`, `expired` | `approvals`, `execution_steps` | **all** |
| `log` | `emitted` | `logs` | — |
| `metric` | `recorded` | `metric_samples` | — |
| `custom` | `custom.<anything>` | `events` only, never folded | — |

**Not in this table, on purpose:** anything about creating or editing a definition. There
is no `agent.created` or `workflow.published` event. Those are control-plane mutations
with `audit_log` rows and realtime deltas. §1 and `00-GAP-ANALYSIS.md` C2 say why.

### Example: the MVP workflow, end to end

A scheduled trigger at 07:00 runs *competitor-brief*: research → write → approval → post.

```jsonc
// 1. the scheduler fires; the orchestrator opens an execution
{ "dedup_key": "trig-7b1:2026-11-04T07:00Z", "type": "workflow.execution_started",
  "occurred_at": "2026-11-04T07:00:00.001Z",
  "subject": { "kind": "workflow_execution", "ref": "exec-9f2a" },
  "correlation": { "workflow_slug": "competitor-brief" },
  "payload": { "trigger_kind": "schedule", "version": 3, "input": { "sector": "fintech" } } }

// 2. the orchestrator decides. The event records the decision; the dispatch
//    row that actually causes work is written in the same transaction and is
//    NOT part of this log.
{ "dedup_key": "exec-9f2a:research:0:dispatched", "type": "workflow.step_dispatched",
  "occurred_at": "2026-11-04T07:00:00.014Z",
  "subject": { "kind": "execution_step", "ref": "exec-9f2a:research" },
  "correlation": { "execution_ref": "exec-9f2a", "agent_key": "researcher" },
  "payload": { "step_kind": "agent_task", "attempt": 0, "interaction_mode": "managed" } }

// 3. the managed runner picks up the dispatch and starts work
{ "dedup_key": "exec-9f2a:research:0:started", "type": "workflow.step_started",
  "occurred_at": "2026-11-04T07:00:00.402Z",
  "subject": { "kind": "execution_step", "ref": "exec-9f2a:research" },
  "correlation": { "execution_ref": "exec-9f2a", "run_ref": "run-3f9a" }, "payload": {} }

// 4. the agent's own trace: an LLM turn, then a tool call
{ "dedup_key": "run-3f9a:step-1", "type": "run.step_finished",
  "occurred_at": "2026-11-04T07:00:04.113Z",
  "subject": { "kind": "run", "ref": "run-3f9a" },
  "payload": { "seq": 1, "kind": "llm", "tokens_in": 812, "tokens_out": 240 } }

{ "dedup_key": "run-3f9a:tool-1", "type": "tool.returned",
  "occurred_at": "2026-11-04T07:00:07.550Z",
  "subject": { "kind": "tool_call", "ref": "run-3f9a:tool-1" },
  "correlation": { "run_ref": "run-3f9a", "agent_key": "researcher" },
  "payload": { "tool": "web_search", "duration_ms": 3380, "result_count": 14 } }

// 5. the step finishes; its output is what the next step's input_mapping reads
{ "dedup_key": "exec-9f2a:research:0:succeeded", "type": "workflow.step_succeeded",
  "occurred_at": "2026-11-04T07:00:11.204Z",
  "subject": { "kind": "execution_step", "ref": "exec-9f2a:research" },
  "correlation": { "execution_ref": "exec-9f2a" },
  "payload": { "output_summary": "6 competitors, 14 sources", "tokens": 8421 } }

// 6. ... write runs the same way ... then a real gate. No dispatch exists for
//    the `post` step, and none will until a human decides.
{ "dedup_key": "exec-9f2a:approve:0:requested", "type": "approval.requested",
  "occurred_at": "2026-11-04T07:01:46.900Z",
  "subject": { "kind": "approval", "ref": "appr-21c8" },
  "correlation": { "execution_ref": "exec-9f2a" },
  "payload": { "title": "Post the competitor brief to #market-intel?",
               "required_role": "member", "expires_at": "2026-11-04T19:00:00Z" } }

{ "dedup_key": "appr-21c8:granted", "type": "approval.granted",
  "occurred_at": "2026-11-04T08:12:03.000Z",
  "subject": { "kind": "approval", "ref": "appr-21c8" },
  "correlation": { "execution_ref": "exec-9f2a" },
  "payload": { "decided_by": "ada@example.com", "reason": "looks right" } }

// 7. the consequential step, gated, then done
{ "dedup_key": "exec-9f2a:post:0:succeeded", "type": "workflow.step_succeeded",
  "occurred_at": "2026-11-04T08:12:04.880Z",
  "subject": { "kind": "execution_step", "ref": "exec-9f2a:post" },
  "correlation": { "execution_ref": "exec-9f2a" },
  "payload": { "tool": "send_slack_message", "channel": "#market-intel" } }

{ "dedup_key": "exec-9f2a:completed", "type": "workflow.execution_completed",
  "occurred_at": "2026-11-04T08:12:04.930Z",
  "subject": { "kind": "workflow_execution", "ref": "exec-9f2a" },
  "payload": { "duration_ms": 4324929, "steps_succeeded": 4, "steps_failed": 0 } }
```

Two properties to read off that trace. Every animation the island shows for this run has
a row above it — the character walking out, the progress notches, the smoke, the token
flowing to the warehouse, the town-hall bell for the approval. And re-folding those
eleven events against version 3 of the definition reproduces the final state exactly,
while sending no Slack message, because the dispatch rows are not in the log.

### Compatibility promises

Promises 1–6 in `docs/03-API.md` §5 stand unchanged, and one is added:

> 7. **An event never causes work.** Folding the log mutates state and nothing else.
>    Every side effect — a provider call, a tool invocation, an HTTP dispatch, an email —
>    originates from a `dispatches` row, which is not part of the log and is not replayed.
>    The replay test asserts it by replaying a recorded execution with the dispatch
>    workers running and asserting that no dispatch row is created.

That promise is what lets the other six survive the move from telemetry to orchestration.
