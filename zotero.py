# -*- coding: utf-8 -*-
"""Zotero 7 本地只读 API 联动。

不直接读 zotero.sqlite，不保存 Zotero 密码；通过 Zotero 本机 HTTP API
读取条目、标签、期刊和阅读状态。需 Zotero 正在运行，并允许本机其他应用通信。
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime

from daily import clean_text, normalize_doi, split_terms, title_key

DEFAULT_API = "http://127.0.0.1:23119/api/users/0"
DEFAULT_READ_TAGS = "/done, done, read, 已读"
DEFAULT_READING_TAGS = "/reading, reading, 在读"
DEFAULT_UNREAD_TAGS = "/unread, unread, 未读"

SKIP_TYPES = {"attachment", "note", "annotation"}
_TITLE_STOP = {
    "the", "and", "for", "with", "from", "into", "using", "based", "study",
    "analysis", "research", "paper", "article", "review", "effect", "effects",
    "new", "novel", "towards", "toward", "via", "under", "between", "through",
    "this", "that", "its", "their", "has", "have", "are", "was", "were", "not",
    "high", "low", "data", "model", "models", "method", "methods", "results",
}


class ZoteroError(Exception):
    pass


def normalize_api(value: str | None) -> str:
    text = clean_text(value, 300).strip().rstrip("/") or DEFAULT_API
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ZoteroError("Zotero 本地 API 地址必须是本机的 http://127.0.0.1:23119/…")
    path = parsed.path.rstrip("/")
    if path in ("", "/"):
        path = "/api/users/0"
    elif path == "/api":
        path = "/api/users/0"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def _tag_set(value: str | None, default: str) -> set[str]:
    return {t.casefold() for t in split_terms(value or default)}


def tag_settings(profile: dict) -> tuple[set[str], set[str], set[str]]:
    return (
        _tag_set(profile.get("zotero_read_tags"), DEFAULT_READ_TAGS),
        _tag_set(profile.get("zotero_reading_tags"), DEFAULT_READING_TAGS),
        _tag_set(profile.get("zotero_unread_tags"), DEFAULT_UNREAD_TAGS),
    )


def status_for_tags(tags: list[str], read_tags: set[str], reading_tags: set[str],
                    unread_tags: set[str]) -> str:
    lowered = {str(t).casefold() for t in tags}
    if lowered & read_tags:
        return "read"
    if lowered & reading_tags:
        return "reading"
    if lowered & unread_tags:
        return "unread"
    return "unknown"


def _api_get(url: str, timeout: int = 15):
    req = urllib.request.Request(url, headers={
        "Zotero-API-Version": "3",
        "Accept": "application/json",
        "User-Agent": "Zhixin/1.1 ZoteroLocalBridge",
    })
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.read(), dict(resp.headers)
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read(300).decode("utf-8", "replace")
        except Exception:
            pass
        if exc.code == 403:
            raise ZoteroError("Zotero 拒绝了本地访问。请在 Zotero 设置 → 高级中允许本机其他应用访问。") from exc
        raise ZoteroError(f"Zotero 本地 API 返回 HTTP {exc.code}", ) from exc
    except urllib.error.URLError as exc:
        raise ZoteroError("无法连接 Zotero。请先打开 Zotero，并确认本地 API 已启用。") from exc
    except Exception as exc:
        raise ZoteroError(f"读取 Zotero 失败：{exc}") from exc


def fetch_library(profile: dict, timeout: int = 15) -> dict:
    """读取全部顶层条目，返回统一结构。"""
    api = normalize_api(profile.get("zotero_api"))
    read_tags, reading_tags, unread_tags = tag_settings(profile)
    records = []
    start = 0
    total = None
    while True:
        url = f"{api}/items?" + urllib.parse.urlencode({
            "limit": 100, "start": start, "format": "json",
        })
        raw, headers = _api_get(url, timeout=timeout)
        try:
            page = json.loads(raw.decode("utf-8", "replace"))
        except Exception as exc:
            raise ZoteroError("Zotero 返回了无法解析的数据") from exc
        if not isinstance(page, list):
            raise ZoteroError("Zotero 返回数据格式异常")
        if total is None:
            try:
                total = int(headers.get("Total-Results", len(page)))
            except Exception:
                total = None
        for entry in page:
            data = entry.get("data") or {}
            item_type = data.get("itemType") or ""
            if item_type in SKIP_TYPES:
                continue
            title = clean_text(data.get("title"), 600)
            if not title:
                continue
            tags = [clean_text(t.get("tag"), 120) for t in (data.get("tags") or []) if t.get("tag")]
            doi = normalize_doi(data.get("DOI"))
            journal = ""
            for key in ("publicationTitle", "proceedingsTitle", "bookTitle", "websiteTitle"):
                if data.get(key):
                    journal = clean_text(data.get(key), 240)
                    break
            creators = []
            for c in data.get("creators") or []:
                name = c.get("name") or " ".join(
                    p for p in [c.get("firstName"), c.get("lastName")] if p
                )
                name = clean_text(name, 160)
                if name:
                    creators.append(name)
            records.append({
                "key": clean_text(data.get("key"), 40),
                "itemType": item_type,
                "title": title,
                "doi": doi,
                "journal": journal,
                "date": clean_text(data.get("date"), 40),
                "url": clean_text(data.get("url"), 600),
                "creators": creators[:20],
                "tags": tags,
                "status": status_for_tags(tags, read_tags, reading_tags, unread_tags),
            })
        if len(page) < 100:
            break
        start += len(page)
        if total and start >= total:
            break
        if start > 20000:
            break

    counts = Counter(r["status"] for r in records)
    status_tags = read_tags | reading_tags | unread_tags
    tag_counter = Counter()
    journal_counter = Counter()
    for r in records:
        if r["status"] not in ("read", "reading"):
            continue
        for tag in r["tags"]:
            lowered = tag.casefold()
            if lowered not in status_tags and len(tag) >= 2:
                tag_counter[tag] += 1
        if r["journal"]:
            journal_counter[r["journal"]] += 1
    return {
        "api": api,
        "items": records,
        "total": len(records),
        "counts": {
            "read": counts.get("read", 0),
            "reading": counts.get("reading", 0),
            "unread": counts.get("unread", 0),
            "unknown": counts.get("unknown", 0),
        },
        "top_tags": [{"name": k, "count": v} for k, v in tag_counter.most_common(12)],
        "top_journals": [{"name": k, "count": v} for k, v in journal_counter.most_common(12)],
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def exclusion_set(items: list[dict], mode: str = "read") -> set[str]:
    if mode == "off":
        return set()
    excluded = set()
    for item in items:
        if mode != "all" and item.get("status") not in ("read", "reading"):
            continue
        doi = (item.get("doi") or "").lower()
        if doi:
            excluded.add(doi)
        title = item.get("title") or ""
        if title:
            excluded.add("title:" + title_key(title))
    return excluded


def preference_profile(items: list[dict], profile: dict | None = None) -> dict:
    read_tags, reading_tags, unread_tags = tag_settings(profile or {})
    status_tags = read_tags | reading_tags | unread_tags
    tags = Counter()
    journals = Counter()
    for item in items:
        if item.get("status") not in ("read", "reading"):
            continue
        for tag in item.get("tags") or []:
            tag = clean_text(tag, 80)
            if tag and tag.casefold() not in status_tags and not tag.startswith("/") and len(tag) >= 2:
                tags[tag] += 1
        journal = clean_text(item.get("journal"), 200)
        if journal:
            journals[journal] += 1
    title_tokens = Counter()
    for item in items:
        if item.get("status") not in ("read", "reading"):
            continue
        for token in re.findall(r"[A-Za-z][A-Za-z-]{2,}", item.get("title") or ""):
            lowered = token.casefold()
            if lowered not in _TITLE_STOP and not lowered.isdigit():
                title_tokens[lowered] += 1
    terms = []
    for name, _ in tags.most_common(20):
        if name not in terms:
            terms.append(name)
    for name, _ in title_tokens.most_common(25):
        if name not in terms:
            terms.append(name)
    journal_keys = {name.casefold(): count for name, count in journals.items()}
    return {
        "terms": terms[:35],
        "journals": journal_keys,
        "journal_names": [name for name, _ in journals.most_common(20)],
        "read_items": sum(1 for i in items if i.get("status") in ("read", "reading")),
    }


def summary_from_cache(cache: dict | None) -> dict:
    if not cache:
        return {"total": 0, "counts": {}, "top_tags": [], "top_journals": [], "fetched_at": None}
    return {
        "total": cache.get("total", 0),
        "counts": cache.get("counts", {}),
        "top_tags": (cache.get("top_tags") or [])[:8],
        "top_journals": (cache.get("top_journals") or [])[:8],
        "fetched_at": cache.get("fetched_at"),
    }
