# -*- coding: utf-8 -*-
"""Render 建置階段下載繁體中文 Noto CJK 字型供 Playwright 頁面直接載入。"""

import urllib.request
from pathlib import Path

FONT_URL = (
    "https://raw.githubusercontent.com/googlefonts/noto-cjk/main/"
    "Sans/OTF/TraditionalChinese/NotoSansCJKtc-Regular.otf"
)
PROJECT_DIR = Path(__file__).resolve().parent
FONT_DIR = PROJECT_DIR / "fonts"
FONT_PATH = FONT_DIR / "NotoSansCJKtc-Regular.otf"
MIN_FONT_SIZE = 10_000_000


def main() -> None:
    FONT_DIR.mkdir(parents=True, exist_ok=True)
    if not FONT_PATH.exists() or FONT_PATH.stat().st_size < MIN_FONT_SIZE:
        temp_path = FONT_PATH.with_suffix(".download")
        print("Downloading Noto Sans CJK TC font...", flush=True)
        with urllib.request.urlopen(FONT_URL, timeout=180) as response:
            with temp_path.open("wb") as output:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    output.write(chunk)
        if temp_path.stat().st_size < MIN_FONT_SIZE:
            temp_path.unlink(missing_ok=True)
            raise RuntimeError("下載的中文字型檔案大小異常")
        temp_path.replace(FONT_PATH)

    print(f"CJK font ready: {FONT_PATH} ({FONT_PATH.stat().st_size} bytes)", flush=True)


if __name__ == "__main__":
    main()
