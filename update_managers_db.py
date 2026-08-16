# -*- coding: utf-8 -*-
"""
工商登記查詢系統 — 經理人本地資料庫更新腳本

用途：
  重新下載經濟部商業發展署「公司登記經理人資料集」整批 CSV（官方每月更新，
  約 8.3 萬筆），並重建本地 SQLite 資料庫 data/managers.db，供 app.py 在
  查詢統編時一併查詢經理人清單使用。

重要（請務必先讀完再執行）：
  1. 本腳本為**手動執行**腳本，不做成排程或背景常駐服務。建議配合官方
     「每月更新」的頻率，由系統管理員每月手動執行一次即可，執行前後
     不需要、也不應該讓本腳本自動在背景反覆執行。
  2. 官方資料集頁面
     https://data.gcis.nat.gov.tw/od/detail?oid=8DD4599A-A76B-4290-B64D-7AC8DA3CE99F
     本身以 JavaScript 觸發下載（按鈕 onclick 呼叫 showDialog()），頁面上
     沒有可直接看到的靜態下載連結。經 developer 於 2026-08-12 直接讀取該
     頁面原始碼（HTML），在 onclick 屬性中找到實際觸發下載的網址：
       https://data.gcis.nat.gov.tw/od/file?oid=4F0BBF29-E4D6-4A8B-99F9-92A1642AA080
     並以此網址直接發出 GET 請求，實測下載成功（檔案約 5.4MB，UTF-8 with
     BOM 編碼，79,615 筆資料列，含 header 共 79,616 行，欄位與官方頁面
     說明的「統一編號、公司名稱、經理人姓名、到職日期、備註」完全相符）。
     若日後官方調整此頁面結構或更換 oid，此下載連結可能會失效並導致本
     腳本下載失敗，屆時需重新至上述資料集頁面另行查看原始碼，找出新的
     /od/file?oid=... 下載連結後更新 CSV_DOWNLOAD_URL 常數。
  3. 官方原始檔案本身不含「資料所屬月份」欄位，本次實測 HTTP 回應標頭
     （Content-Disposition / Content-Length / Content-Type / Date /
     Server 等）亦未提供 Last-Modified 或任何可判讀「資料製作月份」的
     欄位。因此本腳本以「本次下載並成功建置資料庫的時間」作為資料時效
     的參考基準，寫入資料庫 meta 表，供 app.py 前端如實顯示為「本地資料
     庫最後建置／更新時間」，而非官方公告的精確資料月份，避免誤導使用者
     以為是官方標示的資料月份。

執行方式：
  python update_managers_db.py

執行後會印出：
  - 下載檔案大小
  - 更新完成筆數（例如「更新完成，共 83,xxx 筆」）
  - 本地資料庫建置／更新時間
"""
import csv
import io
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

import requests

# 實際 CSV 下載連結（取得方式詳見上方模組說明第 2 點）。
CSV_DOWNLOAD_URL = "https://data.gcis.nat.gov.tw/od/file?oid=4F0BBF29-E4D6-4A8B-99F9-92A1642AA080"

DB_PATH = Path(__file__).resolve().parent / "data" / "managers.db"
REQUEST_TIMEOUT_SECONDS = 60

EXPECTED_HEADER = ["統一編號", "公司名稱", "經理人姓名", "到職日期", "備註"]


def download_csv() -> bytes:
    """下載官方經理人資料集 CSV，回傳原始 bytes。"""
    print(f"正在下載經理人資料集 CSV：{CSV_DOWNLOAD_URL}")
    resp = requests.get(CSV_DOWNLOAD_URL, timeout=REQUEST_TIMEOUT_SECONDS)
    resp.raise_for_status()
    if not resp.content:
        raise RuntimeError("下載失敗：回應內容為空，請確認官方下載連結是否仍然有效。")
    print(f"下載完成，檔案大小：{len(resp.content):,} bytes")
    return resp.content


def parse_csv(raw_bytes: bytes):
    """
    解析 CSV bytes 為 (tax_id, company_name, manager_name, onboard_date, note) 的 tuple 清單。

    官方檔案實測為 UTF-8 with BOM 編碼，因此以 utf-8-sig 解碼（可自動去除 BOM）。
    """
    text = raw_bytes.decode("utf-8-sig")
    reader = csv.reader(io.StringIO(text))

    try:
        header = next(reader)
    except StopIteration:
        return []

    if header != EXPECTED_HEADER:
        print(
            f"警告：CSV 標題列與預期不同。預期：{EXPECTED_HEADER}，實際：{header}，"
            "仍嘗試依欄位順序（統一編號、公司名稱、經理人姓名、到職日期、備註）解析。",
            file=sys.stderr,
        )

    rows = []
    skipped = 0
    for line_no, row in enumerate(reader, start=2):
        if len(row) < 5:
            skipped += 1
            continue
        tax_id, company_name, manager_name, onboard_date, note = row[:5]
        rows.append(
            (
                tax_id.strip(),
                company_name.strip(),
                manager_name.strip(),
                onboard_date.strip(),
                note.strip(),
            )
        )
    if skipped:
        print(f"警告：共 {skipped} 列欄位數不足，已略過。", file=sys.stderr)
    return rows


def rebuild_database(rows) -> None:
    """
    以 rows 重建資料庫。採「先寫入暫存檔、成功後才原子性取代正式檔」的方式，
    避免更新過程中若發生錯誤，導致正式使用中的 managers.db 損毀或處於不完整狀態。
    """
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)

    tmp_path = DB_PATH.with_suffix(".db.tmp")
    if tmp_path.exists():
        tmp_path.unlink()

    conn = sqlite3.connect(str(tmp_path))
    try:
        cur = conn.cursor()
        cur.execute(
            """
            CREATE TABLE managers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tax_id TEXT NOT NULL,
                company_name TEXT,
                manager_name TEXT,
                onboard_date TEXT,
                note TEXT
            )
            """
        )
        cur.execute("CREATE INDEX idx_managers_tax_id ON managers (tax_id)")

        cur.executemany(
            "INSERT INTO managers (tax_id, company_name, manager_name, onboard_date, note) "
            "VALUES (?, ?, ?, ?, ?)",
            rows,
        )

        cur.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
        cur.executemany(
            "INSERT INTO meta (key, value) VALUES (?, ?)",
            [
                ("updated_at", updated_at),
                ("source_url", CSV_DOWNLOAD_URL),
                ("row_count", str(len(rows))),
            ],
        )

        conn.commit()
    finally:
        conn.close()

    # 原子性取代舊資料庫檔案，查詢端不會讀到重建過程中的不完整資料庫。
    tmp_path.replace(DB_PATH)


def main():
    try:
        raw_bytes = download_csv()
    except requests.exceptions.Timeout:
        print("下載失敗：連線逾時，請確認網路狀況後重試。", file=sys.stderr)
        sys.exit(1)
    except requests.exceptions.RequestException as exc:
        print(f"下載失敗：無法連線至官方資料平臺（{exc}）", file=sys.stderr)
        sys.exit(1)
    except RuntimeError as exc:
        print(f"下載失敗：{exc}", file=sys.stderr)
        sys.exit(1)

    rows = parse_csv(raw_bytes)
    if not rows:
        print(
            "下載的 CSV 未解析出任何有效資料列，取消更新資料庫，"
            "保留現有 managers.db（若存在）。",
            file=sys.stderr,
        )
        sys.exit(1)

    rebuild_database(rows)

    updated_at = datetime.now().strftime("%Y-%m-%d %H:%M")
    print(f"更新完成，共 {len(rows):,} 筆經理人資料。")
    print(f"資料庫路徑：{DB_PATH}")
    print(f"本地資料庫建置／更新時間：{updated_at}")
    print(
        "備註：官方原始檔案未提供明確的「資料所屬月份」標示，以上時間為本次下載並"
        "建置資料庫的時間，僅供資料時效參考；如需最新資料請洽公司登記機關，"
        "或配合官方每月更新頻率重新執行本腳本。"
    )


if __name__ == "__main__":
    main()
