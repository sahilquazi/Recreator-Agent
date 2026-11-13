"""
config.py - Every setting comes from environment variables (see .env.example).
Nothing secret is hard-coded, so the code can be shared without leaking keys.
"""
import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def _required(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"Missing {name}. Copy .env.example to .env and fill it in.")
    return value


def _optional(name: str) -> str | None:
    value = os.getenv(name, "").strip()
    return value or None


@dataclass(frozen=True)
class Settings:
    supabase_url: str
    supabase_key: str
    tickets_table: str
    runs_table: str
    model: str
    runs_dir: Path
    mm_url: str
    mm_compose_file: Path
    mm_image_template: str | None
    mm_image: str | None
    mm_last_good_image: str | None
    mm_source_dir: Path | None
    max_recreator_turns: int
    max_diagnosis_turns: int


def load_settings() -> Settings:
    _required("ANTHROPIC_API_KEY")  # the Anthropic client reads it itself; we just check it's there
    project_root = Path(__file__).resolve().parent.parent
    source = _optional("MM_SOURCE_DIR")
    return Settings(
        supabase_url=_required("SUPABASE_URL"),
        supabase_key=_required("SUPABASE_SERVICE_KEY"),
        tickets_table=os.getenv("TICKETS_TABLE", "ReportedIssues"),
        runs_table=os.getenv("RUNS_TABLE", "recreator_runs"),
        model=os.getenv("RECREATOR_MODEL", "claude-opus-5-5"),
        runs_dir=project_root / "runs",
        mm_url=os.getenv("MM_URL", "http://localhost:8065").rstrip("/"),
        mm_compose_file=project_root / "docker-compose.mattermost.yml",
        mm_image_template=_optional("MM_IMAGE_TEMPLATE"),
        mm_image=_optional("MM_IMAGE"),
        mm_last_good_image=_optional("MM_LAST_GOOD_IMAGE"),
        mm_source_dir=Path(source).resolve() if source else None,
        max_recreator_turns=int(os.getenv("MAX_RECREATOR_TURNS", "40")),
        max_diagnosis_turns=int(os.getenv("MAX_DIAGNOSIS_TURNS", "25")),
    )
