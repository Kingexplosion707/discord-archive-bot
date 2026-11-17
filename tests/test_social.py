"""The TV/movie tracker, the personal Stremio addon, and live lyrics, end to end."""
from __future__ import annotations

import json
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import aiohttp
import pytest

from bot import lyrics as lyrics_module
from conftest import Harness, until
from fakes import BOB_ID, CAROL_ID, MALLORY_ID, OWNER_ID

SHOW = "tt0903747"
MOVIE = "tt0816692"
OTHER_MOVIE = "tt0133093"


def iso(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def add_titles(fake: Any) -> None:
    fake.metas[f"series:{SHOW}"] = {
        "id": SHOW, "type": "series", "name": "Breaking Bad", "releaseInfo": "2008-2013",
        "poster": f"https://images.metahub.space/poster/small/{SHOW}/img",
        "background": f"https://images.metahub.space/background/medium/{SHOW}/img",
        "description": "A chemistry teacher turns to crime.", "genres": ["Crime", "Drama"],
        "imdbRating": "9.5", "runtime": "49 min", "cast": ["Bryan Cranston", "Aaron Paul"],
        "trailers": [{"source": "HhesaQXLuRY", "type": "Trailer"}],
        "videos": [
            {"id": f"{SHOW}:1:{n}", "season": 1, "episode": n, "name": f"Episode name {n}",
             "overview": f"Overview of episode {n}.", "thumbnail": f"https://episodes.metahub.space/{SHOW}/1/{n}/w780.jpg",
             "released": iso(-400 + n)}
            for n in range(1, 8)
        ],
    }
    fake.metas[f"movie:{MOVIE}"] = {
        "id": MOVIE, "type": "movie", "name": "Interstellar", "releaseInfo": "2014",
        "poster": f"https://images.metahub.space/poster/small/{MOVIE}/img",
        "background": f"https://images.metahub.space/background/medium/{MOVIE}/img",
        "description": "Explorers travel through a wormhole.", "genres": ["Adventure", "Sci-Fi"],
        "imdbRating": "8.7", "runtime": "169 min",
    }
    fake.metas[f"movie:{OTHER_MOVIE}"] = {
        "id": OTHER_MOVIE, "type": "movie", "name": "The Matrix", "releaseInfo": "1999",
        "poster": f"https://images.metahub.space/poster/small/{OTHER_MOVIE}/img", "genres": ["Action", "Sci-Fi"],
        "imdbRating": "8.7", "runtime": "136 min",
    }


async def get(url: str) -> tuple[int, Any]:
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            body = await response.text()
            try:
                return response.status, json.loads(body)
            except ValueError:
                return response.status, body


def pinged(payload: dict[str, Any]) -> set[str]:
    return {str(u) for u in payload["allowed_mentions"].get("users", [])}


def sent_to(fake: Any, channel: int) -> list[dict[str, Any]]:
    return [s for s in fake.sent if s["channel_id"] == channel]


async def link(harness: Harness, user_id: int, channel: int) -> str:
    interaction = await harness.fake.slash(user_id, "link", channel, _group="tracker")
    await until(lambda: harness.reply_text(interaction), 20, "the addon link")
    found = re.search(r"/stremio/([\w-]+)/manifest\.json", harness.reply_text(interaction))
    assert found, harness.reply_text(interaction)
    return found.group(1)


async def test_tracker_end_to_end(harness: Harness) -> None:
    fake = harness.fake
    add_titles(fake)
    general = fake.add_channel("general")
    activity = fake.add_channel("watching")
    client = await harness.start()
    base = client.config.public_url
    groups = {c["name"] for c in fake.synced_commands}
    assert {"archive", "tracker", "watch", "lyrics"} <= groups

    # Only an admin can pick the channel.
    refused = await fake.slash(MALLORY_ID, "setup", general, _group="tracker", channel=activity)
    await until(lambda: harness.reply_text(refused), 20, "the refusal")
    assert "Manage Server" in harness.reply_text(refused)
    done = await fake.slash(OWNER_ID, "setup", general, _group="tracker", channel=activity)
    await until(lambda: "Watch activity will go to" in harness.reply_text(done), 20, "setup")

    owner_token = await link(harness, OWNER_ID, general)
    bob_token = await link(harness, BOB_ID, general)
    assert owner_token != bob_token
    assert owner_token == await link(harness, OWNER_ID, general), "the link stays the same"

    status, manifest = await get(f"{base}/stremio/{owner_token}/manifest.json")
    assert status == 200 and "subtitles" in manifest["resources"] and len(manifest["catalogs"]) == 6
    status, page = await get(f"{base}/stremio/{owner_token}/install")
    assert status == 200 and f"stremio://127.0.0.1:{harness.web_port}/stremio/{owner_token}/manifest.json" in page
    assert (await get(f"{base}/stremio/not-a-token/manifest.json"))[0] == 404

    # Pressing play in Stremio posts the episode with its picture and details.
    status, body = await get(f"{base}/stremio/{owner_token}/subtitles/series/{SHOW}%3A1%3A7/filename=bb.mkv&videoSize=1.json")
    assert status == 200 and body == {"subtitles": []}
    await until(lambda: sent_to(fake, activity), 20, "the activity post")
    post = sent_to(fake, activity)[0]
    embed = post["payload"]["embeds"][0]
    assert embed["title"] == "Breaking Bad · S1E7 · Episode name 7"
    assert embed["author"]["name"] == "user2000 is watching"
    assert embed["thumbnail"]["url"].endswith(f"/poster/medium/{SHOW}/img")
    assert embed["image"]["url"] == f"https://episodes.metahub.space/{SHOW}/1/7/w780.jpg"
    assert embed["description"] == "Overview of episode 7."
    assert "9.5" in json.dumps(embed["fields"]) and "Crime" in json.dumps(embed["fields"])
    buttons = [c for row in post["payload"]["components"] for c in row["components"]]
    assert {b.get("label") for b in buttons} >= {"Add to watchlist", "Rate", "Recommend", "Trailer"}
    assert post["payload"]["allowed_mentions"]["parse"] == []

    # The same episode again soon after isn't posted twice; Bob watching it shows the owner as "also watched".
    await get(f"{base}/stremio/{owner_token}/subtitles/series/{SHOW}:1:7.json")
    await get(f"{base}/stremio/{bob_token}/subtitles/series/{SHOW}:1:7.json")
    await until(lambda: len(sent_to(fake, activity)) == 2, 20, "Bob's post")
    assert f"<@{OWNER_ID}>" in json.dumps(sent_to(fake, activity)[1]["payload"]["embeds"][0]["fields"])
    assert (await get(f"{base}/stremio/bad-token/subtitles/movie/{MOVIE}.json"))[0] == 404

    # Hidden activity is recorded but not posted.
    hide = await fake.slash(BOB_ID, "privacy", general, _group="tracker", mode="hide")
    await until(lambda: harness.reply_text(hide), 20, "privacy")
    await get(f"{base}/stremio/{bob_token}/subtitles/movie/{MOVIE}.json")
    await until(lambda: _count(client, BOB_ID), 20, "Bob's private play")
    assert len(sent_to(fake, activity)) == 2
    show = await fake.slash(BOB_ID, "privacy", general, _group="tracker", mode="show")
    await until(lambda: harness.reply_text(show), 20, "privacy back on")

    # Rate from the button: it opens a dialog.
    rate_button = next(b for b in buttons if b.get("label") == "Rate")
    clicked = await fake.click(CAROL_ID, post["message"], rate_button["custom_id"])
    await until(lambda: fake.replies[clicked], 20, "the dialog")
    modal = fake.replies[clicked][0]["payload"]
    assert modal["type"] == 9
    submitted = await fake.submit_modal(CAROL_ID, activity, modal["data"], ["9", "Amazing ending"])
    await until(lambda: harness.reply_text(submitted), 20, "the rating")
    assert "rated **Breaking Bad** 9/10" in harness.reply_text(submitted)
    again = await fake.click(CAROL_ID, post["message"], rate_button["custom_id"])
    await until(lambda: fake.replies[again], 20, "a second dialog")
    bad = await fake.submit_modal(CAROL_ID, activity, fake.replies[again][0]["payload"]["data"], ["11", ""])
    await until(lambda: harness.reply_text(bad), 20, "the bad score")
    assert "1 to 10" in harness.reply_text(bad)

    # Watchlist from the button shows up in Stremio.
    watch_button = next(b for b in buttons if b.get("label") == "Add to watchlist")
    added = await fake.click(BOB_ID, post["message"], watch_button["custom_id"])
    await until(lambda: harness.reply_text(added), 20, "the watchlist")
    assert "Added" in harness.reply_text(added)
    status, catalog = await get(f"{base}/stremio/{bob_token}/catalog/series/watchlist.json")
    assert catalog["metas"][0]["name"] == "Breaking Bad" and catalog["metas"][0]["poster"]
    status, watching = await get(f"{base}/stremio/{bob_token}/catalog/series/friends-watching.json")
    assert [m["name"] for m in watching["metas"]] == ["Breaking Bad"]

    # Recommend from the button: pick a friend from a menu. They get pinged.
    rec_button = next(b for b in buttons if b.get("label") == "Recommend")
    clicked = await fake.click(OWNER_ID, post["message"], rec_button["custom_id"])
    await until(lambda: fake.replies[clicked], 20, "the friend picker")
    picker = fake.replies[clicked][0]["payload"]["data"]
    before = len(fake.sent)
    picked = await fake.pick_user(OWNER_ID, activity, picker["components"], CAROL_ID, fake.replies[clicked][0]["message_id"])
    await until(lambda: len(fake.sent) > before, 20, "the recommendation")
    rec = fake.sent[-1]["payload"]
    assert rec["content"].startswith(f"<@{CAROL_ID}>")
    assert pinged(rec) == {str(CAROL_ID)}
    await until(lambda: fake.replies[picked], 20, "the confirmation")

    # Recommend by command, with autocomplete, and with pings turned off.
    suggestions = await fake.autocomplete(OWNER_ID, general, "watch", "recommend", "title", "inter")
    await until(lambda: fake.replies[suggestions], 20, "suggestions")
    choices = fake.replies[suggestions][0]["payload"]["data"]["choices"]
    assert choices[0]["value"] == f"movie:{MOVIE}" and "Interstellar (2014)" in choices[0]["name"]
    quiet = await fake.slash(BOB_ID, "pings", general, _group="tracker", mode="off")
    await until(lambda: harness.reply_text(quiet), 20, "pings off")
    before = len(fake.sent)
    sent = await fake.slash(OWNER_ID, "recommend", general, _group="watch", title=f"movie:{MOVIE}", friend=BOB_ID, note="You'll cry")
    await until(lambda: "Sent **Interstellar**" in harness.reply_text(sent), 20, "the command recommendation")
    rec = fake.sent[before]["payload"]
    assert pinged(rec) == set() and "You'll cry" in json.dumps(rec["embeds"])
    status, recs = await get(f"{base}/stremio/{bob_token}/catalog/movie/friends-recs.json")
    assert [m["name"] for m in recs["metas"]] == ["Interstellar"]
    self_rec = await fake.slash(OWNER_ID, "recommend", general, _group="watch", title=f"movie:{MOVIE}", friend=OWNER_ID)
    await until(lambda: "yourself" in harness.reply_text(self_rec), 20, "self recommendation")

    # Info, now, history, stats, match.
    rated = await fake.slash(OWNER_ID, "rate", general, _group="watch", title=f"series:{SHOW}", score=7, review="Slow start")
    await until(lambda: fake.replies[rated] and len(fake.replies[rated]) >= 2, 20, "the command rating")
    info = await fake.slash(MALLORY_ID, "info", general, _group="watch", title="breaking")
    await until(lambda: len(fake.replies[info]) >= 2, 20, "info")
    card = json.dumps(fake.replies[info][1]["payload"]["embeds"][0])
    assert "Breaking Bad" in card and "8/10 (2)" in card and "||Amazing ending||" in card
    for command, options, expect in (
        ("now", {}, "Breaking Bad"),
        ("history", {"friend": OWNER_ID}, "S1E7"),
        ("stats", {}, "Episodes"),
        ("match", {"friend": BOB_ID}, "match"),
        ("recs", {}, "Nobody"),
    ):
        reply = await fake.slash(OWNER_ID, command, general, _group="watch", **options)
        await until(lambda: len(fake.replies[reply]) >= 2, 20, command)
        assert expect in json.dumps(fake.replies[reply][1]["payload"]), (command, fake.replies[reply])

    # Unlinking kills the old link.
    out = await fake.slash(OWNER_ID, "unlink", general, _group="tracker")
    await until(lambda: harness.reply_text(out), 20, "unlink")
    assert (await get(f"{base}/stremio/{owner_token}/manifest.json"))[0] == 404


async def _count(client: Any, user_id: int) -> int:
    row = await client.store.fetch_one("SELECT COUNT(*) AS n FROM watch_event WHERE user_id = ? AND type = 'movie'", (user_id,))
    return row["n"]


async def test_movie_night_episodes_and_recap(harness: Harness) -> None:
    fake = harness.fake
    add_titles(fake)
    general = fake.add_channel("general")
    activity = fake.add_channel("watching")
    client = await harness.start()
    base = client.config.public_url
    await client.store.kv_set("tracker_channel", str(activity))

    for uid, title in ((OWNER_ID, MOVIE), (BOB_ID, MOVIE), (CAROL_ID, OTHER_MOVIE)):
        added = await fake.slash(uid, "add", general, _group="watch", title=f"movie:{title}")
        await until(lambda: len(fake.replies[added]) >= 2, 20, "watchlist add")
        card = fake.replies[added][1]["payload"]["embeds"][0]
        assert "added this to their watchlist" in card["author"]["name"]
        assert card["thumbnail"]["url"].startswith("https://") and card["fields"]

    # Typing a name instead of picking a suggestion still finds it, with full details.
    typed = await fake.slash(OWNER_ID, "info", general, _group="watch", title="Interstellar")
    await until(lambda: len(fake.replies[typed]) >= 2, 20, "typed lookup")
    assert "Interstellar" in json.dumps(fake.replies[typed][1]["payload"]["embeds"][0])

    listed = await fake.slash(BOB_ID, "watchlist", general, _group="watch")
    await until(lambda: len(fake.replies[listed]) >= 2, 20, "the watchlist")
    cards = fake.replies[listed][1]["payload"]["embeds"]
    assert len(cards) == 1 and "Interstellar" in cards[0]["title"]
    assert cards[0]["thumbnail"]["url"].startswith("https://") and "-# " in cards[0]["description"]
    night = await fake.slash(OWNER_ID, "movienight", general, _group="watch", hours=1.0)
    await until(lambda: len(fake.replies[night]) >= 2, 20, "the poll")
    poll = fake.replies[night][1]
    assert "Interstellar · wanted by 2" in json.dumps(poll["payload"], ensure_ascii=False)
    vote_ids = [c["custom_id"] for row in poll["payload"]["components"] for c in row["components"]]
    message = {"id": poll["message_id"], "channel_id": str(general), "author": {"id": "901", "username": "bot", "discriminator": "0"},
               "content": "", "timestamp": "2026-01-01T00:00:00+00:00", "edited_timestamp": None, "tts": False,
               "mention_everyone": False, "mentions": [], "mention_roles": [], "attachments": [], "embeds": [],
               "pinned": False, "type": 0, "components": poll["payload"]["components"]}
    for uid in (OWNER_ID, BOB_ID):
        voted = await fake.click(uid, message, vote_ids[0])
        await until(lambda: fake.replies[voted], 20, "the vote")
    voted = await fake.click(CAROL_ID, message, vote_ids[1])
    await until(lambda: fake.replies[voted], 20, "Carol's vote")
    await client.store.execute("UPDATE movie_night SET ends_at = 0")
    await until(lambda: any("movie night is **Interstellar**" in (s["payload"].get("content") or "") for s in fake.sent), 20, "the winner")
    winner = next(s for s in fake.sent if "movie night is" in (s["payload"].get("content") or ""))
    assert pinged(winner["payload"]) == {str(OWNER_ID), str(BOB_ID), str(CAROL_ID)}
    late = await fake.click(BOB_ID, message, vote_ids[1])
    await until(lambda: "closed" in harness.reply_text(late), 20, "closed poll")

    # Watching a show follows it. Old episodes aren't announced; a new one pings the fans.
    token = await link(harness, BOB_ID, general)
    await get(f"{base}/stremio/{token}/subtitles/series/{SHOW}:1:6.json")
    await until(lambda: _followed(client), 20, "the follow")
    await client.tracker.check_new_episodes()
    assert not any("new episode" in (s["payload"].get("content") or "") for s in fake.sent)
    fake.metas[f"series:{SHOW}"]["videos"].append(
        {"id": f"{SHOW}:2:1", "season": 2, "episode": 1, "name": "Seven Thirty-Seven", "overview": "Back again.",
         "thumbnail": f"https://episodes.metahub.space/{SHOW}/2/1/w780.jpg", "released": iso(-0.1)})
    fake.metas[f"series:{SHOW}"]["videos"].append(
        {"id": f"{SHOW}:2:2", "season": 2, "episode": 2, "name": "Future", "released": iso(5)})
    await client.store.execute("DELETE FROM meta_cache")
    await client.tracker.check_new_episodes()
    alert = next(s for s in fake.sent if "new episode" in (s["payload"].get("content") or ""))
    assert pinged(alert["payload"]) == {str(BOB_ID)}
    assert "S2E1 · Seven Thirty-Seven" in alert["payload"]["embeds"][0]["title"]
    await client.tracker.check_new_episodes()
    assert sum("new episode" in (s["payload"].get("content") or "") for s in fake.sent) == 1

    recap = await fake.slash(OWNER_ID, "recap", general, _group="watch")
    await until(lambda: len(fake.replies[recap]) >= 2, 20, "recap")
    summary = next(s for s in sent_to(fake, activity) if s["payload"].get("embeds") and "This week" in s["payload"]["embeds"][0]["title"])
    assert "Breaking Bad" in json.dumps(summary["payload"])


async def _followed(client: Any) -> bool:
    return bool(await client.store.fetch_all("SELECT 1 FROM followed_show"))


SPICY_SONG = {
    "Status": 200,
    "Body": {"Content": [
        {"Lead": {"StartTime": 0.4, "EndTime": 1.4, "Syllables": [
            {"Text": "First", "StartTime": 0.4, "EndTime": 0.9, "IsPartOfWord": False},
            {"Text": "li", "StartTime": 0.9, "EndTime": 1.1, "IsPartOfWord": True},
            {"Text": "ne", "StartTime": 1.1, "EndTime": 1.4, "IsPartOfWord": False}]}},
        {"Type": "Vocal", "Text": "Second line here", "StartTime": 2.0, "EndTime": 3.0},
        {"Type": "Vocal", "Text": "Third line now", "StartTime": 3.6, "EndTime": 4.5},
    ]},
}


async def test_live_lyrics(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    music = fake.add_channel("music")
    fake.spicy["trackA"] = SPICY_SONG
    fake.lrclib["Song B"] = {"syncedLyrics": "[00:00.50]B one\n[00:02.00]B two", "plainLyrics": "B one\nB two"}
    monkeypatch.setattr(lyrics_module, "STOP_AFTER", 1.0)
    client = await harness.start()

    for bad_key, expect in (("sl_sk_secretsecret", "secret"), ("nope", "doesn't look")):
        bad = await fake.slash(OWNER_ID, "setup", general, _group="lyrics", channel=music, spicy_key=bad_key)
        await until(lambda: harness.reply_text(bad), 20, "the key check")
        assert expect in harness.reply_text(bad)
    done = await fake.slash(OWNER_ID, "setup", general, _group="lyrics", channel=music, spicy_key="sl_pk_testkey123")
    await until(lambda: "Word-synced" in harness.reply_text(done), 20, "setup")

    # Nothing for someone who hasn't turned it on.
    now_ms = int(time.time() * 1000)
    await fake.presence(CAROL_ID, {"track_id": "trackA", "title": "Song A", "artist": "Artist", "album": "Album",
                                   "start_ms": now_ms, "end_ms": now_ms + 10_000})
    on = await fake.slash(OWNER_ID, "on", general, _group="lyrics")
    await until(lambda: harness.reply_text(on), 20, "lyrics on")
    assert not sent_to(fake, music)

    now_ms = int(time.time() * 1000)
    await fake.presence(OWNER_ID, {"track_id": "trackA", "title": "Song A", "artist": "Artist", "album": "Album",
                                   "start_ms": now_ms - 100, "end_ms": now_ms + 9_900})
    await until(lambda: sent_to(fake, music), 20, "the lyrics message")
    first = sent_to(fake, music)[0]
    embed = first["payload"]["embeds"][0]
    assert embed["title"] == "Song A — Artist" and embed["url"] == "https://open.spotify.com/track/trackA"
    assert embed["thumbnail"]["url"] == "https://i.scdn.co/image/ab67616d0000b273cover"
    assert "### First line" in embed["description"] and "Spicy Lyrics" in embed["footer"]["text"]
    assert fake.spicy_auth[-1] == "Bearer sl_pk_testkey123"
    message_id = int(first["message"]["id"])

    def edited_to(text: str) -> bool:
        return any(e["message_id"] == message_id and text in json.dumps(e["payload"]) for e in fake.edits)

    await until(lambda: edited_to("### Second line here"), 10, "the second line")
    await until(lambda: edited_to("### Third line now"), 10, "the third line")
    assert len(sent_to(fake, music)) == 1, "one message, edited in place"

    # The word-by-word page.
    button = first["payload"]["components"][0]["components"][0]
    assert button["label"] == "Word by word"
    status, page = await get(button["url"])
    assert status == 200 and "requestAnimationFrame" in page
    status, state = await get(button["url"] + "/state")
    assert state["track"]["title"] == "Song A" and state["synced"] and state["playing"]
    assert state["lines"][0]["w"] == [[0.4, 0.9, "First"], [0.9, 1.4, "line"]]

    # Next song with no Spicy lyrics falls back to LRCLIB, same message.
    now_ms = int(time.time() * 1000)
    await fake.presence(OWNER_ID, {"track_id": "trackB", "title": "Song B", "artist": "Other", "album": "B",
                                   "start_ms": now_ms, "end_ms": now_ms + 8_000})
    await until(lambda: edited_to("LRCLIB"), 10, "the LRCLIB lyrics")
    await until(lambda: edited_to("### B two"), 10, "LRCLIB timing")

    # Pausing, then stopping, ends the session.
    await fake.presence(OWNER_ID, None)
    await until(lambda: edited_to("paused"), 10, "paused")
    await until(lambda: edited_to("Stopped listening"), 15, "stopped")
    status, _ = await get(button["url"] + "/state")
    assert status == 404

    off = await fake.slash(OWNER_ID, "off", general, _group="lyrics")
    await until(lambda: harness.reply_text(off), 20, "lyrics off")
    before = len(sent_to(fake, music))
    now_ms = int(time.time() * 1000)
    await fake.presence(OWNER_ID, {"track_id": "trackA", "title": "Song A", "artist": "Artist", "album": "Album",
                                   "start_ms": now_ms, "end_ms": now_ms + 9_000})
    await until(lambda: True, 1)
    import asyncio
    await asyncio.sleep(1)
    assert len(sent_to(fake, music)) == before
