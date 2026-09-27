"""Chat service for AI-assisted reading conversations.

Manages chat sessions and messages, provides context-anchored prompts
for the LLM, and handles citation tracing for source-verifiable responses.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.core.chat_client import _truncate_context
from app.db import session

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _generate_id() -> str:
    return str(uuid.uuid4())


# ========== Session Operations ==========


def create_session(paper_id: str, title: str = "") -> dict:
    """Create a new chat session for a paper.

    Args:
        paper_id: ID of the paper this session is anchored to.
        title: Optional session title (auto-generated from first message if empty).

    Returns:
        Session dict with id, paper_id, title, created_at, updated_at.
    """
    session_id = _generate_id()
    now = _now_iso()
    with session() as conn:
        conn.execute(
            "INSERT INTO chat_sessions (id, paper_id, title, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (session_id, paper_id, title, now, now),
        )
    return {
        "id": session_id,
        "paper_id": paper_id,
        "title": title,
        "created_at": now,
        "updated_at": now,
        "messages": [],
    }


def list_sessions(paper_id: str) -> list[dict]:
    """List all chat sessions for a paper, ordered by most recently updated.

    Only sessions that contain at least one non-deleted message are returned,
    so empty conversations are never shown to the user.

    Args:
        paper_id: ID of the paper.

    Returns:
        List of session dicts with message counts (excluding deleted).
    """
    with session() as conn:
        rows = conn.execute(
            """
            SELECT s.*,
                (SELECT COUNT(*) FROM chat_messages m
                 WHERE m.session_id = s.id AND m.is_deleted = 0 AND m.role = 'user') as message_count
            FROM chat_sessions s
            WHERE s.paper_id = ?
              AND EXISTS (
                  SELECT 1 FROM chat_messages m
                  WHERE m.session_id = s.id AND m.is_deleted = 0
              )
            ORDER BY s.updated_at DESC
            """,
            (paper_id,),
        ).fetchall()

    return [
        {
            "id": row["id"],
            "paper_id": row["paper_id"],
            "title": row["title"] or "新对话",
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": row["message_count"],
        }
        for row in rows
    ]


def get_session(session_id: str) -> dict | None:
    """Get a chat session with its non-deleted messages.

    Args:
        session_id: ID of the session.

    Returns:
        Session dict with messages list, or None if not found.
    """
    with session() as conn:
        s_row = conn.execute(
            "SELECT * FROM chat_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
        if not s_row:
            return None

        m_rows = conn.execute(
            """
            SELECT * FROM chat_messages
            WHERE session_id = ? AND is_deleted = 0
            ORDER BY created_at ASC
            """,
            (session_id,),
        ).fetchall()

    messages = [
        {
            "id": row["id"],
            "session_id": row["session_id"],
            "role": row["role"],
            "content": row["content"],
            "citations": json.loads(row["citations_json"]) if row["citations_json"] else [],
            "parent_id": row["parent_id"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }
        for row in m_rows
    ]

    return {
        "id": s_row["id"],
        "paper_id": s_row["paper_id"],
        "title": s_row["title"] or "新对话",
        "created_at": s_row["created_at"],
        "updated_at": s_row["updated_at"],
        "messages": messages,
    }


def delete_session(session_id: str) -> bool:
    """Delete a chat session and all its messages.

    Args:
        session_id: ID of the session to delete.

    Returns:
        True if deleted, False if not found.
    """
    with session() as conn:
        cursor = conn.execute(
            "DELETE FROM chat_sessions WHERE id = ?",
            (session_id,),
        )
        return cursor.rowcount > 0


def update_session_title(session_id: str, title: str) -> bool:
    """Update the title of a chat session.

    Args:
        session_id: ID of the session.
        title: New title.

    Returns:
        True if updated.
    """
    now = _now_iso()
    with session() as conn:
        conn.execute(
            "UPDATE chat_sessions SET title = ?, updated_at = ? WHERE id = ?",
            (title, now, session_id),
        )
    return True


async def ensure_session_title(session_id: str, first_message: str) -> str | None:
    """Generate and persist a title if the session currently has none.

    Checks the raw DB title (not the normalized "新对话" fallback) so a title
    is only generated once, on the first user message.

    Args:
        session_id: ID of the session.
        first_message: The first user message content to derive a title from.

    Returns:
        The new title if generated, or None if the session already had a title.
    """
    with session() as conn:
        row = conn.execute(
            "SELECT title FROM chat_sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    if not row:
        return None
    current_title = (row["title"] or "").strip()
    if current_title:
        return None

    from app.core.chat_client import generate_title

    title = await generate_title(first_message)
    if title:
        update_session_title(session_id, title)
        return title
    return None


# ========== Message Operations ==========


def add_message(
    session_id: str,
    role: str,
    content: str,
    citations: list[dict] | None = None,
    parent_id: str | None = None,
) -> dict:
    """Add a message to a chat session.

    Args:
        session_id: Session ID.
        role: 'user' or 'assistant'.
        content: Message text content.
        citations: Optional list of citation dicts [{section, page, quote}].
        parent_id: Optional parent message ID (for edits).

    Returns:
        Created message dict.
    """
    msg_id = _generate_id()
    now = _now_iso()
    citations_json = json.dumps(citations or [], ensure_ascii=False)

    with session() as conn:
        conn.execute(
            "INSERT INTO chat_messages (id, session_id, role, content, citations_json, parent_id, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (msg_id, session_id, role, content, citations_json, parent_id, now, now),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )

    return {
        "id": msg_id,
        "session_id": session_id,
        "role": role,
        "content": content,
        "citations": citations or [],
        "parent_id": parent_id,
        "created_at": now,
        "updated_at": now,
    }


def soft_delete_message(message_id: str) -> bool:
    """Soft-delete a message (mark as deleted).

    Args:
        message_id: ID of the message.

    Returns:
        True if updated.
    """
    now = _now_iso()
    with session() as conn:
        conn.execute(
            "UPDATE chat_messages SET is_deleted = 1, updated_at = ? WHERE id = ?",
            (now, message_id),
        )
    return True


def update_message(message_id: str, content: str) -> bool:
    """Update the content of an existing message.

    Args:
        message_id: ID of the message.
        content: New content.

    Returns:
        True if updated.
    """
    now = _now_iso()
    with session() as conn:
        conn.execute(
            "UPDATE chat_messages SET content = ?, updated_at = ? WHERE id = ?",
            (content, now, message_id),
        )
    return True


def clear_session_messages(session_id: str) -> bool:
    """Soft-delete all messages in a session (clear context).

    Args:
        session_id: Session ID.

    Returns:
        True if updated.
    """
    now = _now_iso()
    with session() as conn:
        conn.execute(
            "UPDATE chat_messages SET is_deleted = 1, updated_at = ? WHERE session_id = ?",
            (now, session_id),
        )
        conn.execute(
            "UPDATE chat_sessions SET updated_at = ? WHERE id = ?",
            (now, session_id),
        )
    return True


# ========== Context Building ==========


def _get_paper_full_text(paper_id: str) -> str:
    """Retrieve the full extracted text for a paper.

    Tries paper_texts.raw_text first, then falls back to body_extracted,
    then to the MinerU markdown file.

    Args:
        paper_id: Paper ID.

    Returns:
        Full text content, or empty string if not available.
    """
    with session() as conn:
        # 按明确的作用域优先级取全文：'metadata' 行只存题录（标题/摘要），
        # 不能当全文用。此前按 updated_at 排序会让「改一次元数据」把
        # metadata 行顶到最前，导致问答上下文静默降级成摘要。
        row = conn.execute(
            """
            SELECT raw_text, body_extracted
            FROM paper_texts
            WHERE paper_id = ?
              AND text_scope IN ('mineru', 'analysis', 'full')
            ORDER BY CASE text_scope
                         WHEN 'mineru' THEN 0
                         WHEN 'analysis' THEN 1
                         ELSE 2
                     END,
                     updated_at DESC
            LIMIT 1
            """,
            (paper_id,),
        ).fetchone()

    if row:
        raw_text = row["raw_text"] or ""
        if raw_text.strip():
            return raw_text

        body_text = row["body_extracted"] or ""
        if body_text.strip():
            return body_text

    # Fallback: try MinerU markdown file
    from app.services.mineru_service import get_mineru_markdown
    try:
        markdown = get_mineru_markdown(paper_id)
        if markdown:
            return markdown
    except Exception:
        logger.debug("No MinerU markdown found for paper %s", paper_id)

    return ""


def _get_paper_metadata(paper_id: str) -> dict:
    """Retrieve basic paper metadata for context anchoring.

    Args:
        paper_id: Paper ID.

    Returns:
        Dict with title, authors, abstract, etc.
    """
    with session() as conn:
        row = conn.execute(
            """
            SELECT p.id, p.title, p.title_cn, p.title_en, p.authors,
                   p.publish_date, p.abstract, p.source_url,
                   m.abstract_extracted
            FROM papers p
            LEFT JOIN paper_texts m ON m.paper_id = p.id
            WHERE p.id = ?
            ORDER BY m.updated_at DESC
            LIMIT 1
            """,
            (paper_id,),
        ).fetchone()

    if not row:
        return {}

    return {
        "title": row["title"] or "",
        "title_cn": row["title_cn"] or "",
        "title_en": row["title_en"] or "",
        "authors": row["authors"] or "",
        "abstract": row["abstract"] or row["abstract_extracted"] or "",
        "publish_date": row["publish_date"] or "",
        "source": row["source_url"] or "",
    }


# System prompt template for context-anchored, citation-enabled chat.
SYSTEM_PROMPT_TEMPLATE = """你是一名严谨的学术论文阅读助手。你的回答必须严格扎根于用户提供的文献内容。

【核心规则】
1. **上下文锚定**：所有回答必须基于下方提供的文献全文。当用户提问时，你需要在文献中找到相关段落、章节或数据来支撑回答。
2. **来源可溯与反幻觉**：每条基于文献的陈述都必须附带精确的引用定位，格式为 [第X节 "章节标题"，第Y段]。如果答案涉及通用知识而非库内文献，必须明确标注"此部分为通用知识，未在当前文献中找到确切来源"。禁止编造文献内容。
3. **任务导向**：优先完成用户的具体阅读任务（解释、总结、对比、查找等），而非闲聊。

【引用格式示例】
✅ 正确：该方法采用了注意力机制 [第3.2节 "Methodology"，第2段]，通过自注意力捕获长距离依赖 [第3.2节 "Methodology"，第5段]。
❌ 错误：该方法采用了注意力机制。（缺少引用）
❌ 错误：根据参考文献 [1]，该方法很好。（模糊引用）

【文献信息】
标题：{title}
作者：{authors}
摘要：{abstract}

【文献全文】
{full_text}

现在请基于以上文献内容，回答用户的问题。请确保每个关键论点都有明确的文献引用。"""


def build_chat_messages(
    paper_id: str,
    conversation_history: list[dict],
    user_message: str,
    selected_text: str = "",
) -> list[dict]:
    """Build the complete messages list for a chat completion call.

    Injects the paper full text and metadata into the system prompt for
    context-anchored responses. Appends conversation history and the new
    user message.

    Args:
        paper_id: Paper ID for context anchoring.
        conversation_history: List of prior message dicts (role, content).
        user_message: New user question/message.
        selected_text: Optional text the user selected (划词即问).

    Returns:
        List of message dicts ready for the LLM API call.
    """
    metadata = _get_paper_metadata(paper_id)
    full_text = _get_paper_full_text(paper_id)
    context_text = _truncate_context(full_text) if full_text else ""

    title = metadata.get("title") or metadata.get("title_en") or metadata.get("title_cn") or "未知论文"
    authors = metadata.get("authors") or "未知作者"
    abstract = metadata.get("abstract") or ""

    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(
        title=title,
        authors=authors,
        abstract=abstract,
        full_text=context_text if context_text else "（论文全文尚未就绪，请基于已提供的摘要信息回答。）",
    )

    messages = [{"role": "system", "content": system_prompt}]

    # Add conversation history (skip empty/None messages)
    for msg in conversation_history:
        role = msg.get("role", "")
        content = msg.get("content", "")
        if role in ("user", "assistant") and content:
            messages.append({"role": role, "content": content})

    # Build the new user message, including selected text if present.
    # When a citation is provided, instruct the model to anchor its answer in
    # the cited passage first, locate it within the full paper, and trace the
    # reasoning back to the exact section/paragraph for verifiable answers.
    final_user_message = user_message
    if selected_text.strip():
        final_user_message = (
            f"【用户划选的文献原文】\n"
            f"{selected_text.strip()}\n\n"
            f"【用户问题】\n"
            f"{user_message}\n\n"
            f"【回答要求】\n"
            f"1. 优先围绕用户划选的原文片段进行解读，不得脱离该片段编造内容。\n"
            f"2. 结合论文全文上下文，定位该片段所属章节与段落，解释其在论文整体论证中的作用。\n"
            f"3. 若该片段涉及术语、公式或方法，补充其在论文中的定义与推导脉络。\n"
            f"4. 引用时标注 [第X节 \"章节标题\"，第Y段] 精确位置，确保可溯源。"
        )

    messages.append({"role": "user", "content": final_user_message})

    return messages


# ========== Quick Command Templates ==========

QUICK_COMMANDS = [
    {
        "id": "summarize",
        "label": "📋 总结全文",
        "prompt": "请总结这篇论文的核心内容，包括研究背景、主要方法、关键结果和结论。",
    },
    {
        "id": "explain_term",
        "label": "💡 解释术语",
        "prompt": "请解释这篇论文中出现的关键技术术语和概念，并用通俗的语言说明它们的含义和作用。",
    },
    {
        "id": "methodology",
        "label": "🔬 解析方法论",
        "prompt": "请详细解析这篇论文的核心方法论，包括算法/模型架构、关键技术创新点和实现细节。",
    },
    {
        "id": "experiments",
        "label": "📊 分析实验",
        "prompt": "请分析这篇论文的实验设计，包括数据集、对比基线、评价指标和主要实验结果。",
    },
    {
        "id": "contributions",
        "label": "⭐ 核心贡献",
        "prompt": "请列出这篇论文的主要贡献和创新点，并说明它们相对于现有工作的优势。",
    },
    {
        "id": "limitations",
        "label": "⚠️ 局限性",
        "prompt": "请分析这篇论文的局限性、潜在问题和可能的改进方向。",
    },
    {
        "id": "related",
        "label": "🔗 相关工作",
        "prompt": "请介绍这篇论文的相关工作背景，并将其置于当前研究领域的发展脉络中。",
    },
    {
        "id": "citation",
        "label": "📝 生成引用",
        "prompt": "请生成这篇论文的标准学术引用格式（APA、IEEE和GB/T 7714三种格式）。",
    },
]


def get_quick_commands() -> list[dict]:
    """Return the list of quick command templates.

    Returns:
        List of command dicts with id, label, prompt.
    """
    return QUICK_COMMANDS