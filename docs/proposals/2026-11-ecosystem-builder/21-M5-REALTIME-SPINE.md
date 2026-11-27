# M5 — the realtime spine: event bus, `ws-ticket`, gateway

> Step 3 of `docs/STATUS.md` §"Next steps, in order". Implemented 2026-11-26.
> Measured by **R1–R14** in [`04-ROADMAP-AND-MVP.md`](04-ROADMAP-AND-MVP.md); designed by
> **P3–P13** in [`07-DECISION-RECORD.md`](07-DECISION-RECORD.md) §6 and the frame set in
> [`03-API-AND-FRONTEND.md`](03-API-AND-FRONTEND.md) §4 with the client/server frames of
> `docs/03-API.md` §6.

This document records what the spine is, and - the part worth reading - the four questions
the approved design deliberately left to the implementation, with the answer each one got
and the reason. Every frame's *name*, the cursor's meaning and every refusal below come from the
documents named above. Where those documents leave a socket something it cannot leave
open - a payload key, a reason string, an error code, a channel for a snapshot key they
list - §0 names what was chosen and against which sentence, so none of it has to be
inferred from the code.

## 0. Eleven places this differs from, or adds to, the written protocol

Listed first because they are protocol surface, and a reader who only skims should not
have to find them in §3. None of them changes a frame's shape or the cursor's meaning;
each one is a gap the documents leave that a socket cannot leave open.

| # | What | Why, and what the document says |
|---|---|---|
| 1 | **No server-initiated heartbeat *frame*. Transport ping/pong stays on.** | **Corrected 2026-11-26**; an earlier revision of this row said "the server initiates nothing", which is wrong one layer down. `docs/03-API.md` §6's frame list has `ping` **client→server only** and `pong` **server→client only** - there is no documented server→client `ping` frame - so the implemented frame set matches it exactly, and R14 ("with no activity there is no synthetic heartbeat, status or delta") governs frames and is satisfied. §6's separate *Guarantees* line, "server pings every 30s, closes after two misses", is a transport keepalive and **is in force**: uvicorn defaults to `ws_ping_interval=20.0` / `ws_ping_timeout=20.0` and nothing here overrides it, so RFC 6455 pings go out every 20s and a silent socket is closed. The only deviation left is 20s against the documented 30s, which `--ws-ping-interval 30 --ws-ping-timeout 30` closes if the figure matters |
| 2 | **An optional `cursor` query parameter on the socket.** | `resume` is a client *frame*, and `03-API.md` §6 also says the snapshot is sent "immediately on connect" - so a reconnecting client with a cursor would be sent a full snapshot it is about to discard before it could ask for a replay. The parameter is the connect-time form of the same request. The `resume` frame is implemented too, and both are tested |
| 3 | **Eight channels: one per snapshot key, plus `events`** - which adds `connections` and `integrations` to the six `03-…` §4 names in passing. **Two qualifications, stated 2026-11-26 after review found the rule claimed more than it delivers.** First, `step.updated` rides the `executions` channel although **no snapshot key describes steps** - `ExecutionView` carries none, by its own docstring - so "a client cannot be sent entities its snapshot never described" is false for steps, and a client subscribed to `executions` must expect them. Second, **three channels are advertised and unserved**: no frame carries `integrations` or `approvals`, because neither table exists (Phases 3a and 6), and none carries `world`, because the `world/nodes` read API is step 7 - so moving a building produces no delta today even though `03-API.md` §6 documents a `world.updated` frame. Subscribing to any of the three is a silent no-op. They are left in the set so step 7 and Phase 3a need no protocol change |
| 4 | **`subscribe` adds; `unsubscribe` removes.** | `03-API.md` §6 lists both frames and describes "an open log viewer adds `events`". A `subscribe` that replaced the set would make `unsubscribe` redundant. Default is every channel, since the snapshot carries every key |
| 5 | **A refused ticket gets an `error` frame, then the close.** | R1 says "refused before a single frame is sent", and no frame of *state* precedes it: the socket is accepted only so the refusal can be said and nothing is read from the database. `03-API.md` §6 documents the frame's *shape* with the example code `ticket_expired`; the codes used are `invalid_ticket` for every ticket refusal - one code for every cause, as the API-key path does, because a reason would say whether the id existed and whether it had been spent - plus `malformed_frame` and `unknown_frame` for a client frame the server cannot act on. A bare close gives a browser an `onerror` and nothing to show a person |
| 6 | **The tombstone's `data` is `{project_id, entity, id}`.** | §4's example shows `{ "id": "…" }`, and R7 enumerates "project, entity type, entity id and the cursor, and nothing else" - which is the longer list. The frame carries R7's four; the example was illustrative |
| 7 | **`step.updated` carries an `execution_id` beside the step view.** | A `step_key` is unique within an execution and nowhere else, so a client could not place the step without it. The payload still validates against `ExecutionStepView`, which is what R7 asks of a delta; the extra key is addressing, not state |
| 8 | **`resync_required` has a second reason, `consumer_too_slow`.** | `03-API.md` §6 shows `cursor_too_old`. R12 is a different cause with the same remedy, and an operator reading logs needs to tell "asked for something too old" from "could not keep up". Both close the socket |
| 9 | **The snapshot includes archived agents and workflows**, which the REST listings hide. | An archive is an `.updated` frame carrying the whole entity (P11.4), so a client that had never been sent the entity would be told to update something it does not hold. The snapshot's set and the delta's set have to agree; `archived_at` is in the view, and what to show is the client's decision |
| 11 | **A step event's `payload` is withheld on the `events` channel, and the frame says so with `payload_withheld: true`.** | Found in review, 2026-11-26, and fixed in code. For an event folding into `execution_steps` the payload is the unnarrowed source of `ExecutionStepView.output` and `.error`, which `api/v1/executions.py` narrows on purpose - `transform` output withheld whole, an `agent_task` turn projected by field name - and whose docstring says serving what it drops "would make the narrowing below decorative". The `events` channel is readable at `viewer` **and in the default subscription set**, so the payload was reaching the weakest role unasked. Withheld whole rather than filtered by key, because `workflow.step_succeeded` is `AGENT_FOR_DISPATCHED_STEP`: the agent writes that payload, so a blocklist would be a guess at what it names the rest. The authorized entity still arrives on `step.updated`, same batch, same cursor. `payload_withheld` is new frame surface, and is there because a bare `{}` would mean both "the producer sent nothing" and "read the entity delta instead" |
| 10 | **The `event` frame's payload shape is defined here**, as `RawEventView`. | There is no REST route for the raw feed yet, so there is no view to share - which is also why R7's "validates against the same view model the REST route returns" is about the deltas. It carries the event as recorded: id, seq, type, `occurred_at`, subject, correlation, severity, labels, payload |

## 1. What was built

| Piece | Where | What it does |
|---|---|---|
| The bus | `infra/event_bus.py` | `EventBus` with `InProcessEventBus` and `RedisEventBus`, channel `project:{id}` (`01-ARCHITECTURE.md` §5.2), bounded per-subscription queues |
| Frames | `api/ws/frames.py` | **The one** mapping from a committed `events` row to client frames, used by the live path and the replay path both |
| The publisher | `api/ws/publisher.py` | Reads committed rows from a per-project position, builds frames, publishes. P10's Option A, with no outbox table |
| The ticket | `api/v1/realtime.py`, `services/realtime.py` | `POST /v1/projects/{id}/ws-ticket`: 60 seconds, single-use, `realtime.subscribe` at `viewer` |
| The gateway | `api/ws/gateway.py` | `GET /v1/ws/projects/{id}?ticket=…`: snapshot, deltas, `resume`, `resync_required`, ping/pong |
| Two tables | migration `0027` | `ws_tickets` and `event_publication_cursors`. Nothing else; the event row is the record |

**The producer side was not touched.** All fourteen P13 producers already appended their
`events` row in the mutation's own transaction; what R8 was missing was publication *after*
that transaction commits, and that is what this adds. No reducer changed, `fold()` gained
no `definition` key, and the 60 tests in `tests/integration/test_definition_events.py` are
unchanged.

## 2. The four questions the design left open

### 2.1 Where the publisher runs — P10.5, now answered

P10.5 said "the existing worker hosts the publisher, **if it can**", and asked for the
limitation to be written down rather than worked around if it could not. Two findings.

**The worker hosts it on the `redis` bus, as its own task.** `workers/worker.py` starts
`run_publisher` beside the dispatch loop. Not inside it: a dispatch can hold a provider call
for thirty seconds, and a delta a viewer is waiting for cannot queue behind that.

**The give-up bound does not apply to it, and that is the recorded limitation.**
`WORKER_MAX_CONSECUTIVE_ERRORS` exits the process after N consecutive failures, which is
right for dispatches - a worker spinning against a database that refuses it should become a
supervisor's problem - and wrong for a publisher that must keep trying through a Redis
restart. So the publisher counts its own failures, logs each one, and never exits. Nothing
is risked by that: its position is in PostgreSQL, so an outage of any length costs latency
and no data. A failure in publication neither stops a dispatch nor the process.

**On the `inprocess` bus the API process publishes for itself**, in its lifespan. This is
not a second host for one job; it is the only process that *can* do the job when the bus
does not cross processes - a message put on an in-process bus reaches the sockets that
process holds and no others, which is exactly why `Settings` refuses `inprocess` outside
`local` and `test`. `publishes_in_api(kind)` is the single place that decides, and
`test_realtime_fanout.py` asserts both halves of it.

### 2.2 R11's replay window — an operational definition

R11 recorded that "beyond the replay window" had **no definition**: `events` is partitioned
monthly, nothing prunes a partition, and so no cursor was unreachable. It now has one, and
it is two conditions rather than one:

1. **`WS_REPLAY_WINDOW_EVENTS`** (default 10,000, the number `03-API.md` §6 already
   published) bounds how far back a cursor may *ask*. This is the half that bounds the work
   a reconnecting client can demand of the database.
2. **The oldest retained row** bounds how far back the rows actually go. This is the half a
   dropped partition changes, and it is checked now so that retention arriving is a
   configuration change rather than a protocol one.

A cursor ahead of the project's latest is refused the same way: a client claiming a position
that does not exist is not stale, but the answer to it is the same fresh snapshot.

**The setting's `ge=100` floor was not relaxed for the test.** A window small enough to be
convenient in a test would be a deployment that re-snapshots every client on every burst,
and loosening a production bound to shorten a test is how the bound stops meaning anything.
`test_r11_a_cursor_beyond_the_window_gets_resync_required_and_nothing_else` writes 101
events instead, and the pruned-partition half is asserted by deleting rows directly - which
is what retention will do.

### 2.3 Where R13 runs in CI — the `api` job, with Redis

R13 needs two gateway processes on the `redis` bus, and the `api` job started PostgreSQL
only. Two options: move the test to the `compose` job, which starts the whole stack, or
start Redis in the job that runs the suite.

**Redis is now started in the `api` job.** The `compose` job checks that the stack comes up;
it does not run pytest, so a test living there would need a second way to be invoked and
would not be covered by `AGENTWORLD_REQUIRE_INTEGRATION=1`. That variable is the reason this
matters: it turns a skipped integration test into a failure, so a Redis-dependent test with
no Redis fails the job rather than quietly proving nothing. `test_realtime_fanout.py` has
its own gate with the same rule, because the package's gate covers PostgreSQL alone.

The test starts two real `uvicorn` processes against the test database and publishes from a
third `RealtimePublisher` - so the only path from the mutation to the two sockets is a
committed row, a publisher that read it, and a Redis channel. Mis-pointing that publisher at
an unreachable Redis fails the test, which is how the bus was confirmed to be load-bearing
rather than incidental.

### 2.4 Two places the implementation had to choose, and why

**The snapshot runs at `REPEATABLE READ`.** R6 asks that an entity mutated during the
handshake appear *exactly once* across the snapshot and the deltas. The snapshot reports one
cursor and a set of entities, and under `READ COMMITTED` a mutation committing between those
two reads lands in the entities while the cursor excludes it - so it is in the snapshot and
is then replayed. One consistent read fixes it without a lock, and because the sequence is
allocated under a lock held until commit, "the highest `seq` this transaction can see" is
exactly the boundary between the two. `Database.session` gained an `isolation_level`
parameter for this one caller.

The alternative - taking the project's cursor lock for the duration of the snapshot - would
also be correct and would stall a project's ingest once per connecting viewer. It was
rejected for that.

**Deduplication is per event row, never per frame.** One row can produce several frames on
one cursor: an ingested step outcome is `event` *and* `step.updated`. A connection that
remembered "the last cursor I sent" and dropped anything at or below it would deliver the
first frame and swallow the second. So the bus carries `{seq, frames[]}` for every row -
including rows that produce no frame - and the gateway tracks the last *row* it handled. The
sequence is gapless per project, so `seq > last + 1` is a detectable hole, which the gateway
closes from `events` rather than guessing at. That is also what makes at-least-once
publication safe to assert rather than assume.

## 3. What this does not add, and why

- **`integrations` and `approvals` in the snapshot are empty lists.** Neither table exists;
  they arrive in Phases 3a and 6. The keys are present rather than omitted, because a client
  that must discover which keys exist is a client that breaks when one appears.
- **`world` is `{"layout": {}, "placements": []}`.** The `world/nodes` read API is step 7 of
  this phase and owns that view model. Populating it here would build it twice.
- **No server heartbeat frame, and the channel set.** Both are in §0, beside the
  documents they depart from and the reason each one does.
  `test_r14_with_no_activity_the_server_sends_nothing` asserts the first - and note that
  it asserts the absence of a *frame*: uvicorn's transport ping is below the test client
  and stays on. The channel set is one per snapshot key plus `events`, with the two
  qualifications §0 item 3 now states rather than glosses.
- **Nothing sweeps spent tickets.** They are rubbish within a minute and the index for a
  sweep exists; retention is a later phase's, and this is recorded rather than assumed.

## 4. Which criteria this closes

R1–R14 all pass, each with a named test; `04-ROADMAP-AND-MVP.md` carries the test names on
the boxes themselves. The suite is `test_realtime_gateway.py` (R1–R8, R14),
`test_realtime_resume.py` (R9–R12) and `test_realtime_fanout.py` (R13), with R3's foreign-id
half inside `test_tenant_isolation.py`'s sweep and R4's row inside `test_role_matrix.py`,
both deliberately in the existing sweeps so a route added beside them cannot miss them.

**Three tests exist for seams no criterion names**, because each is a line whose deletion
would otherwise fail nothing:

- `test_realtime_worker_host.py` drives `workers.worker._start_publisher` - the hosting
  decision of §2.1 - and asserts that it starts nothing on `inprocess`, that on `redis` it
  publishes a committed row a subscriber receives, and that the loop survives six
  consecutive failures instead of exiting. R13 deliberately publishes from its own
  publisher, since two gateway processes is what that criterion is about, so without this
  the worker's half of P10.5 had nothing exercising it.
- `test_the_handshake_opens_its_snapshot_at_repeatable_read` watches the isolation level
  the *handshake* asks for. R6's own test passes the level itself, so dropping the
  argument from the gateway would have left it green.
- `test_an_ingested_event_produces_an_agent_delta_and_a_raw_event` covers R9 through the
  **ingest** plane, which is the wording R9 uses; the rest of R9 goes through `run` and
  `advance_once`. It is also the branch the island's "status equal to the REST view and
  arriving as a delta" box depends on, asserted one layer below that box.
- `test_realtime_event_payloads.py` is the whole of §0 item 11: what the raw `events`
  channel may carry. Its first test is **total over the taxonomy** - every registered type
  is checked against `NARROWED_BY_READ_MODEL` rather than against a list of names, so a
  type added later is covered the moment it declares what it folds into - and its last two
  drive the concrete case through the real ingest plane, a dispatchable agent reporting its
  own step with fields of its choosing. Reverting the one line that withholds the payload
  turns 14 of them red.

Both of the behaviours §2.4 argues for were confirmed by mutation: removing the
`REPEATABLE READ` argument and removing the cursor assignment in `_replay_from` each turn
a passing test red.

**This does not close Phase 3.** Steps 4 to 9 of `docs/STATUS.md` remain: T13, T7a, T8, the
`world/nodes` read API, the island skeleton and the live-provider run. The spine is what the
island's four boxes were waiting on.
