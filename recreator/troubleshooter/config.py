"""
config.py - Troubleshooter settings, all from environment variables (see .env.example).
Kept separate from recreator/config.py so the original settings don't change.
"""
import os
from dataclasses import dataclass, field

# Placeholder budgets until Bala confirms (open question in the spec).
# P0/P1 don't need one: they go to an engineer immediately.
DEFAULT_BUDGET_MINUTES = {"P2": 60.0, "P3": 120.0}


def _float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except ValueError:
        return default


@dataclass(frozen=True)
class TroubleshootSettings:
    enabled: bool = True
    verify_cmd: str | None = None              # see verifier.CommandVerifier
    verify_timeout_s: int = 3600
    approver: str = "terminal"                 # terminal | deny | approve (approve is for demos only)
    catalog_path: str | None = None            # JSON list of resources that may be requested
    budget_minutes: dict = field(default_factory=lambda: dict(DEFAULT_BUDGET_MINUTES))
    grant_ttl_minutes: float = 30.0
    max_step_turns: int = 20                   # Claude turns per troubleshooting step

    @classmethod
    def from_env(cls) -> "TroubleshootSettings":
        return cls(
            enabled=os.getenv("TROUBLESHOOT_ENABLED", "true").strip().lower() not in {"0", "false", "no"},
            verify_cmd=os.getenv("TROUBLESHOOT_VERIFY_CMD", "").strip() or None,
            verify_timeout_s=int(_float("TROUBLESHOOT_VERIFY_TIMEOUT_S", 3600)),
            approver=os.getenv("TROUBLESHOOT_APPROVER", "terminal").strip().lower() or "terminal",
            catalog_path=os.getenv("TROUBLESHOOT_ACCESS_CATALOG", "").strip() or None,
            budget_minutes={"P2": _float("TROUBLESHOOT_BUDGET_P2_MIN", DEFAULT_BUDGET_MINUTES["P2"]),
                            "P3": _float("TROUBLESHOOT_BUDGET_P3_MIN", DEFAULT_BUDGET_MINUTES["P3"])},
            grant_ttl_minutes=_float("TROUBLESHOOT_GRANT_TTL_MIN", 30.0),
            max_step_turns=int(_float("TROUBLESHOOT_MAX_STEP_TURNS", 20)),
        )
