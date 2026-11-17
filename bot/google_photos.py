"""Uploads to Google Photos through the Library API.

The bot only asks for permission to add to the library. It can't see or change
anything else in the account.
"""
from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import aiohttp
from cryptography.fernet import InvalidToken

from .vault import Vault

API_BASE = "https://photoslibrary.googleapis.com/v1"
SCOPES = [
    "https://www.googleapis.com/auth/photoslibrary.appendonly",
    # Only lets the bot touch the file it creates, not the rest of your Drive.
    "https://www.googleapis.com/auth/drive.file",
]

_LOGIN_HELP = (
    "Uploads are paused because the bot isn't signed in to Google. "
    "Run `/archive google` in the server to sign in."
)
_API_TIMEOUT = aiohttp.ClientTimeout(total=120)
_UPLOAD_TIMEOUT = aiohttp.ClientTimeout(total=4 * 3600, sock_read=600)


class GooglePause(Exception):
    """Stop uploading for a while. Scanning can carry on."""

    def __init__(self, reason: str, seconds: int, alert: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.seconds = seconds
        self.alert = alert


class GoogleTransient(Exception):
    """A temporary failure. Retry this item later."""


class GoogleRejected(Exception):
    """Google refused this item. Retrying won't help."""


@dataclass(frozen=True)
class MediaItem:
    id: str
    product_url: str | None


def save_login(vault: Vault, path: Path, login: dict[str, str]) -> None:
    path.write_bytes(vault.seal(json.dumps(login).encode("utf-8")))
    path.chmod(0o600)


class TokenProvider:
    def __init__(self, session: aiohttp.ClientSession, vault: Vault, path: Path) -> None:
        self._session = session
        self._vault = vault
        self._path = path
        self._access_token: str | None = None
        self._expires_at = 0.0

    def forget(self) -> None:
        self._access_token = None

    def _load(self) -> dict[str, str]:
        try:
            return json.loads(self._vault.unseal(self._path.read_bytes()))
        except FileNotFoundError as exc:
            raise GooglePause("not logged in to Google", 1800, _LOGIN_HELP) from exc
        except InvalidToken as exc:
            raise GooglePause(
                "saved Google login can't be decrypted", 1800, _LOGIN_HELP
            ) from exc

    async def get(self) -> str:
        if self._access_token and time.time() < self._expires_at - 60:
            return self._access_token
        login = self._load()
        form = {
            "client_id": login["client_id"],
            "client_secret": login["client_secret"],
            "refresh_token": login["refresh_token"],
            "grant_type": "refresh_token",
        }
        async with self._session.post(login["token_uri"], data=form, timeout=_API_TIMEOUT) as response:
            status = response.status
            body = await response.read()
        try:
            payload: dict[str, Any] = json.loads(body)
        except ValueError:
            payload = {}
        if status == 200 and "access_token" in payload:
            self._access_token = payload["access_token"]
            self._expires_at = time.time() + float(payload.get("expires_in", 3600))
            return self._access_token
        if status in (400, 401) and payload.get("error") in {
            "invalid_grant",
            "invalid_client",
            "unauthorized_client",
        }:
            raise GooglePause("Google login expired", 1800, _LOGIN_HELP)
        raise GoogleTransient(f"token refresh failed with HTTP {status}")


class GooglePhotos:
    def __init__(
        self,
        session: aiohttp.ClientSession,
        tokens: TokenProvider,
        api_base: str = API_BASE,
        rate_limit_wait: float = 30.0,
    ) -> None:
        self._session = session
        self._tokens = tokens
        self._api_base = api_base.rstrip("/")
        self._rate_limit_wait = rate_limit_wait

    async def _send(self, path: str, make_request: Any) -> tuple[int, bytes]:
        """Send a request, refreshing the login or waiting out a rate limit once each."""
        refreshed = waited = False
        while True:
            token = await self._tokens.get()
            status, body = await make_request(f"{self._api_base}{path}", token)
            if status == 401 and not refreshed:
                refreshed = True
                self._tokens.forget()
                continue
            if status == 429 and not waited:
                waited = True
                await asyncio.sleep(self._rate_limit_wait)
                continue
            return status, body

    @staticmethod
    def _check(status: int, body: bytes) -> None:
        if 200 <= status < 300:
            return
        text = body.decode("utf-8", "replace")[:300]
        if status == 429:
            raise GooglePause("Google rate limit or daily quota reached", 3600)
        if status == 401:
            raise GooglePause("Google login expired", 1800, _LOGIN_HELP)
        if status == 403:
            raise GooglePause(
                "Google refused access",
                1800,
                f"Uploads are paused because Google refused access (HTTP 403): {text}",
            )
        if status >= 500 or status == 408:
            raise GoogleTransient(f"Google returned HTTP {status}")
        raise GoogleRejected(f"Google returned HTTP {status}: {text}")

    async def _post_json(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        async def request(url: str, token: str) -> tuple[int, bytes]:
            headers = {"Authorization": f"Bearer {token}"}
            async with self._session.post(
                url, json=payload, headers=headers, timeout=_API_TIMEOUT
            ) as response:
                return response.status, await response.read()

        status, body = await self._send(path, request)
        self._check(status, body)
        return json.loads(body)

    async def create_album(self, title: str) -> str:
        result = await self._post_json("/albums", {"album": {"title": title}})
        return str(result["id"])

    async def upload_bytes(self, path: Path, mime_type: str) -> str:
        async def request(url: str, token: str) -> tuple[int, bytes]:
            headers = {
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/octet-stream",
                "X-Goog-Upload-Content-Type": mime_type,
                "X-Goog-Upload-Protocol": "raw",
            }
            with path.open("rb") as handle:
                async with self._session.post(
                    url, data=handle, headers=headers, timeout=_UPLOAD_TIMEOUT
                ) as response:
                    return response.status, await response.read()

        status, body = await self._send("/uploads", request)
        self._check(status, body)
        token = body.decode("utf-8").strip()
        if not token:
            raise GoogleTransient("Google returned an empty upload token")
        return token

    async def create_media_item(
        self, upload_token: str, filename: str, album_id: str, description: str | None = None
    ) -> MediaItem:
        new_item: dict[str, Any] = {
            "simpleMediaItem": {"fileName": filename, "uploadToken": upload_token}
        }
        if description:
            new_item["description"] = description[:1000]  # caption shown and searched in Google Photos
        result = await self._post_json(
            "/mediaItems:batchCreate",
            {"albumId": album_id, "newMediaItems": [new_item]},
        )
        results = result.get("newMediaItemResults") or []
        if not results:
            raise GoogleTransient("Google returned no result for the upload")
        item_status = results[0].get("status") or {}
        code = int(item_status.get("code") or 0)
        message = str(item_status.get("message") or "")
        if code:
            lowered = message.lower()
            if code == 8 or "storage" in lowered or "quota" in lowered:  # 8 is RESOURCE_EXHAUSTED
                raise GooglePause(
                    "Google storage is full",
                    6 * 3600,
                    "Uploads are paused because your Google storage is full.",
                )
            if code in {4, 13, 14}:  # deadline exceeded, internal, unavailable
                raise GoogleTransient(f"Google item error {code}: {message}")
            raise GoogleRejected(f"Google item error {code}: {message}")
        item = results[0].get("mediaItem")
        if not item or "id" not in item:
            raise GoogleTransient("Google returned no media item for the upload")
        return MediaItem(id=str(item["id"]), product_url=item.get("productUrl"))
