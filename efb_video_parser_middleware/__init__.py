"""EFB middleware: turn video links into Telegram video messages."""

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import requests

from ehforwarderbot import Message, MsgType, Middleware, coordinator
from ehforwarderbot.message import LinkAttribute
from ehforwarderbot.utils import get_config_path

from . import media
from .config import load_config
from .downloader import Downloader, Prepared, TooLarge, TooLong
from .resolvers import ResolveError, build_resolvers
from .sender import Sender, etm_uses_local_api

__version__ = "1.0.0"
__all__ = ["VideoParserMiddleware", "__version__"]

URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、；：（）【】《》「」]+", re.IGNORECASE)


class VideoParserMiddleware(Middleware):
    """Download videos behind shared links and send them as video messages.

    The original message is delivered untouched; the video follows as a reply
    once downloaded. Videos above the upload limit are split losslessly and
    sent as a single Telegram album.
    """

    middleware_id = "flechazo.video_parser"
    middleware_name = "Video Parser Middleware"
    __version__ = __version__

    def __init__(self, instance_id: Optional[str] = None):
        super().__init__(instance_id)
        self.logger = logging.getLogger(f"plugins.{self.middleware_id}")
        self.config = load_config(get_config_path(self.middleware_id))
        self.session = requests.Session()
        self.resolvers = build_resolvers(self.config, self.session)
        self.downloader = Downloader(self.config, self.session, Path(self.config["tmp_dir"]))
        self.downloader.cleanup_stale()
        self.sender = Sender(reply_to_link=self.config.get("reply_to_link", True),
                             timeout=float(self.config.get("send_timeout", 3600)))
        self.executor = ThreadPoolExecutor(max_workers=int(self.config.get("workers", 2)),
                                           thread_name_prefix="video-parser")
        self._prepared: Dict[str, Tuple[Future, float]] = {}
        self._lock = threading.Lock()
        if not media.available():
            self.logger.warning("ffmpeg/ffprobe not found: videos cannot be remuxed or split")

    # ------------------------------------------------------------------
    def candidates(self, message: Message) -> List[str]:
        urls: List[str] = []
        if message.type == MsgType.Link and isinstance(message.attributes, LinkAttribute):
            if self.config.get("process_links", True) and message.attributes.url:
                urls.append(message.attributes.url)
        elif message.type == MsgType.Text and message.text and self.config.get("process_text", True):
            urls = [u.rstrip(".,;:!?)]}'\"…") for u in URL_RE.findall(message.text)]
        found = []
        for url in urls:
            if url not in found and any(r.matches(url) for r in self.resolvers):
                found.append(url)
        return found[: int(self.config.get("max_videos_per_message", 2))]

    def process_message(self, message: Message) -> Optional[Message]:
        try:
            if message.deliver_to != coordinator.master or getattr(message, "edit", False):
                return message
            for url in self.candidates(message):
                self.executor.submit(self._job, message, url)
        except Exception:
            self.logger.exception("Failed to queue video links of %s", getattr(message, "uid", "?"))
        return message

    # ------------------------------------------------------------------
    def part_bytes(self) -> int:
        configured = float(self.config.get("part_size_mb") or 0)
        if configured <= 0:
            configured = 1950 if etm_uses_local_api() else 49
        return int(configured * 1024 * 1024)

    def _job(self, message: Message, url: str) -> None:
        started = time.time()
        try:
            prepared = self._prepare_shared(url)
            if prepared is None:
                return
            self.sender.send(message, prepared)
            self.logger.info("Delivered video for %s in %.1fs (%d part(s))", url, time.time() - started,
                             len(prepared.parts))
        except (TooLarge, TooLong) as exc:
            self.logger.info("Skip video %s: %s", url, exc)
            self._notify(message, f"⚠️ 视频过大或过长，未转发：{exc}")
        except (ResolveError, media.MediaError, requests.RequestException) as exc:
            self.logger.warning("Video %s failed: %s", url, exc)
            self._notify(message, f"⚠️ 视频解析失败：{exc}")
        except Exception:
            self.logger.exception("Video job crashed for %s", url)
        finally:
            self._expire()

    def _notify(self, message: Message, text: str) -> None:
        if self.config.get("notify_failure"):
            try:
                self.sender.send_text(message, text[:300])
            except Exception:
                self.logger.debug("notify failed", exc_info=True)

    def _prepare_shared(self, url: str) -> Optional[Prepared]:
        """Resolve + download once per URL; concurrent chats share the result."""
        keep = float(self.config.get("keep_minutes", 10)) * 60
        with self._lock:
            entry = self._prepared.get(url)
            owner = entry is None
            if owner:
                future: Future = Future()
                self._prepared[url] = (future, time.time() + keep)
            else:
                future = entry[0]
        if owner:
            try:
                future.set_result(self._prepare(url))
                timer = threading.Timer(keep + 1, self._expire)
                timer.daemon = True
                timer.start()
            except BaseException as exc:
                future.set_exception(exc)
                with self._lock:
                    self._prepared.pop(url, None)
        return future.result()

    def _prepare(self, url: str) -> Optional[Prepared]:
        resolver = next((r for r in self.resolvers if r.matches(url)), None)
        if resolver is None:
            return None
        source = resolver.resolve(url)
        if source is None:
            self.logger.debug("%s: no video at %s", resolver.name, url)
            return None
        max_mb = float(self.config.get("max_size_mb") or 0)
        return self.downloader.prepare(source, self.part_bytes(), int(max_mb * 1024 * 1024),
                                       float(self.config.get("max_duration") or 0))

    def _expire(self) -> None:
        now = time.time()
        with self._lock:
            stale = [u for u, (f, exp) in self._prepared.items() if exp < now and f.done()]
            entries = [self._prepared.pop(u) for u in stale]
        for future, _ in entries:
            try:
                result = future.result()
            except BaseException:
                continue
            if result is not None:
                result.cleanup()
        self.downloader.cleanup_stale(max_age=max(3600.0, float(self.config.get("keep_minutes", 10)) * 120))
