"""
SiteConfig に基づいて実際にページを開き、データを抽出するエンジン。
JavaScriptで動的にコンテンツを描画するサイトを想定し、Playwrightを使用する。
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Callable, Optional

from playwright.sync_api import sync_playwright, Page, TimeoutError as PWTimeoutError

from .config import SiteConfig, FieldConfig

# 進捗通知用コールバックの型: (message: str) -> None
ProgressCallback = Callable[[str], None]


class ScrapeError(Exception):
    pass


def _noop(_msg: str) -> None:
    pass


def _extract_field(item_el, fc: FieldConfig) -> str:
    """1つの要素(item_el)から1項目分の値を取り出す。"""
    try:
        target = item_el.query_selector(fc.selector) if fc.selector else item_el
    except Exception:
        target = None

    if target is None:
        return ""

    try:
        if fc.attr == "text":
            value = target.inner_text()
        elif fc.attr == "html":
            value = target.inner_html()
        else:
            value = target.get_attribute(fc.attr) or ""
    except Exception:
        value = ""

    return (value or "").strip()


def clean_numeric(raw: str) -> Optional[float]:
    """「¥1,980」「1,980円」などから数値を取り出す。取れなければ None。"""
    if not raw:
        return None
    m = re.search(r"-?\d[\d,]*\.?\d*", raw)
    if not m:
        return None
    try:
        return float(m.group(0).replace(",", ""))
    except ValueError:
        return None


def _extract_items(page: Page, cfg: SiteConfig) -> list[dict]:
    rows = []
    elements = page.query_selector_all(cfg.item_selector)
    for el in elements:
        row = {}
        for fc in cfg.fields:
            raw = _extract_field(el, fc)
            row[fc.name] = raw
            if fc.numeric:
                row[f"__num__{fc.name}"] = clean_numeric(raw)
        rows.append(row)
    return rows


def _wait_for_content(page: Page, cfg: SiteConfig) -> None:
    selector = cfg.wait_selector.strip() or cfg.item_selector
    try:
        page.wait_for_selector(selector, timeout=cfg.wait_timeout_ms)
    except PWTimeoutError:
        # 出現しなくても、その時点のDOMから抽出を試みる（0件になる可能性はある）
        pass


def scrape(
    cfg: SiteConfig,
    keyword: str = "",
    start_page: int = 1,
    on_progress: ProgressCallback = _noop,
    headless: bool = True,
) -> list[dict]:
    """
    設定に基づいてスクレイピングを実行し、行データのリストを返す。
    keyword はURLテンプレート中の {keyword} に、page 番号は {page} に埋め込む。
    """
    results: list[dict] = []
    seen_keys: set[tuple] = set()

    def add_rows(rows: list[dict]):
        added = 0
        for r in rows:
            key = tuple(r.get(fc.name, "") for fc in cfg.fields)
            if key in seen_keys:
                continue
            seen_keys.add(key)
            results.append(r)
            added += 1
        return added

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        try:
            if cfg.pagination_type == "page_number":
                for page_num in range(start_page, start_page + cfg.max_pages):
                    url = cfg.list_url.format(page=page_num, keyword=keyword)
                    on_progress(f"[{cfg.name}] {page_num}ページ目を取得中: {url}")
                    page.goto(url, timeout=30000)
                    _wait_for_content(page, cfg)
                    rows = _extract_items(page, cfg)
                    added = add_rows(rows)
                    on_progress(f"  -> {len(rows)}件検出（新規 {added}件）")
                    if added == 0:
                        on_progress("  新規データなし。ページングを終了します。")
                        break

            elif cfg.pagination_type == "click_next":
                url = cfg.list_url.format(page=start_page, keyword=keyword)
                on_progress(f"[{cfg.name}] 初期ページを取得中: {url}")
                page.goto(url, timeout=30000)
                _wait_for_content(page, cfg)

                for i in range(cfg.max_pages):
                    rows = _extract_items(page, cfg)
                    added = add_rows(rows)
                    on_progress(f"[{cfg.name}] {i + 1}ページ目: {len(rows)}件検出（新規 {added}件）")
                    if added == 0 and i > 0:
                        on_progress("  新規データなし。ページングを終了します。")
                        break
                    next_btn = page.query_selector(cfg.next_button_selector)
                    if not next_btn:
                        on_progress("  「次へ」ボタンが見つかりません。終了します。")
                        break
                    try:
                        next_btn.click()
                        page.wait_for_timeout(cfg.scroll_wait_ms)
                        _wait_for_content(page, cfg)
                    except Exception as e:
                        on_progress(f"  クリックに失敗: {e}")
                        break

            elif cfg.pagination_type == "infinite_scroll":
                url = cfg.list_url.format(page=start_page, keyword=keyword)
                on_progress(f"[{cfg.name}] ページを取得中: {url}")
                page.goto(url, timeout=30000)
                _wait_for_content(page, cfg)

                prev_count = 0
                for i in range(cfg.max_scrolls):
                    rows = _extract_items(page, cfg)
                    add_rows(rows)
                    on_progress(f"[{cfg.name}] スクロール{i + 1}回目: 累計 {len(results)}件")
                    if len(rows) == prev_count:
                        on_progress("  これ以上増えないため終了します。")
                        break
                    prev_count = len(rows)
                    page.mouse.wheel(0, 4000)
                    page.wait_for_timeout(cfg.scroll_wait_ms)

            else:  # "none" 単一ページ
                url = cfg.list_url.format(page=start_page, keyword=keyword)
                on_progress(f"[{cfg.name}] ページを取得中: {url}")
                page.goto(url, timeout=30000)
                _wait_for_content(page, cfg)
                rows = _extract_items(page, cfg)
                add_rows(rows)
                on_progress(f"  -> {len(rows)}件検出")

        finally:
            browser.close()

    on_progress(f"完了: 合計 {len(results)} 件取得しました。")
    return results
