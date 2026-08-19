# -*- coding: utf-8 -*-
"""
司法院裁判書查詢系統（FJUD）查詢模組 — 供工商登記查詢系統內部呼叫。

用途：輸入「公司名稱」「負責人姓名」「裁判起訖日期」，自動到
https://judgment.judicial.gov.tw/FJUD/Default_AD.aspx 用 Playwright 模擬瀏覽器操作
（全法院、案件類別勾刑事、全文內容分別查 (公司名稱+負責人)&賄賂 與
(公司名稱+負責人)&政治獻金 兩組），回傳查到的裁判書清單。

重要說明（已用真實瀏覽器現場核對過，非猜測）：
- 全文內容欄位（#jud_kw）原生支援 "+"（或）"&"（且）"-"（不含）"()"（分組），
  (公司名稱+負責人)&賄賂 這種字串可以原封不動填進去查詢。
- 「所有法院」是 <select id="jud_court"> 的預設值，完全不用手動操作。
- 案件類別「刑事」是 name="jud_sys" value="M" 的 checkbox。
- 「裁判期間」是 6 個獨立 textbox：#dy1 #dm1 #dd1（起）、#dy2 #dm2 #dd2（迄），
  吃民國年，數字不用補零。
- 查詢結果會出現在 <iframe id="iframe-data"> 裡面，不是主頁面本身。
- 第一次查詢後不論顯示有資料或查無資料，都一律按一次左邊的「再檢索」
  （#btnAgainQry）；只採用再檢索後的最終頁面與資料作為稽核結果。
- 查詢當下只取得清單（標題／日期／案由／明細連結）。使用者點擊「開啟裁判書」
  後直接前往司法院明細頁，如需 PDF 可使用瀏覽器的列印／另存為 PDF 功能。

效能與逾時提醒：
- 這是同步（sync）Playwright API，會整個佔用呼叫它的 gunicorn worker
  直到查詢完成（實測含兩組關鍵字通常 20~40 秒，視官網回應速度而定）。
  部署時務必確認 gunicorn 有調高 --timeout（見 Procfile），並確保
  worker 數量足夠，避免這段期間卡住其他使用者的請求。
"""

from typing import List, Dict, Optional, Tuple
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, Page

FJUD_URL = "https://judgment.judicial.gov.tw/FJUD/Default_AD.aspx"
RESULTS_FRAME_SELECTOR = "#iframe-data"

# 對司法院伺服器友善一點，每個查詢/每頁之間停頓一下，避免短時間內狂發請求。
# 請不要縮短這個延遲。
POLITE_DELAY_MS = 1200

# 單一關鍵字組合最多翻幾頁，避免異常寬泛的查詢把 worker 卡住太久。
MAX_PAGES_PER_KEYWORD = 5

DEFAULT_KEYWORDS = ["賄賂", "政治獻金"]


def _split_minguo_date(d: str):
    """7 碼民國年 YYYMMDD -> (year, month, day) 三個不補零的字串。"""
    d = (d or "").strip()
    if len(d) != 7 or not d.isdigit():
        raise ValueError(f"日期格式錯誤，應為 7 碼民國年 YYYMMDD，收到: {d!r}")
    y, m, day = d[:3], d[3:5], d[5:7]
    return str(int(y)), str(int(m)), str(int(day))


def _fill_and_submit(page: Page, company_name: str, person_name: str, date_from: str, date_to: str, keyword: str):
    page.goto(FJUD_URL, wait_until="networkidle")

    page.check("input[name='jud_sys'][value='M']")

    y1, m1, d1 = _split_minguo_date(date_from)
    y2, m2, d2 = _split_minguo_date(date_to)
    page.fill("#dy1", y1)
    page.fill("#dm1", m1)
    page.fill("#dd1", d1)
    page.fill("#dy2", y2)
    page.fill("#dm2", m2)
    page.fill("#dd2", d2)

    query = f"({company_name}+{person_name})&{keyword}"
    page.fill("#jud_kw", query)

    page.click("#btnQry")
    _wait_for_results_or_empty(page)

    # 不論第一次查詢是「有資料」或「查無資料」，一律按一次「再檢索」，並且
    # 只採用再檢索完成後的畫面與資料作為最終結果。這是本系統稽核流程的固定
    # 規則，第一次顯示內容不擷取、不回傳。
    #
    # #btnAgainQry 在部分狀態下會被 CSS 隱藏。Playwright 的 force=True 對
    # display:none 元素仍可能拋出 Element is not visible，因此改由頁面內的
    # HTMLElement.click() 直接觸發原生 click 事件。
    # 若無法完成再檢索，不能悄悄沿用第一次結果，必須明確讓整次查詢失敗，避免
    # 將未經再檢索確認的畫面當成稽核資料。
    again_btn = page.locator("#btnAgainQry")
    if again_btn.count() == 0:
        raise RuntimeError("司法院頁面找不到「再檢索」按鈕")
    again_btn.evaluate("element => element.click()")
    page.wait_for_timeout(800)
    _wait_for_results_or_empty(page, timeout=30000)

    page.wait_for_timeout(POLITE_DELAY_MS)


def _wait_for_results_or_empty(page: Page, timeout: int = 20000):
    frame = page.frame_locator(RESULTS_FRAME_SELECTOR)
    try:
        frame.locator("table#jud").first.wait_for(state="visible", timeout=min(timeout, 7000))
    except Exception:
        # 有資料與查無資料是互斥狀態；找不到結果表格時，必須明確等到
        # 「查無資料」文字，兩者皆未出現就拋出逾時，不能擷取不明畫面。
        frame.get_by_text("查無資料", exact=False).first.wait_for(
            state="visible", timeout=max(timeout - 7000, 1000)
        )


def _parse_current_page(page: Page, keyword: str) -> List[Dict]:
    frame = page.frame_locator(RESULTS_FRAME_SELECTOR)
    rows = frame.locator("table#jud tr").filter(has=frame.locator("a.hlTitle_scroll"))
    count = rows.count()

    results = []
    for i in range(count):
        row = rows.nth(i)
        link = row.locator("a.hlTitle_scroll").first
        case_number = link.inner_text().strip()
        href = link.get_attribute("href")
        # 司法院回傳的 href 常是 data.aspx?... 相對路徑；必須以 FJUD 查詢頁
        # 為基準，才會形成正確的 /FJUD/data.aspx?... 明細網址。
        detail_url = urljoin(FJUD_URL, href) if href else None
        tds = row.locator("td")
        judgment_date = None
        case_title = None
        try:
            judgment_date = tds.nth(2).inner_text().strip()
            case_title = tds.nth(3).inner_text().strip()
        except Exception:
            pass

        results.append(
            {
                "keyword": keyword,
                "case_number": case_number,
                "case_title": case_title,
                "judgment_date": judgment_date,
                "detail_url": detail_url,
            }
        )
    return results


def _has_next_page(page: Page) -> bool:
    frame = page.frame_locator(RESULTS_FRAME_SELECTOR)
    next_link = frame.locator("#hlNext").first
    return next_link.count() > 0 and next_link.is_visible()


def _go_next_page(page: Page):
    frame = page.frame_locator(RESULTS_FRAME_SELECTOR)
    frame.locator("#hlNext").first.click()
    frame.locator("table#jud").first.wait_for(state="visible")
    page.wait_for_timeout(POLITE_DELAY_MS)


def _capture_official_result_pdf(page: Page) -> bytes:
    """將司法院目前顯示的查詢結果頁以 Chromium 列印成 PDF（不落地保存）。"""
    # iframe 預設高度可能只顯示畫面的一部分；列印前依內容高度展開，讓官方
    # 查詢條件及完整結果清單一起進入 PDF。若官網日後限制存取，失敗時仍可
    # 使用原本高度列印，不影響查詢本身。
    try:
        page.evaluate(
            """
            () => {
              const frame = document.querySelector('#iframe-data');
              if (frame && frame.contentDocument) {
                const doc = frame.contentDocument.documentElement;
                frame.style.height = Math.max(doc.scrollHeight, 700) + 'px';
              }
            }
            """
        )
    except Exception:
        pass
    page.emulate_media(media="screen")
    return page.pdf(
        format="A4",
        print_background=True,
        margin={"top": "8mm", "right": "8mm", "bottom": "8mm", "left": "8mm"},
    )


def search_fjud_keyword(
    company_name: str,
    person_name: str,
    date_from: str,
    date_to: str,
    keyword: str,
) -> Tuple[List[Dict], List[bytes]]:
    """
    查詢單一關鍵字並回傳 (結構化結果, 官方結果頁PDF清單)。
    每一個司法院結果分頁各擷取一份 PDF；即使查無資料也至少會有一份。
    PDF 只存在本次 HTTP 回應的記憶體中，不寫入磁碟或資料庫。
    """
    company_name = (company_name or "").strip()
    person_name = (person_name or "").strip()
    keyword = (keyword or "").strip()
    if not company_name or not person_name:
        raise ValueError("公司名稱與負責人姓名不可為空")
    if keyword not in DEFAULT_KEYWORDS:
        raise ValueError("不允許的裁判書查詢關鍵字")

    results: List[Dict] = []
    evidence_pdfs: List[bytes] = []
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="zh-TW")
        page = context.new_page()
        try:
            _fill_and_submit(page, company_name, person_name, date_from, date_to, keyword)
            page_idx = 1
            while True:
                results.extend(_parse_current_page(page, keyword))
                evidence_pdfs.append(_capture_official_result_pdf(page))
                if page_idx >= MAX_PAGES_PER_KEYWORD or not _has_next_page(page):
                    break
                _go_next_page(page)
                page_idx += 1
        finally:
            browser.close()
    return results, evidence_pdfs


def search_fjud(
    company_name: str,
    person_name: str,
    date_from: str,
    date_to: str,
    keywords: Optional[List[str]] = None,
) -> List[Dict]:
    """
    主查詢函式：對每個關鍵字組合各跑一次查詢，合併回傳所有結果（不含 PDF）。
    每筆結果: {keyword, case_number, case_title, judgment_date, detail_url}
    """
    company_name = (company_name or "").strip()
    person_name = (person_name or "").strip()
    if not company_name or not person_name:
        raise ValueError("公司名稱與負責人姓名不可為空")

    kws = keywords or DEFAULT_KEYWORDS
    all_results: List[Dict] = []

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="zh-TW")
        page = context.new_page()

        try:
            for kw in kws:
                _fill_and_submit(page, company_name, person_name, date_from, date_to, kw)
                page_idx = 1
                while True:
                    all_results.extend(_parse_current_page(page, kw))
                    if page_idx >= MAX_PAGES_PER_KEYWORD or not _has_next_page(page):
                        break
                    _go_next_page(page)
                    page_idx += 1
        finally:
            browser.close()

    return all_results
