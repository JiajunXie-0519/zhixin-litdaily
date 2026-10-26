# -*- coding: utf-8 -*-
"""候选评分、个性化与日报统计。"""
from __future__ import annotations

import copy
import math
import re
from datetime import date, datetime, timedelta

from daily import (
    DIMENSION_KEYS,
    DIMENSION_MAP,
    DIMENSIONS,
    DEFAULT_WEIGHTS,
    INSPIRATION_TERMS,
    METHOD_TERMS,
    NOVELTY_TERMS,
    READ_STATUS,
    VERSION,
    clamp,
    clean_text,
    contains_term,
    doi_url,
    effective_weights,
    institution_url,
    issue_number,
    parse_date,
    safe_int,
    title_key,
    unique_keep,
)


def _abstract_words(abstract: str) -> int:
    if not abstract:
        return 0
    latin = re.findall(r"[A-Za-z][A-Za-z'-]+", abstract)
    chinese = re.findall(r"[\u4e00-\u9fff]", abstract)
    return len(latin) + len(chinese) // 2


def _recency(published: str, ref: date) -> tuple[float, int | None]:
    d = parse_date(published)
    if not d:
        return 45.0, None
    age = max(0, (ref - d).days)
    score = clamp(102 - age / 1095 * 70, 28, 100)
    return score, age


def _field_match(candidate: dict, field: dict) -> tuple[float, list[str]]:
    title = candidate.get("title") or ""
    abstract = candidate.get("abstract") or ""
    topics = " ".join(candidate.get("topics") or [])
    terms = [t for t in field.get("keywords", []) if t and len(t) >= 2]
    matched, credits = [], []
    for term in terms:
        credit = 0.0
        if contains_term(title, term):
            credit = 1.0
        elif contains_term(topics, term):
            credit = 0.85
        elif contains_term(abstract, term):
            credit = 0.55
        else:
            # 长检索词常以变体出现；按实词覆盖率给一点部分分，但不计入“命中”标签
            tokens = [w for w in re.findall(r"[A-Za-z][A-Za-z-]{2,}", term)
                      if w.lower() not in {"the", "and", "for", "with", "from", "into", "in", "of", "on"}]
            if len(tokens) >= 2:
                title_hits = sum(1 for w in tokens if contains_term(title, w))
                all_hits = sum(1 for w in tokens if contains_term(title + " " + abstract, w))
                if all_hits / len(tokens) >= 0.5:
                    credit = min(0.45, 0.16 * all_hits + (0.12 if title_hits >= 2 else 0))
        if credit:
            if credit >= 0.5:
                matched.append(term)
            credits.append(credit)
    denom = max(2, min(len(terms), 4))
    coverage = min(1.0, sum(credits) / denom)
    distinct = len(set(matched))
    score = 8 + coverage * 62 + min(12, distinct * 6)
    if field.get("id") in (candidate.get("query_fields") or []):
        score += 8
    if not matched and field.get("id") not in (candidate.get("query_fields") or []) and max(credits or [0]) < 0.4:
        return 0.0, []
    return clamp(score), matched


def _personal_boost(candidate: dict, personal: dict) -> tuple[float, list[str]]:
    text = " ".join([
        candidate.get("title") or "",
        " ".join(candidate.get("topics") or []),
        (candidate.get("abstract") or "")[:1500],
    ])
    boost, reasons = 0.0, []
    liked_hits = [t for t in personal.get("liked", []) if contains_term(text, t)]
    if liked_hits:
        boost += min(5.0, 1.5 * len(liked_hits))
        reasons.append("贴近你的阅读兴趣：" + "、".join(liked_hits[:2]))
    journal = (candidate.get("journal") or "").casefold().strip()
    journal_counts = personal.get("journals") or {}
    if journal and journal_counts.get(journal):
        boost += min(2.0, 0.6 + 0.2 * journal_counts[journal])
        reasons.append("来自你常读的期刊")
    disliked_hits = [t for t in personal.get("disliked", []) if contains_term(text, t)]
    if disliked_hits:
        boost -= min(5.0, 1.6 * len(disliked_hits))
    return boost, reasons


def score_candidate(candidate: dict, fields: list[dict], profile: dict, preference: dict | None,
                    ref: date, personal: dict) -> dict:
    cand = copy.deepcopy(candidate)
    field_results = []
    for field in fields:
        score, matched = _field_match(cand, field)
        if score > 0:
            field_results.append({
                "id": field["id"],
                "name": field["name"],
                "color": field.get("color", "#2F5D50"),
                "weight": field.get("weight", 1),
                "score": round(score, 1),
                "matched": matched,
            })
    field_results.sort(key=lambda x: (-x["score"], -float(x.get("weight", 1))))
    if not field_results:
        for field in fields:
            if field["id"] in (cand.get("query_fields") or []):
                field_results.append({
                    "id": field["id"], "name": field["name"],
                    "color": field.get("color", "#2F5D50"),
                    "weight": field.get("weight", 1), "score": 18.0, "matched": [],
                })
    cand["fields"] = field_results[:3]
    relevance = field_results[0]["score"] if field_results else 15
    matched_keywords = unique_keep([kw for f in field_results for kw in f["matched"]])[:8]
    cand["matched_keywords"] = matched_keywords
    multi_field = len(field_results) > 1

    abstract = cand.get("abstract") or ""
    words = _abstract_words(abstract)
    text = " ".join([cand.get("title") or "", abstract, " ".join(cand.get("topics") or [])])
    methods = [t for t in METHOD_TERMS if contains_term(text, t)]
    method_count = len(set(methods))

    # 专业性：摘要信息量 + 方法学线索 + 发表完整性
    expertise = 27 + min(35, words / 7.0) + min(24, method_count * 4.2)
    if cand.get("journal") and cand["journal"] != "未标注期刊":
        expertise += 6
    if cand.get("type") == "review":
        expertise += 5
    if not abstract:
        expertise = 34
    expertise = clamp(expertise)

    # 可靠性：可核验 DOI、同行评议渠道、被引、多源交叉
    reliability = 31
    if cand.get("doi"):
        reliability += 8
    if cand.get("venue_type") == "journal":
        reliability += 15
    elif cand.get("venue_type") == "conference":
        reliability += 8
    if cand.get("journal") and cand["journal"] != "未标注期刊":
        reliability += 5
    citedness = cand.get("venue_citedness")
    if isinstance(citedness, (int, float)):
        works = safe_int(cand.get("venue_works"))
        if citedness >= 5:
            reliability += 10
        elif citedness >= 2:
            reliability += 8
        elif citedness >= 1:
            reliability += 5
        elif citedness >= 0.5:
            reliability += 2
        elif works >= 100 and citedness < 0.1:
            reliability -= 12
        elif works >= 500 and citedness < 0.3:
            reliability -= 7
    cites = safe_int(cand.get("cited_by_count"))
    reliability += min(20, 6.2 * math.log10(cites + 1))
    if abstract:
        reliability += 4
    if safe_int(cand.get("source_count")) >= 2:
        reliability += 10
    if cand.get("type") == "review":
        reliability += 5
    reliability = clamp(reliability)

    # 创新性：新鲜度 + 被引速度 + 新意措辞（仅为可解释的代理指标）
    recency, age_days = _recency(cand.get("published") or "", ref)
    age_years = max(age_days / 365.0, 0.35) if age_days is not None else 1.0
    velocity = min(18, 5.5 * math.log1p(cites / age_years))
    novelty = recency * 0.72 + velocity
    if any(contains_term(cand.get("title") or "", t) for t in NOVELTY_TERMS):
        novelty += 5
    if len(cand.get("topics") or []) >= 4:
        novelty += 3
    novelty = clamp(novelty)

    # 启发性：跨领域、综述、展望型措辞、信息完整度
    inspiration = 38
    if 120 <= words <= 450:
        inspiration += 12
    elif words > 450:
        inspiration += 6
    if cand.get("type") == "review":
        inspiration += 9
    domains = unique_keep(cand.get("domains") or [])
    if len(domains) >= 2:
        inspiration += 10
    if len(domains) >= 3:
        inspiration += 5
    if multi_field:
        inspiration += 9
    insp_hits = [t for t in INSPIRATION_TERMS if contains_term(text, t)]
    inspiration += min(10, len(insp_hits) * 2.5)
    if len(cand.get("topics") or []) >= 3:
        inspiration += 4
    inspiration = clamp(inspiration)

    cand["dimension_scores"] = {
        "relevance": round(relevance, 1),
        "expertise": round(expertise, 1),
        "reliability": round(reliability, 1),
        "novelty": round(novelty, 1),
        "inspiration": round(inspiration, 1),
    }
    weights, weight_pct = effective_weights(profile, preference)
    rank = sum(cand["dimension_scores"][k] * weights[k] for k in DIMENSION_KEYS) / (sum(weights.values()) or 1)
    pboost, preasons = _personal_boost(cand, personal)
    rank += pboost
    max_field_weight = max([float(f.get("weight", 1)) for f in field_results] or [1])
    rank += (max_field_weight - 1) * 2
    cand["rank_score"] = round(clamp(rank, 0, 100), 2)
    cand["personal_boost"] = round(pboost, 1)
    cand["signals"] = _signals(cand, words, method_count, domains, multi_field, age_days, insp_hits)
    cand["why"] = _why(cand, matched_keywords, cites, age_days, preasons)
    cand["_weight_pct"] = weight_pct
    return cand


def _signals(cand: dict, words: int, methods: int, domains: list[str], multi_field: bool,
             age_days: int | None, insp_hits: list[str]) -> dict:
    rel = []
    if cand.get("matched_keywords"):
        rel.append("命中 " + "、".join(cand["matched_keywords"][:3]))
    if len(cand.get("fields") or []) > 1:
        rel.append("跨 " + str(len(cand["fields"])) + " 个研究领域")
    if not rel:
        rel.append("与检索主题间接相关")

    exp = [f"摘要约 {words} 词" if words else "摘要信息有限"]
    if methods:
        exp.append(f"{methods} 处方法学线索")
    if cand.get("type") == "review":
        exp.append("综述 / 总结性文章")

    journal = cand.get("journal") or ""
    reliable = []
    if cand.get("venue_type") == "journal":
        reliable.append("同行评议期刊")
    elif cand.get("type") == "preprint":
        reliable.append("预印本，尚未同行评议")
    else:
        reliable.append(journal or "发表渠道待核验")
    if isinstance(cand.get("venue_citedness"), (int, float)):
        reliable.append(f"期刊 2 年均被引 {cand['venue_citedness']:.2f}")
    cites = safe_int(cand.get("cited_by_count"))
    reliable.append(f"被引 {cites} 次" if cites else "被引数据暂低")
    if safe_int(cand.get("source_count")) >= 2:
        reliable.append(f"{cand['source_count']} 个数据源交叉")

    nov = []
    if age_days is None:
        nov.append("发表时间待核验")
    elif age_days <= 45:
        nov.append("近 45 天发表")
    elif age_days <= 180:
        nov.append(f"近 {max(1, age_days // 30)} 个月发表")
    elif age_days <= 365:
        nov.append("一年内发表")
    else:
        nov.append(f"发表于 {max(1, age_days // 365)} 年前")

    ins = []
    if domains:
        ins.append("跨 " + str(len(domains)) + " 个学科域" if len(domains) > 1 else domains[0] + "领域")
    if cand.get("type") == "review":
        ins.append("适合建立全局视角")
    if insp_hits:
        ins.append("含展望 / 议题线索")
    return {
        "relevance": " · ".join(rel[:3]),
        "expertise": " · ".join(exp[:3]),
        "reliability": " · ".join(reliable[:3]),
        "novelty": " · ".join(nov[:3]),
        "inspiration": " · ".join(ins[:3]) or "信息有限，待阅读判断",
    }


def _why(cand: dict, matched: list[str], cites: int, age_days: int | None, preasons: list[str]) -> list[str]:
    why = []
    if matched:
        why.append("命中「" + "、".join(matched[:2]) + "」")
    if cand.get("venue_type") == "journal":
        why.append("期刊论文")
    if cites:
        why.append(f"被引 {cites}")
    if age_days is not None and age_days <= 180:
        why.append("近期发表")
    if safe_int(cand.get("source_count")) >= 2:
        why.append("多源交叉")
    why.extend(preasons)
    return unique_keep(why)[:4]


def rank_candidates(candidates: list[dict], fields: list[dict], profile: dict, preference: dict | None,
                    ref: date, personal: dict) -> list[dict]:
    ranked = []
    for candidate in candidates:
        try:
            ranked.append(score_candidate(candidate, fields, profile, preference, ref, personal))
        except Exception:
            continue
    ranked.sort(key=lambda c: (-c.get("rank_score", 0), c.get("title", "")))
    return ranked


def _diversity(candidate: dict, selected: list[dict]) -> float:
    if not selected:
        return 0.0
    bonus = 0.0
    sel_fields = {f["id"] for p in selected for f in p.get("fields", [])}
    candidate_fields = {f["id"] for f in candidate.get("fields", [])}
    if candidate_fields and not (candidate_fields & sel_fields):
        bonus += 7
    sel_domains = {d for p in selected for d in (p.get("domains") or [])}
    if candidate.get("domains") and not (set(candidate["domains"]) & sel_domains):
        bonus += 3
    sel_journals = {(p.get("journal") or "").lower() for p in selected}
    if (candidate.get("journal") or "").lower() not in sel_journals:
        bonus += 3
    sel_topics = {t.lower() for p in selected for t in (p.get("topics") or [])[:4]}
    overlap = len(sel_topics & {t.lower() for t in (candidate.get("topics") or [])[:4]})
    if overlap == 0:
        bonus += 3
    elif overlap >= 2:
        bonus -= 5
    return bonus


def select_picks(ranked: list[dict], count: int) -> list[dict]:
    if not ranked:
        return []
    count = max(1, min(5, count))
    # 过滤掉明显不相关且数量充足时的噪声
    eligible = [c for c in ranked if c.get("dimension_scores", {}).get("relevance", 0) >= 22]
    if len(eligible) < count:
        eligible = ranked
    first = eligible[0]
    selected = [first]
    remaining = eligible[1:]
    while len(selected) < count and remaining:
        best, best_score = None, -1
        for cand in remaining:
            combined = cand.get("rank_score", 0) + _diversity(cand, selected)
            if combined > best_score:
                best, best_score = cand, combined
        if best is None:
            break
        selected.append(best)
        remaining.remove(best)
    return selected


def personal_signals(state: dict, ref_date: str, lookback_days: int = 30) -> dict:
    ref = parse_date(ref_date) or date.today()
    liked, disliked = {}, {}
    for date_key, day in (state.get("days") or {}).items():
        d = parse_date(date_key)
        if not d or d >= ref or (ref - d).days > lookback_days:
            continue
        for pick in day.get("picks") or []:
            scores = pick.get("scores") or {}
            if not scores:
                continue
            avg = sum(scores.values()) / len(scores)
            bucket = liked if avg >= 3.8 else disliked if avg <= 2.2 else None
            if bucket is None:
                continue
            for topic in (pick.get("topics") or [])[:5]:
                if topic and len(topic) <= 50:
                    bucket[topic] = bucket.get(topic, 0) + 1
            for kw in (pick.get("matched_keywords") or [])[:3]:
                if kw:
                    bucket[kw] = bucket.get(kw, 0) + 1
    liked_terms = [t for t, _ in sorted(liked.items(), key=lambda kv: -kv[1])[:8]]
    disliked_terms = [t for t, _ in sorted(disliked.items(), key=lambda kv: -kv[1])[:5] if t not in liked_terms]
    return {"liked": liked_terms, "disliked": disliked_terms}


def prior_exclusions(state: dict, date_str: str, days: int = 45, include_today: bool = False) -> set[str]:
    """已评分/已读文献永久排除；未读文献在滑动窗口内排除。

    同时记录 DOI、内部 id 和标准化标题，避免预印本/正式版因 DOI 不同而重复出现。
    """
    ref = parse_date(date_str) or date.today()
    excluded = set()
    for day_key, day in (state.get("days") or {}).items():
        d = parse_date(day_key)
        if not d or d > ref or (d == ref and not include_today):
            continue
        for pick in day.get("picks") or []:
            finished = bool(pick.get("scores")) or pick.get("read_status") in ("skimmed", "read", "archived")
            if not finished and (ref - d).days > days:
                continue
            if pick.get("id"):
                excluded.add(str(pick["id"]).lower())
            if pick.get("doi"):
                excluded.add(str(pick["doi"]).lower())
            if pick.get("title"):
                excluded.add("title:" + title_key(pick["title"]))
    return excluded


def build_day(fetched: dict, profile: dict, date_str: str, preference: dict | None,
              preference_source: str | None, personal: dict) -> dict:
    fields = profile.get("fields") or []
    ref = parse_date(date_str) or date.today()
    ranked = rank_candidates(fetched.get("candidates", []), fields, profile, preference, ref, personal)
    threshold = float(profile.get("min_venue_citedness") or 0)
    missing_policy = profile.get("venue_missing_policy", "exclude")
    venue_filtered = 0
    eligible = []
    for cand in ranked:
        if threshold > 0:
            metric = cand.get("venue_citedness")
            if metric is None:
                if missing_policy == "exclude":
                    venue_filtered += 1
                    continue
            else:
                try:
                    if float(metric) < threshold:
                        venue_filtered += 1
                        continue
                except Exception:
                    if missing_policy == "exclude":
                        venue_filtered += 1
                        continue
        eligible.append(cand)
    if not eligible:
        raise ValueError(
            f"没有候选达到期刊引用门槛（2 年均被引 ≥ {threshold:g}）。"
            "请在设置里降低门槛，或允许指标缺失的文献进入推荐。"
        )
    count = int(profile.get("daily_count", 2))
    picks = select_picks(eligible, count)
    if not picks:
        raise ValueError("没有找到足够相关的候选文献")
    _, weight_pct = effective_weights(profile, preference)
    for index, pick in enumerate(picks, 1):
        pick.pop("_weight_pct", None)
        pick["pick_no"] = index
        pick["scores"] = {}
        pick["score_note"] = ""
        pick["read_status"] = "unread"
        pick["scored_at"] = None
        pick["total"] = None
        pick["access_url"] = institution_url(profile.get("institution_template", ""), pick.get("doi", ""))
        pick["doi_url"] = doi_url(pick.get("doi", "")) if pick.get("doi") else ""
    day = {
        "date": date_str,
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "generator": VERSION,
        "picks": picks,
        "preference": None,
        "field_snapshot": [
            {"id": f.get("id"), "name": f.get("name"), "color": f.get("color")}
            for f in fields
        ],
        "meta": {
            "candidate_count": len(eligible),
            "pre_filter_count": len(ranked),
            "venue_filtered": venue_filtered,
            "min_venue_citedness": threshold,
            "venue_missing_policy": missing_policy,
            "raw_count": fetched.get("raw_count", 0),
            "sources": fetched.get("status", []),
            "warnings": fetched.get("errors", []),
            "weights": weight_pct,
            "preference_source": preference_source,
            "preference_focus": (preference or {}).get("focus", []),
            "lookback_years": profile.get("lookback_years", 3),
            "personal_signals": personal,
            "fields": [
                {"id": f.get("id"), "name": f.get("name"), "color": f.get("color")}
                for f in fields
            ],
        },
    }
    return day


def decorate_day(day: dict | None, profile: dict, days: dict | None = None) -> dict | None:
    if not day:
        return None
    out = copy.deepcopy(day)
    out["issue"] = issue_number(days or {}, out.get("date", ""))
    scored = 0
    for pick in out.get("picks") or []:
        pick["access_url"] = institution_url(profile.get("institution_template", ""), pick.get("doi", ""))
        pick["doi_url"] = doi_url(pick.get("doi", "")) if pick.get("doi") else ""
        pick["read_status_label"] = READ_STATUS.get(pick.get("read_status", "unread"), "待读")
        scores = pick.get("scores") or {}
        if scores:
            scored += 1
            pick["total"] = round(sum(scores.values()) / len(scores), 2)
            pick["total_pct"] = round(pick["total"] / 5 * 100)
        else:
            pick["total"] = None
            pick["total_pct"] = None
    out["scored_count"] = scored
    out["count"] = len(out.get("picks") or [])
    return out


def total_of(pick: dict) -> float | None:
    scores = pick.get("scores") or {}
    if not scores:
        return None
    return round(sum(scores.values()) / len(scores), 2)


def compute_stats(state: dict) -> dict:
    days = state.get("days") or {}
    all_picks = []
    day_stats = []
    for date_key in sorted(days):
        day = days[date_key]
        picks = day.get("picks") or []
        scored_picks = [p for p in picks if p.get("scores")]
        for pick in scored_picks:
            all_picks.append(pick)
        avg = round(sum(total_of(p) or 0 for p in scored_picks) / len(scored_picks), 2) if scored_picks else None
        day_stats.append({
            "date": date_key,
            "count": len(picks),
            "scored": len(scored_picks),
            "avg": avg,
            "focus": (day.get("preference") or {}).get("focus", []),
        })
    scored_count = len(all_picks)
    dim_avg = {}
    for key in DIMENSION_KEYS:
        vals = [p["scores"][key] for p in all_picks if key in (p.get("scores") or {})]
        dim_avg[key] = round(sum(vals) / len(vals), 2) if vals else None
    total_avg = round(sum(total_of(p) or 0 for p in all_picks) / scored_count, 2) if scored_count else None
    journals, fields = {}, {}
    for day in days.values():
        for pick in day.get("picks") or []:
            journal = (pick.get("journal") or "").strip()
            if journal and journal != "未标注期刊":
                journals[journal] = journals.get(journal, 0) + 1
            for field in pick.get("fields") or []:
                fields[field.get("name", "")] = fields.get(field.get("name", ""), 0) + 1

    # 连续打分天数：从今天（或昨天）向前数
    active = {s["date"] for s in day_stats if s["scored"] > 0}
    today = date.today()
    cursor = today if today.isoformat() in active else today - timedelta(days=1)
    streak = 0
    while cursor.isoformat() in active:
        streak += 1
        cursor -= timedelta(days=1)

    last14 = []
    for offset in range(13, -1, -1):
        d = today - timedelta(days=offset)
        key = d.isoformat()
        row = next((s for s in day_stats if s["date"] == key), None)
        last14.append({
            "date": key,
            "label": f"{d.month}/{d.day}",
            "count": row["count"] if row else 0,
            "scored": row["scored"] if row else 0,
            "avg": row["avg"] if row else None,
        })

    total_recommended = sum(s["count"] for s in day_stats)
    read_count = sum(1 for day in days.values() for p in day.get("picks") or []
                     if p.get("read_status") in ("read", "skimmed"))
    return {
        "days": len(day_stats),
        "total_recommended": total_recommended,
        "scored_count": scored_count,
        "scored_rate": round(scored_count / total_recommended * 100) if total_recommended else 0,
        "avg_total": total_avg,
        "dimension_avg": dim_avg,
        "streak": streak,
        "journals": [{"name": k, "count": v} for k, v in sorted(journals.items(), key=lambda kv: -kv[1])[:6]],
        "fields": [{"name": k, "count": v} for k, v in sorted(fields.items(), key=lambda kv: -kv[1])[:6] if k],
        "last14": last14,
        "read_count": read_count,
        "day_stats": day_stats[-30:],
    }


def history_rows(state: dict, limit: int = 60) -> list[dict]:
    days = state.get("days") or {}
    rows = []
    for date_key in sorted(days, reverse=True)[:limit]:
        day = days[date_key]
        picks = []
        for pick in day.get("picks") or []:
            picks.append({
                "id": pick.get("id"),
                "title": pick.get("title"),
                "journal": pick.get("journal"),
                "doi": pick.get("doi"),
                "published": pick.get("published"),
                "total": total_of(pick),
                "scores": pick.get("scores") or {},
                "fields": [f.get("name") for f in pick.get("fields") or []],
                "read_status": pick.get("read_status", "unread"),
            })
        scored = [p for p in picks if p["total"] is not None]
        rows.append({
            "date": date_key,
            "issue": issue_number(days, date_key),
            "picks": picks,
            "scored": len(scored),
            "count": len(picks),
            "avg": round(sum(p["total"] for p in scored) / len(scored), 2) if scored else None,
            "preference": day.get("preference"),
            "generated_at": day.get("generated_at"),
        })
    return rows
