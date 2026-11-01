# AgentWorld — Database Schema

> PostgreSQL 16. Revision 1 (2026-10-31). This document is the source of truth for the
> first Alembic migration; the SQL below is written to be runnable as-is.

## 1. Shape of the data

```
users (mirrors Supabase auth.users)
  └─ workspace_members ─┐
                        ├─ workspaces
                        │     ├─ api_keys            (workspace- or project-scoped)
                        │     ├─ audit_log
                        │     └─ projects
                        │           ├─ agents ──────── agent_status_history
                        │           ├─ tasks ───────── task_dependencies
                        │           ├─ runs ────────── run_steps
                        │           ├─ events        (partitioned, append-only)
                        │           ├─ logs          (partitioned, append-only)
                        │           ├─ metric_samples(partitioned)
                        │           ├─ integrations
                        │           ├─ webhooks ───── webhook_deliveries
                        │           └─ world_placements   <- presentation only
```

Three groups, deliberately separated:

| Group | Tables | Property |
|-------|--------|----------|
| **Tenancy** | `users`, `workspaces`, `workspace_members`, `api_keys`, `audit_log` | Written by the control plane only |
| **Domain** | `agents`, `tasks`, `runs`, `run_steps`, `events`, `logs`, `metric_samples`, `integrations`, `webhooks` | Current-state rows written **only** by reducers; `events`/`logs` are append-only |
| **Presentation** | `world_placements`, `world_layouts` | Written by the UI. No reducer, adapter or SDK touches these. This is what makes goal G8 structural rather than aspirational. |

## 2. Conventions

- Primary keys: `uuid` with `gen_random_uuid()` (`pgcrypto`).
- All timestamps `timestamptz`, always UTC, named `*_at`.
- Soft delete via `archived_at` on tenancy and domain entities; append-only tables are
  never deleted, only dropped by partition.
- Enums as native PostgreSQL types — they appear in the OpenAPI document and the
  generated TypeScript, so drift is caught at build time.
- Indexes are **named explicitly**, never left to PostgreSQL's auto-naming. The
  model/migration drift check compares them by name, and an auto-named index is
  reported as a difference on every run.
- Every tenant-scoped table carries `workspace_id` **and** its direct parent id. The
  denormalised `workspace_id` makes every tenant filter a single-table predicate and
  makes a missing filter visible in review.
- `labels jsonb` on domain rows for user-defined, low-cardinality dimensions. Indexed
  with GIN where filtered.

## 3. Enums

```sql
CREATE TYPE member_role AS ENUM ('owner', 'admin', 'member', 'viewer');

CREATE TYPE agent_status AS ENUM (
  'provisioning',  -- registered, never seen
  'offline',       -- heartbeat TTL exceeded
  'idle',          -- connected, no active task
  'working',       -- executing a task
  'blocked',       -- waiting on input or a dependency
  'error',         -- last run failed, not recovered
  'maintenance'    -- operator-silenced
);

CREATE TYPE task_status AS ENUM (
  'pending', 'queued', 'running', 'blocked',
  'completed', 'failed', 'cancelled'
);

CREATE TYPE task_priority AS ENUM ('low', 'normal', 'high', 'urgent');

CREATE TYPE run_status AS ENUM ('running', 'succeeded', 'failed', 'cancelled');

CREATE TYPE event_severity AS ENUM ('debug', 'info', 'warning', 'error', 'critical');

CREATE TYPE api_key_scope AS ENUM ('ingest', 'control', 'full');

CREATE TYPE delivery_status AS ENUM ('pending', 'delivered', 'failed', 'dead');
```

## 4. Tenancy

```sql
-- Mirror of Supabase auth.users. id == JWT `sub`. Never stores credentials.
CREATE TABLE users (
  id            uuid PRIMARY KEY,
  email         citext NOT NULL UNIQUE,
  display_name  text,
  avatar_url    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_seen_at  timestamptz
);

CREATE TABLE workspaces (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  slug        citext NOT NULL UNIQUE,
  name        text   NOT NULL,
  created_by  uuid   NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  settings    jsonb  NOT NULL DEFAULT '{}'::jsonb,
  created_at  timestamptz NOT NULL DEFAULT now(),
  updated_at  timestamptz NOT NULL DEFAULT now(),
  archived_at timestamptz
);

CREATE TABLE workspace_members (
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  user_id      uuid NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  role         member_role NOT NULL DEFAULT 'member',
  invited_by   uuid REFERENCES users(id) ON DELETE SET NULL,
  joined_at    timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (workspace_id, user_id)
);
CREATE INDEX workspace_members_user_id_idx ON workspace_members (user_id);

-- Exactly one owner per workspace, enforced in the service layer and asserted here.
CREATE UNIQUE INDEX workspace_single_owner
  ON workspace_members (workspace_id) WHERE role = 'owner';

CREATE TABLE projects (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  slug         citext NOT NULL,
  name         text   NOT NULL,
  description  text,
  settings     jsonb  NOT NULL DEFAULT '{}'::jsonb,
  -- e.g. {"heartbeat_ttl_seconds": 60, "log_severity_floor": "info",
  --       "event_retention_days": 90}
  created_at   timestamptz NOT NULL DEFAULT now(),
  updated_at   timestamptz NOT NULL DEFAULT now(),
  archived_at  timestamptz,
  UNIQUE (workspace_id, slug)
);
```

### API keys

```sql
CREATE TABLE api_keys (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid REFERENCES projects(id) ON DELETE CASCADE, -- NULL = workspace-wide
  name          text NOT NULL,
  prefix        text NOT NULL UNIQUE,   -- 'aw_live_8f2c1a', the only queryable part
  secret_hash   text NOT NULL,          -- Argon2id of the secret half
  scope         api_key_scope NOT NULL DEFAULT 'ingest',
  agent_key_allowlist text[],           -- NULL = any agent_key in the project
  rate_limit_rps int NOT NULL DEFAULT 50,
  created_by    uuid NOT NULL REFERENCES users(id) ON DELETE RESTRICT,
  created_at    timestamptz NOT NULL DEFAULT now(),
  last_used_at  timestamptz,
  expires_at    timestamptz,
  revoked_at    timestamptz,
  CONSTRAINT ingest_keys_are_project_scoped
    CHECK (scope <> 'ingest' OR project_id IS NOT NULL)
);
CREATE INDEX api_keys_workspace_active_idx
  ON api_keys (workspace_id) WHERE revoked_at IS NULL;
CREATE INDEX api_keys_project_idx ON api_keys (project_id);
```

The wire format is `aw_<env>_<random>.<secret>` — `aw_test_8f2c1a3d5e70.Zm9vY...`.
The prefix before the `.` is `prefix` above; the secret after it is hashed and
never stored. `secrets.token_urlsafe` emits no `.`, which is what makes the
first `.` in a key unambiguously the separator. The random part of the prefix is
48 bits: at 24 bits a workspace with a few thousand keys has a noticeable chance
of colliding on the unique index, surfacing as a failed mint for no good reason.

The plaintext secret exists once, in the `POST /v1/workspaces/{ws}/api-keys`
response. Lookup is `prefix -> row -> Argon2id verify`, so a timing attack
learns nothing beyond the (public) prefix — and an unknown prefix still pays for
one verification against a dummy hash, so it cannot be told apart by latency.

The environment label is checked on every use: a `test` key presented to a
deployed server is refused rather than ignored, so a development credential
pasted into production fails loudly.

### Audit log

```sql
CREATE TABLE audit_log (
  id            bigserial PRIMARY KEY,
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  actor_type    text NOT NULL,          -- 'user' | 'api_key' | 'system'
  actor_id      uuid,
  action        text NOT NULL,          -- 'project.create', 'api_key.revoke', ...
  resource_type text NOT NULL,
  resource_id   uuid,
  -- The same value as the response's X-Request-Id and a problem document's
  -- `instance`, so a user's bug report joins to the record of what their
  -- request actually did.
  request_id    text,
  metadata      jsonb NOT NULL DEFAULT '{}'::jsonb,
  ip            inet,
  user_agent    text,
  created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_log_workspace_created_idx ON audit_log (workspace_id, created_at DESC);
```

## 5. Domain

### Agents

```sql
CREATE TABLE agents (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id   uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id     uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  agent_key      citext NOT NULL,          -- caller-chosen stable identity
  display_name   text NOT NULL,
  kind           text,                     -- free text: 'planner', 'etl', 'support-bot'
  -- Descriptive only. The core never branches on these.
  runtime        jsonb NOT NULL DEFAULT '{}'::jsonb,
  -- e.g. {"language":"python","framework":"langgraph","provider":"anthropic",
  --       "model":"claude-opus-5","version":"1.4.2"}
  capabilities   text[] NOT NULL DEFAULT '{}',
  labels         jsonb  NOT NULL DEFAULT '{}'::jsonb,
  status         agent_status NOT NULL DEFAULT 'provisioning',
  status_reason  text,
  current_task_id uuid,                    -- FK added after tasks (circular)
  heartbeat_ttl_seconds int NOT NULL DEFAULT 60,
  last_heartbeat_at timestamptz,
  last_event_at     timestamptz,
  registered_at  timestamptz NOT NULL DEFAULT now(),
  archived_at    timestamptz,
  UNIQUE (project_id, agent_key)
);
CREATE INDEX ON agents (project_id, status) WHERE archived_at IS NULL;
CREATE INDEX ON agents USING gin (labels);

-- Drives the "offline" sweep: cheap to scan, no full table pass.
CREATE INDEX agents_heartbeat_watch
  ON agents (last_heartbeat_at)
  WHERE archived_at IS NULL AND status NOT IN ('offline', 'maintenance');

CREATE TABLE agent_status_history (
  id          bigserial PRIMARY KEY,
  agent_id    uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  from_status agent_status,
  to_status   agent_status NOT NULL,
  reason      text,
  event_id    uuid,                        -- the event that caused the transition
  changed_at  timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ON agent_status_history (agent_id, changed_at DESC);
```

### Tasks

```sql
CREATE TABLE tasks (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  task_ref      citext,                    -- caller's own id, unique per project
  title         text NOT NULL,
  description   text,
  status        task_status NOT NULL DEFAULT 'pending',
  priority      task_priority NOT NULL DEFAULT 'normal',
  assigned_agent_id uuid REFERENCES agents(id) ON DELETE SET NULL,
  created_by_user_id uuid REFERENCES users(id) ON DELETE SET NULL,
  created_by_agent_id uuid REFERENCES agents(id) ON DELETE SET NULL,
  progress_percent smallint CHECK (progress_percent BETWEEN 0 AND 100),
  progress_message text,
  input         jsonb NOT NULL DEFAULT '{}'::jsonb,
  result        jsonb,
  error         jsonb,                     -- {"code","message","retryable","stack"}
  attempt       int NOT NULL DEFAULT 0,
  max_attempts  int NOT NULL DEFAULT 1,
  labels        jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at    timestamptz NOT NULL DEFAULT now(),
  queued_at     timestamptz,
  started_at    timestamptz,
  finished_at   timestamptz,
  deadline_at   timestamptz,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, task_ref)
);
CREATE INDEX ON tasks (project_id, status, priority DESC, created_at);
CREATE INDEX ON tasks (assigned_agent_id, status);
CREATE INDEX ON tasks USING gin (labels);

ALTER TABLE agents
  ADD CONSTRAINT agents_current_task_fk
  FOREIGN KEY (current_task_id) REFERENCES tasks(id) ON DELETE SET NULL;

CREATE TABLE task_dependencies (
  task_id     uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  depends_on_id uuid NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  PRIMARY KEY (task_id, depends_on_id),
  CONSTRAINT no_self_dependency CHECK (task_id <> depends_on_id)
);
```

### Runs and steps

A **run** is one execution attempt of a task (or a standalone agent activity). A task with
`max_attempts = 3` can have three runs.

```sql
CREATE TABLE runs (
  id           uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id   uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  run_ref      citext,
  agent_id     uuid NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
  task_id      uuid REFERENCES tasks(id) ON DELETE SET NULL,
  status       run_status NOT NULL DEFAULT 'running',
  step_count   int NOT NULL DEFAULT 0,
  tool_call_count int NOT NULL DEFAULT 0,
  error        jsonb,
  started_at   timestamptz NOT NULL DEFAULT now(),
  finished_at  timestamptz,
  duration_ms  int GENERATED ALWAYS AS
               (EXTRACT(EPOCH FROM (finished_at - started_at)) * 1000)::int STORED,
  UNIQUE (project_id, run_ref)
);
CREATE INDEX ON runs (task_id, started_at DESC);
CREATE INDEX ON runs (agent_id, started_at DESC);

CREATE TABLE run_steps (
  id          bigserial PRIMARY KEY,
  run_id      uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  seq         int  NOT NULL,
  name        text NOT NULL,
  kind        text,                    -- 'llm' | 'tool' | 'decision' | 'io' | free text
  status      run_status NOT NULL DEFAULT 'running',
  input       jsonb,
  output      jsonb,
  error       jsonb,
  started_at  timestamptz NOT NULL DEFAULT now(),
  finished_at timestamptz,
  UNIQUE (run_id, seq)
);
```

### Events (append-only, partitioned)

```sql
CREATE TABLE events (
  id           uuid NOT NULL DEFAULT gen_random_uuid(),
  workspace_id uuid NOT NULL,
  project_id   uuid NOT NULL,
  seq          bigint NOT NULL,          -- per-project monotonic cursor
  dedup_key    text,
  type         text NOT NULL,            -- 'task.progress', 'custom.cache_warmed'
  severity     event_severity NOT NULL DEFAULT 'info',
  agent_id     uuid,
  task_id      uuid,
  run_id       uuid,
  source       text NOT NULL,            -- 'rest' | 'sdk-python' | adapter slug
  api_key_id   uuid,
  labels       jsonb NOT NULL DEFAULT '{}'::jsonb,
  payload      jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at  timestamptz NOT NULL,     -- client clock
  received_at  timestamptz NOT NULL DEFAULT now(),  -- server clock
  clock_skew_ms int,                     -- received_at - occurred_at, recorded not fixed
  PRIMARY KEY (project_id, seq, received_at)
) PARTITION BY RANGE (received_at);

CREATE UNIQUE INDEX events_dedup
  ON events (project_id, dedup_key, received_at) WHERE dedup_key IS NOT NULL;
CREATE INDEX ON events (project_id, received_at DESC);
CREATE INDEX ON events (agent_id, received_at DESC);
CREATE INDEX ON events (task_id, received_at DESC);
CREATE INDEX ON events (project_id, type, received_at DESC);

CREATE TABLE events_2026_11 PARTITION OF events
  FOR VALUES FROM ('2026-11-01') TO ('2026-12-01');
-- ... created ahead of time by the scheduler process.
```

`seq` comes from one sequence per project (`project_event_seq`, a row-level counter
incremented inside the ingest transaction). It gives WebSocket clients a cursor with no
dependence on wall-clock ordering, which is what makes reconnect-replay exact.

> The unique indexes include `received_at` because PostgreSQL requires the partition key
> in every unique constraint on a partitioned table. Dedup is still exact in practice:
> the ingest path looks up `(project_id, dedup_key)` for the current and previous
> partition before inserting.

### Logs and metrics

Separated from `events` on purpose: logs are high-volume free text with a different
retention policy, and mixing them would make the event stream expensive to query.

```sql
CREATE TABLE logs (
  id          uuid NOT NULL DEFAULT gen_random_uuid(),
  project_id  uuid NOT NULL,
  agent_id    uuid,
  task_id     uuid,
  run_id      uuid,
  severity    event_severity NOT NULL DEFAULT 'info',
  message     text NOT NULL,
  context     jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL,
  received_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (project_id, id, received_at)
) PARTITION BY RANGE (received_at);

CREATE INDEX ON logs (project_id, received_at DESC);
CREATE INDEX ON logs USING gin (to_tsvector('simple', message));

CREATE TABLE metric_samples (
  project_id  uuid NOT NULL,
  agent_id    uuid,
  name        text NOT NULL,             -- 'tokens.input', 'latency_ms', 'cost.usd'
  value       double precision NOT NULL,
  unit        text,
  labels      jsonb NOT NULL DEFAULT '{}'::jsonb,
  occurred_at timestamptz NOT NULL,
  PRIMARY KEY (project_id, name, occurred_at, agent_id)
) PARTITION BY RANGE (occurred_at);

-- Rollup the dashboards actually read; refreshed by the worker.
CREATE MATERIALIZED VIEW agent_metrics_hourly AS
SELECT project_id, agent_id, name,
       date_trunc('hour', occurred_at) AS bucket,
       count(*) AS samples, sum(value) AS sum, avg(value) AS avg,
       percentile_disc(0.95) WITHIN GROUP (ORDER BY value) AS p95
FROM metric_samples
GROUP BY 1, 2, 3, 4;
CREATE UNIQUE INDEX ON agent_metrics_hourly (project_id, agent_id, name, bucket);
```

### Integrations and outbound webhooks

```sql
CREATE TABLE integrations (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  adapter_slug  text NOT NULL,            -- resolved against the adapter registry
  name          text NOT NULL,
  config        jsonb NOT NULL DEFAULT '{}'::jsonb,  -- validated by adapter.config_schema
  secret_hash   text,                     -- inbound HMAC secret, if the adapter verifies
  enabled       boolean NOT NULL DEFAULT true,
  last_inbound_at timestamptz,
  error_count   int NOT NULL DEFAULT 0,
  last_error    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, adapter_slug, name)
);

CREATE TABLE webhooks (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  project_id  uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  url         text NOT NULL,
  secret      text NOT NULL,              -- HMAC-SHA256 signing key for outbound calls
  event_types text[] NOT NULL DEFAULT '{"*"}',
  enabled     boolean NOT NULL DEFAULT true,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE webhook_deliveries (
  id          bigserial PRIMARY KEY,
  webhook_id  uuid NOT NULL REFERENCES webhooks(id) ON DELETE CASCADE,
  event_id    uuid NOT NULL,
  status      delivery_status NOT NULL DEFAULT 'pending',
  attempt     int NOT NULL DEFAULT 0,
  response_status int,
  response_body text,
  next_attempt_at timestamptz,
  created_at  timestamptz NOT NULL DEFAULT now(),
  delivered_at timestamptz
);
CREATE INDEX ON webhook_deliveries (status, next_attempt_at)
  WHERE status IN ('pending', 'failed');
```

## 6. Presentation (the island)

Isolated by design. Nothing here is referenced by any reducer, adapter or SDK; the
backend would behave identically if these two tables were dropped.

```sql
CREATE TABLE world_layouts (
  project_id  uuid PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE,
  theme       text NOT NULL DEFAULT 'island-temperate',
  grid_width  smallint NOT NULL DEFAULT 16,
  grid_height smallint NOT NULL DEFAULT 16,
  tiles       jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- [{"q":3,"r":5,"terrain":"grass","decor":"tree"}, ...]
  landmarks   jsonb NOT NULL DEFAULT '[]'::jsonb,
  -- [{"kind":"warehouse","q":8,"r":8}, {"kind":"plaza","q":8,"r":10}]
  updated_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE world_placements (
  agent_id      uuid PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  building_kind text NOT NULL,         -- 'workshop', 'library', 'lighthouse', ...
  character_kind text NOT NULL,        -- sprite set id
  tile_q        smallint NOT NULL,
  tile_r        smallint NOT NULL,
  facing        text NOT NULL DEFAULT 'south-east',
  accent_color  text,
  auto_assigned boolean NOT NULL DEFAULT true,
  updated_at    timestamptz NOT NULL DEFAULT now(),
  UNIQUE (project_id, tile_q, tile_r)
);
```

When a new agent registers, the control plane assigns a free tile and a
`building_kind` derived from `agents.kind` (with `auto_assigned = true`) so the island is
never empty on first connect. A user dragging a building sets `auto_assigned = false` and
their choice is then never overwritten.

## 7. Multi-tenant isolation

Two layers, because one is never enough:

1. **Application layer (primary).** Tenant-scoped repositories cannot be *constructed*
   without a `TenantScope(workspace_id, project_id?)`, so there is no such thing as an
   unscoped query against a tenant table. `tests/integration/test_tenant_isolation.py`
   calls every control-plane endpoint with a foreign resource id and asserts `404` (not
   `403`: existence is itself information) — and asserts the owner can do all of the same
   things, so those 404s cannot be explained by broken routes.
2. **Database layer (defence in depth).** PostgreSQL RLS, which catches the query that
   satisfies the type checker and still forgets its `WHERE`.

```sql
ALTER TABLE projects ENABLE ROW LEVEL SECURITY;
ALTER TABLE projects FORCE  ROW LEVEL SECURITY;
CREATE POLICY projects_tenant ON projects
  USING      (workspace_id = ANY (string_to_array(
                NULLIF(current_setting('agentworld.workspace_ids', true), ''), ',')::uuid[]))
  WITH CHECK (workspace_id = ANY (string_to_array(
                NULLIF(current_setting('agentworld.workspace_ids', true), ''), ',')::uuid[]));
```

Four details in there are load-bearing, and each was arrived at by the alternative
failing:

- **`FORCE`, not just `ENABLE`.** A table's owner bypasses its own policies. The
  application owns these tables in a single-role deployment, so `ENABLE` alone creates
  policies that are never consulted — and a test suite that passes while proving nothing.
- **The application must not be a superuser.** A superuser, or any role holding
  `BYPASSRLS`, ignores policies even under `FORCE`.
  `infra/compose/postgres-init/01-app-role.sql` creates `agentworld_app` for exactly this
  reason, and the *first* test in `test_rls.py` asserts the connected role cannot bypass,
  so the rest of that file cannot pass vacuously.
- **A set of workspaces, not one.** `GET /v1/projects/{id}` is given a project id and
  nothing else, so it must read `projects` to learn the workspace — which a single-id
  policy makes impossible, because the id it would filter on is what the read is for.
  Keying on the caller's whole visibility set dissolves the circle; every alternative
  needs a `SECURITY DEFINER` resolver or a `BYPASSRLS` role, and either is a backdoor
  someone eventually reaches for again.
- **`NULLIF` before the cast.** An unset session variable becomes SQL `NULL` rather than a
  cast error, and `= ANY (NULL)` is `NULL`, which RLS reads as "no rows". The failure mode
  of a missing tenant is total invisibility, not total visibility.

The session variable is written with `set_config(..., is_local => true)`, so it is scoped
to the transaction and cannot survive back into the connection pool to be read by the next
request that borrows that connection. `set_config` rather than `SET LOCAL` because `SET`
takes a literal, not a bind parameter — the difference between a parameterised query and
string concatenation on the one value that decides which tenants' data a request can see.

**What RLS does not cover, and why.** The rule is that RLS protects tenant *data*, and not
the tables that establish which tenant a request belongs to. Those are `users`,
`workspace_members`, `workspaces` and `api_keys`: a token carries only a subject, so the
first two produce the very set the policies read, and a presented API key carries only a
globally unique prefix, so `api_keys` is where the ingest plane discovers its workspace. A
policy on any of them would need the answer before the question. They are protected by the
application layer instead, and their whole cross-tenant surface is the few
`GlobalRepository` subclasses, each of which documents why it is unscoped — listed in one
place so the surface can be reviewed rather than discovered.

RLS is enabled from Phase 1 rather than retrofitted, because adding it to a live schema
means auditing every existing query.

**Append-only is a privilege, not a convention.** Migration `0005` grants the application
role `SELECT, INSERT` on `audit_log` and nothing else, so no code path can rewrite history
even if one tries; the attempt fails at the database. `events` and `logs` are granted the
same way in Phase 2.

## 8. Retention and growth

| Table | Policy |
|-------|--------|
| `events` | Monthly partitions. Detach and drop past `projects.settings.event_retention_days` (default 90). |
| `logs` | Monthly partitions, default 30 days. Severity floor per project drops debug lines at ingest. |
| `metric_samples` | Monthly partitions, raw 30 days; `agent_metrics_hourly` kept 13 months. |
| `agent_status_history` | Kept 1 year; it is low-volume and the most useful forensic table. |
| `webhook_deliveries` | 14 days for delivered, 90 for dead. |
| `audit_log` | Never auto-deleted. |

Rough sizing: an agent emitting 1 event/s produces ~2.6M events/month. At ~400 bytes
per row that is ~1 GB/month per such agent before indexes — hence partitioning from day
one, not as a later optimisation.

## 9. Migration plan

| # | Migration | Phase | State |
|---|-----------|-------|-------|
| `0001` | `extensions` — `pgcrypto`, `citext` | 1 | done |
| `0002` | `enums` — every native enum type | 1 | done |
| `0003` | `tenancy` — users, workspaces, members, projects, api_keys, audit_log | 1 | done |
| `0004` | `rls` — enable, FORCE and policy the tenant tables | 1 | done |
| `0005` | `app_role_grants` — privileges for the non-superuser application role | 1 | done |
| `0006` | `domain_core` — agents, tasks, runs, run_steps, status history | 2 | planned |
| `0007` | `append_only` — events, logs, metric_samples, partition maintenance | 2 | planned |
| `0008` | `integrations` — integrations, webhooks, deliveries | 4 | planned |
| `0009` | `rollups` — `agent_metrics_hourly` | 3 | planned |
| `0010` | `presentation` — world_layouts, world_placements | 6 | planned |

RLS moved from the planned `0008` to `0004`, ahead of any table it would otherwise have
had to be retrofitted onto. Each later migration enables RLS on its own tables as it
creates them, rather than one sweep at the end.

**A migration never imports application code.** The enum labels in `0002` are written out
as literals even though `domain.enums` holds the same list, because a migration that reads
current code stops being a frozen record of a change: when Phase 2 adds a member to
`agent_status`, `0002` would start creating it on a fresh database, and the Phase 2
`ALTER TYPE ... ADD VALUE` would then fail there while still succeeding on an existing
one. The duplication is checked instead, below.

Forward-only, and the application role holds no DDL rights: migrations run as the schema
owner, through `MIGRATION_DATABASE_URL`.

**Drift is tested, not trusted.** Three sources of truth have to agree — the Python enums,
the migrations, and the SQLAlchemy models — and `tests/integration/test_schema.py` asks a
freshly migrated database whether they do. It compares `pg_enum` against
`domain.enums.PG_ENUMS` label for label and in declared order (PostgreSQL compares enum
values by that order, so it is not cosmetic), in both directions, and runs Alembic's own
`compare_metadata` over the models. That is what makes it safe to hand-write migrations —
which `citext`, partial indexes and check constraints require — without the models quietly
describing a different schema.
