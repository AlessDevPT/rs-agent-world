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

CREATE TYPE dispatch_status AS ENUM (
  'pending',       -- claimable
  'leased',        -- a worker holds it, until leased_until
  'succeeded',
  'failed',        -- this attempt failed; retryable, back to pending
  'dead',          -- out of attempts, or a terminal error. Never retried automatically
  'needs_review',  -- ambiguous: the side effect may or may not have happened
  'cancelled'
);

-- Copied from the step or tool at dispatch time, not looked up at retry
-- time: the retry policy must not change because somebody edited a tool
-- while work was in flight.
CREATE TYPE side_effect AS ENUM ('read', 'write', 'consequential');

CREATE TABLE dispatches (
  id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  workspace_id  uuid NOT NULL REFERENCES workspaces(id) ON DELETE CASCADE,
  project_id    uuid NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
  execution_id  uuid REFERENCES workflow_executions(id) ON DELETE CASCADE,
  execution_step_id uuid REFERENCES execution_steps(id) ON DELETE CASCADE,
  kind          dispatch_kind NOT NULL,
  status        dispatch_status NOT NULL DEFAULT 'pending',
  side_effect_class side_effect NOT NULL,

  -- The whole instruction, resolved: the step's input after mapping, the
  -- agent or tool to use, credential *references* and never credentials.
  instruction   jsonb NOT NULL,

  -- Forwarded to the external side as X-AgentWorld-Idempotency-Key, so a
  -- receiver that honours it can dedupe a retry. Stable across attempts of
  -- the same work, which is the whole point.
  idempotency_token uuid NOT NULL DEFAULT gen_random_uuid(),

  attempt       int NOT NULL DEFAULT 0,
  max_attempts  int NOT NULL DEFAULT 3,

  -- A lease, not a lock: a worker that dies holds nothing.
  leased_by     text,
  leased_until  timestamptz,
  -- Set before the outbound call and cleared after it returns. If a lease
  -- expires with this set, the side effect was in flight when the worker
  -- died and nobody knows whether it landed.
  side_effect_started_at timestamptz,

  run_after     timestamptz NOT NULL DEFAULT now(),
  last_error    text,
  last_error_code text,
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
CREATE INDEX dispatches_attention_idx ON dispatches (project_id, status)
  WHERE status IN ('dead', 'needs_review');
-- Added in T6c, for the advancement reconciler's one question per candidate
-- execution: "is any dispatch of yours still unsettled?". `needs_review` is
-- among the three on purpose, so an execution waiting on a person is never a
-- candidate for automatic advancement.
CREATE INDEX dispatches_unsettled_by_execution_idx ON dispatches (execution_id, status)
  WHERE execution_id IS NOT NULL
    AND status IN ('pending', 'leased', 'needs_review');
```

`dispatches` is **not** append-only and **not** under the event log's retention: rows are
updated as they progress and pruned after completion. It is operational state; the history
is in `events`.

### State machine

```
                    ┌──────────────── retryable error, attempts remain ────┐
                    ▼                                                      │
  (advance) ──► pending ──claim──► leased ──┬── success ──► succeeded       │
                   ▲                        │                              │
                   └── lease expired, ──────┤                              │
                       side effect not      ├── retryable error ──► failed ┘
                       in flight            │
                                            ├── terminal error ──► dead
                                            ├── attempts exhausted ► dead
                                            └── lease expired WITH a
                                                consequential side effect
                                                in flight ──► needs_review
  pending | leased ──user or execution cancel──► cancelled
```

Every terminal state resolves its step. A `dead`, `needs_review` or `cancelled` dispatch
writes `workflow.step_failed` (or `step_cancelled`) so the execution advances. An
execution can never sit in `running` with no live dispatch and no terminal step — see the
no-stuck-executions invariant in §8.

### Idempotent dispatch

Three layers, because each catches a different duplicate:

1. **`UNIQUE (execution_step_id, kind, attempt)`** — the orchestrator cannot create two
   dispatches for one step attempt. Two concurrent `advance` calls produce a constraint
   violation on the second, not two Slack messages. This is the layer that makes
   `advance` safe to call liberally.
2. **The lease** — only one worker holds a row at a time, and `FOR UPDATE SKIP LOCKED`
   means a second worker skips it rather than waiting and then repeating it.
3. **`idempotency_token`** — stable across attempts, forwarded outbound as
   `X-AgentWorld-Idempotency-Key`. A dispatchable agent, a webhook receiver or a provider
   that honours it collapses a retry into one effect. Documented in the SDK so the
   obvious implementation is the correct one.

Layer 3 is the only one that reaches outside AgentWorld, and it only works where the
receiver cooperates. Which is why §8 is explicit about what happens where it does not.

### Claiming

```sql
UPDATE dispatches SET status='leased', leased_by=:worker,
       leased_until=now() + make_interval(secs => :lease_seconds),
       claim_count=claim_count+1
WHERE id IN (
  SELECT id FROM dispatches
  WHERE status='pending' AND run_after <= now()
  ORDER BY run_after, id
  FOR UPDATE SKIP LOCKED
  LIMIT :batch
)
RETURNING *;
```

`lease_seconds` is per kind: a tool call leases for 60s, a managed agent turn for 600s. A
worker that needs longer extends its own lease rather than taking a generous one up front,
so a crash is noticed in seconds rather than minutes.

> **Corrected in T0 (D4).** This statement used to increment `attempt` rather than
> `claim_count`, and that could not coexist with the other two things said about `attempt`
> in this document. Layer 1 of idempotent dispatch above relies on
> `UNIQUE (execution_step_id, kind, attempt)` to make a double `advance` collide — which
> requires the key to be *stable*. If claiming moves `attempt`, the key moves out from
> under the row it is meant to pin, so it stops guarding creation at all. And a row claimed
> once, incremented to `attempt = 1`, then collides with the retry row §8 says a human
> creates at `attempt + 1` — the retry failing for a reason unrelated to the retry.
>
> So `attempt` is now the **identity of an attempt**: written once at insert, and a
> `BEFORE UPDATE` trigger in migration 0019 refuses to change it. Lease bookkeeping lives
> in `claim_count`, which a worker may increment freely because nothing depends on it.
> `execution_step_id` became `NOT NULL` in the same migration, because NULLs are distinct
> in a unique constraint and a step-less row therefore had no duplicate protection
> whatsoever (D5).

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
   -> advance returns RequestApproval, carrying the requirement resolved by
      domain/approvals/policy.py from the project policy, the step config,
      the tool and the connection -- strictest of the four
   -> one transaction: approvals row (pending, with required_role,
                       requires_different_user and attributed_to_user_id
                       FROZEN at this moment)
                     + approval.requested event
                     + execution.status = 'waiting_approval'
   -> NO dispatch row exists. There is nothing to execute and no worker will find one.

human decides (control plane, role-gated, different-user-gated, audited)
   -> granted: approval.granted event + advance() again -> now a dispatch appears
   -> denied:  approval.denied  event + step failed, on_failure policy applies
   -> expired: a worker writes approval.expired; same as denied
```

The gate is the *absence of a dispatch*, not a flag a worker is trusted to check. A bug in
the approval UI cannot cause an unapproved send, because the thing that sends has no row to
act on — and there is no `dispatch.create` action for anything else to use.

> **What that argument did not cover, closed in T0 (D1).** It defends against a buggy
> approval UI. It did not defend against a *forged* `approval.granted` event, because the
> forged event does not need to create a dispatch — the reducer folds it to
> `execution_steps.status = ready`, and `advance()` then creates the dispatch itself, doing
> exactly what it is supposed to do with a step that is ready. Until T0, `EventSpec`
> described what an event was about and which tables it folded into but said nothing about
> who may originate one, and ingest checked the key's scope, its project pin, the clock skew
> and the agent allowlist — never the type. So any holder of an ingest key could post
> `approval.granted`, or `workflow.step_succeeded` with any `output` that a later step then
> read through the expression language.
>
> `Producer` now answers "who may originate this type", and ingest enforces it. All four
> `approval.*` types are `CONTROL_PLANE`: never ingestible. Every `workflow.*` type is
> `ORCHESTRATOR`, except `step_succeeded` and `step_failed`, which §6 requires a
> `dispatchable` agent to send — those are `AGENT_FOR_DISPATCHED_STEP`, accepted only for a
> step that is *currently* dispatched or running and held by an agent the key may act as.
> Everything else keeps the old rule. So the gate now has two independent halves: the
> absence of a dispatch, and the fact that nothing outside the control plane can write the
> event that would cause one.

**The requirement is frozen when the approval is created**, not recomputed when it is
decided. Editing the project policy therefore cannot change what a pending approval
demands, in either direction: a tightened policy does not strand an approval mid-flight,
and a loosened one cannot be used to wave through something already waiting.

**Self-approval is policy, not an accident.** `requires_different_user` comes from
`approval_policies`, which defaults to `false` for `read`/`write` work and `true` for
anything `consequential`. The full resolution rules, the two hard floors no policy can go
under, and how scheduling is prevented from becoming a route around the gate are in
`01-DOMAIN-AND-PROTOCOL.md` §7. Three layers enforce it: the service, a check constraint
on `approvals`, and the absence of a dispatch.

`required_role` means a viewer can be shown a pending approval and be unable to decide it.
Decisions are `audit_log` rows as well as events, because "who approved the 412-recipient
email" is an accountability question and `events` is partitioned with a 90-day retention.

## 8. Reliability: failure, retry, dead letters, recovery

### Retry policy

Classification first, because retrying the wrong error is worse than not retrying:

| Error | Class | Behaviour |
|-------|-------|-----------|
| Connection refused, timeout, DNS failure | retryable | backoff and retry |
| HTTP 429, with or without `Retry-After` | retryable | honour `Retry-After` when present, else backoff |
| HTTP 500, 502, 503, 504 | retryable | backoff and retry |
| Provider overloaded / rate limited | retryable | backoff and retry |
| HTTP 400, 404, 422 — a malformed or impossible request | **terminal** | straight to `dead`. Retrying an invalid request produces the same invalid request |
| HTTP 401, 403 — bad or revoked credential | **terminal** | straight to `dead`, reason named. A retry cannot fix authorization |
| Credential revoked locally | **terminal** | `dead`, reason `credential_revoked` |
| Tool not granted | **terminal** | `dead`. The grant changed; the decision must be remade |
| Model refused or returned unusable output | retryable once, then terminal | one retry absorbs a transient refusal; two means the prompt is wrong |

Backoff: `ceiling = min(cap, base * 2^(claims - 1))` with **equal** jitter -
`uniform(ceiling / 2, ceiling)` - `base = 2s`, `cap = 15m` for in-execution work. Jitter is not decoration:
without it, a provider outage produces a synchronised retry storm from every parked
dispatch the moment it recovers. *Equal* rather than full (`uniform(0, ceiling)`) because
full jitter put a measured 4.95% of first retries inside 100 ms - a delay named "backoff"
that did not delay anything - while half the ceiling still leaves a 50% spread. The
exponent counts **claims**, not `attempt`: migration 0019 makes `attempt` immutable, so an
exponent reading it never moved. See `15-T6-WORKER.md` §9.

`max_attempts` defaults to 3 and is set per step. A `consequential` dispatch defaults to
**1**: retrying something that may already have happened is a decision for a human, not a
default.

### Dead letters

A dispatch reaches `dead` when attempts are exhausted or an error is terminal. What
happens then is the part that is usually missing:

1. `last_error` and `last_error_code` are recorded.
2. A `workflow.step_failed` event is emitted with the reason, so the reducer advances the
   step and `on_failure` applies. **A dead dispatch always resolves its step.**
3. The execution appears in the project's `attention` list, which is the same field the
   island's alert state and the nav badge read.
4. `POST /v1/executions/{id}/steps/{key}/retry` creates a *new* dispatch with
   `attempt + 1` and a **new** `idempotency_token`, because a human has decided this is
   new work rather than a repeat.

There is no separate dead-letter table. A `dead` row keeps its instruction, its error and
its lineage, and a separate table would lose the link to the step it belongs to.

### `needs_review`: ambiguous consequential work

The honest answer to "prevent duplicate external side effects where supported", and the
admission that it is not always supported.

A worker sets `side_effect_started_at` immediately before an outbound call and clears it
after the call returns, whatever the outcome. If a lease expires with that column set, the
request was in flight when the worker died, and **nobody knows whether it landed**:

| `side_effect_class` | Lease expired mid-flight | Why |
|---------------------|-------------------------|-----|
| `read` | auto-retry | repeating a read costs a little money and nothing else |
| `write` | auto-retry, forwarding the same `idempotency_token` | a cooperating receiver collapses it; a non-cooperating one gets a duplicate write, which is recoverable |
| `consequential` | **`needs_review`** — never auto-retried | the email may have been sent. Sending it twice is worse than sending it late, and only a person can tell which |

A `needs_review` dispatch surfaces in `attention` with the question stated plainly — *"We
asked Slack to post this and did not hear back. It may have posted. Retry, or mark this
step failed?"* — and offers exactly those two actions. No timer resolves it, because a
timer would be guessing.

This is the one place the product cannot be made automatic, so it is made visible instead.

### Crash recovery

| What died | What happens |
|-----------|--------------|
| A worker between claim and the outbound call | lease expires, `dispatch_reaper` returns the row to `pending`. No side effect occurred |
| A worker mid-outbound-call | lease expires; `read`/`write` retry, `consequential` → `needs_review` |
| A worker after the call, before recording the result | same as mid-call. The result is lost; the classification decides |
| The API process mid-transaction | the transaction rolls back. Event, state change and dispatch row were one transaction, so none of the three survives |
| PostgreSQL | nothing is lost that was committed. Leases expire on restart and work resumes |
| An execution whose worker fleet is gone entirely | every lease expires; on restart the reaper requeues everything. No execution needs manual repair |

The reaper also re-runs `advance` for any execution in `running` whose dispatches are all
terminal — the self-healing path for a decision lost to a crash between commit and the
next trigger.

### Timeouts and cancellation

| Situation | Mechanism |
|-----------|-----------|
| Step exceeds `timeout_seconds` | `timeout_sweeper` writes `step_failed`; a late report is dropped as a duplicate on `dedup_key` |
| Dispatchable agent never reports | the same step timeout — no special case |
| Approval expires | `approval.expired`, treated as a denial per `on_failure` |
| User cancels an execution | status `cancelled`; `pending` dispatches → `cancelled`; `leased` ones are asked to stop and the step is marked cancelled when it reports or its lease expires |
| Whole workflow is wrong | the user edits, creating a new version. Running executions finish on the old one |

Cancellation is where the UI must not overclaim: it says **"cancelling"** until the
in-flight step actually reports, because a `POST` already on the wire to Slack cannot be
recalled. The previous design made the same point about commands to third-party agents,
and it is the same honesty.

### Observability

| Signal | Why it is the one that matters | Alert |
|--------|-------------------------------|-------|
| oldest `pending` age | the only number that tells you work is not moving | > 60s |
| `leased` rows past `leased_until` | workers dying, or leases too short | any sustained |
| `dead` rate | a tool, credential or provider is broken | > 1% of dispatches |
| `needs_review` count | needs a human, and will not clear itself | any, immediately |
| decision latency (`advance` duration) | the orchestrator becoming the bottleneck | p95 > 250ms |
| `events` append rate vs dispatch completion rate | divergence means state is drifting from work | — |

### Acceptance criteria

Each is a test, and each names the failure it would catch.

**Idempotency**
- Two concurrent `advance` calls on one execution produce exactly one dispatch per ready
  step; the loser sees a constraint violation, not a second dispatch.
- Replaying a recorded execution's events with the dispatch workers running creates
  **zero** rows in `dispatches` — protocol promise 7.
- The same `idempotency_token` is sent on every attempt of one dispatch, and a new one on
  a human-initiated retry.

**Retry**
- A 429 followed by success completes the step once, with `attempt = 2` and one output.
- A 422 goes to `dead` on the first attempt without a second request being made.
- A 401 from a revoked credential goes to `dead` with reason `credential_revoked`.
- Backoff intervals are jittered: 100 simulated retries do not share a wake-up instant.

**Dead letters**
- Every `dead` dispatch has a corresponding `workflow.step_failed` event.
- **No-stuck-executions invariant:** after a randomised fault-injection run (killed
  workers, provider errors, expired leases, cancelled executions), no execution remains in
  `running`, `pending` or `waiting_*` with no live dispatch, no pending approval and no
  terminal step. Asserted by a query, not by inspection.
- A human retry of a dead step produces a new dispatch and leaves the dead row intact as
  evidence.

**Crash recovery**
- A worker killed between claim and call: the step completes after the lease expires, with
  one side effect.
- A worker killed mid-call on a `write`: the step completes, the retry carries the same
  token.
- A worker killed mid-call on a `consequential`: the dispatch is `needs_review`, is **not**
  retried by any worker or timer, and appears in `attention`.
- The API killed mid-transaction leaves no event, no state change and no dispatch.

**Purity**
- No module under `domain/reducers/` or `domain/orchestration/` imports `httpx`,
  `sqlalchemy`, `redis`, `agentworld.infra`, `agentworld.services` or
  `agentworld.repositories` — the import-linter contract, verified by planting a violation.
- `advance()` called twice on identical state returns identical decisions.

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
