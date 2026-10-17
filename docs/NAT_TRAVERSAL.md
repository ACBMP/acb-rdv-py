# NAT traversal

ACB is peer-to-peer. The RendezVous server does not relay gameplay traffic,
with one exception: the optional relay described below, which is off by default. Its job is to help two
players' game clients find a working path to each other. This document covers:

- what the server does to help clients find each other;
- where each piece comes from;
- how the game picks a path, and what a URL must carry for that to work;
- how the server supports AC Relay, the client-side relay;
- how to find out which pieces the game actually needs.

## Sources

| Piece | Origin |
|---|---|
| `RequestProbeInitiation` (NAT protocol 3, method 1): tell the *target* peer to probe the caller | acb-rdv `NATTraversalService` |
| Caller URL in the full form `prudp:/address;port;CID=1;PID;sid=1;RVCID;stream=3;type=2` | acb-rdv `StationUrl(ClientInfo)` |
| NATPING echo on the PRUDP layer | acb-rdv `PrudpHandler` |
| `RegisterEx` answering with the address the server sees (`prudps:/…;sid=15;type=3`) | acb-rdv `SecureService` |
| `RequestProbeInitiationExt` (method 3) | earlier NAT experiments |
| Bidirectional probing: also tell the *caller* to probe every candidate | earlier NAT experiments |
| Candidate expansion with every URL the target registered | earlier NAT experiments |
| Reflected candidate: the server-observed address added to a host's URLs | earlier NAT experiments |
| UDP relay (`quazal/nat/relay.py`) and its injection into "join a friend" results | earlier NAT experiments |
| Resolving a probe target by `RVCID` when the URL has no `PID` | new |
| Per-candidate tagging, answer tracking and relay counters | new |
| Relay URLs carrying the `PID`/`RVCID` of the peer behind them | new (the earlier relay URL had neither, see below) |
| Passing AC Relay URLs through and tagging them `acrelay` | new |

Everything is built on acb-rdv's transport model:
- server-initiated requests use per-connection sequence counters;
- they are sent on the player's secure connection (`PRUDPServer.push`).

## Message flow

`A` is the player who wants to connect, `B` is the peer (usually a session
host) and `S` is the server.

1. **Registration.**
   - Both players log in and call `Secure.RegisterEx`.
   - `S` answers each with the address it sees them at.
   - The game adds that reflected URL to the candidates it registers later. The
     capture shows clients registering `prudps:/…;sid=15;type=3;PID;RVCID`.
2. **Hosting.**
   - `B` creates a session and calls `GameSession.RegisterURLs` with its local
     and reflected URLs.
   - Search, `GetSession` and contact results list `B`'s URLs exactly as
     registered (tag `known`, or `acrelay` for an AC Relay URL; an address
     that is not `B`'s own IP is not filtered or rewritten), plus:
     - `B`'s server-observed address, if `B` did not already list it (tag
       `reflected`);
     - the relay URL for the pair `A`–`B`, once one is allocated (tag `relay`).
3. **Joining a friend (`RetrieveGameSessionFromContact`).** `S` allocates a
   relay port for `A`–`B` and appends it to the host URLs in the reply.
4. **Probe request.** `A` sends `RequestProbeInitiation(urls)` or
   `RequestProbeInitiationExt(urls, myUrl)`. `S` then pushes these
   `InitiateProbe(url)` calls (protocol 3, method 2):

   | Push | Config switch | Tag |
   |---|---|---|
   | to `A`, one per URL `A` sent | `push_to_caller` | `client` (`acrelay` if at an AC Relay server) |
   | to `A`, one per extra candidate of `B`: registered URLs, reflected address, existing relay | `push_to_caller` + `expand_known` | `known` / `acrelay` / `reflected` / `relay` |
   | to `A`, the relay for `A`–`B` (allocated now if needed), naming `B` | `push_to_caller` + `relay` | `relay` |
   | to `B`, `A`'s URL (the server-observed one, or `myUrl` for method 3) | `push_to_target` | `caller` |
   | to `B`, the relay, naming `A` | `push_to_target` + `relay` | `relay` |
   | to `B`, one per URL `A` registered itself (login and session), rebuilt as `prudp:/address;port;PID=<A>;RVCID=<A>` | `push_to_target` + `expand_caller` | `caller-known` (`acrelay` if at an AC Relay server) |

5. **Punching.** Both clients send PRUDP probes at the same time. A NAT
   mapping on each side lets the other's packets in. With the relay, both sides
   send to the relay port, and it forwards between the first two endpoints it
   hears from.
6. **Choosing a path.** The game dials the candidate with the lowest probe
   ping (`JobConnectEndPoint::GetPreferredURL`). A candidate whose probes never
   got a reply is never chosen.

## How the game matches probe replies

Reverse engineered from the client (`acbmp_sf.exe`):

- every probe datagram carries the sender's `RVCID`;
- `Quazal::URLProbeList::ReceiveProbe` rebuilds a URL from the datagram's
  source address and that `RVCID`, and `UpdateProbe` → `FindProbe` looks the
  probe entry up by **IP address string + `RVCID`**;
- `URLProbe::Update` then only rewrites the entry's port.

So a candidate URL only ever gets a ping if it carries the `RVCID` of the peer
whose replies arrive from that IP. Consequences:

- **Server relay URLs name the peer behind them.** The first relay advertised
  `prudp:/address=<relay>;port=<port>` with no `RVCID`. Replies through the
  relay come from the relay's IP carrying the peer's `RVCID`, never matched,
  and the relay could never be picked. Allocations are still keyed by the
  unordered pid pair (one port per pair), but the URL is built per recipient:
  `A` gets `prudp:/address=<relay>;port=<port>;CID=1;PID=<B>;sid=1;RVCID=<B>;stream=3;type=2`
  and `B` gets the same with `A`'s `PID`/`RVCID`. `RVCID` is what matching
  needs; `PID` lets the server resolve the URL to a player when a client lists
  it in `RequestProbeInitiation`; `CID=1;sid=1;stream=3;type=2` mirror the
  peer's own server-observed URL (acb-rdv's `StationUrl(ClientInfo)`, the
  `reflected`/`caller` form), so the relay URL looks like any other URL of
  that peer. The `RVCID` is the one the server handed the peer in
  `RegisterEx`.
- **Two candidates of one peer on the same IP are ambiguous.** Matching
  ignores the port, so if an AC Relay server runs on the same IP as this
  server's relay, a reply may update the wrong entry. Run AC Relay on another
  address, or turn the server relay off.

## The relay

- It is a blind UDP forwarder. It does not parse PRUDP.
- It has one port per player pair, from `relay_port_start`–`relay_port_end`
  (40000–40199 by default).
- It is advertised per recipient as
  `prudp:/address=<public address>;port=<port>;CID=1;PID=<peer>;sid=1;RVCID=<peer>;stream=3;type=2`,
  where `<peer>` is the other player of the pair (see above).
- An allocation is freed after `relay_idle_timeout` seconds without traffic.
- It has no authentication. Anyone who sends to an allocated port first becomes
  one of its two peers. That is by design; it is acceptable for a
  fallback path.
- Open the port range in the firewall.
- It only ever serves a pair the server knows about: a probe request or a
  "join a friend" between two players. Links between two non-host players of
  a session, which the game negotiates peer to peer, never get one.
- It is **off by default**: AC Relay (below) covers the same players and the
  mesh links this relay never sees. Turn it on with `[nat] relay = true`
  (`ACB_RDV_NAT_RELAY=1`); `relay_port_start = 0` keeps it off regardless.

## AC Relay (client-side relay)

AC Relay is a separate, client-side relay: a game patch (ASI) allocates a UDP
port on a relay server the player picks, and makes the game publish
`prudp:/address=<relay ip>;port=<alloc port>` as one more of its own station
URLs, next to its LAN and public ones. The protocol between the patch and the
relay is in [AC_RELAY_PROTOCOL.md](AC_RELAY_PROTOCOL.md).

How it fits:

- The URL is published by the owner's own game, so it carries the owner's
  `RVCID`, and probe replies through the relay come from exactly that
  address. Probe matching works without any server help, and the game's
  lowest-ping race picks direct or relayed per peer.
- It covers every link of the session mesh, including links between non-host
  players that the server relay never sees, and it works for players whose
  NAT cannot be punched at all, as long as one side of each pair has a relay.
- A relay URL only helps if the *other* player sends to it. When the joiner
  has a relay and the host doesn't, the joiner's game dials the host's
  addresses, and nothing makes the host send to the joiner's relay. So the
  server also tells the host to probe every URL the joiner registered
  (`expand_caller`), including the joiner's relay URL. The host's probe opens
  the host's NAT towards the relay, and the joiner's patch then routes its
  traffic for the host through the relay.
- The server only has to pass these URLs through. It does: `RegisterURLs`
  keeps every URL whatever its address (only unknown keys such as `natf` are
  dropped by canonicalisation; `address`, `port`, `PID`, `RVCID`, ... stay),
  search/contact results list them unchanged, the reflected address is still
  added (the relay URL is not the host's own address), and
  `RequestProbeInitiation` pushes them to the caller like any other
  registered URL.

Configuration:

- `[nat] ac_relay_servers` (`ACB_RDV_NAT_AC_RELAY_SERVERS`): the relay
  servers players use, IPv4 addresses or hostnames, comma-separated (or a TOML
  list). Optional. When set, URLs at those addresses are tagged `acrelay`
  instead of `known`/`client`, `RegisterURLs` logs them, and `/nat` lists the
  currently registered ones. URLs on this server's own relay ports are never
  tagged `acrelay`. Unset, nothing is detected and the URLs are plain `known`
  candidates.
- When everyone uses AC Relay, turn the server relay off
  (`[nat] relay = false`). That frees the port range, and avoids the
  same-IP ambiguity above if both run on one host.

## Settling what the game actually uses

Earlier NAT experiments left a contradiction:
- one observation was that a server-pushed `InitiateProbe` "does not reach
  real punch logic on the client";
- yet the probing design was built around those pushes.

A packet capture from acb-rdv shows clients answering 279 of 280 pushed `InitiateProbe` calls
successfully. So the client does process the pushes, but that alone does not
prove it probes. The capture was taken over a VPN (10.8.0.x), where no NAT had
to be crossed.

The server records the data needed to decide:
- every push is logged as `InitiateProbe[<tag>] -> <player>: <url>`;
- the client's answer is logged as `...: ok | error 0x... | no answer`;
- `GET /nat` on the status port shows the counts per tag and outcome, the
  registered AC Relay URLs, and every relay allocation with packets and bytes
  per peer;
- relay allocations log their first and second peer, and a summary when they
  are released.

**Test matrix.** Use two, then three, real clients.

| Setup | What it tests |
|---|---|
| Same LAN | local candidates |
| Different home NATs (full-cone / port-restricted) | classic hole punching |
| One side behind a symmetric / CGNAT connection (mobile hotspot) | reflected address is wrong for new destinations |
| Both sides symmetric | only a relay can work (server relay or AC Relay) |
| Tailscale path (as used by the launcher today) | VPN addresses |

For each setup:
1. Join by matchmaking search and by "join a friend".
2. Record whether the match started.
3. Record which relay allocation carried traffic, and whether `acrelay`
   candidates were pushed (`/nat`).
4. Check the logs for which candidates were pushed.

Then switch the pieces off one at a time: `ACB_RDV_NAT_PUSH_TO_CALLER=0`,
`…_EXPAND_KNOWN=0`, `…_RELAY_IN_CONTACTS=0` and so on. Repeat the failing
setups, and keep only the pieces that make a difference. Three signs to look
for:
- a relay whose summary says "never used" never helped;
- if matches through symmetric NATs only work with a relay, and only when it is
  in the contact results, that observation was right;
- redundant connection attempts, which were seen when the target was told to
  probe the same URL several times, show up as repeated `InitiateProbe[caller]`
  lines for one request. The server now sends those once per target.

## Not implemented

- NAT protocol methods 4–7: `ReportNATTraversalResult`, `ReportNATProperties`,
  `GetRelaySignatureKey`, and so on.
- `natm`/`natf`/`upnp`/`pmp` StationURL fields. None of these appear in the
  capture. Unknown URL keys are kept when a URL is parsed, but `RegisterURLs`
  and `RegisterEx` canonicalise URLs (acb-rdv's form), which drops them.
- Port prediction for symmetric NATs.
