"""Ranked lists: create, paste many titles at once, reorder, remove, show to friends."""
from __future__ import annotations

import json

from conftest import Harness, until
from fakes import BOB_ID, OWNER_ID
from test_social import MOVIE, SHOW, add_titles


def lines_of(payload: dict) -> list[str]:
    return payload["embeds"][0]["description"].splitlines()


async def test_ranked_lists(harness: Harness) -> None:
    fake = harness.fake
    add_titles(fake)
    general = fake.add_channel("general")
    await harness.start()
    command = next(c for c in fake.synced_commands if c["name"] == "list")
    assert {o["name"] for o in command["options"]} == {"create", "add", "move", "remove", "show", "all", "delete"}

    # Creating a list opens the paste box right away; several titles go in at once, in order.
    made = await fake.slash(OWNER_ID, "create", general, _group="list", name="All time")
    await until(lambda: fake.replies[made], 20, "the paste box")
    modal = fake.replies[made][0]["payload"]["data"]
    pasted = await fake.submit_modal(OWNER_ID, general, modal, [
        f"1. Interstellar (2014)\n2) https://www.imdb.com/title/{SHOW}/\n3. Some Film That Doesn't Exist"
    ])
    await until(lambda: len(fake.replies[pasted]) >= 2, 30, "the added titles")
    reply = fake.replies[pasted][1]["payload"]
    assert "Added 2" in reply["content"] and "Couldn't find: Some Film That Doesn't Exist" in reply["content"]
    ranked = lines_of(reply)
    assert ranked[0].startswith("🥇") and "Interstellar" in ranked[0]
    assert ranked[1].startswith("🥈") and "Breaking Bad" in ranked[1]
    assert reply["embeds"][0]["thumbnail"]["url"].startswith("https://")

    # Same name twice is refused; pasting a duplicate says so.
    again = await fake.slash(OWNER_ID, "create", general, _group="list", name="all TIME")
    await until(lambda: harness.reply_text(again), 20, "duplicate list name")
    assert "already have a list" in harness.reply_text(again)
    opened = await fake.slash(OWNER_ID, "add", general, _group="list", list="All time")
    await until(lambda: fake.replies[opened], 20, "the paste box")
    dup = await fake.submit_modal(OWNER_ID, general, fake.replies[opened][0]["payload"]["data"], ["Interstellar"])
    await until(lambda: len(fake.replies[dup]) >= 2, 20, "the duplicate reply")
    assert "Already on it: Interstellar" in fake.replies[dup][1]["payload"]["content"]

    # Autocomplete offers the person's lists and the titles on a list.
    lists = await fake.autocomplete(OWNER_ID, general, "list", "show", "list", "")
    await until(lambda: fake.replies[lists], 20, "list suggestions")
    assert "All time (2)" in json.dumps(fake.replies[lists][0]["payload"])

    moved = await fake.slash(OWNER_ID, "move", general, _group="list", list="All time", title=SHOW, position=1)
    await until(lambda: len(fake.replies[moved]) >= 2, 20, "the move")
    ranked = lines_of(fake.replies[moved][1]["payload"])
    assert "Breaking Bad" in ranked[0] and "Interstellar" in ranked[1]

    # Friends can look at it.
    shown = await fake.slash(BOB_ID, "show", general, _group="list", list="all time", user=OWNER_ID)
    await until(lambda: len(fake.replies[shown]) >= 2, 20, "a friend viewing it")
    assert "Breaking Bad" in lines_of(fake.replies[shown][1]["payload"])[0]

    removed = await fake.slash(OWNER_ID, "remove", general, _group="list", list="All time", title=SHOW)
    await until(lambda: harness.reply_text(removed), 20, "the removal")
    every = await fake.slash(OWNER_ID, "all", general, _group="list")
    await until(lambda: fake.replies[every], 20, "all lists")
    assert "All time** · 1 titles" in json.dumps(fake.replies[every][0]["payload"], ensure_ascii=False)

    gone = await fake.slash(OWNER_ID, "delete", general, _group="list", list="All time")
    await until(lambda: harness.reply_text(gone), 20, "the delete")
    missing = await fake.slash(OWNER_ID, "show", general, _group="list", list="All time")
    await until(lambda: harness.reply_text(missing), 20, "the missing list")
    assert "no list called" in harness.reply_text(missing)
