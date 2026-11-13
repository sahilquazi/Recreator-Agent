"""base.py - The contract every app adapter follows."""
from abc import ABC, abstractmethod

from ..llm import Tool
from ..schemas import Ticket


class AppAdapter(ABC):
    #: Shown to the recreator agent: how ticket fields map onto this app.
    environment_notes: str = ""

    @classmethod
    @abstractmethod
    def handles(cls, app_name: str) -> bool:
        """True if this adapter can reproduce bugs in the named app."""

    @abstractmethod
    def images_for(self, ticket: Ticket) -> tuple[str, str | None]:
        """(build to reproduce on, last known good build or None)."""

    @abstractmethod
    def reset(self, build: str) -> None:
        """Wipe the sandbox and start a fresh one on the given build."""

    @abstractmethod
    def tools(self) -> list[Tool]:
        """The tools the recreator agent may use against the sandbox."""

    @abstractmethod
    def run_repro_script(self, code: str) -> tuple[int, str]:
        """Run the agent's repro script against the sandbox: (exit code, output)."""

    @abstractmethod
    def close(self) -> None:
        """Release browsers and other resources (the sandbox itself is wiped on reset)."""
