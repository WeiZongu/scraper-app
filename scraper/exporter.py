"""
検索・抽出結果を、写真を活かしたリッチなPowerPointスライドとして出力する。
構成: 表紙スライド → 全体サマリースライド → 結果スライド（1件1枚、
画像を背景いっぱいに敷き、タイトルと抜粋テキストを中央に配置するカード風レイアウト）。
"""
from __future__ import annotations

import urllib.request
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path

from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Inches, Pt

from .config import SearchConfig

SLIDE_WIDTH = Inches(13.333)
SLIDE_HEIGHT = Inches(7.5)

ACCENT_COLOR = RGBColor(0x30, 0x54, 0x96)
OVERLAY_COLOR = RGBColor(0x0A, 0x0E, 0x1A)
SUMMARY_BG_COLOR = RGBColor(0xF5, 0xF6, 0xFA)
TEXT_COLOR = RGBColor(0x33, 0x33, 0x33)
MUTED_COLOR = RGBColor(0xC7, 0xCC, 0xD8)
LINK_COLOR = RGBColor(0x9E, 0xC4, 0xFF)
WHITE = RGBColor(0xFF, 0xFF, 0xFF)

OVERLAY_ALPHA_WITH_IMAGE = 55  # 画像がある場合のオーバーレイの不透明度(%)
DOWNLOAD_TIMEOUT_SEC = 8


def _set_transparency(fill, alpha_pct: int) -> None:
    """python-pptxには塗りつぶし透明度の公開APIが無いため、XMLを直接操作する。
    alpha_pct: 0(完全に透明)〜100(完全に不透明)
    """
    color_elm = fill.fore_color._xFill.find(qn("a:srgbClr"))
    if color_elm is None:
        color_elm = fill.fore_color._xFill.find(qn("a:schemeClr"))
    if color_elm is None:
        return
    alpha = OxmlElement("a:alpha")
    alpha.set("val", str(int(alpha_pct * 1000)))
    color_elm.append(alpha)


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


def _add_cover_background_image(slide, image_stream: BytesIO) -> bool:
    """画像をスライド全体を覆うように拡大配置する（はみ出た部分はスライド外に
    出るため、PowerPoint上では自動的に切り取られて表示される）。失敗したら False。
    """
    try:
        pic = slide.shapes.add_picture(image_stream, 0, 0)
        scale = max(SLIDE_WIDTH / pic.width, SLIDE_HEIGHT / pic.height)
        new_w = int(pic.width * scale)
        new_h = int(pic.height * scale)
        pic.width = new_w
        pic.height = new_h
        pic.left = int((SLIDE_WIDTH - new_w) / 2)
        pic.top = int((SLIDE_HEIGHT - new_h) / 2)
        return True
    except Exception:
        return False


def _add_overlay(slide, has_image: bool) -> None:
    overlay = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SLIDE_WIDTH, SLIDE_HEIGHT)
    overlay.line.fill.background()
    overlay.shadow.inherit = False
    overlay.fill.solid()
    if has_image:
        overlay.fill.fore_color.rgb = OVERLAY_COLOR
        _set_transparency(overlay.fill, OVERLAY_ALPHA_WITH_IMAGE)
    else:
        overlay.fill.fore_color.rgb = ACCENT_COLOR


def _add_title_slide(prs: Presentation, cfg: SearchConfig, count: int) -> None:
    slide = prs.slides.add_slide(prs.slide_layouts[6])  # 白紙レイアウト
    _add_overlay(slide, has_image=False)

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
        f"抽出キーワード: {', '.join(cfg.extract_keywords)}",
        f"件数: {count}件　|　作成日時: {datetime.now().strftime('%Y-%m-%d %H:%M')}",
    ]
    for i, line in enumerate(lines):
        p = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        p.text = line
        p.alignment = PP_ALIGN.CENTER
        p.font.size = Pt(16)
        p.font.color.rgb = MUTED_COLOR
        p.space_after = Pt(8)


def _add_summary_slide(prs: Presentation, rows: list[dict]) -> None:
    """抽出キーワードごとの件数を一覧できる、全体サマリースライドを1枚追加する。
    キーワードを増やすほど個々の結果スライドの話題が分散して見えるため、
    冒頭で全体像を掴めるようにする。
    """
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    bg = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SLIDE_WIDTH, SLIDE_HEIGHT)
    bg.line.fill.background()
    bg.shadow.inherit = False
    bg.fill.solid()
    bg.fill.fore_color.rgb = SUMMARY_BG_COLOR

    title_box = slide.shapes.add_textbox(Inches(0.7), Inches(0.5), Inches(12), Inches(0.9))
    tf = title_box.text_frame
    tf.text = "全体サマリー"
    tf.paragraphs[0].font.size = Pt(30)
    tf.paragraphs[0].font.bold = True
    tf.paragraphs[0].font.color.rgb = ACCENT_COLOR

    counter: Counter[str] = Counter()
    for row in rows:
        for kw in str(row.get("keyword", "")).split(","):
            kw = kw.strip()
            if kw:
                counter[kw] += 1

    body_box = slide.shapes.add_textbox(Inches(0.9), Inches(1.7), Inches(11.5), Inches(5.3))
    tf = body_box.text_frame
    tf.word_wrap = True

    p = tf.paragraphs[0]
    p.text = f"取得件数: 合計 {len(rows)} 件"
    p.font.size = Pt(22)
    p.font.bold = True
    p.font.color.rgb = TEXT_COLOR
    p.space_after = Pt(20)

    if counter:
        p = tf.add_paragraph()
        p.text = "キーワード別の件数"
        p.font.size = Pt(18)
        p.font.bold = True
        p.font.color.rgb = ACCENT_COLOR
        p.space_after = Pt(10)

        for kw, cnt in counter.most_common():
            p = tf.add_paragraph()
            p.text = f"　・{kw}　―　{cnt} 件"
            p.font.size = Pt(18)
            p.font.color.rgb = TEXT_COLOR
            p.space_after = Pt(8)


def _add_result_slide(prs: Presentation, row: dict, index: int, total: int) -> None:
    """1件を1枚のカード風スライドにする。画像があれば背景いっぱいに敷き、
    半透明の暗いオーバーレイの上にタイトルと抜粋テキストを中央揃えで配置する
    （画像が無い場合はアクセントカラーの単色背景になる）。
    """
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    image_stream = _download_image_bytes(row.get("image_url", ""))
    has_image = image_stream is not None and _add_cover_background_image(slide, image_stream)
    _add_overlay(slide, has_image)

    title_box = slide.shapes.add_textbox(Inches(1), Inches(0.6), SLIDE_WIDTH - Inches(2), Inches(1.1))
    tf = title_box.text_frame
    tf.word_wrap = True
    tf.text = row.get("keyword", "")
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.font.size = Pt(30)
    p.font.bold = True
    p.font.color.rgb = WHITE

    page_box = slide.shapes.add_textbox(SLIDE_WIDTH - Inches(1.6), Inches(0.25), Inches(1.2), Inches(0.4))
    page_box.text_frame.text = f"{index}/{total}"
    page_box.text_frame.paragraphs[0].font.size = Pt(12)
    page_box.text_frame.paragraphs[0].font.color.rgb = MUTED_COLOR
    page_box.text_frame.paragraphs[0].alignment = PP_ALIGN.RIGHT

    body_box = slide.shapes.add_textbox(Inches(1.3), Inches(2.0), SLIDE_WIDTH - Inches(2.6), Inches(3.9))
    tf = body_box.text_frame
    tf.word_wrap = True
    tf.vertical_anchor = MSO_ANCHOR.MIDDLE
    tf.auto_size = MSO_AUTO_SIZE.NONE
    tf.text = row.get("text", "")
    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
    p.font.size = Pt(24)
    p.font.color.rgb = WHITE

    footer_box = slide.shapes.add_textbox(Inches(0.6), SLIDE_HEIGHT - Inches(0.9), SLIDE_WIDTH - Inches(1.2), Inches(0.7))
    tf = footer_box.text_frame
    tf.word_wrap = True

    title = row.get("title", "")
    url = row.get("url", "")
    video_url = row.get("video_url", "")
    fetched_at = row.get("fetched_at", "")

    p = tf.paragraphs[0]
    p.alignment = PP_ALIGN.CENTER
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
    p2.alignment = PP_ALIGN.CENTER
    p2.text = f"取得日時: {fetched_at}"
    p2.font.size = Pt(10)
    p2.font.color.rgb = MUTED_COLOR


def export_report(cfg: SearchConfig, rows: list[dict], output_path: Path) -> Path:
    """
    rows: engine.scrape() が返す辞書のリスト
          （keyword / text / image_url / video_url / title / url / fetched_at の各キーを持つ）
    表紙 → 全体サマリー → 結果スライド（1件1枚）の順に構成する。
    """
    prs = Presentation()
    prs.slide_width = SLIDE_WIDTH
    prs.slide_height = SLIDE_HEIGHT

    _add_title_slide(prs, cfg, len(rows))
    _add_summary_slide(prs, rows)
    for i, row in enumerate(rows, start=1):
        _add_result_slide(prs, row, i, len(rows))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(output_path)
    return output_path
