"""
調査カテゴリ別のドメイン辞書。

重要:
- 逐語本文（Segment.text）を変更しない
- raw transcript を変更しない
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

_GLOSSARY_DIR = Path(__file__).resolve().parent.parent / "config" / "glossaries"
_CACHE: dict[str, dict[str, Any]] = {}


def _norm(text: str) -> str:
    t = (text or "").lower()
    t = re.sub(r"[\s\-_/　・.,、。!！?？:：;；()（）\[\]\"'「」『』]+", "", t)
    return t


def load_glossary(profile: str | None) -> dict[str, Any]:
    p = (profile or "general").strip().lower() or "general"
    if p in _CACHE:
        return _CACHE[p]

    path = _GLOSSARY_DIR / f"{p}.json"
    if not path.exists():
        path = _GLOSSARY_DIR / "general.json"

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        data = {"profile": "general", "items": []}

    if not isinstance(data, dict):
        data = {"profile": "general", "items": []}
    if not isinstance(data.get("items"), list):
        data["items"] = []

    _CACHE[p] = data
    return data


def normalize_term(term: str, profile: str | None) -> dict[str, Any] | None:
    src = (term or "").strip()
    if not src:
        return None

    src_norm = _norm(src)
    if not src_norm:
        return None

    glossary = load_glossary(profile)
    for item in glossary.get("items", []):
        if not isinstance(item, dict):
            continue
        canonical = (item.get("canonical") or "").strip()
        if not canonical:
            continue
        aliases = item.get("aliases") or []
        display = (item.get("display") or canonical).strip()
        search_keyword = (item.get("search_keyword") or canonical).strip()

        for alias in aliases:
            a = (alias or "").strip()
            if not a:
                continue
            if _norm(a) == src_norm:
                return {
                    "original_term": src,
                    "normalized_term": canonical,
                    "display_text": f"{display}の可能性",
                    "search_keyword": search_keyword,
                    "confidence": 0.9,
                    "source": "domain_glossary",
                    "note": "逐語本文は変更していません",
                }
    return None


def find_glossary_hints(text: str, profile: str | None) -> list[dict[str, Any]]:
    src = text or ""
    if not src:
        return []

    glossary = load_glossary(profile)
    found: list[tuple[int, dict[str, Any]]] = []
    seen = set()

    for item in glossary.get("items", []):
        if not isinstance(item, dict):
            continue
        canonical = (item.get("canonical") or "").strip()
        if not canonical or canonical in seen:
            continue

        aliases = item.get("aliases") or []
        display = (item.get("display") or canonical).strip()
        search_keyword = (item.get("search_keyword") or canonical).strip()

        best_alias = None
        best_pos = None
        for alias in aliases:
            a = (alias or "").strip()
            if not a:
                continue
            idx = src.find(a)
            if idx >= 0 and (best_pos is None or idx < best_pos):
                best_pos = idx
                best_alias = a

        if best_alias is None:
            continue
        seen.add(canonical)
        found.append((best_pos or 0, {
            "original_term": best_alias,
            "normalized_term": canonical,
            "display_text": f"{display}の可能性",
            "search_keyword": search_keyword,
            "confidence": 0.9,
            "source": "domain_glossary",
            "note": "逐語本文は変更していません",
        }))

    found.sort(key=lambda x: x[0])
    return [x[1] for x in found]
