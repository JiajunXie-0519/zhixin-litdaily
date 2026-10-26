# -*- coding: utf-8 -*-
"""抓取、候选合并与排序。"""
from __future__ import annotations

import json
import math
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, timedelta

from daily import (
    DIMENSION_KEYS,
    GENERIC_TERMS,
    INSPIRATION_TERMS,
    METHOD_TERMS,
    NOVELTY_TERMS,
    SOURCE_KEYS,
    SOURCE_MAP,
    USER_AGENT,
    clamp,
    clean_text,
    contains_term,
    doi_url,
    effective_weights,
    make_id,
    matched_terms,
    normalize_doi,
    parse_date,
    safe_int,
    split_terms,
    title_key,
    unique_keep,
)


class SourceError(Exception):
    """单个学术源的错误，message 面向用户。"""

    def __init__(self, message: str, detail: str = "", status: int | None = None):
        super().__init__(message)
        self.message = message
        self.detail = detail
        self.status = status


_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST = 0.0


def _http_get(url: str, timeout: int = 25, retries: int = 1, method: str | None = None,
              data: bytes | None = None, extra_headers: dict | None = None) -> bytes:
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, application/atom+xml, text/xml;q=0.9, */*;q=0.8",
        "Accept-Encoding": "identity",
    }
    if extra_headers:
        headers.update(extra_headers)
    last_error = None
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = exc.read(800).decode("utf-8", "replace")
            except Exception:
                pass
            if exc.code in (429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(1.5 * (attempt + 1))
                last_error = exc
                continue
            if exc.code == 401:
                raise SourceError("接口需要有效密钥", body, exc.code) from exc
            if exc.code == 403:
                detail = body
                if "key_not_activated" in body:
                    raise SourceError("OpenAlex Key 尚未激活，请检查邮箱中的确认链接", detail, exc.code) from exc
                raise SourceError("接口拒绝访问（可能触发额度限制）", detail, exc.code) from exc
            if exc.code == 429:
                raise SourceError("请求过于频繁或今日额度已用完", body, exc.code) from exc
            raise SourceError(f"接口返回 HTTP {exc.code}", body, exc.code) from exc
        except TimeoutError as exc:
            if attempt < retries:
                time.sleep(1.2)
                last_error = exc
                continue
            raise SourceError("连接超时，请稍后重试", str(exc)) from exc
        except urllib.error.URLError as exc:
            if attempt < retries:
                time.sleep(1.2)
                last_error = exc
                continue
            raise SourceError("网络连接失败，请检查网络或代理", str(exc)) from exc
        except Exception as exc:
            raise SourceError("抓取失败", str(exc)) from exc
    raise SourceError("网络连接失败，请稍后重试", str(last_error))


def _http_json(url: str, timeout: int = 25, **kwargs) -> dict:
    raw = _http_get(url, timeout=timeout, **kwargs)
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except Exception as exc:
        raise SourceError("学术源返回了无法解析的数据", str(exc)) from exc


def _query_terms(field: dict) -> list[str]:
    terms = [t for t in field.get("keywords", []) if t and len(t) >= 2]
    if field.get("name") and all(ord(c) < 128 for c in field["name"]) and len(field["name"]) >= 3:
        terms.append(field["name"])
    english = [t for t in unique_keep(terms) if all(ord(c) < 128 for c in t)]
    # 含中文的关键词保留给 Europe PMC（支持部分中文），但国际源优先英文
    return english[:4] if english else unique_keep(terms)[:3]


def _date_window(lookback_years: int) -> tuple[str, str]:
    end = date.today()
    start = end - timedelta(days=max(1, lookback_years) * 365)
    return start.isoformat(), end.isoformat()


def _reconstruct_abstract(index) -> str:
    if not isinstance(index, dict) or not index:
        return ""
    positions = []
    for word, places in index.items():
        if isinstance(places, list):
            for pos in places:
                if isinstance(pos, int):
                    positions.append((pos, word))
    if not positions:
        return ""
    positions.sort(key=lambda pair: pair[0])
    words = [word for _, word in positions[:900]]
    return clean_text(" ".join(words), 8000)


def _openalex_item(item: dict) -> dict | None:
    doi = normalize_doi(item.get("doi"))
    if not doi:
        return None
    title = clean_text(item.get("display_name") or item.get("title"), 500)
    if not title:
        return None
    source = (item.get("primary_location") or {}).get("source") or {}
    journal = clean_text(source.get("display_name"), 200)
    publisher = clean_text(source.get("host_organization_name"), 200)
    authors = [
        clean_text((a or {}).get("author", {}).get("display_name"), 120)
        for a in (item.get("authorships") or [])
    ]
    authors = [a for a in authors if a]
    topics, domains = [], []
    for topic in item.get("topics") or []:
        name = clean_text(topic.get("display_name"), 120)
        if name:
            topics.append(name)
        field = (topic.get("field") or {}).get("display_name")
        domain = (topic.get("domain") or {}).get("display_name")
        if field:
            topics.append(clean_text(field, 100))
        if domain:
            domains.append(clean_text(domain, 100))
    for kw in item.get("keywords") or []:
        name = clean_text(kw.get("display_name"), 100)
        if name:
            topics.append(name)
    best_oa = item.get("best_oa_location") or {}
    open_access = item.get("open_access") or {}
    landing = (item.get("primary_location") or {}).get("landing_page_url") or doi_url(doi)
    source_id = clean_text(source.get("id"), 200)
    source_id = re.sub(r"^https?://openalex\.org/", "", source_id)
    return {
        "doi": doi,
        "title": title,
        "journal": journal,
        "publisher": publisher,
        "authors": authors,
        "published": clean_text(item.get("publication_date"), 30),
        "year": safe_int(item.get("publication_year")),
        "type": clean_text(item.get("type"), 40) or "article",
        "abstract": _reconstruct_abstract(item.get("abstract_inverted_index")),
        "cited_by_count": safe_int(item.get("cited_by_count")),
        "oa_url": clean_text(best_oa.get("pdf_url") or open_access.get("oa_url"), 1000),
        "landing_url": clean_text(landing, 1000),
        "topics": unique_keep(topics)[:12],
        "domains": unique_keep(domains)[:6],
        "source": "openalex",
        "venue_type": clean_text(source.get("type"), 40) or "journal",
        "source_id": source_id,
        "is_oa": bool(open_access.get("is_oa")),
        "language": clean_text(item.get("language"), 20),
    }


def fetch_openalex(field: dict, profile: dict) -> list[dict]:
    terms = _query_terms(field)
    if not terms:
        return []
    start, end = _date_window(profile.get("lookback_years", 3))
    query = " ".join(f'"{t}"' if " " in t else t for t in terms[:3])
    params = {
        "search": query,
        "filter": f"from_publication_date:{start},to_publication_date:{end},has_doi:true,is_retracted:false,type:article|review|preprint",
        "per-page": "25",
        "sort": "relevance_score:desc",
    }
    if profile.get("contact_email"):
        params["mailto"] = profile["contact_email"]
    if profile.get("openalex_api_key"):
        params["api_key"] = profile["openalex_api_key"]
    url = "https://api.openalex.org/works?" + urllib.parse.urlencode(params)
    data = _http_json(url)
    results = data.get("results") if isinstance(data, dict) else None
    if not isinstance(results, list):
        raise SourceError("OpenAlex 返回数据格式异常")
    out = []
    for item in results:
        try:
            parsed = _openalex_item(item)
            if parsed:
                parsed["query_field"] = field.get("id")
                out.append(parsed)
        except Exception:
            continue
    return out


def _crossref_date(item: dict) -> str:
    for key in ("published-online", "published-print", "issued", "created"):
        parts = ((item.get(key) or {}).get("date-parts") or [[]])[0]
        if parts and parts[0]:
            try:
                year = int(parts[0])
                month = int(parts[1]) if len(parts) > 1 and parts[1] else 1
                day = int(parts[2]) if len(parts) > 2 and parts[2] else 1
                return date(year, month, day).isoformat()
            except Exception:
                continue
    return ""


def _crossref_item(item: dict) -> dict | None:
    doi = normalize_doi(item.get("DOI"))
    title_list = item.get("title") or []
    title = clean_text(title_list[0] if title_list else "", 500)
    if not doi or not title:
        return None
    journal_list = item.get("container-title") or []
    journal = clean_text(journal_list[0] if journal_list else "", 200)
    authors = []
    for author in item.get("author") or []:
        name = " ".join(p for p in [clean_text(author.get("given"), 80), clean_text(author.get("family"), 80)] if p)
        if not name:
            name = clean_text(author.get("name"), 120)
        if name:
            authors.append(name)
    published = _crossref_date(item)
    return {
        "doi": doi,
        "title": title,
        "journal": journal,
        "publisher": clean_text(item.get("publisher"), 200),
        "authors": authors,
        "published": published,
        "year": safe_int(published[:4]) if published else 0,
        "type": "article",
        "abstract": clean_text(item.get("abstract"), 8000),
        "cited_by_count": safe_int(item.get("is-referenced-by-count")),
        "oa_url": "",
        "landing_url": clean_text(item.get("URL") or doi_url(doi), 1000),
        "topics": unique_keep([clean_text(s, 100) for s in (item.get("subject") or [])])[:10],
        "domains": [],
        "source": "crossref",
        "venue_type": "journal",
        "is_oa": False,
        "language": clean_text(item.get("language"), 20),
    }


def fetch_crossref(field: dict, profile: dict) -> list[dict]:
    terms = _query_terms(field)
    if not terms:
        return []
    start, end = _date_window(profile.get("lookback_years", 3))
    params = {
        "query.bibliographic": " ".join(terms[:3]),
        "filter": f"from-pub-date:{start},until-pub-date:{end},type:journal-article",
        "rows": "20",
        "sort": "relevance",
    }
    if profile.get("contact_email"):
        params["mailto"] = profile["contact_email"]
    url = "https://api.crossref.org/works?" + urllib.parse.urlencode(params)
    data = _http_json(url)
    items = ((data.get("message") or {}).get("items") if isinstance(data, dict) else None) or []
    out = []
    for item in items:
        try:
            parsed = _crossref_item(item)
            if parsed:
                parsed["query_field"] = field.get("id")
                out.append(parsed)
        except Exception:
            continue
    return out


def _epmc_item(item: dict) -> dict | None:
    doi = normalize_doi(item.get("doi"))
    title = clean_text(item.get("title"), 500)
    if not doi or not title:
        return None
    journal = clean_text(item.get("journalTitle"), 200)
    if not journal:
        journal = clean_text(((item.get("journalInfo") or {}).get("journal") or {}).get("title"), 200)
    published = clean_text(item.get("firstPublicationDate"), 30)
    if not published:
        published = clean_text(item.get("electronicPublicationDate"), 30)
    year = safe_int(item.get("pubYear")) or (safe_int(published[:4]) if published else 0)
    pub_types = [clean_text(p, 60) for p in ((item.get("pubTypeList") or {}).get("pubType") or [])]
    is_review = any("review" in p.lower() for p in pub_types)
    topics = []
    for kw in ((item.get("keywordList") or {}).get("keyword") or []):
        if isinstance(kw, str):
            topics.append(kw)
    for mesh in ((item.get("meshHeadingList") or {}).get("meshHeading") or []):
        name = mesh.get("descriptorName") if isinstance(mesh, dict) else ""
        if name:
            topics.append(name)
    oa_url = ""
    for link in ((item.get("fullTextUrlList") or {}).get("fullTextUrl") or []):
        url = clean_text(link.get("url"), 1000)
        if url and (link.get("availability") == "Open access" or link.get("documentStyle") == "pdf"):
            oa_url = url
            if link.get("documentStyle") == "pdf":
                break
    authors = clean_text(item.get("authorString"), 600)
    author_list = [a.strip() for a in authors.split(",") if a.strip()] if authors else []
    return {
        "doi": doi,
        "title": title,
        "journal": journal,
        "publisher": "",
        "authors": author_list,
        "published": published,
        "year": year,
        "type": "review" if is_review else "article",
        "abstract": clean_text(item.get("abstractText"), 8000),
        "cited_by_count": safe_int(item.get("citedByCount")),
        "oa_url": oa_url,
        "landing_url": f"https://europepmc.org/article/{clean_text(item.get('source'), 10) or 'MED'}/{clean_text(item.get('id'), 40)}",
        "topics": unique_keep([clean_text(t, 100) for t in topics])[:12],
        "domains": ["生命医学"],
        "source": "europepmc",
        "venue_type": "journal",
        "is_oa": item.get("isOpenAccess") == "Y",
        "language": clean_text(item.get("language"), 20),
    }


def fetch_europepmc(field: dict, profile: dict) -> list[dict]:
    terms = _query_terms(field)
    if not terms:
        return []
    start, end = _date_window(profile.get("lookback_years", 3))
    term_query = " OR ".join(f'"{t}"' for t in terms[:3])
    query = f"({term_query}) AND (FIRST_PDATE:[{start} TO {end}])"
    params = {
        "query": query,
        "format": "json",
        "resultType": "core",
        "pageSize": "25",
        "sort": "P_PDATE_D desc",
    }
    url = "https://www.ebi.ac.uk/europepmc/webservices/rest/search?" + urllib.parse.urlencode(params)
    data = _http_json(url)
    items = ((data.get("resultList") or {}).get("result") if isinstance(data, dict) else None) or []
    out = []
    for item in items:
        try:
            parsed = _epmc_item(item)
            if parsed:
                parsed["query_field"] = field.get("id")
                out.append(parsed)
        except Exception:
            continue
    return out


def _arxiv_wait():
    global _ARXIV_LAST
    with _ARXIV_LOCK:
        wait = 3.1 - (time.time() - _ARXIV_LAST)
        if wait > 0:
            time.sleep(wait)
        _ARXIV_LAST = time.time()


def fetch_arxiv(field: dict, profile: dict) -> list[dict]:
    terms = _query_terms(field)
    if not terms:
        return []
    _arxiv_wait()
    search = " OR ".join(f'all:"{t}"' for t in terms[:3])
    params = {
        "search_query": search,
        "start": "0",
        "max_results": "25",
        "sortBy": "submittedDate",
        "sortOrder": "descending",
    }
    url = "https://export.arxiv.org/api/query?" + urllib.parse.urlencode(params)
    raw = _http_get(url, timeout=35)
    try:
        root = ET.fromstring(raw)
    except Exception as exc:
        raise SourceError("arXiv 返回数据格式异常", str(exc)) from exc
    ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
    start, _ = _date_window(profile.get("lookback_years", 3))
    out = []
    for entry in root.findall("a:entry", ns):
        try:
            title = clean_text(entry.findtext("a:title", default="", namespaces=ns), 500)
            abstract = clean_text(entry.findtext("a:summary", default="", namespaces=ns), 8000)
            published = clean_text(entry.findtext("a:published", default="", namespaces=ns), 40)[:10]
            if not title or not published or published < start:
                continue
            abs_url = clean_text(entry.findtext("a:id", default="", namespaces=ns), 500)
            m = re.search(r"arxiv\.org/abs/(.+?)(?:v\d+)?$", abs_url)
            arxiv_id = (m.group(1) if m else "").rstrip("/")
            if arxiv_id.endswith(".pdf"):
                arxiv_id = arxiv_id[:-4]
            if not arxiv_id:
                continue
            doi = f"10.48550/arXiv.{arxiv_id}"
            authors = []
            for author in entry.findall("a:author", ns):
                name = clean_text(author.findtext("a:name", default="", namespaces=ns), 120)
                if name:
                    authors.append(name)
            category_el = entry.find("arxiv:primary_category", ns)
            category = category_el.get("term") if category_el is not None else "arXiv"
            journal_ref = clean_text(entry.findtext("arxiv:journal_ref", default="", namespaces=ns), 200)
            out.append({
                "doi": doi,
                "title": title,
                "journal": f"arXiv · {category}",
                "publisher": "arXiv",
                "authors": authors,
                "published": published,
                "year": safe_int(published[:4]),
                "type": "preprint",
                "abstract": abstract,
                "cited_by_count": 0,
                "oa_url": f"https://arxiv.org/pdf/{arxiv_id}",
                "landing_url": f"https://arxiv.org/abs/{arxiv_id}",
                "topics": unique_keep([category, journal_ref] if journal_ref else [category]),
                "domains": ["预印本"],
                "source": "arxiv",
                "venue_type": "repository",
                "is_oa": True,
                "language": "en",
                "query_field": field.get("id"),
            })
        except Exception:
            continue
    return out


FETCHERS = {
    "openalex": fetch_openalex,
    "crossref": fetch_crossref,
    "arxiv": fetch_arxiv,
    "europepmc": fetch_europepmc,
}


def _clean_abstract(value) -> str:
    text = clean_text(value, 8000)
    text = re.sub(r"^\s*(abstract|summary)\s*[:：.\-–—]?\s+", "", text, flags=re.I)
    return text.strip()


_TITLE_KEEP = {
    "AI", "ML", "LLM", "LLMS", "GPT", "CHATGPT", "NLP", "STEM", "STEAM",
    "COVID", "COVID-19", "SARS", "DNA", "RNA", "WHO", "OECD", "UNESCO", "EU",
    "US", "UK", "USA", "NIH", "NSF", "DOI", "API", "RCT", "BERT", "GPU", "CPU",
}
_TITLE_LOWER = {
    "a", "an", "and", "as", "at", "but", "by", "for", "from", "in", "into",
    "of", "on", "or", "the", "to", "via", "vs", "with", "without", "across",
}


def _smart_title(value: str) -> str:
    """把全大写的题名转成可读的标题式大小写；混合大小写的题名保持原样。"""
    text = clean_text(value, 500)
    letters = [c for c in text if c.isalpha()]
    if len(letters) < 25:
        return text
    upper_ratio = sum(1 for c in letters if c.isupper()) / len(letters)
    if upper_ratio < 0.86:
        return text
    index = 0

    def repl(match):
        nonlocal index
        word = match.group(0)
        position = index
        index += 1
        before = text[:match.start()].rstrip()
        new_sentence = not before or before[-1] in "?!.:"
        if word.upper() in _TITLE_KEEP or any(c.isdigit() for c in word):
            return word.upper() if word.upper() in _TITLE_KEEP else word
        lowered = word.lower()
        if position > 0 and not new_sentence and lowered in _TITLE_LOWER:
            return lowered
        if "'" in lowered:
            parts = lowered.split("'")
            return "'".join(p[:1].upper() + p[1:] for p in parts)
        return lowered[:1].upper() + lowered[1:]

    return re.sub(r"[A-Za-z]+(?:['\-][A-Za-z]+)*", repl, text)


def _merge_candidates(items: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    by_title: dict[str, str] = {}
    for item in items:
        doi = item.get("doi") or ""
        key = doi or ("title:" + title_key(item.get("title", "")))
        if not key:
            continue
        tkey = title_key(item.get("title", ""))
        if key not in merged and tkey and tkey in by_title:
            key = by_title[tkey]
        if key not in merged:
            cand = dict(item)
            cand["sources"] = [item.get("source")]
            cand["source_fields"] = {item.get("source"): [item.get("query_field")]}
            cand["query_fields"] = [item.get("query_field")] if item.get("query_field") else []
            merged[key] = cand
            if tkey:
                by_title[tkey] = key
            continue
        base = merged[key]
        # 同一篇文章可能同时有预印本 DOI 和正式发表 DOI；优先保留正式发表信息
        if base.get("type") == "preprint" and item.get("type") != "preprint":
            for field in ("doi", "journal", "publisher", "published", "year", "type",
                          "venue_type", "landing_url", "oa_url"):
                if item.get(field):
                    base[field] = item[field]
        base["sources"] = unique_keep(base.get("sources", []) + [item.get("source")])
        sf = base.setdefault("source_fields", {})
        sf.setdefault(item.get("source"), []).append(item.get("query_field"))
        if item.get("query_field") and item["query_field"] not in base.setdefault("query_fields", []):
            base["query_fields"].append(item["query_field"])
        if len(item.get("abstract") or "") > len(base.get("abstract") or ""):
            base["abstract"] = item.get("abstract") or ""
        if not base.get("journal") and item.get("journal"):
            base["journal"] = item["journal"]
        if not base.get("published") and item.get("published"):
            base["published"] = item["published"]
        if safe_int(item.get("cited_by_count")) > safe_int(base.get("cited_by_count")):
            base["cited_by_count"] = safe_int(item.get("cited_by_count"))
        if not base.get("oa_url") and item.get("oa_url"):
            base["oa_url"] = item["oa_url"]
        base["topics"] = unique_keep((base.get("topics") or []) + (item.get("topics") or []))[:15]
        base["domains"] = unique_keep((base.get("domains") or []) + (item.get("domains") or []))[:8]
        if len(item.get("authors") or []) > len(base.get("authors") or []):
            base["authors"] = item.get("authors")
        if not base.get("publisher") and item.get("publisher"):
            base["publisher"] = item["publisher"]
        if not base.get("source_id") and item.get("source_id"):
            base["source_id"] = item["source_id"]
        if base.get("type") in (None, "article") and item.get("type") == "review":
            base["type"] = "review"
    for cand in merged.values():
        cand["doi"] = normalize_doi(cand.get("doi")) or cand.get("doi")
        cand["id"] = make_id(cand.get("doi", ""), cand.get("title", ""))
        cand["authors"] = [a for a in (cand.get("authors") or []) if a][:20]
        cand["author_line"] = _author_line(cand["authors"])
        cand["title"] = _smart_title(cand.get("title") or "")
        cand["year"] = safe_int(cand.get("year")) or (safe_int(cand.get("published", "")[:4]) if cand.get("published") else 0)
        cand["abstract"] = _clean_abstract(cand.get("abstract"))
        cand["journal"] = clean_text(cand.get("journal"), 200) or "未标注期刊"
        cand["type"] = clean_text(cand.get("type"), 30) or "article"
        cand["landing_url"] = clean_text(cand.get("landing_url") or doi_url(cand.get("doi", "")), 1000)
        cand["source_count"] = len(cand.get("sources") or [])
    return list(merged.values())


def _author_line(authors: list[str]) -> str:
    if not authors:
        return "作者信息暂缺"
    if len(authors) <= 3:
        return ", ".join(authors)
    return ", ".join(authors[:3]) + " 等"


def _enrich_venues(candidates: list[dict], profile: dict) -> None:
    """批量补充 OpenAlex 期刊层面的引用表现，用于更稳健的可靠性判断。"""
    ids = []
    for cand in candidates:
        sid = cand.get("source_id")
        if sid and re.match(r"^S\d+$", sid) and sid not in ids:
            ids.append(sid)
    if not ids:
        return
    ids = ids[:50]
    params = {
        "filter": "openalex_id:" + "|".join(ids),
        "per-page": str(len(ids)),
        "select": "id,display_name,works_count,summary_stats,is_in_doaj",
    }
    if profile.get("contact_email"):
        params["mailto"] = profile["contact_email"]
    if profile.get("openalex_api_key"):
        params["api_key"] = profile["openalex_api_key"]
    url = "https://api.openalex.org/sources?" + urllib.parse.urlencode(params)
    try:
        data = _http_json(url, timeout=25)
    except Exception:
        return
    lookup = {}
    for item in (data.get("results") or []):
        sid = clean_text(item.get("id"), 200)
        sid = re.sub(r"^https?://openalex\.org/", "", sid)
        stats = item.get("summary_stats") or {}
        lookup[sid] = {
            "venue_citedness": stats.get("2yr_mean_citedness"),
            "venue_h_index": stats.get("h_index"),
            "venue_works": safe_int(item.get("works_count")),
            "venue_is_doaj": bool(item.get("is_in_doaj")),
        }
    for cand in candidates:
        metrics = lookup.get(cand.get("source_id"))
        if metrics:
            cand.update(metrics)


def fetch_candidates(profile: dict, date_str: str, exclude: set[str] | None = None) -> dict:
    fields = profile.get("fields") or []
    sources = [s for s in profile.get("sources", []) if s in FETCHERS]
    if not fields:
        raise SourceError("尚未设置研究领域")
    if not sources:
        raise SourceError("至少启用一个文献数据源")

    # 查询轮转：每天优先不同领域，避免多领域时某个领域长期缺席
    rotation = (parse_date(date_str) or date.today()).toordinal() % len(fields)
    ordered_fields = fields[rotation:] + fields[:rotation]
    tasks = [(source, field) for field in ordered_fields for source in sources]
    status = {
        s: {"key": s, "name": SOURCE_MAP[s]["name"], "ok": True, "count": 0, "fields": 0, "error": ""}
        for s in sources
    }
    raw_items = []
    errors = []
    with ThreadPoolExecutor(max_workers=min(8, max(1, len(tasks)))) as pool:
        future_map = {
            pool.submit(FETCHERS[source], field, profile): (source, field)
            for source, field in tasks
        }
        for future in as_completed(future_map):
            source, field = future_map[future]
            try:
                items = future.result()
                status[source]["fields"] += 1
                status[source]["count"] += len(items)
                raw_items.extend(items)
            except SourceError as exc:
                status[source]["ok"] = False
                status[source]["error"] = exc.message
                errors.append(f"{SOURCE_MAP[source]['name']}：{exc.message}")
            except Exception as exc:
                status[source]["ok"] = False
                status[source]["error"] = "抓取异常"
                errors.append(f"{SOURCE_MAP[source]['name']}：{type(exc).__name__}")
    if not raw_items:
        message = "；".join(unique_keep(errors)) or "没有抓取到候选文献"
        raise SourceError(message)

    merged = _merge_candidates(raw_items)
    _enrich_venues(merged, profile)
    excluded = set(exclude or [])
    prior_ids = set()
    for old in excluded:
        prior_ids.add(old.lower())
    filtered = []
    for cand in merged:
        if cand["id"].lower() in prior_ids or (cand.get("doi") and cand["doi"].lower() in prior_ids):
            continue
        if cand.get("title") and ("title:" + title_key(cand["title"])) in prior_ids:
            continue
        filtered.append(cand)
    return {
        "candidates": filtered,
        "status": list(status.values()),
        "errors": unique_keep(errors),
        "field_order": [f["id"] for f in ordered_fields],
        "raw_count": len(raw_items),
    }
