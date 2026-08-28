"""
マルチサイト対応スクレイピングGUIアプリ（Flet製）

- サイトごとにCSSセレクタ設定をJSONで保存・呼び出し
- Playwrightで動的コンテンツ(JS描画)にも対応
- 結果をプレビュー後、集計・グラフ付きのExcel帳票として出力
"""
from __future__ import annotations

import os
import threading
import traceback
from datetime import datetime
from pathlib import Path

import flet as ft

from scraper.config import SiteConfig, FieldConfig, list_configs, save_config, delete_config
from scraper.engine import scrape
from scraper.exporter import export_report

APP_DIR = Path(__file__).resolve().parent
ASSETS_DIR = APP_DIR / "assets"
OUTPUT_DIR = ASSETS_DIR / "output"
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

APP_PASSWORD = os.environ.get("APP_PASSWORD", "")  # 空文字なら認証なし

PAGINATION_LABELS = {
    "page_number": "URLのページ番号送り（{page}）",
    "click_next": "「次へ」ボタンをクリック",
    "infinite_scroll": "無限スクロール",
    "none": "単一ページのみ",
}


def build_app(page: ft.Page):
    page.title = "サイト別スクレイピングツール"
    page.window.width = 1200
    page.window.height = 800
    page.padding = 10
    page.scroll = ft.ScrollMode.AUTO

    state = {"fields": [], "current_config_name": None, "last_rows": []}

    # ---------------- 左: サイト一覧 ----------------
    site_list = ft.ListView(expand=True, spacing=4)

    def refresh_site_list():
        site_list.controls.clear()
        for cfg in list_configs():
            site_list.controls.append(
                ft.ListTile(
                    title=ft.Text(cfg.name),
                    subtitle=ft.Text(cfg.list_url, size=10, color=ft.Colors.GREY),
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
        status_text.value = f"{len(configs)}件のサイト設定をバックアップファイルにまとめました。下のリンクからダウンロードしてください。"
        page.update()

    file_picker = ft.FilePicker()
    page.overlay.append(file_picker)

    def on_upload_progress(e: ft.FilePickerUploadEvent):
        if e.progress == 1:
            import json
            uploaded_path = Path("uploads") / e.file_name
            try:
                data = json.loads(uploaded_path.read_text(encoding="utf-8"))
                count = 0
                for d in data:
                    save_config(SiteConfig.from_dict(d))
                    count += 1
                refresh_site_list()
                status_text.value = f"{count}件のサイト設定を復元しました。"
            except Exception as ex:
                status_text.value = f"インポートに失敗しました: {ex}"
            page.update()

    file_picker.on_upload = on_upload_progress

    def on_pick_result(e: ft.FilePickerResultEvent):
        if not e.files:
            return
        f = e.files[0]
        upload_url = page.get_upload_url(f.name, 600)
        file_picker.upload([ft.FilePickerUploadFile(name=f.name, upload_url=upload_url)])

    file_picker.on_result = on_pick_result

    # ---------------- 右: サイト設定フォーム ----------------
    name_field = ft.TextField(label="サイト名", expand=True)
    url_field = ft.TextField(
        label="一覧ページURL（{page} でページ番号、{keyword} でキーワードを埋め込み可）",
        expand=True,
        hint_text="https://example.com/search?q={keyword}&page={page}",
    )
    item_selector_field = ft.TextField(label="1件分を囲むCSSセレクタ", expand=True, hint_text=".product-card")
    wait_selector_field = ft.TextField(label="描画完了を待つCSSセレクタ（空欄なら上と同じ）", expand=True)
    wait_timeout_field = ft.TextField(label="待機タイムアウト(ms)", expand=True, value="15000")

    pagination_dd = ft.Dropdown(
        label="ページング方式",
        expand=True,
        options=[ft.dropdown.Option(k, v) for k, v in PAGINATION_LABELS.items()],
        value="page_number",
    )
    max_pages_field = ft.TextField(label="最大ページ数", expand=True, value="5")
    next_button_field = ft.TextField(label="「次へ」ボタンのCSSセレクタ", expand=True, visible=False)
    max_scrolls_field = ft.TextField(label="最大スクロール回数", expand=True, value="10", visible=False)
    scroll_wait_field = ft.TextField(label="スクロール後の待機(ms)", expand=True, value="1000", visible=False)

    def on_pagination_change(e):
        v = pagination_dd.value
        next_button_field.visible = v == "click_next"
        max_pages_field.visible = v in ("page_number", "click_next")
        max_scrolls_field.visible = v == "infinite_scroll"
        scroll_wait_field.visible = v == "infinite_scroll"
        page.update()

    pagination_dd.on_change = on_pagination_change

    # ---- 取得項目（フィールド）編集 ----
    fields_column = ft.Column(spacing=6)

    def render_fields():
        fields_column.controls.clear()
        for i, f in enumerate(state["fields"]):
            fields_column.controls.append(_field_row(i, f))
        page.update()

    def _field_row(i, f: dict):
        name_tf = ft.TextField(label="列名", value=f.get("name", ""), dense=True, col={"xs": 12, "sm": 6, "md": 2},
                                on_change=lambda e: f.__setitem__("name", e.control.value))
        sel_tf = ft.TextField(label="相対CSSセレクタ", value=f.get("selector", ""), dense=True, col={"xs": 12, "sm": 6, "md": 3},
                               on_change=lambda e: f.__setitem__("selector", e.control.value))
        attr_tf = ft.TextField(label="取得方法(text/html/属性名)", value=f.get("attr", "text"), dense=True, col={"xs": 12, "sm": 6, "md": 3},
                                on_change=lambda e: f.__setitem__("attr", e.control.value))
        numeric_cb = ft.Checkbox(label="数値として集計", value=f.get("numeric", False), col={"xs": 8, "sm": 4, "md": 2},
                                  on_change=lambda e: f.__setitem__("numeric", e.control.value))

        def remove(e):
            state["fields"].pop(i)
            render_fields()

        return ft.ResponsiveRow(
            [name_tf, sel_tf, attr_tf, numeric_cb,
             ft.Container(ft.IconButton(icon=ft.Icons.CLOSE, icon_size=18, on_click=remove), col={"xs": 4, "sm": 2, "md": 2})],
            spacing=8,
        )

    def add_field(e):
        state["fields"].append({"name": "", "selector": "", "attr": "text", "numeric": False})
        render_fields()

    def load_into_form(cfg: SiteConfig):
        state["current_config_name"] = cfg.name
        name_field.value = cfg.name
        url_field.value = cfg.list_url
        item_selector_field.value = cfg.item_selector
        wait_selector_field.value = cfg.wait_selector
        wait_timeout_field.value = str(cfg.wait_timeout_ms)
        pagination_dd.value = cfg.pagination_type
        max_pages_field.value = str(cfg.max_pages)
        next_button_field.value = cfg.next_button_selector
        max_scrolls_field.value = str(cfg.max_scrolls)
        scroll_wait_field.value = str(cfg.scroll_wait_ms)
        state["fields"] = [f.to_dict() for f in cfg.fields]
        on_pagination_change(None)
        render_fields()
        tabs.selected_index = 1  # 「サイト設定」タブへ切り替え
        page.update()

    def clear_form(e=None):
        state["current_config_name"] = None
        name_field.value = ""
        url_field.value = ""
        item_selector_field.value = ""
        wait_selector_field.value = ""
        wait_timeout_field.value = "15000"
        pagination_dd.value = "page_number"
        max_pages_field.value = "5"
        next_button_field.value = ""
        max_scrolls_field.value = "10"
        scroll_wait_field.value = "1000"
        state["fields"] = []
        render_fields()
        on_pagination_change(None)
        page.update()

    def build_config_from_form() -> SiteConfig:
        fields = [
            FieldConfig(name=f["name"], selector=f["selector"], attr=f.get("attr", "text") or "text",
                        numeric=f.get("numeric", False))
            for f in state["fields"] if f.get("name")
        ]
        return SiteConfig(
            name=name_field.value.strip(),
            list_url=url_field.value.strip(),
            item_selector=item_selector_field.value.strip(),
            fields=fields,
            pagination_type=pagination_dd.value,
            max_pages=int(max_pages_field.value or 5),
            next_button_selector=next_button_field.value.strip(),
            max_scrolls=int(max_scrolls_field.value or 10),
            scroll_wait_ms=int(scroll_wait_field.value or 1000),
            wait_selector=wait_selector_field.value.strip(),
            wait_timeout_ms=int(wait_timeout_field.value or 15000),
        )

    def on_save(e):
        if not name_field.value.strip():
            status_text.value = "サイト名を入力してください。"
            page.update()
            return
        cfg = build_config_from_form()
        save_config(cfg)
        status_text.value = f"「{cfg.name}」の設定を保存しました。"
        refresh_site_list()
        page.update()

    # ---------------- 実行セクション ----------------
    keyword_field = ft.TextField(label="キーワード（{keyword} を使う場合）", expand=True)
    start_page_field = ft.TextField(label="開始ページ", expand=True, value="1")
    headless_cb = ft.Checkbox(label="ブラウザを表示せず実行(headless)", value=True)
    run_button = ft.ElevatedButton("実行", icon=ft.Icons.PLAY_ARROW)
    export_button = ft.ElevatedButton("帳票出力(Excel)", icon=ft.Icons.TABLE_CHART, disabled=True)
    progress_ring = ft.ProgressRing(visible=False, width=20, height=20)
    status_text = ft.Text("", color=ft.Colors.BLUE_GREY)

    log_view = ft.ListView(height=180, spacing=2, auto_scroll=True)

    def log(msg: str):
        log_view.controls.append(ft.Text(msg, size=11, font_family="monospace"))
        page.update()

    preview_table = ft.DataTable(columns=[ft.DataColumn(ft.Text(""))], rows=[])
    preview_container = ft.Column([preview_table], scroll=ft.ScrollMode.AUTO)

    def render_preview(field_names: list[str], rows: list[dict]):
        preview_table.columns = [ft.DataColumn(ft.Text(n)) for n in field_names] or [ft.DataColumn(ft.Text("(項目なし)"))]
        preview_table.rows = [
            ft.DataRow(cells=[ft.DataCell(ft.Text(str(r.get(n, "")))) for n in field_names])
            for r in rows[:200]  # プレビューは最大200件
        ]
        page.update()

    def do_run(e):
        cfg = build_config_from_form()
        if not cfg.name or not cfg.list_url or not cfg.item_selector or not cfg.fields:
            status_text.value = "サイト名・URL・アイテムセレクタ・取得項目を入力してください。"
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
                    keyword=keyword_field.value.strip(),
                    start_page=int(start_page_field.value or 1),
                    on_progress=log,
                    headless=headless_cb.value,
                )
                state["last_rows"] = rows
                state["last_cfg"] = cfg
                render_preview([f.name for f in cfg.fields], rows)
                status_text.value = f"完了: {len(rows)} 件取得しました。"
                export_button.disabled = len(rows) == 0
            except Exception as ex:
                status_text.value = f"エラーが発生しました: {ex}"
                log("".join(traceback.format_exc().splitlines()[-5:]))
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
        filename = f"{cfg.name}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx"
        out_path = OUTPUT_DIR / filename
        try:
            export_report(cfg, rows, out_path)
            status_text.value = "帳票を出力しました。下のリンクからダウンロードしてください。"
            export_link.controls = [
                ft.TextButton(f"↓ {filename} をダウンロード", url=f"/output/{filename}")
            ]
        except Exception as ex:
            status_text.value = f"帳票出力エラー: {ex}"
        page.update()

    run_button.on_click = do_run
    export_button.on_click = do_export

    # ---------------- レイアウト組み立て（スマホ幅でも使えるようタブ構成） ----------------
    site_list_view = ft.Column([
        ft.ElevatedButton("＋ 新規サイト", icon=ft.Icons.ADD, on_click=clear_form),
        ft.Row([
            ft.OutlinedButton("設定をバックアップ", icon=ft.Icons.DOWNLOAD, on_click=do_backup),
            ft.OutlinedButton("設定を復元", icon=ft.Icons.UPLOAD,
                               on_click=lambda e: file_picker.pick_files(allow_multiple=False, allowed_extensions=["json"])),
        ], wrap=True),
        backup_link,
        ft.Text("※クラウド無料枠は再起動でデータが消えることがあります。定期的にバックアップしてください。",
                size=11, color=ft.Colors.GREY),
        ft.Divider(),
        site_list,
    ], expand=True)

    editor_view = ft.Column([
        ft.Text("サイト設定", weight=ft.FontWeight.BOLD, size=16),
        ft.ResponsiveRow([ft.Container(name_field, col=12)]),
        ft.ResponsiveRow([ft.Container(url_field, col=12)]),
        ft.ResponsiveRow([
            ft.Container(item_selector_field, col={"xs": 12, "md": 5}),
            ft.Container(wait_selector_field, col={"xs": 12, "md": 5}),
            ft.Container(wait_timeout_field, col={"xs": 12, "md": 2}),
        ]),
        ft.ResponsiveRow([
            ft.Container(pagination_dd, col={"xs": 12, "sm": 6, "md": 3}),
            ft.Container(max_pages_field, col={"xs": 12, "sm": 6, "md": 2}),
            ft.Container(next_button_field, col={"xs": 12, "sm": 6, "md": 3}),
            ft.Container(max_scrolls_field, col={"xs": 12, "sm": 6, "md": 2}),
            ft.Container(scroll_wait_field, col={"xs": 12, "sm": 6, "md": 2}),
        ]),
        ft.Divider(),
        ft.Row([ft.Text("取得項目", weight=ft.FontWeight.BOLD), ft.IconButton(icon=ft.Icons.ADD, on_click=add_field)]),
        fields_column,
        ft.ElevatedButton("設定を保存", icon=ft.Icons.SAVE, on_click=on_save),
    ], expand=True, scroll=ft.ScrollMode.AUTO)

    run_view = ft.Column([
        ft.Text("実行", weight=ft.FontWeight.BOLD, size=16),
        ft.Text("※実行するサイトは「サイト設定」タブで選択・保存したものが対象です", size=11, color=ft.Colors.GREY),
        ft.ResponsiveRow([
            ft.Container(keyword_field, col={"xs": 12, "sm": 6}),
            ft.Container(start_page_field, col={"xs": 6, "sm": 3}),
            ft.Container(headless_cb, col={"xs": 6, "sm": 3}),
        ]),
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
                        ft.Tab(label="サイト一覧"),
                        ft.Tab(label="サイト設定"),
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
    render_fields()


def main(page: ft.Page):
    """APP_PASSWORD が設定されていれば簡易パスワード認証を挟んでからアプリ本体を表示する。"""
    if not APP_PASSWORD:
        build_app(page)
        return

    page.title = "サイト別スクレイピングツール"
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
