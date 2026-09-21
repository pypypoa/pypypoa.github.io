"""離線煙霧測試：用假 API 回應跑完整管線。

無需網路即可驗證五個來源的解析、去重、篩選、Crossref 四種驗證狀態，
以及 CSV/BibTeX/檢索紀錄三種輸出。

    python3 .claude/skills/trans-paper-search/scripts/test_pipeline.py
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import search as S

OA = {"results": [
    {"title": "Deep Reinforcement Learning for Traffic Signal Control",
     "doi": "https://doi.org/10.1016/J.TRC.2019.01.026", "publication_year": 2019,
     "cited_by_count": 420,
     "authorships": [{"author": {"display_name": "Wei, Hua"}},
                     {"author": {"display_name": "Zheng, Guanjie"}}],
     "primary_location": {"source": {"display_name": "Transportation Research Part C"}},
     "abstract_inverted_index": {"We": [0], "study": [1], "signals": [2]},
     "id": "https://openalex.org/W123"},
    {"title": "Bus Bunching: A Review",
     "doi": "10.1080/01441647.2020.1712345", "publication_year": 2020,
     "cited_by_count": 88,
     "authorships": [{"author": {"display_name": "Chen, Ming"}}],
     "primary_location": {"source": {"display_name": "Transport Reviews"}},
     "abstract_inverted_index": None, "id": "https://openalex.org/W124"},
    {"title": "A Paper That Does Not Exist In Crossref",
     "doi": "10.9999/fake.0001", "publication_year": 2023, "cited_by_count": 15,
     "authorships": [{"author": {"display_name": "Ghost, A"}}],
     "primary_location": {"source": {"display_name": "Journal of Nowhere"}},
     "abstract_inverted_index": None, "id": "https://openalex.org/W125"},
    {"title": "Mismatched Metadata Study",
     "doi": "10.1111/mismatch.2021", "publication_year": 2015, "cited_by_count": 30,
     "authorships": [{"author": {"display_name": "Lin, Y"}}],
     "primary_location": {"source": {"display_name": "Some Journal"}},
     "abstract_inverted_index": None, "id": "https://openalex.org/W126"},
    {"title": "Low Cited Paper", "doi": "10.2222/low", "publication_year": 2022,
     "cited_by_count": 1, "authorships": [{"author": {"display_name": "Wu, K"}}],
     "primary_location": {"source": {"display_name": "Predatory Journal of Transport"}},
     "abstract_inverted_index": None, "id": "https://openalex.org/W127"},
]}

S2 = {"data": [
    # 同一篇，DOI 大小寫不同 -> 應與 OpenAlex 合併
    {"paperId": "p1", "title": "Deep reinforcement learning for traffic signal control",
     "year": 2019, "venue": "Transportation Research Part C", "citationCount": 455,
     "externalIds": {"DOI": "10.1016/j.trc.2019.01.026"},
     "authors": [{"name": "Wei, Hua"}], "abstract": "s2 abstract",
     "openAccessPdf": {"url": "https://x/p1.pdf"}},
    # 新的一筆，無 DOI
    {"paperId": "p2", "title": "Signal Coordination in Urban Arterials", "year": 2021,
     "venue": "TRB Annual Meeting", "citationCount": 12, "externalIds": {},
     "authors": [{"name": "Hsu, Chia"}], "abstract": "no doi here",
     "openAccessPdf": {}},
]}

ARXIV = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry>
  <id>http://arxiv.org/abs/2101.00001v1</id>
  <published>2021-01-01T00:00:00Z</published>
  <title>Signal Coordination in Urban  Arterials</title>
  <summary>preprint version</summary>
  <author><name>Hsu, Chia</name></author>
</entry>
<entry>
  <id>http://arxiv.org/abs/1801.00002v1</id>
  <published>2018-01-01T00:00:00Z</published>
  <title>Old Preprint Before Year Filter</title>
  <summary>too old</summary>
  <author><name>Old, A</name></author>
</entry>
</feed>'''

CROSSREF = {
    "10.1016/j.trc.2019.01.026": {"message": {
        "title": ["Deep reinforcement learning for traffic signal control: A survey"],
        "container-title": ["Transportation Research Part C: Emerging Technologies"],
        "issued": {"date-parts": [[2019, 3]]}}},
    "10.1080/01441647.2020.1712345": {"message": {
        "title": ["Bus Bunching: A Review"], "container-title": ["Transport Reviews"],
        "issued": {"date-parts": [[2020]]}}},
    "10.1111/mismatch.2021": {"message": {
        "title": ["A Totally Unrelated Article About Marine Biology"],
        "container-title": ["Marine Biology"], "issued": {"date-parts": [[2021]]}}},
}

UNPAY = {
    "10.1016/j.trc.2019.01.026": {"oa_status": "green",
        "best_oa_location": {"url_for_pdf": "https://arxiv.org/pdf/1904.08117.pdf"}},
    "10.1080/01441647.2020.1712345": {"oa_status": "closed", "best_oa_location": None},
}

calls = []

def fake_get_json(self, url, params=None, headers=None):
    calls.append(url)
    self.calls += 1
    if url.startswith(S.OPENALEX_API):
        return OA if (params or {}).get("page") == 1 else {"results": []}
    if url.startswith(S.S2_API):
        return S2 if (params or {}).get("offset") == 0 else {"data": []}
    if url.startswith(S.CROSSREF_API):
        return CROSSREF.get(url[len(S.CROSSREF_API):].replace("%2F", "/"))
    if url.startswith(S.UNPAYWALL_API):
        return UNPAY.get(url[len(S.UNPAYWALL_API):].replace("%2F", "/"))
    raise AssertionError("unexpected " + url)

def fake_get_text(self, url, params=None, headers=None):
    calls.append(url)
    self.calls += 1
    if url.startswith(S.ARXIV_API):
        return ARXIV if (params or {}).get("start") == 0 else None
    raise AssertionError("unexpected " + url)

S.Http.get_json = fake_get_json
S.Http.get_text = fake_get_text

outdir = tempfile.mkdtemp(prefix="tps-test-")
excl = outdir + "/excl.txt"
open(excl, "w").write("# 掠奪性期刊\nPredatory Journal of Transport\n")

rc = S.main(["traffic signal control", "--email", "test@example.edu.tw",
             "--from-year", "2019", "--min-citations", "5",
             "--exclude-venues", excl, "--target", "10",
             "--outdir", outdir, "--prefix", "test"])
csv_text = open(outdir + "/test.csv", encoding="utf-8-sig").read()
bib_text = open(outdir + "/test.bib", encoding="utf-8").read()
log_text = open(outdir + "/test-search-log.md", encoding="utf-8").read()

print("=== CSV ===\n" + csv_text)
print("=== BIB ===\n" + bib_text)
print("=== LOG ===\n" + log_text)

# --- 斷言 ---
failures = []
def check(cond, msg):
    if not cond:
        failures.append(msg)

check(rc == 0, f"exit code 應為 0，實際 {rc}")
# 跨來源去重：OpenAlex 與 Semantic Scholar 的同一篇（DOI 大小寫不同）要合併
check("OpenAlex, Semantic Scholar" in csv_text, "DOI 大小寫不同的同一篇未合併")
# 無 DOI 的同一篇（Semantic Scholar 與 arXiv）也要合併
check("Semantic Scholar, arXiv" in csv_text, "無 DOI 的同一篇未跨來源合併")
# 四種驗證狀態
check(csv_text.count("已驗證") >= 2, "已驗證筆數不足")
check("查無此文" in csv_text, "未標記查無此文")
check("無 DOI，Crossref 無法對帳" in csv_text, "未標記無 DOI 未驗證")
# Crossref 權威欄位覆寫
check("A survey" in csv_text, "已驗證書目未以 Crossref 標題覆寫")
# 篩選條件
check("Predatory" not in csv_text, "排除清單中的期刊未被淘汰")
check("Low Cited Paper" not in csv_text, "低於引用數門檻者未被淘汰")
check("Old Preprint" not in csv_text, "早於年限者未被淘汰")
# BibTeX：查無此文不得進書目檔；citation key 取姓不取名
check("fake.0001" not in bib_text, "查無此文的書目進了 BibTeX")
check("@article{wei2019" in bib_text, "citation key 未取第一作者的姓")
check("@misc{" in bib_text and "howpublished" in bib_text, "無 DOI 者未輸出為 @misc")
# 檢索紀錄
check("逐輪命中紀錄" in log_text and "誠實的邊界" in log_text, "檢索紀錄缺少必要章節")

print("=" * 60)
if failures:
    for f in failures:
        print("FAIL:", f)
    sys.exit(1)
print("全部斷言通過")
