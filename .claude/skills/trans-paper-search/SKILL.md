---
name: trans-paper-search
description: 交通運輸領域的文獻檢索與驗證管線。並列檢索 OpenAlex + Semantic Scholar + arXiv，以 DOI 去重，逐筆回 Crossref 對帳驗證，再用 Unpaywall 取合法開放取用全文，輸出 CSV、BibTeX 與可貼進論文方法章節的檢索紀錄。當使用者要找交通、運輸、ITS、號誌控制、運輸規劃、交通安全、物流等主題的學術文獻，或要求「可驗證／不會有假引用／附 DOI／可寫進方法章節」的文獻清單時使用。也適用於任何需要書目驗證的系統性文獻檢索。
---

# trans-paper-search

交通管理研究所碩士班程度的文獻檢索助理。核心設計原則：**找出來的每一筆書目都要能回溯到權威來源，對不上的一律標記，不讓未驗證的資料無聲進入文獻回顧。**

## 管線架構

```
檢索層   OpenAlex  +  Semantic Scholar  +  arXiv        （並列，各自分輪抓取）
           ↓
去重     DOI 為主鍵；無 DOI 者用 標題正規化 + 第一作者姓 + 年份
           ↓
篩選     出版年限／最低引用數／排除期刊清單（掠奪性期刊）
           ↓
驗證     逐筆拿 DOI 回 Crossref 對帳：DOI 存在？標題相似度 ≥ 門檻？年份差距 ≤ 1 年？
           ↓
全文     Unpaywall 依 DOI 找合法 OA PDF
           ↓
輸出     CSV + BibTeX + 檢索紀錄（Markdown）
```

每個來源的角色分工不同，不要混用：

| 來源 | 角色 | 說明 |
|---|---|---|
| **OpenAlex** | 主檢索入口 | 4.7 億筆文獻，免費、不需帳號，回傳結構化書目（標題／作者／期刊／引用數／DOI） |
| **Semantic Scholar** | 語意檢索與補漏 | 補 OpenAlex 漏掉的，並提供較準的引用數 |
| **arXiv** | 預印本 | 交通流理論、深度學習需求預測、強化學習號誌控制 |
| **Crossref** | 驗證層（**不負責找**） | DOI 的權威來源，只用來對帳 |
| **Unpaywall** | 取全文 | 只取合法開放取用連結 |

## 使用方式

```bash
python3 .claude/skills/trans-paper-search/scripts/search.py "關鍵字" --email you@example.edu.tw
```

只用 Python 標準函式庫，不需 `pip install`。信箱是必填（OpenAlex polite pool 與 Unpaywall 要求識別呼叫者，這也是可稽核檢索的一部分），可改用環境變數 `TPS_EMAIL`。若有 Semantic Scholar API key，設 `S2_API_KEY` 可解除限流；沒有時腳本會自動放慢呼叫節奏。

### 常用範例

近五年、引用數 5 以上，排除掠奪性期刊：

```bash
python3 .claude/skills/trans-paper-search/scripts/search.py \
  "bus bunching" "transit reliability" \
  --email you@example.edu.tw \
  --from-year 2021 --min-citations 5 \
  --exclude-venues predatory.txt \
  --target 20 --outdir tps-out
```

`--exclude-venues` 的檔案格式為每行一個關鍵字（大小寫與標點不敏感），`#` 開頭為註解。

### 主要參數

| 參數 | 預設 | 用途 |
|---|---|---|
| `--sources` | `openalex,s2,arxiv` | 選擇檢索來源 |
| `--from-year` | 無 | 僅收錄此年之後 |
| `--min-citations` | 0 | 最低引用數門檻 |
| `--exclude-venues` | 無 | 掠奪性期刊排除清單檔 |
| `--target` | 20 | 目標筆數 |
| `--per-page` | 50 | 每輪每來源擷取筆數 |
| `--max-rounds` | 4 | 每來源最多輪數 |
| `--patience` | 2 | 連續幾輪無新命中即停止 |
| `--title-threshold` | 0.90 | Crossref 對帳的標題相似度門檻 |
| `--no-verify` / `--no-oa` | 關 | 跳過驗證／取全文（**不建議跳過驗證**） |

停止條件是「連續 `--patience` 輪沒有新命中就停手」，加上累積達 `--target` 三倍候選即停（留餘裕給後續篩選與驗證淘汰）。檢索函式是產生器，所以停止條件會**真的不再發出後續請求**，而不是抓完才丟棄——這對 OpenAlex polite pool 與 Semantic Scholar 的限流都有意義。

## 輸出與判讀

三個檔案：`<prefix>.csv`、`<prefix>.bib`、`<prefix>-search-log.md`。

CSV 第一欄 `verification` 是四種狀態，務必逐欄判讀：

| 狀態 | 意思 | 可以引用嗎 |
|---|---|---|
| **已驗證** | DOI 存在於 Crossref，標題與年份吻合 | 可以。書目欄位已用 Crossref 的權威值覆寫 |
| **欄位不符** | DOI 存在，但標題相似度過低或年份差超過一年 | **不可直接引用**，須人工判讀（可能是檢索來源的 metadata 有誤，也可能是 DOI 掛錯） |
| **查無此文** | Crossref 查不到此 DOI | **絕對不可引用** |
| **未驗證** | 無 DOI（arXiv 預印本、會議論文常見），Crossref 無法對帳 | 須人工確認來源網址與年份後才引用 |

BibTeX 檔只輸出「已驗證」與「未驗證」兩類，「查無此文」與「欄位不符」不會進書目檔，避免被誤引。每筆 `note` 欄位都標著驗證狀態。

檢索紀錄（Markdown）含檢索條件、逐輪命中數、各階段筆數與 API 呼叫統計，可直接改寫成論文方法章節的「文獻檢索策略」一節，也是投稿時生成式 AI 使用揭露的依據。

## 使用這個技能時的行為準則

1. **不要憑記憶生成書目。** 任何文獻清單都要跑過這支腳本，或明確標示為未驗證。
2. **不要替使用者刪掉「查無此文」與「欄位不符」的紀錄。** 那些標記本身就是產出的一部分。
3. **回報時要一起交出檢索紀錄**，包含用了什麼關鍵字、幾輪、篩選條件為何。
4. **判斷仍是使用者的工作。** 這支工具解決的是檢索與驗證的可靠度，不解決「哪篇文獻對研究問題重要」、「理論觀點之間如何對話」、「研究缺口在哪裡」。不要把輸出講成完成的文獻回顧。

## 誠實的邊界：這支腳本查不到的地方

管線只涵蓋有公開 API 的來源。以下是交通領域的關鍵來源，但**必須另行人工檢索**，並在方法章節註明為「非 API 取得」：

- **TRID**（TRB Transport Research International Documentation）：全球最大運輸文獻庫，含 TRB 年會論文與各州 DOT 技術報告，OpenAlex 幾乎沒有收錄。無公開 API。
- **ROSA P / NTL**（美國 DOT 國家運輸圖書館）：聯邦與州運輸技術報告全文。有 OAI-PMH，可另行接。
- **IEEE Xplore**：ITS、車聯網、自駕車。有官方 Metadata API，需申請 key。
- **Scopus / Web of Science**：SSCI 檢核與引用分析。有 API，需校內訂閱。
- **臺灣本土來源**：臺灣博碩士論文知識加值系統、華藝 Airiti／CEPS（《運輸計劃季刊》《運輸學刊》）、GRB 政府研究資訊系統、交通部運輸研究所出版品、中華民國運輸學會年會論文集。這塊 OpenAlex 完全空白，但口試委員一定會問。
- **實證資料（非文獻）**：TDX 運輸資料流通服務平臺、交通部統計查詢網、OECD ITF、Eurostat。

這些來源沒有 DOI 可回 Crossref 對帳，是整條鏈裡可稽核性最弱的一環，人工確認來源網址與年份後才可引用。

取全文一律走 Unpaywall 的合法 OA 連結或校內訂閱 proxy，**不要**使用 Sci-Hub 之類的來源。

動手前請確認所屬機構與投稿期刊的生成式 AI 使用規範，該揭露就揭露。

### `--sources` 拿掉 `arxiv` 的影響

某些網路環境下（校園網路的安全閘道、防毒軟體的網頁防護等）arXiv 的請求會被中途插入 HTTP 406，重試也無效，但問題不在 arXiv 本身或這支腳本——同樣的請求換一個獨立的呼叫環境就會成功。若遇到這種情況、想先跳過 arXiv（`--sources openalex,s2`）：

- **不是完全沒有 arXiv 論文。** Semantic Scholar 自己就有索引大量 arXiv 預印本（DOI 前綴 `10.48550/arxiv.*`），實測中拿掉 arxiv 來源後，S2 仍帶回了同主題的 arXiv 預印本。
- **但驗證狀態會變嚴重。** 原生 arXiv 來源抓到的無 DOI 論文標記「未驗證」（人工確認來源與年份後可引用，會進 BibTeX）；同一篇論文若只透過 S2 拿到、且 S2 附的是 `10.48550/arxiv` 這個 DataCite 而非 Crossref 登記的 DOI，Crossref 對帳查不到，會被標成「查無此文」——這個狀態**不會**寫進 BibTeX，即使論文內容其實沒問題。換句話說，拿掉 arxiv 來源不會讓你漏掉這些預印本，但會讓一部分原本可標「未驗證、人工確認後可引用」的論文，被更嚴格地擋在 BibTeX 之外，需要另外手動加回去。
- **最新的預印本可能真的漏掉。** S2 收錄 arXiv 有落後期，剛掛上去沒幾天的論文，原生 arXiv 來源抓得到但 S2 可能還沒有。

## 離線測試

改動腳本後跑這套測試（以假 API 回應取代網路，不需連線）：

```bash
python3 .claude/skills/trans-paper-search/scripts/test_pipeline.py      # 摘要
python3 .claude/skills/trans-paper-search/scripts/test_pipeline.py -v   # 逐項
```

共 40 項，涵蓋：

- 標題／DOI 正規化、相似度、作者姓名三種格式（`Wei, Hua`／`Hua Wei`／中文）
- OpenAlex 反向索引摘要還原
- 去重：DOI 大小寫與前綴差異、有 DOI 與無 DOI 的雙向合併、三來源接續合併後的索引重建、不同文獻不被誤併
- 三種篩選條件與排除清單的正規化
- Crossref 四種驗證狀態、年份容差、相似度門檻可調、Crossref 未提供標題時不誤判
- Unpaywall 的 PDF／landing page 取用優先序與無 DOI 略過
- HTTP 層：429／5xx／406 退避重試、404 視為有效答案不重試也不計失敗、其餘 4xx 不重試、連線失敗計數、畸形 JSON 與畸形 XML
- 三個來源的參數下達（mailto、年限、offset、API key）與分頁前進／停止
- `--patience` 與 `--target` 停止條件確實減少 API 請求次數
- BibTeX：entry 類型、citation key 取姓、key 衝突加序號、LaTeX 特殊字元單次轉義
- CSV 的中文書目往返與欄位順序
- `main()` 端到端：跨來源去重、篩選、四種驗證狀態、Crossref 欄位覆寫、OA 連結、BibTeX 排除規則、檢索紀錄章節、排序、多關鍵字合併、`--no-verify`／`--no-oa`、來源選擇、參數錯誤的離場碼
- 「查無文獻」（exit 0）與「連線失敗」（exit 3）必須區分

> 這套測試不含對真實 API 的呼叫。第一次在自己的網路環境使用前，建議先
> `--max-rounds 1 --target 5 -v` 小跑一次，確認五個 API 都通得到。
