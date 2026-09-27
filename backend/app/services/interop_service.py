"""文献互通服务：PaperPilot ↔ 通用文献格式（CSL JSON / BibTeX / RIS）。

职责：
- 导出：从数据库收集论文（含元数据、标签、文件夹），转成中间表示
- 导入：把中间表示落库为论文，并处理标签、文件夹与去重

格式解析/序列化的细节见 ``app.core.ref_formats``。
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from app.core import ref_formats as rf
from app.db import session
from app.models import FolderCreate, PaperCreate, PaperUpdate, TagCreate
from app.services.folder_service import create_folder
from app.services.paper_service import PAPER_STATUS_IMPORTED, create_paper, update_paper
from app.services.tag_service import create_tag, set_paper_tags

logger = logging.getLogger(__name__)

# 导入的论文只有题录、没有 PDF，因此不能置为 'uploaded'：
# 前端会把 'uploaded' 当作「正在分析」，导致进度条永远停在 0%。
_IMPORT_STATUS = PAPER_STATUS_IMPORTED


# ============================================================================
# 导出
# ============================================================================

def _norm_title(text: str) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", "", (text or "").lower())


def _load_metadata(conn) -> dict[str, dict[str, Any]]:
    """读取所有论文的 metadata 作用域内容（keywords / doi / year / source）。"""
    result: dict[str, dict[str, Any]] = {}
    rows = conn.execute(
        "SELECT paper_id, sections_json FROM paper_texts WHERE text_scope = 'metadata'"
    ).fetchall()
    for row in rows:
        try:
            sections = json.loads(row["sections_json"] or "{}")
        except (json.JSONDecodeError, TypeError):
            sections = {}
        meta = sections.get("metadata", {}) if isinstance(sections, dict) else {}
        result[row["paper_id"]] = meta if isinstance(meta, dict) else {}
    return result


def _load_tags(conn) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    rows = conn.execute(
        "SELECT pt.paper_id, t.name FROM paper_tags pt JOIN tags t ON t.id = pt.tag_id ORDER BY t.name"
    ).fetchall()
    for row in rows:
        result.setdefault(row["paper_id"], []).append(row["name"])
    return result


def _load_folder_names(conn) -> dict[str, str]:
    return {row["id"]: row["name"] for row in conn.execute("SELECT id, name FROM folders").fetchall()}


def export_records(paper_ids: list[str] | None = None) -> list[dict[str, Any]]:
    """收集论文并转为中间表示列表。"""
    with session() as conn:
        if paper_ids:
            placeholders = ", ".join("?" for _ in paper_ids)
            rows = conn.execute(
                f"SELECT * FROM papers WHERE id IN ({placeholders}) ORDER BY created_at DESC",
                paper_ids,
            ).fetchall()
        else:
            rows = conn.execute("SELECT * FROM papers ORDER BY created_at DESC").fetchall()
        metadata_map = _load_metadata(conn)
        tags_map = _load_tags(conn)
        folder_names = _load_folder_names(conn)

    records: list[dict[str, Any]] = []
    for row in rows:
        meta = metadata_map.get(row["id"], {})
        title = row["title"] or row["title_en"] or row["title_cn"] or ""
        keywords = [k.strip() for k in re.split(r"[,;]", str(meta.get("keywords") or "")) if k.strip()]
        # 标签与关键词合并去重，统一走 keywords 字段
        for tag in tags_map.get(row["id"], []):
            if tag not in keywords:
                keywords.append(tag)

        authors = [a.strip() for a in str(row["authors"] or "").split(";") if a.strip()]
        folder_id = row["folder_id"] if "folder_id" in row.keys() else None

        records.append(rf.normalize_record({
            "title": title,
            "title_cn": row["title_cn"] or "",
            "title_en": row["title_en"] or "",
            "authors": authors,
            "year": rf._extract_year(str(row["publish_date"] or "")),
            "date": row["publish_date"] or "",
            "container": str(meta.get("source") or ""),
            "doi": str(meta.get("doi") or ""),
            "url": row["source_url"] or "",
            "abstract": row["abstract"] or "",
            "keywords": keywords,
            "status": row["status"] or "",
            "folder": folder_names.get(folder_id, "") if folder_id else "",
        }))
    return records


def export_text(fmt: str, paper_ids: list[str] | None = None) -> str:
    """按格式导出为文本。"""
    return rf.serialize_any(export_records(paper_ids), fmt)


# ============================================================================
# 导入
# ============================================================================

def _existing_index() -> tuple[dict[str, str], dict[str, str]]:
    """返回 (标题索引, DOI 索引)，用于导入去重。"""
    with session() as conn:
        titles: dict[str, str] = {}
        for row in conn.execute("SELECT id, title, title_cn, title_en FROM papers"):
            for value in (row["title"], row["title_cn"], row["title_en"]):
                key = _norm_title(value)
                if key:
                    titles.setdefault(key, row["id"])
        dois: dict[str, str] = {}
        for row in conn.execute("SELECT paper_id, sections_json FROM paper_texts WHERE text_scope = 'metadata'"):
            try:
                meta = json.loads(row["sections_json"] or "{}").get("metadata", {})
            except (json.JSONDecodeError, TypeError):
                meta = {}
            doi = str((meta or {}).get("doi") or "").strip().lower()
            if doi:
                dois.setdefault(doi, row["paper_id"])
    return titles, dois


def _find_or_create_folder(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return None
    with session() as conn:
        row = conn.execute(
            "SELECT id FROM folders WHERE name = ? AND parent_id IS NULL", (name,)
        ).fetchone()
        if row is not None:
            return row["id"]
    try:
        return create_folder(FolderCreate(name=name)).id
    except Exception:
        logger.warning("import: 创建文件夹失败 name=%s", name, exc_info=True)
        return None


def _find_or_create_tag(name: str) -> str | None:
    name = (name or "").strip()
    if not name:
        return None
    try:
        return create_tag(TagCreate(name=name)).id
    except Exception:
        logger.warning("import: 创建标签失败 name=%s", name, exc_info=True)
        return None


def _assign_folder(paper_id: str, folder_id: str) -> None:
    with session() as conn:
        conn.execute("UPDATE papers SET folder_id = ? WHERE id = ?", (folder_id, paper_id))


def import_records(
    records: list[dict[str, Any]],
    default_folder_id: str | None = None,
    skip_duplicates: bool = True,
) -> dict[str, Any]:
    """把中间表示落库为论文。

    Args:
        records: 中间表示列表。
        default_folder_id: 未指定文件夹时使用的目标文件夹。
        skip_duplicates: 是否按 DOI / 标题跳过已存在的论文。

    Returns:
        {imported, skipped, failed, items:[{title, paper_id, status, error}]}
    """
    titles, dois = _existing_index() if skip_duplicates else ({}, {})
    seen_titles: set[str] = set()
    seen_dois: set[str] = set()

    items: list[dict[str, Any]] = []
    imported = skipped = failed = 0

    for rec in records:
        rec = rf.normalize_record(rec)
        title = rec["title"] or rec["title_en"] or rec["title_cn"]
        if not title:
            failed += 1
            items.append({"title": "", "paper_id": "", "status": "failed", "error": "缺少标题"})
            continue

        doi = (rec["doi"] or "").strip().lower()
        title_key = _norm_title(title)

        if skip_duplicates:
            duplicate = (doi and (doi in dois or doi in seen_dois)) or (
                title_key and (title_key in titles or title_key in seen_titles)
            )
            if duplicate:
                skipped += 1
                items.append({"title": title, "paper_id": "", "status": "skipped", "error": "已存在"})
                continue

        try:
            authors = "; ".join(rec["authors"])
            paper_id = create_paper(PaperCreate(
                title=title,
                title_cn=rec["title_cn"],
                # 不猜测 title_en：留空即可，get_paper 会按语言检测正确回填，
                # 否则中文标题会被错误写入 title_en。
                title_en=rec["title_en"],
                authors=authors,
                publish_date=rec["date"] or rec["year"],
                abstract=rec["abstract"],
                source_url=rec["url"],
                status=_IMPORT_STATUS,
            ))

            # 元数据（doi / keywords / year / 期刊名）写入 metadata 作用域
            update_paper(paper_id, PaperUpdate(
                doi=rec["doi"] or None,
                keywords=", ".join(rec["keywords"]) if rec["keywords"] else None,
                year=rec["year"] or None,
                source=rec["container"] or None,
            ))

            # 标签
            tag_ids = [tid for tid in (_find_or_create_tag(k) for k in rec["keywords"]) if tid]
            if tag_ids:
                set_paper_tags(paper_id, tag_ids)

            # 文件夹：记录自带 > 默认
            folder_id = _find_or_create_folder(rec["folder"]) or default_folder_id
            if folder_id:
                _assign_folder(paper_id, folder_id)

            if title_key:
                seen_titles.add(title_key)
            if doi:
                seen_dois.add(doi)

            imported += 1
            items.append({"title": title, "paper_id": paper_id, "status": "imported", "error": ""})
        except Exception as exc:  # noqa: BLE001 - 单条失败不应中断整体导入
            failed += 1
            logger.warning("import: 导入失败 title=%s error=%s", title, exc, exc_info=True)
            items.append({"title": title, "paper_id": "", "status": "failed", "error": str(exc)[:200]})

    return {"imported": imported, "skipped": skipped, "failed": failed, "total": len(records), "items": items}


def import_text(
    text: str,
    fmt: str | None = None,
    filename: str = "",
    default_folder_id: str | None = None,
    skip_duplicates: bool = True,
) -> dict[str, Any]:
    """解析文本并导入。"""
    records = rf.parse_any(text, fmt, filename)
    result = import_records(records, default_folder_id=default_folder_id, skip_duplicates=skip_duplicates)
    result["format"] = fmt if fmt in rf.FORMATS else rf.detect_format(filename, text)
    return result
