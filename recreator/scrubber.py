"""
scrubber.py - Privacy layer. Runs BEFORE anything is sent to Claude.

What it does:
  * Swaps personal details for consistent placeholders, so relationships
    survive: alice@contoso.com becomes user_1@example.test everywhere.
  * Replaces correlation/session IDs, tokens, URLs and IP addresses.
  * Keeps a private mapping (real -> placeholder) that never leaves this
    process. It is used at the end to check no real value leaked into the
    results.

Limits, honestly stated: regexes catch emails, IDs and secrets well, but not
plain first names like "Bob". If presidio-analyzer is installed it is used to
catch names too. Install it before real customer tickets go through.
"""
import re
from typing import Any

try:  # optional, better name detection
    from presidio_analyzer import AnalyzerEngine
    _ANALYZER = AnalyzerEngine()
except Exception:  # noqa: BLE001 - not installed or model missing: fall back to regex only
    _ANALYZER = None

# (pattern, placeholder kind). Order matters: emails before @mentions.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"(?i)\bbearer\s+[\w.\-~+/]+=*"), "secret"),
    (re.compile(r"(?i)\b(?:token|api[_-]?key|secret|password|pwd)\s*[:=]\s*\S+"), "secret"),
    (re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "email"),
    (re.compile(r"https?://[^\s\"')]+"), "url"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "guid"),
    (re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b"), "ip"),
    (re.compile(r"(?<![\w@])@[a-zA-Z][\w.-]{1,63}"), "mention"),
    # Phone numbers like (425) 555-0100 or +1 425-555-0100. Strict on purpose so dates
    # and version numbers (2026-11-12, 16.0.123) are NOT mistaken for phones.
    (re.compile(r"(?:\+\d{1,3}[\s-]?)?(?:\(\d{3}\)\s?|\b\d{3}[\s.-])\d{3}[\s.-]\d{4}\b"), "phone"),
]

# Fields whose list items are identifiers and should be swapped wholesale.
_ID_FIELDS = {"correlation_ids": "corr", "session_ids": "session"}


class Scrubber:
    def __init__(self, known_names: list[str] | None = None):
        self._map: dict[str, str] = {}        # real value -> placeholder (private!)
        self._counts: dict[str, int] = {}
        self._known_names = [n for n in (known_names or []) if len(n) >= 2]

    # -- placeholders ------------------------------------------------------
    def _placeholder(self, real: str, kind: str) -> str:
        if real not in self._map:
            self._counts[kind] = self._counts.get(kind, 0) + 1
            n = self._counts[kind]
            self._map[real] = {
                "email": f"user_{n}@example.test",
                "mention": f"@user_{n}",
                "url": f"https://redacted.example/{n}",
                "secret": "[REDACTED]",
            }.get(kind, f"{kind}_{n}")
        return self._map[real]

    # -- text --------------------------------------------------------------
    def scrub_text(self, text: str) -> str:
        if not text:
            return text
        for pattern, kind in _PATTERNS:
            text = pattern.sub(lambda m, k=kind: self._placeholder(m.group(0), k), text)
        for name in self._known_names:
            text = re.sub(rf"(?i)\b{re.escape(name)}\b", lambda m: self._placeholder(name, "person"), text)
        if _ANALYZER:
            # Replace from the end so earlier character offsets stay valid.
            for hit in sorted(_ANALYZER.analyze(text=text, entities=["PERSON"], language="en"),
                              key=lambda r: r.start, reverse=True):
                real = text[hit.start:hit.end]
                if real.startswith(("user_", "person_", "corr_", "session_")):
                    continue  # already a placeholder
                text = text[:hit.start] + self._placeholder(real, "person") + text[hit.end:]
        return text

    # -- whole objects -------------------------------------------------------
    def scrub(self, value: Any, key: str | None = None) -> Any:
        if key in _ID_FIELDS and isinstance(value, list):
            return [self._placeholder(str(v), _ID_FIELDS[key]) for v in value]
        if isinstance(value, dict):
            return {k: self.scrub(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.scrub(v, key) for v in value]
        if isinstance(value, str):
            return self.scrub_text(value)
        return value

    # -- the final privacy gate ----------------------------------------------
    def leaked(self, text: str) -> list[str]:
        """Return any real value that appears in text. Should always be empty."""
        lowered = text.lower()
        return [real for real in self._map if len(real) >= 3 and real.lower() in lowered]
