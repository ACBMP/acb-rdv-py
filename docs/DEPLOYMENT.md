# Deploying acb-rdv-py

How to run the ACB server stack in front of an existing reverse proxy
(bare-metal nginx or a Traefik container), with AN-API on the same machine or
elsewhere.

This server is meant for hacking and testing rather than production; treat this
guide as a starting point, not a hardened setup.

## What runs where

| Piece | Runs as | Traffic |
|---|---|---|
| `onlineconfig` | container, bridge network | HTTP, reached through your proxy (or port 80 directly) |
| `acb-rdv` | container, **host network** | UDP 21030/21031 + relay UDP 40000–40199, directly; TCP 21032 status API |
| AN-API (with the `/rdv` blueprint) | gunicorn | acb-rdv calls its `/rdv` endpoints; AN-API calls back `POST /internal/notify` |
| MongoDB | behind AN-API | acb-rdv never connects to it |

**The proxy only handles the online-config HTTP request.** The game's UDP
traffic has to reach acb-rdv directly:
- acb-rdv needs the players' real addresses for NAT traversal and to tell each
  client how the server sees it;
- nginx and Traefik can't pass those through for UDP.

## 1. Get the code

On the server (Linux, Docker with Compose v2.24 or newer):

```sh
git clone git@github.com:ACBMP/acb-rdv-py.git
cd acb-rdv-py
cp .env.example .env
mkdir -p data
cp /path/to/gamesettings_c1380_d873_s6285.cxb data/   # the game's settings, not included
chown -R 1000:1000 data        # both containers run as uid 1000
```

## 2. Configure `.env`

| Variable | Value |
|---|---|
| `ACB_RDV_PUBLIC_ADDRESS` | The server's public IP, or a hostname that resolves to it. Game clients send UDP straight to this address, so no Cloudflare proxy (orange cloud) and no load balancer. |
| `ACB_RDV_ANAPI_URL` | `http://127.0.0.1:8000` if AN-API runs on this machine (its gunicorn listens there); otherwise `https://api.assassins.network` |
| `ACB_RDV_ANAPI_SERVICE_KEY` | A long random string: `openssl rand -hex 32`. AN-API gets the same value as `RDV_SERVICE_KEY`. |
| `ACB_RDV_INTERNAL_KEY` | Another random string. AN-API gets the same value as `RDV_NOTIFY_KEY`. |
| `ACB_RDV_STATUS_HOST` | `127.0.0.1` when AN-API is on this machine. If AN-API is elsewhere, leave it at `0.0.0.0` and firewall the port (step 3). |
| `ONLINECONFIG_PUBLISH_PORT` | `127.0.0.1:8081` behind nginx; not needed with Traefik (step 4); `80` with no proxy |
| `ONLINECONFIG_ACR_ADDRESS`, `ONLINECONFIG_ACR_PORT` | Where ACR's RendezVous server runs, if ACR clients use this online config too. Defaults: this address, port 21040. |

The other defaults match acb-rdv: ports 21030/21031/21032 and relay ports
40000–40199. The server relay itself is off by default (`ACB_RDV_NAT_RELAY=1`
turns it on); players use AC Relay instead.

## 3. Firewall

| Port | Open to |
|---|---|
| UDP 21030, 21031 | everyone |
| UDP 40000–40199 | everyone, only if the server relay is on (`ACB_RDV_NAT_RELAY=1`; off by default) |
| TCP 80 | everyone, via your proxy |
| TCP 21032 | nobody, unless AN-API runs elsewhere; then only AN-API's IP (it carries the friend-change webhook) |

## 4. Reverse proxy

The game fetches
`http://<RedirectHost>/OnlineConfigService.svc/GetOnlineConfig?onlineConfigID=...`
over **plain HTTP**. The launcher sets `RedirectHost` to
`acb.assassins.network`, so point that name at this server. The request must
never be redirected to HTTPS: the game cannot follow the redirect. A global
http→https redirect breaks it.

### nginx on the host

```nginx
server {
    listen 80;
    server_name acb.assassins.network onlineconfigservice.ubi.com;

    location /OnlineConfigService.svc/ {
        proxy_pass http://127.0.0.1:8081;     # ONLINECONFIG_PUBLISH_PORT
        proxy_set_header Host $host;
        proxy_pass_header Server;             # keep the IIS header the game always got
    }
}
```

### Traefik in a container

Don't publish the port. Attach `onlineconfig` to Traefik's network with a
`docker-compose.override.yml` next to `docker-compose.yml`. Compose picks it up
automatically.

```yaml
services:
  onlineconfig:
    ports: !reset []
    networks: [traefik]
    labels:
      - traefik.enable=true
      - traefik.http.routers.onlineconfig.rule=PathPrefix(`/OnlineConfigService.svc`)
      - traefik.http.routers.onlineconfig.entrypoints=web       # the plain-HTTP entrypoint
      - traefik.http.services.onlineconfig.loadbalancer.server.port=8080

networks:
  traefik:
    external: true      # use the name of your Traefik network
```

A redirect to HTTPS configured on the `web` entrypoint itself (`entryPoints.web.http.redirections`)
applies to every router on it, including this one. If you have that, move the
redirect onto the other routers as middleware, or give this router its own
entrypoint on port 80.

`acb-rdv` does not go through the proxy.

## 5. AN-API

1. Deploy an AN-API version with the `/rdv` blueprint.
2. Set these in AN-API's environment, then restart gunicorn:

   | Variable | Value |
   |---|---|
   | `RDV_SERVICE_KEY` | same as `ACB_RDV_ANAPI_SERVICE_KEY` |
   | `RDV_NOTIFY_KEY` | same as `ACB_RDV_INTERNAL_KEY` |
   | `RDV_NOTIFY_URL` | same machine: leave the default `http://127.0.0.1:21032/internal/notify`. Elsewhere: `http://<acb-rdv server>:21032/internal/notify` |
   | `RDV_LEGACY_PASSWORD` | `ACB` while players still use the old launcher (it logs in with that fixed password); remove afterwards |

3. **AN-API in a container on the same machine:** `127.0.0.1` inside that
   container means the container itself.
   - Give it `extra_hosts: ["host.docker.internal:host-gateway"]`.
   - Use `http://host.docker.internal:21032/internal/notify` as `RDV_NOTIFY_URL`.
   - Make sure its API port is published on the host, and set
     `ACB_RDV_ANAPI_URL` to that address.

## 6. Start

```sh
docker compose up -d --build
docker compose ps                  # both services "healthy"
docker compose logs -f acb-rdv
```

## 7. Check

```sh
# online config through the proxy: SandboxUrl must show the public address
curl "http://acb.assassins.network/OnlineConfigService.svc/GetOnlineConfig?onlineConfigID=ACB"

# on the server
curl http://127.0.0.1:21032/settings    # the .cxb loaded (size, sha1)
curl http://127.0.0.1:21032/clients     # online players
curl http://127.0.0.1:21032/nat         # NAT probe and relay counters
```

Then test with the game:

1. Log in with the new launcher. The acb-rdv log shows `LoginEx` and the
   player logging in.
2. Open the in-game friends list.
3. Send a friend request from the launcher's Friends page to a player who is
   online in game. They get the in-game notification.
4. Play a match between two players on different home connections. Afterwards,
   `/nat` shows which probes were answered and whether the relay carried
   traffic.

Use `docs/NAT_TRAVERSAL.md` to decide which NAT features to keep.

## 8. Afterwards

- Once everyone has the new launcher, remove `RDV_LEGACY_PASSWORD` from AN-API.
- When the CXB library exists, switch to XML settings. The commented lines in
  `docker-compose.yml` and `docs/CXB.md` describe how.
- To update: `git pull && docker compose up -d --build`.

## Troubleshooting

| Symptom | Look at |
|---|---|
| Game can't reach the server at start | The `curl` in step 7: HTTP must answer on port 80 without a redirect. `SandboxUrl` must be the public address. |
| Login fails for everyone | acb-rdv log: `backend unavailable` means `ACB_RDV_ANAPI_URL` or the service key is wrong. AN-API answers 401 when the keys differ. |
| Login fails for old-launcher players only | `RDV_LEGACY_PASSWORD=ACB` isn't set on AN-API |
| Friend changes on the launcher don't show in game | AN-API can't reach `RDV_NOTIFY_URL`, or `RDV_NOTIFY_KEY` ≠ `ACB_RDV_INTERNAL_KEY` (acb-rdv answers 401) |
| Players connect but can't join each other | `/nat` and the log's `InitiateProbe` lines; UDP 40000–40199 open? |
| Empty settings / missing modes | `/settings`: the `.cxb` path, and whether uid 1000 can read `data/` |
