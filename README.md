# osu-completionist pipeline — Milestone 2: Rendering

> apologies for the vibecoded slop. i swear i know how to code, i'm just very lazy

Windows dev (this machine):

```powershell
pip install -r requirements.txt
pip install -e .
osu-pipeline discover --scan-root ./fixtures/replays --db ./data/db/pipeline.sqlite
osu-pipeline status --db ./data/db/pipeline.sqlite
pytest
```

Ubuntu render server (run by operator):

```bash
# 1. Mount NAS on the host (read-only bind into container via compose.yml)
ls /mnt/nas/osu-replays
docker compose build
docker compose run --rm pipeline discover
docker compose run --rm pipeline render --limit 30
docker compose run --rm pipeline status
ls data/rendered/*/
```

Steady state is a host cron job (single worker, ~1 min/map at 720p30):

```bash
# crontab -e — every 30 min, up to 25 renders per slot
*/30 * * * * cd ~/completioninator && docker compose run --rm pipeline discover && docker compose run --rm pipeline render --limit 25
```

Watch a batch and stop it mid-run:

```bash
docker compose run --rm pipeline progress            # today's UTC batch
docker compose run --rm pipeline progress 2026-03-29 # any day
docker compose run --rm pipeline stop                # loop exits after its current job
```

`stop` drops a sentinel next to the database, so it reaches a running loop
from any other container invocation; Ctrl+C works too (exit 130). The
interrupted job returns to `pending` automatically on the next run — nothing
is ever half-recorded. Per-map timeout defaults to 2h (`timeout_seconds`);
30-minute+ maps render at ~2.8x, so even those finish with wide headroom.

Each job writes its full danser log to `/data/logs/job-<id>.log` (console shows
the tail only). Rendering refuses to start below `min_free_disk_gb` free space
(default 5GB) so a full disk can't corrupt the queue.

Optional: skip danser's per-job GitHub update check (saves ~4s/job and removes
a network dependency). First verify the flag exists in this build:

```bash
docker compose run --rm --entrypoint sh pipeline -c '/opt/danser/danser-cli -h 2>&1 | grep -i update'
```

If `-noupdatecheck` is listed, enable it via `config.toml`:

```toml
[render]
extra_args = ["-noupdatecheck"]
```

Never add `-nodbcheck` — danser must import newly downloaded maps every run.

Beatmaps resolve through the hinamizawa.ai mirror by default — direct MD5
lookup, no key, ranked + graveyard coverage. Downloaded `.osz` files are
cached in `/data/beatmaps/songs` for danser. (`backend = "mino"` in
`config.toml` switches back to catboy.best + official-API fallback.)

If the mirror misses a map hash, the pipeline can fall back to the official
osu! API (`beatmaps/lookup?checksum=`). That needs a free OAuth app
(https://osu.ppy.sh/home/account/edit — create an application, no callback
URL needed for this flow), then:

```bash
# on the render server (or compose.override.yml environment:)
export PIPELINE_OSU_CLIENT_ID=12345
export PIPELINE_OSU_CLIENT_SECRET='...'
docker compose run --rm pipeline requeue   # retry previously failed jobs
docker compose run --rm pipeline render --limit 10
```

Only `.osr` files carry a beatmap MD5 (no beatmap id inside), so hash
resolution is the only replay-native key — the manual escape hatch is
`render --beatmapset-id <id>` when you know the set.

Failure triage (`progress` shows the short form, `/data/logs/job-<id>.log`
the full danser output):

- `transient: …` (mirror 503/pressure, timeouts) — job goes back to
  `pending` by itself; the attempts cap still bounds endless retries.
- `no_beatmap: …` — hash found on no mirror (deleted map?). No local
  sourcing exists by design — everything is remote — so these stay parked
  until the map reappears upstream.
- `danser: beatmap not found … updated since play` — the set downloaded
  fine but the replay's hash is absent from its current version (mirrors
  only host the latest); parked as `unrenderable`, same remote-only rule.

Rules: never write to `/replays`, only `.osr` is processed (never `.part`),
day boundary is UTC midnight, identity is `(path, sha256)`.

Gaming PC sync — Windows/SMB (operator runs on gaming PC):

```powershell
# one-off flags, or set $env:OSU_REPLAYS_SOURCE / $env:NAS_REPLAYS_DEST
.\scripts\sync-replays.ps1 -WhatIf
.\scripts\sync-replays.ps1 -Source 'C:\Users\user\osu!\Replays' -Destination '\\nas\osu\replays'
```

Only new `.osr` files are copied (never overwritten/deleted); they stage in
`staging/` then move into place so the scanner never sees a partial file.

```powershell
# later: hourly background sync (scheduling deferred for now)
# schtasks /create /tn OsuReplaySync /tr "powershell -File C:\path\to\sync-replays.ps1" /sc HOURLY
```

Gaming PC sync — Linux/rsync alternative:

```bash
rsync -a --ignore-existing ~/osu/replays/ nas:/srv/osu/replays/
```
Upload as `12345.osr.part`, rename to `12345.osr` when complete.
