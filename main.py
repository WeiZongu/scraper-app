"""
キーワードだけで使えるWeb検索・抽出GUIアプリ（Flet製）

- CSSセレクタの指定は不要。検索キーワードでWebを検索し、
  各ページの中から抽出キーワードを含むテキストの塊をまるごと拾う。
- Playwrightで動的コンテンツ(JS描画)にも対応
- 結果をプレビュー後、1件1スライドの要約されたPowerPointスライドとして出力
"""
from __future__ import annotations

import logging
import os
import threading
import traceback
from datetime import datetime
from pathlib import Path

import flet as ft

from scraper.config import SearchConfig, list_configs, save_config, delete_config
from scraper.engine import scrape
from scraper.exporter import export_report

APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
OUTPUT_DIR = ASSETS_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# 実行ログを標準出力にも残す（クラウドのログ画面から後で確認できるようにするため。
# 画面上の「実行ログ」はブラウザを閉じると消えてしまうが、こちらは残る）。
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("scraper_app")

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # 空文字なら認証なし

PREVIEW_COLUMNS = ["keyword", "text", "image_url", "video_url", "title", "url", "fetched_at"]
PREVIEW_LABELS = ["キーワード", "抜粋テキスト", "画像URL", "動画URL", "ページタイトル", "URL", "取得日時"]


def build_app(page: ft.Page):
    page.title = "キーワード検索・抽出ツール"
    page.window.width = 1200
    page.window.height = 800
    page.padding = 10
    page.scroll = ft.ScrollMode.AUTO

    state = {"current_config_name": None, "last_rows": []}

    # ---------------- 左: 検索設定一覧 ----------------
    site_list = ft.ListView(expand=True, spacing=4)

    def refresh_site_list():
        site_list.controls.clear()
        for cfg in list_configs():
            subtitle = f"検索: {cfg.search_keyword} / 抽出: {', '.join(cfg.extract_keywords)}"
            site_list.controls.append(
                ft.ListTile(
                    title=ft.Text(cfg.name),
                    subtitle=ft.Text(subtitle, size=10, color=ft.Colors.GREY),
                    on_click=lambda e, c=cfg: load_into_form(c),
                    trailing=ft.IconButton(
                        icon=ft.Icons.DELETE_OUTLINE,
                        icon_size=18,
                        on_click=lambda e, c=cfg: (delete_config(c.name), refresh_site_list(), page.update()),
                    ),
                )
            )
        page.update()

    # ---------------- 設定のバックアップ／復元（クラウドの再起動でデータが消えても復旧できるように） ----------------
    DOWNLOADS_DIR = ASSETS_DIR / "downloads"
    DOWNLOADS_DIR.mkdir(parents=True, exist_ok=True)
    backup_link = ft.Row([])

    def do_backup(e):
        import json
        configs = [c.to_dict() for c in list_configs()]
        backup_path = DOWNLOADS_DIR / "site_configs_backup.json"
        backup_path.write_text(json.dumps(configs, ensure_ascii=False, indent=2), encoding="utf-8")
        backup_link.controls = [
            ft.TextButton(
                "↓ site_configs_backup.json をダウンロード",
                url=f"/downloads/{backup_path.name}",
                on_click=lambda e: None,
            )
        ]
        status_text.value = f"{len(configs)}件の検索設定をバックアップファイルにまとめました。下のリンクからダウンロードしてください。"
        page.update()

    file_picker = ft.FilePicker()
    page.services.append(file_picker)

    def on_upload_progress(e: ft.FilePickerUploadEvent):
        if e.progress == 1:
            import json
            uploaded_path = Path("uploads") / e.file_name
            try:
                data = json.loads(uploaded_path.read_text(encoding="utf-8"))
                count = 0
                for d in data:
                    save_config(SearchConfig.from_dict(d))
                    count += 1
                refresh_site_list()
                status_text.value = f"{count}件の検索設定を復元しました。"
            except Exception as ex:
                status_text.value = f"インポートに失敗しました: {ex}"
            page.update()

    file_picker.on_upload = on_upload_progress

    async def on_pick_click(e):
        files = await file_picker.pick_files(allow_multiple=False, allowed_extensions=["json"])
        if not files:
            return
        f = files[0]
        upload_url = page.get_upload_url(f.name, 600)
        await file_picker.upload([ft.FilePickerUploadFile(name=f.name, upload_url=upload_url)])

    # ---------------- 右: 検索設定フォーム ----------------
    name_field = ft.TextField(label="設定名", expand=True)
    search_keyword_field = ft.TextField(
        label="検索キーワード（Webを検索するためのキーワード）",
        expand=True,
        hint_text="例: 東京 ラーメン 新宿",
    )
    extract_keywords_field = ft.TextField(
        label="抽出キーワード（カンマ区切りで複数可。このキーワードを含む文章の塊を丸ごと抜き出します）",
        expand=True,
        hint_text="例: 営業時間, 定休日, 住所",
    )
    def load_into_form(cfg: SearchConfig):
        state["current_config_name"] = cfg.name
        name_field.value = cfg.name
        search_keyword_field.value = cfg.search_keyword
        extract_keywords_field.value = ", ".join(cfg.extract_keywords)
        tabs.selected_index = 1  # 「検索設定」タブへ切り替え
        page.update()

    def clear_form(e=None):
        state["current_config_name"] = None
        name_field.value = ""
        search_keyword_field.value = ""
        extract_keywords_field.value = ""
        page.update()

    def build_config_from_form() -> SearchConfig:
        extract_keywords = [k.strip() for k in extract_keywords_field.value.split(",") if k.strip()]
        return SearchConfig(
            name=name_field.value.strip(),
            search_keyword=search_keyword_field.value.strip(),
            extract_keywords=extract_keywords,
        )

    def on_save(e):
        if not name_field.value.strip():
            status_text.value = "設定名を入力してください。"
            page.update()
            return
        cfg = build_config_from_form()
        save_config(cfg)
        status_text.value = f"「{cfg.name}」の設定を保存しました。"
        refresh_site_list()
        page.update()

    # ---------------- 実行セクション ----------------
    run_button = ft.ElevatedButton("実行", icon=ft.Icons.PLAY_ARROW)
    export_button = ft.ElevatedButton("スライド出力(PowerPoint)", icon=ft.Icons.SLIDESHOW, disabled=True)
    progress_ring = ft.ProgressRing(visible=False, width=20, height=20)
    status_text = ft.Text("", color=ft.Colors.BLUE_GREY)

    log_view = ft.ListView(height=180, spacing=2, auto_scroll=True)

    def log(msg: str):
        logger.info(msg)
        log_view.controls.append(ft.Text(msg, size=11, font_family="monospace"))
        page.update()

    preview_table = ft.DataTable(columns=[ft.DataColumn(ft.Text(l)) for l in PREVIEW_LABELS], rows=[])
    preview_container = ft.Column([preview_table], scroll=ft.ScrollMode.AUTO)

    def render_preview(rows: list[dict]):
        preview_table.rows = [
            ft.DataRow(cells=[ft.DataCell(ft.Text(str(r.get(c, "")))) for c in PREVIEW_COLUMNS])
            for r in rows[:200]  # プレビューは最大200件
        ]
        page.update()

    def do_run(e):
        cfg = build_config_from_form()
        if not cfg.name or not cfg.search_keyword or not cfg.extract_keywords:
            status_text.value = "設定名・検索キーワード・抽出キーワードを入力してください。"
            page.update()
            return

        run_button.disabled = True
        export_button.disabled = True
        progress_ring.visible = True
        log_view.controls.clear()
        status_text.value = "実行中..."
        page.update()

        def worker():
            try:
                rows = scrape(
                    cfg,
                    on_progress=log,
                    headless=True,
                )
                state["last_rows"] = rows
                state["last_cfg"] = cfg
                render_preview(rows)
                status_text.value = f"完了: {len(rows)} 件取得しました。"
                export_button.disabled = len(rows) == 0
            except Exception as ex:
                status_text.value = f"エラーが発生しました: {ex}"
                # 画面には要点だけ、サーバー側のログには完全なトレースバックを残す
                logger.exception("scrape() failed")
                log_view.controls.append(
                    ft.Text("\n".join(traceback.format_exc().splitlines()[-5:]), size=11, font_family="monospace")
                )
                page.update()
            finally:
                run_button.disabled = False
                progress_ring.visible = False
                page.update()

        threading.Thread(target=worker, daemon=True).start()

    export_link = ft.Row([])

    def do_export(e):
        cfg = state.get("last_cfg")
        rows = state.get("last_rows", [])
        if not cfg or not rows:
            status_text.value = "先にデータを取得してください。"
            page.update()
            return
        filename = f"{cfg.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.pptx"
        out_path = OUTPUT_DIR / filename
        logger.info("スライド出力を開始します: %s（%d件）", filename, len(rows))
        try:
            export_report(cfg, rows, out_path)
            logger.info("スライド出力が完了しました: %s", filename)
            status_text.value = "スライドを出力しました。下のリンクからダウンロードしてください。"
            export_link.controls = [
                ft.TextButton(f"↓ {filename} をダウンロード", url=f"/output/{filename}")
            ]
        except Exception as ex:
            logger.exception("export_report() failed")
            status_text.value = f"スライド出力エラー: {ex}"
        page.update()

    run_button.on_click = do_run
    export_button.on_click = do_export

    # ---------------- レイアウト組み立て（スマホ幅でも使えるようタブ構成） ----------------
    site_list_view = ft.Column([
        ft.ElevatedButton("＋ 新規設定", icon=ft.Icons.ADD, on_click=clear_form),
        ft.Row([
            ft.OutlinedButton("設定をバックアップ", icon=ft.Icons.DOWNLOAD, on_click=do_backup),
            ft.OutlinedButton("設定を復元", icon=ft.Icons.UPLOAD, on_click=on_pick_click),
        ], wrap=True),
        backup_link,
        ft.Text("※クラウド無料枠は再起動でデータが消えることがあります。定期的にバックアップしてください。",
                size=11, color=ft.Colors.GREY),
        ft.Divider(),
        site_list,
    ], expand=True)

    editor_view = ft.Column([
        ft.Text("検索設定", weight=ft.FontWeight.BOLD, size=16),
        ft.ResponsiveRow([ft.Container(name_field, col=12)]),
        ft.ResponsiveRow([ft.Container(search_keyword_field, col=12)]),
        ft.ResponsiveRow([ft.Container(extract_keywords_field, col=12)]),
        ft.ElevatedButton("設定を保存", icon=ft.Icons.SAVE, on_click=on_save),
    ], expand=True, scroll=ft.ScrollMode.AUTO)

    run_view = ft.Column([
        ft.Text("実行", weight=ft.FontWeight.BOLD, size=16),
        ft.Text("※実行する設定は「検索設定」タブで選択・保存したものが対象です", size=11, color=ft.Colors.GREY),
        ft.Row([run_button, progress_ring, export_button], wrap=True),
        export_link,
        status_text,
        ft.Text("実行ログ", size=12, color=ft.Colors.GREY),
        ft.Container(log_view, border=ft.Border.all(1, ft.Colors.GREY_300), padding=6),
        ft.Text("プレビュー（最大200件・横スクロール可）", size=12, color=ft.Colors.GREY),
        ft.Container(preview_container, border=ft.Border.all(1, ft.Colors.GREY_300), padding=6, height=300),
    ], expand=True, scroll=ft.ScrollMode.AUTO)

    tabs = ft.Tabs(
        selected_index=0,
        length=3,
        expand=True,
        content=ft.Column(
            expand=True,
            controls=[
                ft.TabBar(
                    tabs=[
                        ft.Tab(label="検索設定一覧"),
                        ft.Tab(label="検索設定"),
                        ft.Tab(label="実行・結果"),
                    ],
                ),
                ft.TabBarView(
                    expand=True,
                    controls=[
                        ft.Container(site_list_view, padding=10),
                        ft.Container(editor_view, padding=10),
                        ft.Container(run_view, padding=10),
                    ],
                ),
            ],
        ),
    )

    page.add(tabs)

    refresh_site_list()


def main(page: ft.Page):
    """APP_PASSWORD が設定されていれば簡易パスワード認証を挟んでからアプリ本体を表示する。"""
    if not APP_PASSWORD:
        build_app(page)
        return

    page.title = "キーワード検索・抽出ツール"
    pw_field = ft.TextField(label="パスワード", password=True, can_reveal_password=True, width=280,
                             on_submit=lambda e: try_login(None))
    error_text = ft.Text("", color=ft.Colors.RED)

    def try_login(e):
        if pw_field.value == APP_PASSWORD:
            page.controls.clear()
            page.update()
            build_app(page)
        else:
            error_text.value = "パスワードが違います。"
            page.update()

    page.add(
        ft.Column(
            [ft.Text("パスワードを入力してください", size=18, weight=ft.FontWeight.BOLD),
             pw_field, error_text,
             ft.ElevatedButton("ログイン", on_click=try_login)],
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
            alignment=ft.MainAxisAlignment.CENTER,
        )
    )


if __name__ == "__main__":
    # host="0.0.0.0" にすることで、同じネットワーク上の他端末（iPhone等）や
    # クラウド環境からアクセスできるようになる。ポートはクラウド側が指定する
    # 環境変数 PORT を優先し、なければローカル用に8550を使う。
    ft.app(
        target=main,
        view=ft.AppView.WEB_BROWSER,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8550)),
        assets_dir="assets",
        upload_dir="uploads",
    )
