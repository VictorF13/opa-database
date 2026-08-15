# Remote database access

The `docker-compose.yml` Postgres+PostGIS instance binds to `0.0.0.0`, so
it's reachable from any device on the same network, not just the machine
running Docker. This project accesses it remotely over
[Tailscale](https://tailscale.com/), using MagicDNS names rather than raw
IPs.

## Direct Postgres connection

For `psql`, DBeaver, Postico, TablePlus, or any other Postgres client:

```text
postgresql://<user>:<password>@<tailscale-hostname>:5432/opa
```

Replace `<tailscale-hostname>` with the Docker host's Tailscale MagicDNS
name (e.g. `opa-server`), and `<user>`/`<password>` with whatever
`DB_USER`/`DB_PASSWORD` are actually set to in `.env` - there's no
universal default account to fall back on; the original `opa` superuser
was retired (`NOLOGIN`) in favor of a per-deployment admin account.

## Browser-based access (Adminer)

`docker-compose.yml` also runs [Adminer](https://www.adminer.org/), a
lightweight web-based SQL client, on port 8080, so no local Postgres
client install is needed.

- **On the host machine, or over plain Tailscale IP/hostname**:
  `http://<tailscale-hostname>:8080`
- **Clean HTTPS URL, no port** (via `tailscale serve`): once configured
  (see below), the same UI is reachable at
  `https://<tailscale-hostname>.<tailnet-name>.ts.net`.

Adminer login: System **PostgreSQL**, Server `postgres` (the
`docker-compose.yml` service name), then the same user/password/database
as above.

### Setting up `tailscale serve` for a clean URL

`tailscale serve --bg 8080` reverse-proxies Adminer behind a proper HTTPS
URL instead of `host:8080`. This requires two one-time steps on the Docker
host:

1. Enable the **Serve** feature at the tailnet level. The first time you
   run `tailscale serve`, Tailscale prints a one-time web approval link.
   Open it and approve.
2. Grant your user "operator" rights so `tailscale serve` doesn't need
   `sudo` every time:

   ```bash
   sudo tailscale set --operator=$USER
   ```

After that, `tailscale serve --bg 8080` persists in the background across
reboots of the Tailscale daemon.

## Why not pgAdmin

Adminer was chosen over pgAdmin for now: it's a single static binary with
no separate login/session setup, which was simpler to stand up first. If
pgAdmin (a more Postgres-native UI, but heavier) ever replaces it, this
document and `docker-compose.yml` should be updated together.
