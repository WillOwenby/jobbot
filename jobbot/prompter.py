"""Terminal prompts for review mode. In auto mode every prompt answers itself."""
from __future__ import annotations

from .models import Job


class Prompter:
    def __init__(self, mode: str):
        self.interactive = mode == "review"

    def confirm_submit(self, job: Job) -> bool:
        if not self.interactive:
            return True
        reply = input(f"\n>>> Submit application for '{job.title}' at {job.company}? "
                      "Check the browser first. [y/N] ")
        return reply.strip().lower() in ("y", "yes")

    def resolve_manually(self, job: Job, questions: list[str]) -> bool:
        """Ask the user to answer questions in the browser. False means skip the job."""
        if not self.interactive:
            return False
        print(f"\n>>> '{job.title}' at {job.company} needs answers jobbot doesn't have:")
        for q in questions:
            print(f"      - {q}")
        print("    Tip: add rules for these under `answers:` in config.yaml for next time.")
        reply = input(">>> Fill them in the browser and press Enter to continue, or type 's' to skip: ")
        return reply.strip().lower() != "s"

    def wait_for_login(self, site: str) -> None:
        input(f"\n>>> Log in to {site} in the browser window, then press Enter here... ")
