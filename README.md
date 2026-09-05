# osu-completionist pipeline — Milestone 1: Replay Discovery

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
docker compose run --rm pipeline status
```

Rules: never write to `/replays`, only `.osr` is processed (never `.part`),
day boundary is UTC midnight, identity is `(path, sha256)`.

Gaming PC rsync convention (operator runs on gaming PC):

```bash
rsync -a --ignore-existing ~/osu/replays/ nas:/srv/osu/replays/
```
Upload as `12345.osr.part`, rename to `12345.osr` when complete.
