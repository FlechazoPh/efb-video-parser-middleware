import threading
import time
import types
from pathlib import Path

import pytest

pytest.importorskip("ehforwarderbot")

from ehforwarderbot import Message, MsgType, coordinator  # noqa: E402
from ehforwarderbot.message import LinkAttribute  # noqa: E402

import efb_video_parser_middleware as mw  # noqa: E402
from efb_video_parser_middleware import media, sender as sender_mod  # noqa: E402
from efb_video_parser_middleware.downloader import Prepared  # noqa: E402
from efb_video_parser_middleware.resolvers import VideoSource  # noqa: E402


@pytest.fixture
def middleware(monkeypatch, tmp_path):
    monkeypatch.setattr(mw, "get_config_path", lambda mid: tmp_path / "none.yaml")
    master = types.SimpleNamespace(channel_id="other.master")
    monkeypatch.setattr(coordinator, "master", master, raising=False)
    sent = []
    monkeypatch.setattr(coordinator, "send_message", lambda m: sent.append(
        (m.type, m.uid, m.text, m.target, m.file.closed if m.file else None, getattr(m, "path", None))),
        raising=False)
    monkeypatch.setattr(mw.Downloader, "cleanup_stale", lambda self, max_age=0: None)
    m = mw.VideoParserMiddleware()
    m.config["tmp_dir"] = str(tmp_path)
    return m, master, sent


def chat():
    from ehforwarderbot.chat import PrivateChat
    return PrivateChat(module_id="honus.comwechat", module_name="w", channel_emoji="w",
                       uid="wxid_a", name="A")


def test_candidates(middleware):
    m, master, _ = middleware
    link = Message(type=MsgType.Link, deliver_to=master,
                   attributes=LinkAttribute(title="t", url="https://b23.tv/AbCd123"))
    assert m.candidates(link) == ["https://b23.tv/AbCd123"]
    text = Message(type=MsgType.Text, deliver_to=master,
                   text="7.92 复制打开抖音，看看【作品】 https://v.douyin.com/ZzYyXx12345/ ）还有 https://example.com/a")
    assert m.candidates(text) == ["https://v.douyin.com/ZzYyXx12345/"]
    article = Message(type=MsgType.Link, deliver_to=master,
                      attributes=LinkAttribute(title="t", url="https://mp.weixin.qq.com/s/x"))
    assert m.candidates(article) == []


def test_message_returned_immediately_and_video_follows(middleware, tmp_path):
    m, master, sent = middleware
    part = tmp_path / "video.mp4"
    part.write_bytes(b"0" * 10)
    gate = threading.Event()

    def slow_prepare(url):
        gate.wait(5)
        return Prepared(VideoSource(webpage_url=url, title="T"), tmp_path,
                        [part], [media.VideoInfo(duration=5, size=10)])

    m._prepare = slow_prepare
    c = chat()
    original = Message(type=MsgType.Link, deliver_to=master, chat=c, author=c.other, uid="42",
                       attributes=LinkAttribute(title="t", url="https://b23.tv/x"))
    t = time.time()
    assert m.process_message(original) is original
    assert time.time() - t < 0.5 and sent == []
    gate.set()
    m.executor.shutdown(wait=True)
    assert len(sent) == 1
    mtype, uid, text, target, closed_during_send, path = sent[0]
    assert mtype == MsgType.Video and uid == "42.video" and target is original
    assert text.startswith("🎬 T") and closed_during_send is False and path == part


def test_same_link_downloaded_once_for_two_chats(middleware, tmp_path):
    m, master, sent = middleware
    part = tmp_path / "v.mp4"
    part.write_bytes(b"0")
    calls = []

    def prepare(url):
        calls.append(url)
        time.sleep(0.3)
        return Prepared(VideoSource(webpage_url=url), tmp_path, [part], [media.VideoInfo(1, size=1)])

    m._prepare = prepare
    for uid in ("1", "2"):
        c = chat()
        m.process_message(Message(type=MsgType.Text, deliver_to=master, chat=c, author=c.other, uid=uid,
                                  text="https://b23.tv/same"))
    m.executor.shutdown(wait=True)
    assert calls == ["https://b23.tv/same"] and len(sent) == 2


def test_failures_are_quiet_by_default_and_notify_when_enabled(middleware):
    m, master, sent = middleware
    from efb_video_parser_middleware.resolvers import ResolveError
    m._prepare = lambda url: (_ for _ in ()).throw(ResolveError("login required"))
    c = chat()
    msg = Message(type=MsgType.Text, deliver_to=master, chat=c, author=c.other, uid="9", text="https://b23.tv/a")
    m._job(msg, "https://b23.tv/a")
    assert sent == []
    m.config["notify_failure"] = True
    m._job(msg, "https://b23.tv/b")
    assert sent and sent[0][0] == MsgType.Text and "login required" in sent[0][2]


def test_album_via_etm(monkeypatch, tmp_path):
    parts = []
    for i in range(12):
        p = tmp_path / f"part{i}.mp4"
        p.write_bytes(b"x")
        parts.append(p)
    calls = []

    class Bot:
        def send_media_group(self, chat_id, media, **kwargs):
            calls.append((chat_id, media, kwargs))
            return [types.SimpleNamespace(chat_id=chat_id, message_id=100 + len(calls))]

    logged = []
    processor = types.SimpleNamespace(
        is_silent=lambda msg: False,
        get_slave_msg_dest=lambda msg: ("A:", (-100123, 77)),
        format_text_message_template=lambda tpl, author: "<b>A:</b>",
        process_file_obj=lambda f, path: Path(path).absolute().as_uri())
    etm = types.SimpleNamespace(
        channel_id="blueset.telegram", slave_messages=processor,
        bot_manager=types.SimpleNamespace(updater=types.SimpleNamespace(bot=Bot())),
        db=types.SimpleNamespace(get_msg_log=lambda **kw: types.SimpleNamespace(master_msg_id="-100123.55"),
                                 add_or_update_message_log=lambda m, t: logged.append(t)),
        chat_manager=None)
    monkeypatch.setattr(coordinator, "master", etm, raising=False)
    monkeypatch.setattr(sender_mod.Sender, "_etm_log", staticmethod(lambda etm_, proto, tg: logged.append(tg)))
    info = media.VideoInfo(duration=10, width=640, height=360, size=1)
    prepared = Prepared(VideoSource(webpage_url="u", title="长视频 <b>"), tmp_path, parts, [info] * 12)
    c = chat()
    original = Message(type=MsgType.Link, chat=c, author=c.other, uid="55", deliver_to=etm,
                       attributes=LinkAttribute(title="t", url="u"))
    sender_mod.Sender().send(original, prepared)
    assert [len(c_[1]) for c_ in calls] == [10, 2]
    first = calls[0][1][0]
    assert first.caption.startswith("<b>A:</b>\n🎬 长视频 &lt;b&gt;") and first.supports_streaming
    assert first.media.startswith("file://") and first.width == 640
    assert calls[0][2]["reply_to_message_id"] == 55 and "reply_to_message_id" not in calls[1][2]
    assert calls[0][2]["api_kwargs"] == {"message_thread_id": 77}
    assert calls[1][1][0].caption == "11/12" and len(logged) == 1
