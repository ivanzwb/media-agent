from app.media.strategies import (
    HttpxDirectVideoStrategy, YtDlpStrategy, _ytdlp_base)

M3U8 = "https://embed-cloudfront.wistia.com/deliveries/abc123.m3u8"
MP4 = "https://cdn.example.com/clip.mp4"


def test_m3u8_not_handled_by_direct_strategy():
    # HLS must not be httpx-direct (that would only fetch the playlist text)
    assert HttpxDirectVideoStrategy().match(M3U8) == 0.0
    # plain mp4 still goes direct
    assert HttpxDirectVideoStrategy().match(MP4) == 0.9


def test_m3u8_routes_to_ytdlp():
    s = YtDlpStrategy()
    # wistia/HLS handled by yt-dlp (platform hint), and not by direct
    assert s.match(M3U8) >= 0.4
    # direct mp4 should defer to the direct strategy
    assert s.match(MP4) == 0.0


def test_ytdlp_base_falls_back_to_module():
    # yt-dlp is installed as a module in CI; base must resolve (CLI or module)
    base = _ytdlp_base()
    assert base is not None and len(base) >= 1
