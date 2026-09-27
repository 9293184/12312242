"""Application service layer for paper operations."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path
from uuid import uuid4

logger = logging.getLogger(__name__)

from app.core.analysis import build_analysis_payload, build_metadata_payload
from app.core.debug_log import append_debug_record, clear_task_logs, log_task_event, log_task_update, task_log_timer
from app.core.pdf_parser import extract_pdf_text, extract_text_from_markdown
from app.core.storage import resolve_attachment_path, store_attachment_file
from app.db import session
from app.db.sqlite import to_utc_isoformat
from app.models import AnalysisCreate, MetadataCreate, PaperCreate, PaperDetailResponse, PaperUpdate

ALLOWED_ATTACHMENT_TYPES = {"original", "translated", "mapped"}

# --- Papers.status state machine ---
# uploaded              → PDF uploaded, no parsing started
# mineru_processing     → MinerU conversion in progress (asynchronous)
# mineru_converted      → MinerU Markdown written to workspace & DB
# ocr_fallback          → MinerU skipped/failed, switched to OCR
# text_extracting       → Text extraction in progress
# metadata_extracting   → Metadata extraction in progress
# analyzing             → 8-dimension analysis in progress
# parsed                → Text extraction done (mineru OR ocr)
# done                  → Metadata + 8-dimension analysis complete
# failed                → Analysis failed
PAPER_STATUS_UPLOADED = "uploaded"
PAPER_STATUS_MINERU_PROCESSING = "mineru_processing"
PAPER_STATUS_MINERU_CONVERTED = "mineru_converted"
PAPER_STATUS_OCR_FALLBACK = "ocr_fallback"
PAPER_STATUS_TEXT_EXTRACTING = "text_extracting"
PAPER_STATUS_METADATA_EXTRACTING = "metadata_extracting"
PAPER_STATUS_ANALYZING = "analyzing"
PAPER_STATUS_PARSED = "parsed"
PAPER_STATUS_DONE = "done"
PAPER_STATUS_FAILED = "failed"
PAPER_STATUS_DUPLICATE_DETECTED = "duplicate_detected"

# Duplicate detection thresholds
DUPLICATE_TITLE_SIMILARITY_THRESHOLD = 0.75
DUPLICATE_AUTHOR_OVERLAP_THRESHOLD = 0.5
DUPLICATE_COMPOSITE_THRESHOLD = 0.6


def _update_paper_status(paper_id: str, status: str, reason: str | None = None) -> None:
    """Atomically update papers.status for the given paper.

    Args:
        paper_id: Target paper.
        status: New status to set.
        reason: Optional explanation (written to debug_record) for
            traceability.
    """
    with session() as conn:
        conn.execute(
            "UPDATE papers SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, paper_id),
        )
    if reason:
        append_debug_record(paper_id, "status_change", new_status=status, reason=reason)
    else:
        append_debug_record(paper_id, "status_change", new_status=status)
    logger.info("paper_status_change paper_id=%s status=%s reason=%s", paper_id, status, reason)


# --- Duplicate Detection Helpers ---

def _normalize_text(text: str) -> str:
    """Normalize text for comparison: lowercase, strip extra whitespace."""
    if not text:
        return ""
    text = text.lower().strip()
    text = re.sub(r'\s+', ' ', text)
    return text


def _tokenize(text: str) -> set[str]:
    """Tokenize text into words for comparison."""
    normalized = _normalize_text(text)
    if not normalized:
        return set()
    tokens = set()
    for word in re.split(r'[\s,.;:!?()\[\]{}"\'/\\]+', normalized):
        if len(word) >= 2:
            tokens.add(word)
    return tokens


def _title_similarity(title1: str, title2: str) -> float:
    """Calculate similarity between two titles using token overlap."""
    if not title1 or not title2:
        return 0.0
    tokens1 = _tokenize(title1)
    tokens2 = _tokenize(title2)
    if not tokens1 or not tokens2:
        return 0.0
    intersection = tokens1 & tokens2
    union = tokens1 | tokens2
    if not union:
        return 0.0
    return len(intersection) / len(union)


def _author_overlap(authors1: str, authors2: str) -> float:
    """Calculate author overlap ratio."""
    if not authors1 or not authors2:
        return 0.0
    set1 = {_normalize_text(a) for a in re.split(r'[;,\s]+', authors1) if _normalize_text(a)}
    set2 = {_normalize_text(a) for a in re.split(r'[;,\s]+', authors2) if _normalize_text(a)}
    if not set1 or not set2:
        return 0.0
    intersection = set1 & set2
    union = set1 | set2
    if not union:
        return 0.0
    return len(intersection) / len(union)


def _keyword_overlap(kw1: str, kw2: str) -> float:
    """Calculate keyword overlap ratio."""
    if not kw1 or not kw2:
        return 0.0
    set1 = {_normalize_text(k) for k in re.split(r'[;,\s]+', kw1) if _normalize_text(k)}
    set2 = {_normalize_text(k) for k in re.split(r'[;,\s]+', kw2) if _normalize_text(k)}
    if not set1 or not set2:
        return 0.0
    intersection = set1 & set2
    union = set1 | set2
    if not union:
        return 0.0
    return len(intersection) / len(union)


def check_duplicate_paper(
    title: str = "",
    title_cn: str = "",
    title_en: str = "",
    authors: str = "",
    keywords: str = "",
    doi: str = "",
    exclude_paper_id: str = "",
) -> list[dict]:
    """Check for duplicate papers using multiple criteria.

    Args:
        title: Combined or main title.
        title_cn: Chinese title.
        title_en: English title.
        authors: Semicolon-separated author names.
        keywords: Semicolon-separated keywords.
        doi: DOI identifier.
        exclude_paper_id: Paper ID to exclude (self-check).

    Returns:
        List of duplicate candidate dicts with scores and match details.
    """
    candidates: list[dict] = []

    with session() as conn:
        rows = conn.execute("SELECT id, title, title_cn, title_en, authors FROM papers").fetchall()

        # Fetch metadata (keywords, doi) from paper_texts for each paper
        paper_metadata: dict[str, dict] = {}
        meta_rows = conn.execute(
            """
            SELECT paper_id, sections_json FROM paper_texts
            WHERE text_scope = 'metadata'
            """
        ).fetchall()
        for mr in meta_rows:
            pid = mr["paper_id"]
            try:
                sections = json.loads(mr["sections_json"]) if mr["sections_json"] else {}
                metadata = sections.get("metadata", sections)
                paper_metadata[pid] = {
                    "keywords": metadata.get("keywords", ""),
                    "doi": metadata.get("doi", ""),
                }
            except (ValueError, TypeError):
                paper_metadata[pid] = {"keywords": "", "doi": ""}

    # 1. DOI exact match
    if doi:
        doi_normalized = _normalize_text(doi)
        for row in rows:
            if exclude_paper_id and row["id"] == exclude_paper_id:
                continue
            row_meta = paper_metadata.get(row["id"], {})
            row_doi = _normalize_text(row_meta.get("doi", ""))
            if row_doi and doi_normalized == row_doi:
                candidates.append({
                    "paper_id": row["id"],
                    "score": 1.0,
                    "matched_criteria": ["DOI 精确匹配"],
                    "title": row["title_en"] or row["title_cn"] or row["title"] or "",
                    "authors": row["authors"] or "",
                    "doi": row_meta.get("doi", ""),
                    "match_type": "doi_exact",
                })
                return candidates  # DOI exact match is conclusive

    # 2. Composite scoring for non-DOI matches
    paper_title = title_en or title_cn or title
    paper_keywords = keywords

    for row in rows:
        if exclude_paper_id and row["id"] == exclude_paper_id:
            continue

        row_title = row["title_en"] or row["title_cn"] or row["title"] or ""
        row_authors = row["authors"] or ""
        row_meta = paper_metadata.get(row["id"], {})
        row_keywords = row_meta.get("keywords", "")

        scores: list[float] = []
        criteria: list[str] = []

        # Title similarity
        if paper_title and row_title:
            title_sim = _title_similarity(paper_title, row_title)
            if title_sim >= DUPLICATE_TITLE_SIMILARITY_THRESHOLD:
                scores.append(title_sim)
                criteria.append(f"标题相似度 {title_sim * 100:.0f}%")
            elif title_sim >= 0.5:
                scores.append(title_sim * 0.7)

        # Author overlap
        if authors and row_authors:
            author_overlap = _author_overlap(authors, row_authors)
            if author_overlap >= DUPLICATE_AUTHOR_OVERLAP_THRESHOLD:
                scores.append(author_overlap)
                criteria.append(f"作者重叠度 {author_overlap * 100:.0f}%")
            elif author_overlap >= 0.3:
                scores.append(author_overlap * 0.7)

        # Keyword overlap
        if paper_keywords and row_keywords:
            kw_overlap = _keyword_overlap(paper_keywords, row_keywords)
            if kw_overlap >= 0.5:
                scores.append(kw_overlap)
                criteria.append(f"关键词重叠度 {kw_overlap * 100:.0f}%")

        if not scores:
            continue

        composite = sum(scores) / len(scores) if scores else 0.0

        if composite >= DUPLICATE_COMPOSITE_THRESHOLD:
            # Add high-confidence label
            if composite >= 0.9:
                criteria.insert(0, "标题高度相似")
            elif composite >= 0.75:
                criteria.insert(0, "标题与作者高度匹配")

            candidates.append({
                "paper_id": row["id"],
                "score": round(composite, 3),
                "matched_criteria": criteria,
                "title": row_title,
                "authors": row_authors,
                "doi": row_meta.get("doi", ""),
                "match_type": "composite",
            })

    # Sort by score descending
    candidates.sort(key=lambda x: x["score"], reverse=True)
    return candidates


# --- Search ---

# Field weights for deep (high-order) search. Higher weight = stronger
# signal of relevance. Used by search_papers() when deep=True.
SEARCH_FIELD_WEIGHTS = {
    "title": 5.0,       # title / title_cn / title_en
    "keywords": 4.0,    # metadata.keywords
    "abstract": 3.0,    # abstract / abstract_cn / abstract_en / abstract_extracted
    "tldr": 2.5,        # paper_analysis.tldr
    "authors": 2.0,     # papers.authors
    "source": 2.0,      # papers.source_url + metadata.source
    "analysis": 1.0,    # 8-dim analysis (motivation/methodology/...)
    "doi": 1.5,         # metadata.doi
    "year": 1.0,        # papers.publish_date + metadata.year
}

# Human-readable labels for matched fields (used by frontend display).
SEARCH_FIELD_LABELS = {
    "title": "标题",
    "keywords": "关键词",
    "abstract": "摘要",
    "tldr": "TLDR",
    "authors": "作者",
    "source": "来源",
    "analysis": "八维分析",
    "doi": "DOI",
    "year": "年份",
}


def _tokenize_query(query: str) -> list[str]:
    """Tokenize a search query into lowercase terms.

    Splits on whitespace. Empty tokens are filtered out. Each token is
    used as a substring match (case-insensitive) so multi-word phrases
    and Chinese terms both work.
    """
    if not query:
        return []
    return [t.lower() for t in re.split(r"\s+", query.strip()) if t]


def _edit_distance(a: str, b: str) -> int:
    """Compute Levenshtein edit distance between two strings."""
    if len(a) < len(b):
        a, b = b, a
    if not b:
        return len(a)
    prev = range(len(b) + 1)
    for i, ca in enumerate(a):
        curr = [i + 1]
        for j, cb in enumerate(b):
            curr.append(min(
                prev[j + 1] + 1,
                curr[j] + 1,
                prev[j] + (ca != cb),
            ))
        prev = curr
    return prev[-1]


# 模糊匹配的规模上限：分析全文可达十万字符，逐字符滑动窗口 + 编辑距离
# 会让单次查询耗时数秒。精确匹配仍覆盖全文，模糊匹配只在有界范围内进行。
_FUZZY_TEXT_LIMIT = 20_000
_FUZZY_MAX_WORDS = 3_000


def _is_fuzzy_match(text: str, term: str, threshold: float = 0.6) -> bool:
    """Check if term approximately matches text via prefix, edit distance, or substring.

    Returns True when the term is likely a fuzzy match for a word in the text.
    """
    if not text or not term:
        return False
    text_lower = text.lower()
    term = term.lower()

    # 精确子串匹配：覆盖全文，成本低
    if term in text_lower:
        return True

    # 以下为模糊匹配：限定在文本前 _FUZZY_TEXT_LIMIT 个字符内，控制耗时
    bounded = text_lower[:_FUZZY_TEXT_LIMIT]
    text_words = re.findall(r"[a-zA-Z0-9\u4e00-\u9fff]+", bounded)[:_FUZZY_MAX_WORDS]
    if not text_words:
        # For CJK-heavy text without spaces, use sliding window
        # Take substrings of length close to len(term) and check edit distance
        term_len = len(term)
        best_ratio = 0.0
        for i in range(len(bounded) - term_len + 1):
            window = bounded[i : i + term_len]
            dist = _edit_distance(window, term)
            ratio = 1.0 - dist / max(len(window), len(term))
            if ratio > best_ratio:
                best_ratio = ratio
        return best_ratio >= threshold

    # Word-level fuzzy matching
    for word in text_words:
        # Prefix match
        if len(term) >= 2 and word.startswith(term):
            return True
        if len(word) >= 2 and term.startswith(word):
            return True
        # Edit distance
        dist = _edit_distance(word, term)
        max_len = max(len(word), len(term))
        if max_len > 0:
            ratio = 1.0 - dist / max_len
            if ratio >= threshold:
                return True
    return False


def _count_term_hits(text: str, terms: list[str], fuzzy: bool = False) -> float:
    """Count how many query terms appear in text, with optional fuzzy matching.

    Returns a float score: exact matches count as 1.0, fuzzy matches count as 0.5.
    """
    if not text or not terms:
        return 0.0
    text_lower = text.lower()
    score = 0.0
    for t in terms:
        if t in text_lower:
            score += 1.0
        elif fuzzy:
            if _is_fuzzy_match(text_lower, t):
                score += 0.5
    return score


def _build_snippet(text: str, terms: list[str], window: int = 80) -> str:
    """Build a short snippet around the first term match in text."""
    if not text or not terms:
        return ""
    text_lower = text.lower()
    pos = -1
    for t in terms:
        p = text_lower.find(t)
        if p >= 0 and (pos < 0 or p < pos):
            pos = p
    if pos < 0:
        return ""
    # Highlight window: 30 chars before, rest after the matched term
    match_len = max(len(t) for t in terms if t in text_lower) if any(t in text_lower for t in terms) else 0
    start = max(0, pos - 30)
    end = min(len(text), pos + max(match_len, 0) + 50)
    snippet = text[start:end]
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return prefix + snippet + suffix


def search_papers(
    query: str,
    deep: bool = False,
    limit: int = 100,
    folder_id: str | None = None,
    tag_ids: list[str] | None = None,
    fuzzy: bool = False,
) -> dict:
    """Search papers across multiple fields with optional weighted scoring.

    Basic search (deep=False):
        Matches on title / title_cn / title_en / authors / abstract using
        substring matching. Any query term match is enough to include the
        paper. Results are ordered by created_at DESC (no relevance score).

    Deep search (deep=True):
        Searches across 9 field groups with weighted scoring:
        - title (5.0): title, title_cn, title_en
        - keywords (4.0): metadata.keywords (from paper_texts.sections_json)
        - abstract (3.0): abstract, abstract_cn, abstract_en, abstract_extracted
        - tldr (2.5): paper_analysis.tldr
        - authors (2.0): papers.authors
        - source (2.0): papers.source_url + metadata.source
        - analysis (1.0): motivation, methodology, experiments, conclusion,
          strengths, weaknesses, ablation, resources
        - doi (1.5): metadata.doi
        - year (1.0): papers.publish_date + metadata.year
        Score = sum(weight * term_hit_count) per matched field. Results are
        ordered by score DESC. Each item includes matched_fields list and
        a snippet for display.

    Fuzzy mode (fuzzy=True, effective in deep search):
        Enables approximate matching via edit distance, prefix matching,
        and character n-gram overlap. Fuzzy matches score 0.5 (vs 1.0 for
        exact matches), ensuring exact results rank higher.

    Args:
        query: Search query string.
        deep: If True, use weighted deep search; if False, basic search.
        limit: Maximum number of results to return.
        folder_id: Optional folder ID to filter results by folder membership.
        tag_ids: Optional list of tag IDs to filter results by tag membership.
            Papers must have ALL of the specified tags (AND semantics).
        fuzzy: If True, enable fuzzy/approximate matching in deep search.

    Returns:
        Dict with keys: query, deep, total, items. Each item is a dict
        with PaperResponse fields plus score, matched_fields, snippet.
    """
    query = (query or "").strip()
    terms = _tokenize_query(query)
    has_filters = bool(folder_id or (tag_ids and len(tag_ids) > 0))
    if not terms and not has_filters:
        return {"query": query, "deep": deep, "total": 0, "items": []}

    # Build SQL-level WHERE clauses for performance pre-filtering.
    # This reduces the number of rows loaded into Python for scoring.
    sql_conditions: list[str] = []
    sql_params: list = []

    # Folder filter: recursively include all descendant folders.
    # Pre-fetch descendant folder IDs via recursive CTE (SQLite doesn't
    # support CTE inside subqueries, so we resolve IDs first).
    if folder_id:
        with session() as conn:
            folder_rows = conn.execute(
                """
                WITH RECURSIVE folder_tree AS (
                    SELECT id FROM folders WHERE id = ?
                    UNION ALL
                    SELECT f.id FROM folders f
                    JOIN folder_tree ft ON f.parent_id = ft.id
                )
                SELECT id FROM folder_tree
                """,
                (folder_id,),
            ).fetchall()
        descendant_ids = [row["id"] for row in folder_rows]
        placeholders = ", ".join("?" for _ in descendant_ids)
        sql_conditions.append(f"p.folder_id IN ({placeholders})")
        sql_params.extend(descendant_ids)

    # Tag filter: papers must have ALL of the specified tags (AND semantics)
    if tag_ids:
        placeholders = ", ".join("?" for _ in tag_ids)
        # AND semantics: papers must have ALL specified tags.
        # Use GROUP BY ... HAVING COUNT(DISTINCT tag_id) = N to enforce
        # conjunction while keeping a single subquery for efficiency.
        sql_conditions.append(
            f"p.id IN ("
            f"SELECT paper_id FROM paper_tags "
            f"WHERE tag_id IN ({placeholders}) "
            f"GROUP BY paper_id "
            f"HAVING COUNT(DISTINCT tag_id) = {len(tag_ids)}"
            f")"
        )
        sql_params.extend(tag_ids)

    # SQL-level term pre-filter: match any term across key text fields.
    # This avoids loading papers that clearly don't match into Python.
    # Also covers metadata (doi/source/year) via sections_json text blob
    # and analysis fields for comprehensive deep search pre-screening.
    for term in terms:
        like_pattern = f"%{term}%"
        sql_conditions.append(
            "(LOWER(p.title) LIKE ? OR LOWER(p.title_cn) LIKE ? OR LOWER(p.title_en) LIKE ? "
            "OR LOWER(p.authors) LIKE ? OR LOWER(p.abstract) LIKE ? "
            "OR LOWER(p.source_url) LIKE ? "
            "OR LOWER(pt.sections_json) LIKE ? "
            "OR LOWER(pa.tldr) LIKE ? OR LOWER(pa.motivation) LIKE ? "
            "OR LOWER(pa.methodology) LIKE ? OR LOWER(pa.conclusion) LIKE ?)"
        )
        sql_params.extend([like_pattern] * 11)

    where_clause = ""
    if sql_conditions:
        where_clause = "WHERE " + " AND ".join(sql_conditions)

    with session() as conn:
        rows = conn.execute(
            f"""
            SELECT
                p.id, p.title, p.title_cn, p.title_en, p.authors,
                p.publish_date, p.abstract, p.source_url, p.status,
                p.folder_id, p.created_at, p.updated_at,
                pt.sections_json AS metadata_sections_json,
                pt.abstract_extracted AS metadata_abstract_extracted,
                pa.tldr, pa.motivation, pa.methodology, pa.experiments,
                pa.conclusion, pa.strengths, pa.weaknesses, pa.ablation,
                pa.resources, pa.analysis_status
            FROM papers p
            LEFT JOIN paper_texts pt
                ON pt.paper_id = p.id AND pt.text_scope = 'metadata'
            LEFT JOIN paper_analysis pa
                ON pa.paper_id = p.id
            {where_clause}
            ORDER BY p.created_at DESC
            """,
            tuple(sql_params),
        ).fetchall()

    items: list[dict] = []
    for row in rows:
        # Parse metadata sections_json (contains keywords, abstract_cn/en, doi, etc.)
        metadata: dict = {}
        if row["metadata_sections_json"]:
            try:
                sections = json.loads(row["metadata_sections_json"])
                if isinstance(sections, dict):
                    metadata = sections.get("metadata", sections)
            except (ValueError, TypeError):
                metadata = {}

        # Build searchable field groups
        title_text = " ".join(filter(None, [
            row["title"] or "", row["title_cn"] or "", row["title_en"] or "",
        ]))
        keywords_text = metadata.get("keywords", "") or ""
        abstract_text = " ".join(filter(None, [
            row["abstract"] or "",
            metadata.get("abstract_cn", "") or "",
            metadata.get("abstract_en", "") or "",
            metadata.get("abstract", "") or "",
            row["metadata_abstract_extracted"] or "",
        ]))
        tldr_text = row["tldr"] or ""
        authors_text = row["authors"] or ""
        source_text = " ".join(filter(None, [
            row["source_url"] or "",
            metadata.get("source", "") or "",
        ]))
        analysis_text = " ".join(filter(None, [
            row["motivation"] or "", row["methodology"] or "",
            row["experiments"] or "", row["conclusion"] or "",
            row["strengths"] or "", row["weaknesses"] or "",
            row["ablation"] or "", row["resources"] or "",
        ]))
        doi_text = metadata.get("doi", "") or ""
        year_text = " ".join(filter(None, [
            row["publish_date"] or "",
            metadata.get("year", "") or "",
        ]))

        field_groups = {
            "title": title_text,
            "keywords": keywords_text,
            "abstract": abstract_text,
            "tldr": tldr_text,
            "authors": authors_text,
            "source": source_text,
            "analysis": analysis_text,
            "doi": doi_text,
            "year": year_text,
        }

        if deep:
            # Deep search: weighted scoring across all field groups
            score = 0.0
            matched_fields: list[str] = []
            if terms:
                for field_name, field_text in field_groups.items():
                    if not field_text:
                        continue
                    hits = _count_term_hits(field_text, terms, fuzzy=fuzzy)
                    if hits > 0:
                        weight = SEARCH_FIELD_WEIGHTS.get(field_name, 1.0)
                        score += weight * hits
                        matched_fields.append(field_name)

                if score == 0.0:
                    continue
            elif not has_filters:
                continue

            # Build snippet from highest-weight matched field
            snippet = ""
            field_priority = ["title", "keywords", "abstract", "tldr", "source", "doi", "year", "analysis", "authors"]
            for field_name in field_priority:
                if field_name not in matched_fields:
                    continue
                snippet = _build_snippet(field_groups[field_name], terms)
                if snippet:
                    break
        else:
            # Basic search: substring match on title/authors/abstract/source
            if terms:
                haystack = " ".join(filter(None, [title_text, authors_text, abstract_text, source_text]))
                haystack_lower = haystack.lower()
                if fuzzy:
                    if not any(t in haystack_lower or _is_fuzzy_match(haystack_lower, t) for t in terms):
                        continue
                else:
                    if not any(t in haystack_lower for t in terms):
                        continue
            score = 0.0
            matched_fields = []
            snippet = ""

        items.append({
            "id": row["id"],
            "title": row["title"] or "",
            "title_cn": row["title_cn"] or "",
            "title_en": row["title_en"] or "",
            "authors": row["authors"] or "",
            "publish_date": row["publish_date"] or "",
            "abstract": row["abstract"] or "",
            "source_url": row["source_url"] or "",
            "status": row["status"] or "uploaded",
            "folder_id": row["folder_id"] if "folder_id" in row.keys() else None,
            "created_at": to_utc_isoformat(row["created_at"]),
            "updated_at": to_utc_isoformat(row["updated_at"]),
            "extraction_method": "",  # filled in by API layer
            "score": round(score, 3),
            "matched_fields": matched_fields,
            "snippet": snippet,
        })

    # Sort
    if deep:
        # Stable sort: first by created_at DESC (tiebreaker), then by score DESC.
        # created_at is an ISO timestamp string, so lexicographic DESC == chronological DESC.
        items.sort(key=lambda x: x["created_at"], reverse=True)
        items.sort(key=lambda x: x["score"], reverse=True)
    # Basic: keep created_at DESC order (already from SQL)

    # Apply limit
    items = items[:limit]

    return {
        "query": query,
        "deep": deep,
        "total": len(items),
        "items": items,
    }


def create_paper(payload: PaperCreate) -> str:
    paper_id = str(uuid4())
    with session() as conn:
        conn.execute(
            """
            INSERT INTO papers (
                id, title, title_cn, title_en, authors, publish_date,
                abstract, source_url, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                paper_id,
                payload.title,
                payload.title_cn,
                payload.title_en,
                payload.authors,
                payload.publish_date,
                payload.abstract,
                payload.source_url,
                payload.status,
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_analysis (
                id, paper_id, analysis_status, prompt_version
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(paper_id) DO NOTHING
            """,
            (str(uuid4()), paper_id, "pending", "v1"),
        )
    return paper_id


def _resolve_extraction_method(paper_id: str, analysis, metadata, paper_status: str) -> str:
    """Determine the effective extraction_method for a paper.

    Priority (highest first):
    1. paper_analysis.extraction_method — reflects what was used for the
       latest analysis run (most reliable for end-user display)
    2. paper_texts.metadata.extraction_method — reflects the text source
       when analysis has not run yet
    3. Derive from papers.status — as a fallback when both tables lack
       data but the paper has reached some processing state
    4. 'first_six_pages' — the OCR default, safe assumption
    """
    # 1) Latest analysis table
    if analysis is not None:
        m = (analysis["extraction_method"] if "extraction_method" in analysis.keys()
             else analysis.get("extraction_method") if hasattr(analysis, "get") else None)
        if m:
            return str(m)

    # 2) paper_texts.metadata row
    if metadata is not None:
        m = (metadata["extraction_method"] if "extraction_method" in metadata.keys()
             else metadata.get("extraction_method") if hasattr(metadata, "get") else None)
        if m:
            return str(m)

    # 3) Fall back to status hint
    if paper_status in ("mineru_converted", "mineru_processing"):
        return "mineru"
    if paper_status in ("ocr_fallback",):
        return "first_six_pages"

    return "first_six_pages"


def get_paper(paper_id: str) -> PaperDetailResponse | None:
    with session() as conn:
        paper = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if paper is None:
            return None
        attachments_rows = conn.execute("SELECT * FROM attachments WHERE paper_id = ? ORDER BY uploaded_at ASC", (paper_id,)).fetchall()
        metadata = conn.execute("SELECT * FROM paper_texts WHERE paper_id = ? AND text_scope = 'metadata'", (paper_id,)).fetchone()
        analysis = conn.execute("SELECT * FROM paper_analysis WHERE paper_id = ?", (paper_id,)).fetchone()
        # extraction_method: determine once, used in response
        extraction_method = _resolve_extraction_method(paper_id, analysis, metadata, paper["status"])

    attachments = [
        {
            "attachment_type": row["attachment_type"],
            "file_name": row["file_name"],
            "file_path": row["file_path"],
            "file_size": row["file_size"],
            "mime_type": row["mime_type"],
            "page_count": row["page_count"],
            "checksum": row["checksum"],
        }
        for row in attachments_rows
    ]
    metadata_payload = None
    if metadata is not None:
        sections = {}
        try:
            sections = __import__("json").loads(metadata["sections_json"] or "{}")
        except Exception:
            sections = {}

        metadata_source = sections.get("metadata", {}) if isinstance(sections, dict) else {}
        section_title = sections.get("title", "") if isinstance(sections, dict) else ""
        section_title_cn = sections.get("title_cn", "") if isinstance(sections, dict) else ""
        section_title_en = sections.get("title_en", "") if isinstance(sections, dict) else ""
        section_authors = sections.get("authors", "") if isinstance(sections, dict) else ""
        section_source = sections.get("source", "") if isinstance(sections, dict) else ""
        section_abstract = sections.get("abstract", "") if isinstance(sections, dict) else ""

        # --- Language-aware title extraction for metadata_payload ---
        # Build title_cn / title_en candidates from multiple sources, then
        # validate language. The old fallback chain included section_title
        # and paper["title"] (the main display title) which may be in the
        # opposite language, causing e.g. a Chinese title to leak into the
        # title_en field. We now only fall back to the main title if it
        # matches the expected language.
        from app.core.metadata_client import _detect_language as _detect_lang_meta
        _meta_title_cn_raw = paper["title_cn"] or metadata_source.get("title_cn", "") or section_title_cn or ""
        _meta_title_en_raw = paper["title_en"] or metadata_source.get("title_en", "") or section_title_en or ""
        _meta_main_title = paper["title"] or metadata["title_extracted"] or section_title or ""
        if not _meta_title_cn_raw and _meta_main_title and _detect_lang_meta(_meta_main_title) == "zh":
            _meta_title_cn_raw = _meta_main_title
        if not _meta_title_en_raw and _meta_main_title and _detect_lang_meta(_meta_main_title) == "en":
            _meta_title_en_raw = _meta_main_title
        # Validate: clear mismatched-language titles
        if _meta_title_cn_raw and _detect_lang_meta(_meta_title_cn_raw) != "zh":
            _meta_title_cn_raw = ""
        if _meta_title_en_raw and _detect_lang_meta(_meta_title_en_raw) != "en":
            _meta_title_en_raw = ""

        metadata_payload = MetadataCreate(
            metadata_status=metadata["parse_status"] or "pending",
            title_cn=_meta_title_cn_raw,
            title_en=_meta_title_en_raw,
            authors=paper["authors"] or metadata_source.get("authors", "") or section_authors or "",
            source=paper["source_url"] or metadata_source.get("source", "") or section_source or "",
            abstract=paper["abstract"] or metadata_source.get("abstract", "") or section_abstract or metadata["abstract_extracted"] or "",
            abstract_cn="",
            abstract_en="",
            keywords=metadata_source.get("keywords", "") or "",
            year=paper["publish_date"] or metadata_source.get("year", "") or "",
            doi=metadata_source.get("doi", "") or "",
            raw_json=metadata["sections_json"] or "",
            model_name=metadata_source.get("model_name", "deepseek") or "deepseek",
            prompt_version=metadata_source.get("prompt_version", "v1") or "v1",
            error_message=metadata["parse_error"] or metadata_source.get("error_message", "") or "",
        )
        # --- Language-aware abstract reconstruction ---
        # Reconstruct abstract_cn and abstract_en from stored metadata.
        # IMPORTANT: Do NOT use paper["abstract"] for language detection.
        # paper["abstract"] stores the "display" abstract which prefers the
        # Chinese version (abstract = abstract_cn or abstract_en). For English
        # papers this field holds the Chinese translation, so detecting its
        # language would incorrectly classify the paper as Chinese and clear
        # the English original abstract. Instead, rely on the language-specific
        # fields (abstract_cn, abstract_en) persisted in metadata_source,
        # which preserve the original language assignment from extraction.
        _abs_cn = metadata_source.get("abstract_cn", "") or ""
        _abs_en = metadata_source.get("abstract_en", "") or ""
        _abs_cn_valid = bool(_abs_cn) and _detect_lang_meta(_abs_cn) == "zh"
        _abs_en_valid = bool(_abs_en) and _detect_lang_meta(_abs_en) == "en"

        if _abs_en_valid:
            # English paper: abstract_en is the English original.
            # abstract_cn (if valid Chinese) is the Chinese translation.
            metadata_payload.abstract_en = _abs_en
            metadata_payload.abstract_cn = _abs_cn if _abs_cn_valid else ""
        elif _abs_cn_valid:
            # Chinese paper: abstract_cn is the Chinese original.
            # abstract_en should be empty (no translation needed for CN papers).
            metadata_payload.abstract_cn = _abs_cn
            metadata_payload.abstract_en = ""
        else:
            # No valid language-specific fields; fall back to raw abstract
            # with language detection as last resort.
            _raw_abs = paper["abstract"] or metadata_source.get("abstract", "") or section_abstract or metadata["abstract_extracted"] or ""
            if _raw_abs:
                _lang = _detect_lang_meta(_raw_abs)
                if _lang == "zh":
                    metadata_payload.abstract_cn = _raw_abs
                    metadata_payload.abstract_en = ""
                elif _lang == "en":
                    metadata_payload.abstract_en = _raw_abs
                    metadata_payload.abstract_cn = ""
        if metadata_payload.metadata_status == "pending" and not any(
            [
                metadata_payload.title_cn,
                metadata_payload.title_en,
                metadata_payload.authors,
                metadata_payload.source,
                metadata_payload.abstract,
                metadata_payload.abstract_cn,
                metadata_payload.abstract_en,
                metadata_payload.raw_json,
                metadata_payload.error_message,
            ]
        ):
            metadata_payload = None

    analysis_payload = None
    if analysis is not None:
        analysis_payload = AnalysisCreate(
            analysis_status=analysis["analysis_status"], tldr=analysis["tldr"] or "" if "tldr" in analysis.keys() else "",
            motivation=analysis["motivation"] or "", methodology=analysis["methodology"] or "",
            experiments=analysis["experiments"] or "", resources=analysis["resources"] or "", ablation=analysis["ablation"] or "",
            conclusion=analysis["conclusion"] or "", strengths=analysis["strengths"] or "", weaknesses=analysis["weaknesses"] or "",
            raw_json=analysis["raw_json"] or "", model_name=analysis["model_name"] or "", prompt_version=analysis["prompt_version"] or "v1",
            extraction_method=analysis["extraction_method"] if "extraction_method" in analysis.keys() else "first_six_pages",
            error_message=analysis["error_message"] or "",
        )
        if analysis_payload.analysis_status == "pending" and not any(
            [
                analysis_payload.tldr,
                analysis_payload.motivation,
                analysis_payload.methodology,
                analysis_payload.experiments,
                analysis_payload.resources,
                analysis_payload.ablation,
                analysis_payload.conclusion,
                analysis_payload.strengths,
                analysis_payload.weaknesses,
                analysis_payload.raw_json,
                analysis_payload.error_message,
            ]
        ):
            analysis_payload = None

    # --- Language-aware title validation for response ---
    # Validate that title_cn is actually Chinese and title_en is actually
    # English. Clear mismatched fields to prevent display confusion (e.g.,
    # a Chinese title appearing in the "English title" slot, which causes
    # the frontend to render duplicate Chinese titles).
    from app.core.metadata_client import _detect_language as _detect_lang
    _resp_title = paper["title"] or ""
    _resp_title_cn = paper["title_cn"] or ""
    _resp_title_en = paper["title_en"] or ""
    if _resp_title_cn and _detect_lang(_resp_title_cn) != "zh":
        _resp_title_cn = ""
    if _resp_title_en and _detect_lang(_resp_title_en) != "en":
        _resp_title_en = ""

    return PaperDetailResponse(
        id=paper["id"], title=_resp_title, title_cn=_resp_title_cn, title_en=_resp_title_en,
        authors=paper["authors"] or "", publish_date=paper["publish_date"] or "", abstract=paper["abstract"] or "",
        source_url=paper["source_url"] or "", status=paper["status"] or "uploaded",
        folder_id=paper["folder_id"] if "folder_id" in paper.keys() else None,
        created_at=to_utc_isoformat(paper["created_at"]),
        updated_at=to_utc_isoformat(paper["updated_at"]),
        extraction_method=extraction_method,
        attachments=attachments, metadata=metadata_payload, analysis=analysis_payload,
    )


def upsert_attachment_file(paper_id: str, attachment_type: str, source_path: str, file_name: str | None = None) -> str:
    if attachment_type not in ALLOWED_ATTACHMENT_TYPES:
        raise ValueError(f"Invalid attachment type: {attachment_type}")
    stored_path, file_size = store_attachment_file(paper_id, attachment_type, source_path)
    attachment_id = str(uuid4())
    final_name = file_name or Path(source_path).name
    with session() as conn:
        conn.execute(
            """
            INSERT INTO attachments (
                id, paper_id, attachment_type, file_name, file_path,
                file_size, mime_type, page_count, checksum
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, attachment_type) DO UPDATE SET
                id=excluded.id,
                file_name=excluded.file_name,
                file_path=excluded.file_path,
                file_size=excluded.file_size
            """,
            (attachment_id, paper_id, attachment_type, final_name, str(stored_path), file_size, "application/pdf", None, None),
        )
        # 仅原件上传才推进解析状态；上传翻译件/对应件不应回退已完成的状态
        if attachment_type == "original":
            conn.execute("UPDATE papers SET status = 'parsed', updated_at = CURRENT_TIMESTAMP WHERE id = ?", (paper_id,))
    return attachment_id


def delete_attachment_file(paper_id: str, attachment_type: str) -> bool:
    with session() as conn:
        row = conn.execute("SELECT file_path FROM attachments WHERE paper_id = ? AND attachment_type = ?", (paper_id, attachment_type)).fetchone()
        if row is None:
            return False
        file_path = resolve_attachment_path(paper_id, row["file_path"])
        conn.execute("DELETE FROM attachments WHERE paper_id = ? AND attachment_type = ?", (paper_id, attachment_type))
    file_path.unlink(missing_ok=True)
    return True


# ========== Paper Annotations ==========
# Annotations (highlights / shapes / freetext notes / drawings) are stored as a
# JSON array per (paper_id, attachment_type). Each item is a highlight object
# whose position uses PDF-page-relative normalized coordinates (ScaledPosition),
# so annotations remain attached to the correct content across zoom/scroll.

def get_paper_annotations(paper_id: str, attachment_type: str = "original") -> dict:
    """Return stored annotations for a paper + attachment type.

    Returns a dict with paper_id, attachment_type, annotations (list) and
    updated_at. If no row exists yet, returns an empty annotations list so the
    frontend can initialize cleanly.
    """
    if attachment_type not in ALLOWED_ATTACHMENT_TYPES:
        raise ValueError(f"Invalid attachment type: {attachment_type}")
    with session() as conn:
        row = conn.execute(
            "SELECT annotations_json, updated_at FROM paper_annotations WHERE paper_id = ? AND attachment_type = ?",
            (paper_id, attachment_type),
        ).fetchone()
    if row is None:
        return {"paper_id": paper_id, "attachment_type": attachment_type, "annotations": [], "updated_at": ""}
    try:
        annotations = json.loads(row["annotations_json"] or "[]")
        if not isinstance(annotations, list):
            annotations = []
    except (ValueError, TypeError):
        annotations = []
    return {
        "paper_id": paper_id,
        "attachment_type": attachment_type,
        "annotations": annotations,
        "updated_at": to_utc_isoformat(row["updated_at"]),
    }


def save_paper_annotations(paper_id: str, attachment_type: str, annotations: list) -> dict:
    """Upsert the full annotation list for a paper + attachment type.

    The frontend sends the entire current annotation array on each save; this
    replaces the stored JSON wholesale (idempotent, no per-item diffing needed).
    """
    if attachment_type not in ALLOWED_ATTACHMENT_TYPES:
        raise ValueError(f"Invalid attachment type: {attachment_type}")
    annotations_json = json.dumps(annotations or [], ensure_ascii=False)
    annotation_id = str(uuid4())
    with session() as conn:
        # 单条 upsert：依赖唯一索引 ux_paper_annotations(paper_id, attachment_type)，
        # 避免「先查后插」在并发保存时插入重复行。
        conn.execute(
            """
            INSERT INTO paper_annotations (id, paper_id, attachment_type, annotations_json)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(paper_id, attachment_type) DO UPDATE SET
                annotations_json = excluded.annotations_json,
                updated_at = CURRENT_TIMESTAMP
            """,
            (annotation_id, paper_id, attachment_type, annotations_json),
        )
    return get_paper_annotations(paper_id, attachment_type)



def _get_existing_mineru_markdown(paper_id: str) -> Path | None:
    """Return path to an existing MinerU Markdown file for the paper, or None.

    Checks the database for a completed ``text_scope='mineru'`` record and
    verifies the Markdown file still exists on disk.

    Args:
        paper_id: Paper ID to check.

    Returns:
        Path to ``full.md`` if a usable MinerU result exists, else None.
    """
    with session() as conn:
        row = conn.execute(
            "SELECT sections_json FROM paper_texts WHERE paper_id = ? AND text_scope = 'mineru' AND parse_status = 'done'",
            (paper_id,),
        ).fetchone()

    if row is None:
        return None

    import json as _json
    try:
        sections = _json.loads(row["sections_json"] or "{}")
    except Exception:
        return None

    md_path_str = sections.get("markdown_path", "")
    if not md_path_str:
        return None

    md_path = Path(md_path_str)
    if not md_path.exists():
        return None

    return md_path


def _parse_with_mineru_or_ocr(
    paper_id: str,
    pdf_path: str,
    force_mineru_refresh: bool,
) -> dict[str, str]:
    """Parse a PDF into a text dict, preferring MinerU Markdown over OCR.

    Strategy:
    1. If ``force_mineru_refresh`` is False and an existing MinerU Markdown
       is found, reuse it (saves API quota and compute).
    2. Otherwise, if a MinerU token is configured:
       - Attempt fresh MinerU conversion synchronously.
       - On success → return Markdown text. On failure → fall back to OCR.
    3. If MinerU was not attempted (no token, or failed), use the local
       OCR-based ``extract_pdf_text``.

    Args:
        paper_id: Paper ID for tracking and storage.
        pdf_path: Path to the original PDF file.
        force_mineru_refresh: If True, ignore existing MinerU result and
            re-run the conversion.

    Returns:
        Parsed text dict compatible with ``extract_pdf_text`` output.
    """
    _update_paper_status(paper_id, PAPER_STATUS_TEXT_EXTRACTING, reason="开始文本提取")

    # 1. Reuse existing MinerU Markdown when available
    if not force_mineru_refresh:
        existing_md = _get_existing_mineru_markdown(paper_id)
        if existing_md is not None:
            logger.info("paper_reuse_mineru_markdown paper_id=%s path=%s", paper_id, existing_md)
            log_task_event(
                paper_id, step="检查已有 MinerU 结果", api="storage.get_existing_mineru_markdown",
                status="success", detail=f"发现已有 Markdown: {existing_md.name}",
            )
            log_task_event(
                paper_id, step="复用 MinerU 解析结果", api="storage.reuse_markdown",
                status="success", detail=f"复用 {existing_md.name}",
            )
            _update_paper_status(
                paper_id,
                PAPER_STATUS_MINERU_CONVERTED,
                reason="reused existing mineru markdown (compute saved)",
            )
            append_debug_record(
                paper_id,
                "reuse_mineru_markdown",
                markdown_path=str(existing_md),
            )
            t0 = task_log_timer()
            parsed = extract_text_from_markdown(existing_md)
            elapsed = int((task_log_timer() - t0) * 1000)
            log_task_event(
                paper_id, step="提取 Markdown 文本", api="pdf.extract_text_from_markdown",
                status="success", duration_ms=elapsed,
                detail=f"提取 {len(parsed.get('raw_text', ''))} 字符",
            )
            parsed["paper_id"] = paper_id
            return parsed

    # 2. Attempt fresh MinerU conversion
    from app.core.config import settings
    if settings.mineru_token:
        _update_paper_status(
            paper_id,
            PAPER_STATUS_MINERU_PROCESSING,
            reason=f"starting mineru conversion (force_refresh={force_mineru_refresh})",
        )
        log_task_event(
            paper_id, step="MinerU 大模型解析", api="mineru.convert_pdf_to_markdown",
            status="running", detail=f"force_refresh={force_mineru_refresh}",
        )
        try:
            from app.services.mineru_service import convert_pdf_to_markdown
            logger.info("paper_mineru_convert_attempt paper_id=%s", paper_id)
            append_debug_record(paper_id, "mineru_convert_attempt", pdf_path=pdf_path)
            t0 = task_log_timer()
            result = convert_pdf_to_markdown(
                paper_id=paper_id,
                pdf_path=pdf_path,
                enable_ocr=False,
            )
            elapsed = int((task_log_timer() - t0) * 1000)
            md_path = Path(result.get("markdown_path", ""))
            if md_path.exists():
                logger.info("paper_mineru_convert_done paper_id=%s path=%s", paper_id, md_path)
                log_task_update(
                    paper_id, step="MinerU 大模型解析", api="mineru.convert_pdf_to_markdown",
                    status="success", duration_ms=elapsed,
                    detail=f"Markdown 已生成: {md_path.name}",
                )
                _update_paper_status(
                    paper_id,
                    PAPER_STATUS_MINERU_CONVERTED,
                    reason="conversion succeeded",
                )
                log_task_event(
                    paper_id, step="提取 Markdown 文本", api="pdf.extract_text_from_markdown",
                    status="running",
                )
                t0 = task_log_timer()
                parsed = extract_text_from_markdown(md_path)
                elapsed = int((task_log_timer() - t0) * 1000)
                log_task_update(
                    paper_id, step="提取 Markdown 文本", api="pdf.extract_text_from_markdown",
                    status="success", duration_ms=elapsed,
                    detail=f"提取 {len(parsed.get('raw_text', ''))} 字符",
                )
                parsed["paper_id"] = paper_id
                return parsed
            # MinerU finished but markdown file missing → treat as failure
            logger.warning("paper_mineru_no_markdown paper_id=%s result=%s", paper_id, result)
            log_task_update(
                paper_id, step="MinerU 大模型解析", api="mineru.convert_pdf_to_markdown",
                status="failed", duration_ms=elapsed,
                detail="MinerU 完成但未生成 Markdown 文件",
                fallback=True,
            )
            _update_paper_status(
                paper_id,
                PAPER_STATUS_OCR_FALLBACK,
                reason=f"mineru completed but no markdown file. result={str(result)[:120]}",
            )
        except Exception as exc:
            logger.warning("paper_mineru_failed paper_id=%s error=%s", paper_id, exc)
            elapsed = 0
            log_task_update(
                paper_id, step="MinerU 大模型解析", api="mineru.convert_pdf_to_markdown",
                status="failed", duration_ms=elapsed,
                detail=f"{type(exc).__name__}: {str(exc)[:200]}",
                fallback=True,
                error=str(exc)[:300],
            )
            _update_paper_status(
                paper_id,
                PAPER_STATUS_OCR_FALLBACK,
                reason=f"mineru failed with exception: {type(exc).__name__}: {str(exc)[:200]}",
            )
            append_debug_record(paper_id, "mineru_convert_failed_fallback_ocr", error=str(exc))
    else:
        logger.info("paper_mineru_skipped_no_token paper_id=%s", paper_id)
        log_task_update(
            paper_id, step="MinerU 大模型解析", api="mineru.convert_pdf_to_markdown",
            status="skipped", detail="MinerU Token 未配置", fallback=True,
        )
        _update_paper_status(
            paper_id,
            PAPER_STATUS_OCR_FALLBACK,
            reason="mineru token not configured",
        )
        append_debug_record(paper_id, "mineru_skipped_no_token")

    # 3. Fall back to OCR (synchronous)
    logger.info("paper_fallback_ocr paper_id=%s", paper_id)
    log_task_event(
        paper_id, step="OCR 文本提取（降级）", api="pdf.extract_pdf_text",
        status="running", detail="使用 OCR 作为降级方案", fallback=True,
    )
    append_debug_record(
        paper_id,
        "fallback_ocr",
        pdf_path=pdf_path,
    )
    t0 = task_log_timer()
    parsed = extract_pdf_text(pdf_path)
    elapsed = int((task_log_timer() - t0) * 1000)
    log_task_update(
        paper_id, step="OCR 文本提取（降级）", api="pdf.extract_pdf_text",
        status="success", duration_ms=elapsed,
        detail=f"提取 {len(parsed.get('raw_text', ''))} 字符",
        fallback=True,
    )
    parsed["paper_id"] = paper_id
    return parsed


def run_analysis_exclusive(
    paper_id: str,
    original_attachment_path: str,
    force_mineru_refresh: bool = False,
) -> None:
    """独占地运行一次分析，避免同一论文的分析任务并发执行。

    若该论文已有分析在跑，则直接跳过本次（后台任务重复触发属正常情况）。
    """
    from app.core.paper_lock import release, try_acquire

    if not try_acquire(paper_id):
        logger.info("paper_analysis_skipped_already_running paper_id=%s", paper_id)
        log_task_event(
            paper_id, step="跳过重复分析", api="run_analysis_exclusive",
            status="skipped", detail="该论文已有分析任务在执行",
        )
        return
    try:
        auto_parse_and_analyze(paper_id, original_attachment_path, force_mineru_refresh)
    finally:
        release(paper_id)


def auto_parse_and_analyze(
    paper_id: str,
    original_attachment_path: str,
    force_mineru_refresh: bool = False,
) -> str:
    # Import at function top to avoid UnboundLocalError when the duplicate
    # detection branch (which also imports this) is not entered.
    from app.core.metadata_client import _detect_language, detect_paper_language

    logger.info(
        "paper_auto_parse_start paper_id=%s path=%s force_mineru_refresh=%s",
        paper_id,
        original_attachment_path,
        force_mineru_refresh,
    )
    # Clear stale task logs from previous runs
    clear_task_logs(paper_id)
    log_task_event(
        paper_id, step="开始分析任务", api="auto_parse_and_analyze",
        status="running", detail=f"force_mineru_refresh={force_mineru_refresh}",
    )
    append_debug_record(
        paper_id,
        "auto_parse_start",
        original_attachment_path=original_attachment_path,
        force_mineru_refresh=force_mineru_refresh,
    )

    log_task_event(
        paper_id, step="PDF 文档解析", api="mineru/ocr",
        status="running",
    )
    parsed = _parse_with_mineru_or_ocr(paper_id, original_attachment_path, force_mineru_refresh)
    logger.info(
        "paper_auto_parse_parsed paper_id=%s title=%r title_cn=%r title_en=%r authors=%r abstract_len=%s extraction_method=%s text_quality=%s",
        paper_id,
        parsed.get("title", "")[:120],
        parsed.get("title_cn", "")[:120],
        parsed.get("title_en", "")[:120],
        parsed.get("authors", "")[:120],
        len(parsed.get("abstract", "")),
        parsed.get("extraction_method", ""),
        parsed.get("text_quality", ""),
    )
    append_debug_record(paper_id, "auto_parse_parsed", parsed=parsed)
    log_task_update(
        paper_id, step="PDF 文档解析", api="mineru/ocr",
        status="success", detail=f"使用 {parsed.get('extraction_method', 'ocr')} 提取文本",
    )

    # Metadata extraction phase
    _update_paper_status(paper_id, PAPER_STATUS_METADATA_EXTRACTING, reason="开始元数据提取")
    log_task_event(
        paper_id, step="元数据与摘要提取", api="deepseek.metadata_extract",
        status="running",
    )
    t_meta = task_log_timer()
    metadata_payload = build_metadata_payload(parsed)
    meta_elapsed = int((task_log_timer() - t_meta) * 1000)
    metadata_status = metadata_payload.get("metadata_status", "done")
    meta_api = metadata_payload.get("model_name", "deepseek")
    meta_detail_parts = []
    if metadata_payload.get("title_en"):
        meta_detail_parts.append(f"title_en={metadata_payload['title_en'][:40]}")
    if metadata_payload.get("title_cn"):
        meta_detail_parts.append(f"title_cn={metadata_payload['title_cn'][:30]}")
    abstract_len = len(metadata_payload.get("abstract", ""))
    meta_detail_parts.append(f"abstract_len={abstract_len}")
    meta_detail = "; ".join(meta_detail_parts)

    if metadata_status == "done":
        log_task_update(
            paper_id, step="元数据与摘要提取", api=f"deepseek.{meta_api}",
            status="success", duration_ms=meta_elapsed,
            detail=meta_detail,
        )
    elif metadata_status == "failed":
        log_task_update(
            paper_id, step="元数据与摘要提取", api=f"deepseek.{meta_api}",
            status="failed", duration_ms=meta_elapsed,
            error=metadata_payload.get("metadata_error_message", "")[:300],
        )
    else:
        log_task_update(
            paper_id, step="元数据与摘要提取", api=f"deepseek.{meta_api}",
            status="running", duration_ms=meta_elapsed,
            detail=meta_detail,
        )

    # Duplicate detection phase
    log_task_event(
        paper_id, step="重复文献检测", api="db.check_duplicate",
        status="running",
    )
    duplicate_candidates = check_duplicate_paper(
        title=parsed.get("title", ""),
        title_cn=metadata_payload.get("title_cn", ""),
        title_en=metadata_payload.get("title_en", ""),
        authors=metadata_payload.get("authors", ""),
        keywords=metadata_payload.get("keywords", ""),
        doi=metadata_payload.get("doi", ""),
        exclude_paper_id=paper_id,
    )

    if duplicate_candidates:
        logger.info(
            "paper_duplicate_detected paper_id=%s candidates_count=%s",
            paper_id, len(duplicate_candidates),
        )
        log_task_update(
            paper_id, step="重复文献检测", api="db.check_duplicate",
            status="warning",
            detail=f"发现 {len(duplicate_candidates)} 篇可能重复的文献",
        )

        # Save parsed data and metadata to DB for later resume
        # Language-aware title selection: detect paper language from the FULL
        # raw text (body), not just the abstract_region. Chinese papers often
        # have both Chinese and English abstracts; detecting from abstract_region
        # alone can misclassify a Chinese paper as English and display the
        # English title as primary. detect_paper_language() samples the full
        # raw_text so the body text (which is in the paper's native language)
        # dominates the CJK ratio.
        _paper_lang = detect_paper_language(parsed)
        if _paper_lang == "en":
            title = metadata_payload.get("title_en", "").strip() or metadata_payload.get("title_cn", "").strip() or parsed.get("title", "").strip()
        else:
            # Chinese paper (or unknown): prefer Chinese title
            title = metadata_payload.get("title_cn", "").strip() or metadata_payload.get("title_en", "").strip() or parsed.get("title", "").strip()
        title_cn = metadata_payload.get("title_cn", "").strip() or parsed.get("title_cn", "").strip()
        title_en = metadata_payload.get("title_en", "").strip() or parsed.get("title_en", "").strip()
        authors = metadata_payload.get("authors", "").strip() or parsed.get("authors", "").strip()
        source = metadata_payload.get("source", "").strip() or parsed.get("source", "").strip()
        abstract = metadata_payload.get("abstract", "").strip() or parsed.get("abstract", "").strip()
        raw_text = parsed.get("raw_text", "")
        extraction_method = parsed.get("extraction_method", "ocr")
        text_quality = parsed.get("text_quality", "")
        metadata_raw = metadata_payload.get("raw_json", "")

        # Save partial data to DB
        with session() as conn:
            conn.execute(
                """
                UPDATE papers
                SET
                    title = COALESCE(NULLIF(?, ''), title),
                    title_cn = ?,
                    title_en = ?,
                    authors = COALESCE(NULLIF(?, ''), authors),
                    abstract = COALESCE(NULLIF(?, ''), abstract),
                    source_url = COALESCE(NULLIF(?, ''), source_url),
                    status = 'duplicate_detected',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
                """,
                (title, title_cn, title_en, authors, abstract, source, paper_id),
            )
            conn.execute(
                """
                INSERT INTO paper_texts (
                    id, paper_id, text_scope, title_extracted, abstract_extracted,
                    sections_json, raw_text, parse_status, parse_error,
                    extraction_method
                ) VALUES (?, ?, 'metadata', ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(paper_id, text_scope) DO UPDATE SET
                    title_extracted=excluded.title_extracted,
                    abstract_extracted=excluded.abstract_extracted,
                    sections_json=excluded.sections_json,
                    raw_text=excluded.raw_text,
                    parse_status=excluded.parse_status,
                    parse_error=excluded.parse_error,
                    extraction_method=excluded.extraction_method,
                    updated_at=CURRENT_TIMESTAMP
                """,
                (
                    str(uuid4()),
                    paper_id,
                    title,
                    abstract,
                    json.dumps(
                        {
                            "extraction_method": extraction_method,
                            "text_quality": text_quality,
                            "title": title,
                            "title_cn": title_cn,
                            "title_en": title_en,
                            "authors": authors,
                            "source": source,
                            "metadata": metadata_payload,
                            "duplicate_candidates": duplicate_candidates,
                        },
                        ensure_ascii=False,
                    ),
                    raw_text,
                    metadata_payload.get("metadata_status", "done"),
                    metadata_payload.get("metadata_error_message", ""),
                    extraction_method,
                ),
            )

        _update_paper_status(paper_id, PAPER_STATUS_DUPLICATE_DETECTED, reason=f"发现 {len(duplicate_candidates)} 篇可能重复的文献")
        log_task_event(
            paper_id, step="等待用户确认", api="auto_parse_and_analyze",
            status="waiting", detail=f"发现 {len(duplicate_candidates)} 篇可能重复的文献，等待用户确认",
        )
        return paper_id  # Pause here - frontend will show duplicate dialog

    # No duplicates found, continue with analysis
    log_task_update(
        paper_id, step="重复文献检测", api="db.check_duplicate",
        status="success",
        detail="未检测到重复文献",
    )

    # Analysis phase
    _update_paper_status(paper_id, PAPER_STATUS_ANALYZING, reason="开始八维分析")
    log_task_event(
        paper_id, step="八维深度分析", api="deepseek.analysis",
        status="running",
    )
    t_analysis = task_log_timer()
    analysis_payload = build_analysis_payload(parsed)
    analysis_elapsed = int((task_log_timer() - t_analysis) * 1000)
    analysis_status = analysis_payload.get("analysis_status", "done")
    analysis_api = analysis_payload.get("model_name", "deepseek")
    analysis_dimensions = [
        k for k in ["motivation", "methodology", "experiments", "resources",
                     "ablation", "conclusion", "strengths", "weaknesses"]
        if analysis_payload.get(k)
    ]
    analysis_detail = f"完成维度: {len(analysis_dimensions)}/8"
    if analysis_dimensions:
        analysis_detail += f" ({', '.join(analysis_dimensions[:4])}...)"

    if analysis_status == "done":
        log_task_update(
            paper_id, step="八维深度分析", api=f"deepseek.{analysis_api}",
            status="success", duration_ms=analysis_elapsed,
            detail=analysis_detail,
        )
    elif analysis_status == "failed":
        log_task_update(
            paper_id, step="八维深度分析", api=f"deepseek.{analysis_api}",
            status="failed", duration_ms=analysis_elapsed,
            error=analysis_payload.get("error_message", "")[:300],
        )
    else:
        log_task_update(
            paper_id, step="八维深度分析", api=f"deepseek.{analysis_api}",
            status="running", duration_ms=analysis_elapsed,
            detail=analysis_detail,
        )

    logger.info(
        "paper_auto_parse_payload paper_id=%s metadata_status=%s metadata_title_cn=%r metadata_title_en=%r metadata_authors=%r metadata_abstract_len=%s analysis_status=%s",
        paper_id,
        metadata_payload.get("metadata_status", ""),
        metadata_payload.get("title_cn", "")[:120],
        metadata_payload.get("title_en", "")[:120],
        metadata_payload.get("authors", "")[:120],
        len(metadata_payload.get("abstract", "")),
        analysis_payload.get("analysis_status", ""),
    )
    append_debug_record(paper_id, "auto_parse_payload", metadata_payload=metadata_payload, analysis_payload=analysis_payload)

    # Language-aware title selection: detect paper language from the FULL
    # raw text (body), not just the abstract_region. See the duplicate
    # detection branch above for rationale.
    _paper_lang = detect_paper_language(parsed)
    if _paper_lang == "en":
        title = metadata_payload.get("title_en", "").strip() or metadata_payload.get("title_cn", "").strip() or parsed.get("title", "").strip()
    else:
        title = metadata_payload.get("title_cn", "").strip() or metadata_payload.get("title_en", "").strip() or parsed.get("title", "").strip()
    title_cn = metadata_payload.get("title_cn", "").strip() or parsed.get("title_cn", "").strip()
    title_en = metadata_payload.get("title_en", "").strip() or parsed.get("title_en", "").strip()
    authors = metadata_payload.get("authors", "").strip() or parsed.get("authors", "").strip()
    source = metadata_payload.get("source", "").strip() or parsed.get("source", "").strip()
    abstract = metadata_payload.get("abstract", "").strip() or parsed.get("abstract", "").strip()
    raw_text = parsed.get("raw_text", "")
    extraction_method = parsed.get("extraction_method", "ocr")
    text_quality = parsed.get("text_quality", "")
    metadata_raw = metadata_payload.get("raw_json", "")
    analysis_raw = analysis_payload.get("raw_json", "")

    _update_paper_status(paper_id, PAPER_STATUS_PARSED, reason="文本提取和分析完成，写入数据库")
    log_task_event(
        paper_id, step="写入解析结果", api="db.insert",
        status="running",
        detail=f"title={title[:60]}, abstract_len={len(abstract)}",
    )

    with session() as conn:
        logger.info(
            "paper_auto_parse_write paper_id=%s title=%r title_cn=%r title_en=%r authors=%r abstract_len=%s source=%r",
            paper_id,
            title[:120],
            title_cn[:120],
            title_en[:120],
            authors[:120],
            len(abstract),
            source[:120],
        )
        append_debug_record(
            paper_id,
            "auto_parse_write",
            title=title,
            title_cn=title_cn,
            title_en=title_en,
            authors=authors,
            source=source,
            abstract=abstract,
            metadata_raw=metadata_raw,
            analysis_raw=analysis_raw,
        )
        conn.execute(
            """
            UPDATE papers
            SET
                title = COALESCE(NULLIF(?, ''), title),
                title_cn = ?,
                title_en = ?,
                authors = COALESCE(NULLIF(?, ''), authors),
                abstract = COALESCE(NULLIF(?, ''), abstract),
                source_url = COALESCE(NULLIF(?, ''), source_url),
                status = 'parsed',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (title, title_cn, title_en, authors, abstract, source, paper_id),
        )
        conn.execute(
            """
            INSERT INTO paper_texts (
                id, paper_id, text_scope, title_extracted, abstract_extracted,
                body_extracted, sections_json, raw_text, parse_status, parse_error,
                extraction_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, text_scope) DO UPDATE SET
                title_extracted=excluded.title_extracted,
                abstract_extracted=excluded.abstract_extracted,
                body_extracted=excluded.body_extracted,
                sections_json=excluded.sections_json,
                raw_text=excluded.raw_text,
                parse_status=excluded.parse_status,
                parse_error=excluded.parse_error,
                extraction_method=excluded.extraction_method,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                str(uuid4()),
                paper_id,
                "metadata",
                title,
                abstract,
                parsed.get("first_pages_text", ""),
                __import__("json").dumps(
                    {
                        "extraction_method": extraction_method,
                        "text_quality": text_quality,
                        "title": title,
                        "title_cn": title_cn,
                        "title_en": title_en,
                        "authors": authors,
                        "source": source,
                        "metadata": metadata_payload,
                    },
                    ensure_ascii=False,
                ),
                raw_text,
                metadata_payload.get("metadata_status", "done"),
                metadata_payload.get("metadata_error_message", ""),
                extraction_method,
            ),
        )
        conn.execute(
            """
            INSERT INTO paper_texts (
                id, paper_id, text_scope, title_extracted, abstract_extracted,
                body_extracted, sections_json, raw_text, parse_status, parse_error,
                extraction_method
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, text_scope) DO UPDATE SET
                title_extracted=excluded.title_extracted,
                abstract_extracted=excluded.abstract_extracted,
                body_extracted=excluded.body_extracted,
                sections_json=excluded.sections_json,
                raw_text=excluded.raw_text,
                parse_status=excluded.parse_status,
                parse_error=excluded.parse_error,
                extraction_method=excluded.extraction_method,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                str(uuid4()),
                paper_id,
                "analysis",
                title,
                abstract,
                parsed.get("full_text", ""),
                __import__("json").dumps(
                    {
                        "extraction_method": extraction_method,
                        "text_quality": text_quality,
                        "analysis": analysis_payload,
                        "metadata_raw": metadata_raw,
                        "analysis_raw": analysis_raw,
                    },
                    ensure_ascii=False,
                ),
                raw_text,
                analysis_payload.get("analysis_status", "done"),
                analysis_payload.get("error_message", ""),
                extraction_method,
            ),
        )
    merged_analysis = AnalysisCreate(**analysis_payload)
    if metadata_raw and not merged_analysis.raw_json:
        merged_analysis.raw_json = analysis_raw
    return upsert_analysis(paper_id, merged_analysis)


def ensure_analysis_placeholder(paper_id: str) -> None:
    with session() as conn:
        conn.execute(
            """
            INSERT INTO paper_analysis (
                id, paper_id, analysis_status, prompt_version
            ) VALUES (?, ?, ?, ?)
            ON CONFLICT(paper_id) DO NOTHING
            """,
            (str(uuid4()), paper_id, "pending", "v1"),
        )


def upsert_analysis(paper_id: str, analysis: AnalysisCreate) -> str:
    analysis_id = str(uuid4())
    with session() as conn:
        conn.execute(
            """
            INSERT INTO paper_analysis (
                id, paper_id, analysis_status, tldr, motivation, methodology, experiments,
                resources, ablation, conclusion, strengths, weaknesses, raw_json,
                model_name, prompt_version, extraction_method, error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id) DO UPDATE SET
                analysis_status=excluded.analysis_status,
                tldr=excluded.tldr,
                motivation=excluded.motivation,
                methodology=excluded.methodology,
                experiments=excluded.experiments,
                resources=excluded.resources,
                ablation=excluded.ablation,
                conclusion=excluded.conclusion,
                strengths=excluded.strengths,
                weaknesses=excluded.weaknesses,
                raw_json=excluded.raw_json,
                model_name=excluded.model_name,
                prompt_version=excluded.prompt_version,
                extraction_method=excluded.extraction_method,
                error_message=excluded.error_message,
                updated_at=CURRENT_TIMESTAMP
            """,
            (analysis_id, paper_id, analysis.analysis_status, analysis.tldr, analysis.motivation, analysis.methodology, analysis.experiments, analysis.resources, analysis.ablation, analysis.conclusion, analysis.strengths, analysis.weaknesses, analysis.raw_json, analysis.model_name, analysis.prompt_version, analysis.extraction_method, analysis.error_message),
        )
    # IMPORTANT: _update_paper_status opens its own DB session. It must be
    # called OUTSIDE the with-session block above, otherwise SQLite deadlocks
    # (writer waiting for another writer to commit, which can't happen until
    # this call returns).
    _update_paper_status(paper_id, PAPER_STATUS_DONE, reason="分析完成")
    log_task_update(
        paper_id, step="写入解析结果", api="db.insert",
        status="success", detail="论文解析和分析结果已写入数据库",
    )
    log_task_update(
        paper_id, step="开始分析任务", api="auto_parse_and_analyze",
        status="success", detail="分析任务全部完成",
    )

    # Final task log
    if analysis.error_message:
        log_task_event(
            paper_id, step="分析完成", api="auto_parse_and_analyze",
            status="completed",
            detail=f"分析有警告: {analysis.error_message[:100]}",
        )
    else:
        log_task_event(
            paper_id, step="分析完成", api="auto_parse_and_analyze",
            status="success", detail="所有步骤执行完成",
        )
    return analysis_id


def delete_paper(paper_id: str) -> bool:
    from app.core.config import settings

    with session() as conn:
        paper = conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if paper is None:
            return False
        attachments = conn.execute("SELECT file_path FROM attachments WHERE paper_id = ?", (paper_id,)).fetchall()
        for row in attachments:
            file_path = Path(row["file_path"])
            if file_path.exists():
                file_path.unlink(missing_ok=True)
        # Remove all workspace artifacts for this paper
        storage_dir = settings.workspace_dir / "storage" / paper_id
        if storage_dir.exists():
            shutil.rmtree(storage_dir, ignore_errors=True)
        (settings.workspace_dir / "task_logs" / f"{paper_id}.jsonl").unlink(missing_ok=True)
        for debug_file in (settings.workspace_dir / "debug_logs").glob(f"*_{paper_id}.jsonl"):
            debug_file.unlink(missing_ok=True)
        # ON DELETE CASCADE handles attachments / paper_texts / paper_analysis / paper_read_state
        conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
    return True


def update_paper(paper_id: str, payload: PaperUpdate) -> dict:
    import json
    
    with session() as conn:
        paper = conn.execute("SELECT id FROM papers WHERE id = ?", (paper_id,)).fetchone()
        if paper is None:
            raise ValueError("Paper not found")
        
        paper_updated = False
        metadata_updated = False
        analysis_updated = False
        
        # 1. 更新 papers 表的基础字段
        updates = []
        params = []
        if payload.title is not None:
            updates.append("title = ?")
            params.append(payload.title)
        if payload.title_cn is not None:
            updates.append("title_cn = ?")
            params.append(payload.title_cn)
        if payload.title_en is not None:
            updates.append("title_en = ?")
            params.append(payload.title_en)
        if payload.authors is not None:
            updates.append("authors = ?")
            params.append(payload.authors)
        if payload.publish_date is not None:
            updates.append("publish_date = ?")
            params.append(payload.publish_date)
        if payload.abstract is not None:
            updates.append("abstract = ?")
            params.append(payload.abstract)
        if payload.source_url is not None:
            updates.append("source_url = ?")
            params.append(payload.source_url)
        if updates:
            updates.append("updated_at = CURRENT_TIMESTAMP")
            params.append(paper_id)
            conn.execute(f"UPDATE papers SET {', '.join(updates)} WHERE id = ?", params)
            paper_updated = True
        
        # 2. 更新 paper_texts 表的 metadata 扩展字段
        metadata_fields_to_update = {}
        if payload.source is not None:
            metadata_fields_to_update["source"] = payload.source
        if payload.abstract_cn is not None:
            metadata_fields_to_update["abstract_cn"] = payload.abstract_cn
        if payload.abstract_en is not None:
            metadata_fields_to_update["abstract_en"] = payload.abstract_en
        if payload.keywords is not None:
            metadata_fields_to_update["keywords"] = payload.keywords
        if payload.year is not None:
            metadata_fields_to_update["year"] = payload.year
        if payload.doi is not None:
            metadata_fields_to_update["doi"] = payload.doi
        
        # 同时同步基础字段到 metadata
        if payload.title_cn is not None:
            metadata_fields_to_update["title_cn"] = payload.title_cn
        if payload.title_en is not None:
            metadata_fields_to_update["title_en"] = payload.title_en
        if payload.authors is not None:
            metadata_fields_to_update["authors"] = payload.authors
        if payload.abstract is not None:
            metadata_fields_to_update["abstract"] = payload.abstract
        
        if metadata_fields_to_update:
            metadata_row = conn.execute(
                "SELECT * FROM paper_texts WHERE paper_id = ? AND text_scope = 'metadata'",
                (paper_id,)
            ).fetchone()
            
            if metadata_row:
                # 解析现有的 sections_json
                sections = {}
                try:
                    sections = json.loads(metadata_row["sections_json"] or "{}")
                except Exception:
                    sections = {}
                
                # 更新 metadata 字段
                metadata_source = sections.get("metadata", {})
                metadata_source.update(metadata_fields_to_update)
                sections["metadata"] = metadata_source
                
                # 更新顶层字段
                if "title_cn" in metadata_fields_to_update:
                    sections["title_cn"] = metadata_fields_to_update["title_cn"]
                if "title_en" in metadata_fields_to_update:
                    sections["title_en"] = metadata_fields_to_update["title_en"]
                if "abstract" in metadata_fields_to_update:
                    sections["abstract"] = metadata_fields_to_update["abstract"]
                
                new_sections_json = json.dumps(sections, ensure_ascii=False)
                
                # 构造更新语句
                meta_updates = ["sections_json = ?"]
                meta_params = [new_sections_json]
                
                # 更新 title_extracted 和 abstract_extracted（如果相关字段被修改）
                if "title_cn" in metadata_fields_to_update or "title_en" in metadata_fields_to_update:
                    meta_updates.append("title_extracted = ?")
                    new_title = metadata_fields_to_update.get("title_en") or metadata_fields_to_update.get("title_cn") or metadata_row["title_extracted"]
                    meta_params.append(new_title)
                
                if "abstract" in metadata_fields_to_update:
                    meta_updates.append("abstract_extracted = ?")
                    meta_params.append(payload.abstract)
                
                meta_updates.append("parse_status = 'done'")
                meta_updates.append("updated_at = CURRENT_TIMESTAMP")
                meta_params.append(paper_id)
                
                conn.execute(
                    f"UPDATE paper_texts SET {', '.join(meta_updates)} WHERE paper_id = ? AND text_scope = 'metadata'",
                    meta_params
                )
                metadata_updated = True
            else:
                # 创建新的 metadata 记录
                from uuid import uuid4
                sections = {"metadata": metadata_fields_to_update}
                sections_json = json.dumps(sections, ensure_ascii=False)
                conn.execute(
                    """
                    INSERT INTO paper_texts (
                        id, paper_id, text_scope, title_extracted, abstract_extracted,
                        body_extracted, sections_json, raw_text, parse_status
                    ) VALUES (?, ?, 'metadata', ?, ?, '', ?, '', 'done')
                    """,
                    (
                        str(uuid4()),
                        paper_id,
                        metadata_fields_to_update.get("title_en") or metadata_fields_to_update.get("title_cn") or "",
                        metadata_fields_to_update.get("abstract") or "",
                        sections_json,
                    ),
                )
                metadata_updated = True
        
        # 3. 更新 paper_analysis 表的八维分析字段
        analysis_fields_to_update = {}
        if payload.tldr is not None:
            analysis_fields_to_update["tldr"] = payload.tldr
        if payload.motivation is not None:
            analysis_fields_to_update["motivation"] = payload.motivation
        if payload.methodology is not None:
            analysis_fields_to_update["methodology"] = payload.methodology
        if payload.experiments is not None:
            analysis_fields_to_update["experiments"] = payload.experiments
        if payload.resources is not None:
            analysis_fields_to_update["resources"] = payload.resources
        if payload.ablation is not None:
            analysis_fields_to_update["ablation"] = payload.ablation
        if payload.conclusion is not None:
            analysis_fields_to_update["conclusion"] = payload.conclusion
        if payload.strengths is not None:
            analysis_fields_to_update["strengths"] = payload.strengths
        if payload.weaknesses is not None:
            analysis_fields_to_update["weaknesses"] = payload.weaknesses

        if analysis_fields_to_update:
            analysis_updates = [f"{key} = ?" for key in analysis_fields_to_update]
            analysis_params = list(analysis_fields_to_update.values())
            analysis_updates.append("analysis_status = 'done'")
            analysis_updates.append("updated_at = CURRENT_TIMESTAMP")
            analysis_params.append(paper_id)

            result = conn.execute(
                f"UPDATE paper_analysis SET {', '.join(analysis_updates)} WHERE paper_id = ?",
                analysis_params
            )
            if result.rowcount > 0:
                analysis_updated = True
            else:
                # 如果没有现有的分析记录，创建一个
                from uuid import uuid4
                conn.execute(
                    """
                    INSERT INTO paper_analysis (
                        id, paper_id, analysis_status, tldr, motivation, methodology, experiments,
                        resources, ablation, conclusion, strengths, weaknesses
                    ) VALUES (?, ?, 'done', ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(uuid4()),
                        paper_id,
                        analysis_fields_to_update.get("tldr", ""),
                        analysis_fields_to_update.get("motivation", ""),
                        analysis_fields_to_update.get("methodology", ""),
                        analysis_fields_to_update.get("experiments", ""),
                        analysis_fields_to_update.get("resources", ""),
                        analysis_fields_to_update.get("ablation", ""),
                        analysis_fields_to_update.get("conclusion", ""),
                        analysis_fields_to_update.get("strengths", ""),
                        analysis_fields_to_update.get("weaknesses", ""),
                    ),
                )
                analysis_updated = True
    
    updated = paper_updated or metadata_updated or analysis_updated
    return {"paper_id": paper_id, "updated": updated}


def continue_analysis_after_duplicate(paper_id: str) -> str:
    """Resume analysis after user confirmed duplicate detection.

    Args:
        paper_id: Paper ID to resume analysis for.

    Returns:
        Paper ID.
    """
    from app.core.analysis import build_analysis_payload
    from app.core.debug_log import clear_task_logs, log_task_event, log_task_update, task_log_timer

    paper = get_paper(paper_id)
    if paper is None:
        raise ValueError(f"Paper {paper_id} not found")

    if paper.status != PAPER_STATUS_DUPLICATE_DETECTED:
        raise ValueError(f"Paper {paper_id} is not in duplicate_detected status (current: {paper.status})")

    logger.info("paper_continue_after_duplicate paper_id=%s", paper_id)

    # Get stored data from paper_texts
    with session() as conn:
        row = conn.execute(
            """
            SELECT raw_text, sections_json FROM paper_texts
            WHERE paper_id = ? AND text_scope = 'metadata'
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (paper_id,),
        ).fetchone()

    if row is None:
        raise ValueError(f"No stored data found for paper {paper_id}")

    # Clear logs and restart the analysis portion
    clear_task_logs(paper_id)

    # Rebuild parsed data
    stored_data = json.loads(row["sections_json"]) if row["sections_json"] else {}
    raw_text = row["raw_text"] or stored_data.get("raw_text", "")
    _stored_meta = stored_data.get("metadata", {})
    parsed = {
        "title": stored_data.get("title", ""),
        "title_cn": stored_data.get("title_cn", ""),
        "title_en": stored_data.get("title_en", ""),
        "authors": stored_data.get("authors", ""),
        "source": stored_data.get("source", ""),
        "abstract": _stored_meta.get("abstract", "") or stored_data.get("abstract", ""),
        "raw_text": raw_text,
        "full_text": raw_text,  # analyze_text() uses full_text for the prompt
        "candidate_text": raw_text,
        "first_pages_text": raw_text[:12000],
        "abstract_region": _stored_meta.get("abstract_region", "") or raw_text[:3500],
        "extraction_method": stored_data.get("extraction_method", "ocr"),
        "text_quality": stored_data.get("text_quality", ""),
        "paper_id": paper_id,
    }

    # --- Language-aware normalization of stored metadata ---
    # The stored metadata may have incorrect language assignment from old code.
    # Re-run language detection and fix abstract_cn/abstract_en/title.
    # IMPORTANT: detect language from the FULL raw_text (body), not from
    # abstract_region. Chinese papers often have both Chinese and English
    # abstracts; abstract_region may capture the English one and misclassify
    # the paper as English. Using detect_paper_language() on the full parsed
    # dict ensures the body text (in the paper's native language) dominates.
    if isinstance(_stored_meta, dict):
        from app.core.metadata_client import _detect_language, detect_paper_language, _translate_to_chinese
        _ar = _stored_meta.get("abstract_region", "") or raw_text[:500]
        _lang = detect_paper_language(parsed)
        _abs_cn = _stored_meta.get("abstract_cn", "")
        _abs_en = _stored_meta.get("abstract_en", "")
        if _lang == "zh":
            # Chinese paper: clear English abstract (translation not needed),
            # but PRESERVE English title if the journal provides one.
            if _abs_cn and _detect_language(_abs_cn) == "zh":
                pass  # OK
            elif _abs_en and _detect_language(_abs_en) == "zh":
                _abs_cn = _abs_en
            elif not _abs_cn and not _abs_en:
                _abs_cn = _stored_meta.get("abstract", "") or _ar[:300]
            _abs_en = ""
            # Fix title: prefer Chinese as main title, keep English as secondary
            _tc = _stored_meta.get("title_cn", "") or parsed.get("title_cn", "")
            _te = _stored_meta.get("title_en", "") or parsed.get("title_en", "")
            # Validate title languages: clear mismatched fields
            if _tc and _detect_language(_tc) != "zh":
                _tc = ""
            if _te and _detect_language(_te) != "en":
                _te = ""
            if _tc:
                parsed["title"] = _tc
            elif _te and _detect_language(_te) == "zh":
                parsed["title"] = _te
            parsed["title_cn"] = _tc
            # Preserve English title only if it's actually English
            parsed["title_en"] = _te
        elif _lang == "en":
            # English paper: ensure English abstract and Chinese translation
            if _abs_en and _detect_language(_abs_en) == "en":
                pass  # OK
            elif _abs_cn and _detect_language(_abs_cn) == "en":
                _abs_en = _abs_cn
            elif not _abs_en:
                _abs_en = _stored_meta.get("abstract", "") or _ar[:300]
            # Ensure Chinese translation exists
            if not _abs_cn or _detect_language(_abs_cn) != "zh":
                _abs_cn = _translate_to_chinese(_abs_en or "", paper_id)
            # Fix title: prefer English
            _tc = _stored_meta.get("title_cn", "") or parsed.get("title_cn", "")
            _te = _stored_meta.get("title_en", "") or parsed.get("title_en", "")
            # Validate title languages: clear mismatched fields
            if _tc and _detect_language(_tc) != "zh":
                _tc = ""
            if _te and _detect_language(_te) != "en":
                _te = ""
            if _te:
                parsed["title"] = _te
            parsed["title_en"] = _te
            parsed["title_cn"] = _tc
        # Update stored metadata
        _stored_meta["abstract_cn"] = _abs_cn
        _stored_meta["abstract_en"] = _abs_en
        stored_data["metadata"] = _stored_meta
        # Sync validated titles back to stored_data so the fallback below
        # doesn't resurrect unvalidated (wrong-language) titles.
        stored_data["title_cn"] = parsed.get("title_cn", "")
        stored_data["title_en"] = parsed.get("title_en", "")
        if parsed.get("title"):
            stored_data["title"] = parsed["title"]
        parsed["abstract"] = _abs_cn or _abs_en or parsed.get("abstract", "")
        parsed["title"] = stored_data.get("title", "") or parsed.get("title", "")
        parsed["title_cn"] = stored_data.get("title_cn", "") or parsed.get("title_cn", "")
        parsed["title_en"] = stored_data.get("title_en", "") or parsed.get("title_en", "")

    log_task_event(
        paper_id, step="继续分析（用户已确认重复）", api="auto_parse_and_analyze",
        status="running",
    )

    # Restore logs for already-completed steps so the terminal shows
    # accurate progress (PDF parsing + metadata + duplicate detection
    # were all done before the pause).
    log_task_update(
        paper_id, step="PDF 文档解析", api="mineru/ocr",
        status="success", duration_ms=0,
        detail=f"使用 {parsed.get('extraction_method', 'ocr')} 提取文本（断点续析）",
    )

    metadata = stored_data.get("metadata", {})
    log_task_update(
        paper_id, step="元数据与摘要提取", api="deepseek.metadata_extract",
        status="success", duration_ms=0,
        detail=f"title_en={metadata.get('title_en', '')[:40]}",
    )

    stored_candidates = stored_data.get("duplicate_candidates", [])
    log_task_update(
        paper_id, step="重复文献检测", api="db.check_duplicate",
        status="warning" if stored_candidates else "success", duration_ms=0,
        detail=(
            f"用户已确认继续（发现 {len(stored_candidates)} 篇可能重复）"
            if stored_candidates else "未检测到重复文献"
        ),
    )

    # Skip to analysis phase
    _update_paper_status(paper_id, PAPER_STATUS_ANALYZING, reason="用户确认重复，继续八维分析")
    log_task_event(
        paper_id, step="八维深度分析", api="deepseek.analysis",
        status="running",
    )
    t_analysis = task_log_timer()
    analysis_payload = build_analysis_payload(parsed)
    analysis_elapsed = int((task_log_timer() - t_analysis) * 1000)
    analysis_status = analysis_payload.get("analysis_status", "done")
    analysis_api = analysis_payload.get("model_name", "deepseek")
    analysis_dimensions = [
        k for k in ["motivation", "methodology", "experiments", "resources",
                     "ablation", "conclusion", "strengths", "weaknesses"]
        if analysis_payload.get(k)
    ]
    analysis_detail = f"完成维度: {len(analysis_dimensions)}/8"
    if analysis_dimensions:
        analysis_detail += f" ({', '.join(analysis_dimensions[:4])}...)"

    if analysis_status == "done":
        log_task_update(
            paper_id, step="八维深度分析", api=f"deepseek.{analysis_api}",
            status="success", duration_ms=analysis_elapsed,
            detail=analysis_detail,
        )
    elif analysis_status == "failed":
        log_task_update(
            paper_id, step="八维深度分析", api=f"deepseek.{analysis_api}",
            status="failed", duration_ms=analysis_elapsed,
            error=analysis_payload.get("error_message", "")[:300],
        )
    else:
        log_task_update(
            paper_id, step="八维深度分析", api=f"deepseek.{analysis_api}",
            status="running", duration_ms=analysis_elapsed,
            detail=analysis_detail,
        )

    # Final write
    # Use already-normalized data from the language-aware normalization above.
    title = parsed.get("title", "").strip()
    title_cn = parsed.get("title_cn", "").strip()
    title_en = parsed.get("title_en", "").strip()
    authors = parsed.get("authors", "").strip()
    source = parsed.get("source", "").strip()
    abstract = parsed.get("abstract", "").strip()
    raw_text = parsed.get("raw_text", "")
    extraction_method = parsed.get("extraction_method", "ocr")
    text_quality = parsed.get("text_quality", "")
    metadata_raw = json.dumps(stored_data.get("metadata", {}), ensure_ascii=False)
    analysis_raw = analysis_payload.get("raw_json", "")

    _update_paper_status(paper_id, PAPER_STATUS_PARSED, reason="继续分析 - 写入结果")

    with session() as conn:
        conn.execute(
            """
            INSERT INTO papers (id, title, title_cn, title_en, authors, abstract, source_url, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'parsed', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
            ON CONFLICT(id) DO UPDATE SET
                title = COALESCE(NULLIF(?, ''), title),
                title_cn = ?,
                title_en = ?,
                authors = COALESCE(NULLIF(?, ''), authors),
                abstract = COALESCE(NULLIF(?, ''), abstract),
                source_url = COALESCE(NULLIF(?, ''), source_url),
                status = 'parsed',
                updated_at = CURRENT_TIMESTAMP
            """,
            (paper_id, title, title_cn, title_en, authors, abstract, source,
             title, title_cn, title_en, authors, abstract, source),
        )
        conn.execute(
            """
            INSERT INTO paper_texts (
                id, paper_id, text_scope, title_extracted, abstract_extracted,
                sections_json, raw_text, parse_status, parse_error,
                extraction_method
            ) VALUES (?, ?, 'metadata', ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(paper_id, text_scope) DO UPDATE SET
                title_extracted=excluded.title_extracted,
                abstract_extracted=excluded.abstract_extracted,
                sections_json=excluded.sections_json,
                raw_text=excluded.raw_text,
                parse_status=excluded.parse_status,
                parse_error=excluded.parse_error,
                extraction_method=excluded.extraction_method,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                str(uuid4()),
                paper_id,
                title,
                abstract,
                json.dumps(
                    {
                        "extraction_method": extraction_method,
                        "text_quality": text_quality,
                        "title": title,
                        "title_cn": title_cn,
                        "title_en": title_en,
                        "authors": authors,
                        "source": source,
                        "metadata": stored_data.get("metadata", {}),
                    },
                    ensure_ascii=False,
                ),
                raw_text,
                "done",
                "",
                extraction_method,
            ),
        )

        # Upsert analysis — include extraction_method
        analysis_status = analysis_payload.get("analysis_status", "failed")
        error_message = analysis_payload.get("error_message", "")
        conn.execute(
            """
            INSERT INTO paper_analysis (
                id, paper_id, analysis_status, error_message,
                tldr, motivation, methodology, experiments, resources,
                ablation, conclusion, strengths, weaknesses,
                raw_json, model_name, prompt_version, extraction_method, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(paper_id) DO UPDATE SET
                analysis_status=excluded.analysis_status,
                error_message=excluded.error_message,
                tldr=excluded.tldr,
                motivation=excluded.motivation,
                methodology=excluded.methodology,
                experiments=excluded.experiments,
                resources=excluded.resources,
                ablation=excluded.ablation,
                conclusion=excluded.conclusion,
                strengths=excluded.strengths,
                weaknesses=excluded.weaknesses,
                raw_json=excluded.raw_json,
                model_name=excluded.model_name,
                prompt_version=excluded.prompt_version,
                extraction_method=excluded.extraction_method,
                updated_at=CURRENT_TIMESTAMP
            """,
            (
                str(uuid4()),
                paper_id,
                analysis_status,
                error_message,
                analysis_payload.get("tldr", ""),
                analysis_payload.get("motivation", ""),
                analysis_payload.get("methodology", ""),
                analysis_payload.get("experiments", ""),
                analysis_payload.get("resources", ""),
                analysis_payload.get("ablation", ""),
                analysis_payload.get("conclusion", ""),
                analysis_payload.get("strengths", ""),
                analysis_payload.get("weaknesses", ""),
                analysis_payload.get("raw_json", ""),
                analysis_payload.get("model_name", "deepseek"),
                analysis_payload.get("prompt_version", "v1"),
                extraction_method,
            ),
        )

    if analysis_status == "done":
        _update_paper_status(paper_id, PAPER_STATUS_DONE, reason="继续分析完成")
        log_task_event(
            paper_id, step="分析完成", api="auto_parse_and_analyze",
            status="success", detail="重复文献确认后八维分析完成",
        )
    else:
        _update_paper_status(paper_id, PAPER_STATUS_FAILED, reason=error_message[:200] or "分析失败")
        log_task_event(
            paper_id, step="分析失败", api="auto_parse_and_analyze",
            status="failed", detail=error_message[:200],
        )

    logger.info(
        "paper_continue_after_duplicate_done paper_id=%s final_status=%s",
        paper_id, analysis_status,
    )
    return paper_id
