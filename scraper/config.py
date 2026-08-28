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
class SearchConfig:
    """1つの検索・抽出設定。"""
    name: str
    search_keyword: str                        # Web検索にかけるキーワード
    extract_keywords: list[str] = field(default_factory=list)  # ページ内で探すキーワード
    max_pages: int = 5                          # 検索結果から何件のページを開くか
    max_snippet_chars: int = 400                # 抜粋テキストの最大文字数

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "SearchConfig":
        return SearchConfig(
            name=d["name"],
            search_keyword=d.get("search_keyword", ""),
            extract_keywords=list(d.get("extract_keywords", [])),
            max_pages=d.get("max_pages", 5),
            max_snippet_chars=d.get("max_snippet_chars", 400),
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
