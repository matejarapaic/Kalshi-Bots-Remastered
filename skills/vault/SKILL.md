# vault

**Trigger:** any skill or agent needs to read or write an Obsidian vault note (skill library, market context, trade history, config).

## What this is for

The single doorway to the Obsidian vault at `~/vaults/kalshi-vault/`. It reads and writes frontmatter-bearing markdown, answers tag/frontmatter-filtered queries, and serves everything through an in-memory TTL cache so that live trading cycles never touch disk. **The rule this skill exists to enforce: no other skill may read vault files directly on a live trading cycle — everything goes through this cache.** (Batch contexts — postmortem, backtests — may read directly but must route *writes* through this skill so the cache never goes stale.)

## Interface

```python
Vault(root: str | None = None)                          # raises VaultError if root missing
read_note(path: str) -> VaultNote                       # raises VaultNotFound
query(q: VaultQuery) -> list[VaultNote]
write_note(path: str, frontmatter: dict, body: str, caller: str = "system") -> None
                                                        # raises VaultScopeError, VaultSchemaError
update_frontmatter(path: str, updates: dict, caller: str) -> None
                                                        # raises VaultScopeError, VaultSchemaError
append_section(path: str, heading: str, content: str, caller: str = "system") -> None
invalidate(path: str | None = None) -> None             # None = whole cache
```

Paths are vault-relative (`"02-trading-skills/opening-drift.md"`). Vault root from `KALSHI_VAULT_PATH` (default `~/vaults/kalshi-vault`). Live window state lives under `03-market-context/active-windows/`; trade notes under `04-trade-history/`.

Exceptions: `VaultError` (base), `VaultNotFound`, `VaultSchemaError`, `VaultScopeError`, `VaultLockTimeout`.

## Behavior

### Query model — tag-filtered, never full-text
1. `query(VaultQuery)` scans (from cache) `.md` notes recursively under `directory` (sorted path order), keeping those where every `frontmatter_filters` key matches exactly (`{"status": "confirmed"}`) and — when `tag_filters` is non-empty — at least one entry is a member of the note's tag pool: the union of the list-valued `market_conditions` and `families` frontmatter fields (so `"KXBTC15M"` matches via `families`, `"live"` via `market_conditions`). Frontmatter filters are AND; tag filters are OR. There is deliberately no full-text search — a trading cycle must never grep bodies.
2. Canonical call (skill-matcher's): `query(VaultQuery(directory="02-trading-skills", frontmatter_filters={"status": "confirmed"}))`.

### Cache
3. In-memory, per-top-level-directory TTLs (Configuration table). Entry = parsed `VaultNote` + file `mtime` + expiry. Every read stats the file (so a deletion surfaces immediately); within TTL the cached note is served without a content check. On expiry: if `mtime` unchanged, refresh TTL without re-parsing; changed → re-parse.
4. Writes are **write-through**: serialize → atomic disk write (write to `path + ".tmp"`, `os.rename`) → update cache entry in the same operation. A write failure leaves cache untouched and raises; a lock timeout additionally invalidates the entry.
5. `03-market-context/` gets a 5s TTL because window-monitor rewrites `active-windows/` notes continuously and trader must see fresh signals; `02-trading-skills/` gets 300s because skill notes change rarely (analyst stats or human edits) and skill-matcher hits it every cycle.

### Frontmatter handling
6. Parse YAML frontmatter (`---` fences); body preserved **byte-for-byte** — round-tripping a note through read→write must be a no-op on the body, including trailing whitespace.
7. `write_note` refuses to silently drop keys: if the target exists and `frontmatter` is missing keys present on disk, raise `VaultSchemaError` (callers must read-modify-write deliberately; `update_frontmatter` is the field-scoped alternative).
8. Schema validation: notes written under `02-trading-skills/` (except `_skill-template.md` itself) are validated against the template field set — all of `skill`, `families`, `signal_types`, `market_conditions`, `confidence_threshold`, `risk_profile`, `win_rate`, `sample_size`, `status`, `last_updated` present; `risk_profile ∈ {low,medium,high}`; `confidence_threshold` numeric in `[0,1]`; `status ∈ {draft,confirmed,retired}`. Validation runs on `write_note` and on the merged result of `update_frontmatter`. Other directories: no validation.
9. Path safety: every path resolves against the vault root; a path that escapes it raises `VaultError`.

### Write scopes (enforced here)
10. All three write methods check `caller` against the scope table (`write_note`/`append_section` default to `"system"`; `append_section` checks directory access only, no field restriction). Caller `"analyst"` may update **only** `win_rate`, `sample_size` (and env-labeled variants `demo_win_rate`, `demo_sample_size`) on `02-trading-skills/*`, but is field-unrestricted under `03-market-context/` and `04-trade-history/`; caller `"trader"` may write only under `03-market-context/` and `04-trade-history/` — never `02-trading-skills/` or `00-meta/`; callers `"window-monitor"`, `"orchestrator"`, `"discord"`, `"tuner"` may write only under `03-market-context/`; `"human"`/`"admin"`/`"system"` are unrestricted. Unknown callers raise `VaultScopeError`, as do all violations. The scope table is data (Configuration), not code.

### Concurrency
11. Single-process assumption documented; agents run as tasks in one process. For cross-process safety (dashboard, manual scripts), writes take an advisory `fcntl` lock on a sidecar `<path>.lock` file (non-blocking, retried every 50ms), timeout 2s → `VaultLockTimeout`.
12. Stale-cache handling: `VaultLockTimeout` triggers `invalidate(path)`; a note deleted on disk drops its cache entry on the read that discovers it; an external `mtime` change is picked up at TTL expiry.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `KALSHI_VAULT_PATH` | `~/vaults/kalshi-vault` | env var |
| `TTL_00_META` | 300s | meta/config notes change rarely — CONFIRMED 2026-07-17 |
| `TTL_02_SKILLS` | 300s | CONFIRMED 2026-07-17 |
| `TTL_03_CONTEXT` | 5s | live signal path (`active-windows/`) — CONFIRMED 2026-07-17 |
| `TTL_04_HISTORY` | 60s | read path; writes always write-through — CONFIRMED 2026-07-17 |
| `DEFAULT_TTL` | 60s | unlisted directories — PROPOSED 2026-07-22 (crypto pivot default, pending owner confirmation) |
| `LOCK_TIMEOUT` | 2s | CONFIRMED 2026-07-17 |
| write-scope table | see rule 10 | data, editable without code change |

## Edge cases
- **Missing frontmatter** (no `---` fence): `VaultNote.frontmatter = {}`, body = whole file; writing back adds fences only if frontmatter non-empty.
- **Malformed YAML** (or frontmatter that isn't a mapping): raise `VaultSchemaError` naming the path — never return a half-parsed note; queries *skip* malformed notes (one bad note must not kill a cycle).
- **`.gitkeep` and non-`.md` files:** invisible to `query` (it globs `*.md` only); `read_note` does not filter by extension — callers pass note paths.
- **Note deleted on disk but cached:** re-stat fails → drop entry, raise `VaultNotFound` on the read that discovered it.
- **`append_section` on a missing note:** creates it with empty frontmatter; content lands as `## <heading>` after the trimmed existing body.
- **Concurrent `append_section` calls to one note:** serialized by the file lock; append order is lock-acquisition order (acceptable — signal log entries carry their own timestamps).
- **Vault root missing:** raise `VaultError` at construction, not mid-cycle.
- **`query` on a directory that doesn't exist:** returns `[]`, never raises.

## Dependencies
None (foundation skill). Called by: every other skill and agent; window-monitor writes active-window state under `03-market-context/active-windows/`; skill-matcher queries `02-trading-skills/`; risk-management persists its exposure ledger; discord-bot persists halt state; trader writes trade notes under `04-trade-history/`; postmortem updates skill-note stats.

## Testing requirements
- Round-trip byte-fidelity: read→write leaves the body bit-identical (fixture with odd spacing, unicode, trailing newline variations).
- Query filters: fixtures for status filtering, tag-pool membership across both `market_conditions` and `families` (including a note matched via `families` alone), combined filters, malformed-note skipping.
- Write-through atomicity: kill-between-tmp-and-rename simulation leaves the original intact.
- Scope enforcement: analyst updating `win_rate` passes; analyst updating `confidence_threshold` raises `VaultScopeError`; trader writing to `02-trading-skills/` raises; window-monitor writing outside `03-market-context/` raises; unknown caller raises.
- TTL/mtime: expired-but-unchanged refreshes without re-parse (assert via parse counter); external modification detected at expiry; deletion detected immediately.
- Schema validation: skill note missing `families` or `signal_types` rejected; `status: bogus` rejected; `confidence_threshold` outside `[0,1]` rejected; `_skill-template.md` itself exempt.
- Path escape (`../`) rejected with `VaultError`.

## New types
None beyond CONTRACTS.md (`VaultNote`, `VaultQuery`).
