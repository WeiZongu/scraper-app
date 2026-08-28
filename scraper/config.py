"""
サイトごとのスクレイピング設定（SiteConfig）の定義と、
site_configs/ 配下の JSON ファイルとの読み書きを行うモジュール。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

CONFIG_DIR = Path(__file__).resolve().parent.parent / "site_configs"
CONFIG_DIR.mkdir(exist_ok=True)


@dataclass
class FieldConfig:
    """1つの取得項目（列）の設定。"""
    name: str                 # 帳票上の列名（例: "タイトル", "価格"）
    selector: str              # item_selector からの相対 CSS セレクタ
    attr: str = "text"         # "text" / "html" / 属性名 (例: "href", "src")
    numeric: bool = False      # 数値として集計対象にするか（円マークやカンマは自動除去）

    def to_dict(self) -> dict:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict) -> "FieldConfig":
        return FieldConfig(
            name=d["name"],
            selector=d["selector"],
            attr=d.get("attr", "text"),
            numeric=d.get("numeric", False),
        )


@dataclass
class SiteConfig:
    """1サイト分のスクレイピング設定。"""
    name: str
    list_url: str                      # 例: "https://example.com/items?page={page}"
    item_selector: str                 # 1件分の要素を囲むセレクタ
    fields: list[FieldConfig] = field(default_factory=list)

    # ページング設定
    pagination_type: str = "page_number"   # "page_number" / "click_next" / "infinite_scroll" / "none"
    max_pages: int = 5                     # page_number / click_next 用の上限
    next_button_selector: str = ""         # click_next 用
    max_scrolls: int = 10                  # infinite_scroll 用
    scroll_wait_ms: int = 1000             # infinite_scroll でのスクロール後待機時間(ms)

    # 描画待ち設定（JS動的描画サイト用）
    wait_selector: str = ""            # このセレクタが出現するまで待つ（空なら item_selector を使う）
    wait_timeout_ms: int = 15000

    def to_dict(self) -> dict:
        d = asdict(self)
        d["fields"] = [f.to_dict() if isinstance(f, FieldConfig) else f for f in self.fields]
        return d

    @staticmethod
    def from_dict(d: dict) -> "SiteConfig":
        cfg = SiteConfig(
            name=d["name"],
            list_url=d["list_url"],
            item_selector=d["item_selector"],
            fields=[FieldConfig.from_dict(f) for f in d.get("fields", [])],
            pagination_type=d.get("pagination_type", "page_number"),
            max_pages=d.get("max_pages", 5),
            next_button_selector=d.get("next_button_selector", ""),
            max_scrolls=d.get("max_scrolls", 10),
            scroll_wait_ms=d.get("scroll_wait_ms", 1000),
            wait_selector=d.get("wait_selector", ""),
            wait_timeout_ms=d.get("wait_timeout_ms", 15000),
        )
        return cfg


def _slug(name: str) -> str:
    slug = re.sub(r"[^\w\-]+", "_", name.strip(), flags=re.UNICODE)
    return slug or "site"


def config_path(name: str) -> Path:
    return CONFIG_DIR / f"{_slug(name)}.json"


def save_config(cfg: SiteConfig) -> Path:
    path = config_path(cfg.name)
    path.write_text(json.dumps(cfg.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def load_config(path: Path) -> SiteConfig:
    d = json.loads(path.read_text(encoding="utf-8"))
    return SiteConfig.from_dict(d)


def list_configs() -> list[SiteConfig]:
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
