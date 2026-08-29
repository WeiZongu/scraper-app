"""
検索・抽出結果を、写真を活かしたリッチなPowerPointスライドとして出力する。
構成: 表紙スライド → 全体サマリースライド → 結果スライド（1件1枚、
そのキーワードに紐づく画像をスケルトン風（輪郭線だけを抽出し、元画像の
色に合わせて着色したもの）の背景として敷き、タイトルと抜粋テキストを
中央に配置するカード風レイアウト）。
"""
from __future__ import annotations

import urllib.request
from collections import Counter
from datetime import datetime
from io import BytesIO
from pathlib import Path

from PIL import Image, ImageFilter, ImageOps
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
SKELETON_SCRIM_ALPHA = 30  # スケルトン画像の上にかける、文字を読みやすくするための暗さ(%)
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


MAX_IMAGE_DIMENSION = 1280  # スケルトン加工前に縮小する上限(px)。メモリ節約のため


def _load_image(url: str) -> Image.Image | None:
    """画像URL（そのキーワードの抽出結果に紐づく画像）をダウンロードし、
    Pillowで扱える画像として読み込む。失敗した場合は None を返す。
    元の写真が大きいとスケルトン加工の中間処理（グレースケール・ぼかし・
    輪郭抽出等）でメモリを多く使うため、縮小してから返す
    （無料サーバーのメモリ上限内に収めるため）。
    """
    stream = _download_image_bytes(url)
    if stream is None:
        return None
    try:
        img = Image.open(stream)
        img.load()
        img = img.convert("RGB")
        img.thumbnail((MAX_IMAGE_DIMENSION, MAX_IMAGE_DIMENSION), Image.Resampling.LANCZOS)
        return img
    except Exception:
        return None


def _average_color(image: Image.Image) -> tuple[int, int, int]:
    """画像を1pxに縮小し、全体の平均色を取得する（「カラーは画像に合わせる」
    ための、背景・線の色のベースとして使う）。
    """
    small = image.resize((1, 1), Image.Resampling.LANCZOS)
    return small.getpixel((0, 0))


def _scale_color(rgb: tuple[int, int, int], factor: float) -> tuple[int, int, int]:
    return tuple(min(255, max(0, int(c * factor))) for c in rgb)


def _blend_color(
    rgb: tuple[int, int, int], other: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return tuple(int(c * (1 - t) + o * t) for c, o in zip(rgb, other))


def _make_skeleton_overlay(image: Image.Image, line_color: tuple[int, int, int]) -> Image.Image:
    """写真から輪郭線だけを抽出し、指定色で着色した透過PNG(RGBA)を作る
    （「背景はスケルトン風のキーワード関連画像が良い」というリクエストへの対応）。
    グレースケール化した画像に輪郭強調フィルタ(CONTOUR)をかけて輪郭線を
    抽出し、線の濃さをそのまま透明度に変換することで、線の部分だけ
    不透明に色が乗った、輪郭線だけの画像（スケルトン）を作る。
    """
    gray = ImageOps.grayscale(image)
    blur_radius = max(1, min(gray.size) // 150)
    if blur_radius > 1:
        gray = gray.filter(ImageFilter.GaussianBlur(blur_radius))
    contour = gray.filter(ImageFilter.CONTOUR)

    # CONTOURの出力は白地(255)に輪郭が暗い線として乗るので、暗いほど
    # 不透明になるよう反転してから透明度として使う（コントラストも強調する）。
    alpha = ImageOps.invert(contour)
    alpha = ImageOps.autocontrast(alpha)
    alpha = alpha.point(lambda v: min(255, int(v * 1.6)))

    overlay = Image.new("RGBA", contour.size, line_color + (0,))
    overlay.putalpha(alpha)
    return overlay


def _add_cover_picture(slide, pil_image: Image.Image) -> bool:
    """画像（PIL Image、透過PNGも可）をスライド全体を覆うように拡大配置する
    （はみ出た部分はスライド外に出るため、PowerPoint上では自動的に切り取られて
    表示される）。失敗したら False。
    """
    try:
        buf = BytesIO()
        pil_image.save(buf, format="PNG")
        buf.seek(0)
        pic = slide.shapes.add_picture(buf, 0, 0)
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


def _add_solid_rect(slide, rgb: RGBColor, alpha_pct: int | None = None):
    shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, 0, 0, SLIDE_WIDTH, SLIDE_HEIGHT)
    shape.line.fill.background()
    shape.shadow.inherit = False
    shape.fill.solid()
    shape.fill.fore_color.rgb = rgb
    if alpha_pct is not None:
        _set_transparency(shape.fill, alpha_pct)
    return shape


def _add_overlay(slide, has_image: bool) -> None:
    if has_image:
        _add_solid_rect(slide, OVERLAY_COLOR, OVERLAY_ALPHA_WITH_IMAGE)
    else:
        _add_solid_rect(slide, ACCENT_COLOR)


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
    """1件を1枚のカード風スライドにする。そのキーワードに紐づく画像があれば、
    輪郭線だけを抽出し元画像の色に合わせて着色した「スケルトン風」の背景として
    敷き、その上にタイトルと抜粋テキストを中央揃えで配置する
    （画像が無い/読み込めない場合はアクセントカラーの単色背景になる）。
    """
    slide = prs.slides.add_slide(prs.slide_layouts[6])

    pil_image = _load_image(row.get("image_url", ""))
    if pil_image is not None:
        avg_color = _average_color(pil_image)
        canvas_color = RGBColor(*_scale_color(avg_color, 0.32))
        line_color = _blend_color(avg_color, (255, 255, 255), 0.6)
    else:
        canvas_color = ACCENT_COLOR
        line_color = None
    _add_solid_rect(slide, canvas_color)

    has_image = False
    if pil_image is not None:
        try:
            skeleton = _make_skeleton_overlay(pil_image, line_color)
            has_image = _add_cover_picture(slide, skeleton)
        except Exception:
            has_image = False

    if has_image:
        _add_solid_rect(slide, OVERLAY_COLOR, SKELETON_SCRIM_ALPHA)

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
