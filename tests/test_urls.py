"""URL normalization & classification tests (requirement 15: test URL normalization)."""

from __future__ import annotations

import pytest

from app.services.urls import (
    UrlError,
    channel_tab_url,
    classify,
    collection_type_for,
    extract_video_id,
    is_video_id,
    normalize_url,
)


@pytest.mark.parametrize(
    "url,expected_id",
    [
        ("https://www.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://youtu.be/dQw4w9WgXcQ?t=42", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/shorts/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/live/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://www.youtube.com/embed/dQw4w9WgXcQ", "dQw4w9WgXcQ"),
        ("https://m.youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz", "dQw4w9WgXcQ"),
        ("music.youtube.com/watch?v=dQw4w9WgXcQ", "dQw4w9WgXcQ"),
    ],
)
def test_video_urls(url, expected_id):
    parsed = normalize_url(url)
    assert parsed.kind == "video"
    assert parsed.video_id == expected_id
    assert parsed.canonical_url == f"https://www.youtube.com/watch?v={expected_id}"
    assert extract_video_id(url) == expected_id


def test_watch_with_playlist_keeps_single_video():
    parsed = normalize_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc")
    assert parsed.kind == "video"
    assert parsed.video_id == "dQw4w9WgXcQ"
    assert parsed.playlist_id == "PLabc"
    # canonical drops the list so single-video saves stay single
    assert "list=" not in parsed.canonical_url


def test_shorts_flag_and_music_flag():
    short = normalize_url("https://www.youtube.com/shorts/dQw4w9WgXcQ")
    assert short.is_short is True
    music = normalize_url("https://music.youtube.com/watch?v=dQw4w9WgXcQ")
    assert music.is_music is True


@pytest.mark.parametrize(
    "url,plist",
    [
        ("https://www.youtube.com/playlist?list=PLABCDEF", "PLABCDEF"),
        ("https://www.youtube.com/watch?list=PLONLY", "PLONLY"),
    ],
)
def test_playlist_urls(url, plist):
    parsed = normalize_url(url)
    assert parsed.kind == "playlist"
    assert parsed.playlist_id == plist
    assert parsed.canonical_url == f"https://www.youtube.com/playlist?list={plist}"


def test_channel_id_url():
    parsed = normalize_url("https://www.youtube.com/channel/UC1234567890123456789012")
    assert parsed.kind == "channel"
    assert parsed.channel_id == "UC1234567890123456789012"


def test_channel_handle_with_tab():
    parsed = normalize_url("https://www.youtube.com/@example/shorts")
    assert parsed.kind == "channel"
    assert parsed.channel_handle == "@example"
    assert parsed.channel_tab == "shorts"
    assert parsed.canonical_url == "https://www.youtube.com/@example/shorts"


def test_channel_user_and_c_forms():
    assert normalize_url("https://www.youtube.com/c/SomeName").channel_name == "SomeName"
    assert normalize_url("https://www.youtube.com/user/Legacy").channel_name == "Legacy"


def test_is_video_id():
    assert is_video_id("dQw4w9WgXcQ") is True
    assert is_video_id("too-short") is False
    assert is_video_id(None) is False


@pytest.mark.parametrize(
    "bad",
    ["", "   ", "https://example.com/watch?v=x", "https://vimeo.com/123"],
)
def test_rejects_non_youtube(bad):
    with pytest.raises(UrlError):
        normalize_url(bad)


# ----- Phase 2A: fine-grained classification -----
@pytest.mark.parametrize(
    "url,expected",
    [
        ("https://youtu.be/dQw4w9WgXcQ", "video"),
        ("https://www.youtube.com/playlist?list=PLabc", "playlist"),
        ("https://www.youtube.com/@ex", "channel"),
        ("https://www.youtube.com/@ex/videos", "channel_videos"),
        ("https://www.youtube.com/@ex/shorts", "channel_shorts"),
        ("https://www.youtube.com/@ex/streams", "channel_streams"),
        ("https://www.youtube.com/channel/UC1234567890123456789012/shorts", "channel_shorts"),
        ("https://example.com/x", "unknown"),
    ],
)
def test_classify(url, expected):
    assert classify(url) == expected


def test_channel_tab_url():
    root = normalize_url("https://www.youtube.com/@ex")
    assert channel_tab_url(root, "videos") == "https://www.youtube.com/@ex/videos"
    tab = normalize_url("https://www.youtube.com/@ex/videos")
    assert channel_tab_url(tab, "shorts") == "https://www.youtube.com/@ex/shorts"


def test_collection_type_for():
    assert collection_type_for(normalize_url("https://www.youtube.com/playlist?list=PLx")) == "playlist"
    assert collection_type_for(normalize_url("https://www.youtube.com/@ex/shorts")) == "channel_shorts"
    # bare channel root maps to its uploads (videos) tab
    assert collection_type_for(normalize_url("https://www.youtube.com/@ex")) == "channel_videos"
