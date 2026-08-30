"""
検索・抽出結果を、抽出キーワードを列とする表形式のPowerPointスライドとして
出力する。構成: 表紙スライド → 表スライド（1ページ5行、5行を超える分は
複数スライドに分割）。各行は抽出キーワードすべてを含む(AND条件)1件の
レコードで、列の値は該当キーワードを含む文・段落そのもの。
"""
from __future__ import annotations

import math
from datetime import datetime
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.util import Inches, Pt

from .config import SearchConfig

SLIDE_WIDTH = Inches(13.333)
SLIDE_HEIGHT = Inches(7.5)

ACCENT_COLOR = RGBColor(0x30, 0x54, 0x96)
HEADER_TEXT_COLOR = RGBColor(0xFF, 0xFF, 0xFF)
ROW_BG_COLOR = RGBColor(0xFF, 0xFF, 0xFF)
ROW_BG_ALT_COLOR = RGBColor(0xF0, 0xF3, 0xFA)
TEXT_COLOR = RGBColor(0x2B, 0x2B, 0x2B)
MUTED_COLOR = RGBColor(0x6B, 0x6B, 0x6B)
LINK_COLOR = RGBColor(0x1A, 0x4B, 0x9E)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

ROWS_PER_SLIDE = 5  # 1ページあたりの表示行数（ヘッダー行は別）


def _add_solid_rect(slide, rgb: RGBColor):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SLIDE_WIDTH, SLIDE_HEIGHT)
    shape.line.fill.background()
    shape.shadow.inherit = False
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb
    return shape


def _add_title_slide(prs: Presentation, cfg: SearchConfig, count: int) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 白紙レイアウト
    _add_solid_rect(slide, ACCENT_COLOR)

    title_box = slide.shapes.add_textbox(Inches(1), Inches(2.6), SLIDE_WIDTH - Inches(2), Inches(1.5))
    tf = title_box.text_frame
    tf.word_wrap = True
    tf.text = cfg.name or "検索結果"
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.font.size = Pt(40)
    p.font.bold = True
    p.font.color.rgb = WHITE

    info_box = slide.shapes.add_textbox(Inches(1), Inches(4.3), SLIDE_WIDTH - Inches(2), Inches(2))
    tf = info_box.text_frame
    tf.word_wrap = True
    lines = [
        f"検索キーワード: {cfg.search_keyword}",
        f"抽出キーワード（列）: {', '.join(cfg.extract_keywords)}",
        f"件数: {count}件　|　作成日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.alignment = PP_ALIGN.CENTER
        p.font.size = Pt(16)
        p.font.color.rgb = RGBColor(0xD7, 0xDF, 0xF2)
        p.space_after = Pt(8)


def _set_cell_text(cell, text: str, *, size: int, color: RGBColor, bold: bool = False) -> None:
    tf = cell.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP
    tf.text = text
    p = tf.paragraphs[0]
    p.font.size = Pt(size)
    p.font.bold = bold
    p.font.color.rgb = color


def _add_source_cell(cell, title: str, url: str, video_url: str, fetched_at: str) -> None:
    tf = cell.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.TOP

    p = tf.paragraphs[0]
    p.text = title
    p.font.size = Pt(10)
    p.font.bold = True
    p.font.color.rgb = TEXT_COLOR

    if url:
        p_url = tf.add_paragraph()
        run = p_url.add_run()
        run.text = url
        run.font.size = Pt(9)
        run.font.color.rgb = LINK_COLOR
        run.font.underline = True
        run.hyperlink.address = url

    if video_url:
        p_video = tf.add_paragraph()
        run = p_video.add_run()
        run.text = "[動画を見る]"
        run.font.size = Pt(9)
        run.font.color.rgb = LINK_COLOR
        run.font.underline = True
        run.hyperlink.address = video_url

    p_date = tf.add_paragraph()
    p_date.text = fetched_at
    p_date.font.size = Pt(8)
    p_date.font.color.rgb = MUTED_COLOR


def _add_table_slide(
    prs: Presentation,
    cfg: SearchConfig,
    rows_chunk: list[dict],
    page_num: int,
    total_pages: int,
) -> None:
    """抽出キーワードを列とする表を1枚のスライドに描画する（最大 ROWS_PER_SLIDE 行）。
    セル内のテキストは折り返し表示にする（複数行になった場合も枠内に収める）。
    """
    slide = prs.slides.add_slide(prs.slide_layouts[6])
    _add_solid_rect(slide, RGBColor(0xFA, 0xFB, 0xFD))

    title_box = slide.shapes.add_textbox(Inches(0.5), Inches(0.25), Inches(10), Inches(0.6))
    tf = title_box.text_frame
    tf.text = cfg.name or "検索結果"
    tf.paragraphs[0].font.size = Pt(20)
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.color.rgb = ACCENT_COLOR

    page_box = slide.shapes.add_textbox(SLIDE_WIDTH - Inches(1.8), Inches(0.3), Inches(1.3), Inches(0.4))
    page_box.text_frame.text = f"{page_num}/{total_pages}"
    page_box.text_frame.paragraphs[0].font.size = Pt(12)
    page_box.text_frame.paragraphs[0].font.color.rgb = MUTED_COLOR
    page_box.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT

    headers = list(cfg.extract_keywords) + ["出典"]
    n_cols = len(headers)
    n_rows_data = len(rows_chunk)
    n_rows = n_rows_data + 1  # ヘッダー行を含む

    table_left = Inches(0.5)
    table_top = Inches(1.1)
    table_width = SLIDE_WIDTH - Inches(1.0)
    table_height = SLIDE_HEIGHT - Inches(1.4)

    graphic_frame = slide.shapes.add_table(n_rows, n_cols, table_left, table_top, table_width, table_height)
    table = graphic_frame.table
    table.first_row = False
    table.horz_banding = False

    # 列幅: 「出典」列は少し広めに、残りをキーワード列で均等割りする
    source_col_width = Inches(2.6)
    keyword_col_width = int((table_width - source_col_width) / max(1, n_cols - 1))
    for c in range(n_cols - 1):
        table.columns[c].width = keyword_col_width
    table.columns[n_cols - 1].width = table_width - keyword_col_width * (n_cols - 1)

    header_row_height = Inches(0.5)
    data_row_height = int((table_height - header_row_height) / max(1, n_rows_data))
    table.rows[0].height = header_row_height
    for r in range(1, n_rows):
        table.rows[r].height = data_row_height

    for c, header in enumerate(headers):
        cell = table.cell(0, c)
        cell.fill.solid()
        cell.fill.fore_color.rgb = ACCENT_COLOR
        _set_cell_text(cell, header, size=13, color=HEADER_TEXT_COLOR, bold=True)

    for r, row in enumerate(rows_chunk, start=1):
        bg = ROW_BG_COLOR if r % 2 == 1 else ROW_BG_ALT_COLOR
        for c, kw in enumerate(cfg.extract_keywords):
            cell = table.cell(r, c)
            cell.fill.solid()
            cell.fill.fore_color.rgb = bg
            _set_cell_text(cell, row["columns"].get(kw, ""), size=12, color=TEXT_COLOR)

        source_cell = table.cell(r, n_cols - 1)
        source_cell.fill.solid()
        source_cell.fill.fore_color.rgb = bg
        _add_source_cell(
            source_cell,
            title=row.get("title", ""),
            url=row.get("url", ""),
            video_url=row.get("video_url", ""),
            fetched_at=row.get("fetched_at", ""),
        )


def export_report(cfg: SearchConfig, rows: list[dict], output_path: Path) -> Path:
    """
    rows: engine.scrape() が返す辞書のリスト
          （columns: {キーワード: 値, ...} / image_url / video_url / title / url /
          fetched_at の各キーを持つ）
    表紙 → 表スライド（1ページ ROWS_PER_SLIDE 行）の順に構成する。
    """
    prs = Presentation()
    prs.slide_width = SLIDE_WIDTH
    prs.slide_height = SLIDE_HEIGHT

    _add_title_slide(prs, cfg, len(rows))

    total_pages = max(1, math.ceil(len(rows) / ROWS_PER_SLIDE))
    for page_num in range(1, total_pages + 1):
        start = (page_num - 1) * ROWS_PER_SLIDE
        chunk = rows[start:start + ROWS_PER_SLIDE]
        if not chunk:
            continue
        _add_table_slide(prs, cfg, chunk, page_num, total_pages)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(output_path)
    return output_path
