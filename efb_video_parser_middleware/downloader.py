"""Download a resolved video and prepare Telegram-ready parts."""

import logging
import mimetypes
import shutil
import time
import urllib.parse
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests

from . import media
from .resolvers import VideoSource, ytdlp_base_opts

logger = logging.getLogger(__name__)


class TooLarge(RuntimeError):
    pass


class TooLong(RuntimeError):
    pass


@dataclass
class Prepared:
    source: VideoSource
    workdir: Path
    parts: List[Path]
    infos: List[media.VideoInfo]

    @property
    def total_size(self) -> int:
        return sum(i.size for i in self.infos)

    @property
    def duration(self) -> float:
        return sum(i.duration for i in self.infos)

    def cleanup(self) -> None:
        shutil.rmtree(self.workdir, ignore_errors=True)


class Downloader:
    def __init__(self, config: Dict[str, Any], session: requests.Session, tmp_root: Path):
        self.config = config
        self.session = session
        self.tmp_root = Path(tmp_root)
        self.tmp_root.mkdir(parents=True, exist_ok=True)

    def cleanup_stale(self, max_age: float = 3600) -> None:
        for path in self.tmp_root.iterdir():
            try:
                if time.time() - path.stat().st_mtime > max_age:
                    shutil.rmtree(path, ignore_errors=True) if path.is_dir() else path.unlink()
            except OSError:
                pass

    # ------------------------------------------------------------------
    def prepare(self, source: VideoSource, part_bytes: int, max_bytes: int,
                max_duration: float) -> Prepared:
        if max_duration and source.duration and source.duration > max_duration:
            raise TooLong(f"{source.duration:.0f}s > {max_duration:.0f}s")
        if max_bytes and source.size and source.size > max_bytes:
            raise TooLarge(f"{source.size / 1e6:.0f} MB > {max_bytes / 1e6:.0f} MB")
        workdir = self.tmp_root / uuid.uuid4().hex[:12]
        workdir.mkdir(parents=True)
        try:
            started = time.time()
            if source.use_ytdlp:
                path = self._download_ytdlp(source, workdir, max_bytes)
            else:
                path = self._download_http(source, workdir, max_bytes)
            downloaded = path.stat().st_size
            path = media.faststart(path, workdir / "video.mp4" if path.name != "video.mp4"
                                   else workdir / "video_fs.mp4")
            info = media.probe(path)
            if max_duration and info.duration > max_duration:
                raise TooLong(f"{info.duration:.0f}s > {max_duration:.0f}s")
            parts = media.split(path, part_bytes, info)
            infos = [info] if len(parts) == 1 else [media.probe(p) for p in parts]
            logger.info("Prepared %s: %.1f MB in %.1fs, %d part(s), %dx%d %s",
                        source.webpage_url, downloaded / 1e6, time.time() - started, len(parts),
                        info.width, info.height, info.vcodec)
            return Prepared(source, workdir, parts, infos)
        except BaseException:
            shutil.rmtree(workdir, ignore_errors=True)
            raise

    def _download_http(self, source: VideoSource, workdir: Path, max_bytes: int) -> Path:
        headers = {"User-Agent": "Mozilla/5.0"}
        headers.update(source.headers or {})
        with self.session.get(source.url, headers=headers, stream=True, timeout=(15, 120)) as r:
            r.raise_for_status()
            length = int(r.headers.get("Content-Length") or 0)
            if max_bytes and length > max_bytes:
                raise TooLarge(f"{length / 1e6:.0f} MB > {max_bytes / 1e6:.0f} MB")
            ext = Path(urllib.parse.urlsplit(r.url).path).suffix.lower()
            if not ext or len(ext) > 5:
                ext = mimetypes.guess_extension((r.headers.get("Content-Type") or "").split(";")[0]) or ".mp4"
            target = workdir / f"download{ext}"
            written = 0
            with open(target, "wb") as f:
                for chunk in r.iter_content(1024 * 1024):
                    f.write(chunk)
                    written += len(chunk)
                    if max_bytes and written > max_bytes:
                        raise TooLarge(f"> {max_bytes / 1e6:.0f} MB")
        if written == 0:
            raise media.MediaError("empty download")
        return target

    def _download_ytdlp(self, source: VideoSource, workdir: Path, max_bytes: int) -> Path:
        import yt_dlp
        from yt_dlp.utils import DownloadError

        opts = ytdlp_base_opts(source.webpage_url, self.config)
        opts.update({
            "format": self.config.get("format"),
            "outtmpl": str(workdir / "download.%(ext)s"),
            "merge_output_format": "mp4",
            "paths": {"home": str(workdir), "temp": str(workdir)},
        })
        if max_bytes:
            opts["max_filesize"] = max_bytes
        opts.update(source.ytdlp_opts or {})
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(source.webpage_url, download=True)
        except DownloadError as exc:
            raise media.MediaError(str(exc).splitlines()[0][:200]) from exc
        if info and not source.title:
            source.title = info.get("title") or ""
        files = [p for p in workdir.glob("download.*") if not p.name.endswith((".part", ".ytdl", ".json"))]
        if not files:
            raise TooLarge("yt-dlp skipped the download (over max_filesize?)")
        return max(files, key=lambda p: p.stat().st_size)
