# acb-rdv-py

A Python rewrite of the Assassin's Creed Brotherhood RendezVous (online) server,
built on `quazal`, a reusable Quazal library. It is a port of
[acb-rdv](https://github.com/michal-kapala/acb-rdv).

> **Status:** this is a project for hacking on and testing the game's online
> protocols, not a hardened production service. Expect rough edges, breaking
> changes and features that only exist to try things out.

- **`quazal/`** is the game-agnostic library:
  - PRUDP v0 transport, RMC, DDL serialisation and Kerberos tickets;
  - the common protocols: Authentication, Secure, NAT traversal, Friends,
    Messaging, MessageDelivery, GameSession and RichPresence;
  - NAT traversal with a UDP relay, and pass-through of AC Relay (client-side relay) URLs;
  - storage interfaces and a test client.

  Game specifics come in through `GameProfile` (keys, checksum constants, ports)
  and `SessionPolicy` (matchmaking rules).
- **`acb_rdv/`** is the ACB server:
  - the Ubisoft service protocols;
  - Hermes stats and contacts;
  - ACB matchmaking;
  - the status API;
  - the AN-API storage backend.
- **`onlineconfig/`** is the `onlineconfigservice.ubi.com` replacement for
  every game at once. Each game's `onlineConfigID` selects that game's
  RendezVous server: ACB goes to this server, ACR to its own server, and more
  games can be added in config. It runs as its own service.

Persistent data (accounts, friends, messages, offline invites, privileges,
rewards, telemetry) lives in MongoDB and goes through **AN-API** (`/rdv`
blueprint); this server holds no database credentials. Online players, game
sessions and NAT state are kept in memory.

## Running

For a full setup (reverse proxy, AN-API) see
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md).

```sh
python3 -m acb_rdv -c config.toml          # see config.example.toml
python3 -m onlineconfig -c onlineconfig.toml   # see onlineconfig.example.toml
python3 -m acb_rdv --debug                 # log every packet
ACB_RDV_BACKEND=memory python3 -m acb_rdv  # no AN-API: any name/password logs in
```

- It needs Python 3.11+ and `httpx`. Everything else is standard library.
- Ports:

  | Port | Service |
  |---|---|
  | UDP 21030 | auth |
  | UDP 21031 | secure server |
  | TCP 80 | online config, all games (`onlineconfig`) |
  | TCP 21032 | status API (`/`, `/sessions`, `/clients`, `/nat`, `/settings`) |
  | UDP 40000–40199 | server relay, only with `[nat] relay = true` (off by default) |

- Under systemd, `deploy/acb-rdv.service` and `deploy/onlineconfig.service` run
  the two services. The online config service uses `CAP_NET_BIND_SERVICE` for
  port 80 instead of authbind.

### Game settings

The `.cxb` settings file is served from `game_settings`, which is one of:
- a directory of XML files (the settings submodule at `gamesettings/`), encoded
  to CXB just in time by the C++ CXB library through `native/cxb.h`;
- a prebuilt `.cxb` file.

See `docs/CXB.md`. `GET /settings` on the status port shows what is being
served.

### Docker

```sh
cp .env.example .env                 # public address, AN-API keys, ports
mkdir -p data && cp /path/to/gamesettings_c1380_d873_s6285.cxb data/   # not included
docker compose up -d --build
docker compose logs -f
```

- **Two services, one image.**
  - `onlineconfig` runs on a normal bridge network, listening on 8080 inside
    the container and published as port 80 (`ONLINECONFIG_PUBLISH_PORT`).
  - Each game's sandbox address and port come from `ONLINECONFIG_*` in
    `.env`. ACB follows `ACB_RDV_PUBLIC_ADDRESS`/`ACB_RDV_AUTH_PORT`; ACR
    defaults to port 21040.
- **Host networking for `acb-rdv`.** It uses `network_mode: host`, for three
  reasons:
  - the server must see players' real addresses for NAT traversal;
  - it talks to AN-API over `127.0.0.1` in both directions (AN-API's gunicorn
    binds `127.0.0.1:8000`);
  - publishing the 200-port relay range through Docker's NAT is slow.
- **Ports.** They are opened directly on the host, so open them in the
  firewall. They must not clash with another server holding
  21030/21031/80/21032; change the ports in `.env` if they do.
- **Privileges.** Both containers run as uid 1000 with every capability
  dropped, a read-only root filesystem and `no-new-privileges`. `./data` must
  be readable by that user (and writable if you enable a packet capture).

### AN-API side

These are set in AN-API's environment (see `AN-API/src/app/config.py`):

| Variable | Purpose |
|---|---|
| `RDV_SERVICE_KEY` | Must equal `anapi_service_key` here. |
| `RDV_NOTIFY_KEY` | Must equal `internal_key` here. With `RDV_NOTIFY_URL` (default `http://127.0.0.1:21032/internal/notify`), friend changes made on the website reach online players in game. |
| `RDV_LEGACY_PASSWORD=ACB` | Only while players still use the old launcher, which sends the fixed password `ACB`. The new launcher sends the player's AN-API session token, and that is what the login checks. |

**Friends from the launcher or website.** Players manage in-game friends
through AN-API with their session token (`Authorization: Bearer <token>`):

| Endpoint | Does |
|---|---|
| `GET /rdv/me/friends` | friends, incoming and outgoing requests, blocked players |
| `POST /rdv/me/friends` `{"name": ...}` | send a request, or accept theirs if they already asked |
| `POST /rdv/me/friends/<name>/accept` | accept a request |
| `POST /rdv/me/friends/<name>/decline` | decline a request |
| `DELETE /rdv/me/friends/<name>` | remove a friend, withdraw a request, or unblock |
| `POST /rdv/me/friends/populate` | add everyone from the player's recorded matches |

Errors come back as JSON (`{"error": ...}`). Online players get the matching
in-game notifications through `/internal/notify`. When a request is accepted,
both players see each other come online.

**Friends from match history.** Players who appear in the same recorded match
(AN-API's `matches` collection) become in-game friends directly, with no
request needed. There are three ways to run it:

- AN-API's `scripts/backfill_rdv_friends.py`, for everyone or `--player`/`--since`;
- `POST /rdv/friends/populate` (admin);
- `POST /rdv/me/friends/populate` (a player's own list, from the website or
  launcher).

Blocks are never touched. A pair where someone declined a request or removed
the other is not added back until one of them sends a new request. Friend lists
are read at login, so online players see new friends after their next login.

## Tests

```sh
python3 -m pytest                  # unit, golden and end-to-end tests
uv run --no-project --with httpx --with pytest --with pytest-asyncio --with flask \
  --with flask_pymongo --with 'pymongo<4.9' --with mongomock --with flasgger \
  --with flask-cors --with markdown --with requests pytest    # + AN-API contract tests
```

| Test | Checks |
|---|---|
| `test_golden.py` | Replays a packet capture taken from acb-rdv (`ACB_CAPTURE`, a `packetLog.bin`) together with that server's database (`ACB_CAPTURE_DB`). Every packet must decode with a valid checksum and re-encode byte for byte. Every recorded request to a deterministic handler must get a byte-identical response, Kerberos tickets included. Skipped when no capture is available; none is included in the repository. |
| `test_e2e.py` | Runs the server on real UDP sockets with `quazal.client.QuazalClient`: login, secure connect, fragmentation, retransmission, duplicate suppression, friends and notifications, matchmaking, NAT probe pushes, per-recipient relay URLs, relay forwarding, join-a-friend, AC Relay URL pass-through, timeouts. |
| `test_nat.py` | Relay URL form (`PID`/`RVCID` of the peer behind it) and the AC Relay config. |
| `test_anapi_contract.py` | Runs the storage client against the real AN-API blueprint (mongomock), then a full game login through it. |

## Leaderboards

`acb_rdv/leaderboards.py` documents what the client sends and reads. Everything
shown comes from AN-API's public routes, cached for a minute. Stats the game
uploads are acknowledged and dropped, so only recorded matches count.

- **Ranking tabs** (the game's Free/Coop/Team/overall "Templar" boards 3, 13,
  23 and 33). These show the MMR ladders (`/boards/ranking/<mode>`) instead of
  Ubisoft's weekly best-three-scores system. `ranking_free`, `ranking_coop`,
  `ranking_team` and `ranking_overall` name the modes on each tab. A one-mode
  tab is that mode's ladder, in the website's order. A tab with several modes
  ranks each player's `ranking_combine` (mean, sum or max) of their MMR in the
  modes they played. Players with fewer than `ranking_placement_games` games
  in all of those modes rank below the rest. A row shows the MMR, the grade and
  the games played. The grade is the player's AN rank for that MMR (AN-Flask
  `rank_title`), spread over the game's 10 grades, where 1 is the best:
  Supreme Overlord Cleric 1, Grand Master Cleric 2, Grand Cleric 4, Cleric
  Supreme 6, Cleric 8, Disciple 10.
- **Statistic tabs and gamer cards** (score, average score, K/D, best score,
  kills, chases, stuns; boards 10 and 11). These show the totals from
  `/players/`, summed over `stats_modes`. Public and private show the same
  numbers. AN-API doesn't record chases, stuns or streaks, so those are 0.
- **XP** is `stats_default_xp` (650000) for everyone, as with the old server.

## News

The game's news panel reads one UbiNews channel: `UbiNews.GetChannel`, then
`News.GetNewsHeaders` for that channel and `News.GetNewsMessages` for the
bodies. The `news` setting picks where the items come from:

- `"anapi"` (default): AN-API's `/news/` posts (the launcher news), HTML
  flattened to text, refreshed every minute; `news_sender` sets the sender
  shown;
- `""`: no news, like acb-rdv;
- a path to a TOML file like `news.example.toml`, reloaded when it changes.

The game shows at most 64 characters of a subject or sender. Other channel
types (`Community`, `Clans`, `Maintenance`) still get no channels.

## Differences from acb-rdv

**Behaviour kept on purpose:**
- the wire formats;
- the dumped Tracking tickets and the fixed session key;
- acb-rdv's matchmaking rules;
- station URLs re-rendered the way acb-rdv did.

**Fixed:**

| Area | Change |
|---|---|
| PRUDP state | Kept per connection, not per player. This fixes pushes that went out with the Tracking connection's session id, and the `-0x10000` DISCONNECT signature hack. |
| News | acb-rdv sent an empty news channel. News now come from a file or AN-API. See above. |
| Statistics and leaderboards | acb-rdv answered with placeholders and empty boards. The leaderboards and gamer cards now show AN-API's MMR ladders and recorded match stats. See below. |
| Delivery | Incoming fragments are reassembled. Unacknowledged packets are retransmitted. A retransmitted request is answered from cache instead of running twice (no more duplicate friend requests). |
| Errors | Unknown methods and calls made before login get Quazal error replies instead of silence. |
| Secure connect | Only accepted for the player who logged in from that endpoint. acb-rdv trusted the pid in the ticket, and ticket keys come from a public dummy password, so anyone could take over an account. |
| Friend requests | Only the invited player can accept one. |
| Messages | A player can only read their own messages. |
| Online config | Split out into the multi-game `onlineconfig` service. The reply is valid JSON (acb-rdv dropped a comma), unknown ids get a 404, and the short aliases `ACB`/`ACR` work (AN-Flask's status check uses `ACB`). |
| Serialisation and robustness | 64-bit reads work. Nothing crashes on a missing session or a client that isn't logged in. There is no shared mutable state across threads (one asyncio loop). |
| NAT | Probe targets can be found by `RVCID` as well as `PID`. Bidirectional probing, extra candidates and an optional UDP relay were added; see `docs/NAT_TRAVERSAL.md`. |

## Provenance

- The code is a port of acb-rdv (MIT with the Commons Clause), so this project
  keeps that licence. See `LICENSE`.
