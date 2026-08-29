"""
検索・抽出結果を、すっきりした一覧形式のExcel帳票として出力する。
画像はサムネイルとして埋め込み、動画はURLへのリンクとして出力する
（どちらも、キーワードに一致したテキストの塊の中で見つかったものだけ）。
"""
from __future__ import annotations

import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter
from openpyxl.drawing.image import Image as XLImage

from .config import SearchConfig

HEADER_FILL = PatternFill(start_color="305496", end_color="305496", fill_type="solid")
HEADER_FONT = Font(name="Arial", size=11, bold=True, color="FFFFFF")
BODY_FONT = Font(name="Arial", size=10)
LINK_FONT = Font(name="Arial", size=10, color="0563C1", underline="single")
TITLE_FONT = Font(name="Arial", size=14, bold=True)
SUBTITLE_FONT = Font(name="Arial", size=10, color="595959")
THIN = Side(style="thin", color="B7B7B7")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP_TOP_LEFT = Alignment(horizontal="left", vertical="top", wrap_text=True)

COLUMNS = ["キーワード", "抜粋テキスト", "画像", "動画URL", "ページタイトル", "URL", "取得日時"]
ROW_KEYS = ["keyword", "text", "image_url", "video_url", "title", "url", "fetched_at"]
COLUMN_WIDTHS = [16, 60, 18, 40, 30, 40, 18]

IMAGE_COL_IDX = ROW_KEYS.index("image_url") + 1
VIDEO_COL_IDX = ROW_KEYS.index("video_url") + 1
URL_COL_IDX = ROW_KEYS.index("url") + 1

THUMBNAIL_MAX_SIZE = (120, 90)
IMAGE_ROW_HEIGHT = 70
DOWNLOAD_TIMEOUT_SEC = 8


def _download_thumbnail(url: str) -> XLImage | None:
    """画像URLをダウンロードし、サムネイルサイズに縮小したExcel埋め込み用画像を返す。
    失敗した場合は None を返す（呼び出し側はURLをそのまま表示するなどでフォールバックする）。
    """
    if not url:
        return None
    try:
        from PIL import Image as PILImage
    except ImportError:
        return None

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            data = resp.read()
        pil_img = PILImage.open(BytesIO(data))
        pil_img.load()
        if pil_img.mode not in ("RGB", "RGBA"):
            pil_img = pil_img.convert("RGBA")
        pil_img.thumbnail(THUMBNAIL_MAX_SIZE)
        buf = BytesIO()
        pil_img.save(buf, format="PNG")
        buf.seek(0)
        return XLImage(buf)
    except Exception:
        return None


def export_report(cfg: SearchConfig, rows: list[dict], output_path: Path) -> Path:
    """
    rows: engine.scrape() が返す辞書のリスト
          （keyword / text / image_url / video_url / title / url / fetched_at の各キーを持つ）
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
            if key == "image_url":
                continue  # 画像列は下でサムネイル埋め込み/フォールバックを行う
            cell = ws.cell(row=r_idx, column=c_idx, value=row.get(key, ""))
            cell.font = BODY_FONT
            cell.border = BORDER
            cell.alignment = WRAP_TOP_LEFT

        image_url = row.get("image_url", "")
        image_cell = ws.cell(row=r_idx, column=IMAGE_COL_IDX)
        image_cell.border = BORDER
        if image_url:
            thumb = _download_thumbnail(image_url)
            if thumb is not None:
                thumb.anchor = f"{get_column_letter(IMAGE_COL_IDX)}{r_idx}"
                ws.add_image(thumb)
                ws.row_dimensions[r_idx].height = IMAGE_ROW_HEIGHT
            else:
                # ダウンロード/埋め込みに失敗した場合は、URLへのリンクとして残す
                image_cell.value = image_url
                image_cell.hyperlink = image_url
                image_cell.font = LINK_FONT
                image_cell.alignment = WRAP_TOP_LEFT

        video_cell = ws.cell(row=r_idx, column=VIDEO_COL_IDX)
        if video_cell.value:
            video_cell.hyperlink = video_cell.value
            video_cell.font = LINK_FONT

        url_cell = ws.cell(row=r_idx, column=URL_COL_IDX)
        if url_cell.value:
            url_cell.hyperlink = url_cell.value
            url_cell.font = LINK_FONT

    last_data_row = header_row + len(rows)
    if rows:
        ws.auto_filter.ref = f"A{header_row}:{get_column_letter(len(COLUMNS))}{last_data_row}"
    ws.freeze_panes = f"A{header_row + 1}"

    for col_idx, width in enumerate(COLUMN_WIDTHS, start=1):
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    output_path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(output_path)
    return output_path
