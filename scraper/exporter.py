"""
検索・抽出結果を、すっきりした一覧形式のExcel帳票として出力する。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

from .config import SearchConfig

HEADER_FILL = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
HEADER_FONT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial", size=10)
TITLE_FONT = Font(name="Arial", size=14, bold=True)
SUBTITLE_FONT = Font(name="Arial", size=10, color="595959")
THIN = Side(style="thin", color="B7B7B7")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP_TOP_LEFT = Alignment(horizontal="left", vertical="top", wrap_text=True)

COLUMNS = ["キーワード", "抜粋テキスト", "ページタイトル", "URL", "取得日時"]
ROW_KEYS = ["keyword", "text", "title", "url", "fetched_at"]
COLUMN_WIDTHS = [16, 70, 30, 45, 18]


def export_report(cfg: SearchConfig, rows: list[dict], output_path: Path) -> Path:
    """
    rows: engine.scrape() が返す辞書のリスト
          （keyword / text / title / url / fetched_at の各キーを持つ）
    """
    wb = Workbook()
    ws = wb.active
    ws.title = "結果"

    ws["A1"] = f"{cfg.name}" if cfg.name else "検索結果"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=len(COLUMNS))

    ws["A2"] = (
        f"検索キーワード: {cfg.search_keyword}　"
        f"抽出キーワード: {', '.join(cfg.extract_keywords)}　"
        f"件数: {len(rows)}　"
        f"取得日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
    ws["A2"].font = SUBTITLE_FONT
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=len(COLUMNS))

    header_row = 4
    for col_idx, name in enumerate(COLUMNS, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER

    for r_idx, row in enumerate(rows, start=header_row + 1):
        for c_idx, key in enumerate(ROW_KEYS, start=1):
            cell = ws.cell(row=r_idx, column=c_idx, value=row.get(key, ""))
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = WRAP_TOP_LEFT
        url_cell = ws.cell(row=r_idx, column=ROW_KEYS.index("url") + 1)
        if url_cell.value:
            url_cell.hyperlink = url_cell.value
            url_cell.font = Font(name="Arial", size=10, color="0563C1", underline="single")

    last_data_row = header_row + len(rows)
    if rows:
        ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(COLUMNS))}{last_data_row}"
    ws.freeze_panes = f"A{header_row + 1}"

    for col_idx, width in enumerate(COLUMN_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path
