"""Turns pinned messages into images, since Google Photos can't store text."""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any, Sequence

import discord
from PIL import Image, ImageDraw, ImageFont, UnidentifiedImageError

from . import media

# Pins share the upload queue with attachments. Their "url" column holds the message.
CONTENT_TYPE = "application/x-discord-pin"

MAX_PIN_IMAGES = 4
_MAX_EMBED_BYTES = 25 * 1024 * 1024
_MAX_IMG_HEIGHT = 560
_IMG_GAP = 20

_REGULAR = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
_WIDTH = 1080
_PADDING = 56
_BACKGROUND = (49, 51, 56)
_TEXT = (219, 222, 225)
_MUTED = (148, 155, 164)
_WHITE = (242, 243, 245)

_CUSTOM_EMOJI = re.compile(r"<a?:(\w+):\d+>")
_EMOJI = re.compile("[\U0001F000-\U0001FAFF☀-➿⬀-⯿]")
_INVISIBLE = re.compile("[️‍\U0001F3FB-\U0001F3FF]")


def _readable(text: str) -> str:
    """Write emoji out as names, because the font can't draw them."""

    def name(match: re.Match[str]) -> str:
        return f":{unicodedata.name(match.group(0), 'emoji').lower().replace(' ', '_')}:"

    return _EMOJI.sub(name, _INVISIBLE.sub("", _CUSTOM_EMOJI.sub(r":\1:", text)))


def message_text(message: discord.Message) -> str:
    parts = [message.clean_content.strip()]
    for embed in message.embeds:
        parts.extend(str(value).strip() for value in (embed.title, embed.description) if value)
    return "\n\n".join(part for part in parts if part)


def row_for(message: discord.Message) -> dict[str, Any] | None:
    """Build a queue entry, or None if there's nothing to show.

    Image attachments are embedded in the rendered card; other attachments (and
    oversized images) are listed as text. The media is also saved on its own.
    """
    text = message_text(message)
    images: list[dict[str, Any]] = []
    others: list[str] = []
    for item in message.attachments:
        is_image = media.classify(item.filename, item.content_type) == media.IMAGE
        if is_image and item.size <= _MAX_EMBED_BYTES and len(images) < MAX_PIN_IMAGES:
            images.append(
                {"id": item.id, "url": item.url, "filename": item.filename,
                 "content_type": item.content_type}
            )
        else:
            others.append(item.filename)
    if not text and not images:
        return None
    attached = "".join(f"\n[Attached: {name}]" for name in others)
    channel = getattr(message.channel, "name", "channel")
    payload = {
        "author": message.author.display_name,
        "channel": channel,
        "when": message.created_at.strftime("%B %d, %Y at %H:%M UTC"),
        "text": text + attached,
        "images": images,
    }
    safe_channel = re.sub(r"[^\w-]+", "-", channel)
    return {
        "id": message.id,
        "message_id": message.id,
        "channel_id": message.channel.id,
        "filename": f"pinned-{safe_channel}-{message.id}.jpg",
        "content_type": CONTENT_TYPE,
        "size": 0,
        "url": json.dumps(payload),
        "author_name": message.author.display_name,
        "author_handle": message.author.name,
    }


def _wrap(text: str, font: ImageFont.FreeTypeFont, width: int) -> list[str]:
    lines: list[str] = []
    for paragraph in text.split("\n"):
        line = ""
        for word in paragraph.split(" "):
            candidate = f"{line} {word}" if line else word
            if font.getlength(candidate) <= width:
                line = candidate
                continue
            if line:
                lines.append(line)
            while font.getlength(word) > width:
                cut = len(word) - 1
                while cut > 1 and font.getlength(word[:cut]) > width:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            line = word
        lines.append(line)
    return lines


def _load_embedded(image_paths: Sequence[Path], inner: int) -> list[Image.Image]:
    """Open each downloaded pin image and scale it to fit the card."""
    loaded = []
    for path in image_paths:
        try:
            with Image.open(path) as opened:
                frame = opened.convert("RGB")
        except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError):
            continue
        scale = min(inner / frame.width, _MAX_IMG_HEIGHT / frame.height, 1.0)
        size = (max(1, round(frame.width * scale)), max(1, round(frame.height * scale)))
        loaded.append(frame.resize(size, Image.Resampling.LANCZOS))
    return loaded


def render(raw_payload: str, target: Path, image_paths: Sequence[Path] = ()) -> None:
    payload = json.loads(raw_payload)
    author_font = ImageFont.truetype(_BOLD, 38)
    meta_font = ImageFont.truetype(_REGULAR, 26)
    body_font = ImageFont.truetype(_REGULAR, 34)
    inner = _WIDTH - 2 * _PADDING
    body = _readable(payload["text"]).strip()
    lines = _wrap(body, body_font, inner) if body else []
    line_height = 48

    pictures = _load_embedded(image_paths, inner)
    header = _PADDING + 56 + 46 + 28
    gap_before = _IMG_GAP if (pictures and lines) else 0
    pictures_block = sum(picture.height + _IMG_GAP for picture in pictures)
    height = header + line_height * len(lines) + gap_before + pictures_block + _PADDING

    image = Image.new("RGB", (_WIDTH, height), _BACKGROUND)
    draw = ImageDraw.Draw(image)
    draw.text((_PADDING, _PADDING), _readable(payload["author"]), font=author_font, fill=_WHITE)
    meta = f"Pinned in #{_readable(payload['channel'])}  |  {payload['when']}"
    draw.text((_PADDING, _PADDING + 56), meta, font=meta_font, fill=_MUTED)
    y = header
    for line in lines:
        draw.text((_PADDING, y), line, font=body_font, fill=_TEXT)
        y += line_height
    y += gap_before
    for picture in pictures:
        image.paste(picture, (_PADDING, y))
        y += picture.height + _IMG_GAP
    image.save(target, format="JPEG", quality=92)
