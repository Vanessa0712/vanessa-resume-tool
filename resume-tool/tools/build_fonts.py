"""构建内嵌中文字体子集（Noto Serif SC → 仅保留界面用字 + GB2312 一级常用字）.

用法：python tools/build_fonts.py
产物：static/fonts/rt-serif-400.woff2 / rt-serif-600.woff2
说明：字体为 Noto Serif SC（SIL OFL 1.1），仅做字形子集化，未修改字形。
"""
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from fontTools import subset
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "static" / "fonts"
SRC = "https://cdn.jsdelivr.net/npm/@fontsource/noto-serif-sc@5.0.0/files/noto-serif-sc-chinese-simplified-{w}-normal.woff2"
WEIGHTS = (400, 600)


def gb2312_chars() -> str:
    """GB2312 全部汉字（一级 3755 + 二级 3008）——覆盖生僻姓名用字."""
    chars = []
    for hi in range(0xB0, 0xF8):
        for lo in range(0xA1, 0xFF):
            try:
                chars.append(bytes([hi, lo]).decode("gb2312"))
            except UnicodeDecodeError:
                pass
    return "".join(chars)


def ui_chars() -> str:
    """界面文案里出现的所有字符（含数字、标点、英文）."""
    buf = []
    for pattern in ("templates/*.html", "*.py", "static/*.js", "config/templates/*.json"):
        for f in ROOT.glob(pattern):
            buf.append(f.read_text(encoding="utf-8"))
    return "".join(buf)


def build() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    chars = set(ui_chars()) | set(gb2312_chars())
    cjk = {c for c in chars if "\u4e00" <= c <= "\u9fff" or "\u3400" <= c <= "\u4dbf"}
    text = "".join(sorted(chars | cjk))
    print(f"子集字符数: {len(text)}（其中汉字 {len(cjk)}）")

    for w in WEIGHTS:
        if True:
            src = OUT / f"_src-{w}.woff2"
            urllib.request.urlretrieve(SRC.format(w=w), src)
            dest = OUT / f"rt-serif-{w}.woff2"
            opts = subset.Options()
            opts.flavor = "woff2"
            opts.desubroutinize = True
            opts.layout_features = ["*"]
            opts.name_IDs = ["*"]
            opts.notdef_outline = True
            font = subset.load_font(str(src), opts)
            sub = subset.Subsetter(options=opts)
            sub.populate(text=text)
            sub.subset(font)
            subset.save_font(font, str(dest), opts)

            # 覆盖率自检：汉字必须全部可用（符号/箭头允许回退到系统字体）
            check = TTFont(str(dest))
            cmap = check.getBestCmap()
            missing_cjk = [c for c in ui_chars()
                           if "\u4e00" <= c <= "\u9fff" and ord(c) not in cmap]
            print(f"  {w}: {dest.stat().st_size/1024:.0f} KB · 缺失汉字 {len(missing_cjk)} 个"
                  + (f" → {''.join(missing_cjk[:20])}" if missing_cjk else " · 覆盖完整"))
            check.close()
            font.close()
            src.unlink(missing_ok=True)
            if missing_cjk:
                return 1
    return 0


if __name__ == "__main__":
    sys.exit(build())
