"""
検索キーワードからWebを検索し、各ページのDOM内から抽出キーワードを含む
テキストの塊を丸ごと抜き出すエンジン。
CSSセレクタの指定なしで使えるように、要素の特定はキーワード一致で行う。

Web検索には Serper.dev の Search API を使う（検索エンジンのHTMLページを直接
スクレイピングする方式は、bot検知やHTML構造の変化・クラウドIPのブロックなどで
繰り返し不安定になったため、正式なAPIに切り替えた。クレジットカード登録不要の
無料クレジットがあるためSerper.devを採用している）。
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

SERPER_SEARCH_API_URL = "https://google.serper.dev/search"
# 1ページあたりの件数と、暴走防止のための取得ページ数の安全上限
# （買い切りクレジット制のため、無制限に使い切らないよう控えめにしておく）
SERPER_RESULTS_PER_PAGE = 10
SERPER_MAX_PAGES = 5

# ページ内でキーワード一致を探す際の、抜粋として妥当とみなすテキスト長の範囲
MIN_SNIPPET_CHARS = 8


class ScrapeError(Exception):
    pass


def _noop(_msg: str) -> None:
    pass


def _serper_search_urls(keyword: str, on_progress: ProgressCallback) -> list[str]:
    """Serper.dev の Search API で検索し、URLを取得する。"""
    api_key = os.environ.get("SERPER_API_KEY", "").strip()
    if not api_key:
        raise ScrapeError(
            "SERPER_API_KEY が設定されていません。"
            "Serper.dev (https://serper.dev/) に登録し、"
            "取得したAPIキーを環境変数 SERPER_API_KEY に設定してください。"
        )

    on_progress(f"検索中: {keyword}")
    urls: list[str] = []
    seen = set()

    for page_num in range(1, SERPER_MAX_PAGES + 1):
        try:
            resp = requests.post(
                SERPER_SEARCH_API_URL,
                json={"q": keyword, "num": SERPER_RESULTS_PER_PAGE, "page": page_num},
                headers={
                    "X-API-KEY": api_key,
                    "Content-Type": "application/json",
                },
                timeout=30,
            )
        except requests.RequestException as e:
            on_progress(f"  -> 検索APIへの接続に失敗しました: {e}")
            break

        if resp.status_code == 401 or resp.status_code == 403:
            raise ScrapeError("Serper APIキーが無効です。設定したAPIキーを確認してください。")
        if resp.status_code == 429:
            on_progress("  -> Serper APIのレート制限/無料クレジット上限に達しました。ここまでの結果で続行します。")
            break
        if resp.status_code != 200:
            on_progress(f"  -> 検索APIエラー（status={resp.status_code}）: {resp.text[:200]}")
            break

        try:
            data = resp.json()
        except ValueError:
            on_progress("  -> 検索APIの応答を解析できませんでした。")
            break

        organic = data.get("organic") or []
        if not organic:
            break

        new_count = 0
        for r in organic:
            url = r.get("link")
            if url and url not in seen:
                seen.add(url)
                urls.append(url)
                new_count += 1

        if new_count == 0:
            break

        on_progress(f"  -> ここまでで {len(urls)} 件のページを発見。続きを確認します…")

    on_progress(f"  -> 検索結果 {len(urls)} 件を対象にします。")
    return urls


# ブラウザ内(JS)で実行し、キーワードを含む「塊」を拾ってくる関数。
# 1. まず各キーワードについて、一致する最も内側の要素（1行程度）を見つける。
# 2. そこから、意味のあるまとまり（段落程度）になるまで親要素を数階層たどって
#    範囲を広げる（1行だけだと情報が少なすぎるため）。
# 3. 複数のキーワードが広げた結果、同じ段落に行き着いた場合は1件にまとめ、
#    キーワードを併記する（重複データを作らないため）。
_EXTRACT_JS = """
(args) => {
    const keywords = args.keywords;
    const minLen = args.minLen;
    const maxLen = args.maxLen;
    const skipTags = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "HEAD"]);
    const all = document.querySelectorAll("body *");

    // 1. 各キーワードについて、一致する最も内側の要素を集める
    const leafMatches = [];
    for (const el of all) {
        if (skipTags.has(el.tagName)) continue;
        const text = (el.innerText || "").replace(/\\s+/g, " ").trim();
        if (!text || text.length < minLen) continue;

        for (const kw of keywords) {
            if (!text.includes(kw)) continue;
            let hasMatchingChild = false;
            for (const child of el.children) {
                const childText = (child.innerText || "").replace(/\\s+/g, " ").trim();
                if (childText.length >= minLen && childText.includes(kw)) {
                    hasMatchingChild = true;
                    break;
                }
            }
            if (hasMatchingChild) continue;
            leafMatches.push({el: el, keyword: kw});
        }
    }

    // 2. 段落程度のまとまりになるまで親要素をたどって範囲を広げる
    //    （広げすぎて無関係な内容まで含めないよう、上限も設ける）
    const containers = new Map(); // 要素 -> {keywords: Set, text: string}
    for (const {el, keyword} of leafMatches) {
        let container = el;
        // 広げすぎて無関係な兄弟要素（別の商品カード等）まで含めてしまわないよう、
        // 親を1階層だけたどる（多くのサイトで、これが「1件分の情報」の
        // カード/行を囲む単位になっていることが多い）。
        for (let hops = 0; hops < 1; hops++) {
            const parent = container.parentElement;
            if (!parent) break;
            const parentText = (parent.innerText || "").replace(/\\s+/g, " ").trim();
            if (!parentText || parentText.length > maxLen * 1.5) break;
            container = parent;
        }

        if (containers.has(container)) {
            containers.get(container).keywords.add(keyword);
        } else {
            const containerText = (container.innerText || "").replace(/\\s+/g, " ").trim();
            containers.set(container, {
                keywords: new Set([keyword]),
                text: containerText.length > maxLen ? containerText.slice(0, maxLen) + "…" : containerText,
            });
        }
    }

    // 3. 画像・動画の関連付け（まとめた段落の中、またはその近くの親要素まで）
    //    と、テキスト内容そのものによる重複排除を行いつつ結果を組み立てる
    const seenText = new Set();
    const results = [];
    for (const [container, data] of containers.entries()) {
        const dedupeKey = data.text.slice(0, 120);
        if (seenText.has(dedupeKey)) continue;
        seenText.add(dedupeKey);

        let imageUrl = "";
        let videoUrl = "";
        let mediaContainer = container;
        for (let hops = 0; hops < 3 && mediaContainer; hops++) {
            const img = mediaContainer.querySelector("img[src]");
            const video = mediaContainer.querySelector("video");
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
            mediaContainer = mediaContainer.parentElement;
        }

        results.push({
            keyword: Array.from(data.keywords).join(", "),
            text: data.text,
            imageUrl: imageUrl,
            videoUrl: videoUrl,
        });
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
    urls = _serper_search_urls(cfg.search_keyword, on_progress)
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
                    # サイト内の別ページに同じ文章（フッターの住所など）が
                    # 繰り返し出てくることがあるため、テキスト内容だけで
                    # サイト全体を通して重複排除する。
                    dedupe_key = m["text"]
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
