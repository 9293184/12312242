"""PDF parsing helpers for Demo V1.

This module prepares model inputs and also performs local abstract extraction
as a fallback when DeepSeek fails to extract the abstract.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

FIRST_PAGES_LIMIT = 6
OCR_MAX_PAGES = 20


try:
    from pypdf import PdfReader  # type: ignore
except Exception:  # pragma: no cover - optional dependency
    PdfReader = None  # type: ignore


def _normalize_text(raw_text: str) -> str:
    return "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())


def _clean_abstract(text: str) -> str:
    text = text.strip()

    end_markers = [
        "\nIndex Terms",
        "\nindex terms",
        "\nKeywords",
        "\nkeywords",
        "\nI. ",
        "\nII. ",
        "\nIII. ",
        "\nIV. ",
        "\nV. ",
        "\nVI. ",
        "\nVII. ",
        "\nVIII. ",
        "\nIX. ",
        "\nX. ",
        "\n1. Introduction",
        "\n2. ",
        "\n3. ",
        "\n4. ",
        "\n5. ",
        "\n6. ",
        "\n7. ",
        "\n8. ",
        "\n9. ",
        "\n10. ",
        "\nReferences",
        "\nREFERENCES",
        "\nAcknowledgements",
        "\nacknowledgements",
        "\nAcknowledgment",
        "\nacknowledgment",
        "\nIntroduction",
        "\nINTRODUCTION",
        "\nAbstract",
        "\nABSTRACT",
        "\n摘要",
    ]

    for marker in end_markers:
        idx = text.find(marker)
        if idx != -1:
            text = text[:idx]

    text = re.sub(r"-\s*\n\s*", "", text)

    text = re.sub(r"\s+", " ", text).strip()

    footnote_match = re.search(r" Received \d+ [A-Za-z]+ \d{4}", text)
    if footnote_match:
        footnote_start = footnote_match.start()
        doi_match = re.search(r"\d+\.\d+/[^\s]+", text[footnote_start:])
        if doi_match:
            footnote_end = footnote_start + doi_match.end()
            text = text[:footnote_start] + text[footnote_end:]
        else:
            text = text[:footnote_start]

    text = re.sub(r"\([^)]+@[^)]+\)", "", text)
    text = re.sub(r" Digital Object Identifier \d+\.\d+/[^\s]+", "", text)
    text = re.sub(r" DOI:\s*\d+\.\d+/[^\s]+", "", text)
    text = re.sub(r" \(Corresponding author:[^)]+\)", "", text)
    text = re.sub(r" are with the [^.]+?\.", "", text)

    text = re.sub(r"\s+", " ", text).strip()

    if len(text) > 3000:
        text = text[:3000].rsplit(".", 1)[0] + "."

    return text


def _locate_abstract_region(text: str) -> str:
    text_normalized = _normalize_text(text)

    abstract_markers = [
        r"Abstract\s*[—–\-:]",
        r"ABSTRACT\s*[—–\-:]",
        r"Abstract\s*\n",
        r"ABSTRACT\s*\n",
        r"Abstract\s*\.\s*",
        r"ABSTRACT\s*\.\s*",
        r"摘要\s*[—–\-:]",
        r"摘要\s*\n",
    ]

    for pattern in abstract_markers:
        match = re.search(pattern, text_normalized, re.DOTALL | re.IGNORECASE)
        if match:
            start_pos = match.end()
            context_before = max(0, start_pos - 100)
            context_after = min(len(text_normalized), start_pos + 3500)
            return text_normalized[context_before:context_after]

    return ""


def _extract_local_abstract(text: str) -> tuple[str, str]:
    abstract_cn = ""
    abstract_en = ""

    text_normalized = _normalize_text(text)

    patterns_en = [
        r"Abstract\s*[—–\-:]\s*(.*)",
        r"ABSTRACT\s*[—–\-:]\s*(.*)",
        r"Abstract\s*\n(.*)",
        r"ABSTRACT\s*\n(.*)",
        r"Abstract\s*\.\s*(.*)",
        r"ABSTRACT\s*\.\s*(.*)",
    ]

    patterns_cn = [
        r"摘要\s*[—–\-:]\s*(.*)",
        r"摘要\s*\n(.*)",
    ]

    for pattern in patterns_en:
        match = re.search(pattern, text_normalized, re.DOTALL | re.IGNORECASE)
        if match:
            candidate = match.group(1).strip()
            if len(candidate) > 50:
                abstract_en = _clean_abstract(candidate)
                break

    for pattern in patterns_cn:
        match = re.search(pattern, text_normalized, re.DOTALL)
        if match:
            candidate = match.group(1).strip()
            if len(candidate) > 50:
                abstract_cn = _clean_abstract(candidate)
                break

    if abstract_en and not abstract_cn:
        if any("\u4e00" <= ch <= "\u9fff" for ch in abstract_en):
            abstract_cn = abstract_en
    elif abstract_cn and not abstract_en:
        if all(ord(ch) < 128 or ch.isspace() for ch in abstract_cn):
            abstract_en = abstract_cn

    return abstract_cn, abstract_en


def _extract_pdf_metadata_text(pdf_path: Path) -> str:
    chunks: list[str] = []

    if PdfReader is not None:
        try:
            reader = PdfReader(str(pdf_path))
            metadata = reader.metadata or {}
            for key, value in metadata.items():
                if value:
                    chunks.append(f"{str(key).lstrip('/').lower()}: {value}")
            xmp = getattr(reader, "xmp_metadata", None)
            if xmp:
                chunks.append(str(xmp))
            return "\n".join(chunks)
        except Exception:
            pass

    try:
        raw = pdf_path.read_bytes().decode("latin-1", errors="ignore")
    except Exception:
        return ""

    patterns = (
        r"/Title\s*\((.*?)\)",
        r"/Author\s*\((.*?)\)",
        r"/Subject\s*\((.*?)\)",
        r"/Keywords\s*\((.*?)\)",
        r"/Creator\s*\((.*?)\)",
        r"/Producer\s*\((.*?)\)",
        r"<dc:title>.*?<rdf:li[^>]*>(.*?)</rdf:li>.*?</dc:title>",
        r"<dc:subject>.*?<rdf:li[^>]*>(.*?)</rdf:li>.*?</dc:subject>",
        r"<dc:creator>.*?<rdf:li[^>]*>(.*?)</rdf:li>.*?</dc:creator>",
        r"<prism:doi>(.*?)</prism:doi>",
        r"<prism:publicationName>(.*?)</prism:publicationName>",
    )

    for pattern in patterns:
        import re

        match = re.search(pattern, raw, flags=re.IGNORECASE | re.DOTALL)
        if match:
            chunks.append(match.group(0).strip())

    return "\n".join(chunks)


def _extract_first_pages_text(pdf_path: Path) -> tuple[str, str, str]:
    try:
        import fitz  # type: ignore
    except Exception:
        return "", "", ""

    try:
        doc = fitz.open(str(pdf_path))
    except Exception:
        return "", "", ""

    # 用上下文管理器确保文档句柄被关闭：反复分析同一 PDF 时不会泄漏
    # 文件句柄与原生内存。同时只遍历一次文档。
    with doc:
        pages: list[str] = []
        marked: list[str] = []
        for index, page in enumerate(doc):
            if index >= FIRST_PAGES_LIMIT:
                break
            text = page.get_text("text") or ""
            pages.append(text)
            marked.append(f"[PAGE {index + 1}]\n{_normalize_text(text)}")

    first_pages_text = _normalize_text("\n".join(pages))
    metadata_pages_text = first_pages_text
    first_pages_marked_text = "\n\n".join(marked)
    return first_pages_text, metadata_pages_text, first_pages_marked_text


def extract_pdf_text(pdf_path: str | Path) -> dict[str, str]:
    path = Path(pdf_path)
    raw_bytes = path.read_bytes()
    logger.info("pdf_parse_start path=%s size=%s", path, len(raw_bytes))

    first_pages_text, metadata_pages_text, first_pages_marked_text = _extract_first_pages_text(path)
    metadata_text = _extract_pdf_metadata_text(path)

    abstract_region = _locate_abstract_region(first_pages_text)
    abstract_cn, abstract_en = _extract_local_abstract(first_pages_text)
    abstract = abstract_cn or abstract_en

    logger.info(
        "pdf_parse_inputs path=%s first_pages_len=%s metadata_pages_len=%s metadata_len=%s abstract_len=%s abstract_region_len=%s",
        path,
        len(first_pages_text),
        len(metadata_pages_text),
        len(metadata_text),
        len(abstract),
        len(abstract_region),
    )

    return {
        "title": "",
        "title_cn": "",
        "title_en": "",
        "authors": "",
        "source": "",
        "abstract": abstract,
        "abstract_cn": abstract_cn,
        "abstract_en": abstract_en,
        "keywords": "",
        "year": "",
        "doi": "",
        "introduction": "",
        "method": "",
        "experiments": "",
        "conclusion": "",
        "full_text": first_pages_text[:120000],
        "raw_text": first_pages_text[:120000],
        "first_pages_text": first_pages_text[:12000],
        "first_pages_marked_text": first_pages_marked_text[:30000],
        "metadata_pages_text": metadata_pages_text[:12000],
        "metadata_text": metadata_text[:12000],
        "candidate_text": first_pages_text[:120000],
        "abstract_region": abstract_region[:3500],
        "extraction_method": "first_six_pages",
        "text_quality": "0.0000",
    }


def extract_text_from_markdown(markdown_path: str | Path) -> dict[str, str]:
    """Build a parsed-text dict from a MinerU-generated Markdown file.

    Produces a dict with the same shape as ``extract_pdf_text`` so downstream
    consumers (``build_metadata_payload`` / ``build_analysis_payload``) work
    unchanged. Field semantics are adapted for Markdown rather than OCR:

    - ``raw_text`` / ``full_text`` / ``candidate_text``: full Markdown text
      (image tags stripped), used as the canonical text source.
    - ``first_pages_text``: leading slice of the Markdown, kept for any
      downstream code that expects a "first pages" preview.
    - ``first_pages_marked_text`` / ``metadata_pages_text`` / ``metadata_text``:
      left empty, because MinerU output has no separate page boundaries or
      PDF metadata stream. ``extract_metadata`` detects ``extraction_method
      == 'mineru'`` and uses a Markdown-specific prompt that does not rely
      on these fields.
    - ``abstract_region`` / ``abstract``: located locally from the Markdown
      via ``_locate_abstract_region`` and ``_extract_local_abstract``.
    - ``extraction_method``: ``"mineru"`` so callers can branch on source.

    Args:
        markdown_path: Path to the ``full.md`` file produced by MinerU.

    Returns:
        Dict compatible with ``extract_pdf_text`` output.
    """
    path = Path(markdown_path)
    if not path.exists():
        logger.warning("mineru_markdown_not_found path=%s", path)
        return extract_pdf_text(path)  # fall back if file missing

    md_text = path.read_text(encoding="utf-8")
    logger.info("mineru_parse_start path=%s size=%s", path, len(md_text))

    # Strip image markdown tags to keep text-focused analysis clean
    text_for_analysis = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", md_text).strip()

    abstract_region = _locate_abstract_region(text_for_analysis)
    abstract_cn, abstract_en = _extract_local_abstract(text_for_analysis)
    abstract = abstract_cn or abstract_en

    # Leading slice kept for downstream compatibility (e.g. analysis preview)
    first_pages_text = text_for_analysis[:12000]

    logger.info(
        "mineru_parse_inputs path=%s text_len=%s abstract_len=%s abstract_region_len=%s",
        path,
        len(text_for_analysis),
        len(abstract),
        len(abstract_region),
    )

    return {
        "title": "",
        "title_cn": "",
        "title_en": "",
        "authors": "",
        "source": "",
        "abstract": abstract,
        "abstract_cn": abstract_cn,
        "abstract_en": abstract_en,
        "keywords": "",
        "year": "",
        "doi": "",
        "introduction": "",
        "method": "",
        "experiments": "",
        "conclusion": "",
        "full_text": text_for_analysis[:120000],
        "raw_text": text_for_analysis[:120000],
        "first_pages_text": first_pages_text,
        # MinerU Markdown has no page boundaries or PDF metadata stream.
        # These fields are intentionally empty; extract_metadata uses a
        # Markdown-specific prompt when extraction_method == 'mineru'.
        "first_pages_marked_text": "",
        "metadata_pages_text": "",
        "metadata_text": "",
        "candidate_text": text_for_analysis[:120000],
        "abstract_region": abstract_region[:3500],
        "extraction_method": "mineru",
        "text_quality": "1.0000",  # MinerU output is high quality
    }
