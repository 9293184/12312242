"""文献互通 API：导入/导出 CSL JSON / BibTeX / RIS。

端点均为同步 ``def``，由 FastAPI 自动放入线程池执行，避免解析与写库阻塞事件循环。
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import Response

from app.core import ref_formats as rf
from app.services.interop_service import export_text, import_text

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/interop", tags=["interop"])

_DECODE_ENCODINGS = ("utf-8-sig", "utf-8", "gb18030", "latin-1")


def _decode(raw: bytes) -> str:
    for encoding in _DECODE_ENCODINGS:
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


@router.get("/formats")
def list_formats() -> dict:
    """列出支持的格式，供前端渲染选项。"""
    return {
        "formats": [
            {"id": fmt, "label": rf.FORMAT_LABELS[fmt], "extension": rf.format_extension(fmt)}
            for fmt in rf.FORMATS
        ]
    }


@router.post("/import")
def import_api(
    file: UploadFile = File(...),
    format: str | None = Form(None),
    folder_id: str | None = Form(None),
    skip_duplicates: bool = Form(True),
) -> dict:
    """导入文献文件（CSL JSON / BibTeX / RIS），格式可自动识别。"""
    raw = file.file.read()
    if not raw:
        raise HTTPException(status_code=400, detail="文件为空")

    resolved = format if format in rf.FORMATS else None
    try:
        result = import_text(
            _decode(raw),
            fmt=resolved,
            filename=file.filename or "",
            default_folder_id=folder_id or None,
            skip_duplicates=skip_duplicates,
        )
    except Exception as exc:  # noqa: BLE001 - 解析失败统一转 400
        logger.warning("interop import failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=400, detail=f"解析或导入失败：{exc}") from exc
    return result


@router.get("/export")
def export_api(
    format: str = Query("csljson", description="csljson | bibtex | ris"),
    paper_ids: str | None = Query(None, description="逗号分隔的论文 ID，缺省导出全部"),
) -> Response:
    """导出文献库为指定格式的文件下载。"""
    if format not in rf.FORMATS:
        raise HTTPException(status_code=400, detail=f"不支持的格式：{format}")

    ids = [p.strip() for p in paper_ids.split(",") if p.strip()] if paper_ids else None
    try:
        text = export_text(format, ids)
    except Exception as exc:  # noqa: BLE001
        logger.warning("interop export failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"导出失败：{exc}") from exc

    filename = f"paperpilot_export.{rf.format_extension(format)}"
    return Response(
        content=text,
        media_type=rf.format_media_type(format),
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )
