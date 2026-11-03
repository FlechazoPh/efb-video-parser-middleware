"""Configuration loading with defaults and ``${ENV}`` expansion."""

import copy
import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

import yaml

DEFAULTS: Dict[str, Any] = {
    "process_links": True,          # WeChat share cards (MsgType.Link)
    "process_text": True,           # video URLs inside text messages
    "max_videos_per_message": 2,
    "resolvers": ["bilibili", "douyin", "ytdlp", "direct"],
    # Sites handed to yt-dlp. Pages without a video (image notes...) are ignored.
    "ytdlp_domains": [
        "youtube.com", "youtu.be", "x.com", "twitter.com", "xiaohongshu.com", "xhslink.com",
        "weibo.com", "weibo.cn", "ixigua.com", "acfun.cn", "vimeo.com", "tiktok.com",
        "instagram.com", "facebook.com", "fb.watch", "reddit.com", "twitch.tv", "nicovideo.jp",
        "dailymotion.com",
    ],
    "proxy": "",                    # e.g. http://proxy:7890
    "proxy_domains": [
        "youtube.com", "youtu.be", "googlevideo.com", "x.com", "twitter.com", "twimg.com",
        "instagram.com", "facebook.com", "fb.watch", "tiktok.com", "vimeo.com", "reddit.com",
        "twitch.tv", "nicovideo.jp", "dailymotion.com",
    ],
    "douyin_api": "",               # base URL of a Douyin_TikTok_Download_API instance
    "cookies_file": "",             # Netscape cookies.txt for yt-dlp (e.g. Douyin)
    "format": ("bv*[vcodec^=avc1][height<=1080]+ba[acodec^=mp4a]/b[vcodec^=avc1][height<=1080]/"
               "bv*[height<=1080]+ba/b[height<=1080]/b"),
    "bilibili_qn": 80,              # 80=1080P (falls back to the best allowed for guests)
    "max_duration": 10800,          # seconds, 0 = unlimited
    "max_size_mb": 2000,            # download limit
    "part_size_mb": 0,              # 0 = auto: 1950 with a local Bot API server, else 49
    "reply_to_link": True,
    "notify_failure": False,        # send a short error reply when a video cannot be fetched
    "workers": 2,
    "tmp_dir": "/tmp/efb_video_parser",
    "keep_minutes": 10,             # reuse a download for the same link in other chats
    "send_timeout": 3600,
}

_ENV = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [_expand(v) for v in value]
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    return value


def load_config(path: Optional[Path]) -> Dict[str, Any]:
    data: Dict[str, Any] = {}
    if path and Path(path).exists():
        with open(path, "r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    config = copy.deepcopy(DEFAULTS)
    extra_ytdlp = data.pop("extra_ytdlp_domains", None) or []
    config.update(data)
    config["ytdlp_domains"] = list(config.get("ytdlp_domains") or []) + list(extra_ytdlp)
    return _expand(config)
