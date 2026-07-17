"""Loads .env (project root, gitignored) into the process environment.

Existing env vars always win (override=False) — a value already exported in
the user's shell profile is never clobbered by the file. Silently a no-op if
python-dotenv isn't installed or no .env exists; env vars can still be set the
old-fashioned way.
"""
from __future__ import annotations

from pathlib import Path


def load_env() -> None:
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    env_path = Path(__file__).resolve().parent.parent / ".env"
    if env_path.exists():
        load_dotenv(env_path, override=False)
