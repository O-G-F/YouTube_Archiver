"""SQLAlchemy ORM models.

This mirrors the DB design in requirement section 8 (all 13 tables) with a few
extra practical columns (e.g. ``jobs.url``, ``jobs.parent_job_id``,
``comments.source``). Column types are deliberately portable so the same models
run on PostgreSQL (production / Docker) and SQLite (tests, local CLI).

Path columns store paths RELATIVE to ``ARCHIVE_ROOT`` (requirement 5.7.2 / added
requirement 3) so the storage root can be relocated (NAS -> external SSD).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def utcnow() -> datetime:
    """Naive UTC timestamp (portable across SQLite and PostgreSQL)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------- #
# Sources & collections
# --------------------------------------------------------------------------- #
class Source(Base):
    __tablename__ = "sources"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # type: url | playlist | channel | likes | takeout | watch_folder
    type: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # api_source: youtube_data_api | takeout | manual | discord | watch_folder
    api_source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    collections: Mapped[list["Collection"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class Collection(Base):
    __tablename__ = "collections"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # type: playlist | channel_videos | channel_shorts | channel_streams | likes
    type: Mapped[str] = mapped_column(String(32), index=True)
    youtube_playlist_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    youtube_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    # crawl_policy: new_only | full | metadata_only ...
    crawl_policy: Mapped[str | None] = mapped_column(String(32), nullable=True)
    download_profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_profiles.id", ondelete="SET NULL"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    source: Mapped["Source | None"] = relationship(back_populates="collections")
    items: Mapped[list["CollectionItem"]] = relationship(
        back_populates="collection", cascade="all, delete-orphan"
    )


class CollectionItem(Base):
    __tablename__ = "collection_items"
    __table_args__ = (
        UniqueConstraint("collection_id", "video_id", name="uq_collection_video"),
        # Phase 2B: DB-level dedup on the external id (the field expand populates).
        UniqueConstraint(
            "collection_id", "youtube_video_id", name="uq_collection_youtube_video"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    collection_id: Mapped[int] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"), index=True
    )
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=True, index=True
    )
    youtube_video_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    position: Mapped[int | None] = mapped_column(Integer, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    removed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    collection: Mapped["Collection"] = relationship(back_populates="items")


# --------------------------------------------------------------------------- #
# Videos & media
# --------------------------------------------------------------------------- #
class Video(Base):
    __tablename__ = "videos"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    youtube_video_id: Mapped[str] = mapped_column(
        String(32), unique=True, index=True
    )
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True, index=True)
    channel_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration: Mapped[int | None] = mapped_column(Integer, nullable=True)
    upload_date: Mapped[str | None] = mapped_column(String(8), nullable=True)  # YYYYMMDD
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    is_short: Mapped[bool] = mapped_column(Boolean, default=False)
    is_live: Mapped[bool] = mapped_column(Boolean, default=False)
    # availability: available | unavailable | private | comments_disabled ...
    availability: Mapped[str | None] = mapped_column(String(32), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_metadata_refresh_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    raw_info_json_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # adaptive comment refresh policy (added requirement 5)
    comments_refresh_policy: Mapped[str] = mapped_column(
        String(32), default="all_videos_adaptive"
    )
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )

    media_files: Mapped[list["MediaFile"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )
    subtitles: Mapped[list["Subtitle"]] = relationship(
        back_populates="video", cascade="all, delete-orphan"
    )


class MediaFile(Base):
    __tablename__ = "media_files"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    # media_type: video | audio | thumbnail | info_json | description | link
    media_type: Mapped[str] = mapped_column(String(32))
    profile: Mapped[str | None] = mapped_column(String(64), nullable=True)
    path: Mapped[str] = mapped_column(Text)  # relative to ARCHIVE_ROOT
    container: Mapped[str | None] = mapped_column(String(16), nullable=True)
    codec_video: Mapped[str | None] = mapped_column(String(32), nullable=True)
    codec_audio: Mapped[str | None] = mapped_column(String(32), nullable=True)
    width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    fps: Mapped[float | None] = mapped_column(Float, nullable=True)
    filesize: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped["Video"] = relationship(back_populates="media_files")


class Subtitle(Base):
    __tablename__ = "subtitles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    language: Mapped[str | None] = mapped_column(String(32), nullable=True)
    is_auto: Mapped[bool] = mapped_column(Boolean, default=False)
    path: Mapped[str] = mapped_column(Text)  # relative to ARCHIVE_ROOT
    format: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)

    video: Mapped["Video"] = relationship(back_populates="subtitles")


# --------------------------------------------------------------------------- #
# Comments & live chat
# --------------------------------------------------------------------------- #
class Comment(Base):
    __tablename__ = "comments"
    __table_args__ = (
        UniqueConstraint("video_id", "comment_id", name="uq_video_comment"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    comment_id: Mapped[str] = mapped_column(String(128))
    parent_comment_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    author_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    like_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    is_deleted_or_missing: Mapped[bool] = mapped_column(Boolean, default=False)
    # source: yt-dlp | youtube_data_api
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    snapshot_id: Mapped[int | None] = mapped_column(
        ForeignKey("metadata_snapshots.id", ondelete="SET NULL"), nullable=True
    )
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class LiveChatMessage(Base):
    __tablename__ = "live_chat_messages"
    __table_args__ = (
        UniqueConstraint("video_id", "message_id", name="uq_video_chatmsg"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), index=True
    )
    message_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    timestamp_ms: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    author_name: Mapped[str | None] = mapped_column(String(512), nullable=True)
    author_channel_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    message: Mapped[str | None] = mapped_column(Text, nullable=True)
    badges: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    amount: Mapped[float | None] = mapped_column(Float, nullable=True)
    currency: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_superchat: Mapped[bool] = mapped_column(Boolean, default=False)
    is_member_message: Mapped[bool] = mapped_column(Boolean, default=False)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


# --------------------------------------------------------------------------- #
# Metadata snapshots
# --------------------------------------------------------------------------- #
class MetadataSnapshot(Base):
    __tablename__ = "metadata_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="CASCADE"), nullable=True, index=True
    )
    # source: yt-dlp | youtube_data_api
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    # snapshot_type: info_json | comments | live_chat
    snapshot_type: Mapped[str] = mapped_column(String(32))
    path: Mapped[str] = mapped_column(Text)  # relative to ARCHIVE_ROOT
    checksum: Mapped[str | None] = mapped_column(String(128), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


# --------------------------------------------------------------------------- #
# Download profiles
# --------------------------------------------------------------------------- #
class DownloadProfile(Base):
    __tablename__ = "download_profiles"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    name: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # media_mode: video | audio | metadata
    media_mode: Mapped[str] = mapped_column(String(16))
    # quality_mode: best | 1080p | 720p | proxy | flac | opus | none
    quality_mode: Mapped[str | None] = mapped_column(String(32), nullable=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    ytdlp_args: Mapped[list] = mapped_column(JSON, default=list)
    ffmpeg_args: Mapped[list] = mapped_column(JSON, default=list)
    metadata_flags: Mapped[dict] = mapped_column(JSON, default=dict)
    is_builtin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# Jobs
# --------------------------------------------------------------------------- #
class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # type: download | metadata_refresh | comments_refresh | live_chat_refresh |
    #       playlist_expand
    type: Mapped[str] = mapped_column(String(32), index=True)
    # status: queued | running | success | failed | canceled
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)
    url: Mapped[str | None] = mapped_column(Text, nullable=True)
    source_id: Mapped[int | None] = mapped_column(
        ForeignKey("sources.id", ondelete="SET NULL"), nullable=True
    )
    collection_id: Mapped[int | None] = mapped_column(
        ForeignKey("collections.id", ondelete="SET NULL"), nullable=True
    )
    video_id: Mapped[int | None] = mapped_column(
        ForeignKey("videos.id", ondelete="SET NULL"), nullable=True
    )
    profile_id: Mapped[int | None] = mapped_column(
        ForeignKey("download_profiles.id", ondelete="SET NULL"), nullable=True
    )
    profile_name: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parent_job_id: Mapped[int | None] = mapped_column(
        ForeignKey("jobs.id", ondelete="SET NULL"), nullable=True
    )
    rq_job_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, default=0)
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Directory holding stdout/stderr/command logs (relative to LOG_ROOT).
    log_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    command_path: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Free-form job inputs (e.g. expand max_items) and results
    # (e.g. discovered_count / created_jobs_count / skipped_existing_count).
    meta: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )


# --------------------------------------------------------------------------- #
# Watch history & diary (populated in later phases; schema present from start)
# --------------------------------------------------------------------------- #
class WatchHistoryEvent(Base):
    __tablename__ = "watch_history_events"
    __table_args__ = (
        # Backstop dedup for events with a video id + timestamp (Phase 3A).
        # Rows with NULL youtube_video_id are deduped in code (by title+time).
        UniqueConstraint(
            "source", "youtube_video_id", "watched_at", name="uq_watch_event"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    # source: takeout | youtube_data_api
    source: Mapped[str | None] = mapped_column(String(32), nullable=True)
    youtube_video_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, index=True
    )
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    channel_title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    watched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)


class SearchHistoryEvent(Base):
    __tablename__ = "search_history_events"
    __table_args__ = (
        UniqueConstraint("source", "query", "searched_at", name="uq_search_event"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    source: Mapped[str | None] = mapped_column(String(32), nullable=True, index=True)
    # Personal data; query is index-bounded (truncated) to stay btree-safe.
    query: Mapped[str | None] = mapped_column(String(512), nullable=True)
    searched_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    raw_json: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class DiaryEntry(Base):
    __tablename__ = "diary_entries"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    date: Mapped[str] = mapped_column(String(10), index=True)  # YYYY-MM-DD
    title: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    markdown: Mapped[str | None] = mapped_column(Text, nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    source_event_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=utcnow, onupdate=utcnow
    )
