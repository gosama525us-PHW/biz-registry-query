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
- 官網第一次查詢偶爾會誤判「查無資料」，需要再按一次左邊的「再檢索」
  （#btnAgainQry）確認，這裡已內建自動重試一次的邏輯。
- 判決明細頁有官方「轉存PDF」按鈕（#hlExportPDF），href 直接是 PDF 檔案的
  相對路徑，可以直接下載，比瀏覽器列印模擬更準確。此模組採「按需下載」
  設計：查詢當下只取清單（標題／日期／案由／明細連結），使用者要看某一筆
  的 PDF 時才另外呼叫 fetch_judgment_pdf()，避免查詢階段就把每一筆都下載
  一次、拖慢查詢速度。

效能與逾時提醒：
- 這是同步（sync）Playwright API，會整個佔用呼叫它的 gunicorn worker
  直到查詢完成（實測含兩組關鍵字通常 20~40 秒，視官網回應速度而定）。
  部署時務必確認 gunicorn 有調高 --timeout（見 Procfile），並確保
  worker 數量足夠，避免這段期間卡住其他使用者的請求。
"""

import re
from typing import List, Dict, Optional
from urllib.parse import urljoin

from playwright.sync_api import sync_playwright, Page, BrowserContext

FJUD_URL = "https://judgment.judicial.gov.tw/FJUD/Default_AD.aspx"
BASE_URL = "https://judgment.judicial.gov.tw"
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

    # 官網第一次查詢偶爾誤判成查無資料，重按一次「再檢索」確認，以那次結果為準。
    #
    # 重要：#btnAgainQry 位於「再檢索」分頁內，該分頁在查無資料當下不一定是
    # 作用中分頁，DOM 上該按鈕可能被 Playwright 判定為「不可見」而讓一般的
    # click() 持續重試到逾時（實測會卡滿 30 秒才報錯）。這裡改用
    # force=True 跳過可見性/穩定性檢查直接觸發點擊事件（按鈕本身能回應
    # click 事件，只是版面上暫時不可見，不影響功能），並設定較短的
    # timeout，即使按鈕真的抓不到也能快速失敗、不拖垮整體查詢時間。
    if _is_no_data(page):
        again_btn = page.locator("#btnAgainQry")
        if again_btn.count() > 0:
            try:
                again_btn.click(force=True, timeout=5000)
                _wait_for_results_or_empty(page, timeout=30000)
            except Exception:
                # 再檢索失敗就維持原本「查無資料」的結果，不讓整個查詢因此中斷。
                pass

    page.wait_for_timeout(POLITE_DELAY_MS)


def _wait_for_results_or_empty(page: Page, timeout: int = 20000):
    frame = page.frame_locator(RESULTS_FRAME_SELECTOR)
    try:
        frame.locator("table#jud, text=查無資料").first.wait_for(state="visible", timeout=timeout)
    except Exception:
        page.wait_for_timeout(2000)


def _is_no_data(page: Page) -> bool:
    frame = page.frame_locator(RESULTS_FRAME_SELECTOR)
    return frame.locator("text=查無資料").count() > 0


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
        detail_url = urljoin(BASE_URL, href) if href else None

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
    return frame.locator("#hlNext").first.count() > 0


def _go_next_page(page: Page):
    frame = page.frame_locator(RESULTS_FRAME_SELECTOR)
    frame.locator("#hlNext").first.click()
    frame.locator("table#jud").first.wait_for(state="visible")
    page.wait_for_timeout(POLITE_DELAY_MS)


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


# 允許被下載 PDF 的網域白名單，避免被拿去當任意網址下載代理（SSRF 防護）。
_ALLOWED_PDF_HOST = "judgment.judicial.gov.tw"


def fetch_judgment_pdf(detail_url: str) -> bytes:
    """
    開啟指定的裁判書明細頁，直接點擊官網「轉存PDF」按鈕(#hlExportPDF)觸發
    真實下載事件取得 PDF 內容（bytes），不落地存檔，由呼叫端決定怎麼回應。

    改版說明：原本改用「自己組 API 請求」(context.request.get) 下載 PDF 連結，
    結果官網回應逾期／異常，研判是官網對下載連結有防盜連或工作階段驗證機制
    （單純用 API 請求不會像真實瀏覽器點擊那樣完整帶上當下的 cookie、Referer、
    瀏覽器環境等資訊）。改成用 Playwright 模擬「真的點一下轉存PDF按鈕」，
    直接攔截瀏覽器原生的下載事件取得檔案內容，行為上等同真人操作，
    可避免上述驗證機制擋下請求。
    """
    if not detail_url or _ALLOWED_PDF_HOST not in detail_url:
        raise ValueError("不允許的裁判書網址")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="zh-TW", accept_downloads=True)
        page = context.new_page()
        try:
            page.goto(detail_url, wait_until="networkidle")
            pdf_link = page.locator("#hlExportPDF")
            pdf_link.wait_for(state="attached", timeout=10000)

            # #hlExportPDF 的 target="_blank"，點擊後會開新分頁；
            # 用 context.expect_page() 接住新分頁，再於該分頁等待瀏覽器
            # 原生的 download 事件（比自組 API 請求更貼近真實使用者行為）。
            with context.expect_page() as new_page_info:
                pdf_link.click()
            download_page = new_page_info.value
            download = download_page.wait_for_event("download", timeout=15000)
            download_path = download.path()
            if not download_path:
                raise RuntimeError("下載事件未取得檔案路徑")
            with open(download_path, "rb") as f:
                data = f.read()
            download_page.close()
            return data
        finally:
            browser.close()
