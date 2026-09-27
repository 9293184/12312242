"""文献格式解析 / 序列化测试（纯函数，不依赖数据库）。"""

from __future__ import annotations

from app.core import ref_formats as rf

BIBTEX = r"""
@article{vaswani2017attention,
  title = {Attention is All You Need},
  author = {Vaswani, Ashish and Shazeer, Noam and Parmar, Niki},
  journal = {Advances in Neural Information Processing Systems},
  volume = {30},
  pages = {5998--6008},
  year = {2017},
  doi = {10.5555/3295222.3295349},
  keywords = {transformer, attention, self-attention}
}

@inproceedings{zhang2020survey,
  title = {预训练模型综述},
  author = {{张三} and {李四}},
  booktitle = {计算机学报},
  year = {2020},
  pages = {1--10}
}

@article{accent1999,
  title = {Caf\'e na\"ive r\'esum\'e \& more},
  author = {M\"uller, J\"org},
  journal = {Zeitschrift f\"ur Physik},
  year = {1999}
}
"""

RIS = """TY  - JOUR
AU  - Shannon, Claude E.
TI  - A Mathematical Theory of Communication
T2  - Bell System Technical Journal
PY  - 1948
VL  - 27
IS  - 4
SP  - 379
EP  - 423
DO  - 10.1002/j.1538-7305.1948.tb01338.x
AB  - The fundamental problem of communication.
KW  - information theory
KW  - entropy
ER  - 
"""

CSL_CITEPROC = """[
  {
    "id": "shannon1948",
    "type": "article-journal",
    "title": "A Mathematical Theory of Communication",
    "author": [{"family": "Shannon", "given": "Claude E."}],
    "issued": {"date-parts": [[1948, 7]]},
    "container-title": "Bell System Technical Journal",
    "volume": "27",
    "issue": "4",
    "page": "379-423",
    "DOI": "10.1002/j.1538-7305.1948.tb01338.x",
    "abstract": "The fundamental problem of communication.",
    "keyword": "information theory, entropy"
  },
  {
    "id": "cn1",
    "type": "article-journal",
    "title": "深度学习综述",
    "author": [{"literal": "王晓明"}, {"family": "Li", "given": "Si"}],
    "issued": {"date-parts": [[2021]]}
  }
]
"""

CSL_ZOTERO_API = """[
  {
    "itemType": "journalArticle",
    "title": "Deep Learning",
    "creators": [
      {"creatorType": "author", "firstName": "Yann", "lastName": "LeCun"},
      {"creatorType": "author", "firstName": "Yoshua", "lastName": "Bengio"}
    ],
    "date": "2015-05-28",
    "publicationTitle": "Nature",
    "volume": "521",
    "pages": "436-444",
    "DOI": "10.1038/nature14539",
    "abstractNote": "Deep learning allows computational models composed of multiple layers.",
    "tags": [{"tag": "deep learning"}, {"tag": "representation learning"}]
  }
]
"""


class TestBibtex:
    def test_parses_entries(self):
        recs = rf.parse_bibtex(BIBTEX)
        assert len(recs) == 3

    def test_english_entry_fields(self):
        rec = rf.parse_bibtex(BIBTEX)[0]
        assert rec["title"] == "Attention is All You Need"
        assert rec["authors"] == ["Ashish Vaswani", "Noam Shazeer", "Niki Parmar"]
        assert rec["container"] == "Advances in Neural Information Processing Systems"
        assert rec["pages"] == "5998-6008"  # -- 归一化为 -
        assert rec["year"] == "2017"
        assert rec["doi"] == "10.5555/3295222.3295349"
        assert rec["keywords"] == ["transformer", "attention", "self-attention"]
        assert rec["type"] == "article-journal"

    def test_chinese_entry(self):
        rec = rf.parse_bibtex(BIBTEX)[1]
        assert rec["title"] == "预训练模型综述"
        assert rec["authors"] == ["张三", "李四"]
        assert rec["type"] == "paper-conference"

    def test_latex_accents_decoded(self):
        rec = rf.parse_bibtex(BIBTEX)[2]
        assert rec["title"] == "Café naïve résumé & more"
        assert rec["authors"] == ["Jörg Müller"]


class TestRis:
    def test_parses_entry(self):
        recs = rf.parse_ris(RIS)
        assert len(recs) == 1
        rec = recs[0]
        assert rec["title"] == "A Mathematical Theory of Communication"
        assert rec["authors"] == ["Claude E. Shannon"]
        assert rec["container"] == "Bell System Technical Journal"
        assert rec["year"] == "1948"
        assert rec["pages"] == "379-423"
        assert rec["doi"].startswith("10.1002/")
        assert rec["keywords"] == ["information theory", "entropy"]


class TestCslJson:
    def test_citeproc_dialect(self):
        recs = rf.parse_csl_json(CSL_CITEPROC)
        assert len(recs) == 2
        rec = recs[0]
        assert rec["title"] == "A Mathematical Theory of Communication"
        assert rec["authors"] == ["Claude E. Shannon"]
        assert rec["date"] == "1948-07"
        assert rec["keywords"] == ["information theory", "entropy"]

    def test_literal_author_kept(self):
        rec = rf.parse_csl_json(CSL_CITEPROC)[1]
        assert rec["authors"] == ["王晓明", "Si Li"]

    def test_zotero_api_dialect(self):
        rec = rf.parse_csl_json(CSL_ZOTERO_API)[0]
        assert rec["title"] == "Deep Learning"
        assert rec["authors"] == ["Yann LeCun", "Yoshua Bengio"]
        assert rec["container"] == "Nature"
        assert rec["keywords"] == ["deep learning", "representation learning"]
        assert rec["year"] == "2015"


class TestDetectFormat:
    def test_by_extension(self):
        assert rf.detect_format("a.bib", BIBTEX) == "bibtex"
        assert rf.detect_format("a.ris", RIS) == "ris"
        assert rf.detect_format("a.json", CSL_CITEPROC) == "csljson"

    def test_by_content_sniffing(self):
        assert rf.detect_format("x", BIBTEX) == "bibtex"
        assert rf.detect_format("x", RIS) == "ris"
        assert rf.detect_format("x", CSL_CITEPROC) == "csljson"


class TestRoundTrip:
    def test_all_formats_preserve_core_fields(self):
        source = rf.parse_csl_json(CSL_CITEPROC)
        for fmt in rf.FORMATS:
            back = rf.parse_any(rf.serialize_any(source, fmt), fmt)
            assert len(back) == len(source), fmt
            assert back[0]["title"] == source[0]["title"], fmt
            assert back[0]["authors"] == source[0]["authors"], fmt
            assert back[0]["keywords"] == source[0]["keywords"], fmt

    def test_csl_json_keeps_paperpilot_extension(self):
        rec = rf.normalize_record({
            "title": "Test Paper", "title_cn": "测试论文", "title_en": "Test Paper",
            "authors": ["Xiaoming Wang"], "status": "done", "folder": "我的文件夹",
            "keywords": ["a", "b"], "year": "2024",
        })
        back = rf.parse_any(rf.serialize_any([rec], "csljson"), "csljson")[0]
        assert back["title_cn"] == "测试论文"
        assert back["status"] == "done"
        assert back["folder"] == "我的文件夹"


class TestSerializationDetails:
    def test_bibtex_citekey_is_ascii(self):
        """中文名/标题不能让引用键变成非 ASCII（经典 bibtex 引擎会失败）。"""
        rec = rf.normalize_record({"title": "预训练模型综述", "authors": ["张三", "李四"], "year": "2020"})
        bib = rf.serialize_bibtex([rec])
        assert "@article{ref2020" in bib
        assert "预训练模型综述" in bib  # 标题本身仍保留中文

    def test_bibtex_preserves_existing_citekey(self):
        rec = rf.normalize_record({"title": "X", "citekey": "mykey2020"})
        assert "@article{mykey2020" in rf.serialize_bibtex([rec])

    def test_ris_da_only_emitted_with_month_or_day(self):
        year_only = rf.normalize_record({"title": "X", "year": "2020", "date": "2020"})
        ris = rf.serialize_ris([year_only])
        assert "PY  - 2020" in ris
        assert "DA  - " not in ris

        full_date = rf.normalize_record({"title": "X", "year": "2020", "date": "2020-05-28"})
        ris2 = rf.serialize_ris([full_date])
        assert "DA  - 2020-05-28" in ris2
