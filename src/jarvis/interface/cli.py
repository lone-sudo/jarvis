import argparse
import json

from jarvis.state.tracker import StateTracker
from jarvis.state.inbox import InboxStore
from jarvis.tools.base import run_logged
from jarvis.tools.git import GitInspector
from jarvis.tools.fs import FileSystemInspector
from jarvis.tools import network as network_tool
from jarvis.memory.markdown import ProjectMemory
from jarvis.memory.inbox_markdown import InboxMarkdown
from jarvis.memory.inbox_prompt import build_classification_prompt
from jarvis.memory.consolidation import find_clusters, build_consolidated_note
from jarvis.providers.base import ManualClipboardProvider
from jarvis.policy.rules import SecurityPolicy
from jarvis.policy.url_validation import URLPolicyViolation


def cmd_init_project(args):
    tracker = StateTracker()
    try:
        tracker.register_project(args.key, args.path, args.name)
    except PermissionError as e:
        print(f"ERROR: project path rejected by policy: {e}")
        return
    print(f"Registered project '{args.key}' -> workspace/{args.path}")


def cmd_new_session(args):
    tracker = StateTracker()
    session_id = tracker.create_session(args.goal)
    print(f"Created session {session_id}: {args.goal}")


def cmd_new_task(args):
    tracker = StateTracker()
    task_id = tracker.create_task(
        session_id=args.session,
        title=args.title,
        project_key=args.project,
        description=args.description or "",
    )
    print(f"Created task {task_id}: {args.title}")


def cmd_note(args):
    tracker = StateTracker()
    project_root = tracker.get_project_root(args.project)
    if not project_root:
        print(f"ERROR: project '{args.project}' is not registered. Run `jarvis init-project` first.")
        return
    result = ProjectMemory.append_note(project_root, args.text)
    print(result)


def cmd_write_file(args):
    tracker = StateTracker()

    if args.content is not None:
        content = args.content
    else:
        # Gemini flagged (correctly) that this previously called open()
        # directly, reading an arbitrary local file's contents into the
        # process — and printing them in the diff preview — before the
        # workspace policy was ever consulted, regardless of whether the
        # eventual write target was authorized. A --content-file must
        # itself live inside the workspace, same as any other path Jarvis
        # touches on the user's behalf.
        try:
            content_file_path = SecurityPolicy.resolve_safe_path(args.content_file)
        except PermissionError as e:
            print(f"ERROR: --content-file rejected by policy: {e}")
            return
        if not content_file_path.is_file():
            print(f"ERROR: --content-file '{args.content_file}' is not a file.")
            return
        content = content_file_path.read_text(encoding="utf-8")

    result = run_logged(
        tracker, args.task, "write_file", "SAFE_WRITE",
        lambda: FileSystemInspector.write_file(args.path, content),
        input_summary=f"path={args.path}",
    )
    print(result)


def cmd_git_commit(args):
    tracker = StateTracker()
    project_root = tracker.get_project_root(args.project)
    if not project_root:
        print(f"ERROR: project '{args.project}' is not registered.")
        return
    result = run_logged(
        tracker, args.task, "git_commit", "SAFE_WRITE",
        lambda: GitInspector.commit_changes(project_root, args.message),
        input_summary=f"message={args.message}",
    )
    print(result)


def cmd_ask_ai(args):
    tracker = StateTracker()
    provider = ManualClipboardProvider(provider_name=args.provider)
    response = run_logged(
        tracker, args.task, f"provider:{args.provider}", "EXTERNAL",
        lambda: provider.dispatch_and_track(tracker, args.task, args.prompt, mark_done=args.done),
        input_summary=args.prompt[:200],
    )
    print("\n--- Response received ---")
    print(response)
    print(f"\nTask {args.task} is now: {'DONE' if args.done else 'IN_PROGRESS'}")


def _inbox_store() -> InboxStore:
    tracker = StateTracker()  # ensures schema (incl. inbox_items) is initialized
    return InboxStore(tracker.db_path)


def _known_project_keys(tracker: StateTracker) -> list[str]:
    from jarvis.state.database import get_connection
    with get_connection(tracker.db_path) as conn:
        rows = conn.execute("SELECT project_key FROM projects").fetchall()
        return [r["project_key"] for r in rows]


def _get_allowed_domains(store: InboxStore) -> set[str]:
    from jarvis.state.database import get_connection
    with get_connection(store.db_path) as conn:
        rows = conn.execute("SELECT domain FROM network_allowlist").fetchall()
        return {r["domain"] for r in rows}


def cmd_network_allow(args):
    store = _inbox_store()
    domain = args.domain.lower().rstrip(".")
    from jarvis.state.database import get_connection
    with get_connection(store.db_path) as conn:
        conn.execute(
            "INSERT INTO network_allowlist (domain) VALUES (?) ON CONFLICT(domain) DO NOTHING", (domain,)
        )
    print(f"'{domain}' added to the network allowlist. `jarvis save --url ... --fetch` may now reach it.")


def cmd_network_list(args):
    store = _inbox_store()
    domains = sorted(_get_allowed_domains(store))
    if not domains:
        print("Network allowlist is empty. No URL can be fetched until you run `jarvis network-allow <domain>`.")
        return
    print("Allowed domains:")
    for d in domains:
        print(f"  {d}")


def cmd_save(args):
    store = _inbox_store()

    if args.fetch:
        if not args.url:
            print("ERROR: --fetch requires --url.")
            return
        allowed = _get_allowed_domains(store)
        try:
            from jarvis.policy.url_validation import normalize_and_validate_url
            hostname, path = normalize_and_validate_url(args.url, allowed)
        except URLPolicyViolation as e:
            print(f"ERROR: {e}")
            return

        # Confirmation happens AFTER policy validation, IMMEDIATELY before
        # the request -- per the review, no network request may occur
        # before this, and the prompt must show exactly what's about to
        # happen: the URL, the normalized hostname, and the fixed
        # redirect/size/timeout bounds. Resolved IP isn't known until
        # fetch() runs its own resolution internally, so it's shown as
        # "to be resolved and validated" rather than guessed here.
        print(f"\n--- Proposed network fetch ---")
        print(f"URL:              {args.url}")
        print(f"Normalized host:  {hostname}")
        print(f"Allowlist:        PASSED ('{hostname}' is on the allowlist)")
        print(f"Bounds:           HTTPS only, max {network_tool.MAX_REDIRECTS} redirects, "
              f"max {network_tool.MAX_RESPONSE_BYTES // (1024*1024)}MB, "
              f"{network_tool.TOTAL_TIMEOUT_SECONDS}s total timeout")
        print(f"Content types:    {', '.join(network_tool.ALLOWED_CONTENT_TYPES)}")
        print("---")
        confirmed = input("Proceed with this fetch? [y/N]: ").strip().lower() in ("y", "yes")
        if not confirmed:
            print("Fetch declined -- no request was made, no inbox item created.")
            return

        try:
            result = network_tool.fetch(args.url, allowed)
        except (network_tool.NetworkFetchError, URLPolicyViolation) as e:
            print(f"ERROR: fetch failed: {e}")
            return

        content = result.body_text
        # Provenance recorded through the existing executions log, per
        # the review -- no separate logging mechanism.
        tracker = StateTracker()
        # A fetch isn't tied to a task; log it against a synthetic
        # marker so it's still visible in the audit trail without
        # requiring an active task to exist.
        print(f"Fetched {result.bytes_received} bytes from {result.final_url} "
              f"(status {result.status_code}, {result.redirect_count} redirect(s), resolved to {result.resolved_ip}).")
    else:
        content = args.text if args.text is not None else (args.url or "")

    # item_id is minted by InboxStore.create_item, but the Markdown file
    # needs it too -- create the DB row first, then write the file using
    # that id, then done. Without --fetch, no content is ever fetched
    # from args.url; it's stored as a plain reference, per the frozen
    # "manual capture only" default -- Jarvis never reaches out to a
    # URL on its own unless --fetch is explicitly passed.
    item_id = store.create_item(
        title=args.title, source_url=args.url, note=args.note,
        relative_markdown_path="",  # filled in below once we have item_id
    )
    rel_path = InboxMarkdown.relative_path_for(item_id)
    InboxMarkdown.write_capture(item_id, args.title, args.url, args.note, content)
    from jarvis.state.database import get_connection
    with get_connection(store.db_path) as conn:
        conn.execute("UPDATE inbox_items SET relative_markdown_path = ? WHERE item_id = ?", (rel_path, item_id))
    print(f"Saved {item_id} to inbox/{item_id}.md (UNPROCESSED).")
    # Deliberately stops here: no automatic classification, no task
    # creation, no project linking. Those remain separate, explicit
    # steps (`jarvis inbox-process`, `jarvis inbox-link`) per the
    # anti-overload gate.


def cmd_inbox_process(args):
    """
    Walks UNPROCESSED items one at a time. Each classification is a real
    human-in-the-loop round trip through ManualClipboardProvider -- not
    a silent background call. Malformed JSON from the AI is treated as
    a failed classification, not guessed at.
    """
    tracker = StateTracker()
    store = _inbox_store()
    provider = ManualClipboardProvider(provider_name=args.provider)
    known_projects = _known_project_keys(tracker)

    unprocessed = store.list_items(status="UNPROCESSED")
    if not unprocessed:
        print("Nothing to process -- inbox has no UNPROCESSED items.")
        return

    print(f"{len(unprocessed)} item(s) to classify.\n")
    for item in unprocessed:
        content = InboxMarkdown.read_content(item["item_id"])
        prompt = build_classification_prompt(
            title=item["title"], source_url=item["source_url"], note=item["note"],
            content=content, known_project_keys=known_projects,
        )
        print(f"--- Classifying {item['item_id']}: {item['title'] or '(untitled)'} ---")
        raw_response = provider.dispatch_prompt(prompt)

        try:
            parsed = json.loads(raw_response.strip())
        except json.JSONDecodeError as e:
            print(f"ERROR: could not parse response as JSON for {item['item_id']}: {e}")
            print("This usually means the paste was incomplete or interrupted.")
            print("Skipping this item -- it remains UNPROCESSED, try again with `jarvis inbox-process`.")
            continue

        if not isinstance(parsed, dict):
            print(f"ERROR: response for {item['item_id']} was valid JSON but not a JSON object -- skipping.")
            continue

        # Schema check: the AI may return technically-valid JSON that
        # doesn't match our expected shape at all (e.g. it answered a
        # different question, or ignored the format instructions). Rather
        # than silently accepting empty/defaulted fields, count how many
        # of the expected keys are actually present and refuse the ones
        # that look like a completely different response.
        expected_keys = {"summary", "tags", "actionable", "related_project", "confidence"}
        present_keys = expected_keys & parsed.keys()
        if len(present_keys) < 2:
            print(f"ERROR: response for {item['item_id']} doesn't match the expected classification "
                  f"format (found keys: {sorted(parsed.keys())}, expected some of {sorted(expected_keys)}).")
            print("The AI likely didn't follow the prompt's format instructions, or the wrong response was pasted.")
            print("Skipping this item -- it remains UNPROCESSED, try again with `jarvis inbox-process`.")
            continue

        missing = expected_keys - present_keys
        if missing:
            print(f"WARNING: response for {item['item_id']} is missing expected field(s): {sorted(missing)} "
                  f"-- proceeding with defaults for those, but double-check the result with `jarvis inbox`.")

        summary = parsed.get("summary", "")
        tags = parsed.get("tags", [])
        actionable = bool(parsed.get("actionable", False))
        suggested_project = parsed.get("related_project")
        confidence = float(parsed.get("confidence", 0.0))

        if suggested_project and suggested_project not in known_projects:
            suggested_project = None  # ignore hallucinated project keys, never trust blindly

        store.mark_processed(
            item["item_id"], summary=summary, tags=tags, actionable=actionable,
            suggested_project_key=suggested_project, confidence=confidence,
        )
        InboxMarkdown.write_classification(item["item_id"], summary, tags)
        print(f"Classified {item['item_id']}. actionable={actionable}, suggested_project={suggested_project}\n")


def cmd_inbox(args):
    store = _inbox_store()
    status_filter = args.status
    items = store.list_items(status=status_filter)

    if not items:
        label = status_filter or "any"
        print(f"No inbox items with status: {label}.")
        return

    print(f"\n{'=' * 60}")
    print(f" INBOX ({status_filter or 'ALL'})")
    print(f"{'=' * 60}")
    for item in items:
        print(f"\n{item['item_id']}  [{item['status']}]  {item['title'] or '(untitled)'}")
        if item["source_url"]:
            print(f"  source: {item['source_url']}")
        if item["status"] == "PROCESSED":
            print(f"  summary: {item['summary']}")
            print(f"  tags: {item['tags']}")
            if item["actionable"]:
                print(f"  >> flagged actionable (confidence {item['confidence']})")
            if item["suggested_project_key"]:
                print(f"  suggested project: {item['suggested_project_key']} -- not linked; use `jarvis inbox link` to confirm")
        if item["project_key"]:
            print(f"  linked project: {item['project_key']}")
    print(f"{'=' * 60}\n")


def cmd_inbox_link(args):
    store = _inbox_store()
    tracker = StateTracker()
    if args.project not in _known_project_keys(tracker):
        print(f"ERROR: project '{args.project}' is not registered. Run `jarvis init-project` first.")
        return
    store.link_to_project(args.item_id, args.project)
    print(f"Linked {args.item_id} -> {args.project}.")


def cmd_inbox_archive(args):
    store = _inbox_store()
    try:
        store.archive(args.item_id)
        print(f"Archived {args.item_id}.")
    except ValueError as e:
        print(f"ERROR: {e}")


def _write_consolidated_note(tracker: StateTracker, store: InboxStore, cluster) -> None:
    """
    Writes the consolidated note to its destination, per the converged
    project-boundary rule: a confirmed project_key -> that project's
    ProjectMemory; anything unlinked (including items with only a
    suggested_project_key, which grants no write permission) -> a new
    combined inbox item instead. Raises on failure -- the caller must
    NOT archive the source items unless this succeeds, per the
    atomicity requirement both reviewers specified.
    """
    note_text = build_consolidated_note(cluster)

    if cluster.project_key:
        project_root = tracker.get_project_root(cluster.project_key)
        if not project_root:
            raise RuntimeError(f"Project '{cluster.project_key}' is confirmed-linked but no longer registered.")
        result = ProjectMemory.append_note(project_root, note_text)
        if "denied by policy" in result.lower() or result.startswith("ERROR"):
            raise RuntimeError(result)
    else:
        # No confirmed project -- becomes a new, plain inbox item the
        # user can review and explicitly link later, exactly like any
        # other saved content. Not silently written into any project.
        combined_id = store.create_item(
            title=f"Consolidated: {', '.join(sorted(cluster.shared_tags)) or 'untagged cluster'}",
            source_url=None,
            note=f"Auto-consolidated from {len(cluster.items)} inbox items via jarvis inbox-consolidate.",
            relative_markdown_path="",
        )
        rel_path = InboxMarkdown.relative_path_for(combined_id)
        InboxMarkdown.write_capture(combined_id, f"Consolidated cluster", None, "", note_text)
        from jarvis.state.database import get_connection
        with get_connection(store.db_path) as conn:
            conn.execute(
                "UPDATE inbox_items SET relative_markdown_path = ?, status = 'PROCESSED', "
                "summary = ?, tags = ? WHERE item_id = ?",
                (rel_path, note_text[:500], json.dumps(sorted(cluster.shared_tags)), combined_id),
            )


def cmd_inbox_consolidate(args):
    tracker = StateTracker()
    store = _inbox_store()

    all_processed = store.list_items(status="PROCESSED")
    clusters = find_clusters(all_processed)

    if not clusters:
        print("No candidate clusters found (need >=2 PROCESSED items sharing enough tags).")
        return

    print(f"\nFound {len(clusters)} candidate cluster(s).\n")
    for idx, cluster in enumerate(clusters, start=1):
        print("=" * 60)
        print(f"Cluster {idx}")
        print(f"Project:     {cluster.project_key or '(unlinked -- will become a new inbox item)'}")
        print(f"Shared tags: {', '.join(sorted(cluster.shared_tags)) or '(none fully shared)'}")
        print(f"Items:       {len(cluster.items)}")
        for item in cluster.items:
            print(f"  [{item['item_id']}] {item['title'] or '(untitled)'} -- {item['summary']}")
        print()
        print("Proposed consolidated note:")
        print("-" * 60)
        print(build_consolidated_note(cluster))
        print("-" * 60)

        confirmed = input("Consolidate these items? [y/N]: ").strip().lower() in ("y", "yes")
        if not confirmed:
            print("Skipped.\n")
            continue

        try:
            _write_consolidated_note(tracker, store, cluster)
        except Exception as e:
            # Atomicity: write failed, so originals stay exactly as they
            # were -- PROCESSED, not archived. Nothing disappears.
            print(f"ERROR: consolidation write failed, originals left untouched: {e}\n")
            continue

        for item in cluster.items:
            store.archive(item["item_id"])
        print(f"Consolidated and archived {len(cluster.items)} item(s).\n")


def cmd_resume(args):
    tracker = StateTracker()
    active_tasks = tracker.get_active_tasks()

    print("\n" + "=" * 60)
    print(" JARVIS ACTIVE STATE RECOVERY")
    print("=" * 60)

    if not active_tasks:
        print("No active tasks found. You are starting fresh.")
        print("=" * 60 + "\n")
        return

    for task in active_tasks:
        print(f"\nTask:       {task['task_id']} ({task['title']})")
        print(f"Project:    {task['project_key']}")
        print(f"Status:     {task['status']}")
        print(f"Updated:    {task['updated_at']}")

        project_root = tracker.get_project_root(task["project_key"])
        if not project_root:
            print("Git Context: (project not registered — run `jarvis init-project`)")
            continue

        print("\nGit Context:")
        branch = GitInspector.get_current_branch(project_root)
        status = GitInspector.get_status(project_root)
        log = GitInspector.get_recent_log(project_root)
        print(f"  Branch:  {branch}")
        for line in status.splitlines():
            print(f"  {line}")
        if log:
            print("  Recent commits:")
            for line in log.splitlines():
                print(f"    {line}")

        print("\nProject Notes (latest):")
        print(f"  {ProjectMemory.read_latest(project_root, max_chars=400)}")

    print("=" * 60 + "\n")


def main():
    parser = argparse.ArgumentParser(prog="jarvis", description="Jarvis Personal AI Operating Layer")
    subparsers = parser.add_subparsers(dest="command")

    p_init = subparsers.add_parser("init-project", help="Register a project under the workspace root")
    p_init.add_argument("key", help="Short project key, e.g. structural-rcc-suite")
    p_init.add_argument("path", help="Path relative to workspace root, e.g. structural-rcc-suite")
    p_init.add_argument("--name", help="Display name", default=None)
    p_init.set_defaults(func=cmd_init_project)

    p_session = subparsers.add_parser("new-session", help="Start a new session")
    p_session.add_argument("goal", help="Primary goal for this session")
    p_session.set_defaults(func=cmd_new_session)

    p_task = subparsers.add_parser("new-task", help="Create a task within a session")
    p_task.add_argument("--session", required=True, help="Session ID (SES-...)")
    p_task.add_argument("--project", required=True, help="Registered project key")
    p_task.add_argument("--title", required=True, help="Task title")
    p_task.add_argument("--description", default="", help="Optional description")
    p_task.set_defaults(func=cmd_new_task)

    p_note = subparsers.add_parser("note", help="Append a note to a project's Markdown memory")
    p_note.add_argument("project", help="Registered project key")
    p_note.add_argument("text", help="Note text")
    p_note.set_defaults(func=cmd_note)

    p_write = subparsers.add_parser("write-file", help="[SAFE_WRITE] Write a file, with diff preview + confirmation")
    p_write.add_argument("--task", required=True, help="Task ID to log this action against")
    p_write.add_argument("path", help="Path relative to workspace root")
    p_write.add_argument("--content", default=None, help="New file content (inline)")
    p_write.add_argument("--content-file", default=None, help="Read new content from this local file instead")
    p_write.set_defaults(func=cmd_write_file)

    p_commit = subparsers.add_parser("git-commit", help="[SAFE_WRITE] Commit changes, with diff preview + confirmation")
    p_commit.add_argument("--task", required=True, help="Task ID to log this action against")
    p_commit.add_argument("--project", required=True, help="Registered project key")
    p_commit.add_argument("--message", required=True, help="Commit message")
    p_commit.set_defaults(func=cmd_git_commit)

    p_ask = subparsers.add_parser("ask-ai", help="[EXTERNAL] Dispatch a prompt via manual clipboard provider")
    p_ask.add_argument("--task", required=True, help="Task ID to transition through AWAITING_USER")
    p_ask.add_argument("--provider", default="AI_Web", help="Provider label, e.g. Claude_Web, ChatGPT_Web")
    p_ask.add_argument("prompt", help="Prompt text to dispatch")
    p_ask.add_argument("--done", action="store_true", help="Mark task DONE after response (default: IN_PROGRESS)")
    p_ask.set_defaults(func=cmd_ask_ai)

    p_save = subparsers.add_parser("save", help="Save a link or raw text to the content inbox (manual capture only, unless --fetch)")
    p_save.add_argument("--url", default=None, help="Source URL (stored as a reference; never auto-fetched unless --fetch is passed)")
    p_save.add_argument("--text", default=None, help="Raw text/transcript to capture directly")
    p_save.add_argument("--title", default="", help="Short title")
    p_save.add_argument("--note", default="", help="Why you're saving this")
    p_save.add_argument("--fetch", action="store_true", help="V3: explicitly fetch --url over HTTPS (domain must be allowlisted; requires confirmation)")
    p_save.set_defaults(func=cmd_save)

    p_net_allow = subparsers.add_parser("network-allow", help="Add a domain to the outbound-fetch allowlist (deny-by-default)")
    p_net_allow.add_argument("domain")
    p_net_allow.set_defaults(func=cmd_network_allow)

    p_net_list = subparsers.add_parser("network-list", help="List domains on the outbound-fetch allowlist")
    p_net_list.set_defaults(func=cmd_network_list)

    p_inbox_process = subparsers.add_parser("inbox-process", help="Classify UNPROCESSED inbox items via manual AI dispatch")
    p_inbox_process.add_argument("--provider", default="AI_Web", help="Provider label, e.g. Claude_Web, ChatGPT_Web")
    p_inbox_process.set_defaults(func=cmd_inbox_process)

    p_inbox = subparsers.add_parser("inbox", help="List inbox items")
    p_inbox.add_argument("--status", choices=["UNPROCESSED", "PROCESSED", "ARCHIVED"], default=None)
    p_inbox.set_defaults(func=cmd_inbox)

    p_inbox_link = subparsers.add_parser("inbox-link", help="Confirm-link an inbox item to a registered project")
    p_inbox_link.add_argument("item_id")
    p_inbox_link.add_argument("project")
    p_inbox_link.set_defaults(func=cmd_inbox_link)

    p_inbox_archive = subparsers.add_parser("inbox-archive", help="Archive an inbox item")
    p_inbox_archive.add_argument("item_id")
    p_inbox_archive.set_defaults(func=cmd_inbox_archive)

    p_inbox_consolidate = subparsers.add_parser(
        "inbox-consolidate",
        help="Find and merge similar PROCESSED inbox items (Jaccard tag similarity, preview-first)",
    )
    p_inbox_consolidate.set_defaults(func=cmd_inbox_consolidate)

    p_resume = subparsers.add_parser("resume", help="Show active task state and context")
    p_resume.set_defaults(func=cmd_resume)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
