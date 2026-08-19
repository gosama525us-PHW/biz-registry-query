# -*- coding: utf-8 -*-
"""Render 建置階段安裝繁體中文 Noto CJK 字型，供 Playwright Chromium 使用。"""

import subprocess
import urllib.request
import os
from pathlib import Path

FONT_URL = (
    "https://raw.githubusercontent.com/googlefonts/noto-cjk/main/"
    "Sans/OTF/TraditionalChinese/NotoSansCJKtc-Regular.otf"
)
PROJECT_DIR = Path(__file__).resolve().parent
FONT_DIR = PROJECT_DIR / "fonts"
FONT_PATH = FONT_DIR / "NotoSansCJKtc-Regular.otf"
FONT_CONFIG_PATH = FONT_DIR / "fonts.conf"
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

    FONT_CONFIG_PATH.write_text(
        f"""<?xml version="1.0"?>
<!DOCTYPE fontconfig SYSTEM "fonts.dtd">
<fontconfig>
  <dir>{FONT_DIR}</dir>
  <cachedir>{FONT_DIR / 'cache'}</cachedir>
  <alias>
    <family>sans-serif</family>
    <prefer><family>Noto Sans CJK TC</family></prefer>
  </alias>
  <alias>
    <family>serif</family>
    <prefer><family>Noto Sans CJK TC</family></prefer>
  </alias>
</fontconfig>
""",
        encoding="utf-8",
    )
    font_env = os.environ.copy()
    font_env["FONTCONFIG_FILE"] = str(FONT_CONFIG_PATH)
    subprocess.run(["fc-cache", "-f"], check=True, env=font_env)
    matched = subprocess.run(
        ["fc-match", "-f", "%{family} | %{file}\n", "Noto Sans CJK TC"],
        check=True,
        capture_output=True,
        text=True,
        env=font_env,
    ).stdout.strip()
    if str(FONT_PATH) not in matched:
        raise RuntimeError(f"fontconfig 未找到繁體中文字型：{matched}")
    print(f"CJK font ready: {matched}", flush=True)


if __name__ == "__main__":
    main()
