"""Profile + yt-dlp argument-building tests (requirement 15: test profile selection)."""

from __future__ import annotations

from app.services.profiles import (
    BUILTIN_PROFILES,
    BuildContext,
    build_ytdlp_args,
    get_profile_spec,
    resolve_sub_langs,
    seed_builtin_profiles,
)
from app.services.ytdlp import redact_args


def _sub_langs_value(args):
    """Return the value passed to --sub-langs, or None if absent."""
    if "--sub-langs" not in args:
        return None
    return args[args.index("--sub-langs") + 1]


def _args(profile_name, **ctx_kwargs):
    ctx = BuildContext(output_template="/archive/out/%(id)s.%(ext)s", **ctx_kwargs)
    return build_ytdlp_args(BUILTIN_PROFILES[profile_name], ctx)


def _pair_present(args, flag, value):
    """True if `flag value` appears consecutively in args."""
    for i, a in enumerate(args):
        if a == flag and i + 1 < len(args) and args[i + 1] == value:
            return True
    return False


def test_builtin_profiles_exist():
    expected = {
        "video_best_archive",
        "video_best_archive_all_subs",
        "video_compressed_1080p",
        "video_proxy_1080p_mp4",
        "audio_flac_best",
        "audio_opus_save_space",
        "metadata_only",
        "comments_refresh_only",
    }
    assert expected == set(BUILTIN_PROFILES)


def test_best_archive_args():
    args = _args("video_best_archive", download_archive="/archive/youtube/archive/history.txt")
    assert _pair_present(args, "-f", "bestvideo+bestaudio/best")
    assert _pair_present(args, "--merge-output-format", "mkv")
    assert "--write-comments" in args
    assert "--write-info-json" in args
    assert "--embed-thumbnail" in args
    assert "--sponsorblock-mark" in args
    assert _pair_present(args, "-o", "/archive/out/%(id)s.%(ext)s")
    assert _pair_present(args, "--download-archive", "/archive/youtube/archive/history.txt")
    assert "--ignore-config" in args


def test_no_archive_yields_no_download_archive():
    args = _args("video_best_archive", download_archive=None)
    assert "--no-download-archive" in args
    assert "--download-archive" not in args


def test_no_playlist_flag():
    args = _args("video_compressed_1080p", no_playlist=True)
    assert "--no-playlist" in args
    assert _pair_present(args, "--merge-output-format", "mp4")


def test_audio_flac_profile():
    args = _args("audio_flac_best")
    assert _pair_present(args, "--audio-format", "flac")
    assert "--extract-audio" in args
    # audio profiles disable sponsorblock
    assert "--sponsorblock-mark" not in args


def test_metadata_only_skips_download_and_body():
    args = _args("metadata_only")
    assert "--skip-download" in args
    # embed/link args must NOT be present when skipping the body
    assert "--embed-thumbnail" not in args
    assert "--embed-metadata" not in args
    assert "--write-link" not in args
    assert "--no-download-archive" in args


def test_comments_refresh_only_is_comment_focused():
    args = _args("comments_refresh_only")
    assert "--skip-download" in args
    assert "--write-comments" in args
    assert "--write-info-json" in args
    # never re-downloads / never uses the duplicate archive
    assert "--download-archive" not in args
    assert "--no-download-archive" in args


def test_live_chat_adds_live_chat_sublang():
    args = _args("video_best_archive")
    # best_archive enables write_live_chat -> sub-langs should include live_chat
    assert "live_chat" in _sub_langs_value(args)


# --------------------------------------------------------------------------- #
# Subtitle-language safety (requirement 1-5, 11): never default to "all"
# --------------------------------------------------------------------------- #
def test_metadata_only_does_not_use_sub_langs_all():
    args = _args("metadata_only")
    value = _sub_langs_value(args)
    assert value is not None  # subs are still requested...
    assert value != "all"  # ...but not "all"
    assert "all" not in value.split(",")


def test_metadata_only_has_remote_components():
    args = _args("metadata_only")
    assert _pair_present(args, "--remote-components", "ejs:github")


def test_compressed_1080p_does_not_use_sub_langs_all():
    args = _args("video_compressed_1080p")
    assert _sub_langs_value(args) != "all"


def test_proxy_does_not_use_sub_langs_all():
    args = _args("video_proxy_1080p_mp4")
    assert _sub_langs_value(args) != "all"


def test_remote_components_on_by_default():
    # present even without explicitly passing it (BuildContext default)
    assert _pair_present(_args("video_compressed_1080p"), "--remote-components", "ejs:github")


def test_default_sub_langs_reflected_in_builder():
    args = _args("metadata_only", default_sub_langs="ja,en")
    assert _pair_present(args, "--sub-langs", "ja,en")


def test_best_archive_uses_archive_sub_langs():
    # video_best_archive follows ARCHIVE_SUB_LANGS; "all" must be opt-in only here
    args = _args("video_best_archive", archive_sub_langs="all")
    assert "all" in _sub_langs_value(args).split(",")
    # default archive langs are limited, not "all"
    args2 = _args("video_best_archive", archive_sub_langs="ja,en.*,en")
    assert "all" not in _sub_langs_value(args2).split(",")


def test_all_subs_profile_uses_all():
    args = _args("video_best_archive_all_subs")
    assert "all" in _sub_langs_value(args).split(",")


def test_remote_components_always_on_even_with_blank_env():
    # A stale .env with YTDLP_REMOTE_COMPONENTS= (blank) must still resolve to
    # ejs:github (requirement 6); only an explicit sentinel disables it.
    from app.config import Settings

    assert Settings(_env_file=None, ytdlp_remote_components="ejs:github").effective_remote_components == "ejs:github"
    assert Settings(_env_file=None, ytdlp_remote_components="").effective_remote_components == "ejs:github"
    assert Settings(_env_file=None, ytdlp_remote_components="none").effective_remote_components is None


def test_resolve_sub_langs():
    assert resolve_sub_langs("@default", "ja,en", "all") == "ja,en"
    assert resolve_sub_langs("@archive", "ja,en", "all") == "all"
    assert resolve_sub_langs("fr,de", "ja,en", "all") == "fr,de"
    assert resolve_sub_langs(None, "ja,en", "all") == "ja,en"


def test_sub_langs_policy_persists_through_db(session):
    seed_builtin_profiles(session)
    archive = get_profile_spec(session, "video_best_archive")
    assert archive.sub_langs == "@archive"
    allsubs = get_profile_spec(session, "video_best_archive_all_subs")
    assert allsubs.sub_langs == "all"
    meta = get_profile_spec(session, "metadata_only")
    assert meta.sub_langs == "@default"
    # the policy key must not leak into the boolean flags
    assert "_sub_langs" not in meta.resolved_flags()


def test_external_tools_appended():
    args = _args(
        "video_compressed_1080p",
        cookies_file="/secrets/cookies.txt",
        ffmpeg_location="/usr/bin",
        deno_path="/usr/local/bin/deno",
        remote_components="ejs:github",
    )
    assert _pair_present(args, "--cookies", "/secrets/cookies.txt")
    assert _pair_present(args, "--ffmpeg-location", "/usr/bin")
    assert _pair_present(args, "--js-runtimes", "deno:/usr/local/bin/deno")
    assert _pair_present(args, "--remote-components", "ejs:github")


def test_redact_masks_password_not_cookies():
    raw = ["yt-dlp", "--cookies", "/secrets/cookies.txt", "--password", "hunter2"]
    red = redact_args(raw)
    assert "/secrets/cookies.txt" in red  # path kept (re-runnable)
    assert "hunter2" not in red  # value masked
    assert "******" in red


def test_seed_and_lookup(session):
    written = seed_builtin_profiles(session)
    assert written == 8
    spec = get_profile_spec(session, "video_best_archive")
    assert spec.media_mode == "video"
    # idempotent: second seed writes 0 new rows
    assert seed_builtin_profiles(session) == 0
