import subprocess
import threading
import time
import types
from pathlib import Path

import pytest

from efb_video_parser_middleware import media, resolvers
from efb_video_parser_middleware.config import load_config
from efb_video_parser_middleware.downloader import Downloader, Prepared, TooLarge
from efb_video_parser_middleware.resolvers import VideoSource

needs_ffmpeg = pytest.mark.skipif(not media.available(), reason="ffmpeg missing")


@pytest.fixture(scope="module")
def sample_video(tmp_path_factory):
    """20 s, noisy (hard to compress) 640x360 H.264 + AAC, ~6 MB, key frame every 2 s."""
    path = tmp_path_factory.mktemp("v") / "sample.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=640x360:rate=25",
                    "-f", "lavfi", "-i", "sine=frequency=440", "-t", "20",
                    "-vf", "noise=alls=60:allf=t", "-c:v", "libx264", "-preset", "ultrafast",
                    "-b:v", "2400k", "-g", "50", "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


# ----------------------------------------------------------------- media ---
@needs_ffmpeg
def test_probe_faststart_split_thumbnail(sample_video, tmp_path):
    src = tmp_path / "in.mkv"
    src.write_bytes(sample_video.read_bytes())
    mp4 = media.faststart(src, tmp_path / "video.mp4")
    assert mp4.suffix == ".mp4" and not src.exists()
    info = media.probe(mp4)
    assert (info.width, info.height, info.vcodec) == (640, 360, "h264") and 19 < info.duration < 21
    limit = int(info.size / 3.2)
    parts = media.split(mp4, limit, info)
    assert len(parts) >= 4 and all(p.stat().st_size <= limit for p in parts)
    total = sum(media.probe(p).duration for p in parts)
    assert abs(total - info.duration) < 1.0
    thumb = media.thumbnail(parts[0], parts[0].with_suffix(".jpg"))
    assert thumb and thumb.stat().st_size < 200 * 1024


@needs_ffmpeg
def test_split_noop_when_small(sample_video, tmp_path):
    src = tmp_path / "x.mkv"
    src.write_bytes(sample_video.read_bytes())
    assert media.split(src, 10 ** 9) == [src]


@needs_ffmpeg
def test_downloader_http_and_limits(sample_video, tmp_path, monkeypatch):
    data = sample_video.read_bytes()

    class Resp:
        url = "https://cdn.example/v.mkv"
        headers = {"Content-Length": str(len(data)), "Content-Type": "video/x-matroska"}

        def raise_for_status(self):
            pass

        def iter_content(self, n):
            for i in range(0, len(data), n):
                yield data[i:i + n]

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    session = types.SimpleNamespace(get=lambda *a, **k: Resp())
    dl = Downloader({}, session, tmp_path / "work")
    src = VideoSource(webpage_url="https://x/v", url="https://cdn.example/v.mkv", title="t")
    prepared = dl.prepare(src, part_bytes=len(data) // 3, max_bytes=10 ** 9, max_duration=0)
    assert len(prepared.parts) >= 3
    assert 19 < prepared.duration < 21
    prepared.cleanup()
    assert not prepared.workdir.exists()
    with pytest.raises(TooLarge):
        dl.prepare(src, part_bytes=10 ** 9, max_bytes=len(data) // 2, max_duration=0)
    assert list((tmp_path / "work").iterdir()) == []  # failed job left nothing behind


# ------------------------------------------------------------- resolvers ---
class FakeResp:
    def __init__(self, data=None, status=200, headers=None):
        self.data, self.status_code, self.headers = data, status, headers or {}
        self.url = (headers or {}).get("X-Url", "")

    def json(self):
        return self.data

    def close(self):
        pass


class FakeSession:
    def __init__(self, routes):
        self.routes = routes
        self.cookies = {}
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs.get("params")))
        for prefix, response in self.routes.items():
            if url.startswith(prefix):
                return response(url, kwargs) if callable(response) else response
        raise AssertionError("unexpected " + url)


def test_bilibili_short_link_multi_page():
    session = FakeSession({
        "https://b23.tv/abc": FakeResp(status=302, headers={"Location": "https://www.bilibili.com/video/BV1xx411c7mD?p=2&x=1"}),
        "https://api.bilibili.com/x/frontend/finger/spi": FakeResp({"data": {"b_3": "a", "b_4": "b"}}),
        "https://api.bilibili.com/x/web-interface/view": FakeResp({"code": 0, "data": {
            "bvid": "BV1xx411c7mD", "title": "合集", "owner": {"name": "UP"},
            "pages": [{"cid": 1, "duration": 10, "part": "一"}, {"cid": 2, "duration": 20, "part": "二"}]}}),
        "https://api.bilibili.com/x/player/playurl": FakeResp({"code": 0, "data": {"durl": [
            {"url": "https://upos/x.mp4", "size": 123}]}}),
    })
    session.cookies = types.SimpleNamespace(set=lambda *a, **k: None, __contains__=lambda s, k: False)
    r = resolvers.BilibiliResolver({}, session)
    assert r.matches("https://b23.tv/abc") and r.matches("https://m.bilibili.com/video/BV1xx411c7mD")
    assert not r.matches("https://www.bilibili.com/read/cv1")
    src = r.resolve("https://b23.tv/abc")
    assert src.title == "合集 - P2 二" and src.duration == 20 and src.url == "https://upos/x.mp4"
    assert src.headers["Referer"] == "https://www.bilibili.com/"
    play_params = [p for u, p in session.calls if "playurl" in u][0]
    assert play_params["cid"] == 2 and play_params["platform"] == "html5"


def test_bilibili_short_link_to_live_room_is_not_video():
    session = FakeSession({"https://b23.tv/x": FakeResp(status=302, headers={"Location": "https://live.bilibili.com/123"})})
    assert resolvers.BilibiliResolver({}, session).resolve("https://b23.tv/x") is None


def test_douyin_needs_api_or_cookies():
    session = FakeSession({"https://v.douyin.com/": FakeResp(status=302, headers={
        "Location": "https://www.iesdouyin.com/share/video/7000000000000000001/?x=1"})})
    assert resolvers.DouyinResolver({}, session).resolve("https://v.douyin.com/abc/") is None
    src = resolvers.DouyinResolver({"cookies_file": "/c.txt"}, session).resolve("https://v.douyin.com/abc/")
    assert src.use_ytdlp and src.webpage_url == "https://www.douyin.com/video/7000000000000000001"
    session.routes["http://api/api/hybrid"] = FakeResp({"data": {"type": "video", "desc": "跳舞",
                                                                 "video_data": {"nwm_video_url_HQ": "https://nwm"}}})
    src = resolvers.DouyinResolver({"douyin_api": "http://api/"}, session).resolve("https://v.douyin.com/abc/")
    assert src.url == "https://nwm" and src.title == "跳舞"
    session.routes["http://api/api/hybrid"] = FakeResp({"data": {"type": "image"}})
    assert resolvers.DouyinResolver({"douyin_api": "http://api"}, session).resolve("https://v.douyin.com/abc/") is None


def test_ytdlp_options_proxy_only_for_listed_domains():
    cfg = load_config(None)
    cfg["proxy"] = "http://proxy:7890"
    assert resolvers.ytdlp_base_opts("https://youtu.be/x", cfg)["proxy"] == "http://proxy:7890"
    assert "proxy" not in resolvers.ytdlp_base_opts("https://www.xiaohongshu.com/x", cfg)


def test_direct_resolver():
    session = FakeSession({"https://a.com/v.mp4": FakeResp(headers={"Content-Type": "video/mp4", "Content-Length": "9",
                                                                   "X-Url": "https://a.com/v.mp4"})})
    r = resolvers.DirectResolver({}, session)
    assert r.matches("https://a.com/v.mp4?x=1") and not r.matches("https://a.com/page")
    assert r.resolve("https://a.com/v.mp4").size == 9


# --------------------------------------------------------------- caption ---
def test_caption():
    from efb_video_parser_middleware.sender import build_caption
    info = media.VideoInfo(duration=3725, size=50 * 1048576)
    p = Prepared(VideoSource(webpage_url="u", title="标题", site="哔哩哔哩", uploader="UP"),
                 Path("/tmp"), [Path("a"), Path("b")], [info, info])
    assert build_caption(p) == "🎬 标题\n哔哩哔哩 · UP\n⏱ 2:04:10 · 100.0 MB · 共 2 段"
