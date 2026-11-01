"""Append-only, partitioned: events, logs, metric_samples.

Phase 2. The event log is the replay source, so this is the table the whole
pipeline's honesty rests on. Three things about partitioning had to be worked
around rather than wished away.

**A unique constraint on a partitioned table must include the partition key.**
So `UNIQUE (project_id, dedup_key)` is not declarable on the parent, and
declaring `(project_id, dedup_key, received_at)` instead would permit the same
`dedup_key` twice at different timestamps - which is exactly what idempotent
ingest must prevent. The dedup index therefore lives on **each partition**,
created alongside it. The honest statement of the guarantee: a `dedup_key` is
unique per project **within a monthly partition**. A retry crossing a month
boundary would not be caught by the index. Ingest also checks explicitly
before inserting, and a retry arrives in seconds rather than weeks, so the
window is theoretical - but it is a window, and pretending otherwise would be
worse than naming it. A non-partitioned dedup side-table would close it at the
cost of a second index insert per event, and is the upgrade if it ever matters.

**Neither privileges nor RLS are inherited by a partition.** This is the
part that bites, and it bites twice. `REVOKE UPDATE, DELETE ON events` stops
`UPDATE events ...` and not `UPDATE events_y2026m11 ...`; worse, the tenant
policy on the parent is *not* applied to a query that names a partition
directly, so `SELECT * FROM events_y2026m11` would have returned every
tenant's events. Migration 0005's default privileges hand each newly created
table `SELECT, INSERT`, so every partition arrives with exactly the access
that opens both holes.

What closes them is that a partition needs no privileges of its own to be
used through its parent - verified, not assumed: with `ALL` revoked on the
partition, an `INSERT INTO events` still routes into it and `SELECT FROM
events` still reads it, while `SELECT FROM events_y2026m11` is refused. So the
application role is granted access to the three parents and to no partition,
and the maintenance function revokes `ALL` on every partition it creates.

The invariant that follows is worth stating plainly, because it is what
tenant isolation on these three tables rests on: **the application role must
hold no privilege on any partition.** Granting it one would reopen
cross-tenant reads, and `test_append_only.py` asserts it over every partition
so that a later grant fails a test rather than leaking data.

**Creating a partition needs DDL rights the application role does not have
and should not get.** `agentworld_ensure_partitions` is therefore
`SECURITY DEFINER`: it runs as the schema owner, so a worker on the
application role can call it. That is a deliberate, narrow exception - the
body is fixed, it can only create partitions of three named tables with
computed names, and `search_path` is pinned so nothing can be hijacked by a
caller-controlled schema. The retention counterpart that *drops* old
partitions arrives with the retention job; nothing needs it until data is
ninety days old.

Revision ID: 0011
Revises: 0010
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import CITEXT, ENUM, JSONB

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SEVERITY = ENUM(name="event_severity", create_type=False)

#: The three partitioned parents. The maintenance function reads this list.
PARTITIONED: tuple[str, ...] = ("events", "logs", "metric_samples")

APP_ROLE = "agentworld_app"

SETTING = "agentworld.workspace_ids"

_VISIBLE = (
    f"workspace_id = ANY (string_to_array("
    f"NULLIF(current_setting('{SETTING}', true), ''), ',')::uuid[])"
)

#: Months of partitions to keep ahead. Two is enough that a month boundary is
#: never a cliff, and the worker calls this far more often than monthly.
MONTHS_AHEAD = 2


def upgrade() -> None:
    _events()
    _logs()
    _metric_samples()
    _maintenance_function()

    # The current month and the next two, so the tables are usable at once.
    op.execute(f"SELECT agentworld_ensure_partitions({MONTHS_AHEAD})")

    for table in PARTITIONED:
        op.execute(f"ALTER TABLE {table} ENABLE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} FORCE ROW LEVEL SECURITY")
        op.execute(
            f"CREATE POLICY {table}_tenant ON {table} USING ({_VISIBLE}) WITH CHECK ({_VISIBLE})"
        )

    # Append-only, through the only door the application role can open: it
    # has `SELECT, INSERT` on the parents and nothing at all on the
    # partitions, so these two verbs are the whole of what it could have
    # used to rewrite history.
    op.execute(
        _conditional_app_role(
            [f"REVOKE UPDATE, DELETE ON {table} FROM {APP_ROLE}" for table in PARTITIONED]
        )
    )


def downgrade() -> None:
    for table in reversed(PARTITIONED):
        op.execute(f"DROP POLICY IF EXISTS {table}_tenant ON {table}")
        op.execute(f"ALTER TABLE {table} NO FORCE ROW LEVEL SECURITY")
        op.execute(f"ALTER TABLE {table} DISABLE ROW LEVEL SECURITY")

    op.execute("DROP FUNCTION IF EXISTS agentworld_ensure_partitions(integer)")
    # Dropping a partitioned parent takes its partitions with it.
    op.drop_table("metric_samples")
    op.drop_table("logs")
    op.drop_table("events")


def _events() -> None:
    op.create_table(
        "events",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        # Per-project monotonic cursor, allocated inside the ingest
        # transaction. What WebSocket resume and cursor pagination read.
        sa.Column("seq", sa.BigInteger(), nullable=False),
        sa.Column("dedup_key", CITEXT(), nullable=False),
        sa.Column("type", sa.Text(), nullable=False),
        sa.Column("protocol_version", sa.Text(), nullable=False),
        # The client's clock, and the server's. Skew is recorded, never
        # silently corrected.
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        # What the event is about: replaces the previous design's flat
        # agent_key/task_ref/run_ref trio, which could not express an
        # execution, a step or an approval without one optional id each.
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("subject_ref", sa.Text(), nullable=False),
        # Resolved references, filled by ingest where the refs name something
        # that exists. No foreign keys: this table outlives the rows it
        # points at, and a cascade would delete history with the entity.
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("execution_id", sa.Uuid(), nullable=True),
        sa.Column("execution_step_id", sa.Uuid(), nullable=True),
        sa.Column("correlation", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column(
            "severity", _SEVERITY, server_default=sa.text("'info'::event_severity"), nullable=False
        ),
        sa.Column("labels", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("payload", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.CheckConstraint("seq > 0", name="event_seq_is_positive"),
        # The partition key has to be in the primary key. `(project_id, seq)`
        # is also exactly the cursor query's access path.
        sa.PrimaryKeyConstraint("project_id", "seq", "received_at", name="events_pkey"),
        postgresql_partition_by="RANGE (received_at)",
    )
    # Parent-level indexes propagate to every partition, present and future.
    op.create_index("events_id_idx", "events", ["id"])
    op.create_index("events_feed_idx", "events", ["project_id", sa.text("received_at DESC")])
    op.create_index(
        "events_agent_idx",
        "events",
        ["agent_id", sa.text("received_at DESC")],
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )
    op.create_index(
        "events_execution_idx",
        "events",
        ["execution_id", "seq"],
        postgresql_where=sa.text("execution_id IS NOT NULL"),
    )


def _logs() -> None:
    op.create_table(
        "logs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("task_id", sa.Uuid(), nullable=True),
        sa.Column("run_id", sa.Uuid(), nullable=True),
        sa.Column("execution_step_id", sa.Uuid(), nullable=True),
        sa.Column(
            "severity", _SEVERITY, server_default=sa.text("'info'::event_severity"), nullable=False
        ),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("context", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("project_id", "id", "received_at", name="logs_pkey"),
        postgresql_partition_by="RANGE (received_at)",
    )
    op.create_index("logs_feed_idx", "logs", ["project_id", sa.text("received_at DESC")])
    op.create_index(
        "logs_agent_idx",
        "logs",
        ["agent_id", sa.text("received_at DESC")],
        postgresql_where=sa.text("agent_id IS NOT NULL"),
    )
    # The full-text index for log search arrives with the Phase 3 read
    # endpoints, which is when its cost starts buying something.


def _metric_samples() -> None:
    op.create_table(
        "metric_samples",
        sa.Column("id", sa.Uuid(), server_default=sa.text("gen_random_uuid()"), nullable=False),
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("agent_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("value", sa.Double(), nullable=False),
        sa.Column("unit", sa.Text(), nullable=True),
        # A fixed allow-list of low-cardinality names, enforced at ingest:
        # an unbounded label set here is how a metrics table becomes
        # unqueryable.
        sa.Column("labels", JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("occurred_at", sa.TIMESTAMP(timezone=True), nullable=False),
        sa.Column(
            "received_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("project_id", "id", "received_at", name="metric_samples_pkey"),
        postgresql_partition_by="RANGE (received_at)",
    )
    op.create_index(
        "metric_samples_series_idx",
        "metric_samples",
        ["project_id", "name", sa.text("received_at DESC")],
    )


def _maintenance_function() -> None:
    """Create monthly partitions, their dedup index, and their privileges.

    SECURITY DEFINER with a pinned search_path: the application role has no
    DDL rights and should keep none, but a worker running as it must be able
    to ensure next month's partition exists before the boundary.
    """
    parents = ", ".join(f"'{name}'" for name in PARTITIONED)
    op.execute(f"""
CREATE OR REPLACE FUNCTION agentworld_ensure_partitions(months_ahead integer DEFAULT 2)
RETURNS integer
LANGUAGE plpgsql
SECURITY DEFINER
-- Pinned with pg_catalog first, so nothing in the body resolves through a
-- caller-controlled schema. Mandatory for any SECURITY DEFINER function -
-- and it means every object this function CREATES must be schema-qualified,
-- because an unqualified CREATE would target the first schema in the path.
SET search_path = pg_catalog, public
AS $function$
DECLARE
  parent      text;
  month_index integer;
  range_start date;
  range_end   date;
  part        text;
  created     integer := 0;
BEGIN
  IF months_ahead < 0 OR months_ahead > 24 THEN
    RAISE EXCEPTION 'months_ahead must be between 0 and 24, got %', months_ahead;
  END IF;

  FOREACH parent IN ARRAY ARRAY[{parents}] LOOP
    FOR month_index IN 0..months_ahead LOOP
      range_start := (date_trunc('month', now()) + make_interval(months => month_index))::date;
      range_end   := (range_start + interval '1 month')::date;
      part := format('%s_y%sm%s', parent,
                     to_char(range_start, 'YYYY'), to_char(range_start, 'MM'));

      IF to_regclass(format('public.%I', part)) IS NULL THEN
        EXECUTE format(
          'CREATE TABLE public.%I PARTITION OF public.%I FOR VALUES FROM (%L) TO (%L)',
          part, parent, range_start, range_end);

        -- Idempotent ingest lives here rather than on the parent: a unique
        -- constraint on a partitioned table must include the partition key,
        -- and including received_at would let the same dedup_key through
        -- twice at different timestamps.
        IF parent = 'events' THEN
          EXECUTE format(
            'CREATE UNIQUE INDEX %I ON public.%I (project_id, dedup_key)',
            part || '_dedup_idx', part);
        END IF;

        -- A new partition picks up migration 0005's default privileges,
        -- which would let the application role address it directly - past
        -- both the append-only revoke and the tenant policy, neither of
        -- which a partition inherits. It needs none of its own to be used
        -- through the parent, so it gets none.
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{APP_ROLE}') THEN
          EXECUTE format('REVOKE ALL ON public.%I FROM {APP_ROLE}', part);
        END IF;

        created := created + 1;
      END IF;
    END LOOP;
  END LOOP;

  RETURN created;
END;
$function$;
""")
    op.execute(
        _conditional_app_role(
            [f"GRANT EXECUTE ON FUNCTION agentworld_ensure_partitions(integer) TO {APP_ROLE}"]
        )
    )


def _conditional_app_role(statements: list[str]) -> str:
    """Run statements only if the application role exists.

    A deployment running the application as the schema owner has no such
    role, and a migration that failed over a role it does not use would be a
    poor welcome - the same reasoning as migration 0005.
    """
    body = "\n".join(f"    EXECUTE {_quote(statement)};" for statement in statements)
    return (
        "DO $$\nBEGIN\n"
        f"  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = {_quote(APP_ROLE)}) THEN\n"
        f"{body}\n"
        "  END IF;\nEND $$;"
    )


def _quote(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"
