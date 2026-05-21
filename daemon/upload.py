"""Backblaze B2 uploads via the native B2 HTTP API (no boto3 / b2sdk needed).

We use B2's API v3 directly with urllib. Authorization tokens are cached for
their lifetime (24h); upload URLs are fetched per upload (they're cheap and
per-upload-URL tokens are recommended by B2 for parallel uploads).
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError

import config


class B2Error(RuntimeError):
    pass


@dataclass
class _AuthState:
    api_url: str
    download_url: str
    auth_token: str
    bucket_id: str
    expires_at: float  # unix seconds; we treat tokens as good for ~23h


_lock = threading.Lock()
_state: _AuthState | None = None


def _http_json(url: str, headers: dict, data: bytes | None = None, method: str | None = None) -> dict:
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        raise B2Error(f"B2 HTTP {e.code} on {method or 'GET'} {url}: {body[:500]}")


def _authorize() -> _AuthState:
    if not config.b2_enabled():
        raise B2Error("B2 credentials are not configured (PODCAST_B2_KEY_ID / _APP_KEY / _BUCKET).")
    creds = f"{config.B2_KEY_ID}:{config.B2_APP_KEY}".encode()
    headers = {"Authorization": "Basic " + base64.b64encode(creds).decode()}
    data = _http_json(
        "https://api.backblazeb2.com/b2api/v3/b2_authorize_account",
        headers=headers,
    )
    api = data["apiInfo"]["storageApi"]
    api_url = api["apiUrl"]
    download_url = api["downloadUrl"]
    auth_token = data["authorizationToken"]

    # Resolve bucket name → bucket id.
    account_id = data.get("accountId") or api.get("accountId") or ""
    bucket_data = _list_buckets(api_url, auth_token, account_id)
    matches = [b for b in bucket_data.get("buckets", []) if b.get("bucketName") == config.B2_BUCKET]
    if not matches:
        raise B2Error(f"bucket {config.B2_BUCKET!r} not found / not accessible with this key")
    bucket_id = matches[0]["bucketId"]

    return _AuthState(
        api_url=api_url,
        download_url=download_url,
        auth_token=auth_token,
        bucket_id=bucket_id,
        expires_at=time.time() + 23 * 3600,
    )


def _list_buckets(api_url: str, auth_token: str, account_id: str) -> dict:
    body = json.dumps({"accountId": account_id, "bucketName": config.B2_BUCKET}).encode()
    return _http_json(
        f"{api_url}/b2api/v3/b2_list_buckets",
        headers={"Authorization": auth_token, "Content-Type": "application/json"},
        data=body,
        method="POST",
    )


def _ensure_auth() -> _AuthState:
    global _state
    with _lock:
        if _state is None or _state.expires_at < time.time():
            _state = _authorize()
        return _state


def _get_upload_url(state: _AuthState) -> tuple[str, str]:
    body = json.dumps({"bucketId": state.bucket_id}).encode()
    data = _http_json(
        f"{state.api_url}/b2api/v3/b2_get_upload_url",
        headers={"Authorization": state.auth_token, "Content-Type": "application/json"},
        data=body,
        method="POST",
    )
    return data["uploadUrl"], data["authorizationToken"]


def _key_with_prefix(key: str) -> str:
    prefix = config.B2_PREFIX
    return f"{prefix}/{key}" if prefix else key


def upload_file(local_path: Path, key: str, content_type: str | None = None) -> str:
    """Upload a file to B2. Returns the public URL.

    `key` is the path within the bucket (e.g. 'episodes/abc.mp3').
    Public URL form: <downloadUrl>/file/<bucket>/<key>
    """
    if not config.b2_enabled():
        raise B2Error("B2 not configured")
    state = _ensure_auth()
    full_key = _key_with_prefix(key)

    data = local_path.read_bytes()
    sha1 = hashlib.sha1(data).hexdigest()
    ctype = content_type or mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"

    # Retry once if our cached upload URL has gone stale.
    last_err: Exception | None = None
    for attempt in range(2):
        upload_url, upload_auth = _get_upload_url(state)
        req = urllib.request.Request(
            upload_url,
            data=data,
            method="POST",
            headers={
                "Authorization": upload_auth,
                "X-Bz-File-Name": urllib.parse.quote(full_key, safe="/"),
                "Content-Type": ctype,
                "Content-Length": str(len(data)),
                "X-Bz-Content-Sha1": sha1,
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                resp.read()
            break
        except HTTPError as e:
            last_err = e
            body = e.read().decode("utf-8", errors="replace")
            # Auth-token-expired style errors → refresh and retry.
            if e.code in (401, 503) and attempt == 0:
                with _lock:
                    global _state
                    _state = None
                state = _ensure_auth()
                continue
            raise B2Error(f"upload failed ({e.code}): {body[:500]}")
    else:
        raise B2Error(f"upload failed: {last_err}")

    return public_url_for(key)


def public_url_for(key: str) -> str:
    """Construct the public URL for an object we've uploaded to B2.

    Requires bucket to be public ("Public" type). Form:
      <downloadUrl>/file/<bucketName>/<key>
    """
    if not config.b2_enabled():
        raise B2Error("B2 not configured")
    state = _ensure_auth()
    full_key = _key_with_prefix(key)
    return f"{state.download_url}/file/{config.B2_BUCKET}/{urllib.parse.quote(full_key, safe='/')}"
