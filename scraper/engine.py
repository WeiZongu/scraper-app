"""
検索キーワードからWebを検索し、各ページのDOM内から抽出キーワードを含む
テキストの塊を丸ごと抜き出すエンジン。
CSSセレクタの指定なしで使えるように、要素の特定はキーワード一致で行う。
"""
from __future__ import annotations

from datetime import datetime
from typing import Callable
from urllib.parse import quote_plus

from playwright.sync_api import sync_playwright, Page

from .config import SearchConfig

# 進捗通知用コールバックの型: (message: str) -> None
ProgressCallback = Callable[[str], None]

SEARCH_URL = "https://html.duckduckgo.com/html/?q={query}"

# ページ内でキーワード一致を探す際の、抜粋として妥当とみなすテキスト長の範囲
MIN_SNIPPET_CHARS = 8


class ScrapeError(Exception):
    pass


def _noop(_msg: str) -> None:
    pass


def _search_result_urls(page: Page, keyword: str, max_results: int, on_progress: ProgressCallback) -> list[str]:
    """DuckDuckGoの検索結果ページから、上位 max_results 件のURLを取得する。"""
    url = SEARCH_URL.format(query=quote_plus(keyword))
    on_progress(f"検索中: {keyword}")
    page.goto(url, timeout=30000)
    try:
        page.wait_for_selector("a.result__a", timeout=10000)
    except Exception:
        pass

    hrefs = page.eval_on_selector_all(
        "a.result__a",
        "els => els.map(e => e.href)",
    )
    urls: list[str] = []
    for href in hrefs:
        if href and href not in urls:
            urls.append(href)
        if len(urls) >= max_results:
            break
    on_progress(f"  -> 検索結果 {len(urls)} 件を対象にします。")
    return urls


# ブラウザ内(JS)で実行し、キーワードを含む「塊」をまるごと拾ってくる関数。
# 親要素と子要素の両方が同じキーワードにマッチする場合は、一番内側（子）の
# 要素だけを採用することで、同じ内容の重複抽出を避ける。
_EXTRACT_JS = """
(args) => {
    const keywords = args.keywords;
    const minLen = args.minLen;
    const maxLen = args.maxLen;
    const results = [];
    const seen = new Set();
    const skipTags = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "HEAD"]);
    const all = document.querySelectorAll("body *");

    for (const el of all) {
        if (skipTags.has(el.tagName)) continue;
        const text = (el.innerText || "").replace(/\\s+/g, " ").trim();
        if (!text || text.length < minLen) continue;

        for (const kw of keywords) {
            if (!text.includes(kw)) continue;

            // 子要素の中に、同じキーワードを含む要素があれば、
            // より内側の要素で拾われるはずなのでここではスキップする。
            let hasMatchingChild = false;
            for (const child of el.children) {
                const childText = (child.innerText || "").replace(/\\s+/g, " ").trim();
                if (childText.length >= minLen && childText.includes(kw)) {
                    hasMatchingChild = true;
                    break;
                }
            }
            if (hasMatchingChild) continue;

            const snippet = text.length > maxLen ? text.slice(0, maxLen) + "…" : text;
            const dedupeKey = kw + "::" + snippet.slice(0, 80);
            if (seen.has(dedupeKey)) continue;
            seen.add(dedupeKey);
            results.push({keyword: kw, text: snippet});
        }
    }
    return results;
}
"""


def _extract_matches(page: Page, keywords: list[str], max_snippet_chars: int) -> list[dict]:
    return page.evaluate(
        _EXTRACT_JS,
        {"keywords": keywords, "minLen": MIN_SNIPPET_CHARS, "maxLen": max_snippet_chars},
    )


def scrape(
    cfg: SearchConfig,
    on_progress: ProgressCallback = _noop,
    headless: bool = True,
) -> list[dict]:
    """
    検索キーワードでWebを検索し、上位ページを開いて抽出キーワードに
    一致するテキストの塊を集める。行データのリストを返す。
    """
    keywords = [k.strip() for k in cfg.extract_keywords if k.strip()]
    if not keywords:
        raise ScrapeError("抽出キーワードを1つ以上入力してください。")

    results: list[dict] = []
    seen_texts: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        page = browser.new_page()

        try:
            urls = _search_result_urls(page, cfg.search_keyword, cfg.max_pages, on_progress)
            if not urls:
                on_progress("検索結果が見つかりませんでした。")

            for i, url in enumerate(urls, start=1):
                on_progress(f"[{i}/{len(urls)}] ページを取得中: {url}")
                try:
                    page.goto(url, timeout=20000)
                    page.wait_for_timeout(500)
                except Exception as e:
                    on_progress(f"  -> 取得失敗: {e}")
                    continue

                title = ""
                try:
                    title = page.title()
                except Exception:
                    pass

                try:
                    matches = _extract_matches(page, keywords, cfg.max_snippet_chars)
                except Exception as e:
                    on_progress(f"  -> 抽出失敗: {e}")
                    continue

                added = 0
                for m in matches:
                    dedupe_key = f"{m['keyword']}::{m['text']}"
                    if dedupe_key in seen_texts:
                        continue
                    seen_texts.add(dedupe_key)
                    results.append({
                        "keyword": m["keyword"],
                        "text": m["text"],
                        "title": title,
                        "url": url,
                        "fetched_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    })
                    added += 1
                on_progress(f"  -> {added}件抽出（累計 {len(results)}件）")

        finally:
            browser.close()

    on_progress(f"完了: 合計 {len(results)} 件取得しました。")
    return results
