"""Starting on a new server: fresh start without re-uploading, and signing in to Google from Discord."""
from __future__ import annotations

import json
from urllib.parse import parse_qs, urlparse

import pytest

from bot.vault import Vault
from conftest import Harness, until
from fakes import OWNER_ID

JPEG = "image/jpeg"


def as_message(reply: dict, channel_id: int) -> dict:
    return {"id": reply["message_id"], "channel_id": str(channel_id),
            "author": {"id": "901", "username": "bot", "discriminator": "0"},
            "content": "", "timestamp": "2026-01-01T00:00:00+00:00", "edited_timestamp": None, "tts": False,
            "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [],
            "pinned": False, "type": 0, "flags": 64, "components": reply["payload"]["data"]["components"]}


async def test_fresh_start_skips_what_is_already_archived(harness: Harness) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    fake.add_message(general, [("old1.jpg", b"\xff\xd8\xff\xe0 old one", JPEG)])
    fake.add_message(general, [("old2.jpg", b"\xff\xd8\xff\xe0 old two", JPEG)])

    # No server or owner ID: both are worked out from Discord.
    client = await harness.start(START_FRESH="true", DISCORD_GUILD_ID="", DISCORD_OWNER_ID="")
    assert client.config.guild_id == 1000 and client.config.owner_id == OWNER_ID
    await until(lambda: _done(client), 30, "the fresh start pass")
    statuses = await harness.statuses()
    assert statuses == {"old1.jpg": "uploaded", "old2.jpg": "uploaded"}
    assert fake.items == []

    await fake.live_message(general, [("new.jpg", b"\xff\xd8\xff\xe0 brand new", JPEG)])
    await until(lambda: "new.jpg" in harness.uploaded(), 30, "the new photo")
    assert harness.uploaded() == {"new.jpg"}

    # A restart doesn't repeat the fresh start: a post made while the bot was away gets uploaded.
    await harness.stop()
    fake.add_message(general, [("newer.jpg", b"\xff\xd8\xff\xe0 newer", JPEG)])
    client = await harness.start(START_FRESH="true", DISCORD_GUILD_ID="", DISCORD_OWNER_ID="")
    await until(lambda: "newer.jpg" in harness.uploaded(), 30, "the newer photo")
    assert harness.uploaded() == {"new.jpg", "newer.jpg"}


async def _done(client) -> bool:
    return await client.store.kv_get("fresh_start_done") == "done"


async def test_sign_in_to_google_from_discord(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    fake = harness.fake
    (harness.tmp / "client_secret.json").write_text(json.dumps({"installed": {
        "client_id": "client", "client_secret": "shh",
        "auth_uri": f"{fake.base}/google/auth", "token_uri": f"{fake.base}/google/token",
        "redirect_uris": ["http://localhost"],
    }}))
    general = fake.add_channel("general")
    client = await harness.start()
    command = next(c for c in fake.synced_commands if c["name"] == "archive")
    assert {o["name"] for o in command["options"]} == {"scan", "status", "google"}

    denied = await fake.slash(2001, "google", general)
    await until(lambda: fake.replies[denied], 20, "the refusal")
    assert "Only the bot owner" in harness.reply_text(denied)

    started = await fake.slash(OWNER_ID, "google", general)
    await until(lambda: fake.replies[started], 20, "the sign-in card")
    reply = fake.replies[started][0]
    buttons = [c for row in reply["payload"]["data"]["components"] for c in row["components"]]
    link = next(b["url"] for b in buttons if b.get("url"))
    paste = next(b["custom_id"] for b in buttons if b.get("custom_id"))
    state = parse_qs(urlparse(link).query)["state"][0]

    clicked = await fake.click(OWNER_ID, as_message(reply, general), paste)
    await until(lambda: fake.replies[clicked], 20, "the paste box")
    modal = fake.replies[clicked][0]["payload"]["data"]
    wrong = await fake.submit_modal(OWNER_ID, general, modal, ["http://localhost:8765/?state=nope&code=abc"])
    await until(lambda: len(fake.replies[wrong]) >= 2, 20, "the mismatch message")
    assert "doesn't match" in harness.reply_text(wrong)

    clicked = await fake.click(OWNER_ID, as_message(reply, general), paste)
    await until(lambda: fake.replies[clicked], 20, "the paste box again")
    modal = fake.replies[clicked][0]["payload"]["data"]
    good = await fake.submit_modal(OWNER_ID, general, modal, [f"http://localhost:8765/?state={state}&code=abc"])
    await until(lambda: len(fake.replies[good]) >= 2, 20, "the confirmation")
    assert "Google is connected" in harness.reply_text(good)
    saved = json.loads(Vault(harness.key).unseal(client.config.token_file.read_bytes()))
    assert saved["refresh_token"] == "refresh-from-login"
