"""End-to-end tests: real discord.py and real ffmpeg against fake services."""
from __future__ import annotations

import asyncio
import io
import json
import random
import shutil
import sqlite3
import subprocess
import tempfile
import threading
import time
from datetime import timezone
from zoneinfo import ZoneInfo
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import discord
import pytest
from cryptography.fernet import Fernet
from PIL import Image, ImageDraw

from bot import check, google_login, media
from bot.config import Config, ConfigError
from bot.store import Store
from bot.vault import Vault
from conftest import Harness, until
from fakes import BOB_ID, CLANKERS_ROLE_ID, MALLORY_ID, OWNER_ID, FakeServices

JPEG = "image/jpeg"
HLS_TRAP = b"#EXTM3U\n#EXT-X-MEDIA-SEQUENCE:0\n#EXTINF:10.0,\nfile:///etc/passwd\n#EXT-X-ENDLIST\n"


def photo(number: int) -> bytes:
    return b"\xff\xd8\xff\xe0 fake photo " + str(number).encode()


def real_video(suffix: str, folder: Path | None = None) -> bytes:
    codec = {
        ".webm": ["-c:v", "libvpx-vp9", "-b:v", "0", "-crf", "45", "-deadline", "realtime", "-cpu-used", "8"],
        ".mp4": ["-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p"],
    }[suffix]
    with tempfile.TemporaryDirectory() as work:
        target = Path(work) / f"out{suffix}"
        subprocess.run(
            ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=5", *codec, str(target)],
            check=True,
        )
        return target.read_bytes()


def real_image(image_format: str) -> bytes:
    buffer = io.BytesIO()
    Image.effect_noise((200, 150), 40).convert("RGB").save(buffer, format=image_format)
    return buffer.getvalue()


async def test_uploads_everything_in_the_server(harness: Harness) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    videos = fake.add_channel("videos")
    secret = fake.add_channel("secret", hidden=True)
    skipped = fake.add_channel("skipme")
    adult = fake.add_channel("adult", nsfw=True)
    voice = fake.add_channel("voice-chat", channel_type=2)
    forum = fake.add_channel("forum", channel_type=15)
    active = fake.add_thread(general, "active")
    archived = fake.add_thread(general, "old", archived=True)
    private = fake.add_thread(general, "private", archived=True, private=True)
    post = fake.add_thread(forum, "post")

    fake.add_message(general, [("a.jpg", photo(1), JPEG), ("b.png", photo(2), "image/png")])
    fake.add_message(general, [("meme.gif", photo(3), "image/gif")])
    fake.add_message(general, [("notes.txt", b"hello", "text/plain")])
    fake.add_message(general, [("repost.jpg", photo(1), JPEG)])
    fake.add_message(general, [("trap.webm", HLS_TRAP, "video/webm")])
    fake.add_message(general, [("huge.jpg", b"x" * 2048, JPEG)])
    fake.add_message(videos, [("clip.webm", real_video(".webm"), "video/webm")])
    fake.add_message(videos, [("clip2.mp4", real_video(".mp4"), "video/mp4")])
    fake.add_message(secret, [("in-secret.jpg", photo(10), JPEG)])
    fake.add_message(skipped, [("in-skipped.jpg", photo(11), JPEG)])
    fake.add_message(adult, [("in-adult.jpg", photo(12), JPEG)])
    fake.add_message(voice, [("in-voice.jpg", photo(13), JPEG)])
    fake.add_message(active, [("in-active.jpg", photo(14), JPEG)])
    fake.add_message(archived, [("in-archived.jpg", photo(15), JPEG)])
    fake.add_message(private, [("in-private.jpg", photo(16), JPEG)])
    fake.add_message(post, [("in-forum.jpg", photo(17), JPEG)])

    client = await harness.start(SKIP_CHANNEL_IDS=skipped)
    assert fake.identify["intents"] == (1 << 0) | (1 << 1) | (1 << 8) | (1 << 9) | (1 << 15)
    command = next(c for c in fake.synced_commands if c["name"] == "archive")
    assert {o["name"] for o in command["options"]} == {"scan", "status", "google", "tunnel"}

    await until(lambda: "in-forum.jpg" in harness.uploaded(), 60, "the crawl to finish")
    await until(harness.drained, 60, "the queue to empty")

    statuses = await harness.statuses()
    expected = {
        "a.jpg", "b.png", "meme.gif", "huge.jpg", "clip.mp4", "clip2.mp4", "in-voice.jpg",
        "in-active.jpg", "in-archived.jpg", "in-private.jpg", "in-forum.jpg",
    }
    assert sorted(item["filename"] for item in fake.items) == sorted(expected), statuses
    assert statuses["repost.jpg"] == "duplicate"
    assert statuses["trap.webm"] == "skipped"
    for left_out in ("notes.txt", "in-secret.jpg", "in-skipped.jpg", "in-adult.jpg"):
        assert left_out not in statuses

    assert list(fake.albums.values()) == ["Discord Archive"]
    assert len({item["album"] for item in fake.items}) == 1
    converted = next(item for item in fake.items if item["filename"] == "clip.mp4")
    assert converted["mime"] == "video/mp4" and converted["data"][4:8] == b"ftyp"

    assert list(client.config.tmp_dir.iterdir()) == []
    assert b"refresh-1" not in client.config.token_file.read_bytes()

    status = await fake.slash(MALLORY_ID, "status", general)
    await until(lambda: harness.reply_text(status), 20, "status")
    assert "Uploaded:** 11" in harness.reply_text(status) and "secret" not in harness.reply_text(status)
    owner_status = await fake.slash(OWNER_ID, "status", general)
    await until(lambda: harness.reply_text(owner_status), 20, "owner status")
    assert "secret" in harness.reply_text(owner_status)

    refused = await fake.slash(MALLORY_ID, "scan", general)
    await until(lambda: harness.reply_text(refused), 20, "the refusal")
    assert "Only the bot owner" in harness.reply_text(refused)

    await fake.live_message(general, [("live.jpg", photo(20), JPEG)])
    await until(lambda: "live.jpg" in harness.uploaded(), 30, "the live photo")

    # A restart picks up what was posted while offline and re-uploads nothing.
    await until(harness.drained, 30, "the live photo to be recorded")
    count = len(fake.items)
    await harness.stop()
    fake.add_message(general, [("offline.jpg", photo(21), JPEG)])
    await harness.start(SKIP_CHANNEL_IDS=skipped)
    await until(lambda: "offline.jpg" in harness.uploaded(), 30, "the offline photo")
    await until(harness.drained, 30, "the restart")
    assert len(fake.items) == count + 1, [i["filename"] for i in fake.items]
    assert len(fake.albums) == 1


async def test_nsfw_setting_and_size_limit(harness: Harness) -> None:
    fake = harness.fake
    adult = fake.add_channel("adult", nsfw=True)
    fake.add_message(adult, [("in-adult.jpg", photo(1), JPEG)])
    fake.add_message(adult, [("too-big.jpg", b"x" * (2 * 1024 * 1024), JPEG)])
    await harness.start(SCAN_NSFW_CHANNELS="true", MAX_DOWNLOAD_MB=1)
    await until(lambda: "in-adult.jpg" in harness.uploaded(), 30, "the upload")
    await until(harness.drained, 30, "the queue")
    assert (await harness.statuses())["too-big.jpg"] == "skipped"


async def _status_is(harness: Harness, filename: str, status: str) -> bool:
    return (await harness.statuses()).get(filename) == status


async def test_expired_links_and_deleted_messages(harness: Harness) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    client = await harness.start()

    await fake.live_message(general, [("stale.jpg", photo(1), JPEG)], stale_link=True)
    await until(lambda: "stale.jpg" in harness.uploaded(), 30, "the refreshed download")

    gone = await fake.live_message(general, [("gone.jpg", photo(2), JPEG)], stale_link=True)
    fake.messages[general] = [m for m in fake.messages[general] if m["id"] != gone["id"]]
    await until(lambda: _status_is(harness, "gone.jpg", "skipped"), 30, "the deleted message")

    # Deleting a message cancels anything still waiting on it.
    client.pipeline._paused_until = time.time() + 600
    client.pipeline._pause_reason = "test"
    waiting = await fake.live_message(general, [("waiting.jpg", photo(3), JPEG)])
    await until(lambda: _status_is(harness, "waiting.jpg", "queued"), 30, "the queue entry")
    await fake.delete_message(general, int(waiting["id"]))
    await until(lambda: _status_is(harness, "waiting.jpg", "skipped"), 20, "the cancellation")


async def test_google_failures_are_handled(harness: Harness) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    client = await harness.start()
    number = iter(range(100, 200))

    async def post(name: str) -> None:
        await fake.live_message(general, [(name, photo(next(number)), JPEG)])

    async def resume() -> None:
        client.pipeline._paused_until = 0
        client.pipeline.wake()

    fake.google_failures["upload"] = [500]
    await post("retry.jpg")
    await until(lambda: "retry.jpg" in harness.uploaded(), 30, "the retry")

    fake.valid_tokens.clear()
    await post("refresh.jpg")
    await until(lambda: "refresh.jpg" in harness.uploaded(), 30, "the token refresh")

    fake.google_failures["batch"] = [429]
    await post("brief-limit.jpg")
    await until(lambda: "brief-limit.jpg" in harness.uploaded(), 30, "the rate limit wait")

    # A daily quota pauses the queue without losing anything.
    fake.google_failures["upload"] = [429, 429]
    await post("quota.jpg")
    await until(lambda: client.pipeline.pause_status, 30, "the pause")
    assert (await harness.statuses())["quota.jpg"] == "queued"
    await resume()
    await until(lambda: "quota.jpg" in harness.uploaded(), 30, "the resume")

    # An expired Google login alerts the owner once.
    alerts = len(fake.sent)
    client.pipeline._google._tokens.forget()
    fake.valid_tokens.clear()
    fake.google_failures["token"] = [(400, {"error": "invalid_grant"})] * 3
    await post("login-expired.jpg")
    await until(lambda: len(fake.sent) > alerts, 30, "the owner alert")
    assert "/archive google" in fake.sent[-1]["payload"]["content"] and fake.sent[-1]["channel_id"] == OWNER_ID + 5000
    fake.google_failures["token"] = []
    await resume()
    await until(lambda: "login-expired.jpg" in harness.uploaded(), 30, "recovery")

    full = {"newMediaItemResults": [{"uploadToken": "x", "status": {"code": 8, "message": "Insufficient storage"}}]}
    fake.google_failures["batch"] = [(200, full)]
    await post("full.jpg")
    await until(lambda: (client.pipeline.pause_status or ("",))[0] == "Google storage is full", 30, "the storage pause")
    assert "storage is full" in fake.sent[-1]["payload"]["content"]
    await resume()
    await until(lambda: "full.jpg" in harness.uploaded(), 30, "recovery from full storage")

    rejected = {"newMediaItemResults": [{"uploadToken": "x", "status": {"code": 3, "message": "Failed: bad file"}}]}
    fake.google_failures["batch"] = [(207, rejected)]
    await post("refused.jpg")
    await until(lambda: _status_is(harness, "refused.jpg", "failed"), 30, "the refusal")
    await post("after-refused.jpg")
    await until(lambda: "after-refused.jpg" in harness.uploaded(), 30, "the next photo")

    rescan = await fake.slash(OWNER_ID, "scan", general)
    await until(lambda: "Retrying 1" in harness.reply_text(rescan), 20, "the retry reply")
    await until(lambda: "refused.jpg" in harness.uploaded(), 30, "the retried upload")

    fake.albums.clear()
    await post("new-album.jpg")
    await until(lambda: "new-album.jpg" in harness.uploaded(), 30, "the new album")
    assert len(fake.albums) == 1


async def test_old_face_data_is_removed_on_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "archive.db"
    db = sqlite3.connect(path)
    db.executescript(
        """
        CREATE TABLE attachment (id INTEGER PRIMARY KEY, message_id INTEGER NOT NULL, channel_id INTEGER NOT NULL,
            filename TEXT NOT NULL, content_type TEXT, size INTEGER NOT NULL, url TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued', attempts INTEGER NOT NULL DEFAULT 0, next_try_at REAL NOT NULL DEFAULT 0,
            sha256 TEXT, media_item_id TEXT, product_url TEXT, note TEXT, updated_at REAL NOT NULL);
        CREATE TABLE person (id INTEGER PRIMARY KEY, embeddings BLOB);
        CREATE TABLE attachment_person (attachment_id INTEGER, person_id INTEGER);
        CREATE TABLE media_hash (sha256 TEXT PRIMARY KEY, decision TEXT NOT NULL, media_item_id TEXT, product_url TEXT);
        INSERT INTO person VALUES (1, x'00');
        INSERT INTO media_hash VALUES ('a', 'no_match', NULL, NULL), ('b', 'uploaded', 'i', 'u');
        INSERT INTO attachment (id, message_id, channel_id, filename, size, url, status, updated_at) VALUES
            (1, 1, 1, 'no-match.jpg', 1, 'u', 'no_match', 0), (2, 1, 1, 'done.jpg', 1, 'u', 'uploaded', 0);
        """
    )
    db.commit()
    db.close()

    store = Store(path)
    await store.open()
    assert await store.status_counts() == {}
    await store.close()

    db = sqlite3.connect(path)
    tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    columns = {row[1] for row in db.execute("PRAGMA table_info(attachment)")}
    db.close()
    assert "person" not in tables and "attachment_person" not in tables
    # The author columns were added to the old table.
    assert {"author_name", "author_handle"} <= columns


def _tags(data: bytes, suffix: str) -> dict:
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / f"file{suffix}"
        path.write_bytes(data)
        out = subprocess.run(["exiftool", "-j", "-G1", "-time:all", str(path)], check=True, capture_output=True).stdout
    return json.loads(out)[0]


def _author_meta(data: bytes, suffix: str) -> str:
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / f"file{suffix}"
        path.write_bytes(data)
        return subprocess.run(
            ["exiftool", "-s3", "-XMP-dc:Creator", "-EXIF:Artist", "-QuickTime:Artist", str(path)],
            check=True, capture_output=True, text=True,
        ).stdout


@pytest.mark.skipif(shutil.which("exiftool") is None, reason="needs exiftool")
async def test_dates_pins_and_skipped_role(harness: Harness) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    thread = fake.add_thread(general, "chat")
    fake.member_roles[BOB_ID] = [CLANKERS_ROLE_ID]
    jpeg = fake.add_message(general, [("a.jpg", real_image("JPEG"), JPEG)])
    fake.add_message(general, [("b.png", real_image("PNG"), "image/png")])
    fake.add_message(general, [("c.webp", real_image("WEBP"), "image/webp")])
    video = fake.add_message(general, [("v.mp4", real_video(".mp4"), "video/mp4")])
    fake.add_message(general, [("w.webm", real_video(".webm"), "video/webm")])
    fake.add_message(general, [("from-bot.jpg", real_image("JPEG"), JPEG)], author=BOB_ID)
    pin = fake.add_pin(general, "Remember the meeting \U0001F600 <:party:123>\n" + "long words " * 80)
    fake.add_pin(thread, "pinned in a thread")
    fake.add_pin(general, "pin by a bot", author=BOB_ID)
    await harness.start()
    await until(lambda: len(harness.uploaded()) >= 7, 60, "the uploads")
    await until(harness.drained, 30, "the queue")

    names = harness.uploaded()
    assert not {n for n in names if "from-bot" in n}
    assert len([n for n in names if n.startswith("pinned-")]) == 2

    zone = ZoneInfo("America/Toronto")
    expected = discord.utils.snowflake_time(int(jpeg["id"])).astimezone(zone).strftime("%Y:%m:%d %H:%M:%S")
    by_name = {item["filename"]: item["data"] for item in fake.items}
    for name, suffix in (("a.jpg", ".jpg"), ("b.png", ".png"), ("c.webp", ".webp")):
        tags = _tags(by_name[name], suffix)
        assert tags["ExifIFD:DateTimeOriginal"] == expected, (name, tags)
        assert tags["ExifIFD:OffsetTimeOriginal"] in {"-05:00", "-04:00"}

    utc = discord.utils.snowflake_time(int(video["id"])).astimezone(timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
    assert _tags(by_name["v.mp4"], ".mp4")["Track1:MediaCreateDate"] == utc or _tags(by_name["v.mp4"], ".mp4")["QuickTime:CreateDate"] == utc
    converted = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time", "-of", "csv=p=0", "pipe:0"],
        input=by_name["w.mp4"], capture_output=True, check=True,
    ).stdout.decode()
    assert converted.startswith(utc[:10].replace(":", "-"))

    card = next(item for item in fake.items if item["filename"].startswith("pinned-general") and str(pin["id"]) in item["filename"])
    assert card["mime"] == "image/jpeg"
    assert _tags(card["data"], ".jpg")["ExifIFD:DateTimeOriginal"][:4] == expected[:4]
    Path(tempfile.gettempdir(), "pin-sample.jpg").write_bytes(card["data"])

    # The sender's name is in each caption (shown + searchable in Google Photos)...
    by_item = {item["filename"]: item for item in fake.items}
    assert by_item["a.jpg"]["description"] == "Sent by user2000 in #general"
    assert by_item["v.mp4"]["description"] == "Sent by user2000 in #general"
    assert card["description"] == "Pinned message from user2000 in #general"
    # ...and embedded in the file metadata.
    assert "user2000" in _author_meta(by_name["a.jpg"], ".jpg")
    assert "user2000" in _author_meta(by_name["b.png"], ".png")
    assert "user2000" in _author_meta(by_name["v.mp4"], ".mp4")

    # A new pin is picked up right away, without waiting for the next scan.
    fake.add_pin(general, "brand new pin")
    await fake.pins_updated(general)
    await until(lambda: len([n for n in harness.uploaded() if n.startswith("pinned-")]) == 3, 30, "the live pin")

    await fake.live_message(general, [("live-bot.jpg", photo(50), JPEG)], author=BOB_ID)
    await fake.live_message(general, [("live-person.jpg", photo(51), JPEG)])
    await until(lambda: "live-person.jpg" in harness.uploaded(), 30, "the live photo")
    assert "live-bot.jpg" not in harness.uploaded()


async def test_pinned_message_embeds_its_images(harness: Harness) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    red = _encoded(Image.new("RGB", (600, 400), (220, 20, 20)), "PNG")
    blue = _encoded(Image.new("RGB", (600, 400), (20, 20, 220)), "PNG")
    pin = fake.add_pin(general, "look at this", files=[("photo.png", red, "image/png")])
    only_image = fake.add_pin(general, "", files=[("solo.png", blue, "image/png")])
    await harness.start()
    await until(lambda: len([n for n in harness.uploaded() if n.startswith("pinned-")]) >= 2, 60, "the pin cards")
    await until(harness.drained, 30, "the queue")

    def card(message: dict) -> dict:
        return next(i for i in fake.items if i["filename"].startswith("pinned-") and str(message["id"]) in i["filename"])

    def count(data: bytes, keep) -> int:
        picture = Image.open(io.BytesIO(data)).convert("RGB")
        lower = picture.crop((0, picture.height // 2, picture.width, picture.height))
        return sum(1 for pixel in lower.getdata() if keep(*pixel))

    # Each card embeds its own attachment (red for one, blue for the other).
    assert count(card(pin)["data"], lambda r, g, b: r > 150 and g < 90 and b < 90) > 2000
    assert count(card(only_image)["data"], lambda r, g, b: b > 150 and r < 90 and g < 90) > 2000

    # The attached images are still uploaded on their own too.
    assert "photo.png" in harness.uploaded() and "solo.png" in harness.uploaded()


def _scene(seed: int) -> Image.Image:
    rng = random.Random(seed)
    image = Image.new("RGB", (900, 600), (rng.randrange(255), rng.randrange(255), rng.randrange(255)))
    draw = ImageDraw.Draw(image)
    for _ in range(40):
        x, y = rng.randrange(900), rng.randrange(600)
        size = rng.randrange(40, 240)
        draw.ellipse((x, y, x + size, y + size), fill=(rng.randrange(255), rng.randrange(255), rng.randrange(255)))
    return image


def _encoded(image: Image.Image, image_format: str = "JPEG", **options: object) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format=image_format, **options)
    return buffer.getvalue()


async def test_repeated_photos_are_detected(harness: Harness) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    original = _scene(1)
    fake.add_message(general, [("original.jpg", _encoded(original, quality=95), JPEG)])
    fake.add_message(general, [("exact.jpg", _encoded(original, quality=95), JPEG)])
    fake.add_message(general, [("compressed.jpg", _encoded(original, quality=35), JPEG)])
    fake.add_message(general, [("resized.png", _encoded(original.resize((450, 300)), "PNG"), "image/png")])
    for seed in range(2, 8):
        fake.add_message(general, [(f"different-{seed}.jpg", _encoded(_scene(seed), quality=90), JPEG)])
    await harness.start()
    await until(lambda: "different-7.jpg" in harness.uploaded(), 30, "the uploads")
    await until(harness.drained, 30, "the queue")
    statuses = await harness.statuses()
    assert {statuses[n] for n in ("exact.jpg", "compressed.jpg", "resized.png")} == {"duplicate"}, statuses
    assert statuses["original.jpg"] == "uploaded"
    assert all(statuses[f"different-{seed}.jpg"] == "uploaded" for seed in range(2, 8)), statuses

    # It remembers across a restart.
    await harness.stop()
    fake.add_message(general, [("later.jpg", _encoded(original, quality=60), JPEG)])
    await harness.start()
    await until(lambda: _status_is(harness, "later.jpg", "duplicate"), 30, "the repeat after a restart")


async def test_message_log_goes_to_drive(harness: Harness) -> None:
    fake = harness.fake
    general = fake.add_channel("general")
    other = fake.add_channel("aaa-chat")
    thread = fake.add_thread(general, "side-talk")
    hidden = fake.add_channel("secret", hidden=True)
    fake.member_roles[BOB_ID] = [CLANKERS_ROLE_ID]

    def say(channel: int, text: str, author: int = OWNER_ID) -> None:
        fake.add_message(channel, [], author=author)["content"] = text

    say(general, "first in general")
    say(general, "second in general")
    say(other, "hello from aaa")
    say(thread, "thread message")
    say(hidden, "cannot be read")
    say(general, "bot spam", BOB_ID)
    fake.add_message(general, [("pic.jpg", photo(1), JPEG)])

    client = await harness.start()
    await until(lambda: fake.drive_files, 30, "the Drive file")
    saved = fake.drive_files["file-0"]
    text = saved["data"].decode()
    assert saved["name"] == "Discord Server Log.txt" and len(fake.drive_files) == 1
    assert text.index("#aaa-chat") < text.index("#general") < text.index("#general > side-talk")
    assert text.index("first in general") < text.index("second in general")
    assert "user2000: thread message" in text and "[Attached: pic.jpg]" in text
    assert "bot spam" not in text and "cannot be read" not in text

    # Live messages are included, and the same file gets updated.
    await fake.live_message(general, [])
    fake.messages[general][-1]["content"] = ""
    message = fake.add_message(general, [])
    message["content"] = "said while online"
    await fake.dispatch("MESSAGE_CREATE", message)
    await until(lambda: _logged(client, "said while online"), 20, "the live message")
    await client.drive_log.export()
    assert len(fake.drive_files) == 1 and fake.drive_files["file-0"]["writes"] == 2
    assert "said while online" in fake.drive_files["file-0"]["data"].decode()

    # If you delete it from Drive, a new one is made.
    fake.drive_files.clear()
    await client.drive_log.export()
    assert list(fake.drive_files) == ["file-0"]

    # Missing Drive access tells the owner instead of crashing.
    before = len(fake.sent)
    fake.drive_status = 403
    await client.drive_log.safe_export(client.alert_owner)
    assert len(fake.sent) == before + 1 and "Google Drive API" in fake.sent[-1]["payload"]["content"]


async def _logged(client: object, text: str) -> bool:
    async with client.store.db.execute("SELECT 1 FROM log_message WHERE text = ?", (text,)) as cursor:  # type: ignore[attr-defined]
        return await cursor.fetchone() is not None


async def test_one_time_reset_on_upgrade(tmp_path: Path) -> None:
    path = tmp_path / "archive.db"
    store = Store(path)
    await store.open()
    await store.add_attachments([{"id": 1, "message_id": 1, "channel_id": 1, "filename": "a.jpg", "content_type": None, "size": 1, "url": "u"}])
    await store.kv_set("album_id", "keep-after-reset")
    await store.close()
    store = Store(path)
    await store.open()
    assert await store.status_counts() == {"queued": 1} and await store.kv_get("album_id") == "keep-after-reset"
    await store.close()

    db = sqlite3.connect(path)
    db.execute("DELETE FROM kv WHERE k = 'reset_for_pin_media'")
    db.commit()
    db.close()
    store = Store(path)
    await store.open()
    assert await store.status_counts() == {} and await store.kv_get("album_id") is None
    await store.close()


def test_ffmpeg_refuses_disguised_playlists(tmp_path: Path) -> None:
    trap = tmp_path / "trap.webm"
    trap.write_bytes(HLS_TRAP)
    with pytest.raises(media.MediaError):
        media.transcode_to_mp4(trap, tmp_path)


def test_file_classification() -> None:
    assert media.classify("a.JPG") == media.IMAGE
    assert media.classify("a.gif") == media.IMAGE
    assert media.classify("a.webm") == media.VIDEO
    assert media.classify("notes.txt", "text/plain") is None
    assert media.classify("logo.svg", "image/svg+xml") is None
    assert media.classify("noext", "video/mp4") == media.VIDEO
    assert media.safe_extension("../../evil.sh", "image/jpeg") == ".jpg"


def test_upload_names_are_cleaned(tmp_path: Path) -> None:
    source = tmp_path / "input.jpg"
    source.write_bytes(b"x")
    when = discord.utils.snowflake_time(1_100_000_000_000_000_000)
    zone = ZoneInfo("America/Toronto")
    assert media.prepare_upload(source, "../../a.jpg", media.IMAGE, tmp_path, when, zone, "Sam").filename == "a.jpg"
    ready = media.prepare_upload(source, "x" * 400 + ".jpg", media.IMAGE, tmp_path, when, zone, "Sam")
    assert len(ready.filename) == 200 and ready.filename.endswith(".jpg")


def test_daily_sync_time() -> None:
    from datetime import datetime

    zone = ZoneInfo("America/Toronto")
    env = {"DISCORD_TOKEN": "t", "DISCORD_GUILD_ID": "1", "DISCORD_OWNER_ID": "2", "ENCRYPTION_KEY": Fernet.generate_key().decode()}
    config = Config.from_env(env)
    assert config.seconds_until_sync(datetime(2026, 11, 8, 18, 0, tzinfo=zone)) == 3600
    assert config.seconds_until_sync(datetime(2026, 11, 8, 19, 0, tzinfo=zone)) == 24 * 3600
    # The day the clocks go back is 25 hours long.
    assert config.seconds_until_sync(datetime(2026, 10, 31, 19, 0, tzinfo=zone)) == 25 * 3600
    with pytest.raises(ConfigError, match="SYNC_TIME"):
        Config.from_env({**env, "SYNC_TIME": "7pm"})


def test_settings_validation() -> None:
    base = {
        "DISCORD_TOKEN": "t",
        "DISCORD_GUILD_ID": "1",
        "DISCORD_OWNER_ID": "2",
        "ENCRYPTION_KEY": Fernet.generate_key().decode(),
    }
    assert Config.from_env(base).album_name == "Discord Archive"
    with pytest.raises(ConfigError, match="DISCORD_TOKEN"):
        Config.from_env({k: v for k, v in base.items() if k != "DISCORD_TOKEN"})
    with pytest.raises(ConfigError, match="whole number"):
        Config.from_env({**base, "DISCORD_GUILD_ID": "abc"})
    assert Config.from_env({**base, "SKIP_CHANNEL_IDS": "5, 6"}).skip_channel_ids == {5, 6}


async def test_check_command(harness: Harness, capsys: pytest.CaptureFixture[str]) -> None:
    config = harness.config()
    harness.save_google_login(config)
    google = f"{harness.fake.base}/google/v1"
    drive = f"{harness.fake.base}/drive/about"
    assert await check.run_check(config, google, drive) == 0
    assert "All good." in capsys.readouterr().out

    harness.fake.app_flags = 0
    assert await check.run_check(config, google, drive) == 1
    assert "Message Content Intent" in capsys.readouterr().out

    harness.fake.app_flags = 1 << 19
    assert await check.run_check(config, google, drive) == 1
    out = capsys.readouterr().out
    assert "Server Members Intent" in out and "Presence Intent" in out

    harness.fake.app_flags = (1 << 13) | (1 << 15) | (1 << 19)
    harness.fake.google_failures["token"] = [(400, {"error": "invalid_grant"})]
    store = Store(config.db_file)
    await store.open()
    await store.kv_delete("album_id")
    await store.close()
    assert await check.run_check(config, google, drive) == 1
    assert "/archive google" in capsys.readouterr().out

    assert await check.run_check(harness.config(DISCORD_TOKEN="wrong"), google, drive) == 1
    assert "Discord token works" in capsys.readouterr().out


def test_google_login(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OAUTHLIB_INSECURE_TRANSPORT", "1")
    loop = asyncio.new_event_loop()
    services = FakeServices()
    loop.run_until_complete(services.start())
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    try:
        secret = tmp_path / "client_secret.json"
        secret.write_text(
            json.dumps(
                {
                    "installed": {
                        "client_id": "client",
                        "client_secret": "shh",
                        "auth_uri": f"{services.base}/google/auth",
                        "token_uri": f"{services.base}/google/token",
                        "redirect_uris": ["http://localhost"],
                    }
                }
            )
        )
        key = Fernet.generate_key().decode()
        config = Config.from_env(
            {"ENCRYPTION_KEY": key, "DATA_DIR": str(tmp_path / "data"), "GOOGLE_CLIENT_SECRET_FILE": str(secret)},
            need_discord=False,
        )
        printed: list[str] = []

        def browser(_: str) -> str:
            url = next(line for line in printed if line.startswith("http"))
            query = parse_qs(urlparse(url).query)
            assert query["access_type"] == ["offline"] and query["prompt"] == ["consent"]
            assert query["scope"] == [
                "https://www.googleapis.com/auth/photoslibrary.appendonly https://www.googleapis.com/auth/drive.file"
            ]
            return f"http://localhost:8765/?state={query['state'][0]}&code=abc"

        assert google_login.run_login(config, ask=browser, say=printed.append) == 0
        saved = json.loads(Vault(key).unseal(config.token_file.read_bytes()))
        assert saved["refresh_token"] == "refresh-from-login"
        assert b"refresh-from-login" not in config.token_file.read_bytes()
        assert oct(config.token_file.stat().st_mode)[-3:] == "600"
        assert google_login.run_login(config, ask=lambda _: "http://localhost:8765/?state=wrong&code=abc", say=printed.append) == 1
    finally:
        asyncio.run_coroutine_threadsafe(services.stop(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        thread.join(5)
