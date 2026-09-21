from pathlib import Path

from jarvis.policy.rules import SecurityPolicy

INBOX_SUBDIR = "inbox"


class InboxMarkdown:
    @staticmethod
    def _path_for(item_id: str) -> Path:
        return SecurityPolicy.resolve_safe_path(f"{INBOX_SUBDIR}/{item_id}.md")

    @staticmethod
    def relative_path_for(item_id: str) -> str:
        return f"{INBOX_SUBDIR}/{item_id}.md"

    @staticmethod
    def write_capture(item_id: str, title: str, source_url: str, note: str, content: str) -> None:
        """
        Called once, at save time. This is the only place the raw
        capture is written — write_classification below never touches
        this section again, only appends above it.
        """
        path = InboxMarkdown._path_for(item_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        body = (
            f"---\n"
            f"id: {item_id}\n"
            f"title: {title or '(no title)'}\n"
            f"source: {source_url or '(none)'}\n"
            f"status: UNPROCESSED\n"
            f"---\n\n"
            f"## Original Capture\n\n"
            f"{content}\n"
        )
        if note:
            body += f"\n## User's Note\n\n{note}\n"
        path.write_text(body, encoding="utf-8")

    @staticmethod
    def read_content(item_id: str) -> str:
        """
        Returns just the original-capture text (for feeding to the
        classification prompt) — not the whole file with frontmatter.
        """
        path = InboxMarkdown._path_for(item_id)
        if not path.exists():
            return ""
        text = path.read_text(encoding="utf-8")
        marker = "## Original Capture\n\n"
        if marker in text:
            after = text.split(marker, 1)[1]
            # Stop at the next section if a note follows.
            return after.split("\n## User's Note", 1)[0].strip()
        return text.strip()

    @staticmethod
    def write_classification(item_id: str, summary: str, tags: list[str]) -> None:
        """
        Rewrites the file to add an AI Summary section, WITHOUT touching
        the Original Capture text — per both reviews' "reversible
        classification" principle. The raw capture stays byte-identical;
        only a new section is added above it.
        """
        path = InboxMarkdown._path_for(item_id)
        if not path.exists():
            return
        text = path.read_text(encoding="utf-8")

        frontmatter, _, rest = text.partition("---\n\n")
        # frontmatter here includes the closing "---" from the opening
        # block up through the blank line; reconstruct with tags added.
        tag_line = f"tags: {tags}\n"
        if "tags:" not in frontmatter:
            frontmatter = frontmatter.rstrip("\n") + f"\n{tag_line}"

        summary_section = f"## AI Summary\n\n{summary}\n\n"
        new_text = frontmatter + "---\n\n" + summary_section + rest
        path.write_text(new_text, encoding="utf-8")
