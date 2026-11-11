"""The three read endpoints a management surface cannot do without.

Stage A of task 3's audit, and each endpoint exists because of something the
API could not answer before:

- `GET /workflows/{id}` - the collection listing excludes archived workflows
  and agents' test harnesses, so a caller holding an id (from an execution,
  which carries nothing else) could not find out what it was holding;
- `…/versions` - `current_version_id` says which version is live and nothing
  says what else was ever published;
- `…/executions` - the project-wide listing returned runs whose workflow a
  client had no route to resolve.

Reads only. Everything here publishes, runs and archives through the
existing routes, so a change to what those do shows up here first.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
from fastapi.testclient import TestClient
from test_agent_test_harness import Harness
from test_executions_api import Scenario

from helpers import Actor

pytestmark = pytest.mark.integration


@pytest.fixture
def scenario(client: TestClient, actor: Actor) -> Scenario:
    return Scenario(client, actor)


def read(scenario: Scenario, workflow_id: str, suffix: str = "") -> Any:
    return scenario.client.get(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}{suffix}",
        headers=scenario.actor.headers,
    )


def published(scenario: Scenario, name: str = "Flow") -> tuple[str, str]:
    """A workflow that has published one version. Returns both ids."""
    workflow_id = scenario.workflow(name=name)
    response = scenario.publish(workflow_id)
    assert response.status_code == 201, response.text
    return workflow_id, str(response.json()["version_id"])


# ------------------------------------------------------------- one workflow ---


def test_a_workflow_is_read_back_by_id(scenario: Scenario) -> None:
    """Identity, timestamps and the two fields that say what kind it is."""
    workflow_id = scenario.workflow(name="Competitor brief")

    response = read(scenario, workflow_id)

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["id"] == workflow_id
    assert body["name"] == "Competitor brief"
    assert body["slug"] == "competitor-brief"
    assert body["created_at"] is not None
    assert body["archived_at"] is None
    assert body["current_version_id"] is None, "nothing has been published"
    assert body["is_test_harness"] is False
    assert body["test_for_agent_id"] is None
    # Unchanged by this stage, and asserted so that changing it is a
    # decision rather than a side effect: nothing writes the column.
    assert body["enabled"] is False


def test_the_pointer_appears_once_a_version_is_published(scenario: Scenario) -> None:
    """`current_version_id` is the version a run would use, and says so here."""
    workflow_id, version_id = published(scenario)

    body = read(scenario, workflow_id).json()

    assert body["current_version_id"] == version_id


def test_an_unknown_workflow_is_a_404(scenario: Scenario) -> None:
    assert read(scenario, str(uuid.uuid4())).status_code == 404


def test_an_archived_workflow_is_readable_and_says_it_is_archived(scenario: Scenario) -> None:
    """200 with `archived_at`, while every lifecycle verb still refuses it.

    The read is deliberately the one caller that sees an archived row - a
    client can explain "archived" instead of showing "not found" for a
    workflow its own execution list still names. What archiving means is
    unchanged: the four write verbs below are the behaviour this stage must
    not touch.
    """
    workflow_id, _ = published(scenario)
    archived = scenario.client.delete(
        f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}",
        headers=scenario.actor.headers,
    )
    assert archived.status_code == 204, archived.text

    body = read(scenario, workflow_id).json()
    assert body["archived_at"] is not None

    # 409 since stage C2: the row is readable, so a refusal that said "not
    # found" would contradict the read above. The draft route is the one
    # exception and deliberately unchanged - it is a read, and stage C2
    # changed write verbs only.
    for refused in (
        scenario.publish(workflow_id),
        scenario.run(workflow_id),
        scenario.client.post(
            f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}/validate",
            headers=scenario.actor.headers,
        ),
    ):
        assert refused.status_code == 409, refused.text
        assert "archived" in refused.json()["detail"]
    assert read(scenario, workflow_id, "/draft").status_code == 404


# ---------------------------------------------------------------- versions ---


def test_a_workflow_with_nothing_published_has_no_history(scenario: Scenario) -> None:
    """Empty, not a 404: the workflow exists and has published nothing."""
    workflow_id = scenario.workflow()

    response = read(scenario, workflow_id, "/versions")

    assert response.status_code == 200, response.text
    assert response.json() == []


def test_version_history_is_newest_first_with_the_live_one_marked(scenario: Scenario) -> None:
    """Two publishes, two versions, and exactly one of them current."""
    workflow_id, first = published(scenario)
    second_response = scenario.publish(workflow_id)
    assert second_response.status_code == 201, second_response.text
    second = str(second_response.json()["version_id"])

    history = read(scenario, workflow_id, "/versions").json()

    assert [row["version"] for row in history] == [2, 1], "newest first"
    assert [row["id"] for row in history] == [second, first]
    assert [row["is_current"] for row in history] == [True, False]
    assert all(row["published_at"] is not None for row in history)
    assert all(row["created_at"] is not None for row in history)
    assert read(scenario, workflow_id).json()["current_version_id"] == second


def test_the_open_draft_is_never_version_history(scenario: Scenario) -> None:
    """A publish opens the next draft behind itself; history must not show it.

    This is the test that would fail if `published_versions` dropped its
    `published_at IS NOT NULL` predicate: every published workflow has an
    unpublished row, and it is the one somebody is still editing.
    """
    workflow_id, version_id = published(scenario)

    draft = read(scenario, workflow_id, "/draft").json()
    history = read(scenario, workflow_id, "/versions").json()

    assert draft["version"] == 2, "the next draft was opened by the publish"
    assert [row["id"] for row in history] == [version_id]
    assert draft["id"] not in {row["id"] for row in history}


def test_graphs_are_not_part_of_history(scenario: Scenario) -> None:
    """History answers what was published, not what each version contained.

    The graph of the version a run executes is reached through the execution
    that names it; a list of every graph a workflow has ever had is a large
    answer to a small question.
    """
    workflow_id, _ = published(scenario)

    [row] = read(scenario, workflow_id, "/versions").json()

    assert "graph" not in row
    assert set(row) == {"id", "version", "published_at", "created_at", "is_current"}


def test_version_history_of_an_unknown_workflow_is_a_404(scenario: Scenario) -> None:
    assert read(scenario, str(uuid.uuid4()), "/versions").status_code == 404


# -------------------------------------------------------------- executions ---


def test_executions_are_scoped_to_their_own_workflow(scenario: Scenario) -> None:
    """Two workflows, each with runs, and neither list contains the other's."""
    first_id, _ = published(scenario, name="First")
    second_id, _ = published(scenario, name="Second")

    one = scenario.run(first_id)
    two = scenario.run(first_id)
    other = scenario.run(second_id)
    assert {one.status_code, two.status_code, other.status_code} == {202}

    listed = read(scenario, first_id, "/executions")
    assert listed.status_code == 200, listed.text
    ids = [row["id"] for row in listed.json()]

    assert set(ids) == {one.json()["id"], two.json()["id"]}
    assert other.json()["id"] not in ids
    assert [row["workflow_id"] for row in listed.json()] == [first_id, first_id]
    # Newest first, like the project-wide listing.
    assert ids == [two.json()["id"], one.json()["id"]]


def test_the_view_is_the_one_the_execution_routes_return(scenario: Scenario) -> None:
    """The same `ExecutionView`, so a client has one shape for both."""
    workflow_id, _ = published(scenario)
    started = scenario.run(workflow_id).json()

    [listed] = read(scenario, workflow_id, "/executions").json()
    directly = scenario.client.get(
        f"/v1/projects/{scenario.project_id}/executions/{started['id']}",
        headers=scenario.actor.headers,
    )

    assert directly.status_code == 200, directly.text
    assert listed == directly.json()


def test_a_workflow_that_never_ran_has_no_executions(scenario: Scenario) -> None:
    workflow_id, _ = published(scenario)

    assert read(scenario, workflow_id, "/executions").json() == []


def test_the_executions_of_an_unknown_workflow_are_a_404(scenario: Scenario) -> None:
    """Not an empty list, which would say the workflow has never run."""
    assert read(scenario, str(uuid.uuid4()), "/executions").status_code == 404


# ------------------------------------------------------------- the refusals ---


def test_another_projects_workflow_is_a_404_on_all_three(
    scenario: Scenario, client: TestClient, actor: Actor
) -> None:
    """Same workspace, same caller, wrong project.

    The repository filters on `project_id` as well as the id, so a workflow
    the caller may genuinely read *elsewhere* is still not readable through
    this project's path - the tenant predicate does not weaken because the
    caller happens to be a member.
    """
    workflow_id, _ = published(scenario)
    scenario.run(workflow_id)
    elsewhere = client.post(
        f"/v1/workspaces/{scenario.workspace_id}/projects",
        json={"name": "Other island"},
        headers=actor.headers,
    ).json()

    for suffix in ("", "/versions", "/executions"):
        response = client.get(
            f"/v1/projects/{elsewhere['id']}/workflows/{workflow_id}{suffix}",
            headers=actor.headers,
        )
        assert response.status_code == 404, f"{suffix}: {response.text}"


def test_a_stranger_reads_none_of_the_three(scenario: Scenario, make_actor: Any) -> None:
    """A different workspace entirely: 404, because the project is invisible.

    The same answer `test_workflows_api.py` pins for the collection: RLS
    hides the project, `_workspace_of` finds nothing, and the refusal cannot
    confirm that the workflow exists.
    """
    workflow_id, _ = published(scenario)
    scenario.run(workflow_id)
    stranger = make_actor("outsider@example.com")

    for suffix in ("", "/versions", "/executions"):
        response = scenario.client.get(
            f"/v1/projects/{scenario.project_id}/workflows/{workflow_id}{suffix}",
            headers=stranger.headers,
        )
        assert response.status_code == 404, f"{suffix}: {response.text}"


# ----------------------------------------------------------- test harnesses ---


def test_a_harness_is_identifiable_by_id_and_its_runs_are_listed(
    client: TestClient, actor: Actor
) -> None:
    """The whole point of the marker, from the client's side.

    `POST /agents/{id}/test` returns an execution carrying a `workflow_id`
    and nothing else. Following it now answers what that workflow is - the
    harness for this agent - so a test run can be labelled as a test instead
    of as a run of a workflow absent from every listing.
    """
    harness = Harness(client, actor)
    started = client.post(
        f"/v1/projects/{harness.project_id}/agents/{harness.agent_id}/test",
        json={"input": "Summarise the fintech funding news."},
        headers=actor.headers,
    )
    assert started.status_code == 202, started.text
    execution = started.json()
    base = f"/v1/projects/{harness.project_id}/workflows/{execution['workflow_id']}"

    workflow = client.get(base, headers=actor.headers)
    assert workflow.status_code == 200, workflow.text
    assert workflow.json()["is_test_harness"] is True
    assert workflow.json()["test_for_agent_id"] == str(harness.agent_id)

    history = client.get(f"{base}/versions", headers=actor.headers).json()
    assert [(row["version"], row["is_current"]) for row in history] == [(1, True)]

    runs = client.get(f"{base}/executions", headers=actor.headers).json()
    assert [row["id"] for row in runs] == [execution["id"]]


def test_the_ordinary_listing_still_hides_harnesses_and_archived_workflows(
    client: TestClient, actor: Actor
) -> None:
    """The collection is unchanged, which is half of what this stage promised.

    A harness and an archived workflow are both reachable by id now, and
    neither may appear in the listing a person browses: one was never drawn,
    the other is over.
    """
    harness = Harness(client, actor)
    project = f"/v1/projects/{harness.project_id}"
    client.post(
        f"{project}/agents/{harness.agent_id}/test",
        json={"input": "Summarise the fintech funding news."},
        headers=actor.headers,
    )
    drawn = client.post(
        f"{project}/workflows", json={"name": "Drawn by hand"}, headers=actor.headers
    ).json()["workflow"]
    gone = client.post(
        f"{project}/workflows", json={"name": "Finished with"}, headers=actor.headers
    ).json()["workflow"]
    assert (
        client.delete(f"{project}/workflows/{gone['id']}", headers=actor.headers).status_code == 204
    )

    listed = client.get(f"{project}/workflows", headers=actor.headers).json()

    assert [row["id"] for row in listed] == [drawn["id"]]
    assert listed[0]["is_test_harness"] is False
    assert listed[0]["archived_at"] is None
