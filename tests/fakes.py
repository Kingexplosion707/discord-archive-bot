"""Stand-ins for Discord's and Google's servers, speaking their real wire formats.

The bot runs the real discord.py library against these, so gateway events,
permissions, slash commands and buttons all go through the real code paths.
"""
from __future__ import annotations

import asyncio
import itertools
import json
from typing import Any

from aiohttp import WSMsgType, web

VIEW_CHANNEL = 1 << 10
SEND_MESSAGES = 1 << 11
EMBED_LINKS = 1 << 14
ATTACH_FILES = 1 << 15
READ_HISTORY = 1 << 16
MANAGE_THREADS = 1 << 34
BOT_PERMISSIONS = VIEW_CHANNEL | SEND_MESSAGES | EMBED_LINKS | ATTACH_FILES | READ_HISTORY | MANAGE_THREADS

APP_ID = 900
BOT_ID = 901
GUILD_ID = 1000
ROLE_ID = 1001
CLANKERS_ROLE_ID = 1002
OWNER_ID = 2000
BOB_ID = 2001
MALLORY_ID = 2002
CAROL_ID = 2003
TIMESTAMP = "2026-01-01T00:00:00+00:00"


def _json(data: Any, status: int = 200) -> web.Response:
    # discord.py only parses a body whose content type is exactly application/json.
    return web.Response(body=json.dumps(data).encode(), status=status, headers={"Content-Type": "application/json"})


def user(user_id: int, name: str | None = None, *, bot: bool = False) -> dict[str, Any]:
    return {
        "id": str(user_id),
        "username": name or f"user{user_id}",
        "discriminator": "0",
        "global_name": None,
        "avatar": None,
        "bot": bot,
    }


def member(user_id: int, *, with_user: bool = True) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "roles": [str(ROLE_ID)] if user_id == BOT_ID else [],
        "joined_at": TIMESTAMP,
        "deaf": False,
        "mute": False,
        "flags": 0,
        "permissions": str(BOT_PERMISSIONS),
    }
    if with_user:
        payload["user"] = user(user_id, bot=user_id == BOT_ID)
    return payload


class FakeServices:
    def __init__(self) -> None:
        self.port = 0
        self._runner: web.AppRunner | None = None
        self._ids = itertools.count(1_100_000_000_000_000_000)
        self._ws: web.WebSocketResponse | None = None
        self._sequence = 0
        self.ready = asyncio.Event()

        # Discord state
        self.channels: dict[int, dict[str, Any]] = {}
        self.messages: dict[int, list[dict[str, Any]]] = {}
        self.active_threads: list[dict[str, Any]] = []
        self.archived_public: dict[int, list[dict[str, Any]]] = {}
        self.archived_private: dict[int, list[dict[str, Any]]] = {}
        self.files: dict[int, bytes] = {}
        self.signatures: dict[int, int] = {}
        self.identify: dict[str, Any] | None = None
        self.synced_commands: list[dict[str, Any]] = []
        self.sent: list[dict[str, Any]] = []
        self.replies: dict[str, list[dict[str, Any]]] = {}
        # Limited Presence (13), Guild Members (15) and Message Content (19) intents switched on.
        self.app_flags = (1 << 13) | (1 << 15) | (1 << 19)
        self.pins: dict[int, list[dict[str, Any]]] = {}
        self.member_roles: dict[int, list[int]] = {}

        # Google state
        self.albums: dict[str, str] = {}
        self.uploads: dict[str, dict[str, Any]] = {}
        self.items: list[dict[str, Any]] = []
        self.token_requests = 0
        self.google_failures: dict[str, list[Any]] = {"upload": [], "batch": [], "token": [], "album": []}
        self.valid_tokens: set[str] = set()
        self.interaction_channels: dict[str, int] = {}
        self.edits: list[dict[str, Any]] = []
        self.metas: dict[str, dict[str, Any]] = {}
        self.meta_requests = 0
        self.spicy: dict[str, dict[str, Any]] = {}
        self.spicy_auth: list[str] = []
        self.lrclib: dict[str, dict[str, Any]] = {}
        # Stremio accounts: email -> (password, authKey); library items per authKey.
        self.stremio_accounts: dict[str, tuple[str, str]] = {}
        self.stremio_library: dict[str, list[dict[str, Any]]] = {}
        self.drive_files: dict[str, dict[str, Any]] = {}
        self.drive_sessions: dict[str, dict[str, Any]] = {}
        self.drive_status = 200

    # ---- lifecycle -------------------------------------------------------

    async def start(self) -> None:
        app = web.Application(client_max_size=1024**3)
        r = app.router
        r.add_get("/gateway", self._gateway)
        api = "/api/v10"
        r.add_get(f"{api}/users/@me", self._me)
        r.add_get(f"{api}/users/@me/guilds", self._my_guilds)
        r.add_get(f"{api}/oauth2/applications/@me", self._application)
        r.add_put(f"{api}/applications/{{app}}/guilds/{{guild}}/commands", self._sync)
        r.add_get(f"{api}/guilds/{{guild}}", self._guild)
        r.add_get(f"{api}/guilds/{{guild}}/members/{{user}}", self._guild_member)
        r.add_get(f"{api}/guilds/{{guild}}/threads/active", self._threads_active)
        r.add_get(f"{api}/channels/{{channel}}/threads/archived/public", self._archived_public)
        r.add_get(f"{api}/channels/{{channel}}/threads/archived/private", self._archived_private)
        r.add_get(f"{api}/channels/{{channel}}/messages", self._history)
        r.add_get(f"{api}/channels/{{channel}}/messages/pins", self._pins)
        r.add_get(f"{api}/channels/{{channel}}/messages/{{message}}", self._message)
        r.add_post(f"{api}/channels/{{channel}}/messages", self._post_message)
        r.add_patch(f"{api}/channels/{{channel}}/messages/{{message}}", self._edit_message)
        r.add_get("/cinemeta/meta/{type}/{id}.json", self._meta)
        r.add_get("/kitsu/meta/{type}/{id}.json", self._meta)
        r.add_get("/cinemeta/catalog/{type}/top/{extra}", self._search)
        r.add_get("/spicy/v1/lyrics/{track}", self._spicy)
        r.add_get("/lrclib/api/get", self._lrclib_get)
        r.add_post("/stremio-api/api/login", self._stremio_login)
        r.add_post("/stremio-api/api/datastoreGet", self._stremio_library)
        r.add_get("/lrclib/api/search", self._lrclib_search)
        r.add_get(f"{api}/users/{{user}}", self._user)
        r.add_post(f"{api}/users/@me/channels", self._dm_channel)
        r.add_post(f"{api}/interactions/{{id}}/{{token}}/callback", self._interaction_callback)
        r.add_post(f"{api}/webhooks/{{app}}/{{token}}", self._followup)
        r.add_get("/cdn/{attachment}/{signature}/{filename}", self._cdn)
        r.add_post("/drive/upload", self._drive_start)
        r.add_patch("/drive/upload/{file}", self._drive_start)
        r.add_put("/drive/session/{session}", self._drive_put)
        r.add_get("/drive/about", self._drive_about)
        r.add_post("/google/token", self._google_token)
        r.add_post("/google/v1/uploads", self._google_upload)
        r.add_post("/google/v1/albums", self._google_album)
        r.add_post("/google/v1/mediaItems:batchCreate", self._google_batch)
        self._runner = web.AppRunner(app)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "127.0.0.1", 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]  # type: ignore[union-attr]

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def snowflake(self) -> int:
        return next(self._ids)

    # ---- building the fake server ----------------------------------------

    def add_channel(self, name: str, *, channel_type: int = 0, hidden: bool = False, nsfw: bool = False) -> int:
        channel_id = self.snowflake()
        overwrites = []
        if hidden:
            overwrites.append({"id": str(GUILD_ID), "type": 0, "allow": "0", "deny": str(VIEW_CHANNEL)})
        payload: dict[str, Any] = {
            "id": str(channel_id),
            "type": channel_type,
            "guild_id": str(GUILD_ID),
            "name": name,
            "position": len(self.channels),
            "permission_overwrites": overwrites,
            "nsfw": nsfw,
            "topic": None,
            "last_message_id": None,
            "parent_id": None,
            "rate_limit_per_user": 0,
        }
        if channel_type == 2:
            payload.update(bitrate=64000, user_limit=0, rtc_region=None)
        if channel_type == 15:
            payload.update(
                available_tags=[],
                default_reaction_emoji=None,
                default_thread_rate_limit_per_user=0,
                default_sort_order=None,
                default_forum_layout=0,
                flags=0,
            )
        self.channels[channel_id] = payload
        self.messages[channel_id] = []
        return channel_id

    def add_thread(self, parent_id: int, name: str, *, archived: bool = False, private: bool = False) -> int:
        thread_id = self.snowflake()
        payload = {
            "id": str(thread_id),
            "type": 12 if private else 11,
            "guild_id": str(GUILD_ID),
            "parent_id": str(parent_id),
            "owner_id": str(OWNER_ID),
            "name": name,
            "message_count": 0,
            "member_count": 1,
            "rate_limit_per_user": 0,
            "last_message_id": None,
            "flags": 0,
            "thread_metadata": {
                "archived": archived,
                "auto_archive_duration": 1440,
                "archive_timestamp": TIMESTAMP,
                "locked": False,
                "invitable": True,
            },
        }
        self.channels[thread_id] = payload
        self.messages[thread_id] = []
        if not archived:
            self.active_threads.append(payload)
        elif private:
            self.archived_private.setdefault(parent_id, []).append(payload)
        else:
            self.archived_public.setdefault(parent_id, []).append(payload)
        return thread_id

    def _attachment(self, attachment_id: int) -> dict[str, Any]:
        meta = self._attachment_meta[attachment_id]
        return {**meta, "url": self._signed_url(attachment_id), "proxy_url": self._signed_url(attachment_id)}

    _attachment_meta: dict[int, dict[str, Any]]

    def _signed_url(self, attachment_id: int, signature: int | None = None) -> str:
        signature = self.signatures[attachment_id] if signature is None else signature
        name = self._attachment_meta[attachment_id]["filename"]
        return f"{self.base}/cdn/{attachment_id}/{signature}/{name}"

    def add_message(
        self,
        channel_id: int,
        files: list[tuple[str, bytes, str | None]],
        *,
        author: int = OWNER_ID,
        stale_link: bool = False,
    ) -> dict[str, Any]:
        if not hasattr(self, "_attachment_meta"):
            self._attachment_meta = {}
        attachments = []
        for filename, data, content_type in files:
            attachment_id = self.snowflake()
            self.files[attachment_id] = data
            self.signatures[attachment_id] = 1
            self._attachment_meta[attachment_id] = {
                "id": str(attachment_id),
                "filename": filename,
                "size": len(data),
                "content_type": content_type,
            }
            attachment = self._attachment(attachment_id)
            if stale_link:
                self.signatures[attachment_id] = 2
            attachments.append(attachment)
        message_id = self.snowflake()
        message = {
            "id": str(message_id),
            "channel_id": str(channel_id),
            "guild_id": str(GUILD_ID),
            "author": user(author),
            "member": member(author, with_user=False),
            "content": "",
            "timestamp": TIMESTAMP,
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": attachments,
            "embeds": [],
            "pinned": False,
            "type": 0,
        }
        self.messages[channel_id].append(message)
        self.channels[channel_id]["last_message_id"] = str(message_id)
        return message

    def guild_payload(self) -> dict[str, Any]:
        return {
            "id": str(GUILD_ID),
            "name": "Test Server",
            "icon": None,
            "splash": None,
            "discovery_splash": None,
            "owner_id": str(OWNER_ID),
            "afk_channel_id": None,
            "afk_timeout": 300,
            "verification_level": 0,
            "default_message_notifications": 0,
            "explicit_content_filter": 0,
            "roles": [
                {
                    "id": str(GUILD_ID),
                    "name": "@everyone",
                    "permissions": str(VIEW_CHANNEL | SEND_MESSAGES | READ_HISTORY),
                    "position": 0,
                    "color": 0,
                    "hoist": False,
                    "managed": False,
                    "mentionable": False,
                    "flags": 0,
                },
                {
                    "id": str(CLANKERS_ROLE_ID),
                    "name": "Clankers",
                    "permissions": "0",
                    "position": 2,
                    "color": 0,
                    "hoist": False,
                    "managed": False,
                    "mentionable": False,
                    "flags": 0,
                },
                {
                    "id": str(ROLE_ID),
                    "name": "Archive Bot",
                    "permissions": str(BOT_PERMISSIONS),
                    "position": 1,
                    "color": 0,
                    "hoist": False,
                    "managed": True,
                    "mentionable": False,
                    "flags": 0,
                },
            ],
            "emojis": [],
            "stickers": [],
            "features": [],
            "mfa_level": 0,
            "system_channel_id": None,
            "system_channel_flags": 0,
            "rules_channel_id": None,
            "vanity_url_code": None,
            "description": None,
            "banner": None,
            "premium_tier": 0,
            "preferred_locale": "en-US",
            "public_updates_channel_id": None,
            "nsfw_level": 0,
            "premium_progress_bar_enabled": False,
            "joined_at": TIMESTAMP,
            "large": False,
            "unavailable": False,
            "member_count": 5,
            "voice_states": [],
            "members": [member(uid) for uid in (BOT_ID, OWNER_ID, BOB_ID, MALLORY_ID, CAROL_ID)],
            "channels": [c for c in self.channels.values() if c["type"] in (0, 2, 13, 15)],
            "threads": [dict(t) for t in self.active_threads],
            "presences": [],
            "stage_instances": [],
            "guild_scheduled_events": [],
            "soundboard_sounds": [],
        }

    # ---- gateway ---------------------------------------------------------

    async def _gateway(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self._ws = ws
        await ws.send_str(json.dumps({"op": 10, "d": {"heartbeat_interval": 41250}}))
        async for message in ws:
            if message.type != WSMsgType.TEXT:
                continue
            data = json.loads(message.data)
            if data["op"] == 1:
                await ws.send_str(json.dumps({"op": 11}))
            elif data["op"] == 2:
                self.identify = data["d"]
                await self.dispatch(
                    "READY",
                    {
                        "v": 10,
                        "user": user(BOT_ID, "archive-bot", bot=True),
                        "guilds": [{"id": str(GUILD_ID), "unavailable": True}],
                        "session_id": "session",
                        "resume_gateway_url": f"ws://127.0.0.1:{self.port}/gateway",
                        "application": {"id": str(APP_ID), "flags": self.app_flags},
                        "shard": [0, 1],
                    },
                )
                await self.dispatch("GUILD_CREATE", self.guild_payload())
                self.ready.set()
            elif data["op"] == 8:
                await self.dispatch("GUILD_MEMBERS_CHUNK", {
                    "guild_id": str(GUILD_ID), "chunk_index": 0, "chunk_count": 1,
                    "nonce": data["d"].get("nonce"),
                    "members": [member(uid) for uid in (BOT_ID, OWNER_ID, BOB_ID, MALLORY_ID, CAROL_ID)],
                })
        return ws

    async def dispatch(self, event: str, data: dict[str, Any]) -> None:
        self._sequence += 1
        assert self._ws is not None
        await self._ws.send_str(json.dumps({"op": 0, "s": self._sequence, "t": event, "d": data}))

    async def live_message(self, channel_id: int, files: list[tuple[str, bytes, str | None]], **kwargs: Any) -> dict[str, Any]:
        message = self.add_message(channel_id, files, **kwargs)
        await self.dispatch("MESSAGE_CREATE", message)
        return message

    def add_pin(
        self, channel_id: int, text: str, *, author: int = OWNER_ID,
        files: list[tuple[str, bytes, str | None]] | None = None,
    ) -> dict[str, Any]:
        message = self.add_message(channel_id, files or [], author=author)
        message["content"] = text
        message["pinned"] = True
        self.pins.setdefault(channel_id, []).append(message)
        return message

    async def pins_updated(self, channel_id: int) -> None:
        await self.dispatch(
            "CHANNEL_PINS_UPDATE",
            {"channel_id": str(channel_id), "guild_id": str(GUILD_ID), "last_pin_timestamp": TIMESTAMP},
        )

    async def delete_message(self, channel_id: int, message_id: int) -> None:
        self.messages[channel_id] = [m for m in self.messages[channel_id] if int(m["id"]) != message_id]
        await self.dispatch(
            "MESSAGE_DELETE",
            {"id": str(message_id), "channel_id": str(channel_id), "guild_id": str(GUILD_ID)},
        )

    # ---- interactions ----------------------------------------------------

    def _interaction_base(self, user_id: int, channel_id: int, *, in_dm: bool = False) -> dict[str, Any]:
        interaction_id = self.snowflake()
        base: dict[str, Any] = {
            "id": str(interaction_id),
            "application_id": str(APP_ID),
            "token": f"token-{interaction_id}",
            "version": 1,
            "channel_id": str(channel_id),
            "locale": "en-US",
            "attachment_size_limit": 10 * 1024 * 1024,
            "entitlements": [],
            "app_permissions": str(BOT_PERMISSIONS),
            "authorizing_integration_owners": {"0": str(GUILD_ID)},
            "context": 0,
        }
        if in_dm:
            base["user"] = user(user_id)
            base["channel"] = {"id": str(channel_id), "type": 1, "recipients": [user(user_id)], "last_message_id": None}
            base["context"] = 1
        else:
            base["guild_id"] = str(GUILD_ID)
            base["guild_locale"] = "en-US"
            base["member"] = member(user_id)
            base["channel"] = self.channels[channel_id]
        self.replies[str(interaction_id)] = []
        self.interaction_channels[str(interaction_id)] = channel_id
        return base

    async def slash(self, user_id: int, subcommand: str, channel_id: int, _group: str = "archive", **options: Any) -> str:
        interaction = self._interaction_base(user_id, channel_id)
        command = next(c for c in self.synced_commands if c["name"] == _group)
        spec = {o["name"]: o for sub in command["options"] if sub["name"] == subcommand for o in sub.get("options", [])}
        resolved: dict[str, Any] = {"users": {}, "members": {}}
        values = []
        for name, value in options.items():
            kind = spec[name]["type"]
            if kind == 6:
                resolved["users"][str(value)] = user(value)
                resolved["members"][str(value)] = member(value, with_user=False)
                value = str(value)
            elif kind == 7:
                channel = self.channels[value]
                resolved.setdefault("channels", {})[str(value)] = {
                    "id": str(value), "name": channel["name"], "type": channel["type"], "permissions": str(BOT_PERMISSIONS)}
                value = str(value)
            values.append({"name": name, "type": kind, "value": value})
        interaction["type"] = 2
        interaction["data"] = {
            "id": command["id"],
            "name": _group,
            "type": 1,
            "guild_id": str(GUILD_ID),
            "options": [{"name": subcommand, "type": 1, "options": values}],
            "resolved": resolved,
        }
        await self.dispatch("INTERACTION_CREATE", interaction)
        return interaction["id"]

    async def autocomplete(self, user_id: int, channel_id: int, group: str, subcommand: str, option: str, current: str) -> str:
        interaction = self._interaction_base(user_id, channel_id)
        command = next(c for c in self.synced_commands if c["name"] == group)
        interaction["type"] = 4
        interaction["data"] = {
            "id": command["id"], "name": group, "type": 1, "guild_id": str(GUILD_ID),
            "options": [{"name": subcommand, "type": 1, "options": [{"name": option, "type": 3, "value": current, "focused": True}]}],
        }
        await self.dispatch("INTERACTION_CREATE", interaction)
        return interaction["id"]

    async def submit_modal(self, user_id: int, channel_id: int, modal: dict[str, Any], values: list[str]) -> str:
        """Answer a modal the bot opened. `modal` is the callback payload's data."""
        interaction = self._interaction_base(user_id, channel_id)
        rows = []
        inputs = [c for row in modal["components"] for c in row["components"]]
        for component, value in zip(inputs, values):
            rows.append({"type": 1, "components": [{"type": 4, "custom_id": component["custom_id"], "value": value}]})
        interaction["type"] = 5
        interaction["data"] = {"custom_id": modal["custom_id"], "components": rows}
        await self.dispatch("INTERACTION_CREATE", interaction)
        return interaction["id"]

    async def pick_user(self, user_id: int, channel_id: int, components: list[dict[str, Any]], picked: int, message_id: str | None = None) -> str:
        """Choose someone in a user menu from an ephemeral reply's components."""
        interaction = self._interaction_base(user_id, channel_id)
        select = next(c for row in components for c in row["components"] if c["type"] == 5)
        interaction["type"] = 3
        interaction["message"] = {
            "id": message_id or str(self.snowflake()), "channel_id": str(channel_id), "author": user(BOT_ID, bot=True),
            "content": "", "timestamp": TIMESTAMP, "edited_timestamp": None, "tts": False, "mention_everyone": False,
            "mentions": [], "mention_roles": [], "attachments": [], "embeds": [], "components": components,
            "pinned": False, "type": 0, "flags": 64,
        }
        interaction["data"] = {
            "custom_id": select["custom_id"], "component_type": 5, "values": [str(picked)],
            "resolved": {"users": {str(picked): user(picked)}, "members": {str(picked): member(picked, with_user=False)}},
        }
        await self.dispatch("INTERACTION_CREATE", interaction)
        return interaction["id"]

    async def presence(self, user_id: int, spotify: dict[str, Any] | None) -> None:
        """Change someone's status. `spotify` has track_id, title, artist, album, start_ms, end_ms."""
        activities = []
        if spotify:
            activities.append({
                "type": 2, "name": "Spotify", "id": "spotify:1", "sync_id": spotify["track_id"],
                "session_id": "session", "party": {"id": f"spotify:{user_id}"}, "details": spotify["title"],
                "state": spotify["artist"], "flags": 48, "created_at": spotify["start_ms"],
                "assets": {"large_image": "spotify:ab67616d0000b273cover", "large_text": spotify["album"]},
                "timestamps": {"start": spotify["start_ms"], "end": spotify["end_ms"]},
            })
        await self.dispatch("PRESENCE_UPDATE", {
            "user": {"id": str(user_id)}, "guild_id": str(GUILD_ID), "status": "online",
            "activities": activities, "client_status": {"desktop": "online"},
        })

    async def click(self, user_id: int, message: dict[str, Any], custom_id: str, *, in_dm: bool = False) -> str:
        interaction = self._interaction_base(user_id, int(message["channel_id"]), in_dm=in_dm)
        interaction["type"] = 3
        interaction["message"] = message
        interaction["data"] = {"custom_id": custom_id, "component_type": 2}
        await self.dispatch("INTERACTION_CREATE", interaction)
        return interaction["id"]

    # ---- discord rest ----------------------------------------------------

    @staticmethod
    async def _body(request: web.Request) -> tuple[dict[str, Any], list[tuple[str, bytes]]]:
        if request.content_type.startswith("multipart/"):
            reader = await request.multipart()
            payload: dict[str, Any] = {}
            files: list[tuple[str, bytes]] = []
            async for part in reader:
                if part.name == "payload_json":
                    payload = json.loads(await part.text())
                else:
                    files.append((part.filename or "", bytes(await part.read())))
            return payload, files
        raw = await request.read()
        return (json.loads(raw) if raw else {}), []

    async def _my_guilds(self, request: web.Request) -> web.Response:
        return _json([{
            "id": str(GUILD_ID), "name": "Test Server", "icon": None, "owner": False,
            "permissions": "0", "features": [],
        }])

    async def _me(self, request: web.Request) -> web.Response:
        if request.headers.get("Authorization") != "Bot fake-token":
            return _json({"message": "401: Unauthorized", "code": 0}, 401)
        return _json(user(BOT_ID, "archive-bot", bot=True))

    async def _application(self, request: web.Request) -> web.Response:
        return _json(
            {
                "id": str(APP_ID),
                "name": "Archive Bot",
                "icon": None,
                "description": "",
                "rpc_origins": [],
                "bot_public": False,
                "bot_require_code_grant": False,
                "owner": user(OWNER_ID),
                "team": None,
                "verify_key": "00",
                "flags": self.app_flags,
            }
        )

    async def _sync(self, request: web.Request) -> web.Response:
        commands = await request.json()
        for command in commands:
            command["id"] = str(self.snowflake())
            command["application_id"] = str(APP_ID)
            command["guild_id"] = str(GUILD_ID)
            command["version"] = "1"
        self.synced_commands = commands
        return _json(commands)

    async def _guild(self, request: web.Request) -> web.Response:
        return _json(self.guild_payload())

    async def _guild_member(self, request: web.Request) -> web.Response:
        user_id = int(request.match_info["user"])
        return _json({**member(user_id), "roles": [str(r) for r in self.member_roles.get(user_id, [])]})

    async def _threads_active(self, request: web.Request) -> web.Response:
        return _json({"threads": self.active_threads, "members": []})

    async def _archived_public(self, request: web.Request) -> web.Response:
        threads = self.archived_public.get(int(request.match_info["channel"]), [])
        return _json({"threads": threads, "members": [], "has_more": False})

    async def _archived_private(self, request: web.Request) -> web.Response:
        threads = self.archived_private.get(int(request.match_info["channel"]), [])
        return _json({"threads": threads, "members": [], "has_more": False})

    def _can_bot_read(self, channel_id: int) -> bool:
        return not self.channels.get(channel_id, {}).get("permission_overwrites")

    def _fresh(self, message: dict[str, Any]) -> dict[str, Any]:
        return {**message, "attachments": [self._attachment(int(a["id"])) for a in message["attachments"]]}

    async def _history(self, request: web.Request) -> web.Response:
        channel_id = int(request.match_info["channel"])
        if not self._can_bot_read(channel_id):
            return _json({"message": "Missing Access", "code": 50001}, 403)
        limit = int(request.query.get("limit", 50))
        after = int(request.query.get("after", 0))
        found = sorted(
            (m for m in self.messages.get(channel_id, []) if int(m["id"]) > after), key=lambda m: int(m["id"])
        )[:limit]
        return _json([self._fresh(m) for m in reversed(found)])

    async def _pins(self, request: web.Request) -> web.Response:
        pins = self.pins.get(int(request.match_info["channel"]), [])
        return _json({"items": [{"pinned_at": TIMESTAMP, "message": self._fresh(m)} for m in pins], "has_more": False})

    async def _message(self, request: web.Request) -> web.Response:
        channel_id = int(request.match_info["channel"])
        message_id = int(request.match_info["message"])
        for message in self.messages.get(channel_id, []):
            if int(message["id"]) == message_id:
                return _json(self._fresh(message))
        return _json({"message": "Unknown Message", "code": 10008}, 404)

    async def _post_message(self, request: web.Request) -> web.Response:
        channel_id = int(request.match_info["channel"])
        payload, files = await self._body(request)
        message = {
            "id": str(self.snowflake()),
            "channel_id": str(channel_id),
            "author": user(BOT_ID, "archive-bot", bot=True),
            "content": payload.get("content", ""),
            "timestamp": TIMESTAMP,
            "edited_timestamp": None,
            "tts": False,
            "mention_everyone": False,
            "mentions": [],
            "mention_roles": [],
            "attachments": [],
            "embeds": payload.get("embeds", []),
            "components": payload.get("components", []),
            "pinned": False,
            "type": 0,
        }
        self.sent.append({"message": message, "payload": payload, "files": files, "channel_id": channel_id})
        return _json(message)

    async def _edit_message(self, request: web.Request) -> web.Response:
        payload, _ = await self._body(request)
        self.edits.append({"message_id": int(request.match_info["message"]), "payload": payload})
        return _json({
            "id": request.match_info["message"], "channel_id": request.match_info["channel"],
            "author": user(BOT_ID, "archive-bot", bot=True), "content": payload.get("content") or "",
            "timestamp": TIMESTAMP, "edited_timestamp": TIMESTAMP, "tts": False, "mention_everyone": False,
            "mentions": [], "mention_roles": [], "attachments": [], "embeds": payload.get("embeds", []),
            "components": payload.get("components", []), "pinned": False, "type": 0,
        })

    async def _meta(self, request: web.Request) -> web.Response:
        self.meta_requests += 1
        key = f"{request.match_info['type']}:{request.match_info['id']}"
        if key not in self.metas:
            return _json({"meta": None})
        return _json({"meta": self.metas[key]})

    async def _search(self, request: web.Request) -> web.Response:
        query = request.match_info["extra"].removeprefix("search=").removesuffix(".json").lower()
        kind = request.match_info["type"]
        found = [m for key, m in self.metas.items() if key.startswith(kind + ":") and query in m["name"].lower()]
        return _json({"metas": found})

    async def _spicy(self, request: web.Request) -> web.Response:
        self.spicy_auth.append(request.headers.get("Authorization", ""))
        if not request.headers.get("Authorization", "").startswith("Bearer sl_pk_"):
            return _json({"Status": 401, "Body": {"error": "missing_authorization"}}, 401)
        found = self.spicy.get(request.match_info["track"])
        return _json(found) if found else _json({"Status": 404}, 404)

    async def _stremio_login(self, request: web.Request) -> web.Response:
        body = await request.json()
        account = self.stremio_accounts.get(body.get("email", ""))
        if body.get("type") != "Login" or account is None or account[0] != body.get("password"):
            return web.json_response({"error": {"message": "Wrong passphrase", "code": 3}})
        return web.json_response({"result": {"authKey": account[1], "user": {"email": body["email"]}}})

    async def _stremio_library(self, request: web.Request) -> web.Response:
        body = await request.json()
        if body.get("collection") != "libraryItem" or body.get("authKey") not in self.stremio_library:
            return web.json_response({"error": {"message": "Session not found", "code": 1}})
        return web.json_response({"result": self.stremio_library[body["authKey"]]})

    async def _lrclib_get(self, request: web.Request) -> web.Response:
        found = self.lrclib.get(request.query.get("track_name", ""))
        return _json(found) if found else _json({"code": 404}, 404)

    async def _lrclib_search(self, request: web.Request) -> web.Response:
        found = self.lrclib.get(request.query.get("track_name", ""))
        return _json([found] if found else [])

    async def _user(self, request: web.Request) -> web.Response:
        return _json(user(int(request.match_info["user"])))

    async def _dm_channel(self, request: web.Request) -> web.Response:
        body = await request.json()
        recipient = int(body["recipient_id"])
        return _json({"id": str(recipient + 5000), "type": 1, "recipients": [user(recipient)], "last_message_id": None})

    async def _interaction_callback(self, request: web.Request) -> web.Response:
        interaction_id = request.match_info["id"]
        payload, files = await self._body(request)
        response_id = str(self.snowflake())
        self.replies[interaction_id].append({"payload": payload, "files": files, "message_id": response_id})
        return _json(
            {
                "interaction": {
                    "id": interaction_id,
                    "type": 2,
                    "response_message_id": response_id,
                    "response_message_loading": False,
                    "response_message_ephemeral": True,
                },
            }
        )

    async def _followup(self, request: web.Request) -> web.Response:
        token = request.match_info["token"]
        interaction_id = token.removeprefix("token-")
        payload, files = await self._body(request)
        message_id = str(self.snowflake())
        self.replies[interaction_id].append({"payload": payload, "files": files, "message_id": message_id})
        return _json(
            {
                "id": message_id,
                "channel_id": str(self.interaction_channels.get(interaction_id, 1)),
                "author": user(BOT_ID, "archive-bot", bot=True),
                "content": payload.get("content", ""),
                "timestamp": TIMESTAMP,
                "edited_timestamp": None,
                "tts": False,
                "mention_everyone": False,
                "mentions": [],
                "mention_roles": [],
                "attachments": [],
                "embeds": [],
                "pinned": False,
                "type": 0,
                "webhook_id": str(APP_ID),
            }
        )

    async def _cdn(self, request: web.Request) -> web.Response:
        attachment_id = int(request.match_info["attachment"])
        if int(request.match_info["signature"]) != self.signatures.get(attachment_id):
            raise web.HTTPNotFound()
        return web.Response(body=self.files[attachment_id], content_type="application/octet-stream")

    # ---- google ----------------------------------------------------------

    async def _drive_about(self, request: web.Request) -> web.Response:
        return _json({"user": {}}, self.drive_status)

    async def _drive_start(self, request: web.Request) -> web.Response:
        bad = self._check_bearer(request)
        if bad is not None:
            return bad
        if self.drive_status != 200:
            return _json({"error": {"message": "insufficient scope"}}, self.drive_status)
        file_id = request.match_info.get("file")
        if file_id and file_id not in self.drive_files:
            return _json({"error": {"message": "not found"}}, 404)
        session = str(len(self.drive_sessions))
        self.drive_sessions[session] = {"file": file_id, "metadata": await request.json()}
        return web.Response(headers={"Location": f"{self.base}/drive/session/{session}"})

    async def _drive_put(self, request: web.Request) -> web.Response:
        session = self.drive_sessions[request.match_info["session"]]
        file_id = session["file"] or f"file-{len(self.drive_files)}"
        entry = self.drive_files.setdefault(file_id, {"name": session["metadata"].get("name"), "writes": 0})
        entry["data"] = await request.read()
        entry["writes"] += 1
        return _json({"id": file_id})

    def _maybe_fail(self, name: str) -> web.Response | None:
        queue = self.google_failures[name]
        if not queue:
            return None
        failure = queue.pop(0)
        if isinstance(failure, int):
            return _json({"error": {"code": failure, "message": "injected"}}, status=failure)
        status, body = failure
        return _json(body, status=status)

    def _check_bearer(self, request: web.Request) -> web.Response | None:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer ") or header[7:] not in self.valid_tokens:
            return _json({"error": {"code": 401, "message": "bad token"}}, status=401)
        return None

    async def _google_token(self, request: web.Request) -> web.Response:
        self.token_requests += 1
        failure = self._maybe_fail("token")
        if failure is not None:
            return failure
        form = await request.post()
        if form.get("grant_type") == "authorization_code":
            return _json(
                {"access_token": "first", "refresh_token": "refresh-from-login", "expires_in": 3600, "token_type": "Bearer",
                 "scope": "https://www.googleapis.com/auth/photoslibrary.appendonly"}
            )
        assert form.get("grant_type") == "refresh_token"
        assert form.get("refresh_token") == "refresh-1"
        token = f"access-{self.token_requests}"
        self.valid_tokens.add(token)
        return _json({"access_token": token, "expires_in": 3600, "token_type": "Bearer"})

    async def _google_upload(self, request: web.Request) -> web.Response:
        bad = self._check_bearer(request)
        if bad is not None:
            return bad
        failure = self._maybe_fail("upload")
        if failure is not None:
            return failure
        assert request.headers["X-Goog-Upload-Protocol"] == "raw"
        assert request.headers["Content-Type"] == "application/octet-stream"
        data = await request.read()
        token = f"upload-{len(self.uploads)}"
        self.uploads[token] = {"data": data, "mime": request.headers["X-Goog-Upload-Content-Type"]}
        return web.Response(text=token)

    async def _google_album(self, request: web.Request) -> web.Response:
        bad = self._check_bearer(request)
        if bad is not None:
            return bad
        failure = self._maybe_fail("album")
        if failure is not None:
            return failure
        body = await request.json()
        album_id = f"album-{len(self.albums)}"
        self.albums[album_id] = body["album"]["title"]
        return _json({"id": album_id, "title": body["album"]["title"], "isWriteable": True})

    async def _google_batch(self, request: web.Request) -> web.Response:
        bad = self._check_bearer(request)
        if bad is not None:
            return bad
        failure = self._maybe_fail("batch")
        if failure is not None:
            return failure
        body = await request.json()
        if body["albumId"] not in self.albums:
            return _json({"error": {"code": 400, "message": "Invalid album ID"}}, status=400)
        results = []
        for new_item in body["newMediaItems"]:
            simple = new_item["simpleMediaItem"]
            upload = self.uploads[simple["uploadToken"]]
            item_id = f"item-{len(self.items)}"
            self.items.append(
                {"id": item_id, "filename": simple["fileName"], "album": body["albumId"],
                 "mime": upload["mime"], "data": upload["data"], "description": new_item.get("description")}
            )
            results.append(
                {
                    "uploadToken": simple["uploadToken"],
                    "status": {"message": "Success"},
                    "mediaItem": {"id": item_id, "productUrl": f"https://photos.google.com/lr/photo/{item_id}", "filename": simple["fileName"]},
                }
            )
        return _json({"newMediaItemResults": results})
