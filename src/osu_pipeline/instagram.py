"""Instagram publishing via Graph API, Facebook-Login flavor (Milestone 7).

Flow (feed VIDEO post — our 16:9 landscape + multi-minute dailies are not
Reels-shaped, and VIDEO posts take them as-is):
    1. POST /{ig-id}/media?upload_type=resumable&media_type=VIDEO&caption
       -> container id (bytes uploaded directly; no public URL needed)
    2. POST rupload.facebook.com/ig-api-upload/.../{container} (offset 0)
    3. Poll /{container}?fields=status_code until FINISHED
    4. POST /{ig-id}/media_publish?creation_id=... -> media id
    5. GET /{media-id}?fields=permalink for the real URL

Auth: long-lived User token with instagram_basic, instagram_content_publish
(+ pages_read_engagement etc. per Meta docs), pasted via env. Tokens live
~60 days; an expired/invalid token surfaces error 190 with instructions to
re-issue (automatic app-id/secret exchange is a future upgrade).
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

log = logging.getLogger(__name__)

GRAPH = "https://graph.facebook.com"
RUPLOAD = "https://rupload.facebook.com/ig-api-upload"


class InstagramError(Exception):
    """Fatal-for-this-video publishing problem (recorded, queue continues)."""


def _api(method: str, url: str, params: dict, data: bytes | None = None,
         headers: dict | None = None, timeout: int = 60) -> object:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    req = urllib.request.Request(f"{url}?{qs}" if qs else url, data=data,
                                 headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", errors="replace")
        except Exception:
            detail = ""
        raise InstagramError(f"meta HTTP {exc.code}: {detail[:300]}") from exc
    except Exception as exc:
        raise InstagramError(f"meta request failed: {exc}") from exc


def _check(payload: object, what: str) -> dict:
    if not isinstance(payload, dict) or "error" in payload:
        raise InstagramError(f"{what} rejected: {payload!r}"[:500])
    return payload


def create_session(api_version: str, ig_user_id: str, token: str, caption: str,
                   timeout: int = 60) -> str:
    payload = _check(_api("POST", f"{GRAPH}/{api_version}/{ig_user_id}/media", {
        "upload_type": "resumable", "media_type": "VIDEO", "caption": caption,
        "access_token": token}, timeout=timeout), "container create")
    container_id = payload.get("id")
    if not container_id:
        raise InstagramError(f"no container id in {payload!r}"[:300])
    return str(container_id)


def upload_bytes(api_version: str, container_id: str, token: str, path: Path,
                 timeout: int = 600) -> None:
    data = Path(path).read_bytes()
    url = f"{RUPLOAD}/{api_version}/{container_id}"
    headers = {"Authorization": f"OAuth {token}", "offset": "0",
               "file_size": str(len(data)), "Content-Type": "application/octet-stream"}
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            resp.read()
    except urllib.error.HTTPError as exc:
        raise InstagramError(f"rupload HTTP {exc.code}") from exc
    except Exception as exc:
        raise InstagramError(f"rupload failed: {exc}") from exc


def wait_finished(api_version: str, container_id: str, token: str,
                  timeout_s: int = 600, interval_s: int = 10) -> None:
    deadline = time.time() + timeout_s
    while True:
        payload = _check(_api(
            "GET", f"{GRAPH}/{api_version}/{container_id}",
            {"fields": "status_code", "access_token": token}), "status poll")
        code = payload.get("status_code")
        if code == "FINISHED":
            return
        if code in ("ERROR", "EXPIRED"):
            raise InstagramError(f"container {code}: {payload.get('status')}")
        if time.time() > deadline:
            raise InstagramError(f"container still {code} after {timeout_s}s")
        time.sleep(interval_s)


def publish_container(api_version: str, ig_user_id: str, container_id: str,
                      token: str, timeout: int = 60) -> str:
    payload = _check(_api("POST", f"{GRAPH}/{api_version}/{ig_user_id}/media_publish", {
        "creation_id": container_id, "access_token": token}, timeout=timeout), "publish")
    media_id = payload.get("id")
    if not media_id:
        raise InstagramError(f"no media id in {payload!r}"[:300])
    return str(media_id)


def permalink(api_version: str, media_id: str, token: str, timeout: int = 60) -> str:
    payload = _check(_api("GET", f"{GRAPH}/{api_version}/{media_id}", {
        "fields": "permalink", "access_token": token}, timeout=timeout), "permalink")
    return str(payload.get("permalink") or "")


def publish_video(api_version: str, ig_user_id: str, token: str, path: Path,
                  caption: str) -> tuple[str, str]:
    """Full chain. Returns (media_id, permalink). Raises InstagramError."""
    container_id = create_session(api_version, ig_user_id, token, caption)
    log.info("instagram container %s", container_id)
    upload_bytes(api_version, container_id, token, path)
    wait_finished(api_version, container_id, token)
    media_id = publish_container(api_version, ig_user_id, container_id, token)
    try:
        link = permalink(api_version, media_id, token)
    except InstagramError as exc:
        log.warning("permalink lookup failed (upload is live anyway): %s", exc)
        link = ""
    return media_id, link
