"""Entry point: python -m bot [run|auth|check]"""
from __future__ import annotations

import asyncio
import logging
import sys

import discord

from .check import run_check
from .config import Config, ConfigError
from .discord_app import ArchiveClient
from .google_login import run_login


def _run() -> int:
    config = Config.from_env()
    client = ArchiveClient(config)
    try:
        client.run(config.discord_token, log_handler=None)
    except discord.LoginFailure:
        print("Discord rejected the bot token. Check DISCORD_TOKEN in .env.", file=sys.stderr)
        return 1
    except discord.PrivilegedIntentsRequired:
        print(
            "Turn on all three Privileged Gateway Intents (Presence, Server Members, Message Content): "
            "Developer Portal > your app > Bot.",
            file=sys.stderr,
        )
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    command = args[0] if args else "run"
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("discord.http").setLevel(logging.WARNING)
    logging.getLogger("discord.gateway").setLevel(logging.WARNING)
    try:
        if command == "run":
            return _run()
        if command == "auth":
            return run_login(Config.from_env(need_discord=False))
        if command == "check":
            return asyncio.run(run_check(Config.from_env()))
    except ConfigError as exc:
        print(exc, file=sys.stderr)
        return 2
    print("Usage: python -m bot [run|auth|check]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
