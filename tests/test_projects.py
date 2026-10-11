"""Project fixtures only: no controller, credentials, provider or game."""
from concurrent.futures import ThreadPoolExecutor

import pytest

from ashad.projects import Projects
from ashad.store import Store


@pytest.fixture
def workspace(tmp_path):
    store = Store(tmp_path / "source")
    yield store, Projects(store)
    store.close()


def test_create_normalizes_names_deduplicates_requests_and_keeps_duplicate_names_distinct(workspace):
    store, projects = workspace
    first = projects.create("  Identical name  ", "request-a")
    assert projects.create("Identical name", "request-a") == first
    second = projects.create("Identical name", "request-b")
    assert first["id"] != second["id"]
    assert first["created_at"] == first["updated_at"]
    with pytest.raises(ValueError, match="different payload"):
        projects.create("Changed", "request-a")
    assert len(store.list("projects")) == 2


@pytest.mark.parametrize("name", ["", "  ", None, 1, "x" * 201, "name\nline", "name\x00", "name\x7f"])
def test_project_name_validation(workspace, name):
    store, projects = workspace
    with pytest.raises(ValueError, match="name"):
        projects.create(name, "request")
    assert store.list("projects") == []


@pytest.mark.parametrize("request_id", ["", "../path", "x" * 101, None, 12])
def test_project_request_validation(workspace, request_id):
    store, projects = workspace
    with pytest.raises(ValueError, match="Request ID"):
        projects.create("Game", request_id)
    assert not store.list("projects")


def test_concurrent_retries_commit_one_project_and_conflicting_requests_do_not_overwrite(workspace):
    store, projects = workspace
    with ThreadPoolExecutor(max_workers=4) as executor:
        results = list(executor.map(lambda _: projects.create("One", "same-request"), range(8)))
    assert len({result["id"] for result in results}) == len(store.list("projects")) == 1


def test_membership_derived_from_build_and_rejects_cross_project_or_absent_records(workspace):
    store, projects = workspace
    first, second = projects.create("One", "one"), projects.create("Two", "two")
    store.put("builds", {"id": "build-a", "project_id": first["id"]})
    store.put("configurations", {"id": "agent-a", "build_id": "build-a"})
    assert projects.project_for_configuration("agent-a") == first["id"]
    assert projects.project_for_build("build-a", first["id"]) == first["id"]
    with pytest.raises(ValueError, match="selected project"):
        projects.project_for_configuration("agent-a", second["id"])
    store.put("configurations", {"id": "agent-a", "build_id": "build-a", "project_id": second["id"]})
    with pytest.raises(ValueError, match="different projects"):
        projects.project_for_configuration("agent-a")
    with pytest.raises(ValueError, match="Unknown builds"):
        projects.project_for_build("absent")
    store.put("builds", {"id": "legacy"})
    with pytest.raises(ValueError, match="no project"):
        projects.project_for_build("legacy")
    assert projects.project_for_build("legacy", allow_unassigned=True) is None
    store.put("configurations", {"id": "legacy-agent", "build_id": "legacy", "project_id": first["id"]})
    with pytest.raises(ValueError, match="different projects"):
        projects.project_for_configuration("legacy-agent", allow_unassigned=True)


@pytest.mark.parametrize("field,value", [("artifact_sha256", "changed"), ("source_revision", "rev-2"),
                                        ("engine_sha256", "changed-engine"), ("command", ["other-executable"])])
def test_existing_build_identity_is_immutable_even_without_references(workspace, field, value):
    store, projects = workspace
    project = projects.create("Game", "request")
    original = {"id": "build-a", "name": "First", "source_revision": "rev-1", "artifact_sha256": "hash", "command": ["fixture"]}
    saved = projects.register_build(original, project["id"])
    assert projects.register_build({**original, "name": "Renamed"}, project["id"])["name"] == "Renamed"
    with pytest.raises(ValueError, match="new build ID"):
        projects.register_build({**original, field: value}, project["id"])
    assert store.require("builds", "build-a")["artifact_sha256"] == saved["artifact_sha256"]
    assert projects.register_build({**original, "id": "new-build", field: value}, project["id"])["id"] == "new-build"


def test_registration_rejects_cross_project_references_and_reassignment(workspace):
    store, projects = workspace
    first, second = projects.create("One", "one"), projects.create("Two", "two")
    build = {"id": "build-a", "source_revision": "r"}
    with pytest.raises(ValueError, match="Select a project"):
        projects.register_build(build)
    store.put("builds", build)
    store.put("configurations", {"id": "agent-a", "build_id": "build-a", "project_id": second["id"]})
    with pytest.raises(ValueError, match="Referenced project configuration"):
        projects.register_build(build, first["id"])
    projects.register_build(build, second["id"])
    with pytest.raises(ValueError, match="cannot move"):
        projects.register_build(build, first["id"])
