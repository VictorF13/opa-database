# Remote database access

The `docker-compose.yml` Postgres+PostGIS instance and Adminer bind to
`BIND_HOST`, a `.env` variable defaulting to `127.0.0.1` (localhost-only,
the right default for a fresh local dev setup with no remote access
needed). To reach them over [Tailscale](https://tailscale.com/) instead,
set `BIND_HOST` to the Docker host's own Tailscale IP (`tailscale ip
-4`). Docker's port publishing needs a literal IP here; it can't resolve
a Tailscale MagicDNS hostname directly, only raw IPs. That IP is stable
for the life of the device (it doesn't change on reconnect or reboot),
so it's safe to set once. Either way, connecting *to* it still uses the
Tailscale MagicDNS hostname (e.g. `opa-server`), not the raw IP; only
the bind side needs the IP.

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

- **Primary way, for now**: `http://<tailscale-hostname>:8080`.
- **Optional, not currently set up**: a clean HTTPS URL with no port,
  via `tailscale serve` (see below) - once configured, the same UI is
  reachable at `https://<tailscale-hostname>.<tailnet-name>.ts.net`.

Adminer login: System **PostgreSQL**, Server `postgres` (the
`docker-compose.yml` service name), then the same user/password/database
as above.

### Setting up `tailscale serve` for a clean URL (optional, not in use)

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
