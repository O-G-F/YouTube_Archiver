"""Download profiles and yt-dlp argument construction.

This module is the new-system equivalent of the old conf overlay design
(requirement 4.1):

    common base args  ->  YouTube overlay  ->  profile overlay  ->  job/context args

Profiles are stored in the DB (``download_profiles``) but the 7 built-ins
(added requirement 7) are defined here in code and seeded idempotently.

``metadata_flags`` is the source of truth for "extras" (comments, subs,
thumbnail, sponsorblock, skip-download); ``ytdlp_args`` holds the format /
quality / container specifics. :func:`build_ytdlp_args` translates a profile +
context into the exact argv list (never a shell string) that the wrapper runs
and records.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from typing import Iterable

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import DownloadProfile

# Default extras; each profile overrides a subset.
DEFAULT_FLAGS: dict[str, bool] = {
    "write_info_json": True,
    "write_description": True,
    "write_playlist_metafiles": True,
    "write_links": True,
    "embed_metadata": True,
    "embed_chapters": True,
    "write_thumbnail": True,
    "embed_thumbnail": False,
    "write_subs": True,
    "write_auto_subs": True,
    "embed_subs": False,
    "write_comments": False,
    "write_live_chat": False,
    "sponsorblock_mark": True,
    "skip_download": False,
}


@dataclass
class ProfileSpec:
    name: str
    media_mode: str  # video | audio | metadata
    quality_mode: str | None
    description: str
    ytdlp_args: list[str] = field(default_factory=list)
    ffmpeg_args: list[str] = field(default_factory=list)
    flags: dict[str, bool] = field(default_factory=dict)
    # Subtitle-language policy. "@default" / "@archive" resolve from settings;
    # any other value (e.g. "all", "ja,en") is used literally.
    sub_langs: str = "@default"
    is_builtin: bool = True

    def resolved_flags(self) -> dict[str, bool]:
        merged = dict(DEFAULT_FLAGS)
        merged.update(self.flags or {})
        # _sub_langs is persisted in metadata_flags but is not a boolean flag.
        merged.pop("_sub_langs", None)
        return merged

    @classmethod
    def from_db(cls, row: DownloadProfile) -> "ProfileSpec":
        flags = dict(row.metadata_flags or {})
        sub_langs = flags.pop("_sub_langs", "@default")
        return cls(
            name=row.name,
            media_mode=row.media_mode,
            quality_mode=row.quality_mode,
            description=row.description or "",
            ytdlp_args=list(row.ytdlp_args or []),
            ffmpeg_args=list(row.ffmpeg_args or []),
            flags=flags,
            sub_langs=sub_langs,
            is_builtin=bool(row.is_builtin),
        )


def _flags(**overrides: bool) -> dict[str, bool]:
    return overrides


def resolve_sub_langs(policy: str | None, default_sub_langs: str,
                      archive_sub_langs: str) -> str:
    """Resolve a profile's sub-langs policy into a literal yt-dlp value.

    "@default" -> DEFAULT_SUB_LANGS, "@archive" -> ARCHIVE_SUB_LANGS, anything
    else is treated as a literal (e.g. "all", "ja,en").
    """
    p = policy or "@default"
    if p == "@default":
        return default_sub_langs
    if p == "@archive":
        return archive_sub_langs
    return p


# --------------------------------------------------------------------------- #
# Built-in profiles (added requirement 7)
# --------------------------------------------------------------------------- #
BUILTIN_PROFILES: dict[str, ProfileSpec] = {
    "video_best_archive": ProfileSpec(
        name="video_best_archive",
        media_mode="video",
        quality_mode="best",
        description="Long-term archive: best quality, mkv, all metadata/comments/live chat. "
        "Subtitles follow ARCHIVE_SUB_LANGS (default limited; set =all for everything).",
        ytdlp_args=["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mkv"],
        flags=_flags(
            embed_thumbnail=True,
            embed_subs=True,
            write_comments=True,
            write_live_chat=True,
        ),
        sub_langs="@archive",
    ),
    "video_best_archive_all_subs": ProfileSpec(
        name="video_best_archive_all_subs",
        media_mode="video",
        quality_mode="best",
        description="Like video_best_archive but captures ALL subtitle languages "
        "(--sub-langs all). Heavier and more likely to hit YouTube 429s.",
        ytdlp_args=["-f", "bestvideo+bestaudio/best", "--merge-output-format", "mkv"],
        flags=_flags(
            embed_thumbnail=True,
            embed_subs=True,
            write_comments=True,
            write_live_chat=True,
        ),
        sub_langs="all",
    ),
    "video_compressed_1080p": ProfileSpec(
        name="video_compressed_1080p",
        media_mode="video",
        quality_mode="1080p",
        description="Default save profile: <=1080p, mp4-compatible when possible, Web-UI friendly.",
        ytdlp_args=[
            "-f",
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/"
            "bestvideo[height<=1080]+bestaudio/"
            "best[height<=1080][ext=mp4]/best[height<=1080]/best",
            "--merge-output-format",
            "mp4",
        ],
        flags=_flags(embed_thumbnail=True, write_comments=True),
    ),
    "video_proxy_1080p_mp4": ProfileSpec(
        name="video_proxy_1080p_mp4",
        media_mode="video",
        quality_mode="proxy",
        description="Browser-playback proxy: <=1080p H.264/AAC mp4 (does not replace the archive).",
        ytdlp_args=[
            "-f",
            "bestvideo[height<=1080][vcodec^=avc1]+bestaudio[acodec^=mp4a]/"
            "best[height<=1080][ext=mp4]/best[height<=1080]/best",
            "--merge-output-format",
            "mp4",
            "--recode-video",
            "mp4",
        ],
        flags=_flags(
            write_comments=False,
            write_subs=True,
            write_auto_subs=False,
            embed_subs=False,
        ),
    ),
    "audio_flac_best": ProfileSpec(
        name="audio_flac_best",
        media_mode="audio",
        quality_mode="flac",
        description="Best-quality audio archive: bestaudio -> flac, thumbnail embedded.",
        ytdlp_args=["-f", "bestaudio", "--extract-audio", "--audio-format", "flac"],
        flags=_flags(
            embed_thumbnail=True,
            write_subs=False,
            write_auto_subs=False,
            sponsorblock_mark=False,
        ),
    ),
    "audio_opus_save_space": ProfileSpec(
        name="audio_opus_save_space",
        media_mode="audio",
        quality_mode="opus",
        description="Space-saving audio: bestaudio -> opus (avoids re-encode when source is opus).",
        ytdlp_args=["-f", "bestaudio", "--extract-audio", "--audio-format", "opus"],
        flags=_flags(
            embed_thumbnail=True,
            write_subs=False,
            write_auto_subs=False,
            sponsorblock_mark=False,
        ),
    ),
    "metadata_only": ProfileSpec(
        name="metadata_only",
        media_mode="metadata",
        quality_mode="none",
        description="No media body: info.json, description, subtitles and thumbnail only.",
        ytdlp_args=[],
        flags=_flags(
            skip_download=True,
            write_comments=False,
            write_thumbnail=True,
            sponsorblock_mark=False,
        ),
    ),
    "comments_refresh_only": ProfileSpec(
        name="comments_refresh_only",
        media_mode="metadata",
        quality_mode="none",
        description="No media body: comment refresh only (used by metadata_refresh jobs).",
        ytdlp_args=[],
        flags=_flags(
            skip_download=True,
            write_comments=True,
            write_thumbnail=False,
            write_subs=False,
            write_auto_subs=False,
            write_description=False,
            write_links=False,
            sponsorblock_mark=False,
        ),
    ),
    "live_chat_refresh_only": ProfileSpec(
        name="live_chat_refresh_only",
        media_mode="metadata",
        quality_mode="none",
        description="No media body: live chat only (--write-subs --sub-langs live_chat).",
        ytdlp_args=[],
        flags=_flags(
            skip_download=True,
            write_comments=False,
            write_thumbnail=False,
            write_subs=False,
            write_auto_subs=False,
            write_description=False,
            write_links=False,
            write_live_chat=True,
            sponsorblock_mark=False,
        ),
        sub_langs="live_chat",
    ),
    "subtitles_refresh_only": ProfileSpec(
        name="subtitles_refresh_only",
        media_mode="metadata",
        quality_mode="none",
        description="No media body: subtitles only (--write-subs --write-auto-subs); "
        "re-fetch subtitles that failed on a metadata/download job.",
        ytdlp_args=[],
        flags=_flags(
            skip_download=True,
            write_subs=True,
            write_auto_subs=True,
            write_comments=False,
            write_thumbnail=False,
            write_info_json=False,
            write_description=False,
            write_links=False,
            write_live_chat=False,
            sponsorblock_mark=False,
        ),
        sub_langs="@default",
    ),
}


# --------------------------------------------------------------------------- #
# Argument builder
# --------------------------------------------------------------------------- #
@dataclass
class BuildContext:
    output_template: str
    download_archive: str | None = None
    no_playlist: bool = False
    cookies_file: str | None = None
    # YouTube fetch stabilization (Phase 7A): browser cookies + PO token (secret).
    cookies_from_browser: str | None = None
    po_token: str | None = None
    ffmpeg_location: str | None = None
    deno_path: str | None = None
    # Remote-components default ON (YouTube JS challenge solver). See requirement 6.
    remote_components: str | None = "ejs:github"
    # Subtitle-language sources; the profile's policy selects between them.
    default_sub_langs: str = "ja,en"
    archive_sub_langs: str = "ja,en"
    max_comments: int = 0
    # Seconds between yt-dlp retries (e.g. on HTTP 429). 0 = yt-dlp default.
    retry_sleep: int = 0
    extra_args: list[str] = field(default_factory=list)


# Runtime/reliability args applied to every invocation (cross-platform subset
# of the old yt-dlp_base.conf). Output and download-archive are NOT here; they
# are per-job context, exactly as in the original conf design.
RUNTIME_ARGS: tuple[str, ...] = (
    "--ignore-config",  # our args are authoritative & reproducible
    "--no-abort-on-error",
    "--retries",
    "5",
    "--file-access-retries",
    "5",
    "--concurrent-fragments",
    "10",
    "--newline",
    "--progress",
    "--mtime",
    "--windows-filenames",  # NAS / Windows-safe filenames
    "--convert-thumbnails",
    "jpg",
)


def build_ytdlp_args(spec: ProfileSpec, ctx: BuildContext) -> list[str]:
    """Build the full yt-dlp argv (excluding the binary and the URL/batch)."""
    flags = spec.resolved_flags()
    skip = bool(flags.get("skip_download"))
    args: list[str] = list(RUNTIME_ARGS)

    # ----- metadata files -----
    if flags.get("write_info_json"):
        args.append("--write-info-json")
    if flags.get("write_description"):
        args.append("--write-description")
    if flags.get("write_playlist_metafiles") and not ctx.no_playlist:
        args.append("--write-playlist-metafiles")

    # ----- embed + link files only make sense when media is downloaded -----
    if not skip:
        if flags.get("embed_metadata"):
            args.append("--embed-metadata")
        if flags.get("embed_chapters"):
            args.append("--embed-chapters")
        if flags.get("write_links"):
            args += [
                "--write-link",
                "--write-url-link",
                "--write-webloc-link",
                "--write-desktop-link",
            ]

    # ----- thumbnail -----
    if flags.get("write_thumbnail"):
        args.append("--write-thumbnail")
    if flags.get("embed_thumbnail") and not skip:
        args.append("--embed-thumbnail")

    # ----- subtitles / live chat -----
    sub_langs = resolve_sub_langs(
        spec.sub_langs, ctx.default_sub_langs, ctx.archive_sub_langs
    )
    want_subs = bool(flags.get("write_subs") or flags.get("write_auto_subs"))
    if flags.get("write_live_chat"):
        want_subs = True
        if "live_chat" not in sub_langs.split(","):
            sub_langs = f"{sub_langs},live_chat"
    if flags.get("write_subs") or flags.get("write_live_chat"):
        args.append("--write-subs")
    if flags.get("write_auto_subs"):
        args.append("--write-auto-subs")
    if flags.get("embed_subs") and not skip:
        args.append("--embed-subs")
    if want_subs:
        args += ["--sub-langs", sub_langs]

    # ----- comments -----
    if flags.get("write_comments"):
        args.append("--write-comments")
        if ctx.max_comments and ctx.max_comments > 0:
            # First field caps the overall number of comments fetched.
            args += ["--extractor-args", f"youtube:max_comments={ctx.max_comments}"]

    # ----- sponsorblock (YouTube overlay) -----
    if flags.get("sponsorblock_mark") and not skip:
        args += ["--sponsorblock-mark", "all,-preview"]

    # ----- skip download -----
    if skip:
        args.append("--skip-download")

    # ----- profile format / quality / container -----
    args += list(spec.ytdlp_args)
    if spec.ffmpeg_args:
        args += ["--postprocessor-args", "ffmpeg:" + " ".join(spec.ffmpeg_args)]

    # ----- output template -----
    args += ["-o", ctx.output_template]

    # ----- download archive (duplicate avoidance). Never for refresh jobs. -----
    if ctx.download_archive:
        args += ["--download-archive", ctx.download_archive]
    else:
        # Ensure no stray archive interferes with refresh/skip-download jobs.
        args.append("--no-download-archive")

    if ctx.no_playlist:
        args.append("--no-playlist")

    # ----- retry backoff (e.g. for HTTP 429) -----
    if ctx.retry_sleep and ctx.retry_sleep > 0:
        args += ["--retry-sleep", str(ctx.retry_sleep)]

    # ----- external tools -----
    if ctx.ffmpeg_location:
        args += ["--ffmpeg-location", ctx.ffmpeg_location]
    if ctx.deno_path:
        args += ["--js-runtimes", f"deno:{ctx.deno_path}"]
    if ctx.remote_components:
        args += ["--remote-components", ctx.remote_components]
    if ctx.cookies_file:
        args += ["--cookies", ctx.cookies_file]
    elif ctx.cookies_from_browser:
        # Only when no cookies.txt (the two are mutually exclusive in yt-dlp).
        args += ["--cookies-from-browser", ctx.cookies_from_browser]
    if ctx.po_token:
        # Secret value; masked in command.txt by redact_args (po_token=******).
        args += ["--extractor-args", f"youtube:po_token={ctx.po_token}"]

    # ----- admin-only escape hatch -----
    if ctx.extra_args:
        args += list(ctx.extra_args)

    return args


# --------------------------------------------------------------------------- #
# DB seeding / lookup
# --------------------------------------------------------------------------- #
def seed_builtin_profiles(session: Session) -> int:
    """Idempotently upsert the built-in profiles. Returns count of writes."""
    written = 0
    for spec in BUILTIN_PROFILES.values():
        row = session.scalar(
            select(DownloadProfile).where(DownloadProfile.name == spec.name)
        )
        if row is None:
            row = DownloadProfile(name=spec.name)
            session.add(row)
            written += 1
        row.media_mode = spec.media_mode
        row.quality_mode = spec.quality_mode
        row.description = spec.description
        row.ytdlp_args = list(spec.ytdlp_args)
        row.ffmpeg_args = list(spec.ffmpeg_args)
        # Store the boolean flags plus the (non-bool) sub-langs policy.
        flags = spec.resolved_flags()
        flags["_sub_langs"] = spec.sub_langs
        row.metadata_flags = flags
        row.is_builtin = True
    session.flush()
    return written


def get_profile_spec(session: Session, name: str) -> ProfileSpec:
    """Resolve a profile by name from the DB, falling back to built-ins."""
    row = session.scalar(select(DownloadProfile).where(DownloadProfile.name == name))
    if row is not None:
        return ProfileSpec.from_db(row)
    if name in BUILTIN_PROFILES:
        return BUILTIN_PROFILES[name]
    raise KeyError(f"unknown profile: {name!r}")


def list_profile_names() -> Iterable[str]:
    return BUILTIN_PROFILES.keys()


def with_overrides(spec: ProfileSpec, **flag_overrides: bool) -> ProfileSpec:
    """Return a copy of a spec with some flags overridden (job-level tweaks)."""
    merged = dict(spec.flags)
    merged.update(flag_overrides)
    return replace(spec, flags=merged)
