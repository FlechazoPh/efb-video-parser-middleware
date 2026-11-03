"""Find the downloadable video behind a share link.

A resolver returns a :class:`VideoSource` describing how to download the
video, or ``None`` when the link is not a video (image note, article, ...).
"""

import logging
import re
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

DESKTOP_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")
MOBILE_UA = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 "
             "(KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
VIDEO_EXTENSIONS = (".mp4", ".m4v", ".mov", ".webm", ".mkv", ".flv", ".ts", ".3gp")
SHORT_LINK_HOSTS = ("b23.tv", "xhslink.com", "v.douyin.com", "t.cn", "v.kuaishou.com", "url.cn")


class ResolveError(RuntimeError):
    """The link is a video but cannot be resolved (login required, removed...)."""


@dataclass
class VideoSource:
    webpage_url: str
    title: str = ""
    duration: float = 0.0
    url: Optional[str] = None              # direct media URL
    headers: Dict[str, str] = field(default_factory=dict)
    size: int = 0                          # bytes, 0 if unknown
    use_ytdlp: bool = False                # let yt-dlp download webpage_url
    ytdlp_opts: Dict[str, Any] = field(default_factory=dict)
    uploader: str = ""
    site: str = ""


def host_of(url: str) -> str:
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def host_in(host: str, domains) -> bool:
    return any(host == d or host.endswith("." + d) for d in domains)


def expand_short_link(session: requests.Session, url: str, timeout: float = 10) -> str:
    """Follow redirects of share short links without downloading the page."""
    if not host_in(host_of(url), SHORT_LINK_HOSTS):
        return url
    current = url
    for _ in range(5):
        r = session.get(current, allow_redirects=False, timeout=timeout,
                        headers={"User-Agent": MOBILE_UA if "douyin" in current else DESKTOP_UA})
        r.close()
        location = r.headers.get("Location")
        if r.status_code not in (301, 302, 303, 307, 308) or not location:
            break
        current = urllib.parse.urljoin(current, location)
        if not host_in(host_of(current), SHORT_LINK_HOSTS):
            break
    return current


class Resolver:
    name = "base"
    domains: List[str] = []

    def __init__(self, config: Dict[str, Any], session: requests.Session):
        self.config = config
        self.session = session

    def matches(self, url: str) -> bool:
        return host_in(host_of(url), self.domains)

    def resolve(self, url: str) -> Optional[VideoSource]:
        raise NotImplementedError


# ------------------------------------------------------------ Bilibili ---
class BilibiliResolver(Resolver):
    """Bilibili via the web API with guest cookies (yt-dlp gets HTTP 412)."""

    name = "bilibili"
    domains = ["bilibili.com", "b23.tv"]
    _VIDEO = re.compile(r"/video/(BV[0-9A-Za-z]{10}|av\d+)", re.I)
    _cookie_lock = threading.Lock()
    _cookie_time = 0.0

    def matches(self, url: str) -> bool:
        host = host_of(url)
        if host == "b23.tv" or host.endswith(".b23.tv"):
            return True
        return host_in(host, ["bilibili.com"]) and bool(self._VIDEO.search(urllib.parse.urlsplit(url).path))

    def _api_session(self) -> requests.Session:
        with self._cookie_lock:
            if time.time() - BilibiliResolver._cookie_time > 3600 or "buvid3" not in self.session.cookies:
                try:
                    spi = self.session.get("https://api.bilibili.com/x/frontend/finger/spi",
                                           headers=self._headers(), timeout=10).json()["data"]
                    self.session.cookies.set("buvid3", spi["b_3"], domain=".bilibili.com")
                    self.session.cookies.set("buvid4", spi["b_4"], domain=".bilibili.com")
                    BilibiliResolver._cookie_time = time.time()
                except Exception:
                    logger.debug("bilibili guest cookie request failed", exc_info=True)
        return self.session

    @staticmethod
    def _headers() -> Dict[str, str]:
        return {"User-Agent": DESKTOP_UA, "Referer": "https://www.bilibili.com/"}

    def resolve(self, url: str) -> Optional[VideoSource]:
        url = expand_short_link(self.session, url)
        parts = urllib.parse.urlsplit(url)
        m = self._VIDEO.search(parts.path)
        if not m:
            return None  # b23.tv also shortens live rooms, articles, spaces ...
        vid = m.group(1)
        params = {"bvid": vid} if vid.upper().startswith("BV") else {"aid": vid[2:]}
        session = self._api_session()
        view = session.get("https://api.bilibili.com/x/web-interface/view", params=params,
                           headers=self._headers(), timeout=15).json()
        if view.get("code") != 0:
            raise ResolveError(f"bilibili view: {view.get('code')} {view.get('message')}")
        data = view["data"]
        pages = data.get("pages") or [{"cid": data.get("cid"), "duration": data.get("duration"), "part": ""}]
        query = dict(urllib.parse.parse_qsl(parts.query))
        try:
            index = max(1, int(query.get("p", "1"))) - 1
        except ValueError:
            index = 0
        page = pages[min(index, len(pages) - 1)]
        play = session.get("https://api.bilibili.com/x/player/playurl", params={
            **params, "cid": page["cid"], "qn": int(self.config.get("bilibili_qn", 80)),
            "fnval": 1, "fourk": 0, "platform": "html5", "high_quality": 1,
        }, headers=self._headers(), timeout=15).json()
        if play.get("code") != 0:
            raise ResolveError(f"bilibili playurl: {play.get('code')} {play.get('message')}")
        durl = (play.get("data") or {}).get("durl") or []
        if not durl:
            raise ResolveError("bilibili playurl returned no mp4")
        title = data.get("title") or vid
        if len(pages) > 1 and page.get("part"):
            title = f"{title} - P{index + 1} {page['part']}"
        return VideoSource(webpage_url=f"https://www.bilibili.com/video/{data.get('bvid', vid)}",
                           title=title, duration=float(page.get("duration") or 0), url=durl[0]["url"],
                           headers=self._headers(), size=int(durl[0].get("size") or 0),
                           uploader=(data.get("owner") or {}).get("name", ""), site="哔哩哔哩")


# -------------------------------------------------------------- Douyin ---
class DouyinResolver(Resolver):
    """Douyin needs either a Douyin_TikTok_Download_API instance or cookies."""

    name = "douyin"
    domains = ["douyin.com", "iesdouyin.com"]
    _ID = re.compile(r"/(?:video|note|share/video|share/note)/(\d{8,})")
    _warned = False

    def resolve(self, url: str) -> Optional[VideoSource]:
        url = expand_short_link(self.session, url)
        api = (self.config.get("douyin_api") or "").rstrip("/")
        if api:
            r = self.session.get(f"{api}/api/hybrid/video_data", params={"url": url, "minimal": "true"},
                                 timeout=30)
            if r.status_code >= 400:
                raise ResolveError(f"douyin api HTTP {r.status_code}")
            data = (r.json() or {}).get("data") or {}
            if data.get("type") not in (None, "video"):
                return None  # image post
            video = data.get("video_data") or {}
            media = video.get("nwm_video_url_HQ") or video.get("nwm_video_url") or video.get("wm_video_url_HQ")
            if not media:
                raise ResolveError("douyin api returned no video url")
            return VideoSource(webpage_url=url, title=data.get("desc") or "抖音视频", url=media,
                               headers={"User-Agent": MOBILE_UA}, site="抖音",
                               uploader=((data.get("author") or {}).get("nickname") or ""))
        if self.config.get("cookies_file"):
            m = self._ID.search(urllib.parse.urlsplit(url).path)
            page = f"https://www.douyin.com/video/{m.group(1)}" if m else url
            return VideoSource(webpage_url=page, use_ytdlp=True, site="抖音")
        if not DouyinResolver._warned:
            logger.warning("Douyin links need `douyin_api` or `cookies_file`; skipping them")
            DouyinResolver._warned = True
        return None


# -------------------------------------------------------------- yt-dlp ---
class YtDlpResolver(Resolver):
    name = "yt-dlp"

    def __init__(self, config, session):
        super().__init__(config, session)
        self.domains = list(config.get("ytdlp_domains") or [])

    def resolve(self, url: str) -> Optional[VideoSource]:
        url = expand_short_link(self.session, url)
        info = extract_info(url, self.config)
        if info is None:
            return None
        return VideoSource(webpage_url=info.get("webpage_url") or url, title=info.get("title") or "",
                           duration=float(info.get("duration") or 0), use_ytdlp=True,
                           size=int(info.get("filesize") or info.get("filesize_approx") or 0),
                           uploader=info.get("uploader") or "", site=info.get("extractor_key") or "")


def ytdlp_base_opts(url: str, config: Dict[str, Any]) -> Dict[str, Any]:
    opts: Dict[str, Any] = {
        "quiet": True, "no_warnings": True, "noplaylist": True, "socket_timeout": 30,
        "retries": 3, "fragment_retries": 3, "concurrent_fragment_downloads": 4,
        "logger": logging.getLogger("yt_dlp"),
    }
    proxy = config.get("proxy") or ""
    if proxy and host_in(host_of(url), config.get("proxy_domains") or []):
        opts["proxy"] = proxy
    if config.get("cookies_file"):
        opts["cookiefile"] = config["cookies_file"]
    return opts


def extract_info(url: str, config: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    import yt_dlp  # optional heavy dependency, imported lazily
    from yt_dlp.utils import DownloadError

    opts = dict(ytdlp_base_opts(url, config), skip_download=True)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except DownloadError as exc:
        text = str(exc)
        if "No video formats" in text or "Unsupported URL" in text or "no video" in text.lower():
            return None
        raise ResolveError(text.splitlines()[0][:200]) from exc
    if not info:
        return None
    if info.get("_type") == "playlist":
        entries = [e for e in info.get("entries") or [] if e]
        if not entries:
            return None
        info = entries[0]
    formats = info.get("formats") or ([info] if info.get("url") else [])
    if not any(f.get("vcodec") not in (None, "none") for f in formats) and not info.get("vcodec"):
        return None
    if info.get("is_live"):
        return None
    return info


# ------------------------------------------------------------ ParseHub ---
class ParseHubResolver(Resolver):
    """Resolve videos through a self-hosted ParseHub HTTP service.

    Covers Douyin / Kuaishou / Weibo / Xiaohongshu videos (and 20+ more) that
    need per-platform cookies the service holds. Image posts are left to the
    Telegraph middleware, so only ``type == video`` is turned into a video.
    """

    name = "parsehub"
    # Only platforms worth routing here; Bilibili keeps its own resolver, and
    # generic sites stay with yt-dlp. Driven by config so it is easy to extend.
    DEFAULT_PLATFORM_HOSTS = {
        "douyin.com", "iesdouyin.com", "v.douyin.com", "kuaishou.com", "v.kuaishou.com",
        "xiaohongshu.com", "xhslink.com", "weibo.com", "weibo.cn", "pipix.com",
        "xiaoheihe.cn", "ixigua.com",
    }

    def __init__(self, config, session):
        super().__init__(config, session)
        self.base = (config.get("parsehub_api") or "").rstrip("/")
        self.timeout = float(config.get("parsehub_timeout", 60))
        self.hosts = set(config.get("parsehub_hosts") or self.DEFAULT_PLATFORM_HOSTS)
        self._warned = False

    def matches(self, url: str) -> bool:
        return bool(self.base) and host_in(host_of(url), self.hosts)

    def resolve(self, url: str) -> Optional[VideoSource]:
        if not self.base:
            if not self._warned:
                logger.warning("parsehub resolver enabled but parsehub_api not set; skipping")
                self._warned = True
            return None
        try:
            r = self.session.get(f"{self.base}/parse", params={"url": url}, timeout=self.timeout)
        except requests.RequestException as exc:
            raise ResolveError(f"parsehub service unreachable: {exc}") from exc
        if r.status_code == 422:
            return None  # service says unsupported platform
        if r.status_code >= 400:
            detail = ""
            try:
                detail = (r.json() or {}).get("detail", "")
            except ValueError:
                detail = r.text[:120]
            raise ResolveError(f"parsehub: {detail or r.status_code}")
        data = r.json() or {}
        if data.get("type") != "video":
            return None  # image / richtext / music -> handled elsewhere
        media = data.get("media")
        if isinstance(media, list):
            media = media[0] if media else None
        if not isinstance(media, dict) or not media.get("url"):
            raise ResolveError("parsehub returned no video url")
        headers = {"User-Agent": DESKTOP_UA}
        if host_in(host_of(media["url"]), {"xhscdn.com", "sinaimg.cn", "qpic.cn"}):
            headers["Referer"] = f"https://{host_of(url)}/"
        return VideoSource(
            webpage_url=data.get("raw_url") or url,
            title=(data.get("title") or data.get("content") or "").strip(),
            # ParseHub documents duration in seconds but some platforms (e.g.
            # Douyin) report milliseconds, so it is unreliable; leave it to the
            # ffprobe pass after download (max_duration is re-checked there).
            duration=0, url=media["url"], headers=headers,
            site=data.get("platform") or host_of(url), uploader="")


# -------------------------------------------------------------- direct ---
class DirectResolver(Resolver):
    name = "direct"

    def matches(self, url: str) -> bool:
        return urllib.parse.urlsplit(url).path.lower().endswith(VIDEO_EXTENSIONS)

    def resolve(self, url: str) -> Optional[VideoSource]:
        r = self.session.get(url, stream=True, timeout=15, headers={"User-Agent": DESKTOP_UA})
        r.close()
        ctype = (r.headers.get("Content-Type") or "").lower()
        if r.status_code >= 400 or not (ctype.startswith("video/") or "octet-stream" in ctype):
            return None
        name = urllib.parse.unquote(urllib.parse.urlsplit(url).path.rsplit("/", 1)[-1])
        return VideoSource(webpage_url=url, title=name, url=r.url, headers={"User-Agent": DESKTOP_UA},
                           size=int(r.headers.get("Content-Length") or 0), site=host_of(url))


def build_resolvers(config: Dict[str, Any], session: requests.Session) -> List[Resolver]:
    enabled = config.get("resolvers") or ["bilibili", "parsehub", "douyin", "ytdlp", "direct"]
    table = {"bilibili": BilibiliResolver, "parsehub": ParseHubResolver,
             "douyin": DouyinResolver, "ytdlp": YtDlpResolver, "direct": DirectResolver}
    return [table[name](config, session) for name in enabled if name in table]
