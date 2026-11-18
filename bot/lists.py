"""Ranked lists: anyone can make lists like "Top 10 horror" and order them 1st to last.

Titles can be pasted many at once, one per line (numbering, years and IMDb links are understood).
"""
from __future__ import annotations

import asyncio
import re
import time
from typing import TYPE_CHECKING

import discord
from discord import app_commands

from .meta import Title

if TYPE_CHECKING:
    from .tracker import Tracker

COLOR = 0xF59E0B
MAX_ITEMS = 100
MAX_PASTE = 50
MEDALS = {1: "🥇", 2: "🥈", 3: "🥉"}

_NUMBERING = re.compile(r"^\s*(?:#?\d{1,3}\s*[.)\-:]\s*|[-*•·]\s+)")
_YEAR_MARKED = re.compile(r"\s*(?:[\(\[]((?:19|20)\d{2})[\)\]]|[-–—,]\s*((?:19|20)\d{2}))\s*$")
_YEAR_BARE = re.compile(r"\s((?:19|20)\d{2})\s*$")
_IMDB = re.compile(r"(tt\d{5,10})")


def parse_pasted(text: str) -> list[tuple[str, str | None]]:
    """Splits pasted text into (name, year) pairs. One per line; commas also work on a single line."""
    lines = [line for line in text.splitlines() if line.strip()]
    if len(lines) == 1 and "," in lines[0] and not _IMDB.search(lines[0]):
        lines = lines[0].split(",")
    found: list[tuple[str, str | None]] = []
    for line in lines:
        line = _NUMBERING.sub("", line).strip().strip("\"'")
        imdb = _IMDB.search(line)
        if imdb:
            found.append((imdb.group(1), None))
            continue
        year = None
        marked = _YEAR_MARKED.search(line)
        bare = _YEAR_BARE.search(line)
        if marked and marked.start() > 0:
            year = marked.group(1) or marked.group(2)
            line = line[: marked.start()].strip(" -–—,")
        elif bare:
            # "Dune 2021" or "Blade Runner 2049": keep the number in the name, use it as a hint too.
            year = bare.group(1)
        if line:
            found.append((line, year))
    return found[:MAX_PASTE]


class Lists:
    def __init__(self, tracker: "Tracker") -> None:
        self.tracker = tracker
        self.store = tracker.store
        self.meta = tracker.meta

    async def find_list(self, user_id: int, name: str):
        return await self.store.fetch_one(
            "SELECT * FROM ranked_list WHERE user_id = ? AND name = ? COLLATE NOCASE", (user_id, name.strip())
        )

    async def lists_of(self, user_id: int) -> list:
        return await self.store.fetch_all(
            "SELECT l.*, (SELECT COUNT(*) FROM ranked_item i WHERE i.list_id = l.id) AS size "
            "FROM ranked_list l WHERE user_id = ? ORDER BY created_at",
            (user_id,),
        )

    async def items(self, list_id: int) -> list:
        return await self.store.fetch_all(
            "SELECT * FROM ranked_item WHERE list_id = ? ORDER BY position", (list_id,)
        )

    async def create(self, user_id: int, name: str) -> bool:
        if await self.find_list(user_id, name):
            return False
        await self.store.execute(
            "INSERT INTO ranked_list (user_id, name, created_at) VALUES (?, ?, ?)", (user_id, name.strip(), time.time())
        )
        return True

    async def _lookup(self, name: str, year: str | None) -> Title | None:
        if _IMDB.fullmatch(name):
            return await self.meta.title("movie", name) or await self.meta.title("series", name)
        return await self.meta.best_match(name, year)

    async def add_many(self, list_id: int, text: str, at: int | None = None) -> tuple[list[Title], list[str], list[str]]:
        """Adds pasted titles in order. Returns (added, already there, not found)."""
        wanted = parse_pasted(text)
        limit = asyncio.Semaphore(4)

        async def look(name: str, year: str | None) -> Title | None:
            async with limit:
                return await self._lookup(name, year)

        found = await asyncio.gather(*(look(name, year) for name, year in wanted))
        existing = {row["title_id"] for row in await self.items(list_id)}
        added: list[Title] = []
        duplicate: list[str] = []
        missing: list[str] = []
        for (name, year), title in zip(wanted, found):
            if title is None:
                missing.append(f"{name} ({year})" if year else name)
            elif title.id in existing:
                duplicate.append(title.name)
            elif len(existing) >= MAX_ITEMS:
                missing.append(f"{name} (list is full at {MAX_ITEMS})")
            else:
                existing.add(title.id)
                added.append(title)
        if added:
            order = [(row["title_id"], row["type"]) for row in await self.items(list_id)]
            start = len(order) if at is None else max(0, min(len(order), at - 1))
            order[start:start] = [(t.id, t.type) for t in added]
            await self._reorder(list_id, order)
        return added, duplicate, missing

    async def _reorder(self, list_id: int, order: list[tuple[str, str]]) -> None:
        async with self.store.transaction() as db:
            await db.execute("DELETE FROM ranked_item WHERE list_id = ?", (list_id,))
            await db.executemany(
                "INSERT INTO ranked_item (list_id, position, title_id, type) VALUES (?, ?, ?, ?)",
                [(list_id, i + 1, tid, kind) for i, (tid, kind) in enumerate(order)],
            )

    async def move(self, list_id: int, title_id: str, position: int) -> int | None:
        order = [(row["title_id"], row["type"]) for row in await self.items(list_id)]
        index = next((i for i, (tid, _) in enumerate(order) if tid == title_id), None)
        if index is None:
            return None
        item = order.pop(index)
        position = max(1, min(len(order) + 1, position))
        order.insert(position - 1, item)
        await self._reorder(list_id, order)
        return position

    async def remove(self, list_id: int, title_id: str) -> bool:
        order = [(row["title_id"], row["type"]) for row in await self.items(list_id)]
        kept = [item for item in order if item[0] != title_id]
        if len(kept) == len(order):
            return False
        await self._reorder(list_id, kept)
        return True

    async def embed(self, owner: discord.abc.User | discord.Member, row) -> discord.Embed:
        items = await self.items(row["id"])
        titles = await asyncio.gather(*(self.meta.title(item["type"], item["title_id"]) for item in items))
        embed = discord.Embed(title=f"🏆 {row['name']}", color=COLOR)
        embed.set_author(name=f"{owner.display_name}'s list", icon_url=owner.display_avatar.url)
        lines = []
        for position, title in enumerate(titles, start=1):
            if title is None:
                continue
            mark = MEDALS.get(position, f"`{position:>2}.`")
            icon = "🎬" if title.type == "movie" else "📺"
            extra = " · ".join(x for x in (title.year, f"⭐ {title.imdb_rating}" if title.imdb_rating else "") if x)
            name = f"[{title.name}]({title.url})" if title.url else title.name
            lines.append(f"{mark} {icon} **{name}**" + (f"  *{extra}*" if extra else ""))
        text = "\n".join(lines) or "Empty. Add titles with `/list add`."
        if len(text) > 4000:
            text = text[:3990].rsplit("\n", 1)[0] + "\n…"
        embed.description = text
        first = next((t for t in titles if t), None)
        if first:
            if first.poster:
                embed.set_thumbnail(url=first.poster)
            if first.background:
                embed.set_image(url=first.background)
        embed.set_footer(text=f"{len(lines)} titles · /list move to reorder")
        return embed


def _summary(added: list[Title], duplicate: list[str], missing: list[str]) -> str:
    parts = []
    if added:
        names = ", ".join(t.name + (f" ({t.year})" if t.year else "") for t in added)
        parts.append(f"✅ Added {len(added)}: {names}")
    if duplicate:
        parts.append(f"↩️ Already on it: {', '.join(duplicate)}")
    if missing:
        parts.append(f"❓ Couldn't find: {', '.join(missing)}. Try adding the year, like `Dune (2021)`, or an IMDb link.")
    text = "\n".join(parts) or "Nothing to add."
    return text if len(text) <= 1900 else text[:1890] + "…"


class PasteModal(discord.ui.Modal):
    titles: discord.ui.TextInput = discord.ui.TextInput(
        label="Titles, one per line, best first",
        style=discord.TextStyle.paragraph,
        placeholder="Interstellar\nBreaking Bad\nDune (2021)\nhttps://www.imdb.com/title/tt0816692/",
        max_length=4000,
    )

    def __init__(self, lists: Lists, list_row, at: int | None) -> None:
        super().__init__(title=f"Add to {list_row['name']}"[:45])
        self.lists, self.list_row, self.at = lists, list_row, at

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(thinking=True)
        added, duplicate, missing = await self.lists.add_many(self.list_row["id"], str(self.titles.value), self.at)
        embed = await self.lists.embed(interaction.user, self.list_row)
        await interaction.followup.send(content=_summary(added, duplicate, missing), embed=embed)


class ListGroup(app_commands.Group):
    """Ranked lists, 1st to last."""

    def __init__(self, tracker: "Tracker") -> None:
        super().__init__(name="list", description="Ranked lists of movies and shows")
        self.lists = Lists(tracker)
        self.tracker = tracker

    async def _own(self, interaction: discord.Interaction, name: str):
        row = await self.lists.find_list(interaction.user.id, name)
        if row is None:
            await interaction.response.send_message(
                f"You don't have a list called **{name}**. Make one with `/list create`.", ephemeral=True
            )
        return row

    async def _list_complete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        target = getattr(interaction.namespace, "user", None)
        user_id = target.id if target else interaction.user.id
        rows = await self.lists.lists_of(user_id)
        return [
            app_commands.Choice(name=f"{row['name']} ({row['size']})"[:100], value=row["name"][:100])
            for row in rows if current.lower() in row["name"].lower()
        ][:25]

    async def _item_complete(self, interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
        name = getattr(interaction.namespace, "list", None)
        row = await self.lists.find_list(interaction.user.id, name or "")
        if row is None:
            return []
        choices = []
        for item in await self.lists.items(row["id"]):
            title = await self.tracker.meta.title(item["type"], item["title_id"])
            label = f"{item['position']}. {title.name if title else item['title_id']}"
            if current.lower() in label.lower():
                choices.append(app_commands.Choice(name=label[:100], value=item["title_id"]))
        return choices[:25]

    @app_commands.command(name="create", description="Make a new ranked list")
    @app_commands.describe(name="Like Top movies, Best anime, 2026 ranked")
    async def create(self, interaction: discord.Interaction, name: app_commands.Range[str, 1, 60]) -> None:
        if not await self.lists.create(interaction.user.id, name):
            await interaction.response.send_message(f"You already have a list called **{name}**.", ephemeral=True)
            return
        row = await self.lists.find_list(interaction.user.id, name)
        await interaction.response.send_modal(PasteModal(self.lists, row, None))

    @app_commands.command(name="add", description="Paste one or many titles into a list")
    @app_commands.describe(list="Which list", position="Insert at this rank (default: the end)")
    async def add(self, interaction: discord.Interaction, list: str, position: app_commands.Range[int, 1, 100] | None = None) -> None:
        row = await self._own(interaction, list)
        if row:
            await interaction.response.send_modal(PasteModal(self.lists, row, position))

    @app_commands.command(name="move", description="Move a title to a new rank")
    async def move(self, interaction: discord.Interaction, list: str, title: str, position: app_commands.Range[int, 1, 100]) -> None:
        row = await self._own(interaction, list)
        if row is None:
            return
        await interaction.response.defer(thinking=True)
        placed = await self.lists.move(row["id"], title, position)
        if placed is None:
            await interaction.followup.send("That title isn't on this list. Pick one from the suggestions.")
            return
        await interaction.followup.send(content=f"Moved to #{placed}.", embed=await self.lists.embed(interaction.user, row))

    @app_commands.command(name="remove", description="Take a title off a list")
    async def remove(self, interaction: discord.Interaction, list: str, title: str) -> None:
        row = await self._own(interaction, list)
        if row is None:
            return
        removed = await self.lists.remove(row["id"], title)
        await interaction.response.send_message("Removed." if removed else "That title isn't on this list.", ephemeral=True)

    @app_commands.command(name="show", description="Show a ranked list (yours or a friend's)")
    async def show(self, interaction: discord.Interaction, list: str, user: discord.Member | None = None) -> None:
        owner = user or interaction.user
        row = await self.lists.find_list(owner.id, list)
        if row is None:
            await interaction.response.send_message(f"{owner.display_name} has no list called **{list}**.", ephemeral=True)
            return
        await interaction.response.defer(thinking=True)
        await interaction.followup.send(embed=await self.lists.embed(owner, row))

    @app_commands.command(name="all", description="See all of someone's lists")
    async def all(self, interaction: discord.Interaction, user: discord.Member | None = None) -> None:
        owner = user or interaction.user
        rows = await self.lists.lists_of(owner.id)
        if not rows:
            await interaction.response.send_message(f"{owner.display_name} hasn't made any lists yet.", ephemeral=True)
            return
        text = "\n".join(f"🏆 **{row['name']}** · {row['size']} titles" for row in rows)
        embed = discord.Embed(title=f"{owner.display_name}'s lists", description=text[:4000], color=COLOR)
        await interaction.response.send_message(embed=embed)

    @app_commands.command(name="delete", description="Delete one of your lists")
    async def delete(self, interaction: discord.Interaction, list: str) -> None:
        row = await self._own(interaction, list)
        if row is None:
            return
        async with self.lists.store.transaction() as db:
            await db.execute("DELETE FROM ranked_item WHERE list_id = ?", (row["id"],))
            await db.execute("DELETE FROM ranked_list WHERE id = ?", (row["id"],))
        await interaction.response.send_message(f"Deleted **{row['name']}**.", ephemeral=True)

    for _cmd in (add, move, remove, show, delete):
        _cmd.autocomplete("list")(_list_complete)
    for _cmd in (move, remove):
        _cmd.autocomplete("title")(_item_complete)
    del _cmd
