"""
検索キーワードからWebを検索し、各ページのDOM内から抽出キーワードを含む
テキストの塊を丸ごと抜き出すエンジン。
CSSセレクタの指定なしで使えるように、要素の特定はキーワード一致で行う。

Web検索には Brave Search API を使う（検索エンジンのHTMLページを直接
スクレイピングする方式は、bot検知やHTML構造の変化・クラウドIPのブロックなどで
繰り返し不安定になったため、正式なAPIに切り替えた）。
"""
from __future__ import annotations

import os
from datetime import datetime
from typing import Callable

import requests
from playwright.sync_api import sync_playwright, Page

from .config import SearchConfig

# 進捗通知用コールバックの型: (message: str) -> None
ProgressCallback = Callable[[str], None]

# ヘッドレスブラウザだと分かるUser-Agent（"HeadlessChrome"表記）だと
# サイト側にブロックされやすいため、通常のデスクトップ版と同じ
# User-Agentを明示的に指定する（検索結果ページを開く際に使用）。
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

BRAVE_SEARCH_API_URL = "https://api.search.brave.com/res/v1/web/search"
BRAVE_RESULTS_PER_PAGE = 20  # Brave Search APIの1リクエストあたりの最大件数
# ページ数の上限はユーザーには設けないが、無料枠を使い切ってしまわないよう、
# 暴走防止の安全上限としてのみ設ける。
BRAVE_OFFSET_SAFETY_LIMIT = 200

# ページ内でキーワード一致を探す際の、抜粋として妥当とみなすテキスト長の範囲
MIN_SNIPPET_CHARS = 8


class ScrapeError(Exception):
    pass


def _noop(_msg: str) -> None:
    pass


def _brave_search_urls(keyword: str, on_progress: ProgressCallback) -> list[str]:
    """Brave Search APIで検索し、見つかる限りのURLを取得する（件数上限なし）。"""
    api_key = os.environ.get("BRAVE_SEARCH_API_KEY", "").strip()
    if not api_key:
        raise ScrapeError(
            "BRAVE_SEARCH_API_KEY が設定されていません。"
            "Brave Search API (https://api-dashboard.search.brave.com/) の無料枠に登録し、"
            "取得したAPIキーを環境変数 BRAVE_SEARCH_API_KEY に設定してください。"
        )

    on_progress(f"検索中: {keyword}")
    urls: list[str] = []
    seen = set()
    offset = 0

    while offset < BRAVE_OFFSET_SAFETY_LIMIT:
        try:
            resp = requests.get(
                BRAVE_SEARCH_API_URL,
                params={"q": keyword, "count": BRAVE_RESULTS_PER_PAGE, "offset": offset},
                headers={"Accept": "application/json", "X-Subscription-Token": api_key},
                timeout=15,
            )
        except requests.RequestException as e:
            on_progress(f"  -> 検索APIへの接続に失敗しました: {e}")
            break

        if resp.status_code == 401:
            raise ScrapeError("Brave Search APIキーが無効です。設定したAPIキーを確認してください。")
        if resp.status_code == 429:
            on_progress("  -> Brave Search APIのレート制限/無料枠上限に達しました。ここまでの結果で続行します。")
            break
        if resp.status_code != 200:
            on_progress(f"  -> 検索APIエラー（status={resp.status_code}）: {resp.text[:200]}")
            break

        try:
            data = resp.json()
        except ValueError:
            on_progress("  -> 検索APIの応答を解析できませんでした。")
            break

        results = ((data.get("web") or {}).get("results")) or []
        if not results:
            break

        new_count = 0
        for r in results:
            url = r.get("url")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
                new_count += 1

        if new_count == 0:
            break

        on_progress(f"  -> ここまでで {len(urls)} 件のページを発見。続きを確認します…")
        offset += BRAVE_RESULTS_PER_PAGE

    on_progress(f"  -> 検索結果 合計 {len(urls)} 件を対象にします。")
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

            // マッチしたテキストの塊の中、またはそのすぐ近く（数階層上の親要素まで）に
            // ある画像・動画だけを「関連メディア」として拾う。画像・商品写真は同じ
            // カード/記事コンテナ内で見出しやテキストの「兄弟要素」になっていることが
            // 多いため、el自身だけでなく少し上の階層まで確認する。ページ全体は見ない。
            let imageUrl = "";
            let videoUrl = "";
            let container = el;
            for (let hops = 0; hops < 3 && container; hops++) {
                const img = container.querySelector("img[src]");
                const video = container.querySelector("video");
                if (img || video) {
                    if (img) imageUrl = img.src;
                    if (video) {
                        videoUrl = video.currentSrc || "";
                        if (!videoUrl) {
                            const source = video.querySelector("source[src]");
                            if (source) videoUrl = source.src;
                        }
                        if (!imageUrl && video.poster) imageUrl = video.poster;
                    }
                    break;
                }
                container = container.parentElement;
            }

            results.push({keyword: kw, text: snippet, imageUrl: imageUrl, videoUrl: videoUrl});
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

    # 検索はAPI呼び出しのみでブラウザ不要なため、Playwright起動前に済ませる
    # （APIキー未設定などで早期に失敗する場合、ブラウザを起動する無駄を避けられる）。
    urls = _brave_search_urls(cfg.search_keyword, on_progress)
    if not urls:
        on_progress("検索結果が見つかりませんでした。")

    results: list[dict] = []
    seen_texts: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=DESKTOP_USER_AGENT, locale="ja-JP")
        page = context.new_page()

        try:
            for i, url in enumerate(urls, start=1):
                on_progress(f"[{i}/{len(urls)}] ページを取得中: {url}")
                try:
                    page.goto(url, timeout=20000, wait_until="domcontentloaded")
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
                        "image_url": m.get("imageUrl", ""),
                        "video_url": m.get("videoUrl", ""),
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
