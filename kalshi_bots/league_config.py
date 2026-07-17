"""Parser for 00-meta/league-config.md (read via the vault skill).

Internal helper shared by espn-data and league-matching. Category A note: the
config is human-edited markdown; this parser reads only the table shapes the
vault file actually uses (per-league `| Key | Value |` tables and 4-column
alias tables) and strips presentation markers (backticks, bold, warning/check
marks).
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from kalshi_bots.skills.vault import Vault

_MARKS = str.maketrans("", "", "`*⚠✅")


def _clean(cell: str) -> str:
    return cell.translate(_MARKS).strip()


@dataclass
class AliasRow:
    kalshi_abbr: str
    espn_abbr: str
    display_name: str
    common_names: list[str] = field(default_factory=list)
    verified: bool = False


@dataclass
class LeagueConfig:
    league: str
    espn_slug: str
    series_ticker: str
    grammar_verified: bool
    aliases: list[AliasRow]

    def by_espn(self, espn_abbr: str) -> AliasRow | None:
        for r in self.aliases:
            if r.espn_abbr == espn_abbr:
                return r
        return None

    def by_kalshi(self, kalshi_abbr: str) -> AliasRow | None:
        for r in self.aliases:
            if r.kalshi_abbr == kalshi_abbr:
                return r
        return None

    def by_name(self, name: str) -> AliasRow | None:
        """Full-name resolution for odds-api. Exact match on display name or
        common-name entries (case-insensitive). No fuzzy matching."""
        low = name.lower().strip()
        for r in self.aliases:
            if r.display_name.lower() == low:
                return r
            if any(c.lower() == low for c in r.common_names):
                return r
        return None


def _table_rows(lines: list[str], start: int) -> list[list[str]]:
    rows = []
    i = start
    while i < len(lines) and lines[i].strip().startswith("|"):
        cells = [c.strip() for c in lines[i].strip().strip("|").split("|")]
        rows.append(cells)
        i += 1
    return rows


def parse_league_config(vault: Vault) -> dict[str, LeagueConfig]:
    note = vault.read_note("00-meta/league-config.md")
    lines = note.body.splitlines()
    configs: dict[str, LeagueConfig] = {}
    current: str | None = None
    kv: dict[str, dict] = {}
    aliases: dict[str, list[AliasRow]] = {}

    i = 0
    while i < len(lines):
        line = lines[i].strip()
        m = re.match(r"^## (NFL|NBA|MLB)\s*$", line)
        if m:
            current = m.group(1).lower()
            kv[current] = {}
            aliases[current] = []
            i += 1
            continue
        if current and line.startswith("|") and "Key" not in line:
            rows = _table_rows(lines, i)
            data_rows = [r for r in rows if not all(set(c) <= {"-", " ", ":"} for c in r)]
            if data_rows and len(data_rows[0]) == 2:
                for k, v in data_rows:
                    kv[current][_clean(k)] = v
            elif data_rows and len(data_rows[0]) == 4:
                for r in data_rows:
                    if _clean(r[0]) in ("Kalshi", ""):  # header row
                        continue
                    raw_common = r[3]
                    common = [c.strip() for c in re.split(r",", re.sub(r"\(.*?\)", "", raw_common)) if c.strip()]
                    aliases[current].append(AliasRow(
                        kalshi_abbr=_clean(r[0]),
                        espn_abbr=_clean(r[1]),
                        display_name=_clean(r[2]),
                        common_names=common,
                        verified="✅" in r[0],
                    ))
            i += len(rows)
            continue
        i += 1

    for lg, table in kv.items():
        slug = _clean(table.get("ESPN sport/league slug", ""))
        series_raw = table.get("Kalshi game series ticker", "")
        series = _clean(series_raw).split()[0] if series_raw.strip() else ""
        gv = _clean(table.get("grammar_verified", "false")).lower() == "true"
        configs[lg] = LeagueConfig(
            league=lg, espn_slug=slug, series_ticker=series,
            grammar_verified=gv, aliases=aliases.get(lg, []),
        )
    return configs
