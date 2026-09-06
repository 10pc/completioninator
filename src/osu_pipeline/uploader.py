"""YouTube uploads via Data API v3 (Milestone 6).

Auth: one-time browser flow (`auth-youtube`, Desktop-app OAuth client),
refresh token persisted to the token path; every later run is headless.
Uploads use the resumable protocol with backoff, so a killed transfer
restarts where it left off. Privacy defaults to unlisted until the Google
project passes its audit for public uploads.
"""

from __future__ import annotations

import logging
import random
import time
import urllib.parse
from pathlib import Path

log = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/youtube.upload"]
RETRIABLE_STATUS = {500, 502, 503, 504}
MAX_RETRIES = 5


class UploadError(Exception):
    """Fatal-for-this-video upload problem (recorded, queue continues)."""


class MissingCredentials(UploadError):
    """No client ID/secret configured."""


def _client_config(client_id: str, client_secret: str) -> dict:
    return {
        "installed": {
            "client_id": client_id,
            "client_secret": client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": ["http://localhost"],
        }
    }


def _extract_code(text: str) -> str:
    """Accept a full callback URL or a bare authorization code."""
    text = text.strip().strip("\"'")
    parsed = urllib.parse.urlparse(text)
    if parsed.query:
        code = urllib.parse.parse_qs(parsed.query).get("code", [None])[0]
        if code:
            return code
    if parsed.scheme or parsed.netloc:
        raise UploadError("pasted URL has no authorization code (approval may have failed)")
    if not text or len(text) > 512 or " " in text:
        raise UploadError("could not find an authorization code in the pasted text")
    return text


def run_auth_manual(client_id: str, client_secret: str, token_path: Path,
                    port: int = 8080) -> None:
    """Manual authorization: print URL, read pasted callback from stdin.

    For environments where the browser cannot reach a local callback server
    (proxied desktops, VNC quirks): approve in ANY browser, copy the
    unreachable localhost URL from the address bar, paste it here. No local
    server binds, so no published ports are needed.
    """
    from google_auth_oauthlib.flow import Flow

    flow = Flow.from_client_config(
        _client_config(client_id, client_secret), SCOPES,
        redirect_uri=f"http://localhost:{port}/")
    auth_url, _ = flow.authorization_url(access_type="offline", prompt="consent")
    print("Open this URL, approve, then paste the full localhost URL back here:")
    print(auth_url)
    try:
        pasted = input("callback URL or code: ")
    except EOFError as exc:
        raise UploadError("no callback pasted (empty stdin)") from exc
    flow.fetch_token(code=_extract_code(pasted))
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(flow.credentials.to_json())
    print(f"authorized; credentials saved to {token_path}")


def run_auth_flow(client_id: str, client_secret: str, token_path: Path,
                   port: int = 8080, host: str = "localhost"):
    """One-time browser authorization. Run with the port published on VNC box.

    The callback server binds all interfaces (bind_addr) while the redirect
    URI stays on `host`: inside Docker, `localhost` can resolve to ::1 and a
    0.0.0.0 redirect is not routable, so the two must differ. Exposure stays
    loopback-only via `-p 127.0.0.1:PORT:PORT` on the run command.
    Prints the consent URL as well (for browsers that don't auto-open).
    Saves refresh-capable credentials to token_path.
    """
    from google_auth_oauthlib.flow import InstalledAppFlow

    flow = InstalledAppFlow.from_client_config(_client_config(client_id, client_secret), SCOPES)
    creds = flow.run_local_server(host=host, bind_addr="0.0.0.0", port=port,
                                  open_browser=False)
    token_path = Path(token_path)
    token_path.parent.mkdir(parents=True, exist_ok=True)
    token_path.write_text(creds.to_json())
    # Never log the token itself; the path is enough for the operator.
    print(f"authorized; credentials saved to {token_path}")
    return creds


def load_credentials(token_path: Path):
    """Load stored credentials, refreshing if expired. None when absent."""
    from google.oauth2.credentials import Credentials

    token_path = Path(token_path)
    if not token_path.exists():
        return None
    creds = Credentials.from_authorized_user_file(str(token_path), SCOPES)
    if creds and creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request

        creds.refresh(Request())
        token_path.write_text(creds.to_json())
    return creds if creds and creds.valid else None


def build_service(creds):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=creds, cache_discovery=False)


def render_title(template: str, day: str, clips: int, span: str | None) -> str:
    return template.format(day=day, clips=clips, span=span or day)


def upload_video(service, file_path: Path, title: str, description: str,
                 category_id: str, privacy: str, chunks_mb: int = 8) -> str:
    """Resumable upload with backoff. Returns the YouTube video ID."""
    from googleapiclient.errors import HttpError
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(file_path), mimetype="video/*", resumable=True,
                            chunksize=chunks_mb * 1024 * 1024)
    request = service.videos().insert(
        part="snippet,status",
        body={"snippet": {"title": title, "description": description,
                          "categoryId": category_id},
              "status": {"privacyStatus": privacy,
                         "selfDeclaredMadeForKids": False}},
        media_body=media,
    )
    response, error, retry = None, None, 0
    while response is None:
        try:
            _, response = request.next_chunk()
        except HttpError as exc:
            if exc.resp.status in RETRIABLE_STATUS:
                error = f"retriable HTTP {exc.resp.status}"
            else:
                raise UploadError(f"youtube error {exc.resp.status}: {exc.content!r}") from exc
        except (OSError, TimeoutError) as exc:
            error = f"retriable network error: {exc}"
        if error is not None:
            retry += 1
            if retry > MAX_RETRIES:
                raise UploadError(f"upload failed after {MAX_RETRIES} retries: {error}")
            sleep_s = random.random() * 2 ** retry
            log.warning("%s; sleeping %.1fs", error, sleep_s)
            time.sleep(sleep_s)
            error = None
    video_id = (response or {}).get("id")
    if not video_id:
        raise UploadError(f"upload finished without a video id: {response!r}")
    return video_id


def video_url(video_id: str) -> str:
    return f"https://youtu.be/{video_id}"
