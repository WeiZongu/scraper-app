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
import re
from datetime import datetime
from statistics import median
from typing import Callable

import requests
from playwright.sync_api import sync_playwright, Page

from .config import SearchConfig
from .translate import translate_text

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

# 検索・抽出キーワードを常にこれらの言語にも翻訳し、多言語で検索・抽出する
# （日本語(ja)以外のページも取りこぼさないようにするための固定リスト）。
# 日本語自体への翻訳は自分自身への変換になり何も起きないため含めていない。
TRANSLATE_LANGUAGES = ["en", "zh-TW", "zh-CN", "ko", "es"]


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


# 数値だけ微妙に異なる重複情報（例:「営業時間 11:00〜21:00」と「営業時間 11:00〜22:00」）を
# 1件にまとめ、代表値として数値部分の中央値を採用するための処理。
_NUMBER_RE = re.compile(r"\d+(?:[:.,]\d+)*")


def _normalize_digits(text: str) -> str:
    """数値部分を "#" に置き換えた文字列を返す。これが一致する行同士だけを
    「数値だけ違う重複」とみなしてグルーピングする（無関係な情報を誤って
    まとめてしまわないようにするため）。"""
    return _NUMBER_RE.sub("#", text)


def _median_numeric_string(raw_values: list[str]) -> str:
    """"11:30" のような時刻表記と、通常の数値表記の両方に対応した中央値を返す。"""
    if all(re.fullmatch(r"\d{1,2}:\d{2}", v) for v in raw_values):
        minutes = []
        for v in raw_values:
            h, m = v.split(":")
            minutes.append(int(h) * 60 + int(m))
        med = int(round(median(minutes)))
        return f"{med // 60:02d}:{med % 60:02d}"

    try:
        numbers = [float(v.replace(",", "")) for v in raw_values]
        med = median(numbers)
        return str(int(med)) if med == int(med) else str(med)
    except ValueError:
        pass

    sorted_values = sorted(raw_values)
    return sorted_values[len(sorted_values) // 2]


def _consolidate_near_duplicates(rows: list[dict], on_progress: ProgressCallback) -> list[dict]:
    """同じキーワードで、数値部分だけが微妙に異なる行（別ページに載っている
    同じ情報の表記ゆれ等）を1件にまとめ、数値部分は中央値に置き換える。
    数値以外の部分が異なる場合は別の情報とみなし、まとめない。"""
    groups: dict[tuple[str, str], list[dict]] = {}
    order: list[tuple[str, str]] = []
    for row in rows:
        key = (row["keyword"], _normalize_digits(row["text"]))
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(row)

    consolidated: list[dict] = []
    merged_count = 0
    for key in order:
        group = groups[key]
        if len(group) == 1:
            consolidated.append(group[0])
            continue

        number_lists = [_NUMBER_RE.findall(r["text"]) for r in group]
        n_numbers = len(number_lists[0])
        if n_numbers == 0 or any(len(nl) != n_numbers for nl in number_lists):
            # 数値の個数が揃わない場合はまとめ方が判断できないため、そのまま残す
            consolidated.extend(group)
            continue

        medians = [_median_numeric_string([nl[i] for nl in number_lists]) for i in range(n_numbers)]
        template = group[0]["text"]
        parts = _NUMBER_RE.split(template)
        rebuilt = parts[0]
        for part, med in zip(parts[1:], medians):
            rebuilt += med + part

        merged_row = dict(group[0])
        merged_row["text"] = rebuilt
        consolidated.append(merged_row)
        merged_count += len(group) - 1

    if merged_count:
        on_progress(f"  -> 数値だけ異なる重複 {merged_count} 件を中央値でまとめました。")
    return consolidated


def _build_keyword_variants(
    keywords: list[str], languages: list[str], on_progress: ProgressCallback
) -> dict[str, str]:
    """各キーワードを指定言語に翻訳し、"翻訳後の語 -> 元のキーワード" のマップを
    返す（元のキーワード自身も自分自身にマップする）。複数言語で検索・抽出できる
    ようにするための処理で、結果には常に元のキーワードで表示するために使う。
    翻訳は無料の非公式サービスに依存しており失敗することがあるが、その場合は
    そのキーワード・言語の組だけ翻訳なしでスキップし、処理は継続する。
    """
    variant_to_original: dict[str, str] = {}
    translated_count = 0
    attempted_count = 0
    for kw in keywords:
        variant_to_original.setdefault(kw, kw)
        for lang in languages:
            attempted_count += 1
            translated = translate_text(kw, lang)
            if translated and translated not in variant_to_original:
                variant_to_original[translated] = kw
                translated_count += 1
                on_progress(f"  -> 翻訳({lang}): 「{kw}」→「{translated}」")

    if attempted_count and not translated_count:
        on_progress(
            "  -> 翻訳にすべて失敗しました（翻訳サービスが利用できない可能性があります）。"
            "元のキーワードのみで続行します。"
        )
    return variant_to_original


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

    # 抽出キーワードを固定の対象言語に翻訳し、元言語以外のページでも一致するようにする。
    # マッチした結果はどの言語のキーワードで一致しても、常に元のキーワードで表示する。
    on_progress(f"抽出キーワードを {', '.join(TRANSLATE_LANGUAGES)} に翻訳しています…")
    keyword_variants = _build_keyword_variants(keywords, TRANSLATE_LANGUAGES, on_progress)
    keywords_for_matching = list(keyword_variants.keys())

    # 検索キーワードも同様に翻訳し、それぞれの言語で個別に検索してURLを集める
    # （検索エンジンは基本的に検索キーワードと同じ言語のページを優先的に返すため）。
    sk_variants = _build_keyword_variants([cfg.search_keyword], TRANSLATE_LANGUAGES, on_progress)
    search_keywords = list(sk_variants.keys())

    # 検索はAPI呼び出しのみでブラウザ不要なため、Playwright起動前に済ませる
    # （APIキー未設定などで早期に失敗する場合、ブラウザを起動する無駄を避けられる）。
    urls: list[str] = []
    seen_urls: set[str] = set()
    for sk in search_keywords:
        for url in _serper_search_urls(sk, on_progress):
            if url not in seen_urls:
                seen_urls.add(url)
                urls.append(url)
    if not urls:
        on_progress("検索結果が見つかりませんでした。")

    results: list[dict] = []
    seen_texts: set[str] = set()

    with sync_playwright() as p:
        def launch():
            # 無料サーバー(メモリ512MB程度)でもOOMで落ちないよう、コンテナ向けの
            # 省メモリオプションを付けてChromiumを起動する。
            b = p.chromium.launch(
                headless=headless,
                args=["--disable-dev-shm-usage", "--disable-gpu", "--no-zygote"],
            )
            c = b.new_context(user_agent=DESKTOP_USER_AGENT, locale="ja-JP")
            # 画像・フォントはDOMのテキストやsrc属性の取得には不要
            # （実際に画面へ描画するわけではないため）。読み込みをブロックすることで
            # メモリ使用量と通信量を削減できる。
            # ※CSS(stylesheet)は innerText が「画面に表示されている文字か」を
            #   CSSの表示状態に基づいて判定するため、ブロックすると隠し要素まで
            #   拾ってしまい抽出結果が変わる可能性があるため対象外にする。
            # ※動画(media)は video.currentSrc の解決に読み込みが関わる場合があるため
            #   同様に対象外にする（件数も画像ほど多くなく、影響は小さい）。
            c.route(
                "**/*",
                lambda route: route.abort()
                if route.request.resource_type in ("image", "font")
                else route.continue_(),
            )
            return b, c

        # Chromiumは長時間・大量ページを開き続けるとネイティブ側のメモリ使用量が
        # 徐々に増えていく傾向があるため（OSにすぐ返却されない等）、一定件数ごとに
        # ブラウザごと再起動してメモリを解放する。
        BROWSER_RESTART_EVERY = 15
        browser, context = launch()

        try:
            for i, url in enumerate(urls, start=1):
                if i > 1 and (i - 1) % BROWSER_RESTART_EVERY == 0:
                    on_progress("  -> メモリ解放のためブラウザを再起動します…")
                    context.close()
                    browser.close()
                    browser, context = launch()

                on_progress(f"[{i}/{len(urls)}] ページを取得中: {url}")
                # ページ単位で使い切ったら閉じることで、そのページが保持していた
                # メモリ（DOM・画像バッファ等）を都度解放する。
                page = context.new_page()
                try:
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
                        matches = _extract_matches(page, keywords_for_matching, cfg.max_snippet_chars)
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
                        # 翻訳したキーワード（他言語）が一致した場合も、表示上は
                        # 常に元のキーワードにまとめる（結果やサマリーが言語ごとに
                        # 分散してしまわないようにするため）。
                        matched_keyword = ", ".join(dict.fromkeys(
                            keyword_variants.get(k, k) for k in m["keyword"].split(", ")
                        ))
                        results.append({
                            "keyword": matched_keyword,
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
                    page.close()

        finally:
            context.close()
            browser.close()

    results = _consolidate_near_duplicates(results, on_progress)

    on_progress(f"完了: 合計 {len(results)} 件取得しました。")
    return results
