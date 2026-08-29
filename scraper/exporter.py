"""
検索・抽出結果を、1件1スライドの要約されたPowerPointスライドとして出力する。
画像はスライドに埋め込み、動画・出典ページはリンクとして掲載する
（どちらも、キーワードに一致したテキストの塊の中で見つかったものだけ）。
"""
from __future__ import annotations

import urllib.request
from datetime import datetime
from io import BytesIO
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import PP_ALIGN
from pptx.util import Inches, Pt

from .config import SearchConfig

SLIDE_WIDTH = Inches(13.333)
SLIDE_HEIGHT = Inches(7.5)

ACCENT_COLOR = RGBColor(0x30, 0x54, 0x96)
TEXT_COLOR = RGBColor(0x33, 0x33, 0x33)
MUTED_COLOR = RGBColor(0x77, 0x77, 0x77)
LINK_COLOR = RGBColor(0x05, 0x63, 0xC1)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

IMAGE_MAX_WIDTH = Inches(4.2)
IMAGE_MAX_HEIGHT = Inches(4.2)
DOWNLOAD_TIMEOUT_SEC = 8


def _download_image_bytes(url: str) -> BytesIO | None:
    """画像URLをダウンロードする。失敗した場合は None を返す
    （呼び出し側は画像なしでスライドを作る等でフォールバックする）。
    """
    if not url:
        return None
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT_SEC) as resp:
            data = resp.read()
        return BytesIO(data)
    except Exception:
        return None


def _add_title_slide(prs: Presentation, cfg: SearchConfig, count: int) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 白紙レイアウト

    bar = slide.shapes.add_shape(1, 0, 0, SLIDE_WIDTH, Inches(1.8))  # MSO_SHAPE.RECTANGLE = 1
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT_COLOR
    bar.line.fill.background()

    title_box = slide.shapes.add_textbox(Inches(0.6), Inches(0.4), Inches(12), Inches(1))
    tf = title_box.text_frame
    tf.text = cfg.name or "検索結果"
    tf.paragraphs[0].font.size = Pt(32)
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.color.rgb = WHITE

    info_box = slide.shapes.add_textbox(Inches(0.6), Inches(2.2), Inches(12), Inches(3))
    tf = info_box.text_frame
    tf.word_wrap = True
    lines = [
        f"検索キーワード: {cfg.search_keyword}",
        f"抽出キーワード: {', '.join(cfg.extract_keywords)}",
        f"件数: {count}件",
        f"作成日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.font.size = Pt(18)
        p.font.color.rgb = TEXT_COLOR
        p.space_after = Pt(10)


def _add_result_slide(prs: Presentation, row: dict, index: int, total: int) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 白紙レイアウト

    bar = slide.shapes.add_shape(1, 0, 0, SLIDE_WIDTH, Inches(0.9))
    bar.fill.solid()
    bar.fill.fore_color.rgb = ACCENT_COLOR
    bar.line.fill.background()

    title_box = slide.shapes.add_textbox(Inches(0.5), Inches(0.12), Inches(11.5), Inches(0.7))
    tf = title_box.text_frame
    tf.word_wrap = True
    tf.text = row.get("keyword", "")
    tf.paragraphs[0].font.size = Pt(24)
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.color.rgb = WHITE

    page_box = slide.shapes.add_textbox(SLIDE_WIDTH - Inches(1.6), Inches(0.3), Inches(1.2), Inches(0.4))
    page_box.text_frame.text = f"{index}/{total}"
    page_box.text_frame.paragraphs[0].font.size = Pt(12)
    page_box.text_frame.paragraphs[0].font.color.rgb = WHITE
    page_box.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT

    image_stream = _download_image_bytes(row.get("image_url", ""))
    text_width = Inches(12.3) if image_stream is None else Inches(7.6)

    body_box = slide.shapes.add_textbox(Inches(0.5), Inches(1.2), text_width, Inches(5))
    tf = body_box.text_frame
    tf.word_wrap = True
    tf.text = row.get("text", "")
    tf.paragraphs[0].font.size = Pt(18)
    tf.paragraphs[0].font.color.rgb = TEXT_COLOR

    if image_stream is not None:
        try:
            pic = slide.shapes.add_picture(image_stream, Inches(8.3), Inches(1.2), height=IMAGE_MAX_HEIGHT)
            if pic.width > IMAGE_MAX_WIDTH:
                ratio = IMAGE_MAX_WIDTH / pic.width
                pic.width = IMAGE_MAX_WIDTH
                pic.height = int(pic.height * ratio)
        except Exception:
            pass

    footer_box = slide.shapes.add_textbox(Inches(0.5), Inches(6.55), Inches(12.3), Inches(0.8))
    tf = footer_box.text_frame
    tf.word_wrap = True

    title = row.get("title", "")
    url = row.get("url", "")
    video_url = row.get("video_url", "")
    fetched_at = row.get("fetched_at", "")

    p = tf.paragraphs[0]
    p.text = f"出典: {title}　"
    p.font.size = Pt(11)
    p.font.color.rgb = MUTED_COLOR

    if url:
        run = p.add_run()
        run.text = url
        run.font.size = Pt(11)
        run.font.color.rgb = LINK_COLOR
        run.font.underline = True
        run.hyperlink.address = url

    if video_url:
        run = p.add_run()
        run.text = "　[動画を見る]"
        run.font.size = Pt(11)
        run.font.color.rgb = LINK_COLOR
        run.font.underline = True
        run.hyperlink.address = video_url

    p2 = tf.add_paragraph()
    p2.text = f"取得日時: {fetched_at}"
    p2.font.size = Pt(10)
    p2.font.color.rgb = MUTED_COLOR


def export_report(cfg: SearchConfig, rows: list[dict], output_path: Path) -> Path:
    """
    rows: engine.scrape() が返す辞書のリスト
          （keyword / text / image_url / video_url / title / url / fetched_at の各キーを持つ）
    1件につき1枚のスライドを作成する。
    """
    prs = Presentation()
    prs.slide_width = SLIDE_WIDTH
    prs.slide_height = SLIDE_HEIGHT

    _add_title_slide(prs, cfg, len(rows))
    for i, row in enumerate(rows, start=1):
        _add_result_slide(prs, row, i, len(rows))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(output_path)
    return output_path
