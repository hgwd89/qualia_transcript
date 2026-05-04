"""
発話テキスト中の商品名候補を楽天APIで照合し、補足候補を返す。

注意:
- 逐語本文は変更しない
- 補足は表示レイヤーで利用する
"""
from __future__ import annotations

import json
import re
from difflib import SequenceMatcher
from time import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

import config
from models.setting import AppSetting

_RAKUTEN_ENDPOINTS = [
    "https://openapi.rakuten.co.jp/ichibams/api/IchibaItem/Search/20260401",
    "https://app.rakuten.co.jp/services/api/IchibaItem/Search/20220601",
]
_TOKEN_RE = re.compile(r"[ァ-ヴー]{3,30}|[A-Za-z0-9][A-Za-z0-9+\-]{2,30}")
_NON_PRODUCT_TERMS = {
    "インタビュー", "サービス", "テスト", "会社", "同期", "友人", "休日", "平日",
    "睡眠", "休暇", "上京", "大学", "音声", "会話", "データ", "分析",
}
_CACHE_TTL_SEC = 60 * 60 * 24
_CACHE: dict[tuple[str, str], tuple[float, list[dict[str, Any]]]] = {}


def _get_setting(key: str, default: str = "") -> str:
    return (AppSetting.get(key) or default).strip()


def _provider() -> str:
    v = _get_setting("product_hint_provider", config.PRODUCT_HINT_PROVIDER).lower()
    return v if v in {"none", "rakuten"} else "rakuten"


def _rakuten_app_id() -> str:
    return _get_setting("rakuten_application_id", config.RAKUTEN_APPLICATION_ID)


def _rakuten_access_key() -> str:
    return _get_setting("rakuten_access_key", config.RAKUTEN_ACCESS_KEY)


def _rakuten_affiliate_id() -> str:
    return _get_setting("rakuten_affiliate_id", config.RAKUTEN_AFFILIATE_ID)


def _norm(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[\\s\\-_/　・.,、。!！?？:：;；()（）\\[\\]\"'「」『』]+", "", t)
    return t


def _extract_terms(text: str, limit: int = 4) -> list[str]:
    terms: list[str] = []
    seen = set()
    for token in _TOKEN_RE.findall(text or ""):
        t = token.strip(" 　")
        if not t or t in _NON_PRODUCT_TERMS:
            continue
        if t in seen:
            continue
        seen.add(t)
        terms.append(t)
        if len(terms) >= limit:
            break
    return terms


def _parse_items(payload: dict[str, Any]) -> list[dict[str, Any]]:
    items = payload.get("Items") or payload.get("items") or []
    out: list[dict[str, Any]] = []
    for entry in items:
        row = entry.get("Item") if isinstance(entry, dict) and "Item" in entry else entry
        if not isinstance(row, dict):
            continue
        name = (row.get("itemName") or "").strip()
        url = (row.get("itemUrl") or row.get("affiliateUrl") or "").strip()
        shop = (row.get("shopName") or "").strip()
        if not name:
            continue
        out.append({"item_name": name, "item_url": url, "shop_name": shop})
    return out


def _score_match(term: str, item_name: str) -> float:
    t = _norm(term)
    n = _norm(item_name)
    if not t or not n:
        return 0.0
    if t == n:
        return 1.0
    if t in n:
        return 0.86 if n.startswith(t) else 0.78
    return SequenceMatcher(None, t, n).ratio() * 0.7


def _rakuten_search(term: str, max_hits: int = 5) -> list[dict[str, Any]]:
    app_id = _rakuten_app_id()
    if not app_id:
        return []

    cache_key = ("rakuten", term)
    cached = _CACHE.get(cache_key)
    now = time()
    if cached and now - cached[0] <= _CACHE_TTL_SEC:
        return cached[1]

    params = {
        "applicationId": app_id,
        "keyword": term,
        "format": "json",
        "formatVersion": 2,
        "hits": max_hits,
        "elements": "itemName,itemUrl,shopName",
    }
    access_key = _rakuten_access_key()
    if access_key:
        params["accessKey"] = access_key
    affiliate_id = _rakuten_affiliate_id()
    if affiliate_id:
        params["affiliateId"] = affiliate_id

    last_error: Exception | None = None
    for endpoint in _RAKUTEN_ENDPOINTS:
        url = f"{endpoint}?{urlencode(params)}"
        try:
            with urlopen(url, timeout=8) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
                items = _parse_items(payload)
                _CACHE[cache_key] = (now, items)
                return items
        except (HTTPError, URLError, TimeoutError, ValueError) as e:
            last_error = e
            continue

    # エラーは上位に投げず、補足なしで返す（UI阻害を避ける）
    _CACHE[cache_key] = (now, [])
    if last_error:
        return []
    return []


def lookup_product_hints(text: str, max_hints: int = 1) -> list[dict[str, Any]]:
    """
    発話テキストから商品候補を検索して返す。
    返却例:
    [
      {"term":"ビオレ", "item_name":"...", "item_url":"...", "shop_name":"...", "confidence":0.82}
    ]
    """
    if _provider() != "rakuten":
        return []

    terms = _extract_terms(text, limit=max(1, max_hints * 2))
    hints: list[dict[str, Any]] = []
    for term in terms:
        items = _rakuten_search(term, max_hits=5)
        if not items:
            continue
        scored = []
        for item in items:
            score = _score_match(term, item["item_name"])
            scored.append((score, item))
        scored.sort(key=lambda x: x[0], reverse=True)
        best_score, best = scored[0]
        if best_score < 0.62:
            continue
        hints.append({
            "term": term,
            "item_name": best["item_name"],
            "item_url": best["item_url"],
            "shop_name": best["shop_name"],
            "confidence": round(float(best_score), 3),
        })
        if len(hints) >= max_hints:
            break
    return hints


def render_inline_hint(hint: dict[str, Any]) -> str:
    name = hint.get("item_name") or ""
    if not name:
        return ""
    return f"（この商品と思われる: {name}）"
