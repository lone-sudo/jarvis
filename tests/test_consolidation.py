import json
import sys
from unittest.mock import patch

import pytest

from jarvis.memory.consolidation import find_clusters, jaccard_similarity, normalize_tags, build_consolidated_note
from jarvis.memory.markdown import ProjectMemory
from jarvis.state.tracker import StateTracker
from jarvis.state.inbox import InboxStore
from jarvis.interface.cli import main


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def _item(item_id, tags, project_key=None, title="t", summary="s"):
    return {"item_id": item_id, "tags": json.dumps(tags), "project_key": project_key, "title": title, "summary": summary}


# --- Clustering math ---

def test_jaccard_identical_sets_is_one():
    assert jaccard_similarity({"a", "b"}, {"a", "b"}) == 1.0


def test_jaccard_small_overlap_in_large_sets_is_low():
    a = {"python", "sql"}
    b = {"python", "sql", "docker", "airflow", "kubernetes", "terraform", "aws", "gcp", "linux", "bash"}
    assert jaccard_similarity(a, b) < 0.5  # 2 shared tags, but NOT proportionally similar


def test_tag_normalization_case_and_whitespace():
    assert normalize_tags([" Python ", "SQL", "python"]) == {"python", "sql"}


def test_transitive_clustering_via_connected_components():
    """A~B passes, B~C passes, A~C does NOT pass alone -- all 3 still cluster together."""
    items = [_item("INB-A", ["x", "y", "z"]), _item("INB-B", ["y", "z", "w"]), _item("INB-C", ["z", "w", "v"])]
    assert jaccard_similarity(normalize_tags(["x", "y", "z"]), normalize_tags(["y", "z", "w"])) >= 0.5
    assert jaccard_similarity(normalize_tags(["y", "z", "w"]), normalize_tags(["z", "w", "v"])) >= 0.5
    assert jaccard_similarity(normalize_tags(["x", "y", "z"]), normalize_tags(["z", "w", "v"])) < 0.5

    clusters = find_clusters(items)
    assert len(clusters) == 1
    assert {i["item_id"] for i in clusters[0].items} == {"INB-A", "INB-B", "INB-C"}


def test_clustering_is_deterministic_regardless_of_input_order():
    items = [_item("INB-A", ["x", "y", "z"]), _item("INB-B", ["y", "z", "w"]), _item("INB-C", ["z", "w", "v"])]
    result1 = find_clusters(items)
    result2 = find_clusters(list(reversed(items)))
    ids1 = sorted(tuple(sorted(i["item_id"] for i in c.items)) for c in result1)
    ids2 = sorted(tuple(sorted(i["item_id"] for i in c.items)) for c in result2)
    assert ids1 == ids2


def test_unrelated_items_do_not_cluster():
    items = [_item("INB-A", ["python", "sql"]), _item("INB-B", ["cooking", "recipe"])]
    assert find_clusters(items) == []


def test_confirmed_projects_never_mix_even_with_identical_tags():
    """The core structural guarantee both reviewers required."""
    items = [
        _item("INB-A", ["python", "sql"], project_key="project-alpha"),
        _item("INB-B", ["python", "sql"], project_key="project-beta"),
    ]
    assert find_clusters(items) == []


def test_unlinked_items_with_identical_tags_do_cluster():
    items = [_item("INB-A", ["python", "sql"], project_key=None), _item("INB-B", ["python", "sql"], project_key=None)]
    clusters = find_clusters(items)
    assert len(clusters) == 1
    assert clusters[0].project_key is None


def test_items_with_no_tags_excluded_from_clustering():
    items = [_item("INB-A", ["python", "sql"]), _item("INB-B", [])]
    clusters = find_clusters(items)
    # INB-B has no tags -- can't cluster with anything, and shouldn't crash
    assert all("INB-B" not in {i["item_id"] for i in c.items} for c in clusters)


# --- CLI integration ---

def _setup_two_linked_items(project_key="proj"):
    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    tracker.register_project(project_key, project_key)
    ids = []
    for title, tags in [("A", ["x", "y", "z"]), ("B", ["y", "z", "w"])]:
        item_id = store.create_item(title=title, source_url=None, note="", relative_markdown_path="")
        store.mark_processed(item_id, summary=f"summary {title}", tags=tags, actionable=False, suggested_project_key=None, confidence=0.8)
        store.link_to_project(item_id, project_key)
        ids.append(item_id)
    return tracker, store, ids


def test_confirmed_cluster_writes_to_project_memory_and_archives_originals(tmp_path):
    tracker, store, ids = _setup_two_linked_items()

    with patch("builtins.input", return_value="y"):
        sys.argv = ["jarvis", "inbox-consolidate"]
        main()

    for item_id in ids:
        assert store.get_item(item_id)["status"] == "ARCHIVED"  # archived, and still readable -- not deleted

    project_root = tracker.get_project_root("proj")
    notes = ProjectMemory.read_latest(project_root, max_chars=5000)
    assert "Consolidated" in notes


def test_declined_confirmation_leaves_everything_untouched(tmp_path):
    tracker, store, ids = _setup_two_linked_items()

    with patch("builtins.input", return_value="n"):
        sys.argv = ["jarvis", "inbox-consolidate"]
        main()

    for item_id in ids:
        assert store.get_item(item_id)["status"] == "PROCESSED"  # untouched


def test_write_failure_does_not_archive_originals(tmp_path):
    """The atomicity invariant both reviewers required, tested at the CLI level."""
    tracker, store, ids = _setup_two_linked_items()

    with patch("jarvis.interface.cli.ProjectMemory.append_note", side_effect=RuntimeError("simulated failure")):
        with patch("builtins.input", return_value="y"):
            sys.argv = ["jarvis", "inbox-consolidate"]
            main()

    for item_id in ids:
        assert store.get_item(item_id)["status"] == "PROCESSED"  # NOT archived despite the "yes"


def test_suggested_project_key_never_grants_project_memory_write(tmp_path):
    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    tracker.register_project("some-project", "some-project")

    ids = []
    for title, tags in [("A", ["x", "y", "z"]), ("B", ["y", "z", "w"])]:
        item_id = store.create_item(title=title, source_url=None, note="", relative_markdown_path="")
        # suggested, but deliberately NEVER linked
        store.mark_processed(item_id, summary=f"summary {title}", tags=tags, actionable=False, suggested_project_key="some-project", confidence=0.9)
        ids.append(item_id)

    with patch("builtins.input", return_value="y"):
        sys.argv = ["jarvis", "inbox-consolidate"]
        main()

    from jarvis.policy.rules import SecurityPolicy
    project_notes_path = SecurityPolicy.get_workspace_root() / "some-project" / "JARVIS_NOTES.md"
    assert not project_notes_path.exists()  # no write ever happened to the suggested project

    for item_id in ids:
        assert store.get_item(item_id)["status"] == "ARCHIVED"  # still consolidated -- just into a new inbox item

    remaining = [i for i in store.list_items() if i["item_id"] not in ids]
    assert len(remaining) == 1
    assert remaining[0]["status"] == "PROCESSED"


def test_no_clusters_found_message(capsys):
    tracker = StateTracker()
    store = InboxStore(tracker.db_path)
    item_id = store.create_item(title="lonely", source_url=None, note="", relative_markdown_path="")
    store.mark_processed(item_id, summary="s", tags=["unique-tag-nobody-shares"], actionable=False, suggested_project_key=None, confidence=0.5)

    sys.argv = ["jarvis", "inbox-consolidate"]
    main()
    out = capsys.readouterr().out
    assert "No candidate clusters found" in out
