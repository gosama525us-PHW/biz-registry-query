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
- <select id="jud_court" multiple> 的預設「所有法院」其實是空值選項；它與
  使用者在多選清單中明確選取全部法院的送出條件不同。本模組會排除空值，
  明確選取目前官網列出的每一個實際法院。
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

import os
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
FONTCONFIG_RUNTIME_DIR = Path("/tmp/fjud-fontconfig")
FONTCONFIG_CACHE_DIR = FONTCONFIG_RUNTIME_DIR / "cache"
FONTCONFIG_FILE = FONTCONFIG_RUNTIME_DIR / "fonts.conf"


def _verify_cjk_font() -> None:
    """確認建置階段下載的字型存在且大小合理。"""
    if not FONT_PATH.exists() or FONT_PATH.stat().st_size < 10_000_000:
        raise RuntimeError(
            "找不到有效的 fonts/NotoSansCJKtc-Regular.otf；請確認 Build Command 已執行 "
            "python install_cjk_font.py"
        )


def _chromium_font_env() -> Dict[str, str]:
    """建立 Chromium 專用、完全可寫入 /tmp 的 Fontconfig 設定。

    Render 建置環境不能寫入系統 fontconfig cache；舊作法把整份 OTF 透過
    FontFace 複製進主頁與 iframe，又會造成記憶體尖峰。這裡不執行 fc-cache，
    只讓 Chromium 在執行期直接掃描專案字型，快取統一寫到 /tmp。
    """
    _verify_cjk_font()
    FONTCONFIG_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    font_dir = str(FONT_PATH.parent).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    cache_dir = str(FONTCONFIG_CACHE_DIR).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    FONTCONFIG_FILE.write_text(
        "<?xml version='1.0'?>\n"
        "<!DOCTYPE fontconfig SYSTEM 'urn:fontconfig:fonts.dtd'>\n"
        "<fontconfig>\n"
        f"  <dir>{font_dir}</dir>\n"
        f"  <cachedir>{cache_dir}</cachedir>\n"
        "</fontconfig>\n",
        encoding="utf-8",
    )
    browser_env = os.environ.copy()
    browser_env["FONTCONFIG_FILE"] = str(FONTCONFIG_FILE)
    browser_env["XDG_CACHE_HOME"] = str(FONTCONFIG_RUNTIME_DIR / "xdg-cache")
    return browser_env


def _split_minguo_date(d: str):
    """7 碼民國年 YYYMMDD -> (year, month, day) 三個不補零的字串。"""
    d = (d or "").strip()
    if len(d) != 7 or not d.isdigit():
        raise ValueError(f"日期格式錯誤，應為 7 碼民國年 YYYMMDD，收到: {d!r}")
    y, m, day = d[:3], d[3:5], d[5:7]
    return str(int(y)), str(int(m)), str(int(day))


def _select_all_courts(page: Page) -> List[str]:
    """明確選取所有非空白法院，不使用預設的空值「所有法院」。"""
    court_select = page.locator("#jud_court")
    court_select.wait_for(state="attached", timeout=10000)
    court_values = court_select.locator("option").evaluate_all(
        "options => options.map(option => option.value).filter(value => value !== '')"
    )
    # 目前官網有 44 個實際法院。保留合理下限，若網站改版或選項異常就停止，
    # 避免悄悄以不完整法院範圍產生稽核資料。
    if len(court_values) < 40:
        raise RuntimeError(
            f"司法院實際法院選項數量異常：只取得 {len(court_values)} 個"
        )
    court_select.select_option(court_values)
    selected_values = court_select.locator("option:checked").evaluate_all(
        "options => options.map(option => option.value).filter(value => value !== '')"
    )
    if set(selected_values) != set(court_values):
        raise RuntimeError(
            f"司法院法院全選失敗：應選 {len(court_values)} 個，"
            f"實際選取 {len(selected_values)} 個"
        )
    return court_values


def _fill_and_submit(page: Page, company_name: str, person_name: str, date_from: str, date_to: str, keyword: str):
    page.goto(FJUD_URL, wait_until="networkidle")

    selected_courts = _select_all_courts(page)
    print(f"[FJUD:{keyword}] 已明確選取全部 {len(selected_courts)} 個法院", flush=True)
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
    required_court_names = (
        "憲法法庭",
        "最高法院",
        "臺灣臺北地方法院",
        "福建連江地方法院",
        "臺灣高雄少年及家事法院",
    )
    missing_courts = [name for name in required_court_names if name not in conditions_text]
    if missing_courts:
        raise RuntimeError(
            "司法院原檢索條件未完整顯示全法院，缺少："
            + "、".join(missing_courts)
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
    # Chromium 已透過執行期 Fontconfig 共用本地 Noto 字型。此處只指定字族，
    # 不再把 16MB OTF 複製進每個 frame，避免 Render 記憶體尖峰重啟。
    font_css = """
      html, body, input, button, select, textarea, table, th, td, a, span, div, p,
      h1, h2, h3, h4, h5, h6, label, li {{
        font-family: 'Noto Sans CJK TC', sans-serif !important;
      }}
      /* 保留官網的圖示字型，避免導覽列圖示被中文字型取代成方框。 */
      [class*='fa-'], .fa, .fas, .far, .fal,
      [class*='icon-'], [class^='icon-'] {{
        font-family: 'Font Awesome 5 Free', 'FontAwesome' !important;
      }}
      .fab {{
        font-family: 'Font Awesome 5 Brands' !important;
      }}
    """.replace("{{", "{").replace("}}", "}")
    target_frames = [
        frame for frame in page.frames
        if "judgment.judicial.gov.tw" in frame.url
    ]
    if not target_frames:
        raise RuntimeError("找不到可套用中文字型的司法院頁面")
    print(f"[FJUD:截圖] 套用共用中文字型（{len(target_frames)} 個頁框）", flush=True)
    for frame in target_frames:
        frame.add_style_tag(content=font_css)
        frame.evaluate(
            """
            async () => {
              await document.fonts.load(
                '16px "Noto Sans CJK TC"',
                '司法院裁判書查詢賄賂政治獻金'
              );
              await document.fonts.ready;
              await new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)));
            }
            """
        )
    print("[FJUD:截圖] 中文字型排版完成", flush=True)
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
    print("[FJUD:截圖] 開始擷取官方畫面", flush=True)
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
    browser_env = _chromium_font_env()
    print(f"[FJUD:{keyword}] 啟動瀏覽器", flush=True)
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            # Render 容器的 /dev/shm 空間有限，避免 Chromium 因共享記憶體不足
            # 被作業系統終止，導致前端收到空白 502 回應。
            args=["--disable-dev-shm-usage"],
            env=browser_env,
        )
        context = browser.new_context(
            locale="zh-TW",
            # 1280px 已足以清楚列印官方頁面；不再使用 1.5 倍像素，避免 PNG、
            # Base64 與 JSON 同時存在記憶體時產生不必要的尖峰。
            viewport={"width": 1280, "height": 900},
            device_scale_factor=1,
            # 保留此設定，避免官網 CSP 阻擋稽核畫面需要加入的樣式。
            bypass_csp=True,
        )
        page = context.new_page()
        try:
            _fill_and_submit(page, company_name, person_name, date_from, date_to, keyword)
            print(f"[FJUD:{keyword}] 已完成查詢並切換再檢索分頁", flush=True)
            page_idx = 1
            while True:
                results.extend(_parse_current_page(page, keyword))
                image = _capture_official_result_image(page)
                evidence_images.append(image)
                print(
                    f"[FJUD:{keyword}] 第 {page_idx} 頁截圖完成：{len(image)} bytes",
                    flush=True,
                )
                if page_idx >= MAX_PAGES_PER_KEYWORD or not _has_next_page(page):
                    break
                _go_next_page(page)
                page_idx += 1
        finally:
            browser.close()
    print(f"[FJUD:{keyword}] 完成，共 {len(results)} 筆", flush=True)
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

    browser_env = _chromium_font_env()
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--disable-dev-shm-usage"],
            env=browser_env,
        )
        context = browser.new_context(locale="zh-TW", bypass_csp=True)
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
