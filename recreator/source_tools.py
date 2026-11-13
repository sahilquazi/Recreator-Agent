"""
source_tools.py - Read-only tools for the code diagnosis agent.

Every path is resolved and checked to stay inside the source folder, git refs
are validated, and nothing here can write files or reach the network.
"""
import re
import shutil
import subprocess
from pathlib import Path

from .llm import Tool, truncate

_SKIP = {".git", "node_modules", "vendor", "dist", "build", "__pycache__"}
_REF = re.compile(r"^[\w][\w./-]{0,100}$")  # blocks refs like "--output=..." (option injection)


class SourceTools:
    def __init__(self, root: Path):
        self.root = root.resolve()
        if not self.root.is_dir():
            raise FileNotFoundError(f"Source folder not found: {self.root}")

    def _safe(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root):
            raise PermissionError("Path is outside the source folder")
        return path

    def list_dir(self, path: str = ".") -> list[str]:
        folder = self._safe(path)
        entries = sorted(p.name + ("/" if p.is_dir() else "") for p in folder.iterdir() if p.name not in _SKIP)
        return entries[:300]

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        lines = self._safe(path).read_text(errors="replace").splitlines()
        start = max(start, 1)
        end = min(end or start + 199, start + 399, len(lines))  # at most 400 lines per read
        body = "\n".join(f"{n:>6}  {lines[n - 1]}" for n in range(start, end + 1))
        return f"{path} (lines {start}-{end} of {len(lines)})\n{body}"

    def search_code(self, pattern: str, path_glob: str | None = None, max_results: int = 60) -> str:
        """Regex search. Uses ripgrep when installed (fast), plain Python otherwise."""
        if shutil.which("rg"):
            cmd = ["rg", "-n", "--no-heading", "--max-count", "5", "-e", pattern]
            if path_glob:
                cmd += ["-g", path_glob]
            out = subprocess.run(cmd + ["--", str(self.root)], capture_output=True, text=True, timeout=60).stdout
            hits = [l.replace(str(self.root) + "/", "") for l in out.splitlines()]
        else:
            rx, hits = re.compile(pattern), []
            for f in self.root.rglob(path_glob or "*"):
                if not f.is_file() or _SKIP & set(f.parts) or f.stat().st_size > 2_000_000:
                    continue
                for n, line in enumerate(f.read_text(errors="ignore").splitlines(), 1):
                    if rx.search(line):
                        hits.append(f"{f.relative_to(self.root)}:{n}:{line.strip()}")
                if len(hits) >= max_results:
                    break
        return truncate("\n".join(hits[:max_results]) or "No matches", 12_000)

    def _git(self, *args: str) -> str:
        if not (self.root / ".git").exists():
            return "No git history in this source folder."
        done = subprocess.run(["git", "-C", str(self.root), *args], capture_output=True, text=True, timeout=60)
        return truncate(done.stdout or done.stderr, 15_000)

    def changed_files(self, from_ref: str, to_ref: str = "HEAD") -> str:
        """Files changed between the last good version and the current one."""
        if not (_REF.match(from_ref) and _REF.match(to_ref)):
            raise ValueError("Invalid git ref")
        return self._git("diff", "--stat", f"{from_ref}..{to_ref}")

    def diff_file(self, from_ref: str, path: str, to_ref: str = "HEAD") -> str:
        if not (_REF.match(from_ref) and _REF.match(to_ref)):
            raise ValueError("Invalid git ref")
        self._safe(path)
        return self._git("diff", f"{from_ref}..{to_ref}", "--", path)

    def tools(self) -> list[Tool]:
        obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
        s, i = {"type": "string"}, {"type": "integer"}
        return [
            Tool("list_dir", "List a folder in the source tree.", obj({"path": s}), self.list_dir),
            Tool("read_file", "Read lines of a source file (max 400 per call).",
                 obj({"path": s, "start": i, "end": i}, ["path"]), self.read_file),
            Tool("search_code", "Regex search across the source, optionally limited by a glob like '*.go'.",
                 obj({"pattern": s, "path_glob": s, "max_results": i}, ["pattern"]), self.search_code),
            Tool("changed_files", "Files changed between two git refs (e.g. last good tag and HEAD).",
                 obj({"from_ref": s, "to_ref": s}, ["from_ref"]), self.changed_files),
            Tool("diff_file", "The git diff of one file between two refs.",
                 obj({"from_ref": s, "path": s, "to_ref": s}, ["from_ref", "path"]), self.diff_file),
        ]
