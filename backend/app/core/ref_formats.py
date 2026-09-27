"""文献格式互转：CSL JSON / BibTeX / RIS。

仅依赖标准库，覆盖 Zotero / Mendeley / EndNote / JabRef / Google Scholar
等常见导出格式的解析与生成。所有格式先统一为 ``normalize_record()`` 定义的
扁平 dict，再由 ``interop_service`` 映射到业务模型。

中间表示字段：
    type       CSL item type，如 "article-journal" / "paper-conference"
    title      主标题（显示用）
    title_cn   中文标题
    title_en   英文标题
    authors    list[str]，自然顺序 "Given Family"
    year       四位年份
    date       完整日期 "YYYY-MM-DD"
    container  期刊 / 会议 / 书名
    volume issue pages
    doi url abstract
    keywords   list[str]
    language note
    citekey    引用键（BibTeX）
    status     阅读状态（PaperPilot 扩展，仅 CSL JSON 携带）
    folder     文件夹名（PaperPilot 扩展，仅 CSL JSON 携带）
"""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

# ============================================================================
# 类型映射
# ============================================================================

_BIBTEX_TO_CSL = {
    "article": "article-journal",
    "inproceedings": "paper-conference",
    "conference": "paper-conference",
    "proceedings": "book",
    "book": "book",
    "booklet": "book",
    "incollection": "chapter",
    "inbook": "chapter",
    "phdthesis": "thesis",
    "mastersthesis": "thesis",
    "techreport": "report",
    "manual": "book",
    "unpublished": "manuscript",
    "online": "webpage",
    "electronic": "webpage",
    "misc": "article",
}

_CSL_TO_BIBTEX = {
    "article-journal": "article",
    "paper-conference": "inproceedings",
    "book": "book",
    "chapter": "incollection",
    "thesis": "phdthesis",
    "report": "techreport",
    "webpage": "misc",
    "manuscript": "unpublished",
    "article": "misc",
    "document": "misc",
}

_RIS_TO_CSL = {
    "JOUR": "article-journal",
    "JFULL": "article-journal",
    "MGZN": "article-journal",
    "CONF": "paper-conference",
    "CPAPER": "paper-conference",
    "BOOK": "book",
    "CHAP": "chapter",
    "ECHAP": "chapter",
    "THES": "thesis",
    "RPRT": "report",
    "ELEC": "webpage",
    "WEB": "webpage",
    "UNPB": "manuscript",
    "GEN": "article",
}

_CSL_TO_RIS = {
    "article-journal": "JOUR",
    "paper-conference": "CPAPER",
    "book": "BOOK",
    "chapter": "CHAP",
    "thesis": "THES",
    "report": "RPRT",
    "webpage": "ELEC",
    "manuscript": "UNPB",
    "article": "GEN",
    "document": "GEN",
}

_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af]")


# ============================================================================
# 中间表示
# ============================================================================

_RECORD_FIELDS = (
    "type", "title", "title_cn", "title_en", "authors", "year", "date",
    "container", "volume", "issue", "pages", "doi", "url", "abstract",
    "keywords", "language", "note", "citekey", "status", "folder",
)


def empty_record() -> dict[str, Any]:
    return {
        "type": "article-journal",
        "title": "",
        "title_cn": "",
        "title_en": "",
        "authors": [],
        "year": "",
        "date": "",
        "container": "",
        "volume": "",
        "issue": "",
        "pages": "",
        "doi": "",
        "url": "",
        "abstract": "",
        "keywords": [],
        "language": "",
        "note": "",
        "citekey": "",
        "status": "",
        "folder": "",
    }


def normalize_record(raw: dict[str, Any] | None) -> dict[str, Any]:
    """把任意来源的 dict 归一化为标准中间表示。"""
    rec = empty_record()
    if not raw:
        return rec
    for key in _RECORD_FIELDS:
        if key not in raw or raw[key] is None:
            continue
        value = raw[key]
        if key in ("authors", "keywords"):
            if isinstance(value, str):
                value = [v.strip() for v in re.split(r"[;,]|\band\b", value) if v.strip()]
            else:
                value = [str(v).strip() for v in value if str(v).strip()]
        else:
            value = str(value).strip()
        rec[key] = value
    if not rec["title"]:
        rec["title"] = rec["title_en"] or rec["title_cn"]
    if not rec["year"] and rec["date"]:
        rec["year"] = _extract_year(rec["date"])
    if not rec["date"] and rec["year"]:
        rec["date"] = rec["year"]
    return rec


def _extract_year(value: str) -> str:
    m = re.search(r"(1[5-9]\d{2}|20\d{2}|21\d{2})", value or "")
    return m.group(1) if m else ""


def _looks_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


# ============================================================================
# 作者姓名
# ============================================================================

def split_name(natural: str) -> tuple[str, str]:
    """把自然顺序姓名拆成 (family, given)。无法可靠拆分时 given 为空。"""
    name = (natural or "").strip()
    if not name:
        return "", ""
    if "," in name:
        family, _, given = name.partition(",")
        return family.strip(), given.strip()
    if _looks_cjk(name) or " " not in name:
        return name, ""
    parts = name.split()
    return parts[-1], " ".join(parts[:-1])


def join_name(family: str, given: str) -> str:
    family = (family or "").strip()
    given = (given or "").strip()
    if family and given:
        return f"{given} {family}"
    return family or given


def author_to_csl(natural: str) -> dict[str, str]:
    family, given = split_name(natural)
    if given:
        return {"family": family, "given": given}
    if _looks_cjk(natural):
        return {"literal": natural}
    return {"family": natural}


def csl_to_author(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if not isinstance(value, dict):
        return str(value).strip()
    literal = (value.get("literal") or "").strip()
    if literal:
        return literal
    return join_name(value.get("family", ""), value.get("given", ""))


def _author_to_bibtex(natural: str) -> str:
    family, given = split_name(natural)
    if given:
        return f"{family}, {given}"
    # 单名 / 中文名用双花括号保护，避免被 BibTeX 拆词或重排
    return f"{{{natural}}}"


# ============================================================================
# 日期
# ============================================================================

def _date_parts(rec: dict[str, Any]) -> list[list[int]]:
    date = rec.get("date") or ""
    year = rec.get("year") or _extract_year(date)
    if not year:
        return []
    parts = [int(year)]
    m = re.match(r"^\s*\d{4}[-/.](\d{1,2})(?:[-/.](\d{1,2}))?", date)
    if m:
        parts.append(int(m.group(1)))
        if m.group(2):
            parts.append(int(m.group(2)))
    return [parts]


def _date_from_parts(value: Any) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        # citeproc: {"date-parts": [[2017, 6, 12]]} 或 {"raw": "2017-06-12"}
        if value.get("raw"):
            return str(value["raw"]).strip()
        dp = value.get("date-parts")
        if isinstance(dp, list) and dp and isinstance(dp[0], list):
            nums = [int(n) for n in dp[0] if str(n).strip().isdigit()]
            if not nums:
                return ""
            out = f"{nums[0]:04d}"
            if len(nums) > 1:
                out += f"-{nums[1]:02d}"
            if len(nums) > 2:
                out += f"-{nums[2]:02d}"
            return out
    return ""


# ============================================================================
# BibTeX
# ============================================================================

_ENTRY_START_RE = re.compile(r"@([A-Za-z]+)\s*([{(])")

_LATEX_ACCENTS = {
    "'": "\u0301", "`": "\u0300", "^": "\u0302", '"': "\u0308", "~": "\u0303",
    "=": "\u0304", ".": "\u0307", "u": "\u0306", "v": "\u030c", "H": "\u030b",
    "c": "\u0327", "k": "\u0328", "r": "\u030a", "b": "\u0331",
}

_LATEX_SYMBOLS = {
    "ss": "ß", "o": "ø", "O": "Ø", "aa": "å", "AA": "Å", "ae": "æ", "AE": "Æ",
    "l": "ł", "L": "Ł", "i": "ı", "j": "ȷ", "dag": "†", "ddag": "‡",
    "S": "§", "P": "¶", "copyright": "©", "pounds": "£", "euro": "€",
    "&": "&", "%": "%", "$": "$", "#": "#", "_": "_", "{": "{", "}": "}",
}

_ACCENT_CMD_RE = re.compile(r"\\([`'^\"~=.uvHckrb])\s*\{?([A-Za-z])\}?")
_SYMBOL_CMD_RE = re.compile(r"\\([A-Za-z]+)\s*\{\}?")


def latex_to_text(text: str) -> str:
    """把 BibTeX 中常见的 LaTeX 转义还原为可读文本（常见子集）。"""
    if not text:
        return ""
    out = text
    # 重音命令：\'e / \'{e} / \"u
    def _accent(m: re.Match[str]) -> str:
        base = m.group(2)
        return unicodedata.normalize("NFC", base + _LATEX_ACCENTS[m.group(1)])

    out = _ACCENT_CMD_RE.sub(_accent, out)
    # 符号命令：\ss / \o / \& / \%
    out = _SYMBOL_CMD_RE.sub(lambda m: _LATEX_SYMBOLS.get(m.group(1), m.group(1)), out)
    # 剩余的转义字符
    out = re.sub(r"\\([&%$#_{}])", r"\1", out)
    # 花括号仅用于保护大小写，直接去掉
    out = out.replace("{", "").replace("}", "")
    out = out.replace("~", " ")
    out = re.sub(r"\s+", " ", out).strip()
    return out


def _bibtex_escape(text: str) -> str:
    out = text or ""
    for ch in ("&", "%", "$", "#", "_"):
        out = out.replace(ch, "\\" + ch)
    return out


def _split_top_level(body: str, sep: str = ",") -> list[str]:
    """按 sep 切分，忽略花括号 / 引号内部的 sep。"""
    parts: list[str] = []
    cur: list[str] = []
    depth = 0
    in_quote = False
    quote_braces = 0
    i = 0
    while i < len(body):
        c = body[i]
        if c == "\\":
            cur.append(c)
            if i + 1 < len(body):
                cur.append(body[i + 1])
            i += 2
            continue
        if in_quote:
            if c == "{":
                quote_braces += 1
            elif c == "}":
                quote_braces -= 1
            elif c == '"' and quote_braces == 0:
                in_quote = False
            cur.append(c)
        elif c == '"':
            in_quote = True
            cur.append(c)
        elif c == "{":
            depth += 1
            cur.append(c)
        elif c == "}":
            depth -= 1
            cur.append(c)
        elif c == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    parts.append("".join(cur))
    return parts


def _scan_entry_body(text: str, start: int) -> tuple[str, int]:
    """从 start（'{' 或 '(' 的位置）扫描出配对的条目体。"""
    open_char = text[start]
    close_char = "}" if open_char == "{" else ")"
    depth = 0
    in_quote = False
    quote_braces = 0
    i = start
    while i < len(text):
        c = text[i]
        if c == "\\":
            i += 2
            continue
        if in_quote:
            if c == "{":
                quote_braces += 1
            elif c == "}":
                quote_braces -= 1
            elif c == '"' and quote_braces == 0:
                in_quote = False
        elif c == '"' and depth == 1:
            in_quote = True
        elif c == open_char:
            depth += 1
        elif c == close_char:
            depth -= 1
            if depth == 0:
                return text[start + 1:i], i + 1
        i += 1
    return text[start + 1:], len(text)


def _split_field(part: str) -> tuple[str, str] | None:
    depth = 0
    in_quote = False
    quote_braces = 0
    for idx, c in enumerate(part):
        if c == "\\":
            continue
        if in_quote:
            if c == "{":
                quote_braces += 1
            elif c == "}":
                quote_braces -= 1
            elif c == '"' and quote_braces == 0:
                in_quote = False
        elif c == '"':
            in_quote = True
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
        elif c == "=" and depth == 0:
            return part[:idx].strip(), part[idx + 1:].strip()
    return None


def _parse_bibtex_value(value: str) -> str:
    value = value.strip()
    # 处理用 # 连接的拼接
    chunks = _split_top_level(value, sep="#")
    if len(chunks) > 1:
        return "".join(_parse_bibtex_value(chunk) for chunk in chunks)
    if value.startswith("{") and value.endswith("}"):
        inner = value[1:-1]
        return latex_to_text(inner)
    if value.startswith('"') and value.endswith('"'):
        return latex_to_text(value[1:-1])
    return latex_to_text(value)


def _parse_bibtex_entry(entry_type: str, body: str) -> dict[str, Any] | None:
    parts = _split_top_level(body, sep=",")
    if not parts:
        return None
    head = parts[0].strip()
    # head 为 citekey；若含 '=' 说明该条目没有 key（罕见）
    citekey = head if "=" not in head else ""
    fields: dict[str, str] = {}
    rest = parts[1:] if citekey else parts
    for chunk in rest:
        if not chunk.strip():
            continue
        pair = _split_field(chunk)
        if not pair:
            continue
        name, raw_value = pair
        fields[name.strip().lower()] = _parse_bibtex_value(raw_value)

    rec = empty_record()
    rec["type"] = _BIBTEX_TO_CSL.get(entry_type, "article-journal")
    rec["citekey"] = citekey
    rec["title"] = fields.get("title", "")
    rec["container"] = fields.get("journal") or fields.get("journaltitle") or fields.get("booktitle", "")
    rec["volume"] = fields.get("volume", "")
    rec["issue"] = fields.get("number") or fields.get("issue", "")
    rec["pages"] = re.sub(r"\s*--+\s*", "-", fields.get("pages", ""))
    rec["doi"] = fields.get("doi", "")
    rec["url"] = fields.get("url", "")
    rec["abstract"] = fields.get("abstract", "")
    rec["language"] = fields.get("language", "")
    rec["note"] = fields.get("note", "")
    rec["year"] = fields.get("year", "")
    rec["date"] = fields.get("date") or rec["year"]

    authors = fields.get("author", "")
    if authors:
        rec["authors"] = [
            join_name(*_bibtex_name_to_family_given(chunk))
            for chunk in re.split(r"\s+and\s+", authors)
            if chunk.strip()
        ]

    keywords = fields.get("keywords") or fields.get("keyword", "")
    if keywords:
        rec["keywords"] = [k.strip() for k in re.split(r"[,;]", keywords) if k.strip()]

    if not rec["title"]:
        return None
    return normalize_record(rec)


def _bibtex_name_to_family_given(chunk: str) -> tuple[str, str]:
    name = chunk.strip()
    # 双花括号保护的单名 / 中文名
    if name.startswith("{{") and name.endswith("}}"):
        return name[2:-2].strip(), ""
    if name.startswith("{") and name.endswith("}"):
        name = name[1:-1].strip()
    name = latex_to_text(name)
    if "," in name:
        family, _, given = name.partition(",")
        return family.strip(), given.strip()
    if _looks_cjk(name) or " " not in name:
        return name, ""
    parts = name.split()
    return parts[-1], " ".join(parts[:-1])


def parse_bibtex(text: str) -> list[dict[str, Any]]:
    """解析 BibTeX 文本，返回中间表示列表。"""
    records: list[dict[str, Any]] = []
    i = 0
    while True:
        m = _ENTRY_START_RE.search(text, i)
        if not m:
            break
        entry_type = m.group(1).lower()
        body, end = _scan_entry_body(text, m.end() - 1)
        i = end
        if entry_type in ("comment", "preamble", "string"):
            continue
        rec = _parse_bibtex_entry(entry_type, body)
        if rec:
            records.append(rec)
    return records


def _ascii_only(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "", text or "")


def _make_citekey(rec: dict[str, Any], used: set[str]) -> str:
    if rec.get("citekey"):
        return rec["citekey"]
    family = ""
    if rec.get("authors"):
        # 引用键保持 ASCII：中文等非 ASCII 名字会让经典 LaTeX 的 bibtex 引擎失败
        family = _ascii_only(split_name(rec["authors"][0])[0])
    family = family or "ref"
    year = rec.get("year") or "0000"
    word = ""
    for token in re.findall(r"[A-Za-z0-9]+", rec.get("title", "")):
        if token.lower() not in ("a", "an", "the", "on", "of", "in"):
            word = token
            break
    base = f"{family}{year}{word}".lower()
    key = base
    n = 2
    while key in used:
        key = f"{base}{n}"
        n += 1
    used.add(key)
    return key


def serialize_bibtex(records: list[dict[str, Any]]) -> str:
    """生成 BibTeX 文本。"""
    used: set[str] = set()
    out: list[str] = ["% PaperPilot BibTeX export", ""]
    for rec in records:
        rec = normalize_record(rec)
        entry = _CSL_TO_BIBTEX.get(rec["type"], "article")
        key = _make_citekey(rec, used)
        lines = [f"@{entry}{{{key},"]
        if rec["authors"]:
            authors = " and ".join(_author_to_bibtex(a) for a in rec["authors"])
            lines.append(f"  author = {{{authors}}},")
        if rec["title"]:
            lines.append(f"  title = {{{_bibtex_escape(rec['title'])}}},")
        if rec["container"]:
            field = "booktitle" if entry in ("inproceedings", "incollection") else "journal"
            lines.append(f"  {field} = {{{_bibtex_escape(rec['container'])}}},")
        if rec["year"]:
            lines.append(f"  year = {{{rec['year']}}},")
        if rec["volume"]:
            lines.append(f"  volume = {{{rec['volume']}}},")
        if rec["issue"]:
            lines.append(f"  number = {{{rec['issue']}}},")
        if rec["pages"]:
            lines.append(f"  pages = {{{rec['pages']}}},")
        if rec["doi"]:
            lines.append(f"  doi = {{{rec['doi']}}},")
        if rec["url"]:
            lines.append(f"  url = {{{rec['url']}}},")
        if rec["keywords"]:
            lines.append(f"  keywords = {{{_bibtex_escape(', '.join(rec['keywords']))}}},")
        if rec["abstract"]:
            lines.append(f"  abstract = {{{_bibtex_escape(rec['abstract'])}}},")
        if rec["note"]:
            lines.append(f"  note = {{{_bibtex_escape(rec['note'])}}},")
        # 去掉最后一行末尾的逗号
        lines[-1] = lines[-1].rstrip(",")
        lines.append("}")
        out.append("\n".join(lines))
        out.append("")
    return "\n".join(out)


# ============================================================================
# RIS
# ============================================================================

_RIS_LINE_RE = re.compile(r"^([A-Z][A-Z0-9])\s{1,2}-\s?(.*)$")

_RIS_MULTI_TAGS = {"AU", "A1", "A2", "A3", "A4", "ED", "KW", "UR", "L1", "L2", "N1", "N2"}


def parse_ris(text: str) -> list[dict[str, Any]]:
    """解析 RIS 文本，返回中间表示列表。"""
    records: list[dict[str, Any]] = []
    current: dict[str, list[str]] | None = None

    def _flush() -> None:
        nonlocal current
        if current is None:
            return
        rec = _ris_to_record(current)
        if rec and rec["title"]:
            records.append(rec)
        current = None

    for raw_line in text.splitlines():
        line = raw_line.rstrip("\r\n")
        m = _RIS_LINE_RE.match(line.strip())
        if not m:
            # 续行：追加到上一个标签
            if current and line.strip():
                last = list(current)[-1]
                current[last][-1] += " " + line.strip()
            continue
        tag, value = m.group(1), m.group(2).strip()
        if tag == "TY":
            _flush()
            current = {"TY": [value]}
            continue
        if tag == "ER":
            _flush()
            continue
        if current is None:
            current = {}
        if tag in _RIS_MULTI_TAGS:
            current.setdefault(tag, []).append(value)
        else:
            current.setdefault(tag, [])
            if current[tag]:
                current[tag][-1] = value
            else:
                current[tag].append(value)
    _flush()
    return records


def _first(tags: dict[str, list[str]], *keys: str) -> str:
    for key in keys:
        values = tags.get(key)
        if values:
            return values[0].strip()
    return ""


def _ris_to_record(tags: dict[str, list[str]]) -> dict[str, Any] | None:
    rec = empty_record()
    ris_type = (_first(tags, "TY") or "GEN").upper()
    rec["type"] = _RIS_TO_CSL.get(ris_type, "article")
    rec["title"] = _first(tags, "TI", "T1")
    rec["container"] = _first(tags, "T2", "JO", "JF", "JA", "J2")
    rec["volume"] = _first(tags, "VL")
    rec["issue"] = _first(tags, "IS", "M1")
    sp = _first(tags, "SP")
    ep = _first(tags, "EP")
    if sp:
        rec["pages"] = f"{sp}-{ep}" if ep else sp
    rec["doi"] = _first(tags, "DO")
    rec["url"] = _first(tags, "UR", "L1", "L2")
    rec["abstract"] = _first(tags, "AB", "N2")
    rec["language"] = _first(tags, "LA")
    rec["note"] = _first(tags, "N1")
    date = _first(tags, "DA", "PY", "Y1")
    rec["date"] = date
    rec["year"] = _extract_year(date)
    authors: list[str] = []
    for key in ("AU", "A1"):
        for value in tags.get(key, []):
            name = value.strip()
            if not name:
                continue
            # RIS 惯例为 "Family, Given"
            family, given = split_name(name)
            authors.append(join_name(family, given))
    rec["authors"] = authors
    keywords: list[str] = []
    for value in tags.get("KW", []):
        keywords.extend(k.strip() for k in re.split(r"[,;]", value) if k.strip())
    rec["keywords"] = keywords
    if not rec["title"]:
        return None
    return normalize_record(rec)


def serialize_ris(records: list[dict[str, Any]]) -> str:
    """生成 RIS 文本。"""
    out: list[str] = []
    for rec in records:
        rec = normalize_record(rec)
        lines = [f"TY  - {_CSL_TO_RIS.get(rec['type'], 'GEN')}"]
        if rec["title"]:
            lines.append(f"TI  - {rec['title']}")
        if rec["container"]:
            lines.append(f"T2  - {rec['container']}")
        for author in rec["authors"]:
            family, given = split_name(author)
            name = f"{family}, {given}" if given else family
            lines.append(f"AU  - {name}")
        if rec["year"]:
            lines.append(f"PY  - {rec['year']}")
        # DA 仅在含月/日时输出，避免与 PY 重复
        if rec["date"] and re.search(r"\d{4}[-/.]\d{1,2}", rec["date"]):
            lines.append(f"DA  - {rec['date']}")
        if rec["volume"]:
            lines.append(f"VL  - {rec['volume']}")
        if rec["issue"]:
            lines.append(f"IS  - {rec['issue']}")
        if rec["pages"]:
            start, _, end = rec["pages"].partition("-")
            lines.append(f"SP  - {start.strip()}")
            if end.strip():
                lines.append(f"EP  - {end.strip()}")
        if rec["doi"]:
            lines.append(f"DO  - {rec['doi']}")
        if rec["url"]:
            lines.append(f"UR  - {rec['url']}")
        if rec["abstract"]:
            lines.append(f"AB  - {rec['abstract']}")
        for keyword in rec["keywords"]:
            lines.append(f"KW  - {keyword}")
        if rec["language"]:
            lines.append(f"LA  - {rec['language']}")
        if rec["note"]:
            lines.append(f"N1  - {rec['note']}")
        lines.append("ER  - ")
        out.append("\r\n".join(lines))
    return "\n".join(out) + "\n"


# ============================================================================
# CSL JSON
# ============================================================================

_CSL_TYPE_MAP = {
    "article-journal": "article-journal",
    "article": "article-journal",
    "paper-conference": "paper-conference",
    "book": "book",
    "chapter": "chapter",
    "thesis": "thesis",
    "report": "report",
    "webpage": "webpage",
    "manuscript": "manuscript",
    "document": "document",
}

_ZOTERO_TYPE_MAP = {
    "journalArticle": "article-journal",
    "conferencePaper": "paper-conference",
    "book": "book",
    "bookSection": "chapter",
    "thesis": "thesis",
    "report": "report",
    "webpage": "webpage",
    "manuscript": "manuscript",
    "preprint": "article-journal",
    "document": "document",
}


def parse_csl_json(text: str) -> list[dict[str, Any]]:
    """解析 CSL JSON，兼容 citeproc 与 Zotero Web API 两种方言。"""
    data = json.loads(text)
    if isinstance(data, dict):
        if isinstance(data.get("items"), list):
            data = data["items"]
        else:
            data = [data]
    if not isinstance(data, list):
        raise ValueError("CSL JSON 根节点必须是数组或含 items 的对象")
    return [rec for rec in (_csl_item_to_record(item) for item in data if isinstance(item, dict)) if rec]


def _csl_item_to_record(item: dict[str, Any]) -> dict[str, Any] | None:
    rec = empty_record()
    # 方言判定：Zotero API 用 itemType / creators
    if "itemType" in item or "creators" in item:
        rec["type"] = _ZOTERO_TYPE_MAP.get(str(item.get("itemType", "")), "article-journal")
        creators = item.get("creators") or []
        authors: list[str] = []
        for creator in creators:
            if not isinstance(creator, dict):
                continue
            ctype = creator.get("creatorType", "author")
            if ctype not in ("author", "editor"):
                continue
            if creator.get("name"):
                authors.append(str(creator["name"]).strip())
            else:
                authors.append(join_name(creator.get("lastName", ""), creator.get("firstName", "")))
        rec["authors"] = [a for a in authors if a]
        rec["container"] = str(item.get("publicationTitle") or item.get("proceedingsTitle") or "")
        rec["abstract"] = str(item.get("abstractNote") or "")
        rec["doi"] = str(item.get("DOI") or "")
        rec["url"] = str(item.get("url") or "")
        rec["language"] = str(item.get("language") or "")
        rec["pages"] = str(item.get("pages") or "")
        rec["volume"] = str(item.get("volume") or "")
        rec["issue"] = str(item.get("issue") or "")
        date = str(item.get("date") or "")
        rec["date"] = date
        rec["year"] = _extract_year(date)
        rec["keywords"] = [
            str(t.get("tag")).strip()
            for t in (item.get("tags") or [])
            if isinstance(t, dict) and t.get("tag")
        ]
        extra = str(item.get("extra") or "").strip()
        if extra:
            rec["note"] = extra
    else:
        rec["type"] = _CSL_TYPE_MAP.get(str(item.get("type", "")), "article-journal")
        rec["authors"] = [csl_to_author(a) for a in (item.get("author") or [])]
        rec["authors"] = [a for a in rec["authors"] if a]
        rec["container"] = str(item.get("container-title") or "")
        rec["abstract"] = str(item.get("abstract") or "")
        rec["doi"] = str(item.get("DOI") or "")
        rec["url"] = str(item.get("URL") or "")
        rec["language"] = str(item.get("language") or "")
        rec["pages"] = str(item.get("page") or "")
        rec["volume"] = str(item.get("volume") or "")
        rec["issue"] = str(item.get("issue") or "")
        rec["date"] = _date_from_parts(item.get("issued"))
        rec["year"] = _extract_year(rec["date"])
        keywords = item.get("keyword")
        if isinstance(keywords, list):
            rec["keywords"] = [str(k).strip() for k in keywords if str(k).strip()]
        elif isinstance(keywords, str):
            rec["keywords"] = [k.strip() for k in re.split(r"[,;]", keywords) if k.strip()]
        if item.get("note"):
            rec["note"] = str(item["note"])
        # Zotero 的 tags 数组（部分导出也带）
        for tag in item.get("tags") or []:
            if isinstance(tag, dict) and tag.get("tag"):
                rec["keywords"].append(str(tag["tag"]).strip())

    rec["title"] = str(item.get("title") or "")

    # PaperPilot 扩展块（自身往返无损）
    ext = item.get("paperpilot")
    if isinstance(ext, dict):
        rec["title_cn"] = str(ext.get("title_cn") or "")
        rec["title_en"] = str(ext.get("title_en") or "")
        rec["status"] = str(ext.get("status") or "")
        rec["folder"] = str(ext.get("folder") or "")

    if not rec["title"]:
        return None
    # 去重关键词
    seen: set[str] = set()
    unique: list[str] = []
    for keyword in rec["keywords"]:
        if keyword not in seen:
            seen.add(keyword)
            unique.append(keyword)
    rec["keywords"] = unique
    return normalize_record(rec)


def serialize_csl_json(records: list[dict[str, Any]]) -> str:
    """生成 CSL JSON（citeproc 风格 + paperpilot 扩展块）。"""
    items: list[dict[str, Any]] = []
    for rec in records:
        rec = normalize_record(rec)
        item: dict[str, Any] = {
            "id": rec.get("citekey") or _make_citekey(rec, set()),
            "type": rec["type"] or "article-journal",
            "title": rec["title"],
        }
        if rec["authors"]:
            item["author"] = [author_to_csl(a) for a in rec["authors"]]
        if rec["container"]:
            item["container-title"] = rec["container"]
        parts = _date_parts(rec)
        if parts:
            item["issued"] = {"date-parts": parts}
        if rec["volume"]:
            item["volume"] = rec["volume"]
        if rec["issue"]:
            item["issue"] = rec["issue"]
        if rec["pages"]:
            item["page"] = rec["pages"]
        if rec["doi"]:
            item["DOI"] = rec["doi"]
        if rec["url"]:
            item["URL"] = rec["url"]
        if rec["abstract"]:
            item["abstract"] = rec["abstract"]
        if rec["language"]:
            item["language"] = rec["language"]
        if rec["keywords"]:
            item["keyword"] = ", ".join(rec["keywords"])
        if rec["note"]:
            item["note"] = rec["note"]
        ext: dict[str, str] = {}
        if rec["title_cn"]:
            ext["title_cn"] = rec["title_cn"]
        if rec["title_en"]:
            ext["title_en"] = rec["title_en"]
        if rec["status"]:
            ext["status"] = rec["status"]
        if rec["folder"]:
            ext["folder"] = rec["folder"]
        if ext:
            item["paperpilot"] = ext
        items.append(item)
    return json.dumps(items, ensure_ascii=False, indent=2)


# ============================================================================
# 统一入口
# ============================================================================

FORMATS = ("csljson", "bibtex", "ris")

FORMAT_LABELS = {
    "csljson": "CSL JSON",
    "bibtex": "BibTeX",
    "ris": "RIS",
}

_EXT_TO_FORMAT = {
    ".json": "csljson",
    ".bib": "bibtex",
    ".bibtex": "bibtex",
    ".ris": "ris",
    ".txt": "ris",
}


def detect_format(filename: str, text: str) -> str:
    """按扩展名与内容特征识别格式，返回 'csljson' | 'bibtex' | 'ris'。"""
    lower = (filename or "").lower()
    for ext, fmt in _EXT_TO_FORMAT.items():
        if lower.endswith(ext):
            # .txt 需要进一步嗅探
            if fmt != "ris" or _sniff_ris(text):
                return fmt

    stripped = (text or "").lstrip()
    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            json.loads(text)
            return "csljson"
        except (json.JSONDecodeError, ValueError):
            pass
    if re.search(r"^\s*TY\s{1,2}-", text or "", re.MULTILINE):
        return "ris"
    if re.search(r"@[A-Za-z]+\s*[{(]", text or ""):
        return "bibtex"
    return "ris"


def _sniff_ris(text: str) -> bool:
    return bool(re.search(r"^\s*TY\s{1,2}-", text or "", re.MULTILINE))


def parse_any(text: str, fmt: str | None = None, filename: str = "") -> list[dict[str, Any]]:
    """按格式解析文本，返回中间表示列表。fmt 为空时自动识别。"""
    resolved = fmt if fmt in FORMATS else detect_format(filename, text)
    if resolved == "bibtex":
        return parse_bibtex(text)
    if resolved == "ris":
        return parse_ris(text)
    return parse_csl_json(text)


def serialize_any(records: list[dict[str, Any]], fmt: str) -> str:
    """按格式生成文本。"""
    if fmt == "bibtex":
        return serialize_bibtex(records)
    if fmt == "ris":
        return serialize_ris(records)
    return serialize_csl_json(records)


def format_extension(fmt: str) -> str:
    return {"csljson": "json", "bibtex": "bib", "ris": "ris"}.get(fmt, "txt")


def format_media_type(fmt: str) -> str:
    return {
        "csljson": "application/json",
        "bibtex": "application/x-bibtex",
        "ris": "application/x-research-info-systems",
    }.get(fmt, "text/plain")
