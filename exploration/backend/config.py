"""Paths, environment, and hard investigation caps."""

from __future__ import annotations

import json
import os
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPLORATION_ROOT = Path(__file__).resolve().parents[1]

load_dotenv(REPO_ROOT / ".env")

HARD_MAX_SEARCHES = 2
HARD_MAX_FETCHED_URLS = 5
HARD_MAX_FETCH_BATCH = 5
HARD_MAX_LLM_CALLS = 10
DEFAULT_PROVIDERS = ("brave", "tavily")
DEFAULT_TIER = "default"
RESULTS_PER_PROVIDER = 3
HTTP_TIMEOUT_SECONDS = 30.0
SEARCH_RETRIES = 2
DEFAULT_TELEM_BASE_URL = "https://router.telem.ai"
DEFAULT_CONVERGENCE_URL = "http://127.0.0.1:8000/api/convergence/evaluate"
DEFAULT_LLM_BASE_URL = "https://api.openai.com/v1"
DEFAULT_LLM_MODEL = "gpt-4o-mini"


def billable_allowed() -> bool:
    """Server-side switch for routes that spend provider money (search, fetch, LLM).

    SEARCHHAX_ALLOW_BILLABLE: "false"/"0"/"no"/"off" refuses them with 403. Unset keeps
    them available (local development); docker-compose sets it to false by default.
    """
    value = os.environ.get("SEARCHHAX_ALLOW_BILLABLE", "").strip().lower()
    return value not in {"false", "0", "no", "off"}


def default_db_path() -> Path:
    """SQLite file for investigations. SEARCHHAX_DB_PATH overrides it (e.g. a mounted volume)."""
    override = os.environ.get("SEARCHHAX_DB_PATH", "").strip()
    if override:
        return Path(override)
    return EXPLORATION_ROOT / "data" / "exploration.sqlite"


def default_cache_dir() -> Path:
    return EXPLORATION_ROOT / "cache"


def story_c_dir() -> Path:
    return EXPLORATION_ROOT / "fixtures" / "story_c"


def profile_fixture_dir() -> Path:
    return EXPLORATION_ROOT / "fixtures" / "profiles"


def replay_fixture_path() -> Path:
    return EXPLORATION_ROOT / "fixtures" / "replay" / "synthetic_search.json"


def env_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def telem_base_url() -> str:
    return os.environ.get("TELEM_BASE_URL", "").strip() or DEFAULT_TELEM_BASE_URL


def convergence_url() -> str:
    return os.environ.get("CONVERGENCE_URL", "").strip() or DEFAULT_CONVERGENCE_URL


def llm_api_key() -> str | None:
    key = os.environ.get("LLM_API_KEY", "").strip() or os.environ.get("OPENAI_API_KEY", "").strip()
    return key or None


def llm_base_url() -> str:
    return os.environ.get("LLM_BASE_URL", "").strip() or DEFAULT_LLM_BASE_URL


def llm_model() -> str:
    return os.environ.get("LLM_MODEL", "").strip() or DEFAULT_LLM_MODEL


def tavily_api_key() -> str | None:
    key = os.environ.get("TAVILY_API_KEY", "").strip()
    return key or None


def _credential_file_has_key(data: object) -> bool:
    if not isinstance(data, dict):
        return False
    for key, value in data.items():
        if str(key).lower() in {"api_key", "apikey", "key", "token"} and isinstance(value, str) and value.strip():
            return True
        if isinstance(value, dict) and _credential_file_has_key(value):
            return True
    return False


def telem_configured() -> bool:
    if os.environ.get("TELEM_API_KEY", "").strip():
        return True
    path = Path.home() / ".telem" / "credentials.json"
    if not path.exists():
        return False
    try:
        return _credential_file_has_key(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return False


def missing_credential_report() -> dict[str, str]:
    """Names only. Never include secret values."""
    names = [
        "TELEM_API_KEY",
        "TELEM_BASE_URL",
        "TAVILY_API_KEY",
        "LLM_API_KEY",
        "LLM_BASE_URL",
        "LLM_MODEL",
        "OPENAI_API_KEY",
        "CONVERGENCE_URL",
    ]
    return {name: "set" if os.environ.get(name, "").strip() else "missing" for name in names}
