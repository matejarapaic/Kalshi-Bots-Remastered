"""vault skill. Spec: skills/vault/SKILL.md.

The single doorway to the Obsidian vault. TTL cache, tag-filtered queries,
byte-faithful frontmatter round-trips, per-agent write scopes. No other skill
reads vault files directly on a live cycle.
"""
from __future__ import annotations

import fcntl
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import yaml

from kalshi_bots.types import VaultNote, VaultQuery


class VaultError(Exception):
    pass


class VaultNotFound(VaultError):
    pass


class VaultSchemaError(VaultError):
    pass


class VaultScopeError(VaultError):
    pass


class VaultLockTimeout(VaultError):
    pass


DIR_TTLS = {  # seconds; spec Configuration table
    "00-meta": 300.0,
    "02-trading-skills": 300.0,
    "03-market-context": 5.0,
    "04-trade-history": 60.0,
}
DEFAULT_TTL = 60.0
LOCK_TIMEOUT = 2.0

# Write-scope table (spec rule 9) — data, not code.
WRITE_SCOPES: dict[str, dict] = {
    "analyst": {
        "02-trading-skills": {"win_rate", "sample_size", "demo_win_rate", "demo_sample_size"},
        "03-market-context": None,   # None = all fields allowed in this dir
        "04-trade-history": None,
    },
    "trader": {"03-market-context": None, "04-trade-history": None},
    "window-monitor": {"03-market-context": None},
    "orchestrator": {"03-market-context": None},
    "discord": {"03-market-context": None},
    "tuner": {"03-market-context": None},
    "human": "*",
    "admin": "*",
    "system": "*",
}

SKILL_TEMPLATE_FIELDS = {"skill", "families", "signal_types", "market_conditions",
                         "confidence_threshold", "risk_profile", "win_rate",
                         "sample_size", "status", "last_updated"}
VALID_SKILL_STATUS = {"draft", "confirmed", "retired"}
VALID_RISK_PROFILES = {"low", "medium", "high"}


def _split_frontmatter(text: str) -> tuple[dict, str]:
    if text.startswith("---\n"):
        end = text.find("\n---", 4)
        if end != -1:
            fm_text = text[4:end]
            body = text[end + 4:]
            body = body[1:] if body.startswith("\n") else body
            try:
                fm = yaml.safe_load(fm_text) or {}
            except yaml.YAMLError as e:
                raise VaultSchemaError(f"malformed YAML frontmatter: {e}") from e
            if not isinstance(fm, dict):
                raise VaultSchemaError("frontmatter is not a mapping")
            return fm, body
    return {}, text


def _serialize(frontmatter: dict, body: str) -> str:
    if not frontmatter:
        return body
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True,
                        default_flow_style=False)
    return f"---\n{fm}---\n{body}"


class Vault:
    def __init__(self, root: str | None = None):
        self.root = Path(os.path.expanduser(
            root or os.environ.get("KALSHI_VAULT_PATH", "~/vaults/kalshi-vault")))
        if not self.root.is_dir():
            raise VaultError(f"vault root missing: {self.root}")
        self._cache: dict[str, tuple[VaultNote, float, float]] = {}  # path -> (note, mtime, expires)
        self._lock = threading.Lock()
        self.parse_count = 0  # test observability

    # --- internals ---

    def _abs(self, path: str) -> Path:
        p = (self.root / path).resolve()
        if not str(p).startswith(str(self.root.resolve())):
            raise VaultError(f"path escapes vault: {path}")
        return p

    @staticmethod
    def _ttl_for(path: str) -> float:
        top = path.split("/", 1)[0]
        return DIR_TTLS.get(top, DEFAULT_TTL)

    def _parse_file(self, path: str, abs_path: Path) -> VaultNote:
        text = abs_path.read_text(encoding="utf-8")
        fm, body = _split_frontmatter(text)
        self.parse_count += 1
        return VaultNote(path=path, frontmatter=fm, body=body,
                         mtime=datetime.fromtimestamp(abs_path.stat().st_mtime, timezone.utc))

    def _load(self, path: str) -> VaultNote:
        abs_path = self._abs(path)
        now = time.monotonic()
        with self._lock:
            entry = self._cache.get(path)
        try:
            disk_mtime = abs_path.stat().st_mtime
        except FileNotFoundError:
            with self._lock:
                self._cache.pop(path, None)
            raise VaultNotFound(path) from None
        if entry:
            note, mtime, expires = entry
            if now < expires:
                return note
            if disk_mtime == mtime:  # expired but unchanged: refresh TTL, no re-parse
                with self._lock:
                    self._cache[path] = (note, mtime, now + self._ttl_for(path))
                return note
        note = self._parse_file(path, abs_path)
        with self._lock:
            self._cache[path] = (note, disk_mtime, now + self._ttl_for(path))
        return note

    def _atomic_write(self, path: str, content: str):
        abs_path = self._abs(path)
        abs_path.parent.mkdir(parents=True, exist_ok=True)
        lock_target = abs_path.with_suffix(abs_path.suffix + ".lock")
        deadline = time.monotonic() + LOCK_TIMEOUT
        with open(lock_target, "w") as lf:
            while True:
                try:
                    fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() > deadline:
                        self.invalidate(path)
                        raise VaultLockTimeout(path) from None
                    time.sleep(0.05)
            try:
                tmp = abs_path.with_suffix(abs_path.suffix + ".tmp")
                tmp.write_text(content, encoding="utf-8")
                os.rename(tmp, abs_path)
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)
        try:
            lock_target.unlink()
        except OSError:
            pass
        note_fm, note_body = _split_frontmatter(content)
        with self._lock:
            self._cache[path] = (
                VaultNote(path=path, frontmatter=note_fm, body=note_body,
                          mtime=datetime.fromtimestamp(abs_path.stat().st_mtime, timezone.utc)),
                abs_path.stat().st_mtime,
                time.monotonic() + self._ttl_for(path),
            )

    def _validate(self, path: str, frontmatter: dict):
        if path.startswith("02-trading-skills/") and not path.endswith("_skill-template.md"):
            missing = SKILL_TEMPLATE_FIELDS - set(frontmatter)
            if missing:
                raise VaultSchemaError(f"{path}: missing skill fields {sorted(missing)}")
            if frontmatter["status"] not in VALID_SKILL_STATUS:
                raise VaultSchemaError(f"{path}: bad status {frontmatter['status']!r}")
            if frontmatter["risk_profile"] not in VALID_RISK_PROFILES:
                raise VaultSchemaError(f"{path}: bad risk_profile")
            ct = frontmatter["confidence_threshold"]
            if not isinstance(ct, (int, float)) or not 0 <= ct <= 1:
                raise VaultSchemaError(f"{path}: confidence_threshold outside [0,1]")

    @staticmethod
    def _check_scope(path: str, fields: set[str], caller: str):
        scope = WRITE_SCOPES.get(caller)
        if scope is None:
            raise VaultScopeError(f"unknown caller {caller!r}")
        if scope == "*":
            return
        top = path.split("/", 1)[0]
        allowed = scope.get(top, VaultScopeError)
        if allowed is VaultScopeError:
            raise VaultScopeError(f"{caller!r} may not write under {top}/")
        if allowed is not None and not fields <= allowed:
            raise VaultScopeError(
                f"{caller!r} may only update {sorted(allowed)} under {top}/, "
                f"attempted {sorted(fields)}")

    # --- public interface ---

    def read_note(self, path: str) -> VaultNote:
        return self._load(path)

    def query(self, q: VaultQuery) -> list[VaultNote]:
        base = self._abs(q.directory)
        if not base.is_dir():
            return []
        out = []
        for f in sorted(base.rglob("*.md")):
            rel = str(f.relative_to(self.root))
            try:
                note = self._load(rel)
            except VaultSchemaError:
                continue  # skip malformed, never kill a cycle (spec edge case)
            except VaultNotFound:
                continue
            fm = note.frontmatter
            if any(fm.get(k) != v for k, v in q.frontmatter_filters.items()):
                continue
            if q.tag_filters:
                conditions = fm.get("market_conditions") or []
                families = fm.get("families") or []
                pool = set(conditions) | set(families)
                if not any(t in pool for t in q.tag_filters):
                    continue
            out.append(note)
        return out

    def write_note(self, path: str, frontmatter: dict, body: str,
                   caller: str = "system") -> None:
        self._check_scope(path, set(frontmatter), caller)
        try:
            existing = self._load(path)
            dropped = set(existing.frontmatter) - set(frontmatter)
            if dropped:
                raise VaultSchemaError(
                    f"{path}: write would silently drop keys {sorted(dropped)}; "
                    "use update_frontmatter or pass all keys deliberately")
        except VaultNotFound:
            pass
        self._validate(path, frontmatter)
        self._atomic_write(path, _serialize(frontmatter, body))

    def update_frontmatter(self, path: str, updates: dict, caller: str) -> None:
        self._check_scope(path, set(updates), caller)
        note = self._load(path)
        fm = dict(note.frontmatter)
        fm.update(updates)
        self._validate(path, fm)
        self._atomic_write(path, _serialize(fm, note.body))

    def append_section(self, path: str, heading: str, content: str,
                       caller: str = "system") -> None:
        self._check_scope(path, set(), caller)
        try:
            note = self._load(path)
            body = note.body.rstrip("\n") + f"\n\n## {heading}\n\n{content}\n"
            fm = note.frontmatter
        except VaultNotFound:
            body = f"## {heading}\n\n{content}\n"
            fm = {}
        self._atomic_write(path, _serialize(fm, body))

    def invalidate(self, path: str | None = None) -> None:
        with self._lock:
            if path is None:
                self._cache.clear()
            else:
                self._cache.pop(path, None)
