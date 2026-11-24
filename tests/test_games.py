"""Game tracking end to end: which statuses count as games, sessions, posts, boards and Steam."""
from __future__ import annotations

import asyncio
from typing import Any

import pytest

from bot import games as games_module
from conftest import Harness, until
from fakes import BOB_ID, CAROL_ID, OWNER_ID

ELDEN = {"id": "1001", "name": "ELDEN RING", "aliases": ["Elden Ring"], "icon_hash": "ic", "cover_image_hash": "cv",
         "themes": ["Action", "RPG"], "executables": [], "third_party_skus": [{"distributor": "steam", "id": "1245620"}]}
MEDAL = {"id": "2002", "name": "Medal", "aliases": [], "icon_hash": "m", "third_party_skus": []}
STARDEW = {"id": "3003", "name": "Stardew Valley", "aliases": [], "icon_hash": "s", "cover_image_hash": None,
           "themes": ["Simulation"], "third_party_skus": []}
STEAM_KEY = "STEAMKEY1234567890ABCDEF"
STEAM_ID = "76561198000000001"


def sent_to(fake: Any, channel: int) -> list[dict[str, Any]]:
    return [s for s in fake.sent if s["channel_id"] == channel]


def embeds(harness: Harness, interaction: str) -> list[dict[str, Any]]:
    out = []
    for reply in harness.fake.replies[interaction]:
        payload = reply["payload"].get("data", reply["payload"])
        out += payload.get("embeds") or []
    return out


def text(harness: Harness, interaction: str) -> str:
    parts = [harness.reply_text(interaction)]
    for embed in embeds(harness, interaction):
        parts += [embed.get("title") or "", embed.get("description") or "", (embed.get("author") or {}).get("name", "")]
        parts += [f"{f['name']}\n{f['value']}" for f in embed.get("fields", [])]
    return "\n".join(parts)


async def ask(harness: Harness, user_id: int, sub: str, where: int, wait: str, **options: Any) -> str:
    interaction = await harness.fake.slash(user_id, sub, where, _group="games", **options)
    await until(lambda: wait in text(harness, interaction), 20, f"/games {sub}")
    return text(harness, interaction)


async def test_game_tracking(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    games_channel = fake.add_channel("games")
    fake.detectable = [ELDEN, MEDAL, STARDEW]
    fake.steam_store = [{"id": 367520, "name": "Hollow Knight"}, {"id": 999, "name": "Some App Pro Deluxe"}]
    monkeypatch.setattr(games_module, "MIN_SESSION", 0)
    monkeypatch.setattr(games_module, "POST_AFTER", 0)
    client = await harness.start()
    games = client.games
    await until(lambda: games.catalog_size == 3, 20, "the game list")
    assert "/games" not in {c["name"] for c in fake.synced_commands}
    assert "games" in {c["name"] for c in fake.synced_commands}

    await ask(harness, OWNER_ID, "setup", general, "Game posts", channel=games_channel, start_posts=True)

    async def open_game(user_id: int) -> str | None:
        row = await client.store.fetch_one("SELECT game_key FROM game_open WHERE user_id = ?", (user_id,))
        return row["game_key"] if row else None

    # Clipping software showing as "Playing" isn't a game, even next to a real one.
    await fake.presence(BOB_ID, playing=[{"name": "Medal", "application_id": "2002"}])
    await fake.presence(BOB_ID, playing=[{"name": "OBS Studio"}])
    await fake.presence(BOB_ID, playing=[{"name": "Medal", "application_id": "2002"}, {"name": "ELDEN RING", "application_id": "1001"}])
    await until(lambda: any(s["payload"].get("embeds") for s in sent_to(fake, games_channel)), 20, "the start post")
    assert await open_game(BOB_ID) == "d:1001"
    start = sent_to(fake, games_channel)[0]["payload"]["embeds"][0]
    assert start["title"] == "ELDEN RING" and "is playing" in start["author"]["name"]
    assert start["thumbnail"]["url"].endswith("/1245620/library_600x900.jpg")
    assert start["url"] == "https://store.steampowered.com/app/1245620"
    assert len(sent_to(fake, games_channel)) == 1

    # Same game again is the same session.
    await fake.presence(BOB_ID, playing=[{"name": "ELDEN RING", "application_id": "1001"}])
    # Matched by name or alias without an application id; a Steam store exact match also counts.
    await fake.presence(CAROL_ID, playing=[{"name": "Elden Ring"}])
    await asyncio.sleep(0.5)
    await until(lambda: len(sent_to(fake, games_channel)) == 2, 20, "Carol's start post")
    assert await open_game(CAROL_ID) == "d:1001"

    await fake.presence(BOB_ID, playing=[{"name": "Hollow Knight"}])
    await until(lambda: any(e["message_id"] == int(sent_to(fake, games_channel)[0]["message"]["id"]) for e in fake.edits), 20, "the post update")
    edit = next(e for e in fake.edits if e["message_id"] == int(sent_to(fake, games_channel)[0]["message"]["id"]))
    assert "Played for" in edit["payload"]["embeds"][0]["description"]
    assert await open_game(BOB_ID) == "s:367520"
    assert "Hollow Knight" in fake.steam_store_searches

    # Not on Discord's list and no exact Steam match: not a game.
    await fake.presence(CAROL_ID, playing=[{"name": "Some App"}])
    await until(lambda: "Some App" in fake.steam_store_searches, 20, "the store check")
    await asyncio.sleep(0.5)
    assert await open_game(CAROL_ID) is None
    await fake.presence(BOB_ID)
    await asyncio.sleep(0.5)
    for _ in range(50):
        if await open_game(BOB_ID) is None:
            break
        await asyncio.sleep(0.1)
    assert await open_game(BOB_ID) is None
    sessions = await client.store.fetch_all("SELECT user_id, game_key FROM game_session ORDER BY id")
    assert [(r["user_id"], r["game_key"]) for r in sessions] == [(BOB_ID, "d:1001"), (CAROL_ID, "d:1001"), (BOB_ID, "s:367520")]

    # Give the numbers some weight, then look at the boards.
    await client.store.execute("UPDATE game_session SET seconds = 7200 WHERE user_id = ? AND game_key = 'd:1001'", (BOB_ID,))
    await client.store.execute("UPDATE game_session SET seconds = 3600 WHERE user_id = ?", (CAROL_ID,))
    board = await ask(harness, OWNER_ID, "leaderboard", general, "Most played")
    assert board.index("2.0h") < board.index("1.0h") and "2 games" in board
    top = await ask(harness, OWNER_ID, "top", general, "Top games", period="all")
    assert "ELDEN RING" in top and "2 players" in top
    profile = await ask(harness, OWNER_ID, "profile", general, "games", user=BOB_ID)
    assert "ELDEN RING" in profile and "Hollow Knight" in profile and "1-day streak" in profile
    one = await ask(harness, OWNER_ID, "game", general, "Players", name="d:1001")
    assert "3.0h" in one and "2 people" in one
    now = await ask(harness, OWNER_ID, "now", general, "Nobody")

    # Turning tracking off hides you and stops new sessions.
    await ask(harness, CAROL_ID, "privacy", general, "off for you", tracking="off")
    await fake.presence(CAROL_ID, playing=[{"name": "Stardew Valley"}])
    await asyncio.sleep(0.5)
    assert await open_game(CAROL_ID) is None
    board = await ask(harness, OWNER_ID, "leaderboard", general, "Most played", period="all")
    assert "1.0h" not in board
    epic = await ask(harness, BOB_ID, "epic", general, "Epic")
    assert "public" in epic

    # Steam: the owner adds a key, people link their profile, playing on Steam counts too.
    nokey = await ask(harness, BOB_ID, "steam", general, "Steam", profile="bobsteam")
    assert "steamkey" in nokey
    notowner = await ask(harness, BOB_ID, "steamkey", general, "Only the bot owner")
    opened = await fake.slash(OWNER_ID, "steamkey", general, _group="games")
    await until(lambda: fake.replies[opened], 20, "the key form")
    saved = await fake.submit_modal(OWNER_ID, general, fake.replies[opened][0]["payload"]["data"], [STEAM_KEY])
    await until(lambda: "Saved" in harness.reply_text(saved), 20, "the key saved")
    assert STEAM_KEY not in (await client.store.kv_get(games_module.STEAM_KEY) or "")

    fake.steam_vanity["bobsteam"] = STEAM_ID
    fake.steam_players[STEAM_ID] = {"personaname": "Bobby", "communityvisibilitystate": 3, "avatarfull": "https://a/b.jpg"}
    fake.steam_games[STEAM_ID] = [
        {"appid": 1245620, "name": "ELDEN RING", "playtime_forever": 6000, "playtime_2weeks": 120},
        {"appid": 367520, "name": "Hollow Knight", "playtime_forever": 1200},
    ]
    linked = await ask(harness, BOB_ID, "steam", general, "Linked", profile="https://steamcommunity.com/id/bobsteam/")
    assert "2 games" in linked and "120h" in linked

    fake.steam_players[STEAM_ID]["gameid"] = "367520"
    fake.steam_players[STEAM_ID]["gameextrainfo"] = "Hollow Knight"
    await games.poll_steam()
    assert await open_game(BOB_ID) == "s:367520"
    del fake.steam_players[STEAM_ID]["gameid"]
    await games.poll_steam()
    assert await open_game(BOB_ID) is None
    row = await client.store.fetch_one("SELECT source FROM game_session ORDER BY id DESC LIMIT 1")
    assert row["source"] == "steam"
    profile = await ask(harness, OWNER_ID, "profile", general, "Steam", user=BOB_ID)
    assert "Bobby" in profile and "2 games" in profile

    # Sessions left open when the bot stops are closed at the last time they were seen.
    await fake.presence(BOB_ID, playing=[{"name": "Stardew Valley"}])
    for _ in range(50):
        if await open_game(BOB_ID):
            break
        await asyncio.sleep(0.1)
    await client.store.execute("UPDATE game_open SET started_at = started_at - 600, last_seen = last_seen - 300")
    await harness.stop()
    client = await harness.start()
    await until(lambda: client.games.catalog_size == 3, 20, "the game list again")
    for _ in range(50):
        last = await client.store.fetch_one("SELECT * FROM game_session ORDER BY id DESC LIMIT 1")
        if last["game_key"] == "d:3003":
            break
        await asyncio.sleep(0.1)
    assert last["game_key"] == "d:3003" and 290 <= last["seconds"] <= 320
