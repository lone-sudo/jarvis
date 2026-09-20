import argparse

from jarvis.state.tracker import StateTracker
from jarvis.tools.base import run_logged
from jarvis.tools.git import GitInspector
from jarvis.tools.fs import FileSystemInspector
from jarvis.memory.markdown import ProjectMemory
from jarvis.providers.base import ManualClipboardProvider


def cmd_init_project(args):
    tracker = StateTracker()
    tracker.register_project(args.key, args.path, args.name)
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
    ProjectMemory.append_note(project_root, args.text)
    print(f"Note added to {args.project}.")


def cmd_write_file(args):
    tracker = StateTracker()
    content = args.content if args.content is not None else open(args.content_file, encoding="utf-8").read()
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

    p_resume = subparsers.add_parser("resume", help="Show active task state and context")
    p_resume.set_defaults(func=cmd_resume)

    args = parser.parse_args()
    if hasattr(args, "func"):
        args.func(args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
