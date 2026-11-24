"""SQLite state: scan progress and the upload queue."""
from __future__ import annotations

import asyncio
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import AsyncIterator, Iterable, Mapping, Sequence

import aiosqlite

QUEUED = "queued"
UPLOADED = "uploaded"
DUPLICATE = "duplicate"
SKIPPED = "skipped"
FAILED = "failed"

SCHEMA = """
CREATE TABLE IF NOT EXISTS channel_cursor (
    channel_id INTEGER PRIMARY KEY,
    last_message_id INTEGER NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS attachment (
    id INTEGER PRIMARY KEY,
    message_id INTEGER NOT NULL,
    channel_id INTEGER NOT NULL,
    filename TEXT NOT NULL,
    content_type TEXT,
    size INTEGER NOT NULL,
    url TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'queued',
    attempts INTEGER NOT NULL DEFAULT 0,
    next_try_at REAL NOT NULL DEFAULT 0,
    sha256 TEXT,
    media_item_id TEXT,
    product_url TEXT,
    author_name TEXT,
    author_handle TEXT,
    note TEXT,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS attachment_work ON attachment(status, id);
CREATE TABLE IF NOT EXISTS media_hash (
    sha256 TEXT PRIMARY KEY,
    decision TEXT NOT NULL,
    media_item_id TEXT,
    product_url TEXT
);
CREATE TABLE IF NOT EXISTS image_look (
    attachment_id INTEGER PRIMARY KEY,
    look TEXT NOT NULL,
    ratio REAL NOT NULL,
    media_item_id TEXT,
    product_url TEXT
);
CREATE TABLE IF NOT EXISTS log_channel (
    channel_id INTEGER PRIMARY KEY,
    label TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_message (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    author TEXT NOT NULL,
    text TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS log_message_channel ON log_message(channel_id, message_id);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS meta_cache (id TEXT PRIMARY KEY, json TEXT NOT NULL, fetched_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS tracker_user (
    user_id INTEGER PRIMARY KEY,
    token TEXT NOT NULL UNIQUE,
    private INTEGER NOT NULL DEFAULT 0,
    pings INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS watch_event (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    title_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    type TEXT NOT NULL,
    season INTEGER,
    episode INTEGER,
    watched_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS watch_event_user ON watch_event(user_id, watched_at);
CREATE INDEX IF NOT EXISTS watch_event_title ON watch_event(title_id);
CREATE TABLE IF NOT EXISTS watch_post (
    user_id INTEGER NOT NULL,
    video_id TEXT NOT NULL,
    channel_id INTEGER NOT NULL,
    message_id INTEGER NOT NULL,
    posted_at REAL NOT NULL,
    offset_ms INTEGER,
    duration_ms INTEGER,
    state TEXT NOT NULL DEFAULT 'watching',
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, video_id)
);
CREATE TABLE IF NOT EXISTS stremio_account (
    user_id INTEGER PRIMARY KEY,
    auth_key BLOB NOT NULL,
    email TEXT,
    added_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS rating (
    user_id INTEGER NOT NULL,
    title_id TEXT NOT NULL,
    type TEXT NOT NULL,
    score INTEGER NOT NULL,
    review TEXT,
    updated_at REAL NOT NULL,
    PRIMARY KEY (user_id, title_id)
);
CREATE TABLE IF NOT EXISTS watchlist (
    user_id INTEGER NOT NULL,
    title_id TEXT NOT NULL,
    type TEXT NOT NULL,
    added_at REAL NOT NULL,
    PRIMARY KEY (user_id, title_id)
);
CREATE TABLE IF NOT EXISTS ranked_list (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS ranked_list_name ON ranked_list(user_id, name COLLATE NOCASE);
CREATE TABLE IF NOT EXISTS ranked_item (
    list_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    title_id TEXT NOT NULL,
    type TEXT NOT NULL,
    PRIMARY KEY (list_id, title_id)
);
CREATE TABLE IF NOT EXISTS recommendation (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    from_user INTEGER NOT NULL,
    to_user INTEGER NOT NULL,
    title_id TEXT NOT NULL,
    type TEXT NOT NULL,
    note TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS followed_show (
    user_id INTEGER NOT NULL,
    title_id TEXT NOT NULL,
    last_seen_episode TEXT,
    PRIMARY KEY (user_id, title_id)
);
CREATE TABLE IF NOT EXISTS episode_alert (
    title_id TEXT NOT NULL,
    video_id TEXT NOT NULL,
    PRIMARY KEY (title_id, video_id)
);
CREATE TABLE IF NOT EXISTS movie_night (
    message_id INTEGER PRIMARY KEY,
    channel_id INTEGER NOT NULL,
    options TEXT NOT NULL,
    ends_at REAL NOT NULL,
    closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS movie_night_vote (
    message_id INTEGER NOT NULL,
    user_id INTEGER NOT NULL,
    choice INTEGER NOT NULL,
    PRIMARY KEY (message_id, user_id)
);
CREATE TABLE IF NOT EXISTS lyrics_user (user_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS game_catalog (
    app_id TEXT PRIMARY KEY, name TEXT NOT NULL, name_norm TEXT NOT NULL, icon_hash TEXT, cover_hash TEXT,
    steam_appid INTEGER, themes TEXT
);
CREATE INDEX IF NOT EXISTS game_catalog_name ON game_catalog (name_norm);
CREATE INDEX IF NOT EXISTS game_catalog_steam ON game_catalog (steam_appid);
CREATE TABLE IF NOT EXISTS game_alias (alias_norm TEXT NOT NULL, app_id TEXT NOT NULL, PRIMARY KEY (alias_norm, app_id));
CREATE TABLE IF NOT EXISTS game_name_check (name_norm TEXT PRIMARY KEY, steam_appid INTEGER, name TEXT, checked_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS game (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, discord_id TEXT, steam_appid INTEGER, cover TEXT, icon TEXT, themes TEXT,
    first_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS game_open (
    user_id INTEGER PRIMARY KEY, game_key TEXT NOT NULL, started_at REAL NOT NULL, last_seen REAL NOT NULL,
    source TEXT NOT NULL, channel_id INTEGER, message_id INTEGER
);
CREATE TABLE IF NOT EXISTS game_session (
    id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, game_key TEXT NOT NULL, started_at REAL NOT NULL,
    ended_at REAL NOT NULL, seconds REAL NOT NULL, source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS game_session_user ON game_session (user_id, ended_at);
CREATE INDEX IF NOT EXISTS game_session_game ON game_session (game_key, ended_at);
CREATE TABLE IF NOT EXISTS game_optout (user_id INTEGER PRIMARY KEY, at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS steam_link (
    user_id INTEGER PRIMARY KEY, steam_id TEXT NOT NULL, persona TEXT, avatar TEXT, linked_at REAL NOT NULL, library_at REAL
);
CREATE TABLE IF NOT EXISTS steam_library (
    user_id INTEGER NOT NULL, appid INTEGER NOT NULL, name TEXT, minutes INTEGER NOT NULL, minutes_2w INTEGER NOT NULL,
    PRIMARY KEY (user_id, appid)
);
"""

# Clears out data from the earlier face-matching version, including face profiles.
MIGRATION = """
DROP TABLE IF EXISTS attachment_person;
DROP TABLE IF EXISTS person;
DELETE FROM media_hash WHERE decision != 'uploaded';
UPDATE attachment SET status = 'queued', attempts = 0, next_try_at = 0, note = NULL
    WHERE status IN ('no_face', 'no_match', 'matched', 'review', 'rejected', 'opted_out');
"""

# One time only: items uploaded before dates were added have to go up again,
# into a new album, and the scan restarts so the role filter applies to everything.
_RESET_KEY = "reset_for_pin_media"
RESET_FOR_DATES = """
DELETE FROM attachment;
DELETE FROM channel_cursor;
DELETE FROM media_hash;
DELETE FROM image_look;
DELETE FROM log_message;
DELETE FROM log_channel;
DELETE FROM kv WHERE k = 'album_id';
"""

_JOB_COLUMNS = ("id, message_id, channel_id, filename, content_type, size, url, status, "
                "attempts, author_name, author_handle")


@dataclass(frozen=True)
class Job:
    id: int
    message_id: int
    channel_id: int
    filename: str
    content_type: str | None
    size: int
    url: str
    status: str
    attempts: int
    author_name: str | None
    author_handle: str | None


@dataclass(frozen=True)
class HashEntry:
    decision: str
    media_item_id: str | None
    product_url: str | None


class Store:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._db: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self._db = await aiosqlite.connect(self.path, isolation_level=None)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA journal_mode=WAL")
        await self._db.executescript(SCHEMA)
        await self._ensure_columns()
        await self._db.executescript(MIGRATION)
        if await self.kv_get(_RESET_KEY) is None:
            await self._db.executescript(RESET_FOR_DATES)
            await self.kv_set(_RESET_KEY, "done")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    async def _ensure_columns(self) -> None:
        """Add columns to an attachment table made by an older version."""
        async with self.db.execute("PRAGMA table_info(attachment)") as cursor:
            have = {row["name"] for row in await cursor.fetchall()}
        for column in ("author_name", "author_handle"):
            if column not in have:
                await self.db.execute(f"ALTER TABLE attachment ADD COLUMN {column} TEXT")

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Store is not open.")
        return self._db

    @asynccontextmanager
    async def _tx(self) -> AsyncIterator[aiosqlite.Connection]:
        async with self._lock:
            await self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
            except BaseException:
                await self.db.execute("ROLLBACK")
                raise
            else:
                await self.db.execute("COMMIT")

    # ---- shared helpers for the tracker and lyrics -----------------------

    def transaction(self):  # noqa: ANN201 - async context manager
        return self._tx()

    async def execute(self, sql: str, args: Sequence = ()) -> int:
        async with self._tx() as db:
            cursor = await db.execute(sql, args)
            return cursor.lastrowid if sql.lstrip().upper().startswith("INSERT") else cursor.rowcount

    async def fetch_one(self, sql: str, args: Sequence = ()) -> aiosqlite.Row | None:
        return await self._one(sql, args)

    async def fetch_all(self, sql: str, args: Sequence = ()) -> list[aiosqlite.Row]:
        async with self.db.execute(sql, args) as cursor:
            return list(await cursor.fetchall())

    async def _one(self, sql: str, args: Sequence = ()) -> aiosqlite.Row | None:
        async with self.db.execute(sql, args) as cursor:
            return await cursor.fetchone()

    # ---- key/value -------------------------------------------------------

    async def kv_get(self, key: str) -> str | None:
        row = await self._one("SELECT v FROM kv WHERE k = ?", (key,))
        return None if row is None else row["v"]

    async def kv_set(self, key: str, value: str) -> None:
        async with self._tx() as db:
            await db.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) "
                "ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )

    async def kv_delete(self, key: str) -> None:
        async with self._tx() as db:
            await db.execute("DELETE FROM kv WHERE k = ?", (key,))

    # ---- crawl progress --------------------------------------------------

    async def cursor(self, channel_id: int) -> int:
        row = await self._one(
            "SELECT last_message_id FROM channel_cursor WHERE channel_id = ?",
            (channel_id,),
        )
        return 0 if row is None else int(row["last_message_id"])

    async def record_batch(
        self,
        channel_id: int,
        rows: Iterable[Mapping],
        last_message_id: int,
        logs: Iterable[Mapping] = (),
        *,
        already_saved: bool = False,
    ) -> None:
        """Queue attachments and move the channel cursor in one transaction."""
        now = time.time()
        async with self._tx() as db:
            await self._insert_attachments(db, rows, now, already_saved)
            await self._insert_logs(db, logs)
            await db.execute(
                "INSERT INTO channel_cursor (channel_id, last_message_id, updated_at) "
                "VALUES (?, ?, ?) ON CONFLICT(channel_id) DO UPDATE SET "
                "last_message_id = MAX(last_message_id, excluded.last_message_id), "
                "updated_at = excluded.updated_at",
                (channel_id, last_message_id, now),
            )

    async def add_attachments(self, rows: Iterable[Mapping], *, already_saved: bool = False) -> None:
        async with self._tx() as db:
            await self._insert_attachments(db, rows, time.time(), already_saved)

    @staticmethod
    async def _insert_attachments(
        db: aiosqlite.Connection, rows: Iterable[Mapping], now: float, already_saved: bool = False
    ) -> None:
        # already_saved: media from before a fresh start, which is in the album already.
        status, note = ("uploaded", "from before the fresh start") if already_saved else ("queued", None)
        await db.executemany(
            "INSERT OR IGNORE INTO attachment "
            "(id, message_id, channel_id, filename, content_type, size, url, "
            "author_name, author_handle, status, note, updated_at) "
            "VALUES (:id, :message_id, :channel_id, :filename, :content_type, :size, :url, "
            ":author_name, :author_handle, :status, :note, :updated_at)",
            [{"author_name": None, "author_handle": None, **row,
              "status": status, "note": note, "updated_at": now} for row in rows],
        )

    # ---- message log -----------------------------------------------------

    @staticmethod
    async def _insert_logs(db: aiosqlite.Connection, logs: Iterable[Mapping]) -> None:
        await db.executemany(
            "INSERT OR IGNORE INTO log_message (message_id, channel_id, author, text) "
            "VALUES (:message_id, :channel_id, :author, :text)",
            list(logs),
        )

    async def add_logs(self, logs: Iterable[Mapping]) -> None:
        async with self._tx() as db:
            await self._insert_logs(db, logs)

    async def set_channel_label(self, channel_id: int, label: str) -> None:
        async with self._tx() as db:
            await db.execute(
                "INSERT INTO log_channel (channel_id, label) VALUES (?, ?) "
                "ON CONFLICT(channel_id) DO UPDATE SET label = excluded.label",
                (channel_id, label),
            )

    async def channel_label(self, channel_id: int) -> str | None:
        row = await self._one("SELECT label FROM log_channel WHERE channel_id = ?", (channel_id,))
        return None if row is None else row["label"]

    async def log_rows(self) -> AsyncIterator[tuple[int, str, int, str, str]]:
        async with self.db.execute(
            "SELECT m.channel_id, COALESCE(c.label, '#' || m.channel_id) AS label, m.message_id, m.author, m.text "
            "FROM log_message m LEFT JOIN log_channel c ON c.channel_id = m.channel_id "
            "ORDER BY label COLLATE NOCASE, m.channel_id, m.message_id"
        ) as cursor:
            async for row in cursor:
                yield row[0], row[1], row[2], row[3], row[4]

    # ---- upload queue ----------------------------------------------------

    async def next_job(self) -> Job | None:
        row = await self._one(
            f"SELECT {_JOB_COLUMNS} FROM attachment "
            "WHERE status = ? AND next_try_at <= ? ORDER BY id LIMIT 1",
            (QUEUED, time.time()),
        )
        return None if row is None else Job(**dict(row))

    async def update_url(self, attachment_id: int, url: str) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE attachment SET url = ?, updated_at = ? WHERE id = ?",
                (url, time.time(), attachment_id),
            )

    async def finish(self, attachment_id: int, status: str, *, note: str | None = None) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE attachment SET status = ?, note = ?, updated_at = ? WHERE id = ?",
                (status, note, time.time(), attachment_id),
            )

    async def retry_later(self, attachment_id: int, delay: float, note: str) -> None:
        now = time.time()
        async with self._tx() as db:
            await db.execute(
                "UPDATE attachment SET attempts = attempts + 1, next_try_at = ?, note = ?, "
                "updated_at = ? WHERE id = ?",
                (now + delay, note[:500], now, attachment_id),
            )

    async def mark_uploaded(
        self, attachment_id: int, sha256: str, media_item_id: str, product_url: str | None
    ) -> None:
        now = time.time()
        async with self._tx() as db:
            await db.execute(
                "UPDATE attachment SET status = ?, sha256 = ?, media_item_id = ?, product_url = ?, "
                "note = NULL, updated_at = ? WHERE id = ?",
                (UPLOADED, sha256, media_item_id, product_url, now, attachment_id),
            )
            await db.execute(
                "INSERT INTO media_hash (sha256, decision, media_item_id, product_url) "
                "VALUES (?, ?, ?, ?) ON CONFLICT(sha256) DO UPDATE SET "
                "decision = excluded.decision, media_item_id = excluded.media_item_id, "
                "product_url = excluded.product_url",
                (sha256, UPLOADED, media_item_id, product_url),
            )

    async def mark_duplicate(self, attachment_id: int, sha256: str, entry: HashEntry) -> None:
        async with self._tx() as db:
            await db.execute(
                "UPDATE attachment SET status = ?, sha256 = ?, media_item_id = ?, product_url = ?, "
                "note = NULL, updated_at = ? WHERE id = ?",
                (DUPLICATE, sha256, entry.media_item_id, entry.product_url, time.time(), attachment_id),
            )

    async def hash_get(self, sha256: str) -> HashEntry | None:
        row = await self._one(
            "SELECT decision, media_item_id, product_url FROM media_hash WHERE sha256 = ?",
            (sha256,),
        )
        return None if row is None else HashEntry(**dict(row))

    async def add_look(
        self, attachment_id: int, look: int, ratio: float, media_item_id: str, product_url: str | None
    ) -> None:
        async with self._tx() as db:
            await db.execute(
                "INSERT OR REPLACE INTO image_look VALUES (?, ?, ?, ?, ?)",
                (attachment_id, format(look, "x"), ratio, media_item_id, product_url),
            )

    async def all_looks(self) -> list[tuple[tuple[int, float], HashEntry]]:
        async with self.db.execute(
            "SELECT look, ratio, media_item_id, product_url FROM image_look"
        ) as cursor:
            return [
                ((int(row["look"], 16), float(row["ratio"])), HashEntry(UPLOADED, row["media_item_id"], row["product_url"]))
                for row in await cursor.fetchall()
            ]

    async def requeue_failed(self) -> int:
        async with self._tx() as db:
            cursor = await db.execute(
                "UPDATE attachment SET status = ?, attempts = 0, next_try_at = 0, note = NULL, "
                "updated_at = ? WHERE status = ?",
                (QUEUED, time.time(), FAILED),
            )
            return cursor.rowcount

    async def cancel_for_messages(self, message_ids: Iterable[int]) -> None:
        """Stop work on anything from messages that have been deleted."""
        now = time.time()
        async with self._tx() as db:
            await db.executemany(
                "UPDATE attachment SET status = ?, note = 'message deleted', updated_at = ? "
                "WHERE message_id = ? AND status = ?",
                [(SKIPPED, now, message_id, QUEUED) for message_id in message_ids],
            )

    async def status_counts(self) -> dict[str, int]:
        async with self.db.execute(
            "SELECT status, COUNT(*) AS n FROM attachment GROUP BY status"
        ) as cursor:
            return {row["status"]: int(row["n"]) for row in await cursor.fetchall()}
