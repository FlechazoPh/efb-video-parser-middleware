"""Deliver prepared videos to the master channel.

* One part: a regular EFB video message (replying to the original link), so
  every master channel and middleware handles it as usual.
* Several parts with the Telegram master (ETM): one ``sendMediaGroup`` album,
  routed to the same chat/topic ETM would use and logged in ETM's database so
  replies keep working. Other masters receive the parts one by one.
"""

import html
import logging
import time
from pathlib import Path
from typing import List, Optional

from ehforwarderbot import Message, MsgType, coordinator
from ehforwarderbot.types import MessageID

from .downloader import Prepared

logger = logging.getLogger(__name__)
ETM_ID = "blueset.telegram"


def fmt_duration(seconds: float) -> str:
    seconds = int(round(seconds or 0))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def build_caption(prepared: Prepared, limit: int = 900) -> str:
    src = prepared.source
    lines = [f"🎬 {src.title.strip()}" if src.title else "🎬"]
    meta = [x for x in (src.site, src.uploader) if x]
    stats = f"⏱ {fmt_duration(prepared.duration)} · {prepared.total_size / 1048576:.1f} MB"
    if len(prepared.parts) > 1:
        stats += f" · 共 {len(prepared.parts)} 段"
    lines.append((" · ".join(meta) + "\n" if meta else "") + stats)
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 1] + "…"


def master_is_etm() -> bool:
    return getattr(coordinator.master, "channel_id", None) == ETM_ID


def etm_uses_local_api() -> bool:
    master = coordinator.master
    try:
        return master_is_etm() and bool(master.flag("local_tdlib_api"))
    except Exception:
        return False


class Sender:
    def __init__(self, reply_to_link: bool = True, timeout: float = 3600):
        self.reply_to_link = reply_to_link
        self.timeout = timeout

    def _base_message(self, original: Message, uid: str, text: str) -> Message:
        return Message(
            chat=original.chat, author=original.author, deliver_to=coordinator.master,
            uid=MessageID(uid), text=text,
            target=original if self.reply_to_link else None,
        )

    def wait_for_original(self, original: Message, timeout: float = 20) -> None:
        """Make sure ETM has logged the link message we are going to reply to."""
        if not (self.reply_to_link and master_is_etm()):
            return
        try:
            from efb_telegram_master import utils as etm_utils
            db = coordinator.master.db
            key = etm_utils.chat_id_to_str(chat=original.chat)
        except Exception:
            return
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if db.get_msg_log(slave_msg_id=original.uid, slave_origin_uid=key):
                    return
            except Exception:
                return
            time.sleep(1)

    def send(self, original: Message, prepared: Prepared) -> None:
        self.wait_for_original(original)
        caption = build_caption(prepared)
        if len(prepared.parts) == 1:
            self._send_single(original, prepared.parts[0], caption, f"{original.uid}.video")
        elif master_is_etm():
            self._send_album(original, prepared, caption)
        else:
            for index, part in enumerate(prepared.parts):
                text = caption if index == 0 else f"{index + 1}/{len(prepared.parts)}"
                self._send_single(original, part, text, f"{original.uid}.video{index}")

    def send_text(self, original: Message, text: str) -> None:
        msg = self._base_message(original, f"{original.uid}.video_error", text)
        msg.type = MsgType.Text
        coordinator.send_message(msg)

    def _send_single(self, original: Message, path: Path, caption: str, uid: str) -> None:
        with open(path, "rb") as f:
            msg = self._base_message(original, uid, caption)
            msg.type = MsgType.Video
            msg.file = f
            msg.path = path
            msg.filename = path.name if path.name.endswith(".mp4") else "video.mp4"
            msg.mime = "video/mp4"
            coordinator.send_message(msg)

    # -------------------------------------------------------------- ETM ---
    def _send_album(self, original: Message, prepared: Prepared, caption: str) -> None:
        from telegram import InputMediaVideo
        from efb_telegram_master import utils as etm_utils

        etm = coordinator.master
        processor = etm.slave_messages
        proto = self._base_message(original, f"{original.uid}.video", caption)
        proto.type = MsgType.Video
        silent = processor.is_silent(proto)
        if silent is None:
            logger.debug("Album suppressed by ETM silent settings")
            return
        msg_template, (tg_dest, thread_id) = processor.get_slave_msg_dest(proto)
        if tg_dest is None:
            return
        prefix = processor.format_text_message_template(msg_template, proto.author)
        reply_to = self._etm_reply_target(etm, etm_utils, original, tg_dest)
        first_caption = (prefix + "\n" if prefix else "") + html.escape(caption)

        handles = []
        try:
            media: List[InputMediaVideo] = []
            total = len(prepared.parts)
            # No custom thumbnails: PTB 13 still sends the pre-Bot-API-6.6
            # field "thumb", which current Bot API servers reject. Telegram
            # generates thumbnails for streamable videos by itself.
            for index, (part, info) in enumerate(zip(prepared.parts, prepared.infos)):
                f = open(part, "rb")
                handles.append(f)
                file_input = processor.process_file_obj(f, part)
                media.append(InputMediaVideo(
                    file_input, filename=f"part{index + 1}.mp4",
                    caption=(first_caption if index == 0 else f"{index + 1}/{total}")[:1024],
                    parse_mode="HTML", width=info.width or None, height=info.height or None,
                    duration=int(info.duration) or None, supports_streaming=True))
            bot = etm.bot_manager.updater.bot
            sent = []
            for start in range(0, len(media), 10):
                batch = media[start:start + 10]
                kwargs = {"timeout": self.timeout, "disable_notification": bool(silent),
                          "allow_sending_without_reply": True}
                if reply_to and start == 0:
                    kwargs["reply_to_message_id"] = reply_to
                if thread_id:
                    kwargs["api_kwargs"] = {"message_thread_id": thread_id}
                sent += bot.send_media_group(tg_dest, batch, **kwargs)
        finally:
            for f in handles:
                f.close()
        logger.info("Sent album of %d parts to %s", len(sent), tg_dest)
        self._etm_log(etm, proto, sent[0] if sent else None)

    @staticmethod
    def _etm_reply_target(etm, etm_utils, original: Message, tg_dest) -> Optional[int]:
        try:
            log = etm.db.get_msg_log(slave_msg_id=original.uid,
                                     slave_origin_uid=etm_utils.chat_id_to_str(chat=original.chat))
            if log:
                chat_id, message_id = etm_utils.message_id_str_to_id(log.master_msg_id)
                if int(chat_id) == int(tg_dest):
                    return int(message_id)
        except Exception:
            logger.debug("Cannot resolve reply target", exc_info=True)
        return None

    @staticmethod
    def _etm_log(etm, proto: Message, tg_msg) -> None:
        if tg_msg is None:
            return
        try:
            from efb_telegram_master.message import ETMMsg
            from efb_telegram_master.msg_type import TGMsgType
            etm_msg = ETMMsg.from_efbmsg(proto, etm.chat_manager)
            etm_msg.type_telegram = TGMsgType.Video
            etm_msg.put_telegram_file(tg_msg)
            etm.db.add_or_update_message_log(etm_msg, tg_msg)
        except Exception:
            logger.debug("Could not log album in ETM database", exc_info=True)
