"""Backup & restore service for PaperReading.

Provides two backup modes and one restore mode:

- Full backup:  packs the entire workspace directory (DB, API config, paper
  storage, task logs, debug logs) into a single ZIP with a manifest so the
  system can be fully restored on another machine.
- Papers-only export: rebuilds the user's folder tree on disk and copies
  each paper's original PDF renamed to "<title> - <authors>.pdf" inside its
  folder. Useful for handing off a tidy library.
- Restore: validates an uploaded backup ZIP by its manifest, then replaces
  the workspace contents in place so the running app picks up the new data
  after the API call returns.

The implementation deliberately stays synchronous and uses Python's stdlib
``zipfile`` / ``shutil`` to avoid new third-party dependencies. SQLite file
copies are safe because the demo opens a fresh connection per request via
``app.db.session`` and never holds a long-lived file handle.
"""

from __future__ import annotations

import io
import json
import logging
import re
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from app.core.config import settings
from app.core.storage import resolve_attachment_path
from app.db import session
from app.services.api_config import CONFIG_FILE

logger = logging.getLogger(__name__)

# Directories under the workspace that constitute a full backup.
FULL_BACKUP_SUBDIRS = ("storage", "task_logs", "debug_logs")

# Files that live outside the workspace subdirs and need explicit (source, name)
# pairs. The api_config.json is intentionally read from CONFIG_FILE because the
# api_config service hardcodes that location; restoring must write it back to
# the same path so the running app picks up the new config.
FULL_BACKUP_FILES: list[tuple[Path, str]] = [
    (settings.db_path, "paperreading.db"),
    (CONFIG_FILE, "api_config.json"),
]

BACKUP_MANIFEST_NAME = "backup_manifest.json"
BACKUP_MANIFEST_VERSION = 1
UNCATEGURED_FOLDER_NAME = "未分类文献"

# Characters that are illegal or risky in filenames across operating systems.
_INVALID_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _sanitize_filename_segment(value: str, fallback: str) -> str:
    """Strip illegal filename characters and collapse whitespace.

    Returns *fallback* when the cleaned string is empty so the resulting
    filename is always non-empty.
    """
    cleaned = _INVALID_FILENAME_CHARS.sub(" ", value or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    cleaned = cleaned.replace("/", "-").replace("\\", "-")
    return cleaned if cleaned else fallback


def _build_paper_display_name(title: str, title_cn: str, title_en: str, authors: str, paper_id: str) -> str:
    """Choose a human-friendly filename body: <title> - <authors>."""
    title = (title or "").strip()
    if not title:
        title = (title_cn or "").strip() or (title_en or "").strip() or f"未命名文献_{paper_id[:8]}"
    authors = (authors or "").strip()
    if authors:
        return f"{_sanitize_filename_segment(title, 'untitled')} - {_sanitize_filename_segment(authors, 'unknown')}"
    return _sanitize_filename_segment(title, "untitled")


def _format_size(size_bytes: int) -> str:
    if size_bytes < 1024:
        return f"{size_bytes} B"
    units = ["KB", "MB", "GB", "TB"]
    size = float(size_bytes) / 1024.0
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            return f"{size:.2f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


def _add_dir_to_zip(zip_writer: zipfile.ZipFile, root: Path, archive_base: str) -> int:
    """Recursively add every file under *root* into the ZIP under *archive_base*.

    Returns the number of files added.
    """
    if not root.exists():
        return 0
    count = 0
    for entry in root.rglob("*"):
        if entry.is_file():
            arcname = f"{archive_base}/{entry.relative_to(root).as_posix()}"
            zip_writer.write(entry, arcname)
            count += 1
    return count


def build_full_backup() -> tuple[bytes, str, dict]:
    """Build a complete workspace backup as an in-memory ZIP.

    Returns ``(zip_bytes, suggested_filename, manifest)``.
    """
    workspace = settings.workspace_dir
    manifest = {
        "version": BACKUP_MANIFEST_VERSION,
        "type": "full",
        "created_at": _utc_now_iso(),
        "workspace_path": str(workspace),
        "includes": ["database", "api_config", "paper_storage", "task_logs", "debug_logs"],
        "files": [],
    }

    buffer = io.BytesIO()
    total_files = 0
    total_bytes = 0

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for source_path, archive_name in FULL_BACKUP_FILES:
            if source_path.exists() and source_path.is_file():
                zf.write(source_path, archive_name)
                size = source_path.stat().st_size
                total_files += 1
                total_bytes += size
                manifest["files"].append({"name": archive_name, "size_bytes": size})
        for subdir in FULL_BACKUP_SUBDIRS:
            dir_path = workspace / subdir
            added = _add_dir_to_zip(zf, dir_path, subdir)
            total_files += added
            manifest["files"].append({"name": subdir, "type": "directory", "file_count": added})

        manifest["total_files"] = total_files
        manifest["total_size_bytes"] = total_bytes
        manifest["total_size_display"] = _format_size(total_bytes)
        zf.writestr(BACKUP_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

    data = buffer.getvalue()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"paperreading_full_backup_{ts}.zip"
    logger.info("full_backup_built files=%s size=%s", total_files, _format_size(len(data)))
    return data, filename, manifest


def _fetch_papers_with_folders() -> list[dict]:
    """Return one row per paper joined with its folder id and name."""
    with session() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.title_cn, p.title_en, p.authors,
                   p.folder_id, f.name AS folder_name
            FROM papers p
            LEFT JOIN folders f ON f.id = p.folder_id
            ORDER BY p.created_at ASC
            """,
        ).fetchall()
    return [dict(r) for r in rows]


def _fetch_folder_tree() -> dict[str | None, list[dict]]:
    """Return a map of parent_id -> list of child folder rows."""
    with session() as conn:
        rows = conn.execute(
            "SELECT id, name, parent_id, level FROM folders ORDER BY name ASC"
        ).fetchall()
    tree: dict[str | None, list[dict]] = {}
    for r in rows:
        tree.setdefault(r["parent_id"], []).append(dict(r))
    return tree


def _build_folder_path_chain(folder_id: str, folders_by_id: dict[str, dict]) -> list[str]:
    """Walk from *folder_id* up to the root, returning [root, ..., leaf] names."""
    chain: list[str] = []
    current = folders_by_id.get(folder_id)
    guard = 0
    while current and guard < 16:
        chain.append(_sanitize_filename_segment(current["name"], f"folder_{current['id'][:8]}"))
        parent_id = current["parent_id"]
        if not parent_id:
            break
        current = folders_by_id.get(parent_id)
        guard += 1
    chain.reverse()
    return chain


def _original_pdf_path(paper_id: str) -> Path | None:
    """Return the path to a paper's original PDF if it exists."""
    pdf_path = settings.workspace_dir / "storage" / paper_id / "original.pdf"
    return pdf_path if pdf_path.exists() and pdf_path.is_file() else None


def build_papers_export() -> tuple[bytes, str, dict]:
    """Build a folder-structured ZIP of original PDFs renamed <title> - <authors>.

    Papers without an assigned folder go into a top-level "未分类文献" directory.
    Papers whose original PDF is missing are listed in the manifest under
    ``skipped_papers`` so the user knows the export is incomplete.
    """
    papers = _fetch_papers_with_folders()
    folders_by_parent = _fetch_folder_tree()
    folders_by_id: dict[str, dict] = {}
    for children in folders_by_parent.values():
        for f in children:
            folders_by_id[f["id"]] = f

    # Track used filenames per directory to avoid silent overwrites when two
    # papers share the same <title> - <authors> string.
    used_names_per_dir: dict[str, set[str]] = {}
    skipped: list[dict] = []
    exported = 0
    total_bytes = 0

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for paper in papers:
            pdf_path = _original_pdf_path(paper["id"])
            if pdf_path is None:
                skipped.append({
                    "paper_id": paper["id"],
                    "title": paper["title"] or paper["title_cn"] or paper["title_en"],
                    "reason": "original_pdf_missing",
                })
                continue

            if paper["folder_id"]:
                chain = _build_folder_path_chain(paper["folder_id"], folders_by_id)
            else:
                chain = [UNCATEGURED_FOLDER_NAME]
            dir_key = "/".join(chain)
            used = used_names_per_dir.setdefault(dir_key, set())

            base_name = _build_paper_display_name(
                paper["title"], paper["title_cn"], paper["title_en"], paper["authors"], paper["id"]
            )
            candidate = f"{base_name}.pdf"
            counter = 2
            while candidate in used:
                candidate = f"{base_name} ({counter}).pdf"
                counter += 1
            used.add(candidate)

            arcname = f"{dir_key}/{candidate}"
            zf.write(pdf_path, arcname)
            exported += 1
            total_bytes += pdf_path.stat().st_size

        manifest = {
            "version": BACKUP_MANIFEST_VERSION,
            "type": "papers_export",
            "created_at": _utc_now_iso(),
            "exported_count": exported,
            "skipped_count": len(skipped),
            "skipped_papers": skipped,
            "total_size_bytes": total_bytes,
            "total_size_display": _format_size(total_bytes),
        }
        zf.writestr(BACKUP_MANIFEST_NAME, json.dumps(manifest, ensure_ascii=False, indent=2))

    data = buffer.getvalue()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"paperreading_papers_export_{ts}.zip"
    logger.info("papers_export_built exported=%s skipped=%s size=%s", exported, len(skipped), _format_size(len(data)))
    return data, filename, manifest


def _read_manifest_from_zip(zip_bytes: bytes) -> dict | None:
    """Return the parsed backup manifest from a ZIP, or ``None`` if missing/invalid."""
    try:
        with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
            if BACKUP_MANIFEST_NAME not in zf.namelist():
                return None
            with zf.open(BACKUP_MANIFEST_NAME) as f:
                return json.loads(f.read().decode("utf-8"))
    except (zipfile.BadZipFile, json.JSONDecodeError, OSError) as exc:
        logger.warning("backup_manifest_read_failed error=%s", exc)
        return None


def _iter_zip_members(zip_bytes: bytes) -> Iterable[tuple[str, bytes]]:
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            with zf.open(info) as f:
                yield info.filename, f.read()


def _resolve_restore_target(arcname: str) -> Path | None:
    """Map a backup archive entry name to its real restore target on disk.

    ``api_config.json`` is restored to ``CONFIG_FILE`` (the hardcoded location
    expected by the api_config service), while every other entry is restored
    inside ``settings.workspace_dir``.

    Returns ``None`` for unsafe (path traversal) entries.
    """
    normalized = Path(arcname).as_posix()
    if normalized.startswith("/") or ".." in normalized.split("/"):
        return None

    if normalized == "api_config.json":
        return CONFIG_FILE
    return settings.workspace_dir / normalized


def _reanchor_attachment_paths() -> int:
    """Rewrite stale absolute attachment paths after a restore.

    A backup made on another machine stores absolute file paths under that
    machine's workspace. After a restore the files land in the current
    workspace's canonical layout, so re-point rows whose stored path no
    longer resolves to an existing file.
    """
    updated = 0
    with session() as conn:
        rows = conn.execute("SELECT id, paper_id, file_path FROM attachments").fetchall()
        for row in rows:
            resolved = resolve_attachment_path(row["paper_id"], row["file_path"])
            if resolved.exists() and str(resolved) != row["file_path"]:
                conn.execute(
                    "UPDATE attachments SET file_path = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (str(resolved), row["id"]),
                )
                updated += 1
    if updated:
        logger.info("restore_reanchored_attachment_paths count=%s", updated)
    return updated


def _reanchor_markdown_paths() -> int:
    """Rewrite stale MinerU markdown paths inside paper_texts.sections_json.

    sections_json embeds absolute paths (top-level ``markdown_path`` and the
    nested MinerU result). Re-root any stale ``/Users/.../workspace/`` prefix
    onto the current workspace when the referenced file exists there.
    """
    current_prefix = str(settings.workspace_dir) + "/"
    stale_prefix_re = re.compile(r"/Users/[^\"\\\s]*/workspace/")
    updated = 0
    with session() as conn:
        rows = conn.execute(
            "SELECT id, sections_json FROM paper_texts WHERE sections_json LIKE '%markdown_path%'"
        ).fetchall()
        for row in rows:
            raw = row["sections_json"] or ""
            prefixes = set(stale_prefix_re.findall(raw))
            rewritten = raw
            changed = False
            for prefix in prefixes:
                if prefix == current_prefix:
                    continue
                candidate = rewritten.replace(prefix, current_prefix)
                try:
                    payload = json.loads(candidate)
                except json.JSONDecodeError:
                    continue
                md_path = str(payload.get("markdown_path", "") or "")
                if md_path and Path(md_path).exists():
                    rewritten = candidate
                    changed = True
            if changed:
                conn.execute(
                    "UPDATE paper_texts SET sections_json = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                    (rewritten, row["id"]),
                )
                updated += 1
    if updated:
        logger.info("restore_reanchored_markdown_paths count=%s", updated)
    return updated


def restore_full_backup(zip_bytes: bytes) -> dict:
    """Validate and apply a full backup ZIP to the current workspace.

    Strategy:
    1. Validate the manifest declares ``type == "full"``.
    2. Stream members into the workspace; existing files are overwritten.
    3. Reset the in-memory settings cache so subsequent requests reload config.

    Returns a summary dict with counts of files restored.
    """
    manifest = _read_manifest_from_zip(zip_bytes)
    if not manifest:
        raise ValueError("无效的备份文件：缺少 backup_manifest.json 或文件已损坏")
    if manifest.get("type") != "full":
        raise ValueError("该备份不是全量备份，无法用于恢复（仅全量备份支持恢复）")

    workspace = settings.workspace_dir
    workspace.mkdir(parents=True, exist_ok=True)
    for subdir in FULL_BACKUP_SUBDIRS:
        (workspace / subdir).mkdir(parents=True, exist_ok=True)
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)

    restored_files = 0
    restored_bytes = 0
    for arcname, data in _iter_zip_members(zip_bytes):
        if arcname == BACKUP_MANIFEST_NAME:
            continue
        target = _resolve_restore_target(arcname)
        if target is None:
            logger.warning("restore_skip_unsafe_path path=%s", arcname)
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        restored_files += 1
        restored_bytes += len(data)

    # Force reload of cached API config on the next request.
    try:
        settings.reset()
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("settings_reset_after_restore_failed error=%s", exc)

    # The restored DB may carry absolute paths from the backup's original
    # machine; re-point them at the current workspace where possible.
    reanchored = _reanchor_attachment_paths()
    reanchored += _reanchor_markdown_paths()

    summary = {
        "restored_files": restored_files,
        "restored_size_bytes": restored_bytes,
        "restored_size_display": _format_size(restored_bytes),
        "reanchored_attachment_paths": reanchored,
        "backup_created_at": manifest.get("created_at", ""),
        "workspace_path": str(workspace),
    }
    logger.info(
        "full_restore_done files=%s size=%s backup_created_at=%s",
        restored_files, summary["restored_size_display"], summary["backup_created_at"],
    )
    return summary


def estimate_backup_sizes() -> dict:
    """Provide rough size estimates for the two backup modes."""
    workspace = settings.workspace_dir

    def _dir_size(path: Path) -> int:
        if not path.exists():
            return 0
        return sum(f.stat().st_size for f in path.rglob("*") if f.is_file())

    def _file_size(path: Path) -> int:
        return path.stat().st_size if path.exists() and path.is_file() else 0

    db_size = _file_size(settings.db_path)
    config_size = _file_size(CONFIG_FILE)
    storage_size = _dir_size(workspace / "storage")
    task_logs_size = _dir_size(workspace / "task_logs")
    debug_logs_size = _dir_size(workspace / "debug_logs")
    full_total = db_size + config_size + storage_size + task_logs_size + debug_logs_size

    # Papers-only export: only original.pdf per paper.
    papers_only_size = 0
    papers_count = 0
    storage_dir = workspace / "storage"
    if storage_dir.exists():
        for paper_dir in storage_dir.iterdir():
            if not paper_dir.is_dir():
                continue
            original = paper_dir / "original.pdf"
            if original.exists() and original.is_file():
                papers_only_size += original.stat().st_size
                papers_count += 1

    return {
        "full": {
            "size_bytes": full_total,
            "size_display": _format_size(full_total),
        },
        "papers_export": {
            "size_bytes": papers_only_size,
            "size_display": _format_size(papers_only_size),
            "paper_count": papers_count,
        },
    }
