# -*- coding: utf-8 -*-
"""
工商登記查詢系統 — Flask 主程式

功能：
  輸入台灣公司統一編號（8 碼數字），由後端代理呼叫經濟部商業發展署
  「商工行政資料開放平臺」開放資料 API，合併「應用一」（基本資料）、
  「應用三」（所營事業）、「董監事資料」三支即時 API 結果，並另外查詢
  本地經理人資料庫（data/managers.db，定期整批下載建置，非即時 API），
  回傳整理後的公司登記資料。
  另提供「查詢深度」選項（單選，query_depth，值為 "1"／"2"／"3"）：
    深度 1（預設）：僅查詢本公司（即以下四大區塊），不觸發任何法人反查。
    深度 2：額外以「公司登記關鍵字查詢」API 反查董監事資料中「所代表法人」
            （Juristic_Person_Name）所對應的工商登記資料及其自身董監事清單
            （第二層）。
    深度 3：在深度 2 的基礎上，再對每一筆查到的第二層法人，取其自身董監事
            清單中的「所代表法人」，比照第二層做法再查一次（第三層）。
            **深度上限固定為 3，絕對不可再往下展開第四層**（不可對第三層
            法人的董監事「所代表法人」繼續查詢），程式中相關位置皆有明確
            註解標示此邊界，避免日後被誤改為無限遞迴。
  因該 API 為 LIKE 模糊比對，程式端會再做「完全比對過濾」避免誤判
  （詳見 api_research_name_lookup.md 與 query_juristic_person_registry()）。
  第二、三層合計對外部「公司登記關鍵字查詢」API 的實際呼叫次數，另設全域
  防禦性上限 MAX_JURISTIC_PERSON_LOOKUPS_TOTAL，超過上限者名稱仍會顯示於
  清單中（不會從畫面上消失），僅標示為未查詢（status="capped"），並以
  cache 避免同一法人名稱重複觸發外部呼叫。

安全設計重點：
  1. 使用者輸入一律先以 regex ^\\d{8}$ 驗證，僅允許 8 位數字，
     驗證失敗直接回錯誤訊息，絕不將原始輸入直接串入送給外部 API 的
     $filter 字串，避免 OData 注入。
  2. 前端顯示一律經由 Jinja2 auto-escape（未使用 |safe），可防止反射型 XSS。
  3. 伺服器僅綁定 127.0.0.1，且 Flask 預設僅提供本檔案所在目錄下的
     templates/static，不會外洩整個專案資料夾。
  4. 對外部 API 呼叫皆設定逾時（timeout），並捕捉例外，不將原始例外堆疊
     顯示給使用者。
  5. 登入驗證：所有既有查詢相關路由（/、/query）皆以 @login_required 裝飾器
     保護，須通過固定帳號密碼驗證才會建立已登入 session；密碼僅以雜湊值存放
     （werkzeug 產生），程式中不含任何明文密碼；session cookie 設定
     HttpOnly／SameSite=Lax，正式環境另加 Secure；並有簡易登入失敗節流機制。
     詳見本檔案「登入驗證設定」區塊。
     （註：本系統原設計為帳密＋Google Authenticator TOTP 兩關登入，因帳密
     登入本身持續發生「帳號或密碼錯誤」問題，CEO 明確要求先移除 TOTP 二次
     驗證、改回單純帳號密碼登入，待穩定後如有需要再評估加回；相關異動見
     2026-08-16 修改紀錄。）

資料來源：經濟部商業發展署 商工行政資料開放平臺（data.gcis.nat.gov.tw）
規格參考：C:\\AI-Team\\working\\biz_registry_query\\api_research.md（唯讀，不修改）
"""
import hmac
import os
import re
import ssl
import sqlite3
import time
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

import requests
from flask import Flask, redirect, render_template, request, session, url_for
from werkzeug.security import check_password_hash

app = Flask(__name__)


# ---------------------------------------------------------------------------
# 登入驗證設定
# ---------------------------------------------------------------------------
#
# CEO 核准之登入機制：單一固定帳號，帳號密碼驗證通過即可進入系統。
#
# 註：本系統原設計為帳密 + Google Authenticator TOTP 動態驗證碼兩關，因帳密
# 登入本身持續發生「帳號或密碼錯誤」問題（懷疑為環境變數複製貼上或手機鍵盤
# 自動大寫等操作性因素），CEO 於 2026-08-16 明確要求先移除 TOTP 二次驗證、
# 改回單純帳號密碼登入，待穩定後如有需要再評估加回。TOTP 相關程式碼
# （/verify-totp 路由、pyotp 相依套件、AUTH_TOTP_SECRET 環境變數）已一併移除。
#
# 重要（安全設計）：
#   - 密碼一律以雜湊值（werkzeug.security 產生）存放於環境變數
#     AUTH_PASSWORD_HASH，程式中不會、也絕不可出現明文密碼。
#   - FLASK_SECRET_KEY 為 Flask session 簽章用金鑰，同樣僅透過環境變數提供。
#   - 本機開發測試時可用專案根目錄下的 .env 檔案提供以上環境變數（見
#     _load_dotenv_if_present()），.env 已列入 .gitignore，絕不可提交版控
#     或部署至 Render；正式環境一律使用 Render Dashboard 設定的環境變數，
#     不依賴 .env 檔案是否存在。


def _load_dotenv_if_present() -> None:
    """
    輕量級本機開發用 .env 讀取器（僅本機測試使用，刻意不引入 python-dotenv
    這類額外套件相依，維持 requirements.txt 精簡）。

    僅在專案根目錄存在 .env 檔案時才讀取（正式部署環境 Render 不會有此檔案，
    環境變數由 Dashboard 直接注入，此函式此時不做任何事，不影響正式環境）。
    使用 os.environ.setdefault()，若環境變數已由外部（如 Render、或呼叫端
    shell）設定，則不會被 .env 內容覆蓋。
    """
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            os.environ.setdefault(key, value)


_load_dotenv_if_present()

# AUTH_USERNAME 額外做 .strip()：避免環境變數在 Render Dashboard 複製貼上時
# 不慎夾帶前後空白或換行，導致帳號比對一律失敗（曾為登入持續失敗之疑因之一）。
AUTH_USERNAME = os.environ.get("AUTH_USERNAME", "").strip()
AUTH_PASSWORD_HASH = os.environ.get("AUTH_PASSWORD_HASH", "")
FLASK_SECRET_KEY = os.environ.get("FLASK_SECRET_KEY", "")
IS_PRODUCTION = os.environ.get("FLASK_ENV", "").strip().lower() == "production"

# 兩項登入相關設定皆齊全才視為「已設定」，任一缺漏則一律拒絕登入（fail closed），
# 不會讓系統在設定不完整的狀態下意外允許登入或以不安全的預設值運作。
AUTH_CONFIGURED = bool(AUTH_USERNAME and AUTH_PASSWORD_HASH)

if IS_PRODUCTION and not (AUTH_CONFIGURED and FLASK_SECRET_KEY):
    # 正式環境缺少必要登入設定或 session 金鑰時，直接拒絕啟動，避免帶著不安全的
    # 設定上線（例如缺 FLASK_SECRET_KEY 會導致 session 無法安全簽章）。
    raise RuntimeError(
        "缺少必要環境變數（AUTH_USERNAME / AUTH_PASSWORD_HASH / "
        "FLASK_SECRET_KEY），正式環境（FLASK_ENV=production）拒絕啟動。"
    )

# 本機開發若未設定 FLASK_SECRET_KEY，退而使用僅供本次程序執行期間有效的隨機值
# （每次重啟伺服器都會不同，既有 session 會全部失效，屬預期行為，僅本機開發
# 適用，正式環境已由上方檢查強制要求必須明確提供）。
app.secret_key = FLASK_SECRET_KEY or os.urandom(32)

app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    # 僅正式環境（Render，強制 HTTPS）才要求 Secure cookie；本機測試為 http，
    # 若強制 Secure 會導致本機瀏覽器直接不送出 cookie，無法登入測試。
    SESSION_COOKIE_SECURE=IS_PRODUCTION,
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
)

# 登入失敗防護（單一帳號之小型內部系統，採簡易但合理的節流機制，非高複雜度方案）：
# 以 Flask session 記錄同一使用者（同一瀏覽器 session）連續失敗次數，達上限後
# 短時間內鎖定、要求稍候重試。此機制的已知限制：攻擊者若清除 cookie 可重新計次，
# 對於單一固定帳號之小型內部系統，此為合理、非高複雜度的基本防護，非用於抵禦
# 具備清除 cookie 能力之持續性自動化攻擊（如需更高強度防護，建議另加伺服器端
# IP 限流或帳號鎖定機制，屬未來加強項目）。
LOGIN_MAX_ATTEMPTS = 5
LOGIN_LOCKOUT_SECONDS = 60


def _check_lockout(fail_key: str, locked_key: str):
    """回傳 (是否仍在鎖定中 bool, 剩餘秒數 int)。鎖定時間已過期則自動清除。"""
    locked_until = session.get(locked_key)
    if locked_until and time.time() < locked_until:
        return True, int(locked_until - time.time()) + 1
    if locked_until:
        session.pop(locked_key, None)
        session.pop(fail_key, None)
    return False, 0


def _register_failure(fail_key: str, locked_key: str, max_attempts: int, lockout_seconds: int) -> None:
    """記錄一次失敗嘗試；達上限則設定鎖定到期時間並歸零計數。"""
    count = session.get(fail_key, 0) + 1
    if count >= max_attempts:
        session[locked_key] = time.time() + lockout_seconds
        session[fail_key] = 0
    else:
        session[fail_key] = count


def _clear_lockout(fail_key: str, locked_key: str) -> None:
    session.pop(fail_key, None)
    session.pop(locked_key, None)


def login_required(view_func):
    """保護既有查詢相關路由：未登入（session 未通過帳密驗證）一律導向 /login。"""

    @wraps(view_func)
    def wrapped_view(*args, **kwargs):
        if not session.get("logged_in"):
            return redirect(url_for("login"))
        return view_func(*args, **kwargs)

    return wrapped_view


# ---------------------------------------------------------------------------
# 常數設定
# ---------------------------------------------------------------------------

TAX_ID_PATTERN = re.compile(r"^[0-9]{8}$")

API_BASE_ONE = "https://data.gcis.nat.gov.tw/od/data/api/5F64D864-61CB-4D0D-8AD9-492047CC1EA6"
API_BASE_THREE = "https://data.gcis.nat.gov.tw/od/data/api/236EE382-4942-41A9-BD03-CA0709025E7C"
# 董監事資料（應用四）：語法與應用一/應用三完全一致，免金鑰，經 researcher 實測確認
# （C:\AI-Team\working\biz_registry_query\api_research_directors_managers.md 第二節）。
API_BASE_DIRECTORS = "https://data.gcis.nat.gov.tw/od/data/api/4E5F7653-1B91-4DDC-99D5-468530FAE396"

# 「公司登記關鍵字查詢」API：用於反查董監事「所代表法人」名稱對應的統一編號與基本資料
# （新功能：所代表法人查詢），免金鑰，語法為
# Company_Name like '關鍵字' and Company_Status eq '狀態代碼'（兩者皆為必填）。
# 重要：比對機制為 LIKE（字串包含），非精確比對，已由 researcher 實測證實會誤判
# （例如查「台積電」會誤中「台積電機」「台積電梯」兩間無關公司），因此程式端呼叫後
# 必須另外做完全比對過濾，詳見 filter_exact_company_name_matches()，不得將未過濾的
# LIKE 查詢結果直接顯示給使用者。查證依據：
# C:\\AI-Team\\working\\biz_registry_query\\api_research_name_lookup.md。
API_BASE_KEYWORD_SEARCH = "https://data.gcis.nat.gov.tw/od/data/api/6BBA2268-1367-4B42-9CCA-BC17499EBE8C"

REQUEST_TIMEOUT_SECONDS = 10


class _RelaxedStrictSSLAdapter(requests.adapters.HTTPAdapter):
    """
    僅放寬 Python 3.13+ 預設啟用的 VERIFY_X509_STRICT（RFC 5280 結構嚴格檢查），
    用於相容 data.gcis.nat.gov.tw 憑證鏈缺少 Subject Key Identifier 擴充欄位的情況
    （2026-08-16 部署到 Render 後發現：本機開發環境 Python 版本較舊未觸發此問題，
    Render 環境 Python 3.13+ 會因此拒絕連線，回傳 SSLCertVerificationError）。

    重要（安全性說明）：此設定僅放寬「憑證格式是否嚴格符合 RFC 5280 結構規範」
    （例如 CA 憑證是否具備 SKI/AKI 擴充欄位）的檢查，*不影響*信任鏈驗證（是否
    為受信任 CA 簽發）、憑證有效期、網域名稱比對等核心 TLS 安全機制。
    官方修法來源：https://docs.python.org/3/whatsnew/3.13.html
    """

    def init_poolmanager(self, *args, **kwargs):
        ctx = ssl.create_default_context()
        ctx.verify_flags &= ~ssl.VERIFY_X509_STRICT
        kwargs["ssl_context"] = ctx
        return super().init_poolmanager(*args, **kwargs)


# 僅套用於政府開放資料平臺這個 host，不影響其他任何對外連線的憑證驗證嚴謹度。
_gcis_session = requests.Session()
_gcis_session.mount("https://data.gcis.nat.gov.tw", _RelaxedStrictSSLAdapter())

# 單次查詢請求（第二層＋第三層合計，不分層各自計算）對外部「公司登記關鍵字查詢」
# API 實際發動查詢次數的「全域防禦性上限」（CEO 明確要求：正常查詢不會觸及此數字，
# 目的僅為防止異常資料如巨量交叉代表關係拖垮系統或對政府免費 API 造成不合理負擔）。
#
# 重要：與舊版 MAX_JURISTIC_PERSON_LOOKUPS（已移除）不同，觸及此上限**不會**讓法人
# 名稱從畫面上消失——該筆法人仍會出現在清單中，只是不會真的發動 API 查詢，狀態標示
# 為 status="capped"。任何「所代表法人」皆不可被悄悄漏掉、清單上完全看不到。
MAX_JURISTIC_PERSON_LOOKUPS_TOTAL = 200

# 經理人本地資料庫（方案 A：定期整批下載 CSV 建索引，非即時 API，經 CEO 核准）。
# 資料庫由獨立的 update_managers_db.py 手動執行建置／更新，本檔案僅負責唯讀查詢，
# 一律使用參數化查詢（? placeholder），不做任何字串拼接。
MANAGERS_DB_PATH = Path(__file__).resolve().parent / "data" / "managers.db"


# ---------------------------------------------------------------------------
# 工具函式
# ---------------------------------------------------------------------------

def is_valid_tax_id(tax_id: str) -> bool:
    """僅允許 8 位純數字，任何非此格式（含英文字母、特殊符號、長度不符）一律拒絕。"""
    if not isinstance(tax_id, str):
        return False
    return bool(TAX_ID_PATTERN.fullmatch(tax_id))


def roc_date_to_ad(roc_date: str) -> str:
    """
    將民國年格式（無分隔符，如 0760221）轉換為西元年顯示（如 1987-02-21）。
    格式不符或空值時，回傳「無資料」，避免顯示錯誤或程式崩潰。
    """
    if not roc_date or not isinstance(roc_date, str):
        return "無資料"
    roc_date = roc_date.strip()
    if not re.fullmatch(r"\d{7}", roc_date):
        return "無資料"
    try:
        roc_year = int(roc_date[:3])
        month = int(roc_date[3:5])
        day = int(roc_date[5:7])
        ad_year = roc_year + 1911
        # 驗證日期合法性（例如避免 00 月 00 日等異常值造成顯示錯誤）
        datetime(ad_year, month, day)
        return f"{ad_year}-{month:02d}-{day:02d}"
    except (ValueError, IndexError):
        return "無資料"


def format_print_date() -> str:
    """
    產生「列印日期」字串，代表伺服器處理本次查詢當下的日期（動態產生，非固定
    文字），格式比照 PDF（input/商工登記頁面.pdf 最上方「列印日期:115-08-13」）：
    「{民國年}-{月:02d}-{日:02d}」，冒號後不加空格。

    民國年 = 西元年 - 1911（與 roc_date_to_ad() 的反向換算邏輯一致）。
    僅使用伺服器當下時間，不涉及任何使用者輸入或外部 API 資料，故不需額外
    的輸入驗證或例外處理。
    """
    today = datetime.now()
    roc_year = today.year - 1911
    return f"{roc_year}-{today.month:02d}-{today.day:02d}"


def format_amount(value) -> str:
    """將金額數字轉為千分位字串，供頁面顯示；無資料時回傳「無資料」。"""
    if value is None or value == "":
        return "無資料"
    try:
        return f"{int(value):,}"
    except (ValueError, TypeError):
        return str(value)


def parse_director_items(director_list: list) -> list:
    """
    將董監事資料 API（API_BASE_DIRECTORS）回傳的原始資料 list，轉換為前端可直接
    使用的 {"position", "name", "juristic_person", "shareholding"} dict list。

    此函式為共用解析邏輯，供 query_company()（主公司董監事資料）與
    query_juristic_person_registry()（第二層法人自身董監事資料）共同呼叫，
    避免同一段欄位轉換邏輯重複維護兩份。

    director_list 若為 None 或空 list，回傳空 list，不拋出例外。
    """
    items = []
    for item in director_list or []:
        items.append(
            {
                "position": item.get("Person_Position_Name") or "無資料",
                "name": item.get("Person_Name") or "無資料",
                "juristic_person": item.get("Juristic_Person_Name") or "",
                "shareholding": format_amount(item.get("Person_Shareholding")),
            }
        )
    return items


def call_gcis_api(base_url: str, tax_id: str, top: int | None = None):
    """
    呼叫商工行政資料開放平臺 API。

    重要：tax_id 在呼叫此函式前已經過 is_valid_tax_id() 驗證（僅 8 位數字），
    因此可安全地組入 $filter 字串，不含任何使用者可控的特殊字元。

    回傳：(成功與否 bool, 資料 list 或 None, 錯誤訊息 str 或 None)
    """
    params = {
        "$format": "json",
        "$filter": f"Business_Accounting_NO eq '{tax_id}'",
    }
    if top is not None:
        params["$top"] = top

    try:
        resp = _gcis_session.get(base_url, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.Timeout as e:
        print(f"[GCIS_API_ERROR] type=Timeout detail={e} url={base_url}", flush=True)
        return False, None, "查詢外部政府資料平臺逾時，請稍後再試。"
    except requests.exceptions.RequestException as e:
        print(f"[GCIS_API_ERROR] type={type(e).__name__} detail={e} url={base_url}", flush=True)
        return False, None, "無法連線至外部政府資料平臺，請稍後再試。"

    if resp.status_code != 200:
        return False, None, f"外部政府資料平臺回應異常（狀態碼 {resp.status_code}），請稍後再試。"

    # 實測發現：查無資料時，官方 API 回傳 HTTP 200 但內容為完全空白（0 bytes），
    # 而非研究報告原先預期的 JSON 空陣列字面值 "[]"。此為邊界情況的實測結果，
    # 與 api_research.md 第七節第5點所述「尚待測試」的推測不同，需視同「查無資料」處理，
    # 而非視為格式錯誤。
    if not resp.text.strip():
        return True, [], None

    try:
        data = resp.json()
    except ValueError:
        return False, None, "外部政府資料平臺回傳格式異常，請稍後再試。"

    # 已知錯誤格式：API 在 $filter 有誤時會回傳如
    # {"error": {"message": "$filter參數有誤，請查明後繼續"}} 或類似結構，
    # 而非預期的陣列，需先判斷型別避免後續處理出錯。
    if not isinstance(data, list):
        return False, None, "外部政府資料平臺查詢條件異常，請稍後再試。"

    return True, data, None


# ---------------------------------------------------------------------------
# 新功能：董監事「所代表法人」工商登記反查
# ---------------------------------------------------------------------------
#
# 需求來源：CEO 交辦，查詢統編時若使用者啟用開關，額外查詢本次結果中董監事資料的
# 「所代表法人」（Juristic_Person_Name 欄位，非空字串者）所對應的工商登記資料。
# 官方 API 規格與比對機制風險，已由 researcher 查證（api_research_name_lookup.md）：
# 比對為 LIKE（字串包含），非精確比對，實測會誤判（如「台積電」誤中「台積電機」
# 「台積電梯」），因此以下實作**必須**在程式端做「完全比對過濾」，只保留
# Company_Name 去除頭尾空白後與查詢用法人名稱（同樣去除頭尾空白）完全相等的筆數。

ODATA_SINGLE_QUOTE = "'"


def escape_odata_string_literal(value: str) -> str:
    """
    將字串中的單引號依 OData 字串常值escaping 慣例，替換為兩個單引號（'' ），
    避免破壞 $filter 語法或造成注入疑慮。此為 OData / SQL 常見的字串常值轉義慣例
    （單引號跳脫為連續兩個單引號），非本系統自創規則。
    """
    return value.replace(ODATA_SINGLE_QUOTE, ODATA_SINGLE_QUOTE * 2)


def sanitize_juristic_person_name(name: str):
    """
    查詢「所代表法人」名稱前的正規化與安全性檢查。

    回傳：(是否可送出查詢 bool, 正規化後名稱 str)

    - 去除頭尾空白（trim），與 researcher 報告 3.4 節「多一個空白即查無資料」的
      實測發現一致，trim 可避免因原始資料尾端空白造成不必要的查無結果。
    - 空字串（trim 後）直接視為不可查詢。
    - 含換行、Tab 等控制字元視為異常名稱，直接排除、不送出查詢，避免破壞
      HTTP 查詢參數或 $filter 語法。
    - 名稱長度異常過長（超過 100 字）視為異常資料，直接排除不查詢，屬防禦性上限，
      正常公司登記全名不會超過此長度。

    單引號不在此函式處理（單引號是合法可查詢字元，僅需在組 $filter 字串時透過
    escape_odata_string_literal() 轉義，而非視為異常直接排除），故此函式不排除
    含單引號的名稱。
    """
    if not isinstance(name, str):
        return False, ""
    trimmed = name.strip()
    if not trimmed:
        return False, ""
    if re.search(r"[\r\n\t]", trimmed):
        return False, trimmed
    if len(trimmed) > 100:
        return False, trimmed
    return True, trimmed


def call_gcis_keyword_search_api(company_name_query: str, top: int = 20):
    """
    呼叫「公司登記關鍵字查詢」API（$filter 使用
    Company_Name like '關鍵字' and Company_Status eq 01，01＝核准設立）。

    重要：此 API 為 LIKE（字串包含）比對，非精確比對，本函式**僅負責發送請求並回傳
    原始結果**，不做任何過濾。呼叫端（query_juristic_person_registry()）取得結果後
    必須再呼叫 filter_exact_company_name_matches() 做完全比對過濾，才可顯示給使用者，
    避免張冠李戴（誤判風險已由 researcher 實測證實，見 api_research_name_lookup.md）。

    company_name_query 必須是已經過 sanitize_juristic_person_name() 正規化、且已經過
    escape_odata_string_literal() 轉義單引號後的字串，呼叫前不驗證，交由呼叫端保證。

    回傳：(成功與否 bool, 資料 list 或 None, 錯誤訊息 str 或 None)
    """
    params = {
        "$format": "json",
        "$filter": f"Company_Name like '{company_name_query}' and Company_Status eq 01",
        "$top": top,
    }

    try:
        resp = _gcis_session.get(API_BASE_KEYWORD_SEARCH, params=params, timeout=REQUEST_TIMEOUT_SECONDS)
    except requests.exceptions.Timeout as e:
        print(f"[GCIS_API_ERROR] type=Timeout detail={e} url={API_BASE_KEYWORD_SEARCH}", flush=True)
        return False, None, "查詢外部政府資料平臺逾時，請稍後再試。"
    except requests.exceptions.RequestException as e:
        print(f"[GCIS_API_ERROR] type={type(e).__name__} detail={e} url={API_BASE_KEYWORD_SEARCH}", flush=True)
        return False, None, "無法連線至外部政府資料平臺，請稍後再試。"

    if resp.status_code != 200:
        return False, None, f"外部政府資料平臺回應異常（狀態碼 {resp.status_code}），請稍後再試。"

    # 查無資料時回傳 HTTP 200 但內容完全空白，與既有 call_gcis_api() 的實測發現一致
    # （見 api_research_name_lookup.md 第 3.5、3.6 節：政府機關／財團法人查無資料時
    # 回應行為與此完全相同）。
    if not resp.text.strip():
        return True, [], None

    try:
        data = resp.json()
    except ValueError:
        return False, None, "外部政府資料平臺回傳格式異常，請稍後再試。"

    if not isinstance(data, list):
        return False, None, "外部政府資料平臺查詢條件異常，請稍後再試。"

    return True, data, None


def filter_exact_company_name_matches(records: list, query_name: str) -> list:
    """
    對 call_gcis_keyword_search_api() 回傳的原始 LIKE 查詢結果做「完全比對過濾」：
    只保留 Company_Name 去除頭尾空白後，與 query_name（同樣去除頭尾空白）完全相等
    的筆數，其餘一律捨棄。

    這是本功能避免誤判（如查「台積電」誤中「台積電機」「台積電梯」）的核心防線，
    務必在任何顯示邏輯之前呼叫，不得省略。

    records 中每筆若非 dict 或缺少 Company_Name（型別非 str）者，一律視為不相符、
    直接排除，不拋出例外。
    """
    normalized_query = query_name.strip()
    exact_matches = []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        company_name = record.get("Company_Name")
        if isinstance(company_name, str) and company_name.strip() == normalized_query:
            exact_matches.append(record)
    return exact_matches


def query_juristic_person_registry(raw_name: str) -> dict:
    """
    查詢單一「所代表法人」名稱之工商登記基本資料，並額外查詢該法人自身的董監事
    清單（同一層內的資料，非遞迴）。

    重要：本函式本身**絕對不會**對取得的董監事清單中「所代表法人」欄位再次呼叫
    任何查詢函式（不論是本函式或 call_gcis_keyword_search_api()）——是否要再往下
    查一層（第三層），完全由呼叫端（路由層 /query）依使用者選擇的 query_depth
    明確決定並另行呼叫 query_juristic_persons_for_directors()，本函式本身不含任何
    遞迴邏輯，避免日後被誤改為無限遞迴。

    回傳 dict：
      status: "found"（完全比對後查到唯一資料）
            | "not_found"（查無資料，含完全比對後被過濾掉的情況）
            | "skipped"（名稱格式異常，未送出查詢）
            | "error"（外部 API 呼叫失敗，如逾時／連線失敗／回應格式異常）
      name: str，原始（未正規化）法人名稱，供前端顯示標題
      data: dict 或 None，找到時的基本資料（tax_id / company_name / status /
            responsible_name / capital_stock / paid_in_capital / location /
            director_items）；director_items 為該法人自身的董監事清單（格式同
            parse_director_items() 回傳），查詢失敗或查無資料時為空 list
      message: str 或 None，狀態說明文字，found 時為 None
    """
    can_query, normalized_name = sanitize_juristic_person_name(raw_name)
    if not can_query:
        return {
            "status": "skipped",
            "name": raw_name,
            "data": None,
            "message": "法人名稱格式異常（空白、過長或含控制字元），已略過查詢。",
        }

    escaped_name = escape_odata_string_literal(normalized_name)
    ok, records, err = call_gcis_keyword_search_api(escaped_name)
    if not ok:
        return {
            "status": "error",
            "name": raw_name,
            "data": None,
            "message": err or "查詢外部政府資料平臺時發生錯誤，請稍後再試。",
        }

    exact_matches = filter_exact_company_name_matches(records, normalized_name)
    if not exact_matches:
        return {
            "status": "not_found",
            "name": raw_name,
            "data": None,
            "message": "查無工商登記資料（可能為政府機關、財團法人等非公司登記主體，或名稱寫法有差異）。",
        }

    matched = exact_matches[0]
    matched_tax_id = matched.get("Business_Accounting_NO") or "無資料"

    # 額外查詢該法人自身的董監事資料：僅多呼叫一次既有的董監事 API
    # （API_BASE_DIRECTORS，與 query_company() 查主公司董監事所用的同一支 API）。
    # 本函式到此為止，絕對不可對這裡查到的董監事清單中「所代表法人」再次呼叫
    # query_juristic_person_registry() 或 call_gcis_keyword_search_api()——
    # 是否要再往下查一層（第三層），一律由路由層 /query 依 query_depth 明確決定，
    # 本函式本身不含任何遞迴邏輯，避免日後被誤改為無限遞迴
    # （見本檔案頂端 module docstring）。此處查詢失敗或查無資料，比照
    # query_company() 對董監事查詢失敗的容錯方式，不視為整體失敗，僅
    # director_items 留空 list。
    jp_director_items = []
    if is_valid_tax_id(matched_tax_id):
        ok_dir, jp_director_list, _err_dir = call_gcis_api(
            API_BASE_DIRECTORS, matched_tax_id, top=100
        )
        if ok_dir and jp_director_list:
            jp_director_items = parse_director_items(jp_director_list)

    return {
        "status": "found",
        "name": raw_name,
        "data": {
            "tax_id": matched_tax_id,
            "company_name": matched.get("Company_Name") or "無資料",
            "status": matched.get("Company_Status_Desc") or "無資料",
            "responsible_name": matched.get("Responsible_Name") or "無資料",
            "capital_stock": format_amount(matched.get("Capital_Stock_Amount")),
            "paid_in_capital": format_amount(matched.get("Paid_In_Capital_Amount")),
            "location": matched.get("Company_Location") or "無資料",
            "director_items": jp_director_items,
        },
        "message": None,
    }


def collect_juristic_person_names(director_items: list) -> list:
    """
    從董監事清單中收集所有「所代表法人」不為空字串者，依原始出現順序去重
    （去重比對以 trim 後字串為 key，避免僅空白差異被誤判為不同法人；回傳值本身
    仍保留每個去重後名稱第一次出現時的原始字串，未經 trim，供前端如實顯示）。
    """
    seen = set()
    names = []
    for item in director_items or []:
        juristic_person = item.get("juristic_person")
        if not juristic_person:
            continue
        key = juristic_person.strip()
        if not key or key in seen:
            continue
        seen.add(key)
        names.append(juristic_person)
    return names


def query_juristic_person_registry_cached(raw_name: str, cache: dict, call_counter: list) -> dict:
    """
    query_juristic_person_registry() 的快取＋全域防禦性上限包裝版本。

    cache：跨「同一次 /query 請求」（第二層＋第三層合計，不跨 request 共用，路由層
           每次請求各自建立新的 cache）共用的 dict，key 為 trim 後的法人名稱，
           value 為該名稱的查詢結果 dict。同一法人名稱（例如互為代表關係、或同一
           法人在第二層與第三層皆出現）只會實際觸發一次外部 API 呼叫，第二次起
           直接回傳快取結果，兩處顯示的資料因此保證一致。

    call_counter：單元素 list（如 [0]），作為可變計數器，記錄本次請求已對外部
           「公司登記關鍵字查詢」API 實際發動查詢的次數（不含快取命中）。當累計
           次數達到 MAX_JURISTIC_PERSON_LOOKUPS_TOTAL 時，之後未快取的名稱一律
           回傳 status="capped"，**不會**呼叫 query_juristic_person_registry()，
           但仍會回傳一筆結果（讓該法人名稱可以顯示在畫面清單上，不會消失）。
    """
    key = raw_name.strip() if isinstance(raw_name, str) else raw_name
    if key in cache:
        return cache[key]

    if call_counter[0] >= MAX_JURISTIC_PERSON_LOOKUPS_TOTAL:
        result = {
            "status": "capped",
            "name": raw_name,
            "data": None,
            "message": "已達本次查詢防禦性上限（200筆），為保護系統穩定性未再查詢。",
        }
        cache[key] = result
        return result

    call_counter[0] += 1
    result = query_juristic_person_registry(raw_name)
    cache[key] = result
    return result


def query_juristic_persons_for_directors(director_items: list, cache: dict, call_counter: list) -> dict:
    """
    依董監事清單，查詢每個「所代表法人」的工商登記資料。

    CEO 明確要求「有多少查多少，不得遺漏」：本函式**不做任何切片／截斷**，去重後
    的每一筆「所代表法人」皆會出現在回傳的 results 中；僅在觸及全域防禦性上限
    MAX_JURISTIC_PERSON_LOOKUPS_TOTAL 時，才會停止實際發動外部查詢（該筆結果仍會
    出現在 results 中，status="capped"，詳見 query_juristic_person_registry_cached()）。

    cache、call_counter：由呼叫端（路由層 /query）建立並在第二層／第三層之間共用，
    確保同一法人名稱不重複計入呼叫次數、也不重複實際查詢。

    回傳 dict：
      results: list，每筆為 query_juristic_person_registry_cached() 的回傳結果，
               筆數等於去重後的「所代表法人」總數（total_count），不截斷
      total_count: int，去重後的「所代表法人」總數
      capped_count: int，results 中 status=="capped" 的筆數，供前端提醒用，非阻擋
    """
    names = collect_juristic_person_names(director_items)
    results = [
        query_juristic_person_registry_cached(name, cache, call_counter) for name in names
    ]
    capped_count = sum(1 for r in results if r["status"] == "capped")
    return {
        "results": results,
        "total_count": len(names),
        "capped_count": capped_count,
    }


def query_managers_local(tax_id: str) -> dict:
    """
    查詢本地經理人資料庫（data/managers.db）。

    此資料庫由獨立腳本 update_managers_db.py 手動執行下載官方整批 CSV
    （公司登記經理人資料集，每月更新）後建置，非即時 API，查詢邏輯與
    call_gcis_api() 呼叫外部 API 的邏輯彼此獨立、互不影響。

    重要：tax_id 在呼叫此函式前已經過 is_valid_tax_id() 驗證（僅 8 位數字），
    但此處仍一律使用參數化查詢（? placeholder），不做字串拼接，即使輸入已
    受信任，仍維持「絕不可信任輸入」的防禦性寫法，避免日後修改時不慎引入
    SQL injection 風險。

    回傳 dict：
      status: "ok"（資料庫存在且查詢成功，records 可能為空清單代表查無資料）
            | "no_db"（資料庫檔案尚未建置）
            | "error"（資料庫存在但查詢時發生非預期錯誤）
      records: list，每筆為 {"name", "onboard_date", "note"}
      updated_at: str 或 None，資料庫建置／更新時間（供前端顯示資料時效）
      message: str 或 None，狀態說明文字（no_db / error 時提供）

    注意：此 dict 的鍵名刻意避開 "items"，因為 Jinja2 模板中對 dict 使用
    「.」屬性存取語法時，若鍵名與 dict 內建方法（如 items、keys、values）
    同名，會取得該內建方法本身而非對應的鍵值，導致模板迴圈出錯
    （TypeError: 'builtin_function_or_method' object is not iterable）。
    """
    if not MANAGERS_DB_PATH.exists():
        return {
            "status": "no_db",
            "records": [],
            "updated_at": None,
            "message": "經理人資料庫尚未建置，請聯繫系統管理員。",
        }

    conn = None
    try:
        conn = sqlite3.connect(str(MANAGERS_DB_PATH))
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        cur.execute(
            "SELECT manager_name, onboard_date, note FROM managers WHERE tax_id = ? "
            "ORDER BY onboard_date",
            (tax_id,),
        )
        rows = cur.fetchall()

        updated_at = None
        try:
            cur.execute("SELECT value FROM meta WHERE key = ?", ("updated_at",))
            meta_row = cur.fetchone()
            if meta_row:
                updated_at = meta_row[0]
        except sqlite3.Error:
            # meta 表若不存在或格式異常，不影響經理人清單本身的查詢結果，
            # 僅無法顯示資料庫更新時間。
            updated_at = None
    except sqlite3.Error:
        return {
            "status": "error",
            "records": [],
            "updated_at": None,
            "message": "查詢經理人本地資料庫時發生錯誤，請稍後再試或聯繫系統管理員。",
        }
    finally:
        if conn is not None:
            conn.close()

    records = [
        {
            "name": row["manager_name"] or "無資料",
            "onboard_date": roc_date_to_ad(row["onboard_date"]),
            "note": row["note"] or "",
        }
        for row in rows
    ]
    return {
        "status": "ok",
        "records": records,
        "updated_at": updated_at,
        "message": None,
    }


def query_company(tax_id: str):
    """
    合併呼叫「應用一」（基本資料）與「應用三」（所營事業）兩支 API，
    整理為前端可直接使用的資料結構。

    回傳：(成功與否 bool, 結果 dict 或 None, 錯誤訊息 str 或 None)
    """
    ok, basic_list, err = call_gcis_api(API_BASE_ONE, tax_id)
    if not ok:
        return False, None, err

    if not basic_list:
        return False, None, "查無此統一編號之公司登記資料，請確認輸入是否正確。"

    basic = basic_list[0]

    ok3, biz_list, err3 = call_gcis_api(API_BASE_THREE, tax_id, top=5)
    business_items = []
    if ok3 and biz_list:
        cmp_business = biz_list[0].get("Cmp_Business") or []
        for item in cmp_business:
            desc = item.get("Business_Item_Desc")
            code = item.get("Business_Item")
            if desc:
                business_items.append({"code": code or "", "desc": desc})
    # 應用三查詢失敗不視為整體失敗（基本資料仍可顯示），僅所營事業清單留空。

    ok4, director_list, err4 = call_gcis_api(API_BASE_DIRECTORS, tax_id, top=100)
    director_items = []
    if ok4 and director_list:
        director_items = parse_director_items(director_list)
    # 董監事查詢失敗同樣不視為整體失敗，僅該區塊清單留空（與所營事業處理方式一致）。

    managers_info = query_managers_local(tax_id)

    result = {
        "tax_id": basic.get("Business_Accounting_NO") or tax_id,
        "company_name": basic.get("Company_Name") or "無資料",
        "status": basic.get("Company_Status_Desc") or "無資料",
        "responsible_name": basic.get("Responsible_Name") or "無資料",
        "capital_stock": format_amount(basic.get("Capital_Stock_Amount")),
        "paid_in_capital": format_amount(basic.get("Paid_In_Capital_Amount")),
        # 每股金額／已發行股份總數：「應用一」原本即有回傳此 2 欄位（Share_Val／
        # Equity_Amt，見 api_research.md 第 3.2、4.1 節實測紀錄），舊版程式未取用，
        # 本次比照 CEO 提供之官方頁面版面新增顯示，皆用 format_amount() 千分位處理。
        "share_val": format_amount(basic.get("Share_Val")),
        "equity_amt": format_amount(basic.get("Equity_Amt")),
        "location": basic.get("Company_Location") or "無資料",
        "setup_date": roc_date_to_ad(basic.get("Company_Setup_Date")),
        # 最後核准變更日期：實測（統編 97160609）確認 Change_Of_Approval_Data 格式與
        # Company_Setup_Date 相同，皆為民國年無分隔符 7 碼（如 "1150708"），故沿用
        # roc_date_to_ad()；該函式對格式不符或空值一律回傳「無資料」，不會顯示錯誤
        # 格式或造成例外，即使日後遇到格式不同的資料也能安全降級。
        "last_change_date": roc_date_to_ad(basic.get("Change_Of_Approval_Data")),
        "register_org": basic.get("Register_Organization_Desc") or "無資料",
        "business_items": business_items,
        "director_items": director_items,
        "managers": managers_info,
        # 董監事「所代表法人」工商登記查詢結果，預設 None（query_depth=="1" 時維持
        # None，前端據此判斷是否顯示該區塊）。此鍵值不受本函式（query_company()）
        # 內部邏輯影響，一律由路由層 query() 依使用者選擇的 query_depth 決定是否
        # 覆寫，確保「query_depth=="1" 時的行為與既有四大區塊查詢完全等價，不會多
        # 觸發任何額外 API 呼叫」。
        "juristic_persons": None,
    }
    return True, result, None


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------

# 「查詢深度」選項合法值；表單缺漏或送出非預期值時一律視為預設值 "1"
# （僅查詢本公司），採白名單方式處理，不信任前端傳來的原始字串。
VALID_QUERY_DEPTHS = ("1", "2", "3")
DEFAULT_QUERY_DEPTH = "1"


def normalize_query_depth(raw_value) -> str:
    """將表單傳入的 query_depth 正規化為合法值，非白名單內的值一律視為預設值。"""
    if raw_value in VALID_QUERY_DEPTHS:
        return raw_value
    return DEFAULT_QUERY_DEPTH


@app.route("/login", methods=["GET", "POST"])
def login():
    """
    帳號密碼登入。驗證成功即直接設定 session["logged_in"] = True。

    （註：本系統原設計為帳密登入後尚須通過 /verify-totp 第二關，因帳密登入
    本身持續發生「帳號或密碼錯誤」問題，CEO 於 2026-08-16 明確要求先移除
    TOTP 二次驗證，改回單純帳號密碼登入，待穩定後如有需要再評估加回。）

    驗證失敗（帳號錯、密碼錯、或系統未設定登入資訊）一律顯示相同的中性錯誤訊息，
    不透露具體是帳號還是密碼有誤，避免帳號列舉攻擊。
    """
    if session.get("logged_in"):
        return redirect(url_for("index"))

    error = None
    if request.method == "POST":
        locked, remaining_seconds = _check_lockout("login_fail_count", "login_locked_until")
        if locked:
            error = f"嘗試次數過多，請稍候約 {remaining_seconds} 秒後再試。"
        else:
            # .strip() 避免行動裝置輸入法或複製貼上不慎夾帶前後空白導致帳號比對
            # 失敗；密碼欄位刻意不 strip（密碼理論上可能故意含前後空白）。
            username = request.form.get("username", "").strip()
            password = request.form.get("password", "")
            valid = False
            if AUTH_CONFIGURED:
                # hmac.compare_digest 避免帳號比對本身受時序攻擊影響；
                # check_password_hash 內部亦採用時序安全比對。
                username_ok = hmac.compare_digest(username, AUTH_USERNAME)
                password_ok = check_password_hash(AUTH_PASSWORD_HASH, password)
                valid = username_ok and password_ok

            if valid:
                session.permanent = True
                session["logged_in"] = True
                _clear_lockout("login_fail_count", "login_locked_until")
                return redirect(url_for("index"))

            _register_failure(
                "login_fail_count", "login_locked_until", LOGIN_MAX_ATTEMPTS, LOGIN_LOCKOUT_SECONDS
            )
            error = "帳號或密碼錯誤，請確認後重試。"

    return render_template("login.html", error=error)


@app.route("/logout", methods=["POST"])
def logout():
    """登出：清除整個 session（含 logged_in 及失敗計數狀態）。"""
    session.clear()
    return redirect(url_for("login"))


@app.route("/", methods=["GET"])
@login_required
def index():
    return render_template(
        "index.html",
        result=None,
        error=None,
        query_value="",
        query_depth_value=DEFAULT_QUERY_DEPTH,
        print_date=None,
    )


@app.route("/query", methods=["POST"])
@login_required
def query():
    raw_input = request.form.get("tax_id", "")
    # 僅取顯示用途，避免前端 echo 出過長或異常內容；顯示時仍交由 Jinja2 自動跳脫。
    display_value = raw_input.strip()[:20]

    tax_id = raw_input.strip()

    # 「查詢深度」單選（取代舊版 checkbox「同時查詢董監事所代表法人之工商登記資料」）：
    #   "1" = 僅第一層（本公司基本資料，即舊版未勾選 checkbox 的行為）
    #   "2" = 查到第二層（所代表法人基本資料＋其董監事，即舊版勾選 checkbox 的行為）
    #   "3" = 查到第三層（新功能，見 query_juristic_persons_for_directors() 與下方邏輯）
    query_depth = normalize_query_depth(request.form.get("query_depth"))

    if not is_valid_tax_id(tax_id):
        return render_template(
            "index.html",
            result=None,
            error="統一編號格式錯誤，請輸入 8 位數字（不含空白、字母或符號）。",
            query_value=display_value,
            query_depth_value=query_depth,
            print_date=None,
        )

    ok, result, err = query_company(tax_id)
    if not ok:
        return render_template(
            "index.html",
            result=None,
            error=err,
            query_value=display_value,
            query_depth_value=query_depth,
            print_date=None,
        )

    # 既有四大區塊查詢（基本資料／所營事業／董監事／經理人）已於 query_company() 完成，
    # 不受 query_depth 影響。query_depth=="1" 時完全不觸發任何法人反查的額外外部查詢，
    # 維持與「移除 checkbox 前、未勾選」的行為完全等價。
    if query_depth in ("2", "3"):
        # cache／call_counter 為本次 request 專屬（每個 /query 請求各自建立新的一份，
        # 不跨 request 共用），確保同一法人名稱在第二層、第三層間只實際查詢一次，且
        # 全域防禦性上限 MAX_JURISTIC_PERSON_LOOKUPS_TOTAL 是「整個本次請求」合計計算。
        cache: dict = {}
        call_counter = [0]

        second_layer = query_juristic_persons_for_directors(
            result["director_items"], cache, call_counter
        )

        if query_depth == "3":
            # 第三層：針對第二層中每一筆查到（status=="found"）的法人，取出該法人
            # 自身董監事清單（jp_result["data"]["director_items"]）中的「所代表法人」，
            # 比照第二層做法再查一次，結果放入該筆的 data["sub_juristic_persons"]。
            #
            # ★★★ 深度上限，務必維持 ★★★
            # 這裡是查詢深度的絕對終點。第三層查到的法人（third_layer 的結果）
            # 只用於顯示（基本資料＋其自身董監事清單），**絕對不可**再對第三層法人
            # 董監事清單中的「所代表法人」呼叫 query_juristic_persons_for_directors()
            # 或任何查詢函式——那樣會展開成第四層，違反本功能「深度上限為 3」的
            # 既定設計。未來如需修改查詢深度邏輯，請勿在此迴圈內或其下游新增任何
            # 進一步的遞迴查詢呼叫。
            for jp_result in second_layer["results"]:
                if jp_result["status"] == "found":
                    third_layer = query_juristic_persons_for_directors(
                        jp_result["data"]["director_items"], cache, call_counter
                    )
                    jp_result["data"]["sub_juristic_persons"] = third_layer
                    # 第三層到此為止：third_layer["results"] 中的法人，無論
                    # status 為何，皆不再往下查詢，僅供顯示。

        result["juristic_persons"] = second_layer

    # 「列印日期」：伺服器處理本次查詢當下的日期，動態產生（非固定文字），
    # 僅在有查詢結果時才顯示，比照 PDF 版面（查詢結果頁才有這行，首頁未查詢時不顯示）。
    return render_template(
        "index.html",
        result=result,
        error=None,
        query_value=display_value,
        query_depth_value=query_depth,
        print_date=format_print_date(),
    )


@app.errorhandler(404)
def not_found(_error):
    return render_template("404.html"), 404


@app.errorhandler(500)
def server_error(_error):
    # 不將原始例外堆疊顯示給使用者。
    return render_template(
        "index.html",
        result=None,
        error="系統發生未預期錯誤，請稍後再試。",
        query_value="",
        query_depth_value=DEFAULT_QUERY_DEPTH,
        print_date=None,
    ), 500


if __name__ == "__main__":
    # 僅供公司內部使用：綁定 127.0.0.1，不對外公開。
    app.run(debug=False, host="127.0.0.1", port=5050)
