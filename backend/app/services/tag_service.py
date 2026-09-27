"""Tag service for PaperReading.

Flat (no hierarchy) many-to-many classification dimension for papers,
orthogonal to folders. Tag names are case-insensitively deduplicated: creating
a tag whose name already exists returns the existing tag (auto-reuse) instead
of erroring. Colors are restricted to a soft preset palette (no fluorescent).
"""

from __future__ import annotations

import logging
import re
import sqlite3
from uuid import uuid4

from app.db import session
from app.db.sqlite import to_utc_isoformat
from app.models import (
    MAX_TAG_NAME_LEN,
    TAG_PRESET_COLORS,
    TagCreate,
    TagResponse,
    TagUpdate,
)

logger = logging.getLogger(__name__)

# At least one letter / digit / underscore / CJK ideograph — rejects
# pure-punctuation names like "!!!" or "---".
_NAME_HAS_CONTENT_RE = re.compile(r"[\w\u4e00-\u9fff]", re.UNICODE)
_HEX_COLOR_RE = re.compile(r"^#[0-9a-fA-F]{6}$")


def _row_to_response(row, paper_count: int = 0) -> TagResponse:
    return TagResponse(
        id=row["id"],
        name=row["name"],
        color=row["color"],
        paper_count=paper_count,
        created_at=to_utc_isoformat(row["created_at"]),
        updated_at=to_utc_isoformat(row["updated_at"]),
    )


def _get_paper_counts() -> dict[str, int]:
    """Bulk-fetch paper counts per tag_id."""
    counts: dict[str, int] = {}
    with session() as conn:
        rows = conn.execute(
            "SELECT tag_id, COUNT(*) AS cnt FROM paper_tags GROUP BY tag_id"
        ).fetchall()
    for r in rows:
        counts[r["tag_id"]] = r["cnt"]
    return counts


def _normalize_and_validate_name(raw: str) -> str:
    """Trim, enforce length, and reject pure-symbol names."""
    name = (raw or "").strip()
    if not name:
        raise ValueError("标签名不能为空")
    if len(name) > MAX_TAG_NAME_LEN:
        raise ValueError(f"标签名最长 {MAX_TAG_NAME_LEN} 字符")
    if not _NAME_HAS_CONTENT_RE.search(name):
        raise ValueError("标签名不能为纯符号")
    return name


def _validate_color(color: str | None) -> str:
    """Return a valid preset color; raise on fluorescent/invalid input.

    Falls back to the first preset when color is missing. Rejects any hex
    not in the closed preset set, which is how fluorescent colors are banned
    at the data layer.
    """
    if not color:
        return TAG_PRESET_COLORS[0]
    c = color.strip()
    if c not in TAG_PRESET_COLORS:
        if _HEX_COLOR_RE.match(c):
            raise ValueError("颜色不在柔和预设色板内（禁止荧光色）")
        raise ValueError("颜色格式无效，需为 #RRGGBB 预设色")
    return c


def list_tags() -> list[TagResponse]:
    """Flat list of all tags ordered by created_at, each with paper_count."""
    counts = _get_paper_counts()
    with session() as conn:
        rows = conn.execute("SELECT * FROM tags ORDER BY created_at ASC").fetchall()
    return [_row_to_response(r, counts.get(r["id"], 0)) for r in rows]


def get_tag(tag_id: str) -> TagResponse | None:
    counts = _get_paper_counts()
    with session() as conn:
        row = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
    if row is None:
        return None
    return _row_to_response(row, counts.get(tag_id, 0))


def create_tag(payload: TagCreate) -> TagResponse:
    """Create a tag. If the name already exists (case-insensitive), reuse it.

    This implements the "auto-complete and reuse" rule: a duplicate name is
    NOT an error — the existing tag is returned so callers can treat create
    as an upsert-by-name.
    """
    name = _normalize_and_validate_name(payload.name)
    color = _validate_color(payload.color)
    with session() as conn:
        # 先取写锁再查：避免并发创建同名标签时两个请求都查不到、各插一条。
        # （tags.name 没有唯一约束，唯一性由服务层的大小写不敏感查重保证。）
        try:
            conn.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError:
            pass
        # Case-insensitive dedup so 'Deep Learning' reuses 'deep learning'.
        existing = conn.execute(
            "SELECT * FROM tags WHERE LOWER(name) = LOWER(?)", (name,)
        ).fetchone()
        if existing is not None:
            logger.info("tag_reused id=%s name=%s", existing["id"], name)
            return _row_to_response(existing, _count_for(conn, existing["id"]))
        tag_id = str(uuid4())
        conn.execute(
            "INSERT INTO tags (id, name, color) VALUES (?, ?, ?)",
            (tag_id, name, color),
        )
        row = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
    logger.info("tag_created id=%s name=%s color=%s", tag_id, name, color)
    return _row_to_response(row, 0)


def _count_for(conn, tag_id: str) -> int:
    row = conn.execute(
        "SELECT COUNT(*) AS cnt FROM paper_tags WHERE tag_id = ?", (tag_id,)
    ).fetchone()
    return row["cnt"] if row else 0


def update_tag(tag_id: str, payload: TagUpdate) -> TagResponse | None:
    """Rename and/or recolor a tag.

    Renaming to a name that already exists (case-insensitive) on a *different*
    tag is an error — merging must go through the dedicated merge endpoint,
    not rename. Renaming to the tag's own current name is a no-op.
    """
    new_name = _normalize_and_validate_name(payload.name) if payload.name is not None else None
    new_color = _validate_color(payload.color) if payload.color is not None else None

    with session() as conn:
        row = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if row is None:
            return None
        if new_name is not None:
            clash = conn.execute(
                "SELECT id FROM tags WHERE LOWER(name) = LOWER(?) AND id <> ?",
                (new_name, tag_id),
            ).fetchone()
            if clash is not None:
                raise ValueError("标签名已被占用，如需合并请使用合并功能")
        if new_name is not None or new_color is not None:
            conn.execute(
                "UPDATE tags SET name = COALESCE(?, name), color = COALESCE(?, color), updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (new_name, new_color, tag_id),
            )
        row = conn.execute("SELECT * FROM tags WHERE id = ?", (tag_id,)).fetchone()
        count = _count_for(conn, tag_id)
    return _row_to_response(row, count)


def delete_tag(tag_id: str) -> bool:
    """Delete a tag. paper_tags rows are cleared by FK ON DELETE CASCADE."""
    with session() as conn:
        row = conn.execute("SELECT id FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    logger.info("tag_deleted id=%s", tag_id)
    return True


def merge_tags(source_ids: list[str], target_id: str) -> TagResponse:
    """Merge source tags into the target tag.

    For each source, all paper_tags links are copied into the target (deduped
    by the composite PK via INSERT OR IGNORE), then the source tags are
    deleted (their residual paper_tags rows are cleaned by FK CASCADE).
    The target itself is excluded from sources to avoid self-merge.
    """
    # Dedup + exclude target
    seen: set[str] = set()
    sources: list[str] = []
    for sid in source_ids:
        if sid == target_id or sid in seen:
            continue
        seen.add(sid)
        sources.append(sid)

    with session() as conn:
        if conn.execute("SELECT 1 FROM tags WHERE id = ?", (target_id,)).fetchone() is None:
            raise ValueError("目标标签不存在")
        # Validate sources exist (skip silently those that don't)
        valid_sources: list[str] = []
        for sid in sources:
            if conn.execute("SELECT 1 FROM tags WHERE id = ?", (sid,)).fetchone() is not None:
                valid_sources.append(sid)
        for src in valid_sources:
            conn.execute(
                "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id, created_at) "
                "SELECT paper_id, ?, created_at FROM paper_tags WHERE tag_id = ?",
                (target_id, src),
            )
        if valid_sources:
            placeholders = ",".join("?" * len(valid_sources))
            conn.execute(
                f"DELETE FROM tags WHERE id IN ({placeholders})", valid_sources
            )
    logger.info("tags_merged target=%s sources=%s", target_id, valid_sources)
    result = get_tag(target_id)
    if result is None:
        raise ValueError("目标标签不存在")
    return result


def get_tag_papers(tag_id: str) -> list[dict]:
    """List papers carrying the given tag."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.title_cn, p.title_en, p.authors, p.status,
                   p.folder_id, p.created_at
            FROM papers p
            JOIN paper_tags pt ON pt.paper_id = p.id
            WHERE pt.tag_id = ?
            ORDER BY p.created_at DESC
            """,
            (tag_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"] or "",
            "title_cn": r["title_cn"] or "",
            "title_en": r["title_en"] or "",
            "authors": r["authors"] or "",
            "status": r["status"] or "uploaded",
            "folder_id": r["folder_id"],
            "created_at": to_utc_isoformat(r["created_at"]),
        }
        for r in rows
    ]


def get_tag_available_papers(tag_id: str) -> list[dict]:
    """List papers NOT yet carrying the given tag.

    Used to populate the batch-add picker on the tag management page. A paper
    may appear here even if it carries other tags — tags are orthogonal and
    many-to-many.
    """
    with session() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.title_cn, p.title_en, p.authors, p.status,
                   p.folder_id, p.created_at
            FROM papers p
            WHERE p.id NOT IN (SELECT paper_id FROM paper_tags WHERE tag_id = ?)
            ORDER BY p.created_at DESC
            """,
            (tag_id,),
        ).fetchall()
    return [
        {
            "id": r["id"],
            "title": r["title"] or "",
            "title_cn": r["title_cn"] or "",
            "title_en": r["title_en"] or "",
            "authors": r["authors"] or "",
            "status": r["status"] or "uploaded",
            "folder_id": r["folder_id"],
            "created_at": to_utc_isoformat(r["created_at"]),
        }
        for r in rows
    ]


def batch_add_papers(tag_id: str, paper_ids: list[str]) -> dict:
    """Batch-link many papers to one tag (idempotent via composite PK).

    No count cap — the flat many-to-many design imposes no quota on how many
    papers a tag may carry. Non-existent paper IDs are reported as failures.
    Returns aggregate {total, success_count, failed_count, results}.
    """
    results: list[dict] = []
    with session() as conn:
        if conn.execute("SELECT 1 FROM tags WHERE id = ?", (tag_id,)).fetchone() is None:
            raise ValueError("标签不存在")
        for pid in paper_ids:
            exists = conn.execute("SELECT 1 FROM papers WHERE id = ?", (pid,)).fetchone()
            if exists is None:
                results.append({"paper_id": pid, "success": False, "error": "文献不存在"})
                continue
            conn.execute(
                "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)",
                (pid, tag_id),
            )
            results.append({"paper_id": pid, "success": True, "error": None})
    success_count = sum(1 for r in results if r["success"])
    failed_count = len(results) - success_count
    logger.info(
        "tag_batch_add tag_id=%s total=%s success=%s failed=%s",
        tag_id, len(results), success_count, failed_count,
    )
    return {
        "tag_id": tag_id,
        "total": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }


def batch_remove_papers(tag_id: str, paper_ids: list[str]) -> dict:
    """Batch-unlink many papers from one tag.

    No count cap. Returns aggregate {total, success_count, failed_count, results}.
    """
    results: list[dict] = []
    with session() as conn:
        for pid in paper_ids:
            conn.execute(
                "DELETE FROM paper_tags WHERE paper_id = ? AND tag_id = ?",
                (pid, tag_id),
            )
            results.append({"paper_id": pid, "success": True, "error": None})
    success_count = sum(1 for r in results if r["success"])
    failed_count = len(results) - success_count
    logger.info(
        "tag_batch_remove tag_id=%s total=%s success=%s failed=%s",
        tag_id, len(results), success_count, failed_count,
    )
    return {
        "tag_id": tag_id,
        "total": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }


def get_paper_tags(paper_id: str) -> list[TagResponse]:
    """List all tags on a paper, ordered by name."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT t.* FROM tags t
            JOIN paper_tags pt ON pt.tag_id = t.id
            WHERE pt.paper_id = ?
            ORDER BY t.name ASC
            """,
            (paper_id,),
        ).fetchall()
    return [_row_to_response(r, 0) for r in rows]


def get_all_paper_tags() -> list[dict]:
    """Bulk-fetch every (paper_id, tag_id, name, color) link in one query.

    Used by the sidebar to render per-paper color dots and drawer checkmarks
    without N+1 requests. Returns a flat list; the frontend groups by paper_id.
    """
    with session() as conn:
        rows = conn.execute(
            """
            SELECT pt.paper_id, pt.tag_id, t.name, t.color
            FROM paper_tags pt
            JOIN tags t ON t.id = pt.tag_id
            ORDER BY t.name ASC
            """
        ).fetchall()
    return [
        {
            "paper_id": r["paper_id"],
            "tag_id": r["tag_id"],
            "name": r["name"],
            "color": r["color"],
        }
        for r in rows
    ]


def set_paper_tags(paper_id: str, tag_ids: list[str]) -> list[TagResponse]:
    """Full-replace a paper's tags. Diffs against current set to minimize writes."""
    with session() as conn:
        paper = conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if paper is None:
            raise ValueError("论文不存在")
        current = {
            r["tag_id"]
            for r in conn.execute(
                "SELECT tag_id FROM paper_tags WHERE paper_id = ?", (paper_id,)
            ).fetchall()
        }
        target = set(tag_ids)
        for tid in target - current:
            conn.execute(
                "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)",
                (paper_id, tid),
            )
        for tid in current - target:
            conn.execute(
                "DELETE FROM paper_tags WHERE paper_id = ? AND tag_id = ?",
                (paper_id, tid),
            )
    return get_paper_tags(paper_id)


def add_paper_tag(paper_id: str, tag_id: str) -> list[TagResponse]:
    """Add a single tag to a paper (idempotent via composite PK)."""
    with session() as conn:
        paper = conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if paper is None:
            raise ValueError("论文不存在")
        tag = conn.execute("SELECT id FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if tag is None:
            raise ValueError("标签不存在")
        conn.execute(
            "INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?, ?)",
            (paper_id, tag_id),
        )
    return get_paper_tags(paper_id)


def remove_paper_tag(paper_id: str, tag_id: str) -> list[TagResponse]:
    """Remove a single tag from a paper."""
    with session() as conn:
        conn.execute(
            "DELETE FROM paper_tags WHERE paper_id = ? AND tag_id = ?",
            (paper_id, tag_id),
        )
    return get_paper_tags(paper_id)
