"""
スクレイピング結果を、装飾・集計・グラフ付きのExcel帳票として出力する。
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.chart import BarChart, Reference

from .config import SiteConfig

HEADER_FILL = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
HEADER_FONT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial", size=10)
TITLE_FONT = Font(name="Arial", size=14, bold=True)
THIN = Side(style="thin", color="B7B7B7")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def export_report(cfg: SiteConfig, rows: list[dict], output_path: Path) -> Path:
    """
    rows: engine.scrape() が返す辞書のリスト（"__num__<列名>" キーに数値化済みの値が入る）
    """
    wb = Workbook()

    # ---------- Dataシート ----------
    ws = wb.active
    ws.title = "Data"

    field_names = [f.name for f in cfg.fields]
    numeric_fields = [f.name for f in cfg.fields if f.numeric]

    ws["A1"] = f"{cfg.name} 取得データ"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=max(len(field_names), 1))
    ws["A2"] = f"取得日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}　件数: {len(rows)}"
    ws["A2"].font = BODY_FONT

    header_row = 4
    for col_idx, name in enumerate(field_names, start=1):
        cell = ws.cell(row=header_row, column=col_idx, value=name)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = BORDER

    for r_idx, row in enumerate(rows, start=header_row + 1):
        for c_idx, name in enumerate(field_names, start=1):
            # 数値項目は数値化済みの値があればそちらを、なければ文字列を入れる
            value = row.get(f"__num__{name}")
            if value is None:
                value = row.get(name, "")
            cell = ws.cell(row=r_idx, column=c_idx, value=value)
            cell.font = BODY_FONT
            cell.border = BORDER

    last_data_row = header_row + len(rows)
    if rows:
        ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(field_names))}{last_data_row}"
    ws.freeze_panes = f"A{header_row + 1}"

    for col_idx, name in enumerate(field_names, start=1):
        max_len = max([len(str(name))] + [len(str(r.get(name, ""))) for r in rows]) if rows else len(name)
        ws.column_dimensions[get_column_letter(col_idx)].width = min(max(max_len + 2, 10), 50)

    # ---------- Summaryシート ----------
    ws2 = wb.create_sheet("Summary")
    ws2["A1"] = f"{cfg.name} 集計サマリー"
    ws2["A1"].font = TITLE_FONT

    ws2["A3"] = "総件数"
    ws2["A3"].font = Font(name="Arial", bold=True)
    if field_names:
        first_col = get_column_letter(1)
        ws2["B3"] = f"=COUNTA(Data!{first_col}{header_row + 1}:{first_col}{max(last_data_row, header_row + 1)})"
    else:
        ws2["B3"] = 0
    ws2["B3"].font = BODY_FONT

    summary_start_row = 5
    if numeric_fields and rows:
        headers = ["項目", "件数", "合計", "平均", "最大", "最小"]
        for c_idx, h in enumerate(headers, start=1):
            cell = ws2.cell(row=summary_start_row, column=c_idx, value=h)
            cell.font = HEADER_FONT
            cell.fill = HEADER_FILL
            cell.border = BORDER

        for i, name in enumerate(numeric_fields):
            col_idx = field_names.index(name) + 1
            col_letter = get_column_letter(col_idx)
            data_range = f"Data!{col_letter}{header_row + 1}:{col_letter}{last_data_row}"
            r = summary_start_row + 1 + i
            ws2.cell(row=r, column=1, value=name).font = BODY_FONT
            ws2.cell(row=r, column=2, value=f"=COUNT({data_range})").font = BODY_FONT
            ws2.cell(row=r, column=3, value=f"=SUM({data_range})").font = BODY_FONT
            ws2.cell(row=r, column=4, value=f"=IFERROR(AVERAGE({data_range}),0)").font = BODY_FONT
            ws2.cell(row=r, column=5, value=f"=IFERROR(MAX({data_range}),0)").font = BODY_FONT
            ws2.cell(row=r, column=6, value=f"=IFERROR(MIN({data_range}),0)").font = BODY_FONT
            for c in range(1, 7):
                ws2.cell(row=r, column=c).border = BORDER

        # ---------- グラフ（1つ目の数値項目の分布を棒グラフで） ----------
        chart_field = numeric_fields[0]
        chart_col_idx = field_names.index(chart_field) + 1
        chart_col_letter = get_column_letter(chart_col_idx)

        # ラベル列（先頭のテキスト項目があればそれを、なければ行番号を使う）
        label_field_idx = 1
        for i, f in enumerate(field_names):
            if f not in numeric_fields:
                label_field_idx = i + 1
                break

        chart = BarChart()
        chart.title = f"{chart_field} の分布"
        chart.y_axis.title = chart_field
        chart.x_axis.title = field_names[label_field_idx - 1]

        max_points = min(len(rows), 30)  # 見やすさのため上位30件まで
        data_ref = Reference(
            ws, min_col=chart_col_idx, min_row=header_row,
            max_row=header_row + max_points,
        )
        cats_ref = Reference(
            ws, min_col=label_field_idx, min_row=header_row + 1,
            max_row=header_row + max_points,
        )
        chart.add_data(data_ref, titles_from_data=True)
        chart.set_categories(cats_ref)
        chart.width = 24
        chart.height = 12
        ws2.add_chart(chart, f"A{summary_start_row + len(numeric_fields) + 3}")
    else:
        ws2["A5"] = "数値項目が設定されていないため、集計・グラフはありません。"
        ws2["A5"].font = BODY_FONT

    ws2.column_dimensions["A"].width = 20
    for col in "BCDEF":
        ws2.column_dimensions[col].width = 14

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path
