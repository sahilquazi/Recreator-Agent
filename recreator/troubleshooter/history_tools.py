"""
history_tools.py - Agent B's read-only source tools, plus commit history.

The self-recovery pass needs "recent commits that touched the affected code".
SourceTools already has changed_files/diff_file between two refs; this adds
`recent_commits` for one path, with the same safety checks (paths stay inside
the source folder, refs are validated, nothing writes or reaches the network).
"""
from ..llm import Tool
from ..source_tools import _REF, SourceTools


class HistoryTools(SourceTools):
    def recent_commits(self, path: str = ".", max_count: int = 15, since_ref: str | None = None) -> str:
        self._safe(path)
        max_count = max(1, min(int(max_count), 50))
        args = ["log", f"--max-count={max_count}", "--date=short", "--format=%h %ad %s"]
        if since_ref:
            if not _REF.match(since_ref):
                raise ValueError("Invalid git ref")
            args.append(f"{since_ref}..HEAD")
        return self._git(*args, "--", path)

    def tools(self) -> list[Tool]:
        obj = {"type": "object", "properties": {"path": {"type": "string"}, "max_count": {"type": "integer"},
                                                 "since_ref": {"type": "string"}}, "required": []}
        return super().tools() + [
            Tool("recent_commits", "Recent commits that touched a file or folder (newest first). "
                 "Optionally only commits after since_ref (e.g. the last good version tag).",
                 obj, self.recent_commits),
        ]
