"""Markdown → PDF для печати на листах A4: конспект A5 (2 на лист), шпоры A6 (4 на лист) и A7 (8 на лист).

Использование: python3 -m app.printing [--A6|--A7] konspekty/*.md
Результат: konspekty/print/<имя>_A4x<N>.pdf
"""
import asyncio
import re
import sys
from pathlib import Path

import markdown
from playwright.async_api import async_playwright
from pypdf import PageObject, PdfReader, PdfWriter, Transformation

from . import config

MM = 72 / 25.4  # пунктов в миллиметре

# Формат страницы → как раскладывать на листе A4: (ширина, высота страницы в мм, колонок, строк, лист альбомный?)
FORMATS = {
    "A5": (148, 210, 2, 1, True),   # 2 страницы на альбомном A4 — конспект
    "A6": (105, 148, 2, 2, False),  # 4 страницы на книжном A4 — шпора
    "A7": (74, 105, 4, 2, True),    # 8 страниц на альбомном A4 — мини-шпора
}
# Базовый размер шрифта и поля для каждого формата
SIZES = {"A5": (8.4, "9mm 8mm 11mm 8mm"), "A6": (6.6, "5mm 5mm 7mm 5mm"), "A7": (5.2, "3.5mm 3.5mm 5mm 3.5mm")}


def css(fmt: str) -> str:
    f, margin = SIZES[fmt]
    k = f / 8.4  # всё масштабируем от размеров A5
    pt = lambda x: f"{x * k:.2f}pt"  # noqa: E731
    return f"""
@page {{ size: {FORMATS[fmt][0]}mm {FORMATS[fmt][1]}mm; margin: {margin}; }}
* {{ box-sizing: border-box; }}
body {{ font-family: 'DejaVu Sans', 'Noto Color Emoji', sans-serif; font-size: {pt(8.4)};
       line-height: 1.3; color: #111; margin: 0; }}
h1 {{ font-size: {pt(12.5)}; margin: 0 0 {pt(4)}; line-height: 1.2; }}
h2 {{ font-size: {pt(10.5)}; margin: {pt(9)} 0 {pt(3)}; padding-bottom: {pt(1.5)}; border-bottom: {pt(1.2)} solid #333;
     break-after: avoid; }}
h3 {{ font-size: {pt(9.2)}; margin: {pt(7)} 0 {pt(2)}; break-after: avoid; }}
p {{ margin: {pt(2.5)} 0; }}
ul, ol {{ margin: {pt(2)} 0; padding-left: {pt(17)}; }}
li {{ margin: {pt(0.8)} 0; }}
blockquote {{ margin: {pt(4)} 0; padding: {pt(3)} {pt(6)}; border-left: {pt(2.2)} solid #888; background: #f2f2f2; }}
blockquote p {{ margin: {pt(1)} 0; }}
table {{ width: 100%; border-collapse: collapse; margin: {pt(3.5)} 0; font-size: {pt(7.6)}; line-height: 1.22; }}
th, td {{ border: 0.5pt solid #999; padding: {pt(1.8)} {pt(3)}; vertical-align: top; text-align: left; }}
th {{ background: #e4e4e4; }}
tr {{ break-inside: avoid; }}
pre {{ background: #f2f2f2; padding: {pt(3)} {pt(5)}; font-size: {pt(7.4)}; white-space: pre-wrap; margin: {pt(3)} 0;
      break-inside: avoid; }}
code {{ font-family: 'DejaVu Sans Mono', monospace; font-size: {pt(7.6)}; }}
hr {{ border: 0; border-top: 0.5pt dashed #aaa; margin: {pt(6)} 0; }}
del {{ color: #a00; }}
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
    in_code = False
    for line in md.splitlines():
        if line.lstrip().startswith("```"):
            in_code = not in_code
        elif not in_code and out and LIST_OR_TABLE.match(line) and out[-1].strip() \
                and not LIST_OR_TABLE.match(out[-1]):
            out.append("")
        out.append(line)
    return "\n".join(out)


def to_html(md_text: str, fmt: str = "A5") -> str:
    body = markdown.markdown(prepare(md_text),
                             extensions=["tables", "fenced_code", "sane_lists"])
    return f"<!doctype html><html lang='ru'><meta charset='utf-8'><style>{css(fmt)}</style><body>{body}</body></html>"


async def _browser(p):
    launch = {"executable_path": config.CHROMIUM_PATH} if config.CHROMIUM_PATH else {"channel": "chromium"}
    return await p.chromium.launch(**launch)


async def render_pages(html: str, out: Path, fmt: str) -> None:
    """HTML → PDF из страниц формата fmt (с номерами страниц внизу)."""
    foot = {"A5": 7, "A6": 5.5, "A7": 4.5}[fmt]
    async with async_playwright() as p:
        browser = await _browser(p)
        page = await browser.new_page()
        await page.set_content(html, wait_until="load")
        await page.pdf(path=str(out), prefer_css_page_size=True, display_header_footer=True,
                       header_template="<span></span>",
                       footer_template=f"<div style='font-size:{foot}pt;width:100%;text-align:center;color:#666'>"
                                       "<span class='pageNumber'></span> / <span class='totalPages'></span></div>")
        await browser.close()


async def _cut_lines(fmt: str, out: Path) -> None:
    """Лист A4 с пунктирными линиями отреза между страницами."""
    w, h, cols, rows, landscape = FORMATS[fmt]
    sw, sh = (297, 210) if landscape else (210, 297)
    lines = "".join(f"<div style='position:absolute;left:{c * w}mm;top:0;height:{sh}mm;border-left:0.3pt dashed #bbb'></div>"
                    for c in range(1, cols))
    lines += "".join(f"<div style='position:absolute;top:{r * h}mm;left:0;width:{sw}mm;border-top:0.3pt dashed #bbb'></div>"
                     for r in range(1, rows))
    html = f"<html><style>@page{{size:{sw}mm {sh}mm;margin:0}}body{{margin:0}}</style><body>{lines}</body></html>"
    async with async_playwright() as p:
        browser = await _browser(p)
        page = await browser.new_page()
        await page.set_content(html)
        await page.pdf(path=str(out), prefer_css_page_size=True)
        await browser.close()


def n_up(pages_pdf: Path, out: Path, fmt: str, guides: Path | None = None) -> int:
    """Раскладывает страницы формата fmt по листам A4 по порядку (слева направо, сверху вниз)."""
    w, h, cols, rows, landscape = FORMATS[fmt]
    sw, sh = ((297, 210) if landscape else (210, 297))
    pages = PdfReader(str(pages_pdf)).pages
    guide = PdfReader(str(guides)).pages[0] if guides else None
    per_sheet = cols * rows
    writer = PdfWriter()
    for i in range(0, len(pages), per_sheet):
        sheet = PageObject.create_blank_page(width=sw * MM, height=sh * MM)
        if guide is not None:
            sheet.merge_page(guide)
        for j, page in enumerate(pages[i:i + per_sheet]):
            col, row = j % cols, j // cols
            sx = w * MM / float(page.mediabox.width)
            sy = h * MM / float(page.mediabox.height)
            tx, ty = col * w * MM, (sh - (row + 1) * h) * MM
            sheet.merge_transformed_page(page, Transformation().scale(sx, sy).translate(tx=tx, ty=ty))
        writer.add_page(sheet)
    with open(out, "wb") as f:
        writer.write(f)
    return len(pages)


async def build_print_pdf(md_text: str, out: Path, fmt: str = "A5") -> int:
    """Markdown → PDF на листах A4: A5 — по 2, A6 — по 4, A7 — по 8 страниц на лист. Возвращает число страниц."""
    tmp = out.with_name(out.stem + f"_{fmt}_pages.pdf")
    guides = out.with_name(out.stem + "_guides.pdf") if fmt != "A5" else None
    await render_pages(to_html(md_text, fmt), tmp, fmt)
    if guides:
        await _cut_lines(fmt, guides)
    n = n_up(tmp, out, fmt, guides)
    tmp.unlink()
    if guides:
        guides.unlink()
    return n


def main() -> None:
    """python3 -m app.printing [--A6|--A7] файлы.md"""
    fmt = "A5"
    files = []
    for arg in sys.argv[1:]:
        if arg.startswith("--"):
            fmt = arg[2:].upper()
        else:
            files.append(Path(arg))
    per_sheet = FORMATS[fmt][2] * FORMATS[fmt][3]
    for src in files:
        out_dir = src.parent / "print"
        out_dir.mkdir(exist_ok=True)
        out = out_dir / f"{src.stem}_A4x{per_sheet}.pdf"
        n = asyncio.run(build_print_pdf(src.read_text(encoding="utf-8"), out, fmt))
        print(f"{src.name}: {n} стр. {fmt} → {-(-n // per_sheet)} лист(ов) A4 → {out}")


if __name__ == "__main__":
    main()
