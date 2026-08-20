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
- 第一次查詢後不論顯示有資料或查無資料，都一律切換到左邊的「再檢索」
  分頁（a[href="#tabsearchagain"]），顯示原檢索條件後才擷取稽核畫面。
  #btnAgainQry 是分頁內的放大鏡搜尋按鈕，不可在空白狀態下觸發。
- 查詢當下只取得清單（標題／日期／案由／明細連結）。使用者點擊「開啟裁判書」
  後直接前往司法院明細頁，如需 PDF 可使用瀏覽器的列印／另存為 PDF 功能。

效能與逾時提醒：
- 這是同步（sync）Playwright API，會整個佔用呼叫它的 gunicorn worker
  直到查詢完成（實測含兩組關鍵字通常 20~40 秒，視官網回應速度而定）。
  部署時務必確認 gunicorn 有調高 --timeout（見 Procfile），並確保
  worker 數量足夠，避免這段期間卡住其他使用者的請求。
"""

from pathlib import Path
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

FONT_PATH = Path(__file__).resolve().parent / "fonts" / "NotoSansCJKtc-Regular.otf"
FONT_ROUTE_URL = "https://fjud-local-font.invalid/NotoSansCJKtc-Regular.otf"


def _verify_cjk_font() -> None:
    """確認建置階段下載的字型存在且大小合理。"""
    if not FONT_PATH.exists() or FONT_PATH.stat().st_size < 10_000_000:
        raise RuntimeError(
            "找不到有效的 fonts/NotoSansCJKtc-Regular.otf；請確認 Build Command 已執行 "
            "python install_cjk_font.py"
        )


def _register_cjk_font_route(page: Page) -> None:
    """以 Playwright 路由直接將本地 OTF 提供給官網頁面，不依賴 fontconfig。"""
    page.route(
        FONT_ROUTE_URL,
        lambda route: route.fulfill(
            path=str(FONT_PATH),
            content_type="font/otf",
            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "public, max-age=3600"},
        ),
    )


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

    # 「再檢索」有兩個不同元素：
    #   1. a[href="#tabsearchagain"]：切換到使用者指定的再檢索分頁（正確）
    #   2. #btnAgainQry：分頁內放大鏡按鈕，會用 #txtAKW 再送一次查詢（不可按）
    #
    # 稽核畫面要保留右側第一次查詢的正式結果，同時讓左側顯示「再檢索」及
    # 「原檢索條件」，所以此處只切換分頁，絕對不觸發 #btnAgainQry。
    again_tab = page.locator("a[href='#tabsearchagain']").first
    if again_tab.count() == 0:
        raise RuntimeError("司法院頁面找不到「再檢索」分頁")
    again_tab.wait_for(state="visible", timeout=10000)
    again_tab.click(timeout=10000)

    again_panel = page.locator("#tabsearchagain.active")
    again_panel.wait_for(state="visible", timeout=10000)
    page.locator("#txtAKW").wait_for(state="visible", timeout=10000)
    original_conditions = page.locator("#dlQryCond")
    original_conditions.wait_for(state="visible", timeout=10000)
    conditions_text = original_conditions.inner_text()
    if query not in conditions_text:
        raise RuntimeError(
            "司法院再檢索分頁未顯示本次全文檢索條件，停止產生稽核畫面"
        )

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


def _capture_official_result_image(page: Page) -> bytes:
    """將司法院目前顯示的查詢結果頁擷取成高解析 PNG（不落地保存）。"""
    # 不依賴 Render 的 fontconfig。直接從 Playwright 攔截的本地字型網址抓取
    # OTF 二進位內容，再用 FontFace API 加入每一個官方 frame。這比單純插入
    # @font-face 更可靠：若 CSP、CORS 或字型解析失敗，會在此明確拋錯，不會
    # 繼續產出看似成功、實際全是方框的稽核圖片。
    font_css = f"""
      html, body, input, button, select, textarea, table, th, td, a, span, div, p,
      h1, h2, h3, h4, h5, h6, label, li {{
        font-family: 'FJUD Audit CJK', sans-serif !important;
      }}
      /* 保留官網的圖示字型，避免導覽列圖示被中文字型取代成方框。 */
      [class*='fa-'], .fa, .fas, .far, .fal,
      [class*='icon-'], [class^='icon-'] {{
        font-family: 'Font Awesome 5 Free', 'FontAwesome' !important;
      }}
      .fab {{
        font-family: 'Font Awesome 5 Brands' !important;
      }}
    """
    target_frames = [
        frame for frame in page.frames
        if "judgment.judicial.gov.tw" in frame.url
    ]
    if not target_frames:
        raise RuntimeError("找不到可套用中文字型的司法院頁面")
    for frame in target_frames:
        loaded = frame.evaluate(
            """
            async (fontUrl) => {
              const response = await fetch(fontUrl, {cache: 'force-cache'});
              if (!response.ok) {
                throw new Error(`中文字型下載失敗：HTTP ${response.status}`);
              }
              const fontBytes = await response.arrayBuffer();
              if (fontBytes.byteLength < 10000000) {
                throw new Error(`中文字型內容異常：${fontBytes.byteLength} bytes`);
              }
              const font = new FontFace(
                'FJUD Audit CJK',
                fontBytes,
                {style: 'normal', weight: '400'}
              );
              await font.load();
              document.fonts.add(font);
              await document.fonts.ready;
              return document.fonts.check(
                '16px "FJUD Audit CJK"',
                '司法院裁判書查詢賄賂政治獻金'
              );
            }
            """,
            FONT_ROUTE_URL,
        )
        if loaded is not True:
            raise RuntimeError(f"司法院頁面中文字型驗證失敗：{frame.url}")
        frame.add_style_tag(content=font_css)
        # 等待瀏覽器實際完成一次中文排版後再擷取。
        frame.evaluate(
            """
            async () => {
              await document.fonts.load(
                '16px "FJUD Audit CJK"',
                '司法院裁判書查詢賄賂政治獻金'
              );
              await document.fonts.ready;
              await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            }
            """
        )
    # iframe 預設高度可能只顯示畫面的一部分；列印前依內容高度展開，讓官方
    # 查詢條件及完整結果清單一起進入影像。使用 PNG 可避免 Chromium 在產生
    # 文字型 PDF 時因 Render 缺少繁體中文字型而出現亂碼。
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
    return page.screenshot(full_page=True, type="png")


def search_fjud_keyword(
    company_name: str,
    person_name: str,
    date_from: str,
    date_to: str,
    keyword: str,
) -> Tuple[List[Dict], List[bytes]]:
    """
    查詢單一關鍵字並回傳 (結構化結果, 官方結果頁PNG清單)。
    每一個司法院結果分頁各擷取一份影像；即使查無資料也至少會有一份。
    PNG 只存在本次 HTTP 回應的記憶體中，不寫入磁碟或資料庫。
    """
    company_name = (company_name or "").strip()
    person_name = (person_name or "").strip()
    keyword = (keyword or "").strip()
    if not company_name or not person_name:
        raise ValueError("公司名稱與負責人姓名不可為空")
    if keyword not in DEFAULT_KEYWORDS:
        raise ValueError("不允許的裁判書查詢關鍵字")

    results: List[Dict] = []
    evidence_images: List[bytes] = []
    _verify_cjk_font()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(
            locale="zh-TW",
            viewport={"width": 1600, "height": 1200},
            device_scale_factor=1.5,
            # 司法院頁面的 CSP 可能封鎖自訂字型來源；本頁只用於本次稽核
            # 截圖，字型內容仍由本機白名單路由提供。
            bypass_csp=True,
        )
        page = context.new_page()
        _register_cjk_font_route(page)
        try:
            _fill_and_submit(page, company_name, person_name, date_from, date_to, keyword)
            page_idx = 1
            while True:
                results.extend(_parse_current_page(page, keyword))
                evidence_images.append(_capture_official_result_image(page))
                if page_idx >= MAX_PAGES_PER_KEYWORD or not _has_next_page(page):
                    break
                _go_next_page(page)
                page_idx += 1
        finally:
            browser.close()
    return results, evidence_images


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

    _verify_cjk_font()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        context = browser.new_context(locale="zh-TW", bypass_csp=True)
        page = context.new_page()
        _register_cjk_font_route(page)

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
