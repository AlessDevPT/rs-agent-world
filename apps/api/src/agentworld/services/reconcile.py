"""Finishing advances that nobody finished.

## The gap this closes

Settling a dispatch and advancing its execution are two transactions, and
they have to be: `advance_once` takes the execution's row lock and decides
from **committed** state, so the step's outcome must be folded and committed
before it can see the step as finished. First transaction: the dispatch is
settled (or resolved), the outcome event is appended and folded, the audit
row is written. Second transaction: `advance_once` decides what happens next.

A process that dies between them leaves an execution that is correct,
consistent and **stalled**. Nothing was half-applied and nothing is wrong -
there is simply nobody left to decide the next step. Before T6c nothing
retried the second transaction, so that execution stopped for ever.

## How it finds them, and why there is no marker

The condition is derived, not recorded: a `running` execution with no
dispatch in `pending`, `leased` or `needs_review` is either owed an advance
or already finished.

The alternative was a column set in the first transaction and cleared by the
advance. That was rejected because it adds an invariant every present and
future writer of a dispatch outcome has to maintain, and a writer that
forgets reintroduces exactly this gap - silently, because a missing marker
looks identical to "nothing is owed". A derived condition has nothing to
forget. It costs a partial index instead
(`dispatches_unsettled_by_execution_idx`), and an index can only be slow,
which is measurable.

## Why calling it when nothing is owed is harmless

`advance()` returns no decisions for an execution that needs nothing, and
`advance_once` then writes nothing at all - no event, no dispatch, no status
change. So a false positive costs one row lock. That is what makes this safe
to run on a timer rather than needing to be exactly right.

## Multi-instance behaviour

Unlike the reaper's `FOR UPDATE SKIP LOCKED` claim, `advance_once` locks with
a plain `SELECT ... FOR UPDATE`, and T6c does not change that contract. Two
reconcilers that pick the same execution therefore **serialise**: the second
waits for the first to commit, reads committed state, finds nothing to decide
and writes nothing. Correct, idempotent, and blocking for as long as one
advance takes.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from uuid import UUID

from agentworld.domain.errors import NotFoundError
from agentworld.infra.logging import get_logger
from agentworld.repositories.base import TenantScope
from agentworld.repositories.unit_of_work import UnitOfWorkFactory
from agentworld.services.executions import ExecutionService

log = get_logger(__name__)


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """What one pass did, in numbers an operator can watch.

    `candidates` counts rows the query offered and `advanced` counts the ones
    where `advance_once` actually decided something. The difference is how
    often the derived condition was a false positive, and it is the number
    worth watching: a persistently large gap means the grace period is too
    short or the condition too loose.

    `failures` counts candidates whose advance raised. They are not lost -
    whatever made them candidates is still true, so the next pass offers them
    again.

    `workspaces_failed` is separate from `failures` because one unreachable
    workspace must not be reported as healthy work.
    """

    workspaces: int
    candidates: int
    advanced: int
    failures: int
    workspaces_failed: int


class AdvanceReconciler:
    """Sweeps every workspace for executions owed an advance.

    Modelled on `LeaseReaper`: all workspaces every pass, a bounded number of
    candidates within each, and no cursor. The dispatch worker's rotating
    cursor exists because `run_once` returns after claiming **one** dispatch,
    so a permanently busy workspace could starve the ones after it in the
    list; a sweep that visits every workspace cannot starve anything.

    So the per-workspace limit bounds the work *within* a workspace, not the
    work across a pass: total sweep duration grows with workspace count. That
    is a deliberate trade and the thing to measure before anyone adds a
    cursor - `18-T6C-ADVANCEMENT-RECOVERY.md` records what to watch.
    """

    def __init__(
        self,
        factory: UnitOfWorkFactory,
        *,
        executions: ExecutionService,
        grace_seconds: int,
    ) -> None:
        self._factory = factory
        self._executions = executions
        self._grace_seconds = grace_seconds

    async def sweep(self, *, limit: int) -> ReconcileResult:
        """One pass. Never raises for one workspace's or one candidate's sake.

        A failure is counted and logged and the pass continues: a workspace
        whose connection drops must not stop the others, and a candidate
        whose advance raises must not stop its neighbours. Nothing is lost by
        continuing, because candidacy is derived rather than consumed -
        whatever made a row a candidate is still true on the next pass.

        The workspace list is read once, in its own transaction, the way the
        reaper reads it. The staleness cut-off is computed once for the whole
        pass rather than per workspace, so every workspace in one pass is
        judged against the same clock.
        """
        async with self._factory.begin() as work:
            workspace_ids = list(await work.workspaces.live_ids())

        stale_before = dt.datetime.now(dt.UTC) - dt.timedelta(seconds=self._grace_seconds)
        candidates = advanced = failures = workspaces_failed = 0

        for workspace_id in workspace_ids:
            try:
                async with self._factory.begin(visible_workspaces={workspace_id}) as work:
                    found = await work.executions(TenantScope(workspace_id)).needing_advance(
                        stale_before=stale_before, limit=limit
                    )
            except Exception:
                log.exception("reconcile_query_failed", workspace_id=str(workspace_id))
                workspaces_failed += 1
                continue

            candidates += len(found)
            for execution_id, project_id in found:
                did, failed = await self._advance(workspace_id, project_id, execution_id)
                advanced += did
                failures += failed

        result = ReconcileResult(
            workspaces=len(workspace_ids),
            candidates=candidates,
            advanced=advanced,
            failures=failures,
            workspaces_failed=workspaces_failed,
        )
        if candidates or workspaces_failed:
            log.info(
                "reconcile_swept",
                workspaces=result.workspaces,
                candidates=result.candidates,
                advanced=result.advanced,
                failures=result.failures,
                workspaces_failed=result.workspaces_failed,
            )
        return result

    async def _advance(
        self, workspace_id: UUID, project_id: UUID, execution_id: UUID
    ) -> tuple[int, int]:
        """`(advanced, failed)` for one candidate, 0 or 1 each.

        Through `ExecutionService.advance_once` and nothing else. There is one
        path that decides what an execution does next; a recovery mechanism
        with its own would be deciding differently from the worker by
        definition, which is the whole thing T6 was careful about.
        """
        try:
            applied = await self._executions.advance_once(
                project_id, execution_id, visible={workspace_id}
            )
        except NotFoundError:
            # Gone between the query and the lock. Not a failure: there is no
            # execution left to owe anything.
            log.info("reconcile_execution_gone", execution_id=str(execution_id))
            return (0, 0)
        except Exception:
            log.exception(
                "reconcile_advance_failed",
                execution_id=str(execution_id),
                project_id=str(project_id),
            )
            return (0, 1)

        if applied.made_progress:
            log.info(
                "reconcile_advanced",
                execution_id=str(execution_id),
                decisions=len(applied.decisions),
                dispatches=len(applied.dispatches),
            )
            return (1, 0)
        return (0, 0)
