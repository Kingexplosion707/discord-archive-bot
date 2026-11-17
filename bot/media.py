"""Working out what a file is and converting video Google can't take.

Anyone in the server can post a file, so ffmpeg is restricted to local files and
a list of real video containers. Without that, a playlist renamed to .webm could
make ffmpeg read other files on the server or fetch URLs.
"""
from __future__ import annotations

import logging
import mimetypes
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger(__name__)

IMAGE = "image"
VIDEO = "video"

_IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif", ".avif", ".gif",
}
_VIDEO_EXTENSIONS = {
    ".mp4", ".mov", ".webm", ".mkv", ".m4v", ".avi", ".wmv", ".mpg", ".mpeg", ".3gp", ".3g2",
    ".ts", ".m2ts", ".mts", ".ogv", ".flv",
}

# Containers whose date can be set without re-encoding. Anything else is converted.
_KEEP_VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v"}
_GOOGLE_MAX_PHOTO_BYTES = 200 * 1024 * 1024

_SAFE_DEMUXERS = "mov,mp4,m4a,3gp,3g2,mj2,matroska,webm,avi,gif,mpegts,mpeg,asf,ogg,flv"
_INPUT_GUARD = ["-protocol_whitelist", "file", "-format_whitelist", _SAFE_DEMUXERS]

_EXTRA_MIME_TYPES = {
    ".heic": "image/heic",
    ".heif": "image/heif",
    ".avif": "image/avif",
    ".webp": "image/webp",
    ".mkv": "video/x-matroska",
    ".m4v": "video/x-m4v",
    ".3g2": "video/3gpp2",
    ".m2ts": "video/mp2t",
    ".mts": "video/mp2t",
}


class MediaError(Exception):
    """The file can't be used. Retrying won't help."""


@dataclass(frozen=True)
class UploadFile:
    path: Path
    filename: str
    mime_type: str


def extension(filename: str) -> str:
    return PurePosixPath(filename.replace("\\", "/")).suffix.lower()


def classify(filename: str, content_type: str | None = None) -> str | None:
    """Return the kind of media, or None if it isn't a photo or video."""
    suffix = extension(filename)
    if suffix in _IMAGE_EXTENSIONS:
        return IMAGE
    if suffix in _VIDEO_EXTENSIONS:
        return VIDEO
    kind = (content_type or "").split(";")[0].strip().lower()
    if kind.startswith("image/") and kind != "image/svg+xml":
        return IMAGE
    if kind.startswith("video/"):
        return VIDEO
    return None


def safe_extension(filename: str, content_type: str | None) -> str:
    """Pick the extension for a temp file without trusting the posted name."""
    suffix = extension(filename)
    if suffix in _IMAGE_EXTENSIONS | _VIDEO_EXTENSIONS:
        return suffix
    guessed = mimetypes.guess_extension((content_type or "").split(";")[0].strip())
    return guessed if guessed else ".bin"


def transcode_to_mp4(
    path: Path, out_dir: Path, when: datetime | None = None, timeout: float = 3600
) -> Path:
    target = out_dir / "converted.mp4"
    date_args = []
    if when is not None:
        stamp = when.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000000Z")
        date_args = ["-metadata", f"creation_time={stamp}"]
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-nostdin", "-v", "error", "-y", *_INPUT_GUARD, "-i", str(path),
                "-map", "0:v:0", "-map", "0:a:0?",
                "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2",
                "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart",
                *date_args,
                str(target),
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise MediaError("video conversion timed out") from exc
    if result.returncode != 0 or not target.exists():
        raise MediaError(f"could not convert video (ffmpeg exit {result.returncode})")
    return target


def mime_type_for(filename: str) -> str:
    suffix = extension(filename)
    if suffix in _EXTRA_MIME_TYPES:
        return _EXTRA_MIME_TYPES[suffix]
    guessed, _ = mimetypes.guess_type(f"file{suffix}")
    return guessed or "application/octet-stream"


def look_hash(path: Path, size: int = 16) -> tuple[int, float] | None:
    """Fingerprint how a photo looks, so re-saved or resized copies still match."""
    try:
        with Image.open(path) as image:
            if getattr(image, "is_animated", False):
                return None
            upright = ImageOps.exif_transpose(image)
            ratio = upright.width / upright.height
            small = upright.convert("L").resize((size + 1, size), Image.Resampling.LANCZOS)
    except (UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError, SyntaxError, ZeroDivisionError):
        return None
    pixels = small.tobytes()
    value = 0
    for row in range(size):
        for column in range(size):
            start = row * (size + 1) + column
            value = (value << 1) | (pixels[start] > pixels[start + 1])
    # Blank-looking images all fingerprint alike, so they only match byte for byte.
    if not 0.2 <= value.bit_count() / (size * size) <= 0.8:
        return None
    return value, ratio


def looks_alike(a: tuple[int, float], b: tuple[int, float]) -> bool:
    return (a[0] ^ b[0]).bit_count() <= 10 and abs(a[1] - b[1]) <= 0.03 * max(a[1], b[1])


def clean_author(author: str | None) -> str:
    """Collapse whitespace and cap length so the name is safe to write as a tag."""
    if not author:
        return ""
    return " ".join(str(author).split())[:200]


def stamp_metadata(
    path: Path, kind: str, when: datetime, zone: ZoneInfo, author: str | None = None
) -> None:
    """Write the post's date and who sent it into the file.

    Google Photos reads the date from here; the author (XMP/EXIF) travels with the
    file but isn't shown by Google Photos, so the caption carries it there instead.
    """
    local = when.astimezone(zone)
    taken = local.strftime("%Y:%m:%d %H:%M:%S")
    offset = local.strftime("%z")
    offset = f"{offset[:3]}:{offset[3:]}"
    is_gif = path.suffix.lower() == ".gif"
    if kind == VIDEO:
        utc = when.astimezone(timezone.utc).strftime("%Y:%m:%d %H:%M:%S")
        tags = [f"-QuickTime:CreateDate={utc}", f"-QuickTime:MediaCreateDate={utc}", f"-QuickTime:TrackCreateDate={utc}"]
    elif is_gif:
        tags = [f"-XMP:DateTimeOriginal={taken}{offset}", f"-XMP:CreateDate={taken}{offset}"]
    else:
        tags = [
            f"-EXIF:DateTimeOriginal={taken}",
            f"-EXIF:CreateDate={taken}",
            f"-EXIF:OffsetTimeOriginal={offset}",
        ]
    name = clean_author(author)
    if name:
        tags.append(f"-XMP-dc:Creator={name}")  # the one field every format accepts
        if kind == VIDEO:
            tags.append(f"-QuickTime:Artist={name}")
        elif not is_gif:
            tags.append(f"-EXIF:Artist={name}")
    try:
        result = subprocess.run(
            ["exiftool", "-q", "-q", "-overwrite_original", *tags, str(path)],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        log.warning("Couldn't run exiftool", exc_info=True)
        return
    if result.returncode != 0:
        log.info("Couldn't set metadata on a %s file", path.suffix)


def prepare_upload(
    path: Path,
    filename: str,
    kind: str,
    work_dir: Path,
    when: datetime,
    zone: ZoneInfo,
    author: str | None = None,
) -> UploadFile:
    """Return a file Google Photos accepts, carrying the date and sender."""
    clean_name = PurePosixPath(filename.replace("\\", "/")).name or "discord-media"
    suffix = extension(clean_name)
    if len(clean_name) > 200:
        clean_name = clean_name[: 200 - len(suffix)] + suffix
    if kind == VIDEO and suffix not in _KEEP_VIDEO_EXTENSIONS:
        converted = transcode_to_mp4(path, work_dir, when)
        stamp_metadata(converted, VIDEO, when, zone, author)
        stem = PurePosixPath(clean_name).stem or "discord-video"
        return UploadFile(converted, f"{stem}.mp4", "video/mp4")
    if kind == IMAGE and path.stat().st_size > _GOOGLE_MAX_PHOTO_BYTES:
        raise MediaError("photo is over Google's 200 MB limit")
    stamp_metadata(path, kind, when, zone, author)
    return UploadFile(path, clean_name, mime_type_for(clean_name))
