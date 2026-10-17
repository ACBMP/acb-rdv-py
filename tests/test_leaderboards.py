"""In-game leaderboards from AN-API records, and the PlayerStatistics (0x6C) wire format."""

from __future__ import annotations

from quazal.ddl import Reader, Writer
from quazal.storage import MemoryStorage, ModeStats, User

from acb_rdv.config import Config
from acb_rdv.leaderboards import COOP_BOARD, FREE_BOARD, OVERALL_BOARD, TEAM_BOARD, Leaderboards, StatType, \
    grade_for
from acb_rdv.protocols.hermes import ScoreListRead, StatisticValueVariant, StatisticWriteValue, \
    StatisticWriteWithBoard

from .test_e2e import env  # noqa: F401  (fixture)


def m(mmr=1000.0, games=0, won=0, score=0, high=0, kills=0, deaths=0) -> ModeStats:
    return ModeStats(mmr, games, won, games - won, score, high, kills, deaths)


def storage_with(*names: str) -> MemoryStorage:
    storage = MemoryStorage()
    for i, name in enumerate(names):
        storage.add_user(User(10001 + i, name))
    return storage


def boards(storage: MemoryStorage, **cfg) -> Leaderboards:
    return Leaderboards(storage, Config(**cfg))


async def test_ranking_tabs_follow_mmr_mapping():
    storage = storage_with("Alice", "Bob", "Carol")
    storage.records = {
        10001: {"asb": m(1600, 30), "mh": m(1400, 12)},
        10002: {"asb": m(1700, 4), "e": m(1550, 20)},   # 4 asb games: not placed in asb
        10003: {"mh": m(1500, 50), "dm": m(2000, 99)},  # dm is on no tab
    }
    lb = boards(storage)
    free = await lb.ranking(FREE_BOARD)
    assert [(e.name, e.rank, round(e.score)) for e in free] == [("Alice", 1, 1600), ("Bob", 2, 1700)]
    assert [e.name for e in await lb.ranking(TEAM_BOARD)] == ["Carol", "Alice"]
    assert [e.name for e in await lb.ranking(COOP_BOARD)] == ["Bob"]
    overall = await lb.ranking(OVERALL_BOARD)  # mean MMR over asb/mh/e
    assert [(e.name, round(e.score), e.games) for e in overall] == [
        ("Bob", 1625, 24), ("Alice", 1500, 42), ("Carol", 1500, 50)]  # tie: by name

    total, rows = await lb.range(OVERALL_BOARD, 1, 1, 10)
    assert total == 3
    assert {k: x.i for k, x in rows[0].stats.items()} == {1: 1, 2: 1, 3: 1, 4: 3, 5: 3, 6: 1625, 7: 24, 8: 0}

    got = await lb.player_stats([10003], [(TEAM_BOARD, list(range(1, 9))), (FREE_BOARD, [1, 5, 6])])
    (team, team_rank, team_stats), (free_b, free_rank, free_stats) = got[10003]
    assert (team, team_rank, team_stats[6].i) == (TEAM_BOARD, 1, 1500)
    assert (free_b, free_rank, {k: x.i for k, x in free_stats.items()}) == (FREE_BOARD, 0, {1: 0, 5: 2, 6: 0})

    lb_sum = boards(storage, ranking_overall="asb,e", ranking_combine="sum")
    assert [round(e.score) for e in await lb_sum.ranking(OVERALL_BOARD)] == [3250, 1600]


def test_grades_follow_an_ranks():
    # AN-Flask rank_title bounds, spread over the game's 10 grades (1 best)
    assert [grade_for(x) for x in (0, 800.9, 801, 999, 1000, 1099.5, 1100, 1199, 1200, 1399, 1400, 3000)] == [
        10, 10, 8, 8, 6, 6, 4, 4, 2, 2, 1, 1]


async def test_pyramid_stats_per_grade():
    storage = storage_with("A", "B", "C", "D")
    storage.records = {10001: {"mh": m(1450, 20)}, 10002: {"mh": m(1150, 20)},
                       10003: {"mh": m(1120, 20)}, 10004: {"mh": m(700, 20)}}
    total, rows = await boards(storage).range(TEAM_BOARD, 1, 1, 10)
    # (rank, grade, rank within grade, players in grade)
    assert [tuple(r.stats[i].i for i in (1, 2, 3, 4)) for r in rows] == [
        (1, 1, 1, 1), (2, 4, 1, 2), (3, 4, 2, 2), (4, 10, 1, 1)]


async def test_statistic_tabs_from_recorded_stats():
    storage = storage_with("Alice", "Bob", "Carol")
    storage.records = {
        10001: {"asb": m(games=10, won=4, score=30000, high=5000, kills=50, deaths=25),
                "mh": m(games=10, won=6, score=10000, high=6000, kills=30, deaths=15)},
        10002: {"e": m(games=5, won=5, score=50000, high=9000, kills=90, deaths=0)},
        10003: {"dm": m(games=40, score=99999, kills=500)},  # only modes outside stats_modes
    }
    lb = boards(storage)
    alice = lb.career((await storage.player_records())[0])
    assert alice == {2: 40000, 3: 200000, 4: 6000, 5: 20, 9: 10, 10: 50, 12: 200, 13: 80, 14: 40, 16: 400}

    total, rows = await lb.range(11, 13, 1, 10)  # kills tab: shows 13, 16, 22
    assert total == 2
    assert [(r.name, r.rank, {k: x.i for k, x in r.stats.items()}) for r in rows] == [
        ("Bob", 1, {13: 90, 16: 1800, 22: 0}), ("Alice", 2, {13: 80, 16: 400, 22: 0})]
    assert [r.rank for r in (await lb.range(10, 12, 1, 10))[1]] == [1, 2]  # private board: same data
    assert (await lb.around(11, 2, 10001, 1))[1][0].name == "Alice"
    assert await lb.around(11, 2, 10003, 5) == (2, [])  # no games in stats_modes: unranked
    assert [r.name for r in await lb.for_players(11, 2, [10001, 10003])] == ["Alice"]

    got = await lb.player_stats([10002, 10003], [(1, [1]), (11, [2, 5]), (4, [1])])
    assert [(b, {k: x.i for k, x in s.items()}) for b, _, s in got[10002]] == [
        (1, {1: 650000}), (11, {2: 50000, 5: 5}), (4, {1: 0})]
    assert [(b, {k: x.i for k, x in s.items()}) for b, _, s in got[10003]][1] == (11, {2: 0, 5: 0})


def write_body(boards_: list[tuple[int, list[tuple[int, int, int]]]]) -> bytes:
    w = Writer()
    items = [StatisticWriteWithBoard(b, [StatisticWriteValue(i, StatisticValueVariant(int_value=val), m_)
                                         for i, val, m_ in vals]) for b, vals in boards_]
    w.list(items, lambda w, x: x.write(w))
    return w.bytes()


def read_scores(body: bytes, with_total: bool = True):
    r = Reader(body)
    scores = r.list(ScoreListRead.read)
    total = r.u32() if with_total else None
    assert r.eof()
    return scores, total


async def test_protocol_round_trip(env):  # noqa: F811
    server, storage, login, _ = env
    storage.records = {10001: {"mh": m(1450, 15, score=3000, kills=9)},
                       10002: {"mh": m(1510, 11, score=4200, kills=4)}}
    alice = await login("Alice", "pw-a")
    # uploads are acknowledged and dropped
    assert (await alice.call(0x6C, 2, write_body([(1, [(1, 500, 0)]), (11, [(2, 1, 2)])]))).success

    # gamer card: own stats when no names are given
    q = Writer().list([(1, [1]), (11, [2, 3])], lambda w, x: w.u32(x[0]).u32_list(x[1])).string_list([])
    scores, _ = read_scores((await alice.call(0x6C, 3, q.bytes())).body, with_total=False)
    assert [(s.pid, s.name) for s in scores] == [(10001, "Alice")]
    vals = {(b.board, x.id): x.value.int_value for b in scores[0].boards for x in b.values}
    assert vals == {(1, 1): 650000, (11, 2): 3000, (11, 3): 20000}

    # score tab: ranks stat 2 and shows 2, 1, 3
    body = Writer().u32(11).u32(2).s32(1).s32(10).s32(0).s32(0).bytes()
    scores, total = read_scores((await alice.call(0x6C, 4, body)).body)
    assert total == 2
    assert [(s.name, s.boards[0].rank) for s in scores] == [("Bob", 1), ("Alice", 2)]
    assert [x.id for x in scores[1].boards[0].values] == [2, 1, 3]

    # around me / own rank
    body = Writer().u32(11).u32(2).string("Alice").s32(1).s32(0).s32(0).bytes()
    scores, total = read_scores((await alice.call(0x6C, 5, body)).body)
    assert [(s.name, s.boards[0].rank) for s in scores] == [("Alice", 2)]

    # Team tab (board 23) = mh MMR
    body = Writer().u32(TEAM_BOARD).u32(1).s32(1).s32(10).s32(0).s32(0).bytes()
    scores, total = read_scores((await alice.call(0x6C, 4, body)).body)
    assert [(s.name, s.boards[0].rank, {x.id: x.value.int_value for x in s.boards[0].values}[6])
            for s in scores] == [("Bob", 1, 1510), ("Alice", 2, 1450)]
    assert all(x.value.type == StatType.INT for x in scores[0].boards[0].values)

    # friends board
    body = Writer().u32(1).u32(TEAM_BOARD).u32(1).string_list(["Alice", "Bob", "Nobody"]).bytes()
    scores, total = read_scores((await alice.call(0x6C, 6, body)).body)
    assert [s.name for s in scores] == ["Bob", "Alice"] and total == 2
    scores, _ = read_scores((await alice.call(0x6C, 7, body)).body, with_total=False)
    assert len(scores) == 2
