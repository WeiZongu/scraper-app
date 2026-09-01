"""
検索設定（SearchConfig）の定義と、
site_configs/ 配下の JSON ファイルとの読み書きを行うモジュール。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path

CONFIG_DIR = Path(__file__).resolve().parent.parent / "site_configs"
CONFIG_DIR.mkdir(exist_ok=True)


@dataclass
class ExtractField:
    """1つの抽出項目（＝結果の表の1列）。
    title: 表の列名として表示される名前（例: "価格"）。
    keyword: AI（Gemini）にその列で何を探すか伝える説明（自然言語のヒント）。
             例: "価格" や "出発日"。"*" のみの場合は列名から連想される情報を
             自由に埋めるようAIに指示する。
    """
    title: str
    keyword: str

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "ExtractField":
        return ExtractField(title=d.get("title", ""), keyword=d.get("keyword", ""))


@dataclass
class SearchConfig:
    """1つの検索・抽出設定。"""
    name: str
    search_keyword: str                        # Web検索にかけるキーワード
    extract_fields: list[ExtractField] = field(default_factory=list)  # 抽出項目（列）

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "search_keyword": self.search_keyword,
            "extract_fields": [f.to_dict() for f in self.extract_fields],
        }

    @staticmethod
    def from_dict(d: dict) -> "SearchConfig":
        if "extract_fields" in d:
            extract_fields = [ExtractField.from_dict(f) for f in d.get("extract_fields", [])]
        else:
            # 旧形式（抽出キーワードがカンマ区切りの文字列リストだった頃）からの移行。
            # タイトルとキーワードが分かれていなかったため、同じ文字列を両方に使う。
            extract_fields = [ExtractField(title=kw, keyword=kw) for kw in d.get("extract_keywords", [])]
        return SearchConfig(
            name=d["name"],
            search_keyword=d.get("search_keyword", ""),
            extract_fields=extract_fields,
        )


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", name.strip(), flags=re.UNICODE)
    return slug or "search"


def config_path(name: str) -> Path:
    return CONFIG_DIR / f"{_slug(name)}.json"


def save_config(cfg: SearchConfig) -> Path:
    path = config_path(cfg.name)
    path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_config(path: Path) -> SearchConfig:
    d = json.loads(path.read_text(encoding="utf-8"))
    return SearchConfig.from_dict(d)


def list_configs() -> list[SearchConfig]:
    configs = []
    for p in sorted(CONFIG_DIR.glob("*.json")):
        try:
            configs.append(load_config(p))
        except Exception:
            continue
    return configs


def delete_config(name: str) -> None:
    p = config_path(name)
    if p.exists():
        p.unlink()
