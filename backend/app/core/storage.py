"""Filesystem helpers for storing paper attachments.

The demo keeps files inside a fixed workspace directory so local data remains
portable and easy to back up.
"""

from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from app.core.config import settings

ATTACHMENT_FILENAMES = {
    "original": "original.pdf",
    "translated": "translated.pdf",
    "mapped": "mapped.pdf",
}


def paper_storage_dir(paper_id: str) -> Path:
    path = settings.workspace_dir / "storage" / paper_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def paper_storage_relative_dir(paper_id: str) -> str:
    return f"storage/{paper_id}"


def resolve_attachment_path(paper_id: str, stored_path: str) -> Path:
    """Resolve an attachment's stored path, tolerating restores on a new machine.

    The DB keeps absolute file paths, so a full backup restored to a different
    workspace location (or machine) carries stale paths. Files always land in
    the canonical ``workspace/storage/<paper_id>/`` layout, so fall back to the
    same file name there when the stored path no longer exists.
    """
    path = Path(stored_path)
    if path.exists():
        return path
    return settings.workspace_dir / "storage" / paper_id / path.name


def store_attachment_file(paper_id: str, attachment_type: str, source_path: str) -> tuple[Path, int]:
    src = Path(source_path)
    if not src.exists():
        raise FileNotFoundError(f"Source file not found: {source_path}")

    target_dir = paper_storage_dir(paper_id)
    target_name = ATTACHMENT_FILENAMES.get(attachment_type, f"{attachment_type}-{uuid4().hex}.pdf")
    target = target_dir / target_name
    target.write_bytes(src.read_bytes())
    return target, target.stat().st_size
