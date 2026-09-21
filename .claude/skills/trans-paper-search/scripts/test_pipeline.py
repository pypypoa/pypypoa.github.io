"""離線測試套件：不需網路即可完整驗證 trans-paper-search 管線。

    python3 .claude/skills/trans-paper-search/scripts/test_pipeline.py [-v]

涵蓋：正規化與相似度、作者姓名三種格式、摘要還原、去重（雙向與三來源合併）、
三種篩選條件、Crossref 四種驗證狀態、Unpaywall 取連結、HTTP 重試與狀態碼分流、
三個檢索來源的解析與分頁、patience 停止條件、畸形 API 回應、CJK 書目，
以及 main() 的端到端輸出（CSV / BibTeX / 檢索紀錄）與離場碼。
"""

from __future__ import annotations

import io
import os
import sys
import tempfile
import time
import traceback
import urllib.error

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import search as S

VERBOSE = "-v" in sys.argv

# 測試期間不要真的睡（重試退避會讓套件跑很久）
time.sleep = lambda *_a, **_k: None


# --------------------------------------------------------------------------- #
# 極簡測試框架
# --------------------------------------------------------------------------- #

TESTS: list = []
RESULTS: list[tuple[str, str, str]] = []


def test(fn):
    TESTS.append(fn)
    return fn


def eq(got, want, what=""):
    if got != want:
        raise AssertionError(f"{what}：預期 {want!r}，實際 {got!r}")


def ok(cond, what=""):
    if not cond:
        raise AssertionError(what or "斷言失敗")


def contains(haystack, needle, what=""):
    if needle not in haystack:
        raise AssertionError(f"{what}：找不到 {needle!r}")


def absent(haystack, needle, what=""):
    if needle in haystack:
        raise AssertionError(f"{what}：不該出現 {needle!r}")


# --------------------------------------------------------------------------- #
# 假 HTTP 層
# --------------------------------------------------------------------------- #

class FakeHttp(S.Http):
    """以 (url 前綴, params) → 回應 的 handler 取代真實網路。"""

    def __init__(self, handler, email="test@example.edu.tw"):
        super().__init__(email, delay=0, retries=4, timeout=1)
        self.handler = handler
        self.requests: list[tuple[str, dict]] = []

    def get_json(self, url, params=None, headers=None):
        self.calls += 1
        self.requests.append((url, params or {}))
        return self.handler(url, params or {}, headers or {})

    def get_text(self, url, params=None, headers=None):
        self.calls += 1
        self.requests.append((url, params or {}))
        return self.handler(url, params or {}, headers or {})


def rec(**kw) -> S.Record:
    kw.setdefault("title", "t")
    return S.Record(**kw)


# --------------------------------------------------------------------------- #
# 1. 正規化與相似度
# --------------------------------------------------------------------------- #

@test
def t_normalize_text():
    eq(S.normalize_text("  Traffic   Signal-Control!  "), "traffic signal control", "壓縮空白與去標點")
    eq(S.normalize_text("Café Résumé"), "cafe resume", "去重音")
    eq(S.normalize_text("Ｆｕｌｌｗｉｄｔｈ"), "fullwidth", "全形轉半形")
    eq(S.normalize_text("運輸計劃季刊"), "運輸計劃季刊", "保留中日韓字")
    eq(S.normalize_text(""), "", "空字串")
    eq(S.normalize_text(None), "", "None")


@test
def t_normalize_doi():
    for raw in ["10.1/AbC", "https://doi.org/10.1/abc", "http://dx.doi.org/10.1/ABC",
                "doi: 10.1/abc", " 10.1/aBc "]:
        eq(S.normalize_doi(raw), "10.1/abc", f"DOI 正規化 {raw!r}")
    for raw in ["", None, "not-a-doi", "arXiv:2101.00001", "11.1/abc"]:
        eq(S.normalize_doi(raw), "", f"非 DOI 應回空字串 {raw!r}")


@test
def t_title_similarity():
    eq(S.title_similarity("A Study of Bus Delay", "a study of bus delay!"), 1.0, "僅大小寫與標點差異")
    ok(S.title_similarity("Bus delay analysis", "Marine biology of whales") < 0.5, "無關標題應低分")
    eq(S.title_similarity("", "x"), 0.0, "空標題")


@test
def t_first_author_surname():
    eq(rec(authors=["Wei, Hua"]).first_author_surname, "wei", "姓在前逗號格式")
    eq(rec(authors=["Hua Wei"]).first_author_surname, "wei", "名在前空白格式")
    eq(rec(authors=["王明"]).first_author_surname, "王明", "中文姓名")
    eq(rec(authors=[]).first_author_surname, "", "無作者")
    eq(rec(authors=["  Chen ,  Ming "]).first_author_surname, "chen", "多餘空白")


@test
def t_reconstruct_abstract():
    eq(S.reconstruct_abstract({"The": [0], "bus": [1], "delay": [2]}), "The bus delay", "基本還原")
    eq(S.reconstruct_abstract({"the": [0, 2], "bus": [1]}), "the bus the", "同字多位置")
    eq(S.reconstruct_abstract(None), "", "None")
    eq(S.reconstruct_abstract({}), "", "空 dict")


# --------------------------------------------------------------------------- #
# 2. 去重
# --------------------------------------------------------------------------- #

@test
def t_dedup_doi_case_and_prefix():
    d = S.Deduper()
    a = rec(title="Deep RL for Signals", authors=["Wei, Hua"], year=2019,
            doi="10.1016/J.TRC.2019.01.026", sources=["OpenAlex"], citations=10)
    b = rec(title="deep rl for signals", authors=["Wei, Hua"], year=2019,
            doi="https://doi.org/10.1016/j.trc.2019.01.026", sources=["Semantic Scholar"],
            citations=25)
    eq([d.add(a), d.add(b)], [True, False], "DOI 大小寫/前綴不同應視為同一篇")
    eq(len(d.records()), 1, "去重後筆數")
    eq(d.records()[0].citations, 25, "引用數取較大值")
    eq(d.records()[0].sources, ["OpenAlex", "Semantic Scholar"], "來源合併")


@test
def t_dedup_bidirectional():
    """有 DOI 先進、無 DOI 後到（以及反向）都要認得出來。"""
    for order in ("doi_first", "nodoi_first"):
        d = S.Deduper()
        with_doi = rec(title="Bus Bunching Analysis", authors=["Wang, M"], year=2019,
                       doi="10.5/q", sources=["OpenAlex"])
        no_doi = rec(title="bus  bunching   analysis", authors=["Wang, M"], year=2019,
                     sources=["arXiv"], abstract="preprint")
        seq = [with_doi, no_doi] if order == "doi_first" else [no_doi, with_doi]
        flags = [d.add(x) for x in seq]
        eq(flags, [True, False], f"{order}：第二筆應被合併")
        eq(len(d.records()), 1, f"{order}：筆數")
        eq(d.records()[0].doi, "10.5/q", f"{order}：DOI 應保留")
        eq(len(d.records()[0].sources), 2, f"{order}：來源合併")


@test
def t_dedup_three_sources_reindex():
    """先無 DOI、再補上 DOI，第三個來源帶同一 DOI 來時不能漏接。"""
    d = S.Deduper()
    n = rec(title="Signal Coordination", authors=["Hsu, C"], year=2021, sources=["arXiv"])
    m = rec(title="Signal Coordination", authors=["Hsu, C"], year=2021, doi="10.5/q",
            sources=["OpenAlex"])
    o = rec(title="A completely different title", authors=["Zhao, L"], year=2021,
            doi="10.5/Q", sources=["Semantic Scholar"])
    eq([d.add(x) for x in (n, m, o)], [True, False, False], "第三筆同 DOI 應合併")
    eq(len(d.records()), 1, "筆數")
    eq(d.records()[0].sources, ["arXiv", "OpenAlex", "Semantic Scholar"], "三來源合併")


@test
def t_dedup_enrichment_and_rejects():
    d = S.Deduper()
    thin = rec(title="X Study", authors=["Lin, Y"], year=2020, sources=["arXiv"])
    rich = rec(title="X Study", authors=["Lin, Y", "Ko, J"], year=2020, venue="J Transp",
               abstract="abs", url="http://u", citations=7, sources=["OpenAlex"])
    d.add(thin)
    d.add(rich)
    r = d.records()[0]
    eq(r.authors, ["Lin, Y", "Ko, J"], "作者清單取較完整者")
    eq((r.venue, r.abstract, r.url, r.citations), ("J Transp", "abs", "http://u", 7), "補齊缺漏欄位")
    eq(d.add(rec(title="", authors=["A"], year=2020)), False, "無標題應拒收")
    eq(len(d.records()), 1, "無標題不進池")


@test
def t_dedup_distinct_stay_distinct():
    d = S.Deduper()
    eq([d.add(rec(title="Paper A", authors=["Li, W"], year=2020, doi="10.1/a")),
        d.add(rec(title="Paper B", authors=["Li, W"], year=2020, doi="10.1/b")),
        d.add(rec(title="Paper A", authors=["Li, W"], year=2021, doi="10.1/c"))],
       [True, True, True], "不同文獻不應被誤併")
    eq(len(d.records()), 3, "筆數")


# --------------------------------------------------------------------------- #
# 3. 篩選
# --------------------------------------------------------------------------- #

@test
def t_filters():
    base = dict(title="t", venue="Transport Reviews", year=2021, citations=50)
    okc, why = S.passes_filters(rec(**base), 2019, 10, ["predatory"])
    ok(okc and why == "", "全數通過")

    bad, why = S.passes_filters(rec(**{**base, "year": 2015}), 2019, 0, [])
    ok(not bad and "早於" in why, f"年限淘汰：{why}")

    bad, why = S.passes_filters(rec(**{**base, "citations": 2}), None, 10, [])
    ok(not bad and "引用數" in why, f"引用數淘汰：{why}")

    bad, why = S.passes_filters(
        rec(**{**base, "venue": "Intl J of PREDATORY Transport!"}), None, 0,
        [S.normalize_text("predatory transport")])
    ok(not bad and "排除清單" in why, f"期刊淘汰（大小寫與標點不敏感）：{why}")

    okc, _ = S.passes_filters(rec(**{**base, "year": None}), 2019, 0, [])
    ok(okc, "年份未知時不應被年限淘汰")
    okc, _ = S.passes_filters(rec(**{**base, "citations": None}), None, 0, [])
    ok(okc, "引用數未知且門檻為 0 時應通過")


@test
def t_load_exclusions():
    p = os.path.join(tempfile.mkdtemp(), "ex.txt")
    with open(p, "w", encoding="utf-8") as fh:
        fh.write("# 註解不算\n\nPredatory Journal of Transport\n  Fake  Press!  \n")
    got = S.load_exclusions(p)
    eq(got, ["predatory journal of transport", "fake press"], "排除清單載入與正規化")
    eq(S.load_exclusions(None), [], "未指定時為空")


# --------------------------------------------------------------------------- #
# 4. Crossref 驗證
# --------------------------------------------------------------------------- #

CR_DB = {
    "10.1/ok": {"message": {"title": ["Deep RL for Traffic Signal Control: A Survey"],
                            "container-title": ["Transportation Research Part C"],
                            "issued": {"date-parts": [[2019, 3]]}}},
    "10.1/yearoff": {"message": {"title": ["Same Title Here"], "container-title": ["J"],
                                 "issued": {"date-parts": [[2010]]}}},
    "10.1/wrongtitle": {"message": {"title": ["A Study of Marine Biology"],
                                    "container-title": ["Marine Biology"],
                                    "issued": {"date-parts": [[2021]]}}},
    "10.1/notitle": {"message": {"title": [], "container-title": [],
                                 "issued": {"date-parts": [[None]]}}},
}


DC_DB = {
    # arXiv 的 DOI 走 DataCite 登記，Crossref 查不到
    "10.48550/arxiv.2401.12345": {"data": {"attributes": {
        "titles": [{"title": "Deep RL for Traffic Signal Control"}],
        "publicationYear": 2024, "publisher": "arXiv"}}},
    # 標題對不上的 DataCite 記錄
    "10.5281/zenodo.999": {"data": {"attributes": {
        "titles": [{"title": "An Unrelated Dataset About Whales"}],
        "publicationYear": 2020, "publisher": "Zenodo"}}},
    # publisher 為物件的新版 schema
    "10.5281/zenodo.777": {"data": {"attributes": {
        "titles": [{"lang": "en"}, {"title": "Object Publisher Record"}],
        "publicationYear": 2023, "publisher": {"name": "Zenodo"}}}},
}


def cr_handler(url, params, headers):
    """Crossref 查得到就回，查不到再讓 DataCite 回。"""
    if url.startswith(S.CROSSREF_API):
        ok(params.get("mailto"), "Crossref 呼叫應帶 mailto")
        return CR_DB.get(url[len(S.CROSSREF_API):].replace("%2F", "/"))
    if url.startswith(S.DATACITE_API):
        return DC_DB.get(url[len(S.DATACITE_API):].replace("%2F", "/"))
    raise AssertionError("未預期的 URL " + url)


@test
def t_verify_states():
    h = FakeHttp(cr_handler)

    r = rec(title="Deep RL for traffic signal control - a survey", doi="10.1/ok", year=2019)
    S.verify_record(h, r, 0.90)
    eq(r.verification, "已驗證", "標題與年份吻合")
    eq(r.title, "Deep RL for Traffic Signal Control: A Survey", "應以 Crossref 標題覆寫")
    eq(r.venue, "Transportation Research Part C", "應以 Crossref 期刊覆寫")
    eq(r.year, 2019, "年份覆寫")

    r = rec(title="Mismatch", doi="10.1/wrongtitle", year=2021)
    S.verify_record(h, r, 0.90)
    eq(r.verification, "欄位不符", "標題差異過大")
    contains(r.verify_note, "標題相似度", "應說明原因")
    eq(r.authority_title, "A Study of Marine Biology", "保留註冊機構原值供人工判讀")
    eq(r.registry, "Crossref", "應記錄是哪個註冊機構對上的")

    r = rec(title="Same Title Here", doi="10.1/yearoff", year=2021)
    S.verify_record(h, r, 0.90)
    eq(r.verification, "欄位不符", "年份差超過一年")
    contains(r.verify_note, "年份不符", "應說明原因")

    r = rec(title="Same Title Here", doi="10.1/yearoff", year=2011)
    S.verify_record(h, r, 0.90)
    eq(r.verification, "已驗證", "年份差 1 年應容忍")

    r = rec(title="Ghost paper", doi="10.9/nope", year=2023)
    S.verify_record(h, r, 0.90)
    eq(r.verification, "查無此文", "Crossref 查不到")
    contains(r.verify_note, "Crossref 與 DataCite 都查不到", "應說明兩邊都查過")

    r = rec(title="Preprint", doi="", year=2021)
    S.verify_record(h, r, 0.90)
    eq(r.verification, "未驗證", "無 DOI")
    contains(r.verify_note, "無 DOI，無法向註冊機構對帳", "應說明原因")
    eq(r.registry, "", "無 DOI 時不該有註冊機構")

    # Crossref 沒給標題時不應誤判為欄位不符
    r = rec(title="Whatever", doi="10.1/notitle", year=2021)
    S.verify_record(h, r, 0.90)
    eq(r.verification, "已驗證", "Crossref 無標題時不以標題否決")
    eq(r.title, "Whatever", "無 Crossref 標題時保留原標題")


@test
def t_verify_datacite_fallback():
    """Crossref 查無時改查 DataCite：arXiv、Zenodo 的 DOI 是真實存在的。"""
    h = FakeHttp(cr_handler)

    r = rec(title="Deep RL for traffic signal control", year=2024,
            doi="10.48550/arXiv.2401.12345")
    S.verify_record(h, r, 0.90)
    eq(r.verification, "已驗證（DataCite）", "DataCite 登記的 DOI 不該被判為查無此文")
    eq(r.registry, "DataCite", "應記錄註冊機構")
    eq(r.venue, "arXiv", "應以 DataCite 的 publisher 當發表處")
    eq(r.year, 2024, "年份覆寫")
    eq(r.title, "Deep RL for Traffic Signal Control", "標題覆寫")

    # DataCite 也有此 DOI，但標題對不上 → 欄位不符，而非查無此文
    r = rec(title="Bus Bunching in Taipei", year=2020, doi="10.5281/zenodo.999")
    S.verify_record(h, r, 0.90)
    eq(r.verification, "欄位不符", "DataCite 有此 DOI 但標題不符")
    contains(r.verify_note, "DataCite 有此 DOI", "應說明是哪個機構有")

    # 兩邊都查不到才是查無此文
    r = rec(title="Ghost", year=2023, doi="10.9999/nowhere")
    S.verify_record(h, r, 0.90)
    eq(r.verification, "查無此文", "兩邊都查無")

    # publisher 為物件、titles 首項缺 title 的新版 schema
    r = rec(title="Object Publisher Record", year=2023, doi="10.5281/zenodo.777")
    S.verify_record(h, r, 0.90)
    eq(r.verification, "已驗證（DataCite）", "應容忍新版 schema")
    eq(r.venue, "Zenodo", "publisher 為物件時取 name")


@test
def t_verify_prefers_crossref_and_skips_datacite_when_found():
    """Crossref 查到就不該再多打一次 DataCite。"""
    seen = []

    def handler(url, params, headers):
        seen.append("cr" if url.startswith(S.CROSSREF_API) else "dc")
        return cr_handler(url, params, headers)

    h = FakeHttp(handler)
    r = rec(title="Deep RL for traffic signal control - a survey",
            doi="10.1/ok", year=2019)
    S.verify_record(h, r, 0.90)
    eq(seen, ["cr"], "Crossref 命中後不應再查 DataCite")
    eq(r.registry, "Crossref", "註冊機構")


@test
def t_fetch_helpers_return_none_on_missing():
    h = FakeHttp(cr_handler)
    eq(S.fetch_crossref(h, "10.9/none"), None, "Crossref 查無回 None")
    eq(S.fetch_datacite(h, "10.9/none"), None, "DataCite 查無回 None")
    eq(S.fetch_crossref(h, "10.1/ok")[1], 2019, "Crossref 年份")
    eq(S.fetch_datacite(h, "10.48550/arxiv.2401.12345")[2], "arXiv", "DataCite publisher")


@test
def t_verify_threshold_is_configurable():
    h = FakeHttp(cr_handler)
    r1 = rec(title="A Study of Marine Life", doi="10.1/wrongtitle", year=2021)
    S.verify_record(h, r1, 0.99)
    eq(r1.verification, "欄位不符", "高門檻應否決")
    r2 = rec(title="A Study of Marine Life", doi="10.1/wrongtitle", year=2021)
    S.verify_record(h, r2, 0.50)
    eq(r2.verification, "已驗證", "低門檻應通過")


# --------------------------------------------------------------------------- #
# 5. Unpaywall
# --------------------------------------------------------------------------- #

@test
def t_fetch_oa():
    db = {
        "10.1/pdf": {"oa_status": "green",
                     "best_oa_location": {"url_for_pdf": "https://x/a.pdf",
                                          "url": "https://x/landing"}},
        "10.1/landing": {"oa_status": "hybrid",
                         "best_oa_location": {"url": "https://x/landing"}},
        "10.1/closed": {"oa_status": "closed", "best_oa_location": None},
    }

    def handler(url, params, headers):
        ok(params.get("email"), "Unpaywall 呼叫應帶 email")
        return db.get(url[len(S.UNPAYWALL_API):].replace("%2F", "/"))

    h = FakeHttp(handler)
    r = rec(doi="10.1/pdf"); S.fetch_oa(h, r)
    eq((r.oa_status, r.oa_pdf_url), ("green", "https://x/a.pdf"), "優先取 PDF 連結")
    r = rec(doi="10.1/landing"); S.fetch_oa(h, r)
    eq(r.oa_pdf_url, "https://x/landing", "無 PDF 時退回 landing page")
    r = rec(doi="10.1/closed"); S.fetch_oa(h, r)
    eq((r.oa_status, r.oa_pdf_url), ("closed", ""), "閉鎖取用無連結")
    r = rec(doi="10.1/unknown"); S.fetch_oa(h, r)
    eq(r.oa_pdf_url, "", "查無記錄")
    before = h.calls
    r = rec(doi=""); S.fetch_oa(h, r)
    eq(h.calls, before, "無 DOI 不應發出請求")


# --------------------------------------------------------------------------- #
# 6. HTTP 層：重試、退避、狀態碼分流
# --------------------------------------------------------------------------- #

def make_http_with_responses(responses, **kw):
    """responses：每次呼叫要「丟出的例外」或「回傳的位元組」。"""
    h = S.Http("t@e.tw", delay=0, **kw)
    seq = list(responses)

    def fake_urlopen(req, timeout=None):
        item = seq.pop(0)
        if isinstance(item, Exception):
            raise item
        return io.BytesIO(item) if isinstance(item, bytes) else item

    import urllib.request as ur
    h._orig = ur.urlopen
    ur.urlopen = fake_urlopen
    return h, lambda: setattr(ur, "urlopen", h._orig)


def http_error(code):
    return urllib.error.HTTPError("http://x", code, "err", {}, None)


@test
def t_http_retries_then_succeeds():
    h, restore = make_http_with_responses([http_error(503), http_error(429), b'{"a":1}'])
    try:
        eq(h.get_json("https://x/y"), {"a": 1}, "退避後成功")
        eq(h.failures, 0, "成功不計失敗")
        eq(h.calls, 3, "共三次嘗試")
    finally:
        restore()


@test
def t_http_exhausts_retries():
    h, restore = make_http_with_responses([http_error(503)] * 4, retries=4)
    try:
        eq(h.get_json("https://x/y"), None, "重試用盡回 None")
        eq(h.failures, 1, "應計一次失敗")
    finally:
        restore()


@test
def t_http_404_is_an_answer_not_a_failure():
    h, restore = make_http_with_responses([http_error(404)])
    try:
        eq(h.get_json("https://x/y"), None, "404 回 None")
        eq(h.failures, 0, "404 是『查無此文』，不算連線失敗")
        eq(h.calls, 1, "404 不應重試")
    finally:
        restore()


@test
def t_http_406_not_retried_and_host_recorded():
    """406 是中間設備插入的，重試只會白等退避時間。"""
    h, restore = make_http_with_responses([http_error(406)])
    try:
        eq(h.get_json("https://export.arxiv.org/api/query?x=1"), None, "406 回 None")
        eq(h.calls, 1, "406 不應重試")
        eq(h.failures, 1, "應計失敗")
        eq(h.middlebox_hosts, {"export.arxiv.org"}, "應記下疑似被阻擋的主機")
    finally:
        restore()


@test
def t_http_4xx_not_retried():
    h, restore = make_http_with_responses([http_error(401)])
    try:
        eq(h.get_json("https://x/y"), None, "401 回 None")
        eq(h.calls, 1, "非 429/5xx 不重試")
        eq(h.failures, 1, "應計失敗")
    finally:
        restore()


@test
def t_http_urlerror_retried_and_counted():
    err = urllib.error.URLError("Tunnel connection failed: 403 Forbidden")
    h, restore = make_http_with_responses([err] * 4, retries=4)
    try:
        eq(h.get_json("https://x/y"), None, "連線失敗回 None")
        eq(h.calls, 4, "應重試至上限")
        eq(h.failures, 1, "應計失敗")
    finally:
        restore()


@test
def t_http_bad_json_is_handled():
    h, restore = make_http_with_responses([b"<html>not json</html>"])
    try:
        eq(h.get_json("https://x/y"), None, "畸形 JSON 不應拋出例外")
    finally:
        restore()


# --------------------------------------------------------------------------- #
# 7. 三個檢索來源的解析
# --------------------------------------------------------------------------- #

@test
def t_openalex_parsing_and_params():
    page1 = {"results": [
        {"title": "Full Record", "doi": "https://doi.org/10.1/A",
         "publication_year": 2021, "cited_by_count": 9,
         "authorships": [{"author": {"display_name": "Wei, Hua"}}, {"author": None}],
         "primary_location": {"source": {"display_name": "TR Part C"}},
         "abstract_inverted_index": {"We": [0], "study": [1]},
         "id": "https://openalex.org/W1"},
        # 欄位大量缺漏，不應拋出例外
        {"display_name": "Sparse Record", "authorships": [], "primary_location": None,
         "id": "https://openalex.org/W2"},
    ]}

    def handler(url, params, headers):
        eq(params["mailto"], "test@example.edu.tw", "應帶 mailto（polite pool）")
        eq(params["filter"], "from_publication_date:2019-01-01", "年限應下到 API")
        return page1 if params["page"] == 1 else {"results": []}

    h = FakeHttp(handler)
    batches = list(S.search_openalex(h, "q", rounds=3, per_page=50, from_year=2019))
    eq(len(batches), 1, "回傳未滿一頁即停止分頁")
    a, b = batches[0]
    eq((a.doi, a.year, a.citations, a.venue), ("10.1/a", 2021, 9, "TR Part C"), "完整記錄解析")
    eq(a.authors, ["Wei, Hua"], "應略過 author 為 None 者")
    eq(a.abstract, "We study", "摘要還原")
    eq((b.title, b.doi, b.venue, b.year), ("Sparse Record", "", "", None), "缺漏欄位降級處理")


@test
def t_openalex_pagination_advances():
    seen = []

    def handler(url, params, headers):
        seen.append(params["page"])
        return {"results": [{"title": f"P{params['page']}-{i}", "authorships": [],
                             "primary_location": None, "doi": f"10.1/p{params['page']}i{i}"}
                            for i in range(2)]}

    h = FakeHttp(handler)
    batches = list(S.search_openalex(h, "q", rounds=3, per_page=2, from_year=None))
    eq(seen, [1, 2, 3], "每輪頁碼應遞增")
    eq(len(batches), 3, "滿頁應繼續抓下一輪")


@test
def t_s2_parsing_offset_and_ratelimit_pacing():
    offsets = []

    def handler(url, params, headers):
        offsets.append(params["offset"])
        eq(params["year"], "2019-", "年限應下到 API")
        if params["offset"] == 0:
            return {"data": [
                {"paperId": "p1", "title": "With DOI", "year": 2019, "venue": "V",
                 "citationCount": 5, "externalIds": {"DOI": "10.1/S2"},
                 "authors": [{"name": "A B"}], "abstract": "x",
                 "openAccessPdf": {"url": "https://x.pdf"}},
                {"paperId": "p2", "title": "No DOI", "year": 2020, "externalIds": {},
                 "authors": [], "openAccessPdf": {}},
            ]}
        return {"data": []}

    h = FakeHttp(handler)
    os.environ.pop("S2_API_KEY", None)
    delay_before = h.delay
    # per_page=2 讓第一輪「滿頁」，才會續抓下一輪
    batches = list(S.search_semantic_scholar(h, "q", rounds=3, per_page=2, from_year=2019))
    eq(h.delay, delay_before, "跑完應還原原本的節流設定")
    eq(offsets, [0, 2], "滿頁時 offset 應以 per_page 遞增；下一輪回空即停")
    a, b = batches[0]
    eq(a.doi, "10.1/s2", "DOI 應正規化為小寫")
    eq(a.url, "https://www.semanticscholar.org/paper/p1", "有 paperId 時用 S2 連結")
    eq((b.doi, b.venue), ("", ""), "缺 DOI 與 venue 應降級")


@test
def t_s2_uses_api_key_when_present():
    captured = {}

    def handler(url, params, headers):
        captured.update(headers)
        return {"data": []}

    h = FakeHttp(handler)
    os.environ["S2_API_KEY"] = "secret123"
    try:
        list(S.search_semantic_scholar(h, "q", rounds=1, per_page=10, from_year=None))
        eq(captured.get("x-api-key"), "secret123", "有 key 時應帶入 header")
    finally:
        os.environ.pop("S2_API_KEY", None)


ARXIV_XML = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry>
  <id>http://arxiv.org/abs/2101.00001v1</id>
  <published>2021-01-01T00:00:00Z</published>
  <title>Signal Coordination
   in Urban Arterials</title>
  <summary>line one
  line two</summary>
  <author><name>Hsu, Chia</name></author>
  <author><name>Ko, Jen</name></author>
  <arxiv:doi>10.1/ARXIVDOI</arxiv:doi>
  <arxiv:journal_ref>Transportation Science 55(2)</arxiv:journal_ref>
</entry>
<entry>
  <id>http://arxiv.org/abs/1801.00002v1</id>
  <published>2018-01-01T00:00:00Z</published>
  <title>Too Old Preprint</title>
  <summary>old</summary>
  <author><name>Old, A</name></author>
</entry>
<entry>
  <id>http://arxiv.org/abs/2202.00003v1</id>
  <published>2022-02-02T00:00:00Z</published>
  <title>Bare Preprint</title>
  <summary>no doi no journal</summary>
  <author><name>Bare, B</name></author>
</entry>
</feed>'''


@test
def t_arxiv_parsing_and_year_filter():
    def handler(url, params, headers):
        contains(params["search_query"], "traffic flow", "查詢字串應帶入")
        return ARXIV_XML if params["start"] == 0 else None

    h = FakeHttp(handler)
    batches = list(S.search_arxiv(h, "traffic flow", rounds=2, per_page=50, from_year=2019))
    got = batches[0]
    eq(len(got), 2, "早於年限者應被排除")
    a, c = got
    eq(a.title, "Signal Coordination in Urban Arterials", "跨行標題應壓成一行")
    eq(a.abstract, "line one line two", "跨行摘要應壓成一行")
    eq(a.doi, "10.1/arxivdoi", "DOI 應正規化")
    eq(a.venue, "Transportation Science 55(2)", "有 journal_ref 時用它")
    eq(len(a.authors), 2, "多作者")
    eq((c.doi, c.venue), ("", "arXiv (preprint)"), "無 DOI/journal_ref 的預印本")


@test
def t_arxiv_malformed_xml_is_survivable():
    h = FakeHttp(lambda url, params, headers: "<feed><unclosed>")
    eq(list(S.search_arxiv(h, "q", rounds=2, per_page=10, from_year=None)), [[]],
       "畸形 XML 應回空批次而非拋出例外")
    h2 = FakeHttp(lambda url, params, headers: None)
    eq(list(S.search_arxiv(h2, "q", rounds=2, per_page=10, from_year=None)), [[]],
       "無回應應回空批次")


# --------------------------------------------------------------------------- #
# 8. 停止條件
# --------------------------------------------------------------------------- #

@test
def t_main_patience_stop():
    """main() 的停止條件：連續 patience 輪無新命中即停止本來源。

    handler 每輪都回同一筆（且滿頁，所以分頁不會自己停），
    只有 patience 能讓它停下來。
    """
    rounds = {"n": 0}

    def handler(url, params, headers):
        if url.startswith(S.OPENALEX_API):
            rounds["n"] += 1
            return {"results": [{"title": "Same", "doi": "10.1/same", "authorships": [],
                                 "primary_location": None, "publication_year": 2021,
                                 "cited_by_count": 3}]}
        if url.startswith(S.ARXIV_API):
            return '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        if (url.startswith(S.CROSSREF_API) or url.startswith(S.DATACITE_API)
                or url.startswith(S.UNPAYWALL_API)):
            return None
        return {"data": []}

    rc, outdir, out, _ = run_main(["q", "--email", "t@e.tw", "--sources", "openalex",
                                   "--per-page", "1", "--max-rounds", "9",
                                   "--patience", "2"], handler=handler)
    eq(rc, 0, "離場碼")
    contains(out, "連續 2 輪無新命中", "應印出停止原因")
    eq(rounds["n"], 3, "第 1 輪新增、第 2/3 輪無新命中即停：只該發 3 次請求，不是 9 次")

    # patience 調大就會多跑幾輪
    rounds["n"] = 0
    rc, _, out2, _ = run_main(["q", "--email", "t@e.tw", "--sources", "openalex",
                               "--per-page", "1", "--max-rounds", "9",
                               "--patience", "4"], handler=handler)
    eq(rounds["n"], 5, "patience=4 應跑到第 5 輪才停")


@test
def t_main_target_stop():
    """累積達 target 三倍候選即停止本來源。"""
    rounds = {"n": 0}

    def handler(url, params, headers):
        if url.startswith(S.OPENALEX_API):
            rounds["n"] += 1
            n = rounds["n"]
            return {"results": [
                {"title": f"Paper {n}-{i}", "doi": f"10.1/r{n}i{i}", "authorships": [],
                 "primary_location": None, "publication_year": 2021, "cited_by_count": 3}
                for i in range(5)]}
        if url.startswith(S.ARXIV_API):
            return '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        if (url.startswith(S.CROSSREF_API) or url.startswith(S.DATACITE_API)
                or url.startswith(S.UNPAYWALL_API)):
            return None
        return {"data": []}

    rc, _, out, _ = run_main(["q", "--email", "t@e.tw", "--sources", "openalex",
                              "--per-page", "5", "--max-rounds", "20",
                              "--target", "3"], handler=handler)
    eq(rc, 0, "離場碼")
    contains(out, "已累積足夠候選", "應印出停止原因")
    # target=3 → 門檻 9 筆，每輪 5 筆，第 2 輪達 10 筆即停
    eq(rounds["n"], 2, "達到 target 三倍即停：只該發 2 次請求")


# --------------------------------------------------------------------------- #
# 9. 輸出：CSV / BibTeX / 檢索紀錄
# --------------------------------------------------------------------------- #

@test
def t_bibtex_key_and_escaping():
    used: set[str] = set()
    a = rec(title="Deep Learning for Flow", authors=["Wei, Hua"], year=2019)
    eq(S.bibtex_key(a, used), "wei2019deep", "key 應用姓+年+首個長字")
    eq(S.bibtex_key(a, used), "wei2019deep2", "重複 key 應加序號")
    eq(S.bibtex_key(rec(title="A of the", authors=[], year=None), used), "anonnd",
       "無作者無年份、標題無長字時的降級 key")
    eq(S.bibtex_key(rec(title="A of the Arterial", authors=[], year=None), used),
       "anonndarterial", "應取第一個長度大於 3 的字")
    eq(S.bib_escape("a{b}c"), "a\\{b\\}c", "大括號轉義")
    eq(S.bib_escape("a\\d"), "a\\textbackslash{}d",
       "反斜線轉出的大括號不應再被二次轉義")
    eq(S.bib_escape("50% of CO_2 & more #1 $x"),
       "50\\% of CO\\_2 \\& more \\#1 \\$x", "其他 LaTeX 特殊字元")


@test
def t_write_bibtex_entry_types():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "o.bib")
    S.write_bibtex(p, [
        rec(title="Journal Paper", authors=["Wei, Hua"], year=2019, venue="TR Part C",
            doi="10.1/a", verification="已驗證", oa_pdf_url="https://x.pdf"),
        rec(title="Preprint {braces}", authors=["Hsu, C"], year=2021,
            venue="arXiv (preprint)", verification="未驗證", url="https://arxiv/abs/1"),
    ])
    txt = open(p, encoding="utf-8").read()
    contains(txt, "@article{wei2019journal", "有 DOI 應為 @article")
    contains(txt, "journal = {TR Part C}", "@article 用 journal 欄位")
    contains(txt, "@misc{hsu2021preprint", "無 DOI 應為 @misc")
    contains(txt, "howpublished = {arXiv (preprint)}", "@misc 用 howpublished 欄位")
    absent(txt, "journal = {arXiv", "@misc 不應有 journal 欄位")
    contains(txt, "Preprint \\{braces\\}", "標題中的大括號應轉義")
    contains(txt, "note = {trans-paper-search: 已驗證}", "驗證狀態應寫進 note")
    contains(txt, "url = {https://x.pdf}", "有 OA PDF 時優先用它")


@test
def t_write_csv_roundtrip_with_cjk():
    import csv as _csv
    d = tempfile.mkdtemp()
    p = os.path.join(d, "o.csv")
    S.write_csv(p, [rec(title="臺北市號誌時制最佳化", authors=["王明", "李華"], year=2022,
                        venue="運輸計劃季刊", doi="10.1/tw", citations=3,
                        sources=["OpenAlex", "arXiv"], verification="已驗證")])
    rows = list(_csv.DictReader(open(p, encoding="utf-8-sig")))
    eq(len(rows), 1, "一列")
    eq(rows[0]["title"], "臺北市號誌時制最佳化", "中文標題應完整保存")
    eq(rows[0]["authors"], "王明; 李華", "作者以分號串接")
    eq(rows[0]["sources"], "OpenAlex, arXiv", "來源以逗號串接")
    eq(list(rows[0].keys())[0], "verification", "驗證狀態應為第一欄")


# --------------------------------------------------------------------------- #
# 10. main() 端到端
# --------------------------------------------------------------------------- #

OA_FIXTURE = {"results": [
    {"title": "Deep Reinforcement Learning for Traffic Signal Control",
     "doi": "https://doi.org/10.1/OK", "publication_year": 2019, "cited_by_count": 420,
     "authorships": [{"author": {"display_name": "Wei, Hua"}}],
     "primary_location": {"source": {"display_name": "TR Part C"}},
     "abstract_inverted_index": {"We": [0], "study": [1]}, "id": "https://openalex.org/W1"},
    {"title": "A Paper That Does Not Exist", "doi": "10.9/fake",
     "publication_year": 2023, "cited_by_count": 15,
     "authorships": [{"author": {"display_name": "Ghost, A"}}],
     "primary_location": {"source": {"display_name": "Journal of Nowhere"}},
     "abstract_inverted_index": None, "id": "https://openalex.org/W2"},
    {"title": "Predatory Venue Paper", "doi": "10.2/pred", "publication_year": 2022,
     "cited_by_count": 99,
     "authorships": [{"author": {"display_name": "Wu, K"}}],
     "primary_location": {"source": {"display_name": "Predatory Journal of Transport"}},
     "abstract_inverted_index": None, "id": "https://openalex.org/W3"},
    {"title": "Low Cited Paper", "doi": "10.2/low", "publication_year": 2022,
     "cited_by_count": 1, "authorships": [{"author": {"display_name": "Lo, W"}}],
     "primary_location": {"source": {"display_name": "Fine Journal"}},
     "abstract_inverted_index": None, "id": "https://openalex.org/W4"},
]}

S2_FIXTURE = {"data": [
    # 與 OpenAlex 第一筆同一篇（DOI 大小寫不同）→ 應合併
    {"paperId": "p1", "title": "deep reinforcement learning for traffic signal control",
     "year": 2019, "venue": "TR Part C", "citationCount": 455,
     "externalIds": {"DOI": "10.1/ok"}, "authors": [{"name": "Wei, Hua"}],
     "abstract": "s2", "openAccessPdf": {}},
    # 無 DOI，且 arXiv 也會命中同一篇 → 應合併
    {"paperId": "p2", "title": "Signal Coordination in Urban Arterials", "year": 2021,
     "venue": "TRB Annual Meeting", "citationCount": 12, "externalIds": {},
     "authors": [{"name": "Hsu, Chia"}], "abstract": "no doi", "openAccessPdf": {}},
    # S2 帶回 arXiv 預印本，附的是 DataCite 登記的 DOI（Crossref 必然查無）
    {"paperId": "p3", "title": "Graph Neural Networks for Traffic Forecasting",
     "year": 2024, "venue": "arXiv", "citationCount": 30,
     "externalIds": {"DOI": "10.48550/arXiv.2401.99999"},
     "authors": [{"name": "Kuo, Ping"}], "abstract": "preprint via s2",
     "openAccessPdf": {}},
]}

ARXIV_FIXTURE = '''<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom" xmlns:arxiv="http://arxiv.org/schemas/atom">
<entry><id>http://arxiv.org/abs/2101.1</id><published>2021-01-01T00:00:00Z</published>
<title>Signal Coordination in Urban Arterials</title><summary>preprint</summary>
<author><name>Hsu, Chia</name></author></entry>
</feed>'''

E2E_CR = {
    "10.1/ok": {"message": {
        "title": ["Deep reinforcement learning for traffic signal control: A survey"],
        "container-title": ["Transportation Research Part C: Emerging Technologies"],
        "issued": {"date-parts": [[2019, 3]]}}},
}

E2E_DC = {
    "10.48550/arxiv.2401.99999": {"data": {"attributes": {
        "titles": [{"title": "Graph Neural Networks for Traffic Forecasting"}],
        "publicationYear": 2024, "publisher": "arXiv"}}},
}

E2E_UNPAY = {
    "10.1/ok": {"oa_status": "green",
                "best_oa_location": {"url_for_pdf": "https://arxiv.org/pdf/1904.pdf"}},
}


def e2e_handler(url, params, headers):
    if url.startswith(S.OPENALEX_API):
        return OA_FIXTURE if params.get("page") == 1 else {"results": []}
    if url.startswith(S.S2_API):
        return S2_FIXTURE if params.get("offset") == 0 else {"data": []}
    if url.startswith(S.ARXIV_API):
        return ARXIV_FIXTURE if params.get("start") == 0 else None
    if url.startswith(S.CROSSREF_API):
        return E2E_CR.get(url[len(S.CROSSREF_API):].replace("%2F", "/"))
    if url.startswith(S.DATACITE_API):
        return E2E_DC.get(url[len(S.DATACITE_API):].replace("%2F", "/"))
    if url.startswith(S.UNPAYWALL_API):
        return E2E_UNPAY.get(url[len(S.UNPAYWALL_API):].replace("%2F", "/"))
    raise AssertionError("未預期的 URL " + url)


def run_main(argv, handler=e2e_handler):
    """以假 HTTP 跑 main()，回傳 (exit code, outdir, stdout, stderr)。"""
    outdir = tempfile.mkdtemp(prefix="tps-")
    real_http = S.Http
    S.Http = lambda *a, **kw: FakeHttp(handler, email=a[0] if a else "t@e.tw")
    out, err = io.StringIO(), io.StringIO()
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = S.main(argv + ["--outdir", outdir, "--prefix", "t"])
    finally:
        sys.stdout, sys.stderr = so, se
        S.Http = real_http
    return rc, outdir, out.getvalue(), err.getvalue()


def read_outputs(outdir):
    return (open(f"{outdir}/t.csv", encoding="utf-8-sig").read(),
            open(f"{outdir}/t.bib", encoding="utf-8").read(),
            open(f"{outdir}/t-search-log.md", encoding="utf-8").read())


@test
def t_main_end_to_end():
    ex = os.path.join(tempfile.mkdtemp(), "ex.txt")
    open(ex, "w", encoding="utf-8").write("Predatory Journal of Transport\n")
    rc, outdir, out, err = run_main([
        "traffic signal control", "--email", "test@example.edu.tw",
        "--from-year", "2019", "--min-citations", "5", "--exclude-venues", ex,
        "--target", "10",
    ])
    eq(rc, 0, "離場碼")
    csv_t, bib_t, log_t = read_outputs(outdir)

    # 跨來源去重
    contains(csv_t, "OpenAlex, Semantic Scholar", "DOI 大小寫不同的同一篇應合併")
    contains(csv_t, "Semantic Scholar, arXiv", "無 DOI 的同一篇應跨來源合併")
    # 篩選
    absent(csv_t, "Predatory", "排除清單中的期刊應被淘汰")
    absent(csv_t, "Low Cited", "低於引用數門檻應被淘汰")
    # 驗證四態
    contains(csv_t, "已驗證", "應有已驗證書目")
    contains(csv_t, "查無此文", "應標記查無此文")
    contains(csv_t, "無 DOI，無法向註冊機構對帳", "應標記無 DOI 未驗證")
    # Crossref 權威欄位覆寫
    contains(csv_t, "A survey", "已驗證書目應以 Crossref 標題覆寫")
    contains(csv_t, "Emerging Technologies", "應以 Crossref 期刊名覆寫")
    # Unpaywall
    contains(csv_t, "https://arxiv.org/pdf/1904.pdf", "應取得 OA PDF 連結")
    contains(csv_t, "green", "應記錄 oa_status")
    # BibTeX 只收可引用者
    contains(bib_t, "@article{wei2019deep", "已驗證者應進書目")
    absent(bib_t, "10.9/fake", "查無此文不得進 BibTeX")
    absent(bib_t, "Does Not Exist", "查無此文不得進 BibTeX")
    contains(bib_t, "@misc{hsu2021signal", "無 DOI 者應為 @misc")
    # DataCite 登記的預印本：可引用，必須進 BibTeX，不可被誤判為查無此文
    contains(csv_t, "已驗證（DataCite）", "DataCite 登記的 DOI 應標為已驗證（DataCite）")
    contains(csv_t, "10.48550/arxiv.2401.99999", "DataCite 那筆應留在 CSV")
    contains(bib_t, "10.48550/arxiv.2401.99999", "DataCite 那筆必須進 BibTeX")
    contains(bib_t, "note = {trans-paper-search: 已驗證（DataCite）}",
             "BibTeX 的 note 應標明是 DataCite 登記")
    # 檢索紀錄
    for section in ["檢索條件", "逐輪命中紀錄", "篩選與驗證結果", "誠實的邊界", "API 呼叫統計"]:
        contains(log_t, section, "檢索紀錄章節")
    contains(log_t, "traffic signal control", "應記錄關鍵字")
    contains(log_t, "| 出版年限 | 2019 |", "應記錄年限條件")
    contains(log_t, "| 最低引用數 | 5 |", "應記錄引用數門檻")
    contains(log_t, "TRID", "邊界說明應點名未涵蓋的來源")
    contains(log_t, "重試後仍失敗的請求數：0", "應記錄失敗數")
    contains(log_t, "已驗證（DataCite 登記，如 arXiv、Zenodo） | 1",
             "檢索紀錄應分開統計 DataCite 已驗證")
    # 排序：已驗證在前
    body = [l for l in csv_t.splitlines()[1:] if l.strip()]
    ok(body[0].startswith("已驗證"), "已驗證應排在最前")


@test
def t_main_multi_query_merges():
    rc, outdir, out, _ = run_main(["traffic signal control", "bus bunching",
                                   "--email", "t@e.tw", "--target", "10"])
    eq(rc, 0, "離場碼")
    csv_t, _, log_t = read_outputs(outdir)
    contains(out, "檢索主題：traffic signal control", "第一個關鍵字")
    contains(out, "檢索主題：bus bunching", "第二個關鍵字")
    contains(log_t, "traffic signal control、bus bunching", "紀錄應列出兩個關鍵字")
    # 兩個關鍵字回同樣 fixture，去重後不應加倍
    eq(csv_t.count("10.1/ok"), 1, "跨關鍵字仍應去重")


@test
def t_main_no_verify_and_no_oa():
    rc, outdir, out, _ = run_main(["q", "--email", "t@e.tw", "--no-verify", "--no-oa"])
    eq(rc, 0, "離場碼")
    csv_t, bib_t, _ = read_outputs(outdir)
    absent(csv_t, "已驗證", "跳過驗證時不應出現已驗證")
    absent(csv_t, "查無此文", "跳過驗證時不應判定查無此文")
    absent(csv_t, "https://arxiv.org/pdf/1904.pdf", "跳過取全文時不應有 OA 連結")
    contains(out, "已跳過 DOI 對帳驗證", "應提示已跳過驗證")


@test
def t_main_distinguishes_network_failure_from_no_results():
    """全數連線失敗時必須回非 0，而不是靜默報『0 筆』。"""
    def dead(url, params, headers):
        return None

    class DeadHttp(FakeHttp):
        def get_json(self, url, params=None, headers=None):
            self.failures += 1
            return super().get_json(url, params, headers)

        def get_text(self, url, params=None, headers=None):
            self.failures += 1
            return super().get_text(url, params, headers)

    real = S.Http
    S.Http = lambda *a, **kw: DeadHttp(dead)
    out, err = io.StringIO(), io.StringIO()
    so, se = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = S.main(["q", "--email", "t@e.tw", "--max-rounds", "1",
                     "--outdir", tempfile.mkdtemp(), "--prefix", "t"])
    finally:
        sys.stdout, sys.stderr = so, se
        S.Http = real
    eq(rc, 3, "全失敗應回離場碼 3")
    contains(err.getvalue(), "不是「查無文獻」而是連線失敗", "應明確區分失敗與查無")


@test
def t_main_empty_results_without_failure_is_ok():
    """真的查無文獻（API 正常回空）應是 exit 0，不可誤報為連線失敗。"""
    def empty(url, params, headers):
        if url.startswith(S.ARXIV_API):
            return '<feed xmlns="http://www.w3.org/2005/Atom"></feed>'
        return {"results": [], "data": []}

    rc, outdir, out, err = run_main(["nonexistent topic", "--email", "t@e.tw",
                                     "--max-rounds", "1"], handler=empty)
    eq(rc, 0, "查無文獻應為 exit 0")
    absent(err, "連線失敗", "不應誤報連線失敗")
    csv_t, bib_t, log_t = read_outputs(outdir)
    eq(len([l for l in csv_t.splitlines() if l.strip()]), 1, "CSV 只有表頭")
    contains(log_t, "| 去重後 | 0 |", "紀錄應顯示 0 筆")


@test
def t_main_rejects_bad_arguments():
    for argv, code, needle in [
        (["q"], 2, "聯絡信箱"),
        (["q", "--email", "t@e.tw", "--sources", "scopus"], 2, "未知的檢索來源"),
    ]:
        err = io.StringIO()
        se = sys.stderr
        sys.stderr = err
        try:
            rc = S.main(argv)
        finally:
            sys.stderr = se
        eq(rc, code, f"{argv} 的離場碼")
        contains(err.getvalue(), needle, f"{argv} 的錯誤訊息")


@test
def t_main_email_from_env():
    os.environ["TPS_EMAIL"] = "env@example.edu.tw"
    try:
        rc, outdir, out, _ = run_main(["q", "--max-rounds", "1"])
        eq(rc, 0, "應接受環境變數提供的信箱")
        _, _, log_t = read_outputs(outdir)
    finally:
        os.environ.pop("TPS_EMAIL", None)


@test
def t_main_source_selection():
    rc, outdir, out, _ = run_main(["q", "--email", "t@e.tw", "--sources", "openalex",
                                   "--max-rounds", "1"])
    eq(rc, 0, "離場碼")
    contains(out, "OpenAlex", "應跑 OpenAlex")
    absent(out, "-- arXiv", "未選的來源不應執行")
    _, _, log_t = read_outputs(outdir)
    contains(log_t, "| 檢索來源 | OpenAlex |", "紀錄應只列選用的來源")


# --------------------------------------------------------------------------- #
# 執行
# --------------------------------------------------------------------------- #

def run() -> int:
    passed = failed = 0
    for fn in TESTS:
        name = fn.__name__.removeprefix("t_")
        try:
            fn()
        except Exception as exc:  # noqa: BLE001 - 測試框架要收所有例外
            failed += 1
            print(f"FAIL  {name}\n        {exc}")
            if VERBOSE:
                traceback.print_exc()
        else:
            passed += 1
            if VERBOSE:
                print(f"ok    {name}")
    print("-" * 64)
    print(f"{passed} 通過，{failed} 失敗，共 {len(TESTS)} 項")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(run())
