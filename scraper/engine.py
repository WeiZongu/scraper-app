"""
検索キーワードからWebを検索し、各ページのDOM内から抽出項目（列）のいずれか
1つでも見つかった(OR条件)「1件のレコード」を行データとして抜き出すエンジン。
CSSセレクタの指定なしで使えるように、要素の特定はキーワード一致で行う。
各抽出項目は「タイトル（表の列名）」と「キーワード（実際に探すパターン、
"*" によるワイルドカード可）」の組で、抽出項目がそのまま表の列になり、
各行はタイトルごとの値（該当箇所の文・段落。一致しなかった列は空欄）を持つ
key:value の組み合わせになる。

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


# ブラウザ内(JS)で実行し、抽出キーワードを「列」とするレコード（行）を拾ってくる関数。
# 1. まず各キーワード（グループ内のいずれかの表記に一致すればOK。多言語対応のため）に
#    ついて、一致する最も内側の要素（1文・1段落程度）を見つける。
# 2. そこから親要素を1階層たどって「1件分の情報」とみなせる範囲（レコード）を求め、
#    同じ範囲に属する各キーワードの一致テキストをまとめる。
# 3. いずれか1つでもキーワードが見つかったレコードを結果として返す（OR条件）。
#    一致しなかった列は空欄になる。
_EXTRACT_JS = """
(args) => {
    const keywordGroups = args.keywordGroups; // [{original, variants: [...]}, ...]
    const minLen = args.minLen;
    const maxLen = args.maxLen;
    const skipTags = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "HEAD"]);
    const all = document.querySelectorAll("body *");

    // "*" をワイルドカードとして扱う正規表現に変換する（それ以外の正規表現
    // 特殊文字はすべてエスケープし、単純な文字列として扱う）。
    const escapeRegExp = (s) => s.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&");
    const patternToRegex = (pattern) => new RegExp(pattern.split("*").map(escapeRegExp).join(".*"));
    for (const group of keywordGroups) {
        group.regexes = group.variants.map(patternToRegex);
    }

    // 1. 各キーワードグループについて、一致する最も内側の要素を集める
    //    （グループ内のいずれかの表記＝元の言語 or 翻訳後の語、ワイルドカード込みで
    //    一致すればよい）。0件になった際の原因調査用に、キーワードごとの
    //    単独一致件数（グループ化は無視）も数えておく。
    const leafMatches = [];
    const matchCounts = {};
    for (const group of keywordGroups) matchCounts[group.original] = 0;
    for (const el of all) {
        if (skipTags.has(el.tagName)) continue;
        const text = (el.innerText || "").replace(/\\s+/g, " ").trim();
        if (!text || text.length < minLen) continue;

        for (const group of keywordGroups) {
            if (!group.regexes.some(re => re.test(text))) continue;
            let hasMatchingChild = false;
            for (const child of el.children) {
                const childText = (child.innerText || "").replace(/\\s+/g, " ").trim();
                if (childText.length >= minLen && group.regexes.some(re => re.test(childText))) {
                    hasMatchingChild = true;
                    break;
                }
            }
            if (hasMatchingChild) continue;
            matchCounts[group.original]++;
            const snippet = text.length > maxLen ? text.slice(0, maxLen) + "…" : text;
            leafMatches.push({el: el, keyword: group.original, text: snippet});
        }
    }

    // 2. 「1件のレコード」とみなせる範囲（親要素1階層分）でグループ化する
    //    （広げすぎて無関係な兄弟要素＝別のレコードまで含めないよう、上限も設ける）
    const records = new Map(); // 要素 -> Map<keyword, text>
    for (const {el, keyword, text} of leafMatches) {
        let container = el;
        for (let hops = 0; hops < 1; hops++) {
            const parent = container.parentElement;
            if (!parent) break;
            const parentText = (parent.innerText || "").replace(/\\s+/g, " ").trim();
            if (!parentText || parentText.length > maxLen * 1.5) break;
            container = parent;
        }

        if (!records.has(container)) {
            records.set(container, new Map());
        }
        const values = records.get(container);
        if (!values.has(keyword)) {
            values.set(keyword, text);
        }
    }

    // 3. いずれか1つでもキーワードが見つかったレコードを結果とする（OR条件）。
    //    一致しなかった列は空欄になる。画像・動画も付けて結果を組み立て、
    //    内容が完全に同じレコードは重複排除する。
    const seenSignature = new Set();
    const results = [];
    for (const [container, values] of records.entries()) {
        const columns = {};
        for (const group of keywordGroups) columns[group.original] = values.get(group.original) || "";

        const signature = keywordGroups.map(g => columns[g.original]).join("\\u0001");
        if (seenSignature.has(signature)) continue;
        seenSignature.add(signature);

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

        results.push({columns: columns, imageUrl: imageUrl, videoUrl: videoUrl});
    }
    return {records: results, matchCounts: matchCounts};
}
"""


def _extract_matches(page: Page, keyword_groups: list[dict], max_snippet_chars: int) -> dict:
    """{"records": [...OR条件で見つかった行...], "matchCounts": {タイトル: 単独一致件数}} を返す。
    matchCountsはグルーピングを無視した単独一致件数で、0件になった際にどの
    キーワードが原因かを調べるための診断用データ。
    """
    return page.evaluate(
        _EXTRACT_JS,
        {"keywordGroups": keyword_groups, "minLen": MIN_SNIPPET_CHARS, "maxLen": max_snippet_chars},
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


def _merge_column_median(texts: list[str]) -> str | None:
    """同じ列の複数のテキストが、数値部分だけ違う表記ゆれかどうかを判定し、
    そうであれば数値部分を中央値に置き換えたテキストを返す。数値の個数が
    そろわない（＝数値以外の内容も違う）場合は None を返す。
    """
    number_lists = [_NUMBER_RE.findall(t) for t in texts]
    n_numbers = len(number_lists[0])
    if n_numbers == 0 or any(len(nl) != n_numbers for nl in number_lists):
        return None

    medians = [_median_numeric_string([nl[i] for nl in number_lists]) for i in range(n_numbers)]
    parts = _NUMBER_RE.split(texts[0])
    rebuilt = parts[0]
    for part, med in zip(parts[1:], medians):
        rebuilt += med + part
    return rebuilt


def _consolidate_near_duplicates(
    rows: list[dict], column_titles: list[str], on_progress: ProgressCallback
) -> list[dict]:
    """各列（抽出項目）の値が、数値部分だけ微妙に異なる表記ゆれの行
    （別ページに載っている同じ情報等）を1件にまとめ、該当する列の数値部分を
    中央値に置き換える。数値以外の内容が異なる列がある場合は別の情報とみなし、
    その列は元の値のまま残す（＝別の行としては扱われる）。"""
    groups: dict[tuple[str, ...], list[dict]] = {}
    order: list[tuple[str, ...]] = []
    for row in rows:
        key = tuple(_normalize_digits(row["columns"].get(t, "")) for t in column_titles)
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

        merged_columns = dict(group[0]["columns"])
        any_merged = False
        for t in column_titles:
            texts = [r["columns"].get(t, "") for r in group]
            merged_text = _merge_column_median(texts)
            if merged_text is not None:
                merged_columns[t] = merged_text
                any_merged = True

        merged_row = dict(group[0])
        merged_row["columns"] = merged_columns
        consolidated.append(merged_row)
        if any_merged:
            merged_count += len(group) - 1

    if merged_count:
        on_progress(f"  -> 数値だけ異なる重複 {merged_count} 件を中央値でまとめました。")
    return consolidated


def _translate_variants(
    pattern: str, languages: list[str], on_progress: ProgressCallback
) -> list[str]:
    """1つのキーワード（パターン）を指定言語に翻訳し、[元のパターン, 翻訳後の
    表記, ...] のリストを返す。複数言語で検索・抽出できるようにするための処理。
    "*" を含むワイルドカードパターンは自然言語のフレーズではないため翻訳の
    対象外とする（翻訳すると意味が壊れるため）。
    翻訳は無料の非公式サービスに依存しており失敗することがあるが、その場合は
    そのパターン・言語の組だけ翻訳なしでスキップし、処理は継続する。
    """
    variants = [pattern]
    if "*" in pattern:
        return variants

    translated_count = 0
    for lang in languages:
        translated = translate_text(pattern, lang)
        if translated and translated not in variants:
            variants.append(translated)
            translated_count += 1
            on_progress(f"  -> 翻訳({lang}): 「{pattern}」→「{translated}」")

    if languages and not translated_count:
        on_progress(
            f"  -> 「{pattern}」の翻訳にすべて失敗しました"
            "（翻訳サービスが利用できない可能性があります）。元の表記のみで続行します。"
        )
    return variants


def scrape(
    cfg: SearchConfig,
    on_progress: ProgressCallback = _noop,
    headless: bool = True,
) -> list[dict]:
    """
    検索キーワードでWebを検索し、上位ページを開いて抽出項目（列）のいずれか
    1つでも見つかったレコードを集める（OR条件。一致しなかった列は空欄になる）。
    行データのリストを返す。
    """
    fields = [
        (f.title.strip() or f.keyword.strip(), f.keyword.strip())
        for f in cfg.extract_fields
        if f.keyword.strip()
    ]
    if not fields:
        raise ScrapeError("抽出キーワードを1つ以上入力してください。")
    column_titles = [title for title, _ in fields]

    # 抽出キーワード（パターン）を固定の対象言語に翻訳し、元言語以外のページでも
    # 一致するようにする。一致した結果はどの言語（表記）で一致しても、常に
    # 指定したタイトルの列として扱う。
    on_progress(f"抽出キーワードを {', '.join(TRANSLATE_LANGUAGES)} に翻訳しています…")
    keyword_groups = [
        {"original": title, "variants": _translate_variants(keyword, TRANSLATE_LANGUAGES, on_progress)}
        for title, keyword in fields
    ]

    # 検索キーワードも同様に翻訳し、それぞれの言語で個別に検索してURLを集める
    # （検索エンジンは基本的に検索キーワードと同じ言語のページを優先的に返すため）。
    search_keywords = _translate_variants(cfg.search_keyword, TRANSLATE_LANGUAGES, on_progress)

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
    seen_signatures: set[tuple[str, ...]] = set()
    # 最終的に0件だった場合に「AND条件のうちどのキーワードがボトルネックか」を
    # 診断できるよう、AND条件・ページをまたいだグルーピングを無視した
    # キーワード単独の一致件数（全ページ合計）を集計しておく。
    total_match_counts: dict[str, int] = {t: 0 for t in column_titles}
    pages_processed = 0

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
                        extraction = _extract_matches(page, keyword_groups, cfg.max_snippet_chars)
                    except Exception as e:
                        on_progress(f"  -> 抽出失敗: {e}")
                        continue

                    pages_processed += 1
                    for t, count in extraction.get("matchCounts", {}).items():
                        total_match_counts[t] = total_match_counts.get(t, 0) + count

                    added = 0
                    for m in extraction.get("records", []):
                        columns = {t: m["columns"].get(t, "") for t in column_titles}
                        # サイト内の別ページに同じレコード（フッターの住所など）が
                        # 繰り返し出てくることがあるため、全列の内容だけで
                        # サイト全体を通して重複排除する。
                        dedupe_key = tuple(columns[t] for t in column_titles)
                        if dedupe_key in seen_signatures:
                            continue
                        seen_signatures.add(dedupe_key)
                        results.append({
                            "columns": columns,
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

    results = _consolidate_near_duplicates(results, column_titles, on_progress)

    if not results and pages_processed:
        # OR条件なので、いずれかのキーワードが1件でも見つかれば結果は0件にならない。
        # 0件ということは全キーワードが取得したページ内に見つからなかったことを
        # 意味するため、キーワードごとの内訳を見せて原因調査の手がかりにする
        # （表記が違う、検索結果のページ内容とキーワードが噛み合っていない等）。
        breakdown = "、".join(f"{t}: {c}件" for t, c in total_match_counts.items())
        on_progress(f"  -> 0件でした。参考: 各抽出項目の一致件数 — {breakdown}")
        on_progress(
            "  -> すべて0件の場合、取得したページに抽出キーワードが含まれていない"
            "（表記が違う、検索結果のページ内容と合っていない等）可能性があります。"
        )

    on_progress(f"完了: 合計 {len(results)} 件取得しました。")
    return results
