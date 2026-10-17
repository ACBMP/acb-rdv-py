"""In-game leaderboards behind the Hermes PlayerStatistics protocol (0x6C).

Everything shown comes from AN-API: the ranking tabs from the MMR ladders
(``/boards/ranking/<mode>``), the statistic tabs and the gamer card from the
players' recorded match stats (``/players/``). What the game uploads itself is
not kept.

What the client does (reverse-engineered from ACB's ``LeaderboardManager``,
``TemplarGradeManager`` and Hermes ``StatisticsService``):

* **Statistic boards 10 (private) and 11 (public).** The client reads its
  career totals from these at login (``ReadPlayerStats``), adds each match and
  uploads them again. Stat ids: 2 total score, 3 average score per session x100,
  4 best session score, 5 sessions, 9 sessions won, 10 won/played x100,
  12 kills/deaths x100, 13 kills, 14 deaths, 15 best session K/D x100,
  16 kills per session x100, 17-22 stuns, kill streaks and timings, 23-28
  chases and escapes. Here both boards hold the AN-API totals over
  ``stats_modes``; what AN-API doesn't record is 0.
  The statistic tabs rank one stat and show three (``ScoreTypeToBoard``):

  =====  ============  ===========================
  tab    ranking stat  columns shown
  =====  ============  ===========================
  0      2             2, 1, 3
  1      3             3, 1, 4
  2      12            12, 1, 15
  3      4             4, 5, 13
  4      25            25, 24, 28
  5      13            13, 16, 22
  6      17            17, 18, 19
  =====  ============  ===========================

  Every row must carry all three columns: the client dereferences the lookup
  without a null check, so a missing stat crashes it.
* **XP** is board 1, stat 1. It stays at ``stats_default_xp`` for everyone, as
  with the old server.
* **Ranking tabs 7-10** read boards 3 (Free), 13 (Coop), 23 (Team) and 33
  (overall). Ubisoft filled them server-side from a weekly "Templar" score;
  here they show the MMR ladders, with the modes per tab from configuration. A
  one-mode tab is that mode's ladder as the website shows it. The client reads
  stats 1-8 of these boards for the player's "pyramid" and shows stats 6,
  2 (minus one) and 7 on each row: 1 global rank, 2 grade, 3 rank within the
  grade, 4 players in the grade, 5 players in total, 6 score, 7 and 8 average
  sessions played. Here: 6 is the MMR, 7 the games played, and the grade (the
  game has 10, 1 best) is the player's AN rank for that MMR (``AN_RANKS``).
"""

from __future__ import annotations

import enum
import logging
from dataclasses import dataclass

from quazal.storage import PlayerRecord, StatsStorage

from .config import Config

log = logging.getLogger("acb.leaderboards")

FREE_BOARD, COOP_BOARD, TEAM_BOARD, OVERALL_BOARD = 3, 13, 23, 33
PRIVATE_BOARD, PUBLIC_BOARD = 10, 11
XP_BOARD, XP_STAT = 1, 1
RANKING_STATS = range(1, 9)

# AN's ranks (AN-Flask ``rank_title``): upper MMR bound (exclusive) -> game grade,
# spread over the game's 10 grades, where 1 is the best. Disciple, Cleric, Cleric
# Supreme, Grand Cleric, Grand Master Cleric; anything higher is Supreme Overlord Cleric.
AN_RANKS: tuple[tuple[float, int], ...] = ((801, 10), (1000, 8), (1100, 6), (1200, 4), (1400, 2))
TOP_GRADE = 1


def grade_for(mmr: float) -> int:
    return next((grade for bound, grade in AN_RANKS if mmr < bound), TOP_GRADE)

# ranking stat -> the columns a row shows (LeaderboardManager::ScoreTypeToBoard)
DISPLAY_COLUMNS: dict[int, tuple[int, ...]] = {
    2: (2, 1, 3), 3: (3, 1, 4), 12: (12, 1, 15), 4: (4, 5, 13), 25: (25, 24, 28),
    13: (13, 16, 22), 17: (17, 18, 19), 1: (1, 2, 7),
}


class StatType(enum.IntEnum):
    """``StatisticValueVariant`` type tag: which of the variant's fields holds the value."""

    NONE = 0
    INT = 1
    INT64 = 2
    DOUBLE = 3
    FLOAT = 4
    STRING = 5


@dataclass
class StatValue:
    """A statistic as the PlayerStatistics protocol carries it."""

    type: int = StatType.INT
    i: int = 0
    l: int = 0
    d: float = 0.0
    s: str = ""

    @property
    def number(self) -> float:
        if self.type == StatType.INT64:
            return float(self.l)
        if self.type in (StatType.DOUBLE, StatType.FLOAT):
            return self.d
        if self.type == StatType.STRING:
            return 0.0
        return float(self.i)

    @classmethod
    def of(cls, value: int) -> "StatValue":
        return cls(StatType.INT, i=int(value))


@dataclass
class StatRow:
    """One player's line on a board."""

    pid: int
    name: str
    rank: int
    stats: dict[int, StatValue]


@dataclass
class RankingEntry:
    pid: int
    name: str
    score: float
    games: int
    placed: bool
    rank: int = 0


def _modes(spec: str) -> list[str]:
    return [m.strip() for m in spec.split(",") if m.strip()]


def _per(value: int, games: int) -> int:
    return round(value * 100 / games) if games else 0


class Leaderboards:
    def __init__(self, storage: StatsStorage, config: Config):
        self.storage = storage
        self.tabs = {
            FREE_BOARD: _modes(config.ranking_free),
            COOP_BOARD: _modes(config.ranking_coop),
            TEAM_BOARD: _modes(config.ranking_team),
            OVERALL_BOARD: _modes(config.ranking_overall),
        }
        if config.ranking_combine not in ("mean", "sum", "max"):
            raise ValueError(f"ranking_combine must be mean, sum or max, not {config.ranking_combine!r}")
        self.combine = config.ranking_combine
        self.placement_games = config.ranking_placement_games
        self.stats_modes = _modes(config.stats_modes)
        self.default_xp = config.stats_default_xp

    def default(self, board: int, stat: int) -> StatValue:
        if (board, stat) == (XP_BOARD, XP_STAT):
            return StatValue.of(self.default_xp)
        return StatValue()

    # --- ranking tabs (MMR) -----------------------------------------------------

    def is_ranking_board(self, board: int) -> bool:
        return board in self.tabs

    def _score(self, mmrs: list[float]) -> float:
        if self.combine == "sum":
            return sum(mmrs)
        if self.combine == "max":
            return max(mmrs)
        return sum(mmrs) / len(mmrs)

    async def ranking(self, board: int) -> list[RankingEntry]:
        """A tab's players in order. One mode: its ladder exactly as the website shows it.
        Several: each player's combined MMR over the modes played, placed players first."""
        modes = self.tabs[board]
        ladders = [await self.storage.mmr_ladder(m) for m in modes]
        if len(ladders) == 1:
            entries = [RankingEntry(e.pid, e.name, e.mmr, e.games, e.games >= self.placement_games)
                       for e in ladders[0]]
        else:
            merged: dict[str, list] = {}
            for ladder in ladders:
                for e in ladder:
                    merged.setdefault(e.name, []).append(e)
            entries = [RankingEntry(next((e.pid for e in played if e.pid), 0), name,
                                    self._score([e.mmr for e in played]), sum(e.games for e in played),
                                    any(e.games >= self.placement_games for e in played))
                       for name, played in merged.items()]
            entries.sort(key=lambda e: (not e.placed, -e.score, e.name))
        for rank, entry in enumerate(entries, 1):
            entry.rank = rank
        return entries

    @staticmethod
    def ranking_stats(entries: list[RankingEntry]) -> dict[int, dict[int, StatValue]]:
        """The pyramid stats 1-8 of every entry, keyed by ``id(entry)``."""
        grades = [grade_for(e.score) for e in entries]
        in_grade = {g: grades.count(g) for g in set(grades)}
        seen: dict[int, int] = {}
        out = {}
        for entry, grade in zip(entries, grades):
            seen[grade] = seen.get(grade, 0) + 1
            values = {1: entry.rank, 2: grade, 3: seen[grade], 4: in_grade[grade], 5: len(entries),
                      6: round(entry.score), 7: entry.games, 8: 0}
            out[id(entry)] = {i: StatValue.of(v) for i, v in values.items()}
        return out

    @staticmethod
    def unranked_stats(total: int) -> dict[int, StatValue]:
        return {i: StatValue() for i in RANKING_STATS} | {5: StatValue.of(total)}

    # --- statistic tabs (recorded match stats) -----------------------------------

    def career(self, record: PlayerRecord) -> dict[int, int]:
        """Board 10/11 stat values from a player's AN-API totals over ``stats_modes``."""
        played = [record.modes[m] for m in self.stats_modes if m in record.modes]
        games = sum(m.games for m in played)
        won = sum(m.won for m in played)
        score = sum(m.total_score for m in played)
        kills = sum(m.kills for m in played)
        deaths = sum(m.deaths for m in played)
        return {
            2: score, 3: _per(score, games), 4: max((m.high_score for m in played), default=0), 5: games,
            9: won, 10: _per(won, games), 12: kills * 100 if not deaths else round(kills * 100 / deaths),
            13: kills, 14: deaths, 16: _per(kills, games),
        }

    async def _careers(self) -> list[tuple[PlayerRecord, dict[int, int]]]:
        out = []
        for record in await self.storage.player_records():
            values = self.career(record)
            if values[5]:
                out.append((record, values))
        return out

    # --- boards ----------------------------------------------------------------------

    def columns(self, board: int, stat: int) -> tuple[int, ...]:
        if self.is_ranking_board(board):
            return tuple(RANKING_STATS)
        return tuple(dict.fromkeys((stat, *DISPLAY_COLUMNS.get(stat, ()))))

    async def board(self, board: int, stat: int) -> list[StatRow]:
        """Every ranked row of a board, in order, each with the columns the client shows."""
        if self.is_ranking_board(board):
            entries = await self.ranking(board)
            stats = self.ranking_stats(entries)
            return [StatRow(e.pid, e.name, e.rank, stats[id(e)]) for e in entries]
        if board not in (PRIVATE_BOARD, PUBLIC_BOARD):
            return []
        careers = sorted(await self._careers(), key=lambda c: (-c[1].get(stat, 0), c[0].name))
        cols = self.columns(board, stat)
        return [StatRow(r.pid, r.name, rank, {i: StatValue.of(v.get(i, 0)) for i in cols})
                for rank, (r, v) in enumerate(careers, 1)]

    async def player_stats(self, pids: list[int], queries: list[tuple[int, list[int]]]
                           ) -> dict[int, list[tuple[int, int, dict[int, StatValue]]]]:
        """``ReadPlayerStats``: per player, ``(board, rank, {stat: value})`` for each queried board."""
        rankings = {b: await self.ranking(b) for b, _ in queries if self.is_ranking_board(b)}
        careers = {}
        if any(b in (PRIVATE_BOARD, PUBLIC_BOARD) for b, _ in queries):
            careers = {r.pid: self.career(r) for r in await self.storage.player_records() if r.pid}
        out = {}
        for pid in pids:
            boards = []
            for board, ids in queries:
                if board in rankings:
                    entries = rankings[board]
                    entry = next((e for e in entries if e.pid == pid), None)
                    values = (self.ranking_stats(entries)[id(entry)] if entry
                              else self.unranked_stats(len(entries)))
                    boards.append((board, entry.rank if entry else 0,
                                   {i: values.get(i) or StatValue() for i in ids}))
                elif board in (PRIVATE_BOARD, PUBLIC_BOARD):
                    have = careers.get(pid, {})
                    boards.append((board, 0, {i: StatValue.of(have.get(i, 0)) for i in ids}))
                else:
                    boards.append((board, 0, {i: self.default(board, i) for i in ids}))
            out[pid] = boards
        return out

    async def range(self, board: int, stat: int, start: int, count: int) -> tuple[int, list[StatRow]]:
        """Rows ``start`` .. ``start + count - 1`` (1-based) and the board's size."""
        rows = await self.board(board, stat)
        start, count = max(1, start), max(0, min(count, 200))
        return len(rows), rows[start - 1:start - 1 + count]

    async def around(self, board: int, stat: int, pid: int, count: int) -> tuple[int, list[StatRow]]:
        """``count`` rows with the player in the middle; none when the player is unranked."""
        rows = await self.board(board, stat)
        rank = next((r.rank for r in rows if r.pid == pid), 0)
        if rank == 0 or count <= 0:
            return len(rows), []
        start = max(1, min(rank - count // 2, len(rows) - count + 1))
        return len(rows), rows[start - 1:start - 1 + count]

    async def for_players(self, board: int, stat: int, pids: list[int]) -> list[StatRow]:
        """The ranked ones among ``pids`` (a friends board), ordered by rank."""
        wanted = set(pids)
        return [r for r in await self.board(board, stat) if r.pid in wanted]
