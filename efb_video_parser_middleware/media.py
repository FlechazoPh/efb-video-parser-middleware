"""ffprobe / ffmpeg helpers: probe, remux for streaming, split by size, thumbnail."""

import json
import logging
import math
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)


class MediaError(RuntimeError):
    pass


@dataclass
class VideoInfo:
    duration: float = 0.0
    width: int = 0
    height: int = 0
    vcodec: str = ""
    acodec: str = ""
    size: int = 0


def _run(args: List[str], timeout: float = 3600) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        raise MediaError(f"{args[0]} is not installed") from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaError(f"{args[0]} timed out") from exc


def available() -> bool:
    return bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def probe(path: Path) -> VideoInfo:
    result = _run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
                  timeout=120)
    if result.returncode != 0:
        raise MediaError("ffprobe failed: " + result.stderr.decode(errors="ignore")[-300:])
    data = json.loads(result.stdout or b"{}")
    info = VideoInfo(size=os.path.getsize(path))
    try:
        info.duration = float((data.get("format") or {}).get("duration") or 0)
    except ValueError:
        pass
    for stream in data.get("streams") or []:
        if stream.get("codec_type") == "video" and not info.vcodec:
            if (stream.get("disposition") or {}).get("attached_pic"):
                continue
            info.vcodec = stream.get("codec_name") or ""
            info.width = int(stream.get("width") or 0)
            info.height = int(stream.get("height") or 0)
            rotation = str((stream.get("tags") or {}).get("rotate") or "")
            for side in stream.get("side_data_list") or []:
                if "rotation" in side:
                    rotation = str(side["rotation"])
            if rotation.lstrip("-") in ("90", "270"):
                info.width, info.height = info.height, info.width
            if not info.duration:
                try:
                    info.duration = float(stream.get("duration") or 0)
                except ValueError:
                    pass
        elif stream.get("codec_type") == "audio" and not info.acodec:
            info.acodec = stream.get("codec_name") or ""
    if not info.vcodec:
        raise MediaError("no video stream")
    return info


def faststart(src: Path, dst: Path) -> Path:
    """Remux into MP4 with the moov atom first so Telegram can stream it."""
    result = _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                   "-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(dst)])
    if result.returncode != 0 or not dst.exists() or dst.stat().st_size == 0:
        logger.info("Remux failed, keeping original: %s", result.stderr.decode(errors="ignore")[-200:])
        if dst.exists():
            dst.unlink()
        return src
    src.unlink()
    return dst


def split(src: Path, max_bytes: int, info: Optional[VideoInfo] = None) -> List[Path]:
    """Split losslessly (cut at key frames) into parts no larger than ``max_bytes``."""
    info = info or probe(src)
    size = src.stat().st_size
    if size <= max_bytes:
        return [src]
    if not info.duration:
        raise MediaError("cannot split a video without duration")
    parts_needed = math.ceil(size / max_bytes)
    segment = info.duration / parts_needed * 0.92
    out_dir = src.parent / (src.stem + "_parts")
    for attempt in range(5):
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir()
        pattern = out_dir / "part%03d.mp4"
        result = _run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
                       "-c", "copy", "-f", "segment", "-segment_time", f"{segment:.3f}",
                       "-reset_timestamps", "1", "-segment_format_options", "movflags=+faststart",
                       str(pattern)])
        if result.returncode != 0:
            raise MediaError("ffmpeg split failed: " + result.stderr.decode(errors="ignore")[-300:])
        parts = sorted(out_dir.glob("part*.mp4"))
        largest = max((p.stat().st_size for p in parts), default=0)
        if parts and largest <= max_bytes:
            # a tiny trailing segment is merged visually by Telegram anyway; keep it
            src.unlink()
            return parts
        # key frames are sparse: shrink the target duration and retry
        segment *= max(0.4, min(0.85, max_bytes / max(largest, 1) * 0.95))
        logger.debug("Split attempt %d: largest part %.1f MB > limit, segment=%.1fs",
                     attempt + 1, largest / 1e6, segment)
    raise MediaError("could not split video under the size limit (sparse key frames)")


def thumbnail(src: Path, dst: Path, at: float = 1.0) -> Optional[Path]:
    result = _run(["ffmpeg", "-y", "-v", "error", "-ss", f"{max(0.0, at):.2f}", "-i", str(src),
                   "-frames:v", "1", "-vf", "scale='min(320,iw)':-2", "-q:v", "5", str(dst)], timeout=120)
    if result.returncode != 0 or not dst.exists():
        return None
    if dst.stat().st_size > 200 * 1024:  # Telegram limit for thumbnails
        dst.unlink()
        return None
    return dst
