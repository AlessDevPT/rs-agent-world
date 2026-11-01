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
  world_layouts, world_districts, world_nodes, world_routes
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

### Four independent axes, and the mistake of collapsing them

"Provider-independent" is four separate statements, and conflating any two of them is how
a platform quietly becomes a single-framework product:

| Axis | The question | Implementations | Where it lives |
|------|--------------|-----------------|----------------|
| **Model provider** | Who serves the LLM? | Anthropic, OpenAI-compatible, (later) Gemini, Bedrock, local | `infra/providers/` |
| **Agent runtime** | Who executes the agent's loop? | AgentWorld's `step_runner` (`managed`); the customer's own process (`dispatchable`); nobody (`observed`) | `workers/`, or outside AgentWorld entirely |
| **Tool provider** | Who implements a capability? | builtin, MCP server, integration-provided | `infra/tools/` |
| **Communication protocol** | How do AgentWorld and an agent exchange work? | native REST ingest, HTTP push, claim-poll, or an adapter translating a third-party shape | `api/v1/ingest`, `api/v1/self`, `adapters/` |

**The axes are orthogonal, and the architecture must not assume otherwise.** The specific
assumption to avoid: *that an agent's execution framework and its model provider are the
same choice.*

Three combinations that must all work, and do:

- A **managed** agent on **Anthropic** calling an **MCP** tool hosted by a third party.
- A **dispatchable** agent the customer runs on **LangGraph** over **Gemini**, reached by
  **claim-poll**. AgentWorld dispatches work to it and folds its reports, and **never
  learns which model it used** unless the agent chooses to report it descriptively in
  `runtime`. No provider plugin is involved; no Gemini support is required.
- An **observed** agent on a framework nobody here has heard of, reporting through the
  **generic-webhook** adapter, which never executes anything.

The consequence in the schema: `model_profile_id` is meaningful **only** for a `managed`
agent, because it is the only mode where AgentWorld calls a model at all. For the other two
it must be absent, or it would imply a choice AgentWorld does not make and cannot enforce.
A check constraint says so rather than a comment:

```sql
CONSTRAINT model_profile_only_when_managed
  CHECK (interaction_mode = 'managed' OR model_profile_id IS NULL)
```

A dispatchable agent that wants to record "I run on Gemini" puts it in `runtime`, which is
descriptive JSON the core never branches on — the Phase 0 rule, unchanged.

The payoff for the roadmap: supporting externally hosted agents **does not require
supporting their model providers**. `04-ROADMAP-AND-MVP.md` §7 lists two providers in v1
and all three agent modes, and those numbers are independent precisely because of this
separation.

### The table

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
  -- Only for `managed`: see the check constraint below. The other modes
  -- choose their own model, and AgentWorld does not need to know which.
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
  UNIQUE (project_id, agent_key),
  CONSTRAINT model_profile_only_when_managed
    CHECK (interaction_mode = 'managed' OR model_profile_id IS NULL)
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

### Retrieval is an interface, not an implementation

Chunks and embeddings are deliberately **not** in v1's schema. v1 retrieves with
PostgreSQL full-text search: no vector extension, no embedding provider, no re-index
pipeline, and adequate for the document volumes a first user has.

That choice must not reach the agent or workflow contract, so retrieval goes behind an
interface from the start:

```python
# domain/knowledge/retrieval.py — pure types, no I/O, no provider.
@dataclass(frozen=True, slots=True)
class RetrievalQuery:
    text: str
    source_ids: Sequence[UUID]          # which granted sources to search
    top_k: int = 8
    filters: Mapping[str, str] = field(default_factory=dict)   # on document metadata

@dataclass(frozen=True, slots=True)
class Passage:
    document_id: UUID
    source_id: UUID
    title: str
    text: str
    score: float                        # 0..1, comparable within one result set only
    locator: str | None                 # page, heading, offset - for citation
    metadata: Mapping[str, str]
    # Provenance, carried so a later tool call can record where its arguments
    # came from. Retrieved text is untrusted input; knowing which document a
    # tool argument derived from is what makes an exfiltration attempt
    # investigable after the fact. See risk R1.
    provenance: Provenance              # source kind, external ref, retrieved_at

@dataclass(frozen=True, slots=True)
class RetrievalResult:
    passages: Sequence[Passage]
    truncated: bool
    backend: str                        # for diagnostics, never for branching

class KnowledgeRetriever(Protocol):
    async def search(self, query: RetrievalQuery) -> RetrievalResult: ...
    async def index(self, document_id: UUID) -> None: ...
    async def forget(self, document_id: UUID) -> None: ...
```

| Implementation | v1 | What changes |
|---------------|-----|--------------|
| `PostgresFullTextRetriever` | **yes** | `tsvector` over `knowledge_documents.content` |
| `PgVectorRetriever` | v1.1 | adds a chunk table, an embedding provider and a re-index job |
| `HybridRetriever` | v1.2 | full-text and vector, reciprocally rank-fused |
| `ExternalRetriever` | on demand | a vendor retrieval API behind the same Protocol |

Four properties make the swap genuinely contract-free:

- **The agent's surface is the `knowledge_search` tool**, whose input and output JSON
  Schemas are defined by `RetrievalQuery` and `RetrievalResult` above. An agent prompt, a
  workflow `input_mapping` and a step output written against v1 keep working verbatim.
- **`score` is explicitly comparable only within one result set.** A BM25 score and a
  cosine similarity are not the same number, so nothing — no step condition, no UI
  threshold — may compare one across backends. Stated here because a `score > 0.8`
  condition in somebody's workflow is exactly what would break on the swap.
- **`index` and `forget` are part of the interface**, so the re-index pipeline a vector
  backend needs has a place to live without the ingest path changing.
- **Every passage carries provenance**, and a managed agent's turn records which document
  ids were in its context when each tool call was made — on the `tool.invoked` event, as
  `payload.context_sources`. That is "provenance wherever practical": it does not prove a
  causal link between a document and an argument, and it does make "which document was in
  scope when this agent posted our customer list" a query rather than an investigation.
- **`backend` is for diagnostics.** A test asserts no module outside `infra/knowledge/`
  reads it, so it cannot become a branch.

`domain/knowledge/retrieval.py` holds only the types and the Protocol — no SQL, no
embedding call — which keeps it inside the existing contract that `domain/` imports no
framework and no provider.

## 6. Credentials — the separate mechanism

The requirement is a credential the server can *use* and nobody can *read*. That is a
different mechanism from `api_keys`, which must be verifiable and unreadable even by the
server, and the two are kept apart deliberately: conflating them yields either unusable
integrations or recoverable AgentWorld keys.

| | `api_keys` (Phase 1, built) | `credentials` (proposed) |
|---|---|---|
| Purpose | authenticate an inbound caller | authenticate AgentWorld to someone else |
| Stored as | Argon2id hash of a peppered secret | AES-256-GCM ciphertext under a wrapped data key |
| Recoverable by the server | **no, ever** | yes, at the point of use, and nowhere else |
| Recoverable by an attacker with the database | no | no — the key-encryption key is not in it |
| Shown to a user | once, at mint | never |
| Module | `infra/hashing.py` (untouched) | `infra/secrets/` (new) |

### Schema

```sql
CREATE TYPE credential_kind AS ENUM ('api_key', 'oauth2', 'basic', 'custom');

CREATE TABLE credentials (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid REFERENCES projects(id) ON DELETE CASCADE,  -- NULL = workspace-wide
  name          text NOT NULL,
  kind          credential_kind NOT NULL,
  provider_slug text NOT NULL,

  -- Envelope encryption. `ciphertext` is AES-256-GCM over the secret material
  -- under a per-row data key (DEK); `wrapped_key` is that DEK encrypted by the
  -- key-encryption key (KEK) named in `key_id`. The KEK is never in this
  -- database.
  ciphertext    bytea NOT NULL,
  nonce         bytea NOT NULL,       -- 96-bit, unique per encryption
  wrapped_key   bytea NOT NULL,
  key_id        text  NOT NULL,       -- which KEK version wrapped this DEK
  aad_version   smallint NOT NULL DEFAULT 1,

  -- Readable facts, so the UI shows something useful without decrypting.
  display_hint  text,                 -- "xoxb-…4f2a", "ada@example.com"
  scopes        text[],
  expires_at    timestamptz,
  refresh_after timestamptz,

  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz,
  revoked_at    timestamptz,
  revoked_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  deleted_at    timestamptz,          -- tombstone; see "Deletion" below
  UNIQUE (workspace_id, provider_slug, name)
);
CREATE INDEX credentials_refresh_due_idx ON credentials (refresh_after)
  WHERE revoked_at IS NULL AND deleted_at IS NULL AND refresh_after IS NOT NULL;
```

### Envelope encryption, and what each layer is for

```
secret material
   │  AES-256-GCM, nonce unique per encryption,
   │  AAD = workspace_id ‖ credential_id ‖ aad_version
   ▼
ciphertext + nonce                     ──► credentials.ciphertext, .nonce
   ▲
   │ encrypted under
data key (DEK), 256-bit, one per credential
   │  wrapped by
   ▼
key-encryption key (KEK), named by key_id, held in the environment or a KMS
```

Three properties, each the reason a layer exists:

**The AAD binds the ciphertext to its tenant and its row.** Authenticated additional
data is covered by the GCM tag but not stored, so decryption fails unless the caller
supplies the same `workspace_id` and `credential_id`. A row copied from one workspace to
another — by a SQL injection, a bad restore, or a mistaken migration — **cannot be
decrypted in its new home**. This is the strongest tenant-isolation property in the
proposal and it costs nothing: `SecretStore.decrypt()` takes both ids and has no overload
that omits them.

**A per-credential DEK keeps KEK rotation cheap.** Rotating the KEK means unwrapping and
re-wrapping each DEK. That needs no access to the plaintext secret, touches no
`ciphertext`, and can run online over a few thousand rows in seconds.

**`key_id` makes rotation additive rather than a cutover.** Several KEKs are valid at
once; rows name the one that wrapped them.

### Key management and rotation

```
CREDENTIAL_KEKS           = '{"k2026a":"<base64 32 bytes>","k2026b":"<base64>"}'
CREDENTIAL_ACTIVE_KEK_ID  = 'k2026b'
```

`Settings` refuses to construct outside `local` and `test` without both, and refuses an
`ACTIVE_KEK_ID` absent from the map — the same fail-fast validator `API_KEY_PEPPER`
already uses, which Phase 1 has a test for.

| Operation | What it does | Needs plaintext? | Online? |
|-----------|--------------|------------------|---------|
| **KEK rotation** | add a new KEK, point `ACTIVE` at it, re-wrap DEKs in the background, retire the old KEK when no row names it | no | yes |
| **DEK rotation** | new DEK and new ciphertext for one credential | yes | yes, at rotation time |
| **Credential rotation** | the user supplies new material; a new DEK and ciphertext replace the old | n/a | yes |
| **Compromised KEK** | rotate KEK, then rotate every credential it protected — the material must be assumed read | yes | per credential |

`SecretStore` is an interface with an envelope implementation over the environment map.
A KMS or Vault backend implements the same two methods; the choice is a deployment one and
no caller changes.

```python
class SecretStore(Protocol):
    async def encrypt(self, plaintext: bytes, *, workspace_id: UUID,
                      credential_id: UUID) -> EncryptedBlob: ...
    async def decrypt(self, blob: EncryptedBlob, *, workspace_id: UUID,
                      credential_id: UUID) -> bytes: ...
```

### Tenant isolation

Four independent layers, which is one more than any other table gets, because this is the
table worth the most to an attacker:

1. `credentials` is in `RLS_PROTECTED_TABLES`, so a query with the wrong session variable
   returns nothing.
2. The repository is a `ScopedRepository` and cannot be constructed without a
   `TenantScope`.
3. `authorize()` requires `admin` for every credential action (see `05-PERMISSIONS.md`).
4. The AAD binding above, which makes a leaked row useless in another workspace even if
   the first three were bypassed.

### No exposure to frontend clients

- **The domain entity has no field for secret material.** As with `ApiKey` in Phase 1,
  there is nowhere for a response model, a `repr`, a log line or a serialiser to pick it
  up, because the attribute does not exist. Only `infra/secrets/` sees `EncryptedBlob`.
- **No endpoint returns it.** There is no `credential.read_secret` action in the
  permission matrix — not restricted to owners, *absent*.
- **Decryption happens at the point of use**, inside the provider or tool call, keyed by
  `credential_id`. A credential is never loaded into an execution's context, a dispatch's
  `instruction`, or a prompt.
- **A dispatch carries references, never material.** `dispatches.instruction` holds
  `{"credential_id": "…"}`; the worker resolves it immediately before the outbound call.
- *Acceptance:* a test walks the generated OpenAPI document and asserts no schema reachable
  from a `credentials` response has a property named `ciphertext`, `nonce`, `wrapped_key`,
  `secret`, `token`, `password` or `key`.

### Redaction in logs, events, errors and telemetry

Four mechanisms, because each channel leaks differently:

| Channel | Mechanism |
|---------|-----------|
| **Logs** | A `Secret` wrapper whose `__str__`, `__repr__` and `__format__` return `"***"`, so an accidental `log.info("...", cred=c)` renders nothing. Plus a structlog processor that scrubs values matching known credential shapes from every event dict |
| **Events** | Tool and provider schemas declare `secret_fields`; the executor redacts those paths from step input and output **before** the event is appended or published. Redaction happens at the boundary, not at render time, so the stored row is already clean |
| **Errors** | Problem details never echo a request body. Provider and tool errors are mapped to sanitised messages; the raw error goes to the log with the secret already wrapped |
| **Telemetry** | Metric labels are a fixed allow-list of low-cardinality names. No span attribute is populated from a tool argument or result |

*Acceptance (the canary test):* a credential is created whose secret is a known unique
string. A workflow then uses it through a tool, fails, retries and succeeds. The test
asserts the canary appears in **no** row of any table, **no** captured log line, **no**
`events` payload, **no** WebSocket frame, **no** problem document and **no** metric label.
This is the same shape as the Phase 1 test that searched every text column for an API key
plaintext, which already passes.

### Revocation and deletion

**Revoke** — `revoked_at` set, and the effects are immediate and loud rather than silent:

| Consequence | Behaviour |
|-------------|-----------|
| `SecretStore.decrypt` | refuses; the row is treated as absent |
| A dispatch using it | fails with a terminal (non-retryable) error — retrying cannot help |
| The step | `workflow.step_failed`, reason `credential_revoked`; `on_failure` applies |
| The integration | status `error`, with the reason shown in the workspace and on the island portal |
| Model profiles using it | agents using them fail to dispatch, with the same clear reason |
| Running executions | fail the affected step. They are **not** paused and **not** silently skipped: a workflow that quietly continued without the step it needed would be worse than one that stopped |
| Reversible? | No. Un-revoking would mean the window of compromise had no end. Create a new credential |

**Delete** — a two-step operation, because an unreferenced credential is the only kind
safe to remove:

1. `DELETE` is refused with `409` while any integration, model profile or enabled trigger
   references it, and the response names the referents. `ON DELETE RESTRICT` enforces it
   at the database as well.
2. Once detached, deletion zeroes `ciphertext`, `nonce` and `wrapped_key`, sets
   `deleted_at`, and **keeps the row as a tombstone** carrying `id`, `name`,
   `display_hint`, `created_by`, `revoked_at` and `deleted_at`. `audit_log` rows reference
   credential ids, and a dangling reference in an audit trail is worse than a tombstone.

**OAuth revocation** is best-effort and says so: AgentWorld calls the provider's revoke
endpoint, then revokes locally **regardless of the outcome**. The UI reports whether the
provider confirmed. Claiming a third party honoured a revocation is not something this
system can know.

*Acceptance criteria for this section:*

- A revoked credential cannot be decrypted, and the failing step's reason names the cause.
- A referenced credential cannot be deleted; the `409` lists what references it.
- A deleted credential's row holds no key material, and audit rows referencing it still
  resolve.
- A credential row copied into another workspace at the SQL level cannot be decrypted.
- KEK rotation re-wraps every DEK with no plaintext access and no change to `ciphertext`.
- The canary test above passes.

## 7. Execution state

```sql
-- Who started an execution. There is no third value and no NULL: an
-- execution with no recorded initiator is an audit hole.
CREATE TYPE initiator_kind AS ENUM ('user', 'system');

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

  -- The initiator. Immutable: written once at creation, never recomputed,
  -- and never resolved through a mutable row like `triggers.created_by`.
  -- `initiator_kind` is always present; exactly one of the two ids is.
  initiator_kind initiator_kind NOT NULL,
  initiator_user_id uuid REFERENCES users(id) ON DELETE RESTRICT,
  initiator_trigger_id uuid REFERENCES triggers(id) ON DELETE RESTRICT,
  -- A human-readable record that survives both rows being archived, so an
  -- audit two years later does not depend on a join that may have gone.
  initiator_label text NOT NULL,       -- "ada@example.com" | "schedule competitor-brief"
  CONSTRAINT initiator_user_xor_system CHECK (
    (initiator_kind = 'user'   AND initiator_user_id IS NOT NULL
                               AND initiator_trigger_id IS NULL)
    OR
    (initiator_kind = 'system' AND initiator_user_id IS NULL)
  ),
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
CREATE INDEX executions_initiator_idx ON workflow_executions (project_id, initiator_kind, created_at DESC);

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

  -- The resolved requirement, frozen when the approval is created. Resolved
  -- from the policy below; stored rather than recomputed so that editing the
  -- policy cannot change what a pending approval demands.
  required_role member_role NOT NULL DEFAULT 'member',
  requires_different_user boolean NOT NULL DEFAULT false,

  -- Who this approval is "on behalf of", for the different-user rule. Not
  -- simply the person who pressed Run: see "closing the scheduling loophole".
  attributed_to_user_id uuid REFERENCES users(id) ON DELETE SET NULL,

  requested_at  timestamptz NOT NULL DEFAULT now(),
  expires_at    timestamptz,
  decided_at    timestamptz,
  decided_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  decision_reason text,
  UNIQUE (execution_step_id),
  CONSTRAINT different_user_respected CHECK (
    decided_by_user_id IS NULL
    OR NOT requires_different_user
    OR attributed_to_user_id IS NULL
    OR decided_by_user_id <> attributed_to_user_id
  )
);
CREATE INDEX approvals_pending_idx ON approvals (project_id, requested_at)
  WHERE decision = 'pending';
```

An approval is a row that must exist and be `granted` before a dispatch exists. There is
no code path from `waiting_approval` to a dispatched step that does not pass through it.
That is what makes it a gate rather than a notification, and it is structural rather than
remembered.

The `different_user_respected` check is the same guarantee one layer down. The service
enforces the rule; the constraint means a bug in the service, or a direct `UPDATE`, cannot
record a self-approval that the policy forbade. Belt and braces, as with
`workspace_single_owner`.

### Approval policies

Self-approval is not an unrestricted default. It is a configurable policy with defaults
chosen so that **low-risk work flows freely and consequential work needs someone else**.

```sql
CREATE TABLE approval_policies (
  project_id    uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,

  -- The default for an approval whose step declares no requirement.
  default_required_role member_role NOT NULL DEFAULT 'member',
  default_requires_different_user boolean NOT NULL DEFAULT false,

  -- The one knob that does most of the work: anything classified
  -- `consequential` needs a second person, out of the box.
  consequential_required_role member_role NOT NULL DEFAULT 'member',
  consequential_requires_different_user boolean NOT NULL DEFAULT true,

  -- A floor for the whole project. A step may tighten past it, never under.
  min_required_role member_role NOT NULL DEFAULT 'member',

  updated_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  updated_at    timestamptz NOT NULL DEFAULT now()
);
```

A project with no row uses the column defaults, so the policy is opt-in to *edit* and
always in effect. The defaults are exactly the brief's two rules:

| Work | Default requirement |
|------|--------------------|
| `read` or `write` side effects | `member`, self-approval allowed |
| `consequential` side effects | `member`, **and not the person it is attributed to** |

### Resolution, and the direction policies may move

```python
# domain/approvals/policy.py — pure, no I/O.
def resolve_requirement(
    policy: ApprovalPolicy,
    step: WorkflowStep,
    tool: Tool | None,
    connection: Connection | None,
) -> ApprovalRequirement:
    """The requirement for one step. Deterministic, and total."""
```

Four inputs, resolved by **taking the strictest of all of them**:

1. the project policy's class-appropriate pair,
2. the project policy's `min_required_role` floor,
3. the step's own `config.approval`, if it declares one,
4. the tool's `requires_approval_default` and the connection's `requires_approval`.

**A step can only tighten.** A step declaring `requires_different_user: false` against a
policy that says `true` resolves to `true`, and `workflow.validate` reports it as a
`warning` rather than silently honouring the policy — so the author learns their setting
had no effect instead of believing it did.

**No policy can weaken a security control.** Two hard floors sit under everything, and
neither is in the table because neither is configurable:

- **The approver's role may never be below the role required to perform the underlying
  action.** Approving a step that grants or uses a `consequential` tool requires at least
  the role that could have granted it (`admin`, per `05-PERMISSIONS.md` §4). An `admin`
  cannot write a policy letting a `viewer` approve an email send.
- **A policy cannot grant a permission.** Resolution produces a *requirement*, never a
  capability. `authorize(principal, Action.APPROVAL_DECIDE, …)` runs first and
  independently; a policy that named `viewer` would still be refused by the permission
  matrix. The policy can only ask for more.

### Execution identity: immutable, and never a borrowed user

Revision 3 proposed attributing a scheduled run to whoever last performed
`trigger.enable`. **That was wrong and is withdrawn.** Two things break:

- **Authority would be borrowed and would go stale.** A workflow enabled by an admin who
  later drops to `member`, or leaves, would keep running with the authority of a role they
  no longer hold — resolved through a mutable row, `triggers`, long after the decision.
  Permissions that outlive the person holding them are the bug this would have built.
- **It silently disqualifies a bystander.** The enabler would become unable to approve any
  of that workflow's consequential steps, a consequence they never chose and nothing told
  them about.

So every execution carries an **immutable initiator**, written once at creation:

| Execution started by | `initiator_kind` | `initiator_user_id` | `initiator_label` |
|---------------------|------------------|--------------------|-------------------|
| `POST /workflows/{id}/run` | `user` | the caller | their email |
| `manual` trigger | `user` | the caller | their email |
| `schedule` trigger | **`system`** | NULL | `schedule <workflow-slug>` |
| `webhook` trigger | **`system`** | NULL | `webhook <trigger-id>` |
| `integration_event` | **`system`** | NULL | `integration <slug>` |
| `workflow_completed` | **inherited verbatim** from the originating execution | | |

Three properties follow, and each is the point:

**Authority never comes from a user for a system-initiated run.** What a scheduled
execution may do is fixed by the **published `workflow_version`** — its steps, its
assignees, and the tool grants those assignees hold. Those were authorized at publish and
grant time by someone who held the role then. The execution consults no user's live
permissions, so there is no account whose authority it borrows and nothing to go stale.
Revoking a grant stops the next call (checked at call time, `02-ORCHESTRATION.md` §5);
demoting a user changes nothing about a running schedule, because the schedule never had
their authority.

**The initiator is recorded, not derived.** `initiator_label` is written at creation and is
plain text, so an audit still answers "what started this" after the trigger is deleted and
the user row is gone. `ON DELETE RESTRICT` on `initiator_user_id` means a user who has
initiated an execution cannot be hard-deleted out from under the record.

**A `SystemPrincipal` makes this explicit in code.** The principal union becomes
`UserPrincipal | KeyPrincipal | SystemPrincipal`, where the third is what a worker acts
as. It carries no workspace membership and `authorize()` refuses every control-plane
action for it, exactly as it does for a key — so "the scheduler did it" can never widen
into "the scheduler may do anything". A small, pleasing consequence: Phase 1 removed the
`"system"` branch from `services/context.actor_of` as unreachable dead code. It becomes
reachable now, legitimately.

### The different-user rule, and the residual it leaves

`requires_different_user` compares the decider against the execution's initiator **when
that initiator is a user**. For a system-initiated execution there is no user to differ
from: `approvals.attributed_to_user_id` is NULL, the check is satisfied by any authorized
approver, and the approval records `initiator_kind = 'system'` so the audit shows why.

**The residual, stated rather than buried:** a user who cannot self-approve a manual run
could — if they also hold `trigger.enable` — schedule the same workflow and then approve
its steps themselves. Three things bound it, and none of them eliminates it:

- `trigger.enable` is an `admin` action (`05-PERMISSIONS.md` §5). The route is open only to
  someone who could, in most cases, have granted themselves the capability directly.
- The approver's **own** role is still checked independently, so this is never a route to
  an action their role does not permit — only to one they could have approved anyway if
  somebody else had started it.
- Enabling the trigger, the execution and the decision are three `audit_log` rows naming
  the same user.

Closing it properly needs a policy knob — "a system-initiated consequential step needs an
approver who did not enable its trigger" — which reintroduces the mutable-row lookup this
section exists to remove, unless the enabler is also snapshotted onto the trigger at enable
time. That is a v1.1 candidate (`04-ROADMAP-AND-MVP.md` §9), not v1 scope.

### Authorization is checked independently of approval policy

Worth stating as its own rule, because it is the one that makes everything above safe:

```
authorize(principal, Action.APPROVAL_DECIDE, Resource(workspace_id))   # first, always
   ↓ only if that passed
check approvals.required_role            (from the frozen requirement)
   ↓
check requires_different_user            (against the frozen initiator)
   ↓
write the decision
```

- **A policy can only add requirements.** `resolve_requirement` returns an
  `ApprovalRequirement`; nothing in it is consulted by `authorize`, and nothing it could
  say would relax the permission matrix. A policy naming `viewer` is still refused.
- **An approval cannot grant the approver anything.** Granting one writes a decision and
  calls `advance()`. The dispatch that follows runs with the *workflow version's* grants,
  not the approver's — so approving a step does not lend it your permissions.
- **An approval cannot grant the requester anything either.** A `member` whose workflow
  needs a `consequential` tool the project never granted gets
  `tool_not_granted` from `validate` at publish time. No approval exists for it, because
  the workflow could not be published.
- **The floor is the underlying action's own role.** An approver's required role may never
  be below the role required to perform what is being approved, so approval cannot become
  a cheaper path to a privileged act.

### When a requirement cannot be satisfied

`requires_different_user` in a workspace with one member means nothing can ever be
approved — a deadlock built by configuration. It is caught where every other unrunnable
configuration is caught: `workflow.validate` emits

```jsonc
{ "severity": "error", "code": "approval_unsatisfiable",
  "pointer": "/steps/post",
  "detail": "This step needs approval from somebody other than whoever runs it, and this workspace has only one member who can approve.",
  "remedy": "Invite another member, or allow self-approval for this step in Settings → Approvals." }
```

so a workflow that could never complete cannot be published. The same check runs when the
policy is edited, and refuses an edit that would strand published workflows.

### Enforcement is in the backend

Three independent layers, because the brief asks for backend enforcement and one layer is
a claim rather than an enforcement:

1. **`services/approvals.py`** checks the role and the different-user rule before writing a
   decision, and is the only writer of `approvals.decision`.
2. **The check constraint** above refuses a contradictory row even from raw SQL.
3. **The gate itself** is the absence of a `dispatches` row. A frontend that hid the button,
   or showed one it should not, changes nothing: no dispatch exists until the service writes
   the decision, and nothing else can write one (`05-PERMISSIONS.md` §3 — there is no
   `dispatch.create` action).

### What is deliberately not built

Not an enterprise four-eyes system. No approval groups, no quorum ("any two of these
five"), no escalation chains, no delegation or out-of-office, no approval-of-the-approval,
no per-user approval limits. Each is in `04-ROADMAP-AND-MVP.md` §9 as a v1.1 item, and each
fits behind `resolve_requirement` returning a richer `ApprovalRequirement` without the gate
mechanism changing.

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
           │            └─ world_layouts, world_districts,
           │               world_nodes, world_routes            [presentation]
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


## 10. The visual state model, and why it is a separate table set

The island needs persisted layout — where a building sits, how big it is, what it looks
like, which district it belongs to, how a road is routed. That is **not** a view of
execution state and must not be stored with it.

The rule, stated as the two failures it prevents:

> **Moving a building must never change what the system does.** If position lived on
> `agents`, then a drag would be a write to a domain row, would need the permission that
> edits an agent, would appear in the audit trail as a configuration change, and would
> have to be considered by anything that reads agent state.
>
> **An agent that is offline, archived or broken must stay on the island.** If a node's
> existence were derived from execution state, the island would empty itself exactly when
> the user most needs to look at it. A shuttered building is the information.

### Schema

```sql
CREATE TABLE world_layouts (
  project_id   uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  theme        text NOT NULL DEFAULT 'island-temperate',
  grid_width   smallint NOT NULL DEFAULT 24,
  grid_height  smallint NOT NULL DEFAULT 24,
  tiles        jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- [{"q":3,"r":5,"terrain":"grass","decor":"tree"}, ...]
  landmarks    jsonb NOT NULL DEFAULT '[]'::jsonb,
  camera       jsonb NOT NULL DEFAULT '{}'::jsonb,   -- last view, per project
  updated_at   timestamptz NOT NULL DEFAULT now()
);

-- Visual grouping only. A *project* is the domain and tenancy boundary; a
-- *district* is a region of one project's island. The earlier proposal mapped
-- the brief's "projects or districts" onto projects alone; this separates
-- them, so a user can group an ecosystem visually without creating a second
-- tenancy boundary and without the permissions that come with one.
CREATE TABLE world_districts (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  name         text NOT NULL,
  bounds       jsonb NOT NULL,          -- {"q":0,"r":0,"width":8,"height":8}
  accent_color text,
  collapsed    boolean NOT NULL DEFAULT false,
  sort_order   smallint NOT NULL DEFAULT 0,
  UNIQUE (project_id, name)
);

-- One row per placed object, whatever kind it is. Replaces the previous
-- design's `world_placements`, which was keyed on `agent_id` and so could
-- only ever place agents.
CREATE TABLE world_nodes (
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  node_kind    text NOT NULL,           -- 'agent'|'workflow'|'integration'|
                                        -- 'knowledge_source'|'trigger'|'approval_desk'
  node_id      uuid NOT NULL,           -- the domain object's id
  district_id  uuid REFERENCES world_districts(id) ON DELETE SET NULL,

  -- position and size
  tile_q       smallint NOT NULL,
  tile_r       smallint NOT NULL,
  width        smallint NOT NULL DEFAULT 1,
  height       smallint NOT NULL DEFAULT 1,
  rotation     smallint NOT NULL DEFAULT 0,
  z_order      smallint NOT NULL DEFAULT 0,

  -- appearance
  building_kind  text NOT NULL,         -- 'workshop'|'library'|'factory'|'portal'|...
  character_kind text,                  -- sprite set, for agents
  accent_color text,
  icon         text,
  scale        numeric(3,2) NOT NULL DEFAULT 1.00,

  auto_assigned boolean NOT NULL DEFAULT true,
  hidden       boolean NOT NULL DEFAULT false,   -- a user's choice, not a status
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, node_kind, node_id),
  UNIQUE (project_id, tile_q, tile_r)
);

-- Visual routing for a line the user can see. The *existence* of the
-- relationship is domain (`connections`, `workflow_edges`); only its drawn
-- path is here, so rerouting a road changes no grant.
CREATE TABLE world_routes (
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  route_kind   text NOT NULL,           -- 'connection' | 'workflow_edge'
  route_id     uuid NOT NULL,           -- connections.id | workflow_edges.id
  waypoints    jsonb NOT NULL DEFAULT '[]'::jsonb,   -- [{"q":4,"r":6}, ...]
  style        text NOT NULL DEFAULT 'road',         -- 'road'|'bridge'|'cable'|'pipe'
  accent_color text,
  hidden       boolean NOT NULL DEFAULT false,
  updated_at   timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, route_kind, route_id)
);
```

### What these tables deliberately do not contain

No column here holds, mirrors or caches:

- agent execution status (`working`, `idle`, `offline`, `error`, `paused`)
- workflow or step execution state
- integration connection health
- task or step progress
- anything else a reducer writes

Those arrive at render time, from the domain, as the second argument to `projectLive`.
A `hidden` column exists and is a *user's choice to hide something*, which is why it is
here; an `offline` column would be a cache of execution state, which is why there is not
one.

*Acceptance (a structural test, not a convention):* the presentation tables' column names
are asserted to intersect a forbidden list — `status`, `progress`, `running`, `state`,
`last_heartbeat_at`, `current_task_id`, `enabled`, `health` — at zero. A future column
called `agent_status` on `world_nodes` fails CI.

### What enforces the separation

| Guarantee | Mechanism |
|-----------|-----------|
| No reducer writes a world table | the reducer registry's targets are typed to the execution tables; a `world_*` write from `domain/reducers/` does not type-check, and an import-linter contract forbids the presentation repositories there |
| No execution table references a world table | no foreign key points that way. Dropping all four tables leaves every execution query valid — the existing "the backend would behave identically" property, kept |
| Moving an object changes nothing operational | the only write is `world_nodes`; it emits no event, writes no `audit_log` row, and the `world.update` permission is held by `member` and grants nothing else (`05-PERMISSIONS.md`) |
| An offline or archived agent stays visible | `world_nodes` rows are independent of status. An archived agent keeps its node and renders shuttered, and **deleting a node does not touch the agent**. The other direction is *not* structural — see the correction below |
| The domain stays the source of truth | the projections read both and write neither. `interpret()` is pure and cannot fetch; `world/dispatch/` is the only layer that may call an API |

*Acceptance criteria:*

- Moving a node produces no row in `events` and no row in `audit_log`.
- A `viewer` cannot move a node; a `member` can; moving one never requires `agent.update`.
- An agent transitioning to `offline`, and an agent archived, both still render.
- Deleting a `world_nodes` row leaves the agent, its runs and its history intact.
- Dropping all four presentation tables leaves the backend suite green — the Phase 0
  isolation property, widened from two tables to four.

### Correction from implementing migration 0013

One criterion above was written as though it were enforced and is not. **Deleting an
agent does not cascade its node.** `world_nodes.node_id` names its object
polymorphically — one of six kinds, three of which have no table yet — so there is
nothing to point a foreign key at, and the database cannot clean up after a deleted
agent. The orphan also *holds its tile*, because `UNIQUE (project_id, tile_q, tile_r)`
does not care that its referent is gone, which is the user-visible half of the problem.

Two obligations follow, and they are obligations rather than guarantees: the Phase 3
delete paths must remove the node in the same transaction as the domain row, and a
reconciliation sweep must drop nodes whose referent has vanished.
`test_world_separation.py` pins the current behaviour, so the gap fails a test if it is
ever mistaken for a guarantee — and the test says which assertion to change when the
gap closes.

The direction that *does* hold structurally is the one that matters more for the
separation: no foreign key points from the domain into the presentation tables, so
dropping all four leaves every execution query valid.

