#!/usr/bin/env python3
"""trans-paper-search: 交通運輸領域文獻檢索與驗證管線。

管線（聯集版本）：
    檢索層  OpenAlex + Semantic Scholar + arXiv   （並列，各自分輪抓取）
      ↓
    去重    DOI 為主鍵；無 DOI 者用 標題正規化 + 第一作者姓 + 年份
      ↓
    驗證    逐筆拿 DOI 回 Crossref 對帳（存在？標題吻合？年份吻合？）
      ↓
    全文    Unpaywall 依 DOI 找合法 OA PDF
      ↓
    輸出    CSV + BibTeX + 檢索紀錄（Markdown，可貼進方法章節）

只用 Python 標準函式庫，不需 pip install。
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from difflib import SequenceMatcher

USER_AGENT = "trans-paper-search/1.0 (academic literature search; mailto:{email})"

OPENALEX_API = "https://api.openalex.org/works"
S2_API = "https://api.semanticscholar.org/graph/v1/paper/search"
ARXIV_API = "https://export.arxiv.org/api/query"
CROSSREF_API = "https://api.crossref.org/works/"
UNPAYWALL_API = "https://api.unpaywall.org/v2/"

S2_FIELDS = "title,year,venue,citationCount,externalIds,authors,abstract,openAccessPdf"
ATOM = "{http://www.w3.org/2005/Atom}"
ARXIV_NS = "{http://arxiv.org/schemas/atom}"


# --------------------------------------------------------------------------- #
# 資料結構
# --------------------------------------------------------------------------- #

@dataclass
class Record:
    """一筆文獻。sources 記錄它被哪些檢索來源命中（同一筆可能多源命中）。"""

    title: str = ""
    authors: list[str] = field(default_factory=list)
    year: int | None = None
    venue: str = ""
    doi: str = ""
    citations: int | None = None
    abstract: str = ""
    url: str = ""
    sources: list[str] = field(default_factory=list)
    # 驗證階段填入
    verification: str = "未驗證"
    verify_note: str = ""
    crossref_title: str = ""
    crossref_year: int | None = None
    crossref_venue: str = ""
    # 全文階段填入
    oa_status: str = ""
    oa_pdf_url: str = ""

    @property
    def first_author_surname(self) -> str:
        """第一作者的姓。

        各來源格式不一：「Wei, Hua」（姓在前，逗號分隔）與「Hua Wei」（名在前）
        都會出現，中文姓名則無空白。三種都要收斂到同一個姓，否則
        去重鍵與 BibTeX key 都會錯。
        """
        if not self.authors:
            return ""
        name = self.authors[0].strip()
        if "," in name:
            return normalize_text(name.split(",", 1)[0])
        parts = name.split()
        return normalize_text(parts[-1] if parts else name)

    @property
    def doi_key(self) -> str:
        """去重用的 DOI 主鍵（再正規化一次，容忍未經清理的輸入）。"""
        return normalize_doi(self.doi)

    @property
    def title_key(self) -> str:
        """無 DOI 時的備用鍵：標題正規化 + 第一作者姓 + 年份。"""
        return (f"{normalize_text(self.title)}|{self.first_author_surname}|"
                f"{self.year or ''}")


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #

def normalize_text(s: str) -> str:
    """標題/姓名正規化：NFKC、去重音、小寫、僅留英數與 CJK、壓縮空白。"""
    if not s:
        return ""
    s = unicodedata.normalize("NFKC", s)
    s = "".join(c for c in unicodedata.normalize("NFD", s)
                if unicodedata.category(c) != "Mn")
    s = s.lower()
    s = re.sub(r"[^0-9a-z一-鿿]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def normalize_doi(raw: str | None) -> str:
    """統一成小寫裸 DOI（10.xxxx/yyyy），非 DOI 一律回空字串。"""
    if not raw:
        return ""
    doi = raw.strip().lower()
    doi = re.sub(r"^(https?://)?(dx\.)?doi\.org/", "", doi)
    doi = re.sub(r"^doi:\s*", "", doi)
    return doi if doi.startswith("10.") else ""


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_text(a), normalize_text(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


class Http:
    """帶重試與節流的 HTTP GET；沿用環境變數中的 proxy 設定。"""

    def __init__(self, email: str, delay: float = 0.15, retries: int = 4,
                 timeout: int = 30, verbose: bool = False):
        self.email = email
        self.delay = delay
        self.retries = retries
        self.timeout = timeout
        self.verbose = verbose
        self.calls = 0
        self.failures = 0  # 放棄的請求數，用來區分「查無結果」與「連線失敗」

    def get_json(self, url: str, params: dict | None = None,
                 headers: dict | None = None) -> dict | None:
        raw = self._get(url, params, headers)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            self._log(f"JSON 解析失敗 {url}: {exc}")
            return None

    def get_text(self, url: str, params: dict | None = None,
                 headers: dict | None = None) -> str | None:
        raw = self._get(url, params, headers)
        return raw.decode("utf-8", "replace") if raw is not None else None

    def _get(self, url: str, params: dict | None,
             headers: dict | None) -> bytes | None:
        if params:
            url = f"{url}?{urllib.parse.urlencode(params, doseq=True)}"
        hdrs = {"User-Agent": USER_AGENT.format(email=self.email),
                "Accept": "application/json"}
        if headers:
            hdrs.update(headers)

        backoff = 2.0
        for attempt in range(1, self.retries + 1):
            try:
                time.sleep(self.delay)
                self.calls += 1
                req = urllib.request.Request(url, headers=hdrs)
                with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                    return resp.read()
            except urllib.error.HTTPError as exc:
                # 404 是「查無此文」，屬於有效答案，不重試
                if exc.code == 404:
                    return None
                # 429/5xx 退避重試；其餘直接放棄
                if exc.code not in (429, 500, 502, 503, 504) or attempt == self.retries:
                    self._log(f"HTTP {exc.code} {url}")
                    self.failures += 1
                    return None
                self._log(f"HTTP {exc.code}，{backoff:.0f}s 後重試 ({attempt}/{self.retries})")
            except (urllib.error.URLError, TimeoutError, OSError) as exc:
                if attempt == self.retries:
                    self._log(f"連線失敗 {url}: {exc}")
                    self.failures += 1
                    return None
                self._log(f"連線失敗，{backoff:.0f}s 後重試 ({attempt}/{self.retries}): {exc}")
            time.sleep(backoff)
            backoff *= 2
        self.failures += 1
        return None

    def _log(self, msg: str) -> None:
        if self.verbose:
            print(f"  [http] {msg}", file=sys.stderr)


# --------------------------------------------------------------------------- #
# 檢索層
# --------------------------------------------------------------------------- #

def reconstruct_abstract(inverted: dict | None) -> str:
    """OpenAlex 的 abstract_inverted_index 還原成文字。"""
    if not inverted:
        return ""
    positions: list[tuple[int, str]] = []
    for word, idxs in inverted.items():
        positions.extend((i, word) for i in idxs)
    positions.sort()
    return " ".join(w for _, w in positions)


def search_openalex(http: Http, query: str, rounds: int, per_page: int,
                    from_year: int | None) -> list[list[Record]]:
    """回傳每輪的結果（未去重）。"""
    out = []
    for page in range(1, rounds + 1):
        params = {
            "search": query,
            "per-page": per_page,
            "page": page,
            "mailto": http.email,
            "sort": "cited_by_count:desc",
        }
        if from_year:
            params["filter"] = f"from_publication_date:{from_year}-01-01"
        data = http.get_json(OPENALEX_API, params)
        items = (data or {}).get("results") or []
        batch = []
        for it in items:
            authorships = it.get("authorships") or []
            loc = it.get("primary_location") or {}
            src = (loc.get("source") or {}) if isinstance(loc, dict) else {}
            batch.append(Record(
                title=(it.get("title") or it.get("display_name") or "").strip(),
                authors=[(a.get("author") or {}).get("display_name", "")
                         for a in authorships if a.get("author")],
                year=it.get("publication_year"),
                venue=(src.get("display_name") or "").strip(),
                doi=normalize_doi(it.get("doi")),
                citations=it.get("cited_by_count"),
                abstract=reconstruct_abstract(it.get("abstract_inverted_index")),
                url=it.get("id") or "",
                sources=["OpenAlex"],
            ))
        out.append(batch)
        if len(items) < per_page:
            break
    return out


def search_semantic_scholar(http: Http, query: str, rounds: int, per_page: int,
                            from_year: int | None) -> list[list[Record]]:
    api_key = os.environ.get("S2_API_KEY", "").strip()
    headers = {"x-api-key": api_key} if api_key else None
    # 無 key 時 Semantic Scholar 限流很嚴，放慢節奏
    delay_backup = http.delay
    if not api_key:
        http.delay = max(http.delay, 1.2)

    out = []
    try:
        for r in range(rounds):
            params = {
                "query": query,
                "limit": min(per_page, 100),
                "offset": r * min(per_page, 100),
                "fields": S2_FIELDS,
            }
            if from_year:
                params["year"] = f"{from_year}-"
            data = http.get_json(S2_API, params, headers)
            items = (data or {}).get("data") or []
            batch = []
            for it in items:
                ext = it.get("externalIds") or {}
                oa = it.get("openAccessPdf") or {}
                batch.append(Record(
                    title=(it.get("title") or "").strip(),
                    authors=[a.get("name", "") for a in (it.get("authors") or [])],
                    year=it.get("year"),
                    venue=(it.get("venue") or "").strip(),
                    doi=normalize_doi(ext.get("DOI")),
                    citations=it.get("citationCount"),
                    abstract=it.get("abstract") or "",
                    url=(f"https://www.semanticscholar.org/paper/{it['paperId']}"
                         if it.get("paperId") else oa.get("url", "")),
                    sources=["Semantic Scholar"],
                ))
            out.append(batch)
            if len(items) < min(per_page, 100):
                break
    finally:
        http.delay = delay_backup
    return out


def search_arxiv(http: Http, query: str, rounds: int, per_page: int,
                 from_year: int | None) -> list[list[Record]]:
    out = []
    for r in range(rounds):
        params = {
            "search_query": f'all:"{query}"',
            "start": r * per_page,
            "max_results": per_page,
            "sortBy": "relevance",
            "sortOrder": "descending",
        }
        text = http.get_text(ARXIV_API, params, {"Accept": "application/atom+xml"})
        if not text:
            out.append([])
            break
        try:
            root = ET.fromstring(text)
        except ET.ParseError:
            out.append([])
            break
        entries = root.findall(f"{ATOM}entry")
        batch = []
        for e in entries:
            published = (e.findtext(f"{ATOM}published") or "")[:4]
            year = int(published) if published.isdigit() else None
            if from_year and year and year < from_year:
                continue
            journal_ref = e.findtext(f"{ARXIV_NS}journal_ref") or ""
            batch.append(Record(
                title=re.sub(r"\s+", " ", (e.findtext(f"{ATOM}title") or "")).strip(),
                authors=[(a.findtext(f"{ATOM}name") or "").strip()
                         for a in e.findall(f"{ATOM}author")],
                year=year,
                venue=journal_ref.strip() or "arXiv (preprint)",
                doi=normalize_doi(e.findtext(f"{ARXIV_NS}doi")),
                citations=None,
                abstract=re.sub(r"\s+", " ", (e.findtext(f"{ATOM}summary") or "")).strip(),
                url=(e.findtext(f"{ATOM}id") or "").strip(),
                sources=["arXiv"],
            ))
        out.append(batch)
        if len(entries) < per_page:
            break
    return out


SEARCHERS = {
    "openalex": ("OpenAlex", search_openalex),
    "s2": ("Semantic Scholar", search_semantic_scholar),
    "arxiv": ("arXiv", search_arxiv),
}


# --------------------------------------------------------------------------- #
# 去重
# --------------------------------------------------------------------------- #

class Deduper:
    """DOI 為主鍵；無 DOI 者用 標題正規化 + 第一作者姓 + 年份。

    兩個索引同時維護：每筆都進標題索引，有 DOI 者另進 DOI 索引。
    這樣「先以有 DOI 收進、後來同一篇缺 DOI」與反向的情形都能認出來
    （Semantic Scholar 與 arXiv 常常缺 DOI）。
    後到的同一筆會合併 sources，並補上先前缺漏的欄位。
    """

    def __init__(self):
        self.by_doi: dict[str, Record] = {}
        self.by_title: dict[str, Record] = {}
        self.order: list[Record] = []

    def add(self, rec: Record) -> bool:
        """回傳 True 表示這是新命中。"""
        if not rec.title:
            return False
        rec.doi = rec.doi_key  # 入池即正規化
        existing = self._find(rec)
        if existing is not None:
            self._merge(existing, rec)
            return False
        self._index(rec)
        self.order.append(rec)
        return True

    def _find(self, rec: Record) -> Record | None:
        if rec.doi and rec.doi in self.by_doi:
            return self.by_doi[rec.doi]
        # DOI 沒對上（或這筆沒 DOI）時，退回標題鍵
        return self.by_title.get(rec.title_key)

    def _index(self, rec: Record) -> None:
        if rec.doi:
            self.by_doi[rec.doi] = rec
        self.by_title.setdefault(rec.title_key, rec)

    def _merge(self, dst: Record, src: Record) -> None:
        for s in src.sources:
            if s not in dst.sources:
                dst.sources.append(s)
        if not dst.doi and src.doi:
            # 補上 DOI 後要重建索引，否則第三個來源帶同一 DOI 來會漏接
            dst.doi = src.doi
            self.by_doi[dst.doi] = dst
        for fld in ("venue", "abstract", "url"):
            if not getattr(dst, fld) and getattr(src, fld):
                setattr(dst, fld, getattr(src, fld))
        if dst.year is None and src.year is not None:
            dst.year = src.year
            self.by_title.setdefault(dst.title_key, dst)
        if src.citations is not None:
            dst.citations = max(dst.citations or 0, src.citations)
        if len(src.authors) > len(dst.authors):
            dst.authors = src.authors

    def records(self) -> list[Record]:
        return list(self.order)


# --------------------------------------------------------------------------- #
# 驗證層（Crossref 對帳）
# --------------------------------------------------------------------------- #

def verify_with_crossref(http: Http, rec: Record, title_threshold: float) -> None:
    if not rec.doi:
        rec.verification = "未驗證"
        rec.verify_note = "無 DOI，Crossref 無法對帳（預印本或非 DOI 來源需人工確認）"
        return

    data = http.get_json(f"{CROSSREF_API}{urllib.parse.quote(rec.doi)}",
                         {"mailto": http.email})
    msg = (data or {}).get("message")
    if not msg:
        rec.verification = "查無此文"
        rec.verify_note = "Crossref 查不到此 DOI —— 不可直接寫進文獻回顧"
        return

    titles = msg.get("title") or []
    cr_title = titles[0] if titles else ""
    containers = msg.get("container-title") or []
    issued = ((msg.get("issued") or {}).get("date-parts") or [[None]])[0]
    cr_year = issued[0] if issued and isinstance(issued[0], int) else None

    rec.crossref_title = cr_title
    rec.crossref_year = cr_year
    rec.crossref_venue = containers[0] if containers else ""

    problems = []
    sim = title_similarity(rec.title, cr_title)
    if cr_title and sim < title_threshold:
        problems.append(f"標題相似度 {sim:.2f} 低於門檻 {title_threshold:.2f}")
    if rec.year and cr_year and abs(rec.year - cr_year) > 1:
        problems.append(f"年份不符（檢索 {rec.year} / Crossref {cr_year}）")

    if problems:
        rec.verification = "欄位不符"
        rec.verify_note = "；".join(problems)
    else:
        rec.verification = "已驗證"
        rec.verify_note = f"DOI 存在，標題相似度 {sim:.2f}"
        # 以 Crossref 的權威欄位覆寫
        if cr_title:
            rec.title = cr_title
        if cr_year:
            rec.year = cr_year
        if rec.crossref_venue:
            rec.venue = rec.crossref_venue


# --------------------------------------------------------------------------- #
# 全文層（Unpaywall）
# --------------------------------------------------------------------------- #

def fetch_oa(http: Http, rec: Record) -> None:
    if not rec.doi:
        return
    data = http.get_json(f"{UNPAYWALL_API}{urllib.parse.quote(rec.doi)}",
                         {"email": http.email})
    if not data:
        return
    rec.oa_status = data.get("oa_status") or ""
    best = data.get("best_oa_location") or {}
    rec.oa_pdf_url = best.get("url_for_pdf") or best.get("url") or ""


# --------------------------------------------------------------------------- #
# 輸出
# --------------------------------------------------------------------------- #

CSV_COLUMNS = [
    "verification", "verify_note", "title", "authors", "year", "venue", "doi",
    "citations", "sources", "oa_status", "oa_pdf_url", "url",
    "crossref_title", "crossref_year", "crossref_venue", "abstract",
]


def write_csv(path: str, records: list[Record]) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.DictWriter(fh, fieldnames=CSV_COLUMNS, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = asdict(r)
            row["authors"] = "; ".join(r.authors)
            row["sources"] = ", ".join(r.sources)
            w.writerow(row)


def bibtex_key(rec: Record, used: set[str]) -> str:
    surname = re.sub(r"[^a-z]", "", rec.first_author_surname) or "anon"
    word = ""
    for tok in normalize_text(rec.title).split():
        if len(tok) > 3:
            word = re.sub(r"[^a-z0-9]", "", tok)
            break
    base = f"{surname}{rec.year or 'nd'}{word}" or "ref"
    key, n = base, 2
    while key in used:
        key, n = f"{base}{n}", n + 1
    used.add(key)
    return key


def bib_escape(s: str) -> str:
    return s.replace("\\", "\\textbackslash{}").replace("{", "\\{").replace("}", "\\}")


def write_bibtex(path: str, records: list[Record]) -> None:
    used: set[str] = set()
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            entry = "article" if r.doi else "misc"
            fh.write(f"@{entry}{{{bibtex_key(r, used)},\n")
            fh.write(f"  title = {{{bib_escape(r.title)}}},\n")
            if r.authors:
                fh.write(f"  author = {{{bib_escape(' and '.join(r.authors))}}},\n")
            if r.year:
                fh.write(f"  year = {{{r.year}}},\n")
            if r.venue:
                # @misc 沒有 journal 欄位，改用 howpublished
                key = "journal" if entry == "article" else "howpublished"
                fh.write(f"  {key} = {{{bib_escape(r.venue)}}},\n")
            if r.doi:
                fh.write(f"  doi = {{{r.doi}}},\n")
            if r.oa_pdf_url or r.url:
                fh.write(f"  url = {{{r.oa_pdf_url or r.url}}},\n")
            # 驗證狀態一併寫進書目，避免未驗證的資料被無聲引用
            fh.write(f"  note = {{trans-paper-search: {r.verification}}},\n")
            fh.write("}\n\n")


LOG_TEMPLATE = """# 文獻檢索紀錄（trans-paper-search）

> 本檔為自動產生的檢索稽核紀錄，可直接改寫進論文方法章節的「文獻檢索策略」一節，
> 或作為期刊投稿時 AI 使用揭露的依據。

## 一、檢索條件

| 項目 | 內容 |
|---|---|
| 檢索主題（關鍵字） | {queries} |
| 檢索日期 | {stamp} |
| 檢索來源 | {sources} |
| 出版年限 | {from_year} |
| 最低引用數 | {min_citations} |
| 排除期刊／出版者 | {excluded} |
| 每輪擷取筆數 | {per_page} |
| 最大輪數 | {max_rounds} |
| 停止條件 | 連續 {patience} 輪無新命中即停止；或達到目標筆數 {target} |
| 去重規則 | DOI 為主鍵；無 DOI 者以「標題正規化 + 第一作者姓 + 年份」比對 |
| 驗證規則 | 逐筆以 DOI 查詢 Crossref，標題相似度 ≥ {threshold} 且年份差距 ≤ 1 年方標記「已驗證」 |
| 全文來源 | Unpaywall（僅取合法開放取用連結） |

## 二、逐輪命中紀錄

{rounds_table}

## 三、篩選與驗證結果

| 階段 | 筆數 |
|---|---|
| 各來源原始命中合計 | {raw_total} |
| 去重後 | {deduped} |
| 通過篩選條件（年限／引用數／排除期刊） | {filtered} |
| Crossref 已驗證 | {verified} |
| 欄位不符（需人工判讀） | {mismatch} |
| 查無此文（不得引用） | {notfound} |
| 無 DOI 未驗證（預印本等，需人工確認） | {unverified} |
| 取得合法 OA 全文連結 | {oa_found} |

## 四、誠實的邊界

1. 本管線只涵蓋有公開 API 的來源。**TRID、ROSA P、IEEE Xplore、Scopus/WoS、
   臺灣博碩士論文知識加值系統、華藝 Airiti、GRB 政府研究資訊系統、交通部運輸研究所
   出版品**等交通領域關鍵來源未包含在內，須另行人工檢索，並在方法章節註明為
   「非 API 取得」。
2. 標記為「查無此文」與「欄位不符」的書目**不得**直接寫進文獻回顧。
3. 本工具解決的是檢索與驗證的可靠度，不解決判斷。哪篇文獻對研究問題重要、
   理論觀點之間如何對話、研究缺口在哪裡，仍是研究者自己的工作。
4. 動手前請確認所屬機構與投稿期刊的生成式 AI 使用規範，該揭露就揭露。

## 五、API 呼叫統計

- 總計 HTTP 請求數：{http_calls}
- 重試後仍失敗的請求數：{http_failures}（大於 0 表示本次結果可能不完整）
"""


def write_log(path: str, ctx: dict) -> None:
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(LOG_TEMPLATE.format(**ctx))


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #

def load_exclusions(path: str | None) -> list[str]:
    if not path:
        return []
    with open(path, encoding="utf-8") as fh:
        return [normalize_text(ln) for ln in fh
                if ln.strip() and not ln.lstrip().startswith("#")]


def passes_filters(rec: Record, from_year: int | None, min_citations: int,
                   excluded: list[str]) -> tuple[bool, str]:
    if from_year and rec.year and rec.year < from_year:
        return False, f"年份 {rec.year} 早於 {from_year}"
    if min_citations and (rec.citations or 0) < min_citations:
        return False, f"引用數 {rec.citations or 0} 低於 {min_citations}"
    if excluded:
        venue = normalize_text(rec.venue)
        for bad in excluded:
            if bad and bad in venue:
                return False, f"期刊列於排除清單（{rec.venue}）"
    return True, ""


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="trans-paper-search",
        description="交通運輸文獻檢索管線：OpenAlex + Semantic Scholar + arXiv "
                    "→ 去重 → Crossref 驗證 → Unpaywall 取全文 → CSV/BibTeX/檢索紀錄",
    )
    p.add_argument("query", nargs="+", help="檢索關鍵字，可給多組（各自跑一輪管線後合併）")
    p.add_argument("--email", default=os.environ.get("TPS_EMAIL", ""),
                   help="聯絡信箱（OpenAlex polite pool 與 Unpaywall 必填）")
    p.add_argument("--sources", default="openalex,s2,arxiv",
                   help="檢索來源，逗號分隔：openalex,s2,arxiv")
    p.add_argument("--from-year", type=int, default=None, help="僅收錄此年之後的文獻")
    p.add_argument("--min-citations", type=int, default=0, help="最低引用數門檻")
    p.add_argument("--exclude-venues", default=None,
                   help="排除期刊清單檔（每行一個關鍵字，用於淘汰掠奪性期刊）")
    p.add_argument("--target", type=int, default=20, help="目標筆數（達到即停止檢索）")
    p.add_argument("--per-page", type=int, default=50, help="每輪每來源擷取筆數")
    p.add_argument("--max-rounds", type=int, default=4, help="每個來源最多幾輪")
    p.add_argument("--patience", type=int, default=2,
                   help="連續幾輪無新命中即停止（預設 2，對應「連續兩輪沒有新命中就停手」）")
    p.add_argument("--title-threshold", type=float, default=0.90,
                   help="Crossref 對帳的標題相似度門檻")
    p.add_argument("--no-verify", action="store_true", help="跳過 Crossref 驗證（不建議）")
    p.add_argument("--no-oa", action="store_true", help="跳過 Unpaywall 取全文")
    p.add_argument("--outdir", default="tps-out", help="輸出目錄")
    p.add_argument("--prefix", default=None, help="輸出檔名前綴（預設用時間戳）")
    p.add_argument("-v", "--verbose", action="store_true", help="顯示 HTTP 細節")
    args = p.parse_args(argv)

    if not args.email:
        print("錯誤：請用 --email 或環境變數 TPS_EMAIL 提供聯絡信箱。\n"
              "OpenAlex 的 polite pool 與 Unpaywall 都要求識別呼叫者，"
              "這也是可稽核檢索的一部分。", file=sys.stderr)
        return 2

    source_keys = [s.strip().lower() for s in args.sources.split(",") if s.strip()]
    unknown = [s for s in source_keys if s not in SEARCHERS]
    if unknown:
        print(f"錯誤：未知的檢索來源 {unknown}，可用：{list(SEARCHERS)}", file=sys.stderr)
        return 2

    excluded = load_exclusions(args.exclude_venues)
    http = Http(args.email, verbose=args.verbose)
    deduper = Deduper()
    round_log: list[dict] = []
    raw_total = 0

    # ---- 檢索層：各來源並列，逐輪加入去重池 ----
    for query in args.query:
        print(f"\n== 檢索主題：{query}")
        for key in source_keys:
            label, fn = SEARCHERS[key]
            print(f"-- {label}")
            batches = fn(http, query, args.max_rounds, args.per_page, args.from_year)
            stale = 0
            for idx, batch in enumerate(batches, start=1):
                new = sum(1 for rec in batch if deduper.add(rec))
                raw_total += len(batch)
                round_log.append({
                    "query": query, "source": label, "round": idx,
                    "hits": len(batch), "new": new,
                })
                print(f"   第 {idx} 輪：命中 {len(batch)} 筆，新增 {new} 筆"
                      f"（累計 {len(deduper.records())} 筆）")
                stale = stale + 1 if new == 0 else 0
                if stale >= args.patience:
                    print(f"   連續 {stale} 輪無新命中 → 停止本來源")
                    break
                if len(deduper.records()) >= args.target * 3:
                    # 留 3 倍餘裕給後續篩選與驗證淘汰
                    print("   已累積足夠候選 → 停止本來源")
                    break

    records = deduper.records()
    deduped = len(records)
    print(f"\n去重後共 {deduped} 筆")

    # 「查無結果」與「連線失敗」必須分清楚，否則使用者會把被擋的網路
    # 誤讀成「這個主題沒有文獻」。
    if http.failures:
        print(f"\n警告：有 {http.failures} 個 API 請求在重試後仍失敗，"
              f"本次結果可能不完整。請檢查網路或 proxy 設定後重跑。", file=sys.stderr)
    if deduped == 0 and http.failures:
        print("錯誤：所有檢索來源都沒有回應，這不是「查無文獻」而是連線失敗。",
              file=sys.stderr)
        return 3

    # ---- 篩選 ----
    kept, dropped = [], []
    for rec in records:
        ok, why = passes_filters(rec, args.from_year, args.min_citations, excluded)
        (kept if ok else dropped).append(rec if ok else (rec, why))
    print(f"通過篩選 {len(kept)} 筆，淘汰 {len(dropped)} 筆")

    # ---- 驗證層 ----
    if not args.no_verify:
        print(f"\nCrossref 逐筆對帳（{len(kept)} 筆）")
        for i, rec in enumerate(kept, start=1):
            verify_with_crossref(http, rec, args.title_threshold)
            if args.verbose or rec.verification != "已驗證":
                print(f"   [{i}/{len(kept)}] {rec.verification}｜"
                      f"{rec.title[:60]}｜{rec.verify_note}")
    else:
        print("\n（已跳過 Crossref 驗證，所有書目維持「未驗證」）")

    # ---- 全文層 ----
    if not args.no_oa:
        targets = [r for r in kept if r.doi]
        print(f"\nUnpaywall 取合法 OA 全文（{len(targets)} 筆有 DOI）")
        for rec in targets:
            fetch_oa(http, rec)

    # 已驗證優先、再依引用數排序
    order = {"已驗證": 0, "未驗證": 1, "欄位不符": 2, "查無此文": 3}
    kept.sort(key=lambda r: (order.get(r.verification, 9), -(r.citations or 0)))

    # ---- 輸出 ----
    os.makedirs(args.outdir, exist_ok=True)
    stamp = datetime.now(timezone.utc).astimezone()
    prefix = args.prefix or stamp.strftime("%Y%m%d-%H%M%S")
    csv_path = os.path.join(args.outdir, f"{prefix}.csv")
    bib_path = os.path.join(args.outdir, f"{prefix}.bib")
    log_path = os.path.join(args.outdir, f"{prefix}-search-log.md")

    write_csv(csv_path, kept)
    # BibTeX 只輸出可引用的（已驗證 + 無 DOI 未驗證），查無此文與欄位不符不進書目檔
    citable = [r for r in kept if r.verification in ("已驗證", "未驗證")]
    write_bibtex(bib_path, citable)

    counts = {k: sum(1 for r in kept if r.verification == k)
              for k in ("已驗證", "欄位不符", "查無此文", "未驗證")}
    rows = ["| 檢索主題 | 來源 | 輪次 | 命中 | 新增 |", "|---|---|---|---|---|"]
    rows += [f"| {r['query']} | {r['source']} | {r['round']} | {r['hits']} | {r['new']} |"
             for r in round_log]

    write_log(log_path, {
        "queries": "、".join(args.query),
        "stamp": stamp.strftime("%Y-%m-%d %H:%M %Z"),
        "sources": "、".join(SEARCHERS[k][0] for k in source_keys),
        "from_year": args.from_year or "未設限",
        "min_citations": args.min_citations or "未設限",
        "excluded": args.exclude_venues or "未設定",
        "per_page": args.per_page,
        "max_rounds": args.max_rounds,
        "patience": args.patience,
        "target": args.target,
        "threshold": args.title_threshold,
        "rounds_table": "\n".join(rows),
        "raw_total": raw_total,
        "deduped": deduped,
        "filtered": len(kept),
        "verified": counts["已驗證"],
        "mismatch": counts["欄位不符"],
        "notfound": counts["查無此文"],
        "unverified": counts["未驗證"],
        "oa_found": sum(1 for r in kept if r.oa_pdf_url),
        "http_calls": http.calls,
        "http_failures": http.failures,
    })

    print(f"\n完成。已驗證 {counts['已驗證']} 筆、"
          f"欄位不符 {counts['欄位不符']} 筆、"
          f"查無此文 {counts['查無此文']} 筆、"
          f"無 DOI 未驗證 {counts['未驗證']} 筆")
    print(f"  CSV      : {csv_path}")
    print(f"  BibTeX   : {bib_path}（僅含可引用的 {len(citable)} 筆）")
    print(f"  檢索紀錄 : {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
