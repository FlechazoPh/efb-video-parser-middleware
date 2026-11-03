# efb-video-parser-middleware

[EH Forwarder Bot](https://github.com/ehForwarderBot/ehForwarderBot) middleware that turns
**video links** into real video messages: the shared link is delivered as usual, then the video
is downloaded and sent as a reply. Videos above the upload limit are split losslessly and sent as
**one Telegram album**.

Middleware ID: `flechazo.video_parser`

## Supported links

| Site | How |
|---|---|
| Bilibili (`bilibili.com/video/...`, `b23.tv`, multi-part `?p=`) | Built-in client for the web API with guest cookies (yt-dlp gets HTTP 412 from servers), 720p/1080p MP4 |
| Xiaohongshu video notes, YouTube, X/Twitter, Weibo videos, Xigua, AcFun, TikTok, Instagram, Vimeo, Reddit ... | [yt-dlp](https://github.com/yt-dlp/yt-dlp) (H.264 preferred, ≤1080p) |
| Douyin (`v.douyin.com`, `douyin.com/video`) | Needs `douyin_api` ([Douyin_TikTok_Download_API](https://github.com/Evil0ctal/Douyin_TikTok_Download_API)) **or** `cookies_file`; Douyin has no cookie-free endpoint any more |
| Direct `.mp4/.mov/.webm/...` URLs | Plain download |

Links in WeChat share cards and in text (e.g. the Douyin "复制打开抖音" share text) are detected.
Image notes / articles behind the same domains are ignored.

## Behaviour

* Never delays the original message: work happens in a thread pool (`workers`).
* Downloads are remuxed to MP4 with `+faststart` (streamable in Telegram).
* Part size is automatic: **1950 MB** when ETM uses a local Bot API server
  (`local_tdlib_api: true`), otherwise **49 MB**. Larger videos are cut at key frames with
  `ffmpeg -c copy` (no re-encoding) and sent with `sendMediaGroup` (10 per album) to the same
  chat/topic ETM would use, replying to the link and logged in ETM's database.
* A link shared to several chats within `keep_minutes` is downloaded once.
* Limits: `max_duration`, `max_size_mb`. Failures are logged; set `notify_failure: true` to get
  a short reply instead.
* Proxy only for `proxy_domains` (YouTube, X, ...).

Requires `ffmpeg` and `ffprobe` in `PATH`. With a local Bot API server, `tmp_dir` must be
visible to it at the same path (share `/tmp`).

## Install

```bash
pip3 install git+https://github.com/FlechazoPh/efb-video-parser-middleware.git
```

```yaml
# <profile>/config.yaml — put it before flechazo.telegraph
middlewares:
- flechazo.video_parser
```

Optional `<profile>/flechazo.video_parser/config.yaml`, see
[`config.example.yaml`](config.example.yaml).

## Development

```bash
pip install -e . pytest
pytest      # media tests need ffmpeg
```

## License

MIT
