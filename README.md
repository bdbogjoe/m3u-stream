# m3u-stream

A small Flask app that fetches one or more M3U playlists, lets you pick a channel from a web UI, and either casts it to a DLNA TV, opens it in your browser as MP4, or starts a local HTTP relay you can play in mpv / VLC.

## Features

- Pick channels from a browser, filterable by **source** (when multiple M3Us are configured) and **group**.
- **Cast to TV** over DLNA (UPnP `AVTransport`) using SSDP unicast discovery — works in Docker host *and* bridge networking.
- **Watch in browser** — a `/stream.mp4` endpoint serves a fragmented MP4 (audio re-encoded to AAC) so Chrome plays it natively.
- **Start stream** — exposes `/stream.ts` (MPEG-TS) for any LAN client (mpv, VLC, smart TV, etc.).
- Multiple M3U URLs, optionally named (`name=url,name=url`).
- Manual reload button to re-fetch the playlists without restarting.

## Quick start

Copy the example env file and fill it in:

```sh
cp .env.example .env
# edit .env to set at least M3U_URL (and optionally TV_IP)
```

Then:

```sh
docker compose up --build
```

Open http://localhost:8080.

### Pulling the published image instead of building

After the GitHub Action publishes the image to Docker Hub:

```sh
IMAGE=<your-dockerhub-user>/m3u-stream:latest docker compose up
```

(or set `IMAGE=…` in `.env`).

### Bridge networking

By default the compose file uses host networking (simpler for DLNA / LAN reachability). To run in bridge mode:

```sh
docker compose -f docker-compose.yml -f docker-compose.bridge.yml up
```

In bridge mode you **must** set `HOST_IP` explicitly to the host's LAN IP — the auto-detected source IP from inside the container is the Docker bridge address (e.g. `172.17.0.x`) which the TV can't reach.

> **Casting requires host networking.** DLNA discovery uses SSDP, and many TVs (LG webOS in particular) only answer the multicast probe — Docker's bridge network does not forward multicast, so discovery from a bridge-mode container always fails with `could not discover AVTransport endpoint`. Watch-in-browser and the relay endpoints work fine in bridge mode; only `Cast to TV` requires `network_mode: host`.

## Configuration (env vars)

| Var          | Required | Default          | Notes                                                                    |
|--------------|----------|------------------|--------------------------------------------------------------------------|
| `M3U_URL`    | yes      | —                | One or more URLs. Comma-separated. Each item can be a URL or `name=url`. |
| `TV_IP`      | no       | —                | If unset, the *Cast to TV* button is hidden and DLNA discovery is skipped. |
| `WEB_PORT`   | no       | `8080`           | Web UI port.                                                             |
| `RELAY_PORT` | no       | `8888`           | HTTP relay port (`/stream.ts`, `/stream.mp4`).                           |
| `HOST_IP`    | no       | auto             | Auto-detected via `ip route get $TV_IP` (or `1.1.1.1` if no TV). Required in bridge mode. |
| `RELAY_BASE_URL` | no   | —                | Public base URL for the relay (e.g. `https://stream.example.com`). Used only when the request reaches the app through a reverse proxy (any `X-Forwarded-*` header present). DLNA cast always uses `http://$HOST_IP:$RELAY_PORT`. |
| `WEB_BASE_URL`   | no   | —                | Public base URL for the web UI (e.g. `https://m3u.example.com`). Used in the relay's "no stream running" 503 page so the link points at the public UI when reached via the proxy. |
| `AUTH_USER`, `AUTH_PASS` | no | —          | When both are set, HTTP basic auth is required for proxied requests only. LAN-direct requests stay open so DLNA cast / mpv / VLC keep working without credentials. |
| `EPG_URL`    | no       | —                | XMLTV URL (plain or `.gz`). When set, each tile shows the current programme matched by `tvg-id`. Refreshed every 6h in the background. Channels without a `tvg-id` (or with no matching XMLTV entry) show no programme. |
| `AUTH_TRUSTED_CIDRS` | no | RFC1918 + loopback | Comma-separated CIDRs whose clients bypass auth even when reaching the app through the proxy (read from `X-Forwarded-For`). Default trusts any LAN / private IPv4 + IPv6. |
| `IMAGE`      | no       | `m3u-stream:local` | Image reference used by `docker-compose.yml`.                            |

### Multi-source M3U examples

```sh
M3U_URL=https://example.com/playlist.m3u
M3U_URL=https://example.com/france.m3u,https://example.com/sport.m3u
M3U_URL=france=https://a/x.m3u,sport=https://b/y.m3u
```

## Running without Docker

```sh
pip install -r requirements.txt
M3U_URL=… TV_IP=… python -m app.main
```

You'll need `ffmpeg` and `iproute2` available on the host.

## Endpoints

| Method | Path                | Notes                                                  |
|--------|---------------------|--------------------------------------------------------|
| GET    | `/`                 | Web UI.                                                |
| GET    | `/status`           | JSON: cast status (`current`, `casting`).              |
| POST   | `/cast`             | `{channel_id}` — DLNA cast that channel to the TV.     |
| POST   | `/stop`             | Stop the DLNA cast.                                    |
| POST   | `/reload`           | Re-fetch all configured M3U URLs.                      |
| GET    | `/healthz`          | Liveness probe.                                        |
| GET    | `/all.m3u`          | M3U playlist of all channels, with our relay's stream URLs. |
| GET    | `/<source>.m3u`     | Same, restricted to one source (e.g. `/france.m3u`).   |
| GET    | `/<src1>-<src2>.m3u`| Same, restricted to a dash-joined set (e.g. `/france-sport.m3u`). |
| GET    | `:RELAY_PORT/<id>/stream.ts`   | MPEG-TS for a channel (TV cast / mpv / VLC). |
| GET    | `:RELAY_PORT/<id>/stream.mp4`  | Fragmented MP4 (Chrome native; iOS 302→HLS). |
| GET    | `:RELAY_PORT/<id>/stream.m3u8` | HLS playlist for a channel.                  |

## Releases

The GitHub Actions workflow at `.github/workflows/docker.yml` builds a multi-arch image (`linux/amd64` + `linux/arm64`) and pushes it to Docker Hub on every push to `main` / `devel` and on `v*.*.*` tags. Required repository secrets:

- `DOCKERHUB_USERNAME`
- `DOCKERHUB_TOKEN`

