import json

import pytest

from jarvis.state.tracker import StateTracker
from jarvis.state.inbox import InboxStore
from jarvis.memory.inbox_markdown import InboxMarkdown
from jarvis.memory.inbox_prompt import build_classification_prompt


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


@pytest.fixture
def store(tmp_path):
    tracker = StateTracker(db_path=tmp_path / "test.db")
    return InboxStore(tracker.db_path)


def test_create_and_get_item(store):
    item_id = store.create_item("Title", "http://example.com", "a note", "inbox/x.md")
    assert item_id.startswith("INB-")
    item = store.get_item(item_id)
    assert item["status"] == "UNPROCESSED"
    assert item["project_key"] is None
    assert item["suggested_project_key"] is None


def test_mark_processed_never_sets_project_key(store):
    """
    The core anti-overload invariant: classification alone must never
    populate project_key (a confirmed link) -- only suggested_project_key.
    """
    item_id = store.create_item("Title", None, "", "inbox/x.md")
    store.mark_processed(
        item_id, summary="sum", tags=["a", "b"], actionable=True,
        suggested_project_key="some-project", confidence=0.8,
    )
    item = store.get_item(item_id)
    assert item["status"] == "PROCESSED"
    assert item["project_key"] is None  # NOT linked
    assert item["suggested_project_key"] == "some-project"  # advisory only
    assert json.loads(item["tags"]) == ["a", "b"]


def test_link_to_project_is_the_only_way_to_set_project_key(store, tmp_path):
    tracker = StateTracker(db_path=store.db_path)
    tracker.register_project("proj-a", "proj-a")

    item_id = store.create_item("Title", None, "", "inbox/x.md")
    store.mark_processed(item_id, summary="s", tags=[], actionable=False, suggested_project_key="proj-a", confidence=0.5)
    assert store.get_item(item_id)["project_key"] is None

    store.link_to_project(item_id, "proj-a")
    assert store.get_item(item_id)["project_key"] == "proj-a"


def test_archive_transitions_status(store):
    item_id = store.create_item("Title", None, "", "inbox/x.md")
    store.archive(item_id)
    assert store.get_item(item_id)["status"] == "ARCHIVED"


def test_archive_unknown_item_raises(store):
    with pytest.raises(ValueError):
        store.archive("INB-DOES-NOT-EXIST")


def test_list_items_filters_by_status(store):
    a = store.create_item("A", None, "", "inbox/a.md")
    b = store.create_item("B", None, "", "inbox/b.md")
    store.archive(b)

    unprocessed = store.list_items(status="UNPROCESSED")
    archived = store.list_items(status="ARCHIVED")
    assert [i["item_id"] for i in unprocessed] == [a]
    assert [i["item_id"] for i in archived] == [b]


def test_inbox_item_rejects_project_key_not_in_projects_table(tmp_path):
    """
    project_key has a real foreign key to the projects table (unlike
    suggested_project_key, which is deliberately unconstrained free text
    since it's just an AI's guess). Confirming the constraint holds for
    the confirmed-link path specifically.
    """
    tracker = StateTracker(db_path=tmp_path / "test.db")
    store = InboxStore(tracker.db_path)
    item_id = store.create_item("Title", None, "", "inbox/x.md")

    from jarvis.state.database import get_connection
    with pytest.raises(Exception):
        with get_connection(store.db_path) as conn:
            conn.execute(
                "UPDATE inbox_items SET project_key = ? WHERE item_id = ?",
                ("nonexistent-project", item_id),
            )


# --- Markdown: reversible classification ---

def test_write_capture_then_read_content_roundtrip():
    InboxMarkdown.write_capture("INB-1", "Title", "http://x.com", "a note", "the raw content here")
    assert InboxMarkdown.read_content("INB-1") == "the raw content here"


def test_classification_never_alters_original_capture():
    InboxMarkdown.write_capture("INB-2", "Title", None, "", "original text, must survive untouched")
    before = InboxMarkdown.read_content("INB-2")

    InboxMarkdown.write_classification("INB-2", "a summary", ["tag1", "tag2"])
    after = InboxMarkdown.read_content("INB-2")

    assert before == after == "original text, must survive untouched"


def test_classification_adds_ai_summary_section():
    InboxMarkdown.write_capture("INB-3", "Title", None, "", "content")
    InboxMarkdown.write_classification("INB-3", "my summary", ["a"])
    full_text = InboxMarkdown._path_for("INB-3").read_text()
    assert "## AI Summary" in full_text
    assert "my summary" in full_text
    assert "## Original Capture" in full_text


# --- Prompt template ---

def test_classification_prompt_never_suggests_task_creation():
    prompt = build_classification_prompt("T", "http://x", "note", "content", ["proj-a"])
    assert "do not suggest creating a task" in prompt.lower()


def test_classification_prompt_includes_known_projects():
    prompt = build_classification_prompt("T", None, "", "content", ["proj-a", "proj-b"])
    assert "proj-a" in prompt
    assert "proj-b" in prompt


def test_classification_prompt_handles_no_known_projects():
    prompt = build_classification_prompt("T", None, "", "content", [])
    assert "none registered yet" in prompt.lower()
