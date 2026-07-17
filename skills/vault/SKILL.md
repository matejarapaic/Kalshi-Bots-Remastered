# vault

**Trigger:** any skill or agent needs to read or write an Obsidian vault note (skill library, market context, trade history, config).

## What this is for

The single doorway to the Obsidian vault at `~/vaults/kalshi-vault/`. It reads and writes frontmatter-bearing markdown, answers tag/frontmatter-filtered queries, and serves everything through an in-memory TTL cache so that live trading cycles never touch disk. **The rule this skill exists to enforce: no other skill may read vault files directly on a live trading cycle — everything goes through this cache.** (Batch contexts — postmortem, backtests — may read directly but must route *writes* through this skill so the cache never goes stale.)

## Interface

```python
read_note(path: str) -> VaultNote                       # raises VaultNotFound
query(q: VaultQuery) -> list[VaultNote]
write_note(path: str, frontmatter: dict, body: str) -> None       # raises VaultSchemaError
update_frontmatter(path: str, updates: dict, caller: str) -> None # raises VaultScopeError, VaultSchemaError
append_section(path: str, heading: str, content: str) -> None     # e.g. signal log entries
invalidate(path: str | None = None) -> None             # None = whole cache
```

Paths are vault-relative (`"02-trading-skills/garbage-time-mispricing.md"`). Vault root from `KALSHI_VAULT_PATH` (default `~/vaults/kalshi-vault`).

Exceptions: `VaultError` (base), `VaultNotFound`, `VaultSchemaError`, `VaultScopeError`, `VaultLockTimeout`.

## Behavior

### Query model — tag-filtered, never full-text
1. `query(VaultQuery)` scans (from cache) notes under `directory`, keeping those where every `frontmatter_filters` key matches exactly (`{"status": "confirmed"}`) and every `tag_filters` entry is a member of the corresponding list-valued frontmatter field. For this system, tag filters target `market_conditions` and `sports` (list membership: `"live" in note.market_conditions`). There is deliberately no full-text search — a trading cycle must never grep bodies.
2. Canonical call (skill-matcher's): `query(VaultQuery(directory="02-trading-skills", frontmatter_filters={"status": "confirmed"}, tag_filters=["live"]))`.

### Cache
3. In-memory, per-directory TTLs (Configuration table). Entry = parsed `VaultNote` + file `mtime`. On read: expired → re-stat; if `mtime` unchanged, refresh TTL without re-parsing; changed → re-parse.
4. Writes are **write-through**: serialize → atomic disk write (write to `path + ".tmp"`, `os.rename`) → update cache entry in the same operation. A write failure leaves cache untouched and raises.
5. `03-market-context/` gets a 5s TTL because game-monitor rewrites those notes continuously and trader must see fresh signals; `02-trading-skills/` gets 300s because skill notes change rarely (analyst stats or human edits) and skill-matcher hits it every cycle.

### Frontmatter handling
6. Parse YAML frontmatter (`---` fences); body preserved **byte-for-byte** — round-tripping a note through read→write must be a no-op on the body, including trailing whitespace.
7. `write_note` refuses to silently drop keys: if the target exists and `frontmatter` is missing keys present on disk, raise `VaultSchemaError` (callers must read-modify-write deliberately; `update_frontmatter` is the field-scoped alternative).
8. Schema validation hooks per directory: notes written under `02-trading-skills/` are validated against the `_skill-template.md` field set (all template keys present, `risk_profile ∈ {low,medium,high}`, `confidence_threshold ∈ [0,1]`, `status ∈ {draft,confirmed,retired}`); `04-trade-history/trades/` and `postmortems/` against the schemas defined in the trader/postmortem specs. Unknown directories: no validation.

### Write scopes (from agent-roster.md, enforced here)
9. `update_frontmatter(path, updates, caller)` checks `caller` against the scope table: caller `"analyst"` may update **only** `win_rate`, `sample_size` (and env-labeled variants `demo_win_rate`, `demo_sample_size`) on `02-trading-skills/*`; caller `"trader"` may not touch `02-trading-skills/` or `00-meta/` at all; caller `"human"`/`"admin"` is unrestricted. Violations raise `VaultScopeError`. The scope table is data (Configuration), not code.

### Concurrency
10. Single-process assumption documented; agents run as tasks in one process. For cross-process safety (dashboard, manual scripts), writes take an advisory `fcntl` lock on the target file, timeout 2s → `VaultLockTimeout`.
11. Stale-cache detection: any `VaultLockTimeout` or externally-modified `mtime` triggers `invalidate(path)`.

## Configuration
| Parameter | Default | Notes |
|---|---|---|
| `KALSHI_VAULT_PATH` | `~/vaults/kalshi-vault` | env var |
| `TTL_00_META` | 300s | league-config changes are rare |
| `TTL_02_SKILLS` | 300s | |
| `TTL_03_CONTEXT` | 5s | live signal path |
| `TTL_04_HISTORY` | 60s | read path; writes always write-through |
| `LOCK_TIMEOUT` | 2s | |
| write-scope table | see rule 9 | data, editable without code change |

## Edge cases
- **Missing frontmatter** (no `---` fence): `VaultNote.frontmatter = {}`, body = whole file; writing back adds fences only if frontmatter non-empty.
- **Malformed YAML:** raise `VaultSchemaError` naming the path — never return a half-parsed note; queries *skip* malformed notes with a logged warning (one bad note must not kill a cycle).
- **`.gitkeep` and non-`.md` files:** invisible to `query`; `read_note` on them raises `VaultNotFound`.
- **Note deleted on disk but cached:** `mtime` re-stat fails → drop entry, raise `VaultNotFound` on the read that discovered it.
- **Concurrent append_section calls to one note:** serialized by the file lock; append order is lock-acquisition order (acceptable — signal log entries carry their own timestamps).
- **Vault root missing/not a git repo:** raise `VaultError` at startup, not mid-cycle.

## Dependencies
None (foundation skill). Called by: every other skill and agent; espn-data and league-matching read `00-meta/league-config.md` through it; skill-matcher queries `02-trading-skills/`; risk-management persists its ledger; discord-bot persists halt state; postmortem updates stats.

## Testing requirements
- Round-trip byte-fidelity: read→write leaves file bit-identical (fixture with odd spacing, unicode, trailing newline variations).
- Query filters: fixtures for status filtering, list-membership tag filtering, combined filters, malformed-note skipping.
- Write-through atomicity: kill-between-tmp-and-rename simulation leaves the original intact.
- Scope enforcement: analyst updating `win_rate` passes; analyst updating `confidence_threshold` raises `VaultScopeError`; trader writing to `02-trading-skills/` raises.
- TTL/mtime: expired-but-unchanged refreshes without re-parse (assert via parse counter); external modification detected.
- Schema validation: skill note missing `confidence_threshold` rejected; `status: bogus` rejected.

## New types
None beyond CONTRACTS.md (`VaultNote`, `VaultQuery`).
