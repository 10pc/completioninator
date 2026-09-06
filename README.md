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
docker compose run --rm pipeline render --limit 1
docker compose run --rm pipeline status
ls data/rendered/*/
```

Beatmaps come from the catboy.best mirror automatically (no key needed);
downloaded `.osz` files are cached in `/data/beatmaps/songs` for danser.

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
