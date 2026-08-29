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

# ヘッドレスブラウザだと分かるUser-Agent（"HeadlessChrome"表記）だと
# 検索エンジン側にブロックされやすいため、通常のデスクトップ版と同じ
# User-Agentを明示的に指定する。
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# 検索先候補。1つ目がブロックされたり0件だったりした場合、
# 順番に次の候補を試す（DuckDuckGoはHTML構造が変わることがあるため）。
SEARCH_URL_TEMPLATES = [
    "https://html.duckduckgo.com/html/?q={query}",
    "https://lite.duckduckgo.com/lite/?q={query}",
]

# html.duckduckgo.com は "&s=<開始位置>" を30件区切りで進めることで次ページを取得できる。
# ページ数の上限はユーザーには設けず「無限」に近づけるが、検索結果が本当に尽きた後まで
# 永遠にループし続けないよう、暴走防止の安全上限としてのみ使う。
SEARCH_PAGE_SIZE = 30
SEARCH_OFFSET_SAFETY_LIMIT = 3000  # 100ページ相当

# ページ内でキーワード一致を探す際の、抜粋として妥当とみなすテキスト長の範囲
MIN_SNIPPET_CHARS = 8


class ScrapeError(Exception):
    pass


def _noop(_msg: str) -> None:
    pass


def _extract_result_links(page: Page) -> list[str]:
    """検索結果ページから、外部サイトへのリンクを取り出す。"""
    # html.duckduckgo.com の結果リンクに使われるクラス名（変更される可能性あり）
    hrefs = page.eval_on_selector_all("a.result__a", "els => els.map(e => e.href)")
    if hrefs:
        return hrefs
    # 上記で取れない場合（lite版や構造変更時）は、外部への通常リンクを広めに拾い、
    # 検索エンジン自身へのリンク・トラッキングリンクを除外する。
    hrefs = page.eval_on_selector_all("a[href^='http']", "els => els.map(e => e.href)")
    return [h for h in hrefs if h and "duckduckgo.com" not in h]


def _search_result_urls(page: Page, keyword: str, on_progress: ProgressCallback) -> list[str]:
    """検索結果ページから、見つかる限りのURLを取得する（件数上限なし）。"""
    on_progress(f"検索中: {keyword}")
    urls: list[str] = []
    seen = set()

    # 1つ目の検索先（html.duckduckgo.com）は "&s=<開始位置>" で次ページに進めるため、
    # 新規URLが見つからなくなるまでページをめくり続ける。
    base_template = SEARCH_URL_TEMPLATES[0]
    offset = 0
    empty_streak = 0
    while empty_streak < 2 and offset < SEARCH_OFFSET_SAFETY_LIMIT:
        url = f"{base_template.format(query=quote_plus(keyword))}&s={offset}"
        try:
            page.goto(url, timeout=30000)
            page.wait_for_timeout(800)
        except Exception as e:
            on_progress(f"  -> 検索ページの取得に失敗しました: {e}")
            break

        hrefs = _extract_result_links(page)
        new_count = 0
        for href in hrefs:
            if href and href not in seen:
                seen.add(href)
                urls.append(href)
                new_count += 1

        if new_count == 0:
            empty_streak += 1
        else:
            empty_streak = 0
            on_progress(f"  -> ここまでで {len(urls)} 件のページを発見。続きを確認します…")

        offset += SEARCH_PAGE_SIZE

    if not urls:
        try:
            page_title = page.title()
        except Exception:
            page_title = ""
        on_progress(f"  -> html.duckduckgo.com で0件でした（ページタイトル: 「{page_title}」）。")

        # フォールバック: lite版など、その他の検索先を試す（ページングはしない）
        for template in SEARCH_URL_TEMPLATES[1:]:
            url = template.format(query=quote_plus(keyword))
            try:
                page.goto(url, timeout=30000)
                page.wait_for_timeout(800)
            except Exception as e:
                on_progress(f"  -> 検索ページの取得に失敗しました: {e}")
                continue

            hrefs = _extract_result_links(page)
            for href in hrefs:
                if href and href not in seen:
                    seen.add(href)
                    urls.append(href)

            if urls:
                break

            try:
                page_title = page.title()
            except Exception:
                page_title = ""
            on_progress(f"  -> {template.split('/')[2]} でも0件でした（ページタイトル: 「{page_title}」）。")

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

    results: list[dict] = []
    seen_texts: set[str] = set()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(user_agent=DESKTOP_USER_AGENT, locale="ja-JP")
        page = context.new_page()

        try:
            urls = _search_result_urls(page, cfg.search_keyword, on_progress)
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
