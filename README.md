# osu-completionist pipeline — automated .osr → danser → grid → daily video

> apologies for the vibecoded slop. i swear i know how to code, i'm just very lazy

Windows dev (this machine):

```powershell
pip install -r requirements.txt
pip install -e .
osu-pipeline --help
pytest
```

Use `--scan-root` / `--db` flags (or `PIPELINE_REPLAYS_DIR` /
`PIPELINE_DATABASE` env) to point at real folders; `pytest` is the
source of truth and stays green on Windows (ffmpeg-dependent paths
are unit-tested with stubs).

Ubuntu render server (run by operator):

```bash
# 1. Mount NAS on the host (read-only bind into container via compose.yml)
ls /mnt/nas/osu-replays
docker compose build
docker compose run --rm pipeline discover
docker compose run --rm pipeline render --limit 30
docker compose run --rm pipeline status
ls data/rendered/*/
# or the whole close-out in one go:
docker compose run --rm pipeline daily --limit 100
```

Steady state is two host cron jobs (3 render workers, ~1 min/map at 720p30):

```bash
# crontab -e
# intra-day burn, every 30 min:
*/30 * * * * cd ~/completioninator && docker compose run --rm pipeline render --limit 25
# nightly close-out at 03:00 UTC (discover, render, compose, publish backlog):
0 3 * * * cd ~/completioninator && docker compose run --rm pipeline daily --limit 100 --upload
```

`daily` is just the stages chained with one summary line
(`discovered new=X rendered=Y failed=Z composed=ok|skipped|failed
uploaded=youtube:ok,instagram:unconfigured`); a day is composable whenever
it has rendered clips, and stragglers roll forward, so midnight renders
never break batching. `--upload` publishes the latest daily missing a
success row per configured platform (retrying yesterday's failures) and
skips platforms without credentials.

Rendering runs N parallel workers (`--workers`, default from `[render]
workers`, currently 3): claims are atomic so no replay renders twice, and
the beatmap fetch phase is serialized while the minutes-long encodes stay
parallel. Drop to `--workers 1` if danser ever acts up under concurrency.

Watch a batch and stop it mid-run:

```bash
docker compose run --rm pipeline progress            # today's UTC batch
docker compose run --rm pipeline progress 2026-03-29 # any day
docker compose run --rm pipeline stop                # loop exits after its current job
```

Compose the daily grid video (rolling batch: every rendered-but-uncomposited
clip from any day; keeps the longest `max_clips`, default 12; the rest roll
forward to the next batch):

```bash
docker compose run --rm pipeline compose
docker compose run --rm pipeline compose --max-clips 15
ls data/daily/
```

Fixed 1080p canvas; the grid starts full and shrinks as clips finish until
the longest plays alone. Every tile is exactly 16:9 like the sources, so
plain scaling stays aspect-exact with no letterbox bars — including
mid-morph, since lerps between 16:9 boxes can't distort. Grids are centered,
ragged rows included. Resizes animate as 1s glides (dying tiles shrink out);
audio is one continuous mix of all playing clips, so segment joins never
glitch it. Header reads `dd-mm-yyyy | X maps` (`header_extra` appends future
API-sourced data). The video ends with an outro: content fades out, then live
completion stats from the osucomplete profile (`1,133/147,163` + `0.73%`)
fade in over black, hold, and fade out. Manual `[video] completion_*` values
cover a fetch outage; with neither, the video simply ends after the last clip.

`stop` drops a sentinel next to the database, so it reaches a running loop
from any other container invocation; Ctrl+C works too (exit 130). The
interrupted job returns to `pending` automatically on the next run — nothing
is ever half-recorded. Per-map timeout defaults to 2h (`timeout_seconds`);
30-minute+ maps render at ~2.8x, so even those finish with wide headroom.

Each job writes its full danser log to `/data/logs/job-<id>.log` (console shows
the tail only). Rendering refuses to start below `min_free_disk_gb` free space
(default 5GB) so a full disk can't corrupt the queue.

Danser's own map database persists at `/data/db/danser.db` (symlinked into
place on every container start), so the Songs import happens once, not per
job. A `rendering` claim older than `stale_after_minutes` (default 180)
counts as crashed and is requeued — overlapping runs can no longer
double-render a live job. First-time `Beatmap not found` failures retry
once automatically (parallel-import races); only repeats park.

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

danser look — gameplay config and skins:

```bash
# gameplay visuals live in danser/settings/pipeline.json (sections General,
# Recording, Skin, Gameplay, ...). Partial files are fine: danser fills
# defaults for missing keys, and it rewrites the file on every run, so the
# repo copy stays the source of truth.
```

Useful `Gameplay` toggles (every HUD element takes `Show`/`Scale`/`Opacity`):
`Score`, `HpBar`, `ComboCounter`, `PPCounter`, `HitCounter`,
`HitErrorMeter`, `KeyOverlay`, `ScoreBoard`, `StrainGraph`, `Mods`,
`ShowResultsScreen` (+`ResultsScreenTime`), `Underlay.Path` (PNG backdrop).
`Skin` section: `CurrentSkin`, `UseColorsFromSkin`, `UseBeatmapColors`,
plus `Cursor` (`UseSkinCursor`, `Scale`, `TrailScale`, `ForceLongTrail`).
CLI flags the pipeline already passes per render: `-replay=… -record
-out=… -settings=pipeline -skip`, plus anything in `[render] extra_args`.

Custom skin:

```bash
# 1. Unpack your skin (.osk files are zips) into the repo:
#    danser/skins/MySkin/skin.ini (+ assets)
# 2. Select it:
#    config.toml [render] skin = "MySkin"   (or PIPELINE_DANSER_SKIN)
# 3. Rebuild so the image picks up the folder:
docker compose build
docker compose run --rm pipeline render --limit 1
```

The renderer merges `CurrentSkin` into the container's settings profile on
every run ( danser would otherwise keep whichever skin it saw last). If a
render still shows the default skin, check `/data/logs/job-<id>.log` for
`SkinManager: Skin "..." loaded` — a misspelled folder name falls back
silently.

YouTube uploads (one-time setup, then automatic):

```bash
# 1. Google Cloud: project + YouTube Data API v3 + Desktop OAuth client
#    (consent scope: youtube.upload only). Secrets stay in env, never in git:
#    compose.override.yml environment:
#      PIPELINE_YOUTUBE_CLIENT_ID: "...apps.googleusercontent.com"
#      PIPELINE_YOUTUBE_CLIENT_SECRET: "..."
# 2. One-time browser authorization ON the server (VNC browser):
docker compose run --rm -p 127.0.0.1:8080:8080 pipeline auth-youtube --port 8080
# 3. Upload any daily (skips already-uploaded unless --force):
docker compose run --rm pipeline upload 2026-09-06
docker compose run --rm pipeline status   # shows recent uploads
```

Uploads default to `unlisted` (unverified Google projects can't publish
public — flip `privacy` after the project audit). Transfers are resumable
with backoff, and every upload is recorded in SQLite so restarts can't
duplicate. Refresh tokens renew silently; re-run `auth-youtube` only if
Google revokes access.

Storage: per-map renders are deleted automatically once their day is safely
on YouTube (`[youtube] prune_after_upload`, on by default). Replays, DB
rows, daily videos, songs and logs are always kept — renders are pure
derivatives and rebuild exactly. Manual control:

```bash
docker compose run --rm pipeline prune --day 2026-09-06 --dry-run
docker compose run --rm pipeline prune --day 2026-09-06
docker compose run --rm pipeline prune --backfill   # pre-membership renders
```

Instagram uploads (feed VIDEO posts — landscape dailies aren't Reels-shaped):

```bash
# 1. Meta: professional IG account + linked FB Page + app User token with
#    instagram_basic, instagram_content_publish (+ pages perms). Set in
#    compose.override.yml environment (never in git):
#      PIPELINE_INSTAGRAM_USER_ID: "178414..."
#      PIPELINE_INSTAGRAM_TOKEN: "..."
# 2. Publish (bytes upload straight from the server, no public URL needed):
docker compose run --rm pipeline upload 2026-09-06 --platform instagram
```

Each publish is container-create → byte upload → status poll → publish →
permalink, all tracked in the same `uploads` table (`youtube` and
`instagram` rows per day are independent). Tokens live ~60 days; an error
mentioning expiry means re-issue. `--force` re-publishes either platform.

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

Hands-free sync — a watcher runs at logon and syncs ~30s after each new
replay (plus a periodic safety sweep in case an event is ever missed):

```powershell
.\scripts\install-watcher.ps1 -Source 'C:\Users\user\osu!\Replays' -Destination '\\nas\osu\replays'
# check it: Get-ScheduledTask OsuReplayWatcher; Get-Content $env:TEMP\osu-watch-replays.log -Tail 5
# remove it: .\scripts\install-watcher.ps1 -Uninstall
```

Gaming PC sync — Linux/rsync alternative:

```bash
rsync -a --ignore-existing ~/osu/replays/ nas:/srv/osu/replays/
```
Upload as `12345.osr.part`, rename to `12345.osr` when complete.
