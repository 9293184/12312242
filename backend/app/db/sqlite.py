"""SQLite helpers for the PaperReading backend.

The goal is to keep database bootstrapping deterministic:
- create directories when needed
- ensure foreign keys are enabled
- apply schema before serving API requests
"""

from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from app.core.config import settings

logger = logging.getLogger(__name__)


def _connect(db_path: Path) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON;")
    conn.execute("PRAGMA busy_timeout = 10000;")
    # WAL：读不阻塞写、写不阻塞读，适合「请求线程池 + 后台分析线程」并发的本地场景
    try:
        conn.execute("PRAGMA journal_mode = WAL;")
        conn.execute("PRAGMA synchronous = NORMAL;")
    except sqlite3.Error:
        pass
    return conn


def checkpoint_database() -> None:
    """把 WAL 中已提交的事务合并回主库文件。

    启用 WAL 后，仅复制 ``paperreading.db`` 可能拿不到最新数据（还在 -wal 里），
    因此备份前必须先 checkpoint。
    """
    try:
        conn = _connect(settings.db_path)
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE);")
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        logger.warning("wal_checkpoint_failed", exc_info=True)


@contextmanager
def session() -> Iterator[sqlite3.Connection]:
    conn = _connect(settings.db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def to_utc_isoformat(value) -> str:
    """将 SQLite 返回的 UTC 时间字符串转换为带时区标识的 ISO 格式。
    
    SQLite 的 CURRENT_TIMESTAMP 默认存储 UTC 时间，但返回的字符串（如 '2026-07-29 05:15:13'）
    不带时区信息，前端解析时会被当作本地时间导致 8 小时偏差。
    转换为 ISO 格式并加上时区标识，前端可正确解析并显示本地时间。
    """
    if not value:
        return ""
    try:
        if isinstance(value, str):
            for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M:%S.%f"):
                try:
                    dt = datetime.strptime(value, fmt)
                    dt = dt.replace(tzinfo=timezone.utc)
                    return dt.isoformat()
                except ValueError:
                    continue
            return value
        elif isinstance(value, datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=timezone.utc)
            return value.isoformat()
        return str(value)
    except Exception:
        return str(value) if value else ""


def _ensure_legacy_columns(conn: sqlite3.Connection) -> None:
    """Backfill columns that may be missing in an older local database."""

    tables = {
        "papers": ("updated_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
        "attachments": ("updated_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
        "paper_texts": ("updated_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
        "paper_analysis": ("updated_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
        "paper_read_state": ("updated_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
        "import_jobs": ("updated_at", "TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP"),
    }

    for table, (column, definition) in tables.items():
        exists = conn.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
            (table, column),
        ).fetchone()
        if exists is None:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # P1: extraction_method columns for Markdown/OCR source tracking
    extraction_method_columns = {
        "paper_texts": ("extraction_method", "TEXT NOT NULL DEFAULT 'first_six_pages'"),
        "paper_analysis": ("extraction_method", "TEXT NOT NULL DEFAULT 'first_six_pages'"),
    }
    for table, (column, definition) in extraction_method_columns.items():
        exists = conn.execute(
            "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
            (table, column),
        ).fetchone()
        if exists is None:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")

    # TLDR column on paper_analysis — added to support the new TLDR
    # analysis step. Stored next to other eight-dimension analysis fields.
    tldr_exists = conn.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
        ("paper_analysis", "tldr"),
    ).fetchone()
    if tldr_exists is None:
        conn.execute("ALTER TABLE paper_analysis ADD COLUMN tldr TEXT")

    # folder_id column on papers — links a paper to a folder (nullable).
    # Folders feature supports up to 3 levels of nesting.
    folder_id_exists = conn.execute(
        "SELECT 1 FROM pragma_table_info(?) WHERE name = ?",
        ("papers", "folder_id"),
    ).fetchone()
    if folder_id_exists is None:
        conn.execute("ALTER TABLE papers ADD COLUMN folder_id TEXT")

    # Ensure the folders table exists for older databases created before
    # the folders feature. The schema.sql CREATE TABLE IF NOT EXISTS also
    # handles this on fresh init, but _ensure_legacy_columns runs on every
    # startup so this covers the upgrade path reliably.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS folders (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            parent_id TEXT,
            level INTEGER NOT NULL DEFAULT 1 CHECK (level IN (1, 2, 3)),
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (parent_id) REFERENCES folders(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_parent_id ON folders(parent_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_folders_level ON folders(level)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_papers_folder_id ON papers(folder_id)")

    # Chat tables for AI-assisted reading conversations.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_sessions (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            title TEXT NOT NULL DEFAULT '',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_paper_id ON chat_sessions(paper_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_sessions_created_at ON chat_sessions(created_at)")

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS chat_messages (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
            content TEXT NOT NULL,
            citations_json TEXT,
            parent_id TEXT,
            is_deleted INTEGER NOT NULL DEFAULT 0,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (session_id) REFERENCES chat_sessions(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_session_id ON chat_messages(session_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_chat_messages_created_at ON chat_messages(created_at)")

    # Paper annotations table: stores PDF annotation highlights per paper +
    # attachment type as a JSON array. Positions use PDF-page-relative
    # normalized coordinates (ScaledPosition) so they survive zoom/scroll.
    # Created here on legacy databases; schema.sql also handles fresh init.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_annotations (
            id TEXT PRIMARY KEY,
            paper_id TEXT NOT NULL,
            attachment_type TEXT NOT NULL DEFAULT 'original',
            annotations_json TEXT NOT NULL DEFAULT '[]',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
            CONSTRAINT ck_anno_attachment_type CHECK (attachment_type IN ('original', 'translated', 'mapped'))
        )
        """
    )
    conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_paper_annotations ON paper_annotations(paper_id, attachment_type)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_annotations_paper_id ON paper_annotations(paper_id)")

    # Tags + paper_tags: flat many-to-many classification dimension,
    # orthogonal to folders. Created here on legacy databases; schema.sql
    # also handles fresh init. Uniqueness is enforced in the service layer
    # via case-insensitive name lookup (auto-reuse on duplicate).
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS tags (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL,
            color TEXT NOT NULL DEFAULT '#9AA7B4',
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS paper_tags (
            paper_id TEXT NOT NULL,
            tag_id TEXT NOT NULL,
            created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (paper_id, tag_id),
            FOREIGN KEY (paper_id) REFERENCES papers(id) ON DELETE CASCADE,
            FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
        )
        """
    )
    conn.execute("CREATE INDEX IF NOT EXISTS idx_paper_tags_tag_id ON paper_tags(tag_id)")


def initialize_database(with_seed: bool = False) -> None:
    schema_sql = settings.schema_path.read_text(encoding="utf-8")
    with session() as conn:
        conn.executescript(schema_sql)
        _ensure_legacy_columns(conn)
        if with_seed and settings.seed_path.exists():
            conn.executescript(settings.seed_path.read_text(encoding="utf-8"))


def purge_database_data() -> None:
    """Remove all rows while keeping the schema intact."""

    # 删除顺序按外键依赖排列：先删子表再删父表，避免触发外键约束。
    tables = [
        "paper_tags",
        "paper_annotations",
        "chat_messages",
        "chat_sessions",
        "paper_analysis",
        "paper_texts",
        "paper_read_state",
        "attachments",
        "import_jobs",
        "papers",
        "tags",
        "folders",
    ]
    with session() as conn:
        for table in tables:
            conn.execute(f"DELETE FROM {table}")
        # sqlite_sequence 仅在存在 AUTOINCREMENT 列时才会被创建；本项目主键
        # 均为 TEXT，该表通常不存在，直接 DELETE 会抛 "no such table"。
        has_sequence = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'sqlite_sequence'"
        ).fetchone()
        if has_sequence is not None:
            conn.execute("DELETE FROM sqlite_sequence")
