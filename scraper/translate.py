"""
キーワードを他言語に翻訳するための、無料・登録不要な簡易翻訳ヘルパー。

正式なクラウド翻訳API（Google Cloud Translation等）は無料枠を超えると
クレジットカード登録が必要になるため、ここでは MyMemory Translation API
(https://mymemory.translated.net/) を使う。IPアドレスごとに1日あたり
無料で一定量（未登録で約5,000語/日）まで翻訳でき、キー登録・カード登録は
不要。

※ 無料・非公式サービスに依存する機能のため、利用量やサーバー環境によっては
翻訳が失敗することがある（本アプリではこれまでにも無料検索API等で同様の
制約に直面してきた）。失敗した場合は None を返し、呼び出し元は翻訳前の
キーワードのみで動作を継続する（機能が完全に止まることはない）。
"""
from __future__ import annotations

import requests

_TRANSLATE_URL = "https://api.mymemory.translated.net/get"
_TIMEOUT_SEC = 8


def translate_text(text: str, target_lang: str, source_lang: str = "ja") -> str | None:
    """textを source_lang(既定: 日本語) から target_lang（"en", "zh-TW", "ko" 等）に
    翻訳する。失敗した場合や、翻訳結果が元のテキストと変わらなかった場合は
    None を返す。
    """
    text = text.strip()
    if not text or not target_lang.strip():
        return None
    try:
        resp = requests.get(
            _TRANSLATE_URL,
            params={"q": text, "langpair": f"{source_lang}|{target_lang.strip()}"},
            timeout=_TIMEOUT_SEC,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        if data.get("responseStatus") not in (200, "200"):
            return None
        translated = (data.get("responseData") or {}).get("translatedText", "").strip()
        if not translated or translated.lower() == text.lower():
            return None
        return translated
    except Exception:
        return None
