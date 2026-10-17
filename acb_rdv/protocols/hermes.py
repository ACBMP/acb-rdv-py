"""Hermes player statistics (0x6C): Quazal ``PlayerStatisticsProtocol``.

Reads are answered from AN-API's MMR ladders and recorded match stats (see
:mod:`acb_rdv.leaderboards`); the stats the game uploads are acknowledged and
dropped. Type and field names are the client's (``acbmp_sf``).
"""

from __future__ import annotations

import logging

from quazal.context import ServerContext
from quazal.ddl import F64, S32, U8, U32, U64, DateTime, ListOf, String, Structure, field, qdatetime_now
from quazal.rmc import Call, Protocol, method

from ..leaderboards import Leaderboards, StatRow, StatValue

log = logging.getLogger("acb.hermes")

U64_MASK = (1 << 64) - 1


class StatisticValueVariant(Structure):
    int_value: S32 = 0
    int64_value: U64 = 0
    double_value: F64 = 0.0
    string_value: String = ""
    type: U8 = 1  # 1 int, 2 int64, 3/4 double, 5 string

    @classmethod
    def of(cls, v: StatValue) -> "StatisticValueVariant":
        return cls(v.i, v.l & U64_MASK, v.d, v.s, v.type)

    def value(self) -> StatValue:
        int64 = self.int64_value - (1 << 64) if self.int64_value >> 63 else self.int64_value
        return StatValue(self.type, self.int_value, int64, self.double_value, self.string_value)


class StatisticWriteValue(Structure):
    id: U8 = 0
    value: StatisticValueVariant = field(default_factory=StatisticValueVariant)
    method: U8 = 2


class StatisticWriteWithBoard(Structure):
    board: U32 = 0
    values: ListOf(StatisticWriteValue) = field(default_factory=list)


class StatisticData(Structure):
    board: U32 = 0
    ids: ListOf(U32) = field(default_factory=list)


class LeaderboardData(Structure):
    board: U32 = 0
    stat: U32 = 0


class StatisticReadValue(Structure):
    id: U8 = 0
    value: StatisticValueVariant = field(default_factory=StatisticValueVariant)
    unk: U8 = 1
    value2: StatisticValueVariant = field(default_factory=StatisticValueVariant)
    value3: StatisticValueVariant = field(default_factory=StatisticValueVariant)

    @classmethod
    def of(cls, stat: int, v: StatValue) -> "StatisticReadValue":
        empty = StatisticValueVariant(type=v.type)
        return cls(stat & 0xFF, StatisticValueVariant.of(v), 1, empty, empty)


class StatisticReadValueByBoard(Structure):
    board: U32 = 0
    rank: U32 = 0  # StatisticRecord::getRank
    unk: U32 = 0
    values: ListOf(StatisticReadValue) = field(default_factory=list)
    time: DateTime = 0


class ScoreListRead(Structure):
    pid: U32 = 0
    name: String = ""
    boards: ListOf(StatisticReadValueByBoard) = field(default_factory=list)


def _by_board(board: int, rank: int, stats: dict[int, StatValue], now: int) -> StatisticReadValueByBoard:
    return StatisticReadValueByBoard(board, rank, 0, [StatisticReadValue.of(i, v) for i, v in stats.items()], now)


class HermesPlayerStatisticsProtocol(Protocol):
    protocol_id = 0x6C
    name = "HermesPlayerStatistics"

    def __init__(self, ctx: ServerContext, boards: Leaderboards):
        self.ctx = ctx
        self.boards = boards

    def _scores(self, scores: list[ScoreListRead], total: int | None = None):
        w = self.ctx.writer().list(scores, lambda w, s: s.write(w))
        return w if total is None else w.u32(total)

    def _rows(self, board: int, rows: list[StatRow]) -> list[ScoreListRead]:
        now = qdatetime_now()
        return [ScoreListRead(r.pid, r.name, [_by_board(board, r.rank, r.stats, now)]) for r in rows]

    async def _pids(self, names: list[str]) -> dict[int, str]:
        out = {}
        for name in names:
            user = await self.ctx.storage.get_user_by_name(name)
            if user is not None:
                out[user.pid] = user.name
        return out

    @staticmethod
    def _write(call: Call, boards: list[StatisticWriteWithBoard]) -> None:
        # the leaderboards come from AN-API's recorded matches, not from what the client claims
        log.debug("%s stats (dropped): %s", call.client,
                  [(b.board, v.id, v.method, v.value.value().number) for b in boards for v in b.values])

    @method(1, "SendArbitratedPlayerStats")
    async def send_arbitrated_player_stats(self, call: Call):
        call.args.s32()  # arbitration session
        call.args.u32()
        self._write(call, call.args.list(StatisticWriteWithBoard.read))
        return None

    @method(2, "WritePlayerStats")
    async def write_player_stats(self, call: Call):
        self._write(call, call.args.list(StatisticWriteWithBoard.read))
        return None

    @method(3, "ReadPlayerStats")
    async def read_player_stats(self, call: Call):
        queries = [(q.board, list(q.ids)) for q in call.args.list(StatisticData.read)]
        names = call.args.string_list()
        players = await self._pids(names) if names else {call.client.pid: call.client.name}
        stats = await self.boards.player_stats(list(players), queries)
        now = qdatetime_now()
        scores = [ScoreListRead(pid, name, [_by_board(b, rank, values, now) for b, rank, values in stats[pid]])
                  for pid, name in players.items()]
        return self._scores(scores)

    @method(4, "ReadStatsLeaderboardByRange")
    async def read_leaderboard_by_range(self, call: Call):
        a = call.args
        board, stat, start, count = a.u32(), a.u32(), a.s32(), a.s32()
        a.s32(), a.s32()  # filter, unused by the game
        total, rows = await self.boards.range(board, stat, start, count)
        return self._scores(self._rows(board, rows), total)

    @method(5, "ReadStatsLeaderboardByRangeForPlayer")
    async def read_leaderboard_for_player(self, call: Call):
        a = call.args
        board, stat, name, count = a.u32(), a.u32(), a.string(), a.s32()
        a.s32(), a.s32()
        pid = next(iter(await self._pids([name])), None)
        if pid is None:
            return self._scores([], 0)
        total, rows = await self.boards.around(board, stat, pid, count)
        return self._scores(self._rows(board, rows), total)

    async def _for_players(self, call: Call) -> list[ScoreListRead]:
        wanted = call.args.list(LeaderboardData.read)
        players = await self._pids(call.args.string_list())
        if not wanted:
            return []
        board, stat = wanted[0].board, wanted[0].stat
        rows = await self.boards.for_players(board, stat, list(players))
        for row in rows:
            row.name = players.get(row.pid, row.name)
        return self._rows(board, rows)

    @method(6, "ReadStatsLeaderboardForPlayers")
    async def read_leaderboard_for_players(self, call: Call):
        scores = await self._for_players(call)
        return self._scores(scores, len(scores))

    @method(7, "ReadStatsLeaderboardFromSlidingTable")
    async def read_leaderboard_from_sliding_table(self, call: Call):
        return self._scores(await self._for_players(call))
