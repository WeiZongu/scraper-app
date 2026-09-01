"""
Gemini API（Google AI Studio。クレジットカード登録不要の無料枠で利用可能）を
使って、ページのテキストから抽出項目を意味理解ベースで抜き出すモジュール。

DOM構造やキーワードの完全一致・ワイルドカードに頼るヒューリスティック方式は、
サイトによって表記ゆれや情報の配置に弱く「うまく抽出できない」ことが多かった
ため、AIによる意味理解ベースの抽出に切り替えた。抽出キーワードは、もはや
厳密な一致パターンではなく「何を探すかの説明（自然言語のヒント）」として
AIに渡される。
"""
from __future__ import annotations

import json
import os
from typing import Callable

import requests

ProgressCallback = Callable[[str], None]

GEMINI_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
GEMINI_MODEL = "gemini-2.0-flash"
# 無料枠のトークン数を抑えるため、ページテキストはこの文字数までしか渡さない。
MAX_PAGE_TEXT_CHARS = 15000
REQUEST_TIMEOUT_SEC = 30


class AiExtractError(Exception):
    pass


def _build_prompt(fields: list[tuple[str, str]], page_text: str) -> str:
    field_lines = "\n".join(f'- 「{title}」: {keyword}' for title, keyword in fields)
    return f"""あなたはWebページのテキストから情報を抽出するアシスタントです。
以下のページのテキストの中から、次の項目を持つ「レコード」
（1件分のまとまった情報。例えば1つの商品・1件のフライト・1つの店舗情報など）
をすべて見つけてください。

抽出する項目（列名: 探す内容の説明）:
{field_lines}

ルール:
- ページ内に複数のレコード（例: 複数の商品、複数のフライト候補）があれば、
  それぞれを別々のオブジェクトとして返してください。
- ある項目がそのレコードに存在しない、または読み取れない場合は、
  値を空文字列 "" にしてください（無理に埋めないでください）。
- 項目の説明が "*" だけの場合は、その列名から連想される情報があれば
  自由に埋めてください。
- 値は元のテキストの表記をなるべくそのまま使い、要約や言い換えをしないで
  ください。
- 該当するレコードが1つも見つからない場合は、空の配列を返してください。
- ナビゲーションメニューや広告、関係のない定型文はレコードとして扱わないで
  ください。

ページのテキスト:
---
{page_text}
---
"""


def _response_schema(titles: list[str]) -> dict:
    return {
        "type": "ARRAY",
        "items": {
            "type": "OBJECT",
            "properties": {t: {"type": "STRING"} for t in titles},
            "required": titles,
        },
    }


def check_api_key() -> None:
    """APIキーが設定されているかだけを確認する（実際のAPI呼び出しは行わない、
    ブラウザ起動前の早期チェック用）。"""
    if not os.environ.get("GEMINI_API_KEY", "").strip():
        raise AiExtractError(
            "GEMINI_API_KEY が設定されていません。"
            "Google AI Studio (https://aistudio.google.com/) でAPIキーを取得し、"
            "環境変数 GEMINI_API_KEY に設定してください。"
        )


def extract_records(
    fields: list[tuple[str, str]],
    page_text: str,
    on_progress: ProgressCallback = lambda _msg: None,
) -> list[dict[str, str]]:
    """fields: [(タイトル, キーワード/説明), ...]。ページのテキストからAIで
    レコードを抽出し、[{タイトル: 値, ...}, ...] のリストを返す。
    APIキー未設定やAPIエラーの場合は AiExtractError を送出する
    （呼び出し側はそのページだけスキップする想定）。
    """
    check_api_key()
    api_key = os.environ["GEMINI_API_KEY"].strip()

    text = (page_text or "").strip()
    if not text:
        return []
    if len(text) > MAX_PAGE_TEXT_CHARS:
        text = text[:MAX_PAGE_TEXT_CHARS]

    titles = [title for title, _ in fields]
    prompt = _build_prompt(fields, text)

    url = GEMINI_API_URL.format(model=GEMINI_MODEL)
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {
            "responseMimeType": "application/json",
            "responseSchema": _response_schema(titles),
            "temperature": 0,
        },
    }

    try:
        resp = requests.post(url, params={"key": api_key}, json=body, timeout=REQUEST_TIMEOUT_SEC)
    except requests.RequestException as e:
        raise AiExtractError(f"Gemini APIへの接続に失敗しました: {e}") from e

    if resp.status_code == 429:
        raise AiExtractError("Gemini APIのレート制限（無料枠の上限）に達しました。しばらく待ってから再実行してください。")
    if resp.status_code in (400, 401, 403):
        raise AiExtractError(f"Gemini APIキーが無効か、リクエストが拒否されました（status={resp.status_code}）: {resp.text[:200]}")
    if resp.status_code != 200:
        raise AiExtractError(f"Gemini APIエラー（status={resp.status_code}）: {resp.text[:300]}")

    try:
        data = resp.json()
        raw_text = data["candidates"][0]["content"]["parts"][0]["text"]
        records = json.loads(raw_text)
    except (KeyError, IndexError, ValueError, TypeError) as e:
        raise AiExtractError(f"Gemini応答の解析に失敗しました: {e}") from e

    if not isinstance(records, list):
        return []

    results = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        results.append({t: str(rec.get(t) or "").strip() for t in titles})
    return results
