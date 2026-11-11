"""Connections, workflow drafts, and the endpoint the product leans on.

`POST /v1/workflows/{id}/validate` returns structured findings rather than a
boolean, because "no" is useless to the person who has to fix it. Each
finding carries a pointer the UI highlights, a detail written for a beginner,
and a remedy. It is also what the conversational builder calls before
proposing anything, so the builder cannot produce an unrunnable ecosystem and
claim success.

Publishing is not here. A published version is frozen so that something can
run it, and nothing can until the execution core lands - a `POST /publish`
that normalised a draft into `workflow_steps` and then had no runner would be
an endpoint whose only effect was to make the draft uneditable.
"""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Any
from uuid import UUID

from fastapi import APIRouter, status
from pydantic import BaseModel, ConfigDict, Field

from agentworld.api.deps import (
    CallContextDep,
    ConnectionServiceDep,
    CurrentUser,
    WorkflowServiceDep,
)
from agentworld.domain.entities import Connection, Workflow, WorkflowDraft, WorkflowVersion
from agentworld.domain.enums import ConnectionKind
from agentworld.domain.orchestration import Code, Severity, ValidationReport
from agentworld.repositories.definitions import UNSET

connections_router = APIRouter(prefix="/v1/projects/{project_id}/connections", tags=["connections"])
workflows_router = APIRouter(prefix="/v1/projects/{project_id}/workflows", tags=["workflows"])

Name = Annotated[str, Field(min_length=1, max_length=200)]


# ------------------------------------------------------------- connections ---


class ConnectionView(BaseModel):
    id: UUID
    kind: ConnectionKind
    source_kind: str
    source_id: UUID
    target_kind: str
    target_id: UUID
    config: dict[str, Any] = Field(default_factory=dict)
    requires_approval: bool = False
    enabled: bool = True

    @classmethod
    def of(cls, connection: Connection) -> ConnectionView:
        return cls(
            id=connection.id,
            kind=connection.kind,
            source_kind=connection.source_kind,
            source_id=connection.source_id,
            target_kind=connection.target_kind,
            target_id=connection.target_id,
            config=dict(connection.config),
            requires_approval=connection.requires_approval,
            enabled=connection.enabled,
        )


class ConnectionCreate(BaseModel):
    kind: ConnectionKind
    source_kind: str = Field(max_length=64, examples=["agent"])
    source_id: UUID
    target_kind: str = Field(max_length=64, examples=["tool"])
    target_id: UUID
    config: dict[str, Any] = Field(
        default_factory=dict,
        description="For a `grant_tool` connection: `tool_name` and `side_effect`. "
        "A `consequential` side effect makes creating this connection an admin "
        "action, because granting a capability is at least as privileged as using it.",
        examples=[{"tool_name": "send_slack_message", "side_effect": "consequential"}],
    )
    requires_approval: bool = False
    enabled: bool = True


class ConnectionUpdate(BaseModel):
    """What a grant permits, not where it points.

    `extra="forbid"`: `kind`, `source_*` and `target_*` are the connection's
    identity, and a client that sends one is asking for a different
    connection rather than an edit - being told so beats having it ignored.

    Absent means leave it alone; `config` sent at all replaces the mapping
    whole, the way the draft editor replaces a graph, because a grant's
    config is one decision and merging two halves of it would produce a
    permission nobody chose.
    """

    model_config = ConfigDict(extra="forbid")

    config: dict[str, Any] | None = Field(
        default=None,
        description=(
            "Replaced whole. A `consequential` side effect makes this an admin action - "
            "and so does editing a grant that is already consequential."
        ),
    )
    requires_approval: bool | None = None
    enabled: bool | None = Field(
        default=None,
        description="A disabled grant stops counting on the next dispatch, not on restart.",
    )

    def sent(self) -> dict[str, Any]:
        """Only the fields the caller actually included."""
        return {
            name: (UNSET if name not in self.model_fields_set else value)
            for name, value in self.model_dump().items()
        }


@connections_router.get("", response_model=list[ConnectionView], summary="Connections in a project")
async def list_connections(
    project_id: UUID, principal: CurrentUser, service: ConnectionServiceDep
) -> list[ConnectionView]:
    return [ConnectionView.of(c) for c in await service.list_in(principal, project_id)]


@connections_router.post(
    "",
    response_model=ConnectionView,
    status_code=status.HTTP_201_CREATED,
    summary="Grant a capability",
)
async def create_connection(
    project_id: UUID,
    body: ConnectionCreate,
    principal: CurrentUser,
    context: CallContextDep,
    service: ConnectionServiceDep,
) -> ConnectionView:
    """`member`, unless it grants a tool whose effects cannot be taken back."""
    connection = await service.create(
        principal,
        context,
        project_id,
        kind=body.kind,
        source_kind=body.source_kind,
        source_id=body.source_id,
        target_kind=body.target_kind,
        target_id=body.target_id,
        config=body.config,
        requires_approval=body.requires_approval,
        enabled=body.enabled,
    )
    return ConnectionView.of(connection)


@connections_router.patch(
    "/{connection_id}",
    response_model=ConnectionView,
    summary="Change a grant",
)
async def update_connection(
    project_id: UUID,
    connection_id: UUID,
    body: ConnectionUpdate,
    principal: CurrentUser,
    context: CallContextDep,
    service: ConnectionServiceDep,
) -> ConnectionView:
    """`member`, unless either side of the edit grants a consequential tool.

    Both sides: a grant that is already consequential needs an `admin` to
    edit, whatever the new config says. Where it points cannot change -
    repointing a grant is a delete and a create.
    """
    connection = await service.update(principal, context, project_id, connection_id, **body.sent())
    return ConnectionView.of(connection)


@connections_router.delete(
    "/{connection_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Revoke a connection",
)
async def delete_connection(
    project_id: UUID,
    connection_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: ConnectionServiceDep,
) -> None:
    await service.delete(principal, context, project_id, connection_id)


# --------------------------------------------------------------- workflows ---


class WorkflowView(BaseModel):
    """What a workflow is, including what kind of workflow it is.

    `is_test_harness` and `test_for_agent_id` are the same fact twice on
    purpose: the boolean is what a client switches on, and the id is the
    link back to the agent whose test this runs. Without them a harness is
    indistinguishable from something a person drew until the caller provokes
    one of the four 409s, and an execution of one - which carries only a
    `workflow_id` - cannot be labelled at all.

    No `project_id` or `workspace_id`: both are in the path that returned
    this, and the id of a tenant a caller is already inside is not news.
    """

    id: UUID
    slug: str
    name: str
    description: str | None = None
    current_version_id: UUID | None = None
    enabled: bool = False
    is_test_harness: bool = False
    test_for_agent_id: UUID | None = None
    created_at: dt.datetime | None = None
    #: Set means archived: absent from the listing, refused by every
    #: lifecycle verb, and still readable here so a client can say so.
    archived_at: dt.datetime | None = None

    @classmethod
    def of(cls, workflow: Workflow) -> WorkflowView:
        return cls(
            id=workflow.id,
            slug=workflow.slug,
            name=workflow.name,
            description=workflow.description,
            current_version_id=workflow.current_version_id,
            enabled=workflow.enabled,
            is_test_harness=workflow.is_test_harness,
            test_for_agent_id=workflow.test_for_agent_id,
            created_at=workflow.created_at,
            archived_at=workflow.archived_at,
        )


class VersionView(BaseModel):
    """One published version, as history.

    No graph, deliberately: the editor reads the open draft through `GET
    …/draft`, a running execution is bound to its own frozen version, and
    nothing a *history* answers needs every graph a workflow has ever had.

    `is_current` is the field this list exists for. It marks the one version
    `workflows.current_version_id` names, which is the version `POST /run`
    would execute - the question "which of these is live" has no other
    answer a client could compute from the rest of the row.
    """

    id: UUID
    version: int
    published_at: dt.datetime
    created_at: dt.datetime | None = None
    is_current: bool = False

    @classmethod
    def of(cls, version: WorkflowVersion, *, current_version_id: UUID | None) -> VersionView:
        return cls(
            id=version.id,
            version=version.version,
            published_at=version.published_at,
            created_at=version.created_at,
            is_current=version.id == current_version_id,
        )


class DraftView(BaseModel):
    id: UUID
    workflow_id: UUID
    version: int
    graph: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def of(cls, draft: WorkflowDraft) -> DraftView:
        return cls(
            id=draft.id,
            workflow_id=draft.workflow_id,
            version=draft.version,
            graph=dict(draft.graph),
        )


class WorkflowCreated(BaseModel):
    workflow: WorkflowView
    draft: DraftView


class WorkflowCreate(BaseModel):
    name: Name
    slug: str | None = None
    description: str | None = None
    graph: dict[str, Any] | None = Field(
        default=None,
        description="The starting graph. Empty when omitted.",
        examples=[
            {
                "steps": [
                    {"step_key": "research", "kind": "agent_task", "agent_key": "researcher"}
                ],
                "edges": [],
            }
        ],
    )


class WorkflowUpdate(BaseModel):
    """What a workflow is called, and nothing about what it does.

    `extra="forbid"`, so `slug` and `graph` are refused by name rather than
    ignored: the slug is identity, like `agent_key`, and the graph is `PUT
    …/draft`'s - a client that sent one here would otherwise believe it had
    changed what the workflow runs.

    Absent means leave it alone; an explicit `null` clears the description.
    """

    model_config = ConfigDict(extra="forbid")

    name: Name | None = None
    description: str | None = None

    def sent(self) -> dict[str, Any]:
        """Only the fields the caller actually included.

        `exclude_unset` is what makes "clear the description" and "leave the
        description" different requests rather than the same one, exactly as
        in `AgentUpdate`.
        """
        return {
            name: (UNSET if name not in self.model_fields_set else value)
            for name, value in self.model_dump().items()
        }


class DraftSave(BaseModel):
    graph: dict[str, Any]


class FindingView(BaseModel):
    severity: Severity
    code: Code
    pointer: str = Field(description="A JSON pointer the editor can highlight.")
    detail: str
    remedy: str = ""


class ValidationView(BaseModel):
    publishable: bool = Field(
        description="False when any finding is an error. Warnings do not block."
    )
    findings: list[FindingView]

    @classmethod
    def of(cls, report: ValidationReport) -> ValidationView:
        return cls(
            publishable=report.publishable,
            findings=[
                FindingView(
                    severity=f.severity,
                    code=f.code,
                    pointer=f.pointer,
                    detail=f.detail,
                    remedy=f.remedy,
                )
                for f in report.findings
            ],
        )


@workflows_router.get("", response_model=list[WorkflowView], summary="Workflows in a project")
async def list_workflows(
    project_id: UUID, principal: CurrentUser, service: WorkflowServiceDep
) -> list[WorkflowView]:
    return [WorkflowView.of(w) for w in await service.list_in(principal, project_id)]


@workflows_router.post(
    "",
    response_model=WorkflowCreated,
    status_code=status.HTTP_201_CREATED,
    summary="Create a workflow and its first draft",
)
async def create_workflow(
    project_id: UUID,
    body: WorkflowCreate,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkflowServiceDep,
) -> WorkflowCreated:
    """The draft comes with it, so the editor never meets a workflow with no
    version to open."""
    workflow, draft = await service.create(
        principal,
        context,
        project_id,
        name=body.name,
        slug=body.slug,
        description=body.description,
        graph=body.graph,
    )
    return WorkflowCreated(workflow=WorkflowView.of(workflow), draft=DraftView.of(draft))


@workflows_router.patch(
    "/{workflow_id}",
    response_model=WorkflowView,
    summary="Rename or re-describe a workflow",
    description=(
        "Metadata only. The graph is `PUT .../draft`, which is a separate permission; "
        "this cannot change a published version or what a running execution means. An "
        "agent's test harness is refused with a 409."
    ),
)
async def update_workflow(
    project_id: UUID,
    workflow_id: UUID,
    body: WorkflowUpdate,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkflowServiceDep,
) -> WorkflowView:
    """`member`: `workflow.update`, which is not `workflow.update_draft`.

    Renaming a workflow changes what it is called and nothing about what it
    would do, so it is not the permission that edits a graph - even though
    both are `member` today.
    """
    workflow = await service.update(principal, context, project_id, workflow_id, **body.sent())
    return WorkflowView.of(workflow)


@workflows_router.get("/{workflow_id}", response_model=WorkflowView, summary="One workflow")
async def get_workflow(
    project_id: UUID,
    workflow_id: UUID,
    principal: CurrentUser,
    service: WorkflowServiceDep,
) -> WorkflowView:
    """The workflow itself, which the collection listing could not answer for.

    Readable for an archived workflow and for an agent's test harness, both
    of which are absent from `GET /workflows` - the first because it is over
    and the second because nobody drew it. An execution carries a
    `workflow_id` and nothing else, so without this route a client could not
    name the workflow its own run belongs to.
    """
    return WorkflowView.of(await service.get(principal, project_id, workflow_id))


@workflows_router.get(
    "/{workflow_id}/versions",
    response_model=list[VersionView],
    summary="What this workflow has published",
)
async def list_versions(
    project_id: UUID,
    workflow_id: UUID,
    principal: CurrentUser,
    service: WorkflowServiceDep,
) -> list[VersionView]:
    """Published versions, newest first, with the live one marked.

    Drafts are not versions here: a workflow always has one open draft after
    its first publish, and a history that listed it would be reporting
    something still being edited as something that was frozen.
    """
    workflow, versions = await service.versions(principal, project_id, workflow_id)
    return [
        VersionView.of(version, current_version_id=workflow.current_version_id)
        for version in versions
    ]


@workflows_router.get(
    "/{workflow_id}/draft", response_model=DraftView, summary="The draft being edited"
)
async def get_draft(
    project_id: UUID,
    workflow_id: UUID,
    principal: CurrentUser,
    service: WorkflowServiceDep,
) -> DraftView:
    _, draft = await service.get_draft(principal, project_id, workflow_id)
    return DraftView.of(draft)


@workflows_router.put("/{workflow_id}/draft", response_model=DraftView, summary="Save the draft")
async def save_draft(
    project_id: UUID,
    workflow_id: UUID,
    body: DraftSave,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkflowServiceDep,
) -> DraftView:
    """A whole-graph PUT rather than a patch.

    A draft changes as a whole - steps move, edges appear and vanish together
    - and a patch language for a graph would be a second thing to get right
    for no benefit while nothing reads its parts.
    """
    draft = await service.save_draft(principal, context, project_id, workflow_id, graph=body.graph)
    return DraftView.of(draft)


@workflows_router.post(
    "/{workflow_id}/validate",
    response_model=ValidationView,
    summary="Could this draft actually run?",
)
async def validate_workflow(
    project_id: UUID,
    workflow_id: UUID,
    principal: CurrentUser,
    service: WorkflowServiceDep,
) -> ValidationView:
    """Structured findings, not a boolean.

    This is where "a visual line is not proof two components can
    communicate" is enforced: a graph that looks connected on the island and
    cannot run says so, with a pointer the editor highlights.
    """
    return ValidationView.of(await service.validate(principal, project_id, workflow_id))


@workflows_router.delete(
    "/{workflow_id}", status_code=status.HTTP_204_NO_CONTENT, summary="Archive a workflow"
)
async def archive_workflow(
    project_id: UUID,
    workflow_id: UUID,
    principal: CurrentUser,
    context: CallContextDep,
    service: WorkflowServiceDep,
) -> None:
    await service.archive(principal, context, project_id, workflow_id)
