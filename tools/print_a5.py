"""Конспект (.md) → PDF для печати: страницы A5, по две на листе A4 (альбомная ориентация).

Использование: python3 tools/print_a5.py konspekty/*.md
Результат: konspekty/print/<имя>_A4x2.pdf
"""
import asyncio
import re
import sys
from pathlib import Path

import markdown
from playwright.async_api import async_playwright
from pypdf import PageObject, PdfReader, PdfWriter, Transformation

CHROMIUM = "/opt/pw-browsers/chromium"
A4_W, A4_H = 841.89, 595.28  # A4 альбомная, в пунктах
A5_W = A4_W / 2

CSS = """
@page { size: A5; margin: 9mm 8mm 11mm 8mm; }
* { box-sizing: border-box; }
body { font-family: 'DejaVu Sans', 'Noto Color Emoji', sans-serif; font-size: 8.4pt;
       line-height: 1.32; color: #111; margin: 0; }
h1 { font-size: 12.5pt; margin: 0 0 4pt; line-height: 1.2; }
h1 + p, h1 + p + blockquote { margin-top: 2pt; }
h2 { font-size: 10.5pt; margin: 9pt 0 3pt; padding-bottom: 1.5pt; border-bottom: 1.2pt solid #333;
     break-after: avoid; }
h3 { font-size: 9.2pt; margin: 7pt 0 2pt; break-after: avoid; }
p { margin: 2.5pt 0; }
ul, ol { margin: 2pt 0; padding-left: 17pt; }
li { margin: 0.8pt 0; }
blockquote { margin: 4pt 0; padding: 3pt 6pt; border-left: 2.2pt solid #888; background: #f2f2f2; }
blockquote p { margin: 1pt 0; }
table { width: 100%; border-collapse: collapse; margin: 3.5pt 0; font-size: 7.6pt; line-height: 1.25; }
th, td { border: 0.5pt solid #999; padding: 1.8pt 3pt; vertical-align: top; text-align: left; }
th { background: #e4e4e4; }
tr { break-inside: avoid; }
pre { background: #f2f2f2; padding: 3pt 5pt; font-size: 7.4pt; white-space: pre-wrap; margin: 3pt 0;
      break-inside: avoid; }
code { font-family: 'DejaVu Sans Mono', monospace; font-size: 7.6pt; }
hr { border: 0; border-top: 0.5pt dashed #aaa; margin: 6pt 0; }
del { color: #a00; }
"""

LIST_OR_TABLE = re.compile(r"^\s*(- |\* |\d+\. |\|)")


def prepare(md: str) -> str:
    # раскрываем спойлер с ответами — на бумаге его не открыть
    md = re.sub(r"<details>\s*<summary>(.*?)</summary>", r"**\1**\n", md, flags=re.S)
    md = md.replace("</details>", "")
    md = md.replace("- [ ] ", "- ☐ ")
    md = re.sub(r"~~(.+?)~~", r"<del>\1</del>", md)
    # Python-Markdown требует пустую строку перед списком/таблицей после абзаца
    out = []
    for line in md.splitlines():
        if out and LIST_OR_TABLE.match(line) and out[-1].strip() and not LIST_OR_TABLE.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


def to_html(md_path: Path) -> str:
    body = markdown.markdown(prepare(md_path.read_text(encoding="utf-8")),
                             extensions=["tables", "fenced_code", "sane_lists"])
    return f"<!doctype html><html lang='ru'><meta charset='utf-8'><style>{CSS}</style><body>{body}</body></html>"


async def render_a5(html: str, out: Path) -> None:
    async with async_playwright() as p:
        browser = await p.chromium.launch(executable_path=CHROMIUM)
        page = await browser.new_page()
        await page.set_content(html, wait_until="load")
        await page.pdf(path=str(out), prefer_css_page_size=True, display_header_footer=True,
                       header_template="<span></span>",
                       footer_template="<div style='font-size:7pt;width:100%;text-align:center;color:#666'>"
                                       "<span class='pageNumber'></span> / <span class='totalPages'></span></div>")
        await browser.close()


def two_up(a5_pdf: Path, out: Path) -> int:
    pages = PdfReader(str(a5_pdf)).pages
    writer = PdfWriter()
    for i in range(0, len(pages), 2):
        sheet = PageObject.create_blank_page(width=A4_W, height=A4_H)
        for j, page in enumerate(pages[i:i + 2]):
            sx = A5_W / float(page.mediabox.width)
            sy = A4_H / float(page.mediabox.height)
            sheet.merge_transformed_page(page, Transformation().scale(sx, sy).translate(tx=j * A5_W))
        writer.add_page(sheet)
    with open(out, "wb") as f:
        writer.write(f)
    return len(pages)


def main() -> None:
    for arg in sys.argv[1:]:
        src = Path(arg)
        out_dir = src.parent / "print"
        out_dir.mkdir(exist_ok=True)
        a5 = out_dir / f"{src.stem}_A5.pdf"
        asyncio.run(render_a5(to_html(src), a5))
        n = two_up(a5, out_dir / f"{src.stem}_A4x2.pdf")
        a5.unlink()
        print(f"{src.name}: {n} стр. A5 → {(n + 1) // 2} лист(ов) A4")


if __name__ == "__main__":
    main()
