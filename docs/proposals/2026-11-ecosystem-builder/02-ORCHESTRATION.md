# Orchestration and workflow execution

> Proposal, 2026-11-01. Deliverable 6.

## 1. The four capabilities, kept apart

The brief asks that these not be confused. Here is where each one lives, so the boundary
is a module boundary rather than an intention.

| Capability | Question | Component | Property |
|-----------|----------|-----------|----------|
| **Observability** | What happened? | `events` + reducers | append-only, replayable, pure |
| **Orchestration** | What should happen next? | `domain/orchestration/advance.py` | **pure, no I/O, no provider** |
| **Execution** | Who performs it, how? | `workers/` + `infra/providers/` + `infra/tools/` | effectful, at-least-once |
| **Control** | Pause, stop, approve | control-plane services | audited, role-gated |

The one that is easy to get wrong is orchestration. If deciding and performing live in
the same function, then the decision cannot be tested without a provider key and a network,
and replay cannot reproduce the decision without repeating the work.

## 2. The central problem, and the resolution

An event log you replay and an instruction queue you must not replay cannot be the same
table. Stated as the failure it prevents:

> The `post` step calls `send_slack_message`. If dispatching were a reducer's side
> effect, then replaying the log to rebuild state after a bad deploy posts the message
> again — and the replay-equivalence test, which exists to prove the pipeline is honest,
> would be the thing that sent it.

**The resolution is a transactional outbox.** One transaction writes three things:

```
BEGIN
  INSERT INTO events          (the decision, as a fact)      -- replayed
  UPDATE execution_steps      (fold the decision into state) -- rebuilt by replay
  INSERT INTO dispatches      (the intent, as work to do)    -- NEVER replayed
COMMIT
```

Then a worker consumes `dispatches` and performs the side effect. The consequences:

- **Atomic.** No state that claims a step was dispatched with nothing to dispatch it, and
  no dispatch for a step whose state was rolled back.
- **Replay-safe.** Replaying `workflow.step_dispatched` sets the step to `dispatched`.
  Appending to `dispatches` is not something a reducer does, so no reducer can cause work.
  Promise 7 in `01-DOMAIN-AND-PROTOCOL.md` §9 asserts it by replaying with the workers
  running and checking no dispatch row appears.
- **Crash-safe without distributed transactions.** The alternative — write the event, then
  call Slack — loses work when the process dies between the two, or double-posts if it
  retries. The outbox moves the problem to "deliver this row at least once", which a
  unique key and an idempotent receiver solve.
- **No message broker in v1.** `dispatches` in PostgreSQL with `FOR UPDATE SKIP LOCKED` is
  the same mechanism the task claim already uses. Redis or SQS can replace it later behind
  the same interface; neither is needed at this scale and each is another thing to operate.

## 3. The dispatch outbox

```sql
CREATE TYPE dispatch_kind AS ENUM (
  'managed_agent_turn',  -- run a managed agent's loop for one step
  'tool_call',           -- invoke one tool
  'agent_push',          -- HTTP POST work to a dispatchable agent
  'agent_task_offer',    -- make a task claimable by a polling agent
  'webhook_emit'         -- outbound notification
);
CREATE TYPE dispatch_status AS ENUM ('pending','leased','succeeded','failed','dead','cancelled');

CREATE TABLE dispatches (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  execution_id  uuid REFERENCES workflow_executions(id) ON DELETE CASCADE,
  execution_step_id uuid REFERENCES execution_steps(id) ON DELETE CASCADE,
  kind          dispatch_kind NOT NULL,
  status        dispatch_status NOT NULL DEFAULT 'pending',
  -- The whole instruction, resolved: the step's input after mapping, the
  -- agent or tool to use, credential *references* and never credentials.
  instruction   jsonb NOT NULL,
  attempt       int NOT NULL DEFAULT 0,
  max_attempts  int NOT NULL DEFAULT 3,
  -- A lease, not a lock: a worker that dies holds nothing. The reaper
  -- returns rows whose lease expired to `pending`.
  leased_by     text,
  leased_until  timestamptz,
  run_after     timestamptz NOT NULL DEFAULT now(),
  last_error    text,
  created_at    timestamptz NOT NULL DEFAULT now(),
  finished_at   timestamptz,
  -- The idempotency guarantee: one dispatch per step attempt. Calling
  -- `advance` twice concurrently cannot produce two.
  UNIQUE (execution_step_id, kind, attempt)
);
CREATE INDEX dispatches_claimable_idx ON dispatches (run_after, id)
  WHERE status = 'pending';
CREATE INDEX dispatches_expired_lease_idx ON dispatches (leased_until)
  WHERE status = 'leased';
```

`dispatches` is **not** append-only and **not** under the event log's retention: rows are
updated as they progress and pruned after completion. It is operational state, not
history — the history is in `events`.

Claiming:

```sql
UPDATE dispatches SET status='leased', leased_by=:worker,
       leased_until=now() + interval '60 seconds', attempt=attempt+1
WHERE id IN (
  SELECT id FROM dispatches
  WHERE status='pending' AND run_after <= now()
  ORDER BY run_after, id
  FOR UPDATE SKIP LOCKED
  LIMIT :batch
)
RETURNING *;
```

A lease rather than a lock, because a worker killed mid-call must not block the row
forever, and a lease that expires is recoverable without an operator.

## 4. The orchestrator is a pure function

```python
# domain/orchestration/advance.py — no I/O, no provider, no SQLAlchemy.
def advance(definition: WorkflowDefinition, state: ExecutionState) -> list[Decision]:
    """What should happen next. Deterministic, and total over the state space."""
```

```python
Decision =
  | DispatchStep(step_key, attempt, kind, instruction)
  | RequestApproval(step_key, title, summary, payload, required_role, expires_at)
  | CompleteStep(step_key, output)          # condition/transform: no side effect
  | SkipStep(step_key, reason)
  | RetryStep(step_key, attempt, run_after)
  | FailExecution(error)
  | CompleteExecution(output)
  | PauseExecution(reason)
```

The service layer applies decisions transactionally: each becomes an event, a state
change, and — for `DispatchStep` only — an outbox row.

Four properties this buys, and each is a test:

1. **Unit-testable with no database and no network.** A table-driven test per step kind,
   including every out-of-order and partial-failure case, which is where the orchestration
   bugs actually live. The same reason the reducers are pure.
2. **Idempotent.** `advance` emits a decision only for steps whose state does not already
   reflect it. Calling it twice on the same state yields the same decisions, and the
   `UNIQUE (execution_step_id, kind, attempt)` constraint makes a concurrent double-apply
   a constraint violation rather than two Slack messages.
3. **Deterministic.** No clock reads and no randomness inside: `now()` and any generated
   id arrive in `ExecutionState`. So replaying a recorded execution's state sequence
   reproduces the same decisions, which is how a production incident becomes a unit test.
4. **Provider-free.** It decides *that* a managed agent turn should run, never *how*. The
   import-linter contract that forbids provider SDKs in `domain/` covers it unchanged.

### The loop

```
advance(definition, state):
  if state.cancelled:                  -> [FailExecution | nothing]
  ready = steps whose incoming edges are all satisfied
          and whose edge conditions evaluate true
          and which are still `pending`
  for step in ready:
     match step.kind:
       condition  -> evaluate expression over prior outputs; CompleteStep;
                     unsatisfied outgoing branches mark their targets SkipStep
       transform  -> apply mapping; CompleteStep            (both pure, inline, no dispatch)
       approval   -> RequestApproval                        (STOPS this branch)
       wait       -> RetryStep(run_after=deadline)
       tool_call  -> check grant_tool connection exists and is enabled
                     if tool.requires_approval or connection.requires_approval
                        and no granted approval for this attempt -> RequestApproval
                     else DispatchStep(kind='tool_call')
       agent_task -> refuse if agent.interaction_mode == 'observed'
                     managed      -> DispatchStep('managed_agent_turn')
                     dispatchable -> DispatchStep('agent_push' | 'agent_task_offer')
  if nothing ready and nothing in flight:
     all terminal steps succeeded -> CompleteExecution
     any failed with on_failure='fail' -> FailExecution
```

`advance` is called when: an execution is created; a dispatch reports an outcome; an
approval is decided; a wait expires; an operator resumes. Never on a timer — a timer would
make throughput depend on polling interval and hide a missed wake-up.

### Concurrency

One advancer per execution at a time: `SELECT ... FROM workflow_executions WHERE id = :id
FOR UPDATE` for the duration of the decide-and-apply transaction. Executions are
independent, so this serialises nothing that matters, and it means `advance` never sees a
half-applied sibling decision. Parallel fan-out happens *inside* one `advance` call
returning several `DispatchStep`s, not by several advancers racing.

### Expressions

`condition` and `input_mapping` need an expression language, and the choice is a
security decision. **Not Python `eval`, not a sandbox, not a template engine with
arbitrary attribute access.** A small evaluator over a fixed grammar:

- References: `$.input.sector`, `$.steps.research.output.competitors[0].name`
- Literals, comparison, `and`/`or`/`not`, `in`, `exists`, `length`
- No function calls beyond a documented list, no loops, no I/O, no recursion limit to blow

It lives in `domain/expressions/`, is pure, is fuzzed, and cannot reach anything the
orchestrator did not put in scope. An expression language that can call a function is a
sandbox, and a sandbox in a multi-tenant product is a vulnerability with a release
schedule.

## 5. Executing a managed agent turn

This is the component that did not exist in any form before, and the one place AgentWorld
calls an LLM.

```
worker: step_runner
  claim a 'managed_agent_turn' dispatch
  load: agent definition, model profile, granted tools (from connections),
        granted knowledge, the step input
  emit run.started
  loop (bounded by max_turns and a wall clock):
     response = provider.complete(messages, tool_definitions, config)
     emit run.step_finished (kind='llm', token counts)
     if response has tool calls:
        for each call:
           if tool not granted               -> emit tool.denied; feed the refusal back
           if needs approval and none granted -> RequestApproval; park the dispatch; return
           result = tool_executor.invoke(tool, args, credential_id)
           emit tool.invoked / tool.returned | tool.failed   (args and result redacted)
           append result to messages
        continue
     else:
        emit run.finished, workflow.step_succeeded with the final output
        return
```

Points worth stating:

- **`provider.complete` is the only provider surface.** One Protocol in
  `infra/providers/base.py`; `anthropic.py` and `openai_compatible.py` implement it. Adding
  a provider adds a file and a registry entry, and touches nothing else.
- **Tool grants are checked at call time, not at prompt-build time.** A grant revoked mid-run
  takes effect on the next call rather than at the next restart.
- **An approval mid-turn parks the dispatch.** The run is not held open across a human's
  lunch break: the partial message history is persisted on the execution step, the dispatch
  returns to `pending` with `run_after` unset, and granting the approval re-dispatches with
  the history restored. A held-open HTTP call or a sleeping worker would be the obvious
  implementation and would not survive a deploy.
- **Token counts and durations go to `metric_samples`** through the existing metric events,
  so cost analytics later needs no new plumbing — only a UI.

## 6. Executing against a dispatchable agent

Two shapes, because customers' networks differ:

**Push** (`agent_push`) — AgentWorld POSTs the work to a URL the agent registered, signed
with `X-AgentWorld-Signature` exactly as outbound webhooks are. The agent replies `202` and
later reports via ingest. A `2xx` means *accepted*, never *done*: the step stays
`dispatched` until a `workflow.step_succeeded` arrives or the step's timeout expires.

**Poll** (`agent_task_offer`) — AgentWorld materialises a `task` and the agent claims it
with the existing `POST /v1/self/tasks/claim` and its `FOR UPDATE SKIP LOCKED`. Nothing
inbound is required, which is what makes it work from inside a firewall. This is the path
that preserves the whole previously-designed task pipeline.

Both end the same way: the agent emits `workflow.step_succeeded` (or `failed`) carrying
the step output, the reducer folds it, and `advance` runs again. An agent that never
reports is handled by the step timeout, not by a hung dispatch.

**What is deliberately not offered:** controlling an `observed` agent. No dispatch kind can
target one, `advance` refuses it, and the builder will not propose it. The brief's *"Do not
pretend arbitrary external agents can be controlled"* is enforced by the absence of a code
path rather than by a disclaimer.

## 7. Approvals as a real gate

```
step kind = approval, or a tool/connection requiring it
   -> advance returns RequestApproval
   -> one transaction: approvals row (pending) + approval.requested event
                     + execution.status = 'waiting_approval'
   -> NO dispatch row exists. There is nothing to execute and no worker will find one.

human decides (control plane, role-gated, audited)
   -> granted: approval.granted event + advance() again -> now a dispatch appears
   -> denied:  approval.denied  event + step failed, on_failure policy applies
   -> expired: a worker writes approval.expired; same as denied
```

The gate is the *absence of a dispatch*, not a flag a worker is trusted to check. A bug in
the approval UI cannot cause an unapproved send, because the thing that sends has no row
to act on.

`required_role` on the approval means a viewer can be shown a pending approval and not be
able to decide it. Decisions are `audit_log` rows as well as events, because "who approved
the 412-recipient email" is an accountability question and `events` is partitioned with a
90-day retention.

## 8. Failure, retry, timeout, cancel

| Situation | Mechanism |
|-----------|-----------|
| Dispatch worker crashes | lease expires, reaper returns the row to `pending` |
| Provider 429 / 5xx | dispatch retried with exponential backoff and jitter, `attempt+1`, a new unique key |
| Step exceeds `max_attempts` | `workflow.step_failed`; `on_failure` decides fail / continue / route |
| Step exceeds `timeout_seconds` | timeout worker writes `step_failed`; a late report is dropped as a duplicate |
| Dispatchable agent never reports | the same step timeout — no special case |
| User cancels | execution → `cancelled`, pending dispatches → `cancelled`; in-flight work is *asked* to stop and the step is marked cancelled when it reports or times out. Honest: AgentWorld cannot un-send a request already made |
| Whole workflow is wrong | the user edits, which creates a new version; running executions finish on the old one |

The cancel row is the one place the product has to be careful what it claims. The UI says
"cancelling" until the step actually reports, because a `POST` already in flight to Slack
cannot be recalled.

## 9. Workers

| Worker | Job | New? |
|--------|-----|------|
| `step_runner` | managed agent turns, tool calls | **new** |
| `dispatch_sender` | `agent_push`, `webhook_emit` | **new** |
| `dispatch_reaper` | expired leases, retry backoff, dead-lettering | **new** |
| `scheduler` | fires `schedule` triggers off `triggers.next_fire_at` | **new** |
| `timeout_sweeper` | step and approval expiry | **new** |
| `heartbeat_sweeper` | agents past TTL → `offline` | as designed |
| `partition_maintainer`, `retention`, `rollups` | as designed | as designed |

All of them are the same shape: claim a lease, do one thing, report an event, release.
Horizontally scalable because the claim is `SKIP LOCKED` and the work is keyed. No leader
election, no scheduler singleton — the `scheduler` claims a trigger row the same way.

## 10. What this is not

- **Not a workflow language.** No loops, no map-over-collection, no sub-workflows in v1.
  Each is a `Decision` variant and a `step_kind` away, behind an unchanged `advance`
  signature, once something real runs on it.
- **Not distributed.** One PostgreSQL, `SKIP LOCKED`, lease-based workers. Nothing here
  needs Temporal, Airflow or a broker, and each would be a second system to operate before
  the first user exists.
- **Not a general-purpose scheduler.** Cron expressions and a `next_fire_at` column. No
  backfill, no catch-up semantics, no calendars.
- **Not exactly-once.** At-least-once delivery with idempotency keys, which is what is
  actually achievable. Tools declare whether they are idempotent; consequential ones are
  gated by approval, which is the real protection against a double send.
