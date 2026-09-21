from jarvis.policy.validator import PolicyValidator, PolicyViolation
from jarvis.state.tracker import StateTracker

try:
    import pyperclip
    _CLIPBOARD_AVAILABLE = True
except Exception:
    # pyperclip can raise at import time on systems with no clipboard
    # mechanism (e.g. a bare Linux box with no xclip/xsel installed).
    # Gemini's version imports it unconditionally at module load, which
    # means the whole CLI fails to start on such a machine, not just the
    # provider adapter. Fall back to manual copy/paste instead of crashing.
    _CLIPBOARD_AVAILABLE = False


class ManualClipboardProvider:
    """
    Dispatches a prompt to a human-driven AI session (ChatGPT/Claude/
    Gemini web or desktop app) with no API call and no cost. This is
    the only provider adapter in Milestone 001.
    """

    def __init__(self, provider_name: str = "AI_Web"):
        self.provider_name = provider_name

    def dispatch_prompt(self, prompt_text: str) -> str:
        try:
            PolicyValidator.authorize_provider(self.provider_name, expects_cost=False)
        except PolicyViolation as e:
            return f"ERROR: Access denied by policy. {e}"

        print("\n" + "=" * 60)
        if _CLIPBOARD_AVAILABLE:
            try:
                pyperclip.copy(prompt_text)
                print(f"Prompt copied to clipboard for {self.provider_name}.")
            except Exception:
                print(f"Clipboard unavailable — prompt for {self.provider_name} below:\n")
                print(prompt_text)
        else:
            print(f"Clipboard support not installed — prompt for {self.provider_name} below:\n")
            print(prompt_text)

        print("\n1. Paste it into the AI's web/desktop interface.")
        print("2. Copy the AI's response.")
        print("3. Press Enter here, then paste the response and press Enter again.")
        print("=" * 60 + "\n")

        input("Press Enter when the response is on your clipboard (or ready to paste manually)... ")

        if _CLIPBOARD_AVAILABLE:
            try:
                return pyperclip.paste()
            except Exception:
                pass

        print("Paste the response below, then press Enter:")
        return input("> ")

    def dispatch_and_track(
        self, tracker: StateTracker, task_id: str, prompt_text: str, *, mark_done: bool = False
    ) -> str:
        """
        Wraps dispatch_prompt() with the task's state transitions, so the
        state machine is never out of sync with what's actually happening:

          IN_PROGRESS/PENDING -> AWAITING_USER   the instant the prompt is
                                                  handed off (clipboard
                                                  populated / printed) —
                                                  not after the fact.
          AWAITING_USER -> IN_PROGRESS or DONE   only once Lone has
                                                  explicitly pasted the
                                                  response back into the
                                                  CLI. mark_done=True is
                                                  the caller's explicit
                                                  choice, never inferred
                                                  from the response text.

        If the response never comes back (process killed while waiting),
        the task is left at AWAITING_USER — exactly the state `jarvis
        resume` should surface, not silently reset to something else.
        """
        tracker.update_task_status(task_id, "AWAITING_USER")
        response = self.dispatch_prompt(prompt_text)
        tracker.update_task_status(task_id, "DONE" if mark_done else "IN_PROGRESS")
        return response
