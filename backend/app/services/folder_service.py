"""Folder service for PaperReading.

Supports up to 3 levels of nested folders. All write operations validate
the depth constraint and parent existence to keep the tree consistent.
"""

from __future__ import annotations

import logging
import tempfile
from pathlib import Path
from uuid import uuid4

from app.db import session
from app.db.sqlite import to_utc_isoformat
from app.models import FolderCreate, FolderResponse, FolderTreeNode, FolderUpdate, MAX_FOLDER_LEVEL

logger = logging.getLogger(__name__)


def _row_to_response(row, paper_count: int = 0) -> FolderResponse:
    return FolderResponse(
        id=row["id"],
        name=row["name"],
        parent_id=row["parent_id"],
        level=row["level"],
        paper_count=paper_count,
        created_at=to_utc_isoformat(row["created_at"]),
        updated_at=to_utc_isoformat(row["updated_at"]),
    )


def _get_paper_counts() -> dict[str, int]:
    """Bulk-fetch paper counts per folder_id."""
    counts: dict[str, int] = {}
    with session() as conn:
        rows = conn.execute(
            "SELECT folder_id, COUNT(*) AS cnt FROM papers WHERE folder_id IS NOT NULL GROUP BY folder_id"
        ).fetchall()
    for r in rows:
        counts[r["folder_id"]] = r["cnt"]
    return counts


def get_folder(folder_id: str) -> FolderResponse | None:
    with session() as conn:
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    if row is None:
        return None
    counts = _get_paper_counts()
    return _row_to_response(row, counts.get(folder_id, 0))


def _resolve_level(parent_id: str | None) -> int:
    """Determine the level for a new folder given its parent.

    Returns 1 for root folders. Raises ValueError if the parent doesn't
    exist or adding a child would exceed MAX_FOLDER_LEVEL.
    """
    if not parent_id:
        return 1
    with session() as conn:
        parent = conn.execute("SELECT level FROM folders WHERE id = ?", (parent_id,)).fetchone()
    if parent is None:
        raise ValueError("父文件夹不存在")
    new_level = parent["level"] + 1
    if new_level > MAX_FOLDER_LEVEL:
        raise ValueError(f"文件夹最多支持 {MAX_FOLDER_LEVEL} 级，无法在当前文件夹下继续创建子文件夹")
    return new_level


def create_folder(payload: FolderCreate) -> FolderResponse:
    name = (payload.name or "").strip()
    if not name:
        raise ValueError("文件夹名称不能为空")

    level = _resolve_level(payload.parent_id)
    folder_id = str(uuid4())
    with session() as conn:
        conn.execute(
            """
            INSERT INTO folders (id, name, parent_id, level)
            VALUES (?, ?, ?, ?)
            """,
            (folder_id, name, payload.parent_id, level),
        )
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    logger.info("folder_created id=%s name=%s level=%s parent=%s", folder_id, name, level, payload.parent_id)
    return _row_to_response(row, 0)


def update_folder(folder_id: str, payload: FolderUpdate) -> FolderResponse:
    name = (payload.name or "").strip()
    if not name:
        raise ValueError("文件夹名称不能为空")
    with session() as conn:
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE folders SET name = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (name, folder_id),
        )
        row = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
    counts = _get_paper_counts()
    return _row_to_response(row, counts.get(folder_id, 0))


def delete_folder(folder_id: str) -> bool:
    with session() as conn:
        row = conn.execute("SELECT id FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if row is None:
            return False
        # Unlink papers in this folder (ON DELETE SET NULL semantics for
        # papers.folder_id, applied explicitly since folder_id FK may not
        # be enforced on legacy DBs).
        conn.execute("UPDATE papers SET folder_id = NULL WHERE folder_id = ?", (folder_id,))
        # Cascade delete children via FK ON DELETE CASCADE; also do it
        # explicitly for robustness on legacy DBs without FK enforcement.
        conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT id FROM folders WHERE parent_id = ?
                UNION ALL
                SELECT f.id FROM folders f JOIN descendants d ON f.parent_id = d.id
            )
            UPDATE papers SET folder_id = NULL WHERE folder_id IN (SELECT id FROM descendants)
            """,
            (folder_id,),
        )
        conn.execute("DELETE FROM folders WHERE id = ?", (folder_id,))
    logger.info("folder_deleted id=%s", folder_id)
    return True


def move_folder(folder_id: str, new_parent_id: str | None) -> bool:
    """Move a folder (and its descendants) under a new parent.

    When moving, all descendant folders' levels are recalculated relative to
    the new parent. Papers stay attached to their respective folders.

    Raises ValueError if the move would violate MAX_FOLDER_LEVEL or if the
    target is a descendant of the moved folder (cycle prevention).
    """
    with session() as conn:
        folder = conn.execute("SELECT * FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if folder is None:
            raise ValueError("目标文件夹不存在")

        if new_parent_id is None:
            new_level = 1
        else:
            parent = conn.execute("SELECT * FROM folders WHERE id = ?", (new_parent_id,)).fetchone()
            if parent is None:
                raise ValueError("目标父文件夹不存在")
            # Prevent moving into a descendant (cycle check)
            descendants = _collect_descendant_ids(folder_id)
            if new_parent_id in descendants:
                raise ValueError("不能将文件夹移动到其子文件夹中")
            new_level = parent["level"] + 1

        if new_level > MAX_FOLDER_LEVEL:
            raise ValueError(f"文件夹最多支持 {MAX_FOLDER_LEVEL} 级，无法移动到目标位置")

        old_level = folder["level"]
        level_delta = new_level - old_level

        # Update the moved folder itself
        conn.execute(
            "UPDATE folders SET parent_id = ?, level = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (new_parent_id, new_level, folder_id),
        )

        # Recursively update all descendants' levels
        if level_delta != 0:
            conn.execute(
                """
                WITH RECURSIVE descendants(id) AS (
                    SELECT id FROM folders WHERE parent_id = ?
                    UNION ALL
                    SELECT f.id FROM folders f JOIN descendants d ON f.parent_id = d.id
                )
                UPDATE folders SET level = level + ? WHERE id IN (SELECT id FROM descendants)
                """,
                (folder_id, level_delta),
            )

    logger.info("folder_moved id=%s new_parent=%s new_level=%s", folder_id, new_parent_id, new_level)
    return True


def list_folders() -> list[FolderResponse]:
    """Flat list of all folders ordered by created_at."""
    counts = _get_paper_counts()
    with session() as conn:
        rows = conn.execute("SELECT * FROM folders ORDER BY created_at ASC").fetchall()
    return [_row_to_response(r, counts.get(r["id"], 0)) for r in rows]


def get_folder_tree() -> list[FolderTreeNode]:
    """Build the full folder tree (up to 3 levels) with paper counts."""
    counts = _get_paper_counts()
    with session() as conn:
        rows = conn.execute("SELECT * FROM folders ORDER BY created_at ASC").fetchall()

    # Build a lookup: id -> node
    nodes: dict[str, FolderTreeNode] = {}
    for r in rows:
        nodes[r["id"]] = FolderTreeNode(
            id=r["id"],
            name=r["name"],
            parent_id=r["parent_id"],
            level=r["level"],
            paper_count=counts.get(r["id"], 0),
            created_at=to_utc_isoformat(r["created_at"]),
            updated_at=to_utc_isoformat(r["updated_at"]),
            children=[],
        )

    roots: list[FolderTreeNode] = []
    for node in nodes.values():
        if node.parent_id and node.parent_id in nodes:
            nodes[node.parent_id].children.append(node)
        else:
            roots.append(node)
    return roots


def _collect_descendant_ids(folder_id: str) -> list[str]:
    """Recursively collect a folder and all its descendants' IDs."""
    with session() as conn:
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id) AS (
                SELECT id FROM folders WHERE id = ?
                UNION ALL
                SELECT f.id FROM folders f JOIN descendants d ON f.parent_id = d.id
            )
            SELECT id FROM descendants
            """,
            (folder_id,),
        ).fetchall()
    return [r["id"] for r in rows]


def _extract_file_type(file_name: str | None) -> str:
    """Extract a human-readable file type label from an attachment filename."""
    if not file_name:
        return "other"
    from pathlib import Path
    ext = Path(file_name).suffix.lower().lstrip(".")
    mapping = {"pdf": "PDF", "doc": "Word", "docx": "Word", "txt": "Text", "md": "Markdown"}
    return mapping.get(ext, ext.upper() if ext else "other")


def _fetch_paper_file_types(conn, paper_ids: list[str]) -> dict[str, str]:
    """Return a mapping from paper_id to file_type based on original attachments."""
    if not paper_ids:
        return {}
    placeholders = ",".join("?" * len(paper_ids))
    rows = conn.execute(
        f"SELECT paper_id, file_name FROM attachments WHERE paper_id IN ({placeholders}) AND attachment_type = 'original'",
        paper_ids,
    ).fetchall()
    result: dict[str, str] = {}
    for r in rows:
        pid = r["paper_id"]
        if pid not in result:
            result[pid] = _extract_file_type(r["file_name"])
    return result


def get_folder_papers(folder_id: str) -> list[dict]:
    """List papers directly in a folder (does not recurse into subfolders)."""
    with session() as conn:
        rows = conn.execute(
            "SELECT id, title, title_cn, title_en, authors, status, folder_id, created_at FROM papers WHERE folder_id = ? ORDER BY created_at DESC",
            (folder_id,),
        ).fetchall()
        paper_ids = [r["id"] for r in rows]
        file_types = _fetch_paper_file_types(conn, paper_ids)
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
            "file_type": file_types.get(r["id"], "other"),
        }
        for r in rows
    ]


def get_unassigned_papers() -> list[dict]:
    """List papers not assigned to any folder (folder_id IS NULL)."""
    with session() as conn:
        rows = conn.execute(
            "SELECT id, title, title_cn, title_en, authors, status, folder_id, created_at "
            "FROM papers WHERE folder_id IS NULL ORDER BY created_at DESC"
        ).fetchall()
        paper_ids = [r["id"] for r in rows]
        file_types = _fetch_paper_file_types(conn, paper_ids)
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
            "file_type": file_types.get(r["id"], "other"),
        }
        for r in rows
    ]


def move_paper_to_folder(paper_id: str, folder_id: str | None) -> bool:
    """Assign a paper to a folder. Pass folder_id=None to remove from folder."""
    from app.db import session as db_session

    with session() as conn:
        paper = conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if paper is None:
            return False
        if folder_id is not None:
            folder = conn.execute("SELECT id FROM folders WHERE id = ?", (folder_id,)).fetchone()
            if folder is None:
                raise ValueError("目标文件夹不存在")
        conn.execute(
            "UPDATE papers SET folder_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (folder_id, paper_id),
        )
    logger.info("paper_moved paper_id=%s folder_id=%s", paper_id, folder_id)
    return True


def batch_move_papers(folder_id: str, paper_ids: list[str]) -> dict:
    """Batch-assign existing papers to a folder.

    Each paper in *paper_ids* is moved to *folder_id*. Papers that don't
    exist are skipped and reported as failed. Returns an aggregate result
    dict with per-paper status.

    Args:
        folder_id: Target folder ID.
        paper_ids: List of paper IDs to move.

    Returns:
        Dict: {folder_id, total, success_count, failed_count, results}
    """
    results: list[dict] = []
    with session() as conn:
        folder = conn.execute("SELECT id FROM folders WHERE id = ?", (folder_id,)).fetchone()
        if folder is None:
            raise ValueError("目标文件夹不存在")
        for pid in paper_ids:
            entry: dict = {"paper_id": pid, "success": False, "error": ""}
            paper = conn.execute("SELECT id FROM papers WHERE id = ?", (pid,)).fetchone()
            if paper is None:
                entry["error"] = "论文不存在"
                results.append(entry)
                continue
            conn.execute(
                "UPDATE papers SET folder_id = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (folder_id, pid),
            )
            entry["success"] = True
            results.append(entry)
    success_count = sum(1 for r in results if r["success"])
    failed_count = len(results) - success_count
    logger.info(
        "batch_move_papers folder_id=%s total=%s success=%s failed=%s",
        folder_id, len(results), success_count, failed_count,
    )
    return {
        "folder_id": folder_id,
        "total": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }


def batch_remove_papers_from_folder(paper_ids: list[str]) -> dict:
    """Batch-remove papers from any folder (set folder_id to NULL).

    Non-existent paper IDs are reported as failures. Returns aggregate
    result dict with per-paper status.
    """
    results: list[dict] = []
    with session() as conn:
        for pid in paper_ids:
            entry: dict = {"paper_id": pid, "success": False, "error": ""}
            paper = conn.execute("SELECT id FROM papers WHERE id = ?", (pid,)).fetchone()
            if paper is None:
                entry["error"] = "论文不存在"
                results.append(entry)
                continue
            conn.execute(
                "UPDATE papers SET folder_id = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (pid,),
            )
            entry["success"] = True
            results.append(entry)
    success_count = sum(1 for r in results if r["success"])
    failed_count = len(results) - success_count
    logger.info(
        "batch_remove_papers total=%s success=%s failed=%s",
        len(results), success_count, failed_count,
    )
    return {
        "total": len(results),
        "success_count": success_count,
        "failed_count": failed_count,
        "results": results,
    }


def batch_import_files(folder_id: str | None, files: list[tuple[str, Path]]) -> list[dict]:
    """按「已落盘文件」批量导入，逐个处理，避免把全部文件读进内存。

    Each file is stored as the 'original' attachment and triggers background
    analysis (same as the single-upload flow). Returns a per-file result list.

    Args:
        folder_id: Target folder ID, or None for no folder.
        files: List of (filename, source_path) tuples; the source files are
            deleted after their background analysis finishes (or on failure).

    Returns:
        List of dicts: {filename, paper_id, success, error}
    """
    from app.services import create_paper, ensure_analysis_placeholder, run_analysis_exclusive, upsert_attachment_file
    from app.models import PaperCreate
    from threading import Thread

    results: list[dict] = []

    for filename, src_path in files:
        result: dict = {"filename": filename, "paper_id": "", "success": False, "error": ""}
        try:
            if not filename.lower().endswith(".pdf"):
                raise ValueError("仅支持 PDF 文件")
            if not src_path.exists() or src_path.stat().st_size == 0:
                raise ValueError("文件为空")

            paper_title = Path(filename).stem
            paper_id = create_paper(PaperCreate(title=paper_title, status="uploaded"))

            # Assign to folder
            if folder_id:
                with session() as conn:
                    conn.execute(
                        "UPDATE papers SET folder_id = ? WHERE id = ?",
                        (folder_id, paper_id),
                    )

            ensure_analysis_placeholder(paper_id)
            upsert_attachment_file(paper_id, "original", str(src_path), filename)

            # Background analysis
            def worker(pid: str, path: str) -> None:
                try:
                    run_analysis_exclusive(pid, path)
                except Exception:
                    logger.exception("batch_import background analysis failed paper_id=%s", pid)
                finally:
                    Path(path).unlink(missing_ok=True)

            Thread(target=worker, args=(paper_id, str(src_path)), daemon=True).start()

            result["paper_id"] = paper_id
            result["success"] = True
        except Exception as exc:
            result["error"] = str(exc)[:200]
            # 失败时立即清理临时文件，避免堆积
            Path(src_path).unlink(missing_ok=True)
            logger.warning("batch_import_file_failed filename=%s error=%s", filename, exc)
        results.append(result)

    logger.info(
        "batch_import_done folder_id=%s total=%s success=%s failed=%s",
        folder_id, len(results),
        sum(1 for r in results if r["success"]),
        sum(1 for r in results if not r["success"]),
    )
    return results


def batch_import_papers(folder_id: str | None, files: list[tuple[str, bytes]]) -> list[dict]:
    """兼容入口：把内存中的字节写入临时文件后委托给 ``batch_import_files``。

    新代码（上传接口）应直接使用 ``batch_import_files``，以免把全部文件
    同时读进内存。
    """
    temp_dir = Path(tempfile.gettempdir()) / "paperreading"
    temp_dir.mkdir(parents=True, exist_ok=True)
    staged: list[tuple[str, Path]] = []
    for filename, file_bytes in files:
        temp_path = temp_dir / f"{uuid4().hex}_{filename}"
        temp_path.write_bytes(file_bytes)
        staged.append((filename, temp_path))
    return batch_import_files(folder_id, staged)
