"""内置初始文献的播种。

首次启动且文献库为空时：

1. 执行 ``Data/seed.sql``，写入三篇大模型相关论文的题录与标签；
2. 把随仓库内置的 PDF 放进 ``workspace/storage/<paper_id>/original.pdf``，
   建立附件记录，并像「刚上传 PDF」那样触发后台分析。

PDF 内置在 ``Data/seed_pdfs/``，因此离线也能开箱即用。
若某个 PDF 缺失，该篇会降级为「仅题录」终态，不会卡在「正在分析」。
"""

from __future__ import annotations

import logging
from pathlib import Path
from threading import Thread

from app.core.config import settings
from app.db import apply_seed_sql, session
from app.services.paper_service import (
    PAPER_STATUS_IMPORTED,
    ensure_analysis_placeholder,
    run_analysis_exclusive,
    upsert_attachment_file,
)

logger = logging.getLogger(__name__)

# 标记文件：记录是否已播种过，避免每次启动重复插入
SEED_MARKER_NAME = ".initial_seed_applied"

# 内置 PDF 目录（随仓库提供）
_SEED_PDF_DIR = settings.data_dir / "seed_pdfs"

# 内置文献：(论文 ID, 附件显示名) —— ID 与 Data/seed.sql 一致
_SEED_PAPERS: tuple[tuple[str, str], ...] = (
    ("paper-demo-0001", "attention_is_all_you_need.pdf"),
    ("paper-demo-0002", "bert_pre_training_of_deep_bidirectional_transformers.pdf"),
    ("paper-demo-0003", "language_models_are_few_shot_learners.pdf"),
)


def seed_initial_library() -> int:
    """首次启动时写入内置文献，并像上传 PDF 一样触发分析。

    只在「从未播种过」且「库内没有任何论文」时执行，因此不会重复插入，
    用户清空文献库后重启也不会又冒出来。

    Returns:
        写入的论文条数（未播种时为 0）。
    """
    marker = settings.workspace_dir / SEED_MARKER_NAME
    if marker.exists():
        return 0

    with session() as conn:
        existing = conn.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    if existing:
        # 已有数据（例如从备份恢复）：只打标记，不插入
        _mark_seeded(marker)
        return 0

    count = apply_seed_sql()
    if not count:
        return 0

    _attach_bundled_pdfs()
    _mark_seeded(marker)
    logger.info("initial_library_seeded papers=%s", count)
    return count


def _mark_seeded(marker: Path) -> None:
    try:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("1", encoding="utf-8")
    except OSError:
        logger.warning("seed_marker_write_failed", exc_info=True)


def _attach_bundled_pdfs() -> None:
    """给内置文献放置 PDF 并触发分析；缺失 PDF 时降级为「仅题录」终态。"""
    for paper_id, display_name in _SEED_PAPERS:
        pdf_path = _SEED_PDF_DIR / f"{paper_id}.pdf"
        if not pdf_path.exists():
            logger.warning("seed_pdf_missing paper_id=%s path=%s", paper_id, pdf_path)
            _mark_metadata_only(paper_id)
            continue
        try:
            ensure_analysis_placeholder(paper_id)
            upsert_attachment_file(paper_id, "original", str(pdf_path), display_name)
            # 与上传流程一致：存储后的规范路径，后台线程解析并分析
            stored_path = settings.workspace_dir / "storage" / paper_id / "original.pdf"
            Thread(
                target=run_analysis_exclusive,
                args=(paper_id, str(stored_path)),
                daemon=True,
            ).start()
            logger.info("seed_paper_ready paper_id=%s", paper_id)
        except Exception:  # noqa: BLE001 - 单篇失败不应中断播种
            logger.exception("seed_paper_attach_failed paper_id=%s", paper_id)
            _mark_metadata_only(paper_id)


def _mark_metadata_only(paper_id: str) -> None:
    """没有 PDF 时置为终态，避免前端一直显示「正在分析」。"""
    with session() as conn:
        conn.execute(
            "UPDATE papers SET status = ? WHERE id = ?",
            (PAPER_STATUS_IMPORTED, paper_id),
        )
