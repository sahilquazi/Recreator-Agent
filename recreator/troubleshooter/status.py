"""
status.py - What the admin sees. Plain language, never "the agent failed".

Per Bala's 10/23 feedback, a handoff reads as a person stepping in. These
messages carry no code paths or internal details, only what was checked.
(This may move to Kate's tracker; it's kept separate so it's easy to lift out.)
"""
from .models import CaseFile


def _checked(cf: CaseFile, reproduced: bool) -> list[str]:
    lines = []
    if reproduced:
        lines.append("We reproduced the problem in a test environment.")
    ruled_out = sum(c.status == "ruled_out" for c in cf.causes)
    confirmed = sum(c.status == "confirmed" for c in cf.causes)
    if ruled_out:
        lines.append(f"We ruled out {ruled_out} possible cause{'s' * (ruled_out != 1)}.")
    if confirmed:
        lines.append("We narrowed down where the problem comes from.")
    if cf.fixes_tried:
        n = len(cf.fixes_tried)
        lines.append(f"We tested {n} possible fix{'es' * (n != 1)}.")
    return lines or ["We reviewed the details you sent."]


def investigating(cf: CaseFile, reproduced: bool) -> str:
    body = "\n".join(f"- {line}" for line in _checked(cf, reproduced))
    return f"We're still investigating. Here's what we've checked so far:\n{body}"


def waiting_on_admin(questions: list[str]) -> str:
    body = "\n".join(f"{i}. {q}" for i, q in enumerate(questions, 1))
    return f"We need a few details to reproduce this:\n{body}"


def handed_off(cf: CaseFile, reproduced: bool) -> str:
    body = "\n".join(f"- {line}" for line in _checked(cf, reproduced))
    return f"An engineer has your case. Here's what's already been checked, so you won't be asked again:\n{body}"


def fixed() -> str:
    return ("We found the cause and a fix that passes our checks. "
            "An engineer will review it before it's released.")
