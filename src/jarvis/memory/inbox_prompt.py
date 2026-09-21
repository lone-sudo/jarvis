"""
V2: the classification prompt template.

This is dispatched through the existing ManualClipboardProvider — same
mechanism as `ask-ai` already uses. Classification is NOT a silent
background call to an API; it's a human-in-the-loop step where Lone
pastes this prompt into whichever AI (ChatGPT/Claude/Gemini) he's
using, and pastes the JSON response back. No provider gets tool access
during this step — it only ever sees the prompt text below and returns
JSON, nothing else.
"""

CLASSIFICATION_PROMPT_TEMPLATE = """You are classifying a single saved item for a personal knowledge inbox. \
Respond with ONLY a JSON object — no markdown fences, no explanation, no conversational text before or after it. \
If you are uncertain about any field, still provide your best guess and reflect that in "confidence".

Known project keys you may match against (use null if none genuinely fit): {known_project_keys}

Item to classify:
---
Title: {title}
Source URL: {source_url}
User's note (why they saved it, if given): {note}
Captured content:
{content}
---

Return exactly this JSON shape:
{{
  "summary": "<3 bullet points max, as a single string with \\n between them>",
  "tags": ["<lowercase-topic-tag>", "..."],
  "actionable": <true or false — true only if this clearly names a concrete next step, not just "this is interesting">,
  "related_project": "<one of the known project keys above, or null>",
  "confidence": <float 0.0 to 1.0 — your honest confidence in related_project and actionable, not in the summary>
}}

Do not suggest creating a task, do not suggest any action beyond this classification. \
This classification is advisory only — a human decides what happens next."""


def build_classification_prompt(title: str, source_url: str, note: str, content: str, known_project_keys: list[str]) -> str:
    return CLASSIFICATION_PROMPT_TEMPLATE.format(
        title=title or "(no title)",
        source_url=source_url or "(none — raw note)",
        note=note or "(none given)",
        content=content,
        known_project_keys=", ".join(known_project_keys) if known_project_keys else "(none registered yet)",
    )
