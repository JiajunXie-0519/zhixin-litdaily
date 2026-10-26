# -*- coding: utf-8 -*-
"""把知新的阅读记录写成 Obsidian 友好的 Markdown 笔记。

只写入 vault 下配置的子文件夹（默认 知新/），不碰用户的其他笔记。
生成的文献笔记带 `zhixin: true` 标记；重复同步会更新自动区，
并保留 `<!-- zhixin:user-notes:start -->` 与结束标记之间的个人补充。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import urllib.parse
from datetime import datetime
from pathlib import Path

from daily import (
    APP_NAME,
    DIMENSIONS,
    DIMENSION_MAP,
    READ_STATUS,
    SOURCE_MAP,
    VERSION,
    clean_text,
    doi_url,
    institution_url,
    normalize_obsidian_folder,
    parse_date,
)

USER_START = "<!-- zhixin:user-notes:start -->"
USER_END = "<!-- zhixin:user-notes:end -->"


class ObsidianError(Exception):
    """面向用户的 Obsidian 同步错误。"""


def _yaml(value) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _yaml_list(key: str, values: list) -> str:
    if not values:
        return f"{key}: []"
    lines = [f"{key}:"]
    lines.extend(f"  - {_yaml(v)}" for v in values)
    return "\n".join(lines)


def _safe_name(value: str, limit: int = 60) -> str:
    text = clean_text(value, 300)
    text = re.sub(r'[\x00-\x1f<>:"/\\|?*\[\]#^]+', " ", text)
    text = re.sub(r"\s+", " ", text).strip(" .")
    return (text[:limit].rstrip(" .") or "未命名")


def _md_cell(value) -> str:
    text = str(value or "")
    text = text.replace("\r", " ").replace("\n", " ").replace("|", "\\|")
    return text.strip()


def _wikilink(target: str, label: str) -> str:
    safe_label = str(label or "").replace("|", "｜").replace("[", "［").replace("]", "］")
    safe_label = safe_label.replace("\n", " ").strip()
    return f"[[{target}|{safe_label}]]"


def _first_author(pick: dict) -> str:
    authors = [a for a in (pick.get("authors") or []) if a]
    if not authors:
        return "Unknown"
    name = authors[0]
    if "," in name:
        name = name.split(",")[0]
    name = re.sub(r"[^\w\u4e00-\u9fff .'-]", "", name, flags=re.UNICODE)
    return _safe_name(name, 28) or "Unknown"


def _paper_year(pick: dict, fallback_date: str) -> str:
    year = str(pick.get("year") or "")
    if re.match(r"^\d{4}$", year):
        return year
    published = str(pick.get("published") or "")
    if re.match(r"^\d{4}", published):
        return published[:4]
    return fallback_date[:4]


def _paper_filename(record: dict) -> str:
    pick = record["pick"]
    year = _paper_year(pick, record["first_date"])
    author = _first_author(pick)
    title = _safe_name(pick.get("title") or "未命名文献", 48)
    identity = pick.get("doi") or pick.get("id") or pick.get("title") or ""
    digest = hashlib.sha1(identity.encode("utf-8")).hexdigest()[:6]
    return f"{year} - {author} - {title} ({digest})"


def _write(path: Path, content: str, preserve_user_block: bool = False, base: Path | None = None) -> bool:
    """写入笔记；内容未变时跳过，避免 Obsidian 每次同步都重建文件。"""
    if base is not None:
        resolved_base = base.resolve()
        resolved_parent = path.parent.resolve()
        if resolved_parent != resolved_base and resolved_base not in resolved_parent.parents:
            raise ObsidianError("拒绝写入 vault 子文件夹之外的路径")
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = None
    if path.exists():
        if path.is_dir():
            raise ObsidianError(f"目标位置是一个文件夹，无法写入：{path.name}")
        existing = path.read_text(encoding="utf-8", errors="replace")
        if "zhixin: true" not in existing[:1200]:
            raise ObsidianError(
                f"目标文件已存在且不是知新生成的笔记：{path.name}。"
                "为避免覆盖，请在设置里换一个独立的 Obsidian 子文件夹。"
            )
        if preserve_user_block:
            match = re.search(re.escape(USER_START) + r"(.*?)" + re.escape(USER_END), existing, flags=re.S)
            if match:
                content = re.sub(
                    re.escape(USER_START) + r".*?" + re.escape(USER_END),
                    USER_START + match.group(1) + USER_END,
                    content,
                    flags=re.S,
                )
        if content == existing:
            return False
    tmp = path.with_name(path.name + ".zhixin.tmp")
    try:
        tmp.write_text(content, encoding="utf-8", newline="\n")
        os.replace(tmp, path)
    finally:
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
    return True


def _frontmatter(lines: list[str]) -> str:
    return "---\n" + "\n".join(lines) + "\n---\n\n"


def _link_line(label: str, url: str) -> str:
    if not url:
        return ""
    return f"- [{label}]({url})"


def _paper_note(record: dict, profile: dict) -> str:
    pick = record["pick"]
    scores = pick.get("scores") or {}
    total = record["total"]
    fields = [f.get("name") for f in (pick.get("fields") or []) if f.get("name")]
    authors = [a for a in (pick.get("authors") or []) if a]
    status_key = pick.get("read_status") or "unread"
    status = READ_STATUS.get(status_key, "待读")
    doi = pick.get("doi") or ""
    url = pick.get("doi_url") or (doi_url(doi) if doi else "")
    scholar = "https://scholar.google.com/scholar?q=" + urllib.parse.quote(pick.get("title") or "")
    access = institution_url(profile.get("institution_template", ""), doi)
    dates = record["dates"]
    fm = [
        "zhixin: true",
        "type: literature",
        f"title: {_yaml(pick.get('title') or '未命名文献')}",
        _yaml_list("aliases", [pick.get("title") or "未命名文献"]),
        f"journal: {_yaml(pick.get('journal') or '')}",
        f"year: {_yaml(pick.get('year') or '')}",
        f"doi: {_yaml(doi)}",
        f"url: {_yaml(url)}",
        f"published: {_yaml(pick.get('published') or '')}",
        f"recommended: {_yaml(dates[0])}",
        f"last_recommended: {_yaml(dates[-1])}",
        _yaml_list("recommended_dates", dates),
        _yaml_list("authors", authors),
        _yaml_list("fields", fields),
        _yaml_list("tags", ["literature", "zhixin"]),
        f"rating: {total}",
        "status_key: " + _yaml(status_key),
        f"read_status: {_yaml(status)}",
        f"scored_at: {_yaml(pick.get('scored_at') or '')}",
    ]
    for dim in DIMENSIONS:
        fm.append(f"{dim['key']}: {scores.get(dim['key'], 'null')}")
    fm.append(_yaml_list("sources", [SOURCE_MAP.get(s, {}).get("name", s) for s in (pick.get("sources") or [])]))
    fm.extend(["generator: \"知新\"", f"generator_version: {_yaml(VERSION)}"])

    info_lines = [
        f"> **作者**：{', '.join(authors) if authors else '作者信息暂缺'}",
        f"> **期刊**：{pick.get('journal') or '未标注期刊'}",
        f"> **发表**：{pick.get('published') or pick.get('year') or '未知'}",
        f"> **DOI**：{doi or '暂缺'}",
        f"> **阅读状态**：{status}",
        f"> **综合评分**：{total:.1f} / 5",
        f"> **推荐日期**：{'、'.join(dates)}",
    ]
    score_rows = []
    for dim in DIMENSIONS:
        value = scores.get(dim["key"])
        signal = (pick.get("signals") or {}).get(dim["key"], "")
        score_rows.append(
            f"| {dim['label']} | {value if value is not None else '—'} | {_md_cell(signal)} |"
        )
    why = [w for w in (pick.get("why") or []) if w]
    why_block = "\n".join(f"- {w}" for w in why) if why else "- 暂无"
    abstract = pick.get("abstract") or ""
    abstract = re.sub(r"(?m)^#{1,6}\s+", "", abstract).strip()
    abstract_block = abstract if abstract else "_这条记录没有提供摘要，请通过 DOI 查看原文。_"
    note = (pick.get("score_note") or "").strip()
    note_block = note if note else "_还没有写札记。_"
    daily_links = "\n".join(
        f"- {_wikilink(record['daily_targets'][d], d + ' 日报')}" for d in dates
    )
    links = [
        _link_line("DOI / 原文", url),
        _link_line("Google Scholar", scholar),
        _link_line("开放全文", pick.get("oa_url") or ""),
        _link_line("机构入口", access),
    ]
    links_block = "\n".join(x for x in links if x)
    return _frontmatter(fm) + f"""# {pick.get('title') or '未命名文献'}

> [!info] 文献信息
{chr(10).join(info_lines)}

## 摘要

{abstract_block}

## 我的评分

| 维度 | 分数 | 推荐时的信号 |
|---|---:|---|
{chr(10).join(score_rows)}

**综合评分：{total:.1f} / 5**

## 阅读札记

{note_block}

## 入选理由

{why_block}

## 推荐记录

{daily_links}

## 链接

{links_block}

## 我的补充

{USER_START}
（可在这里写自己的延伸思考；重新同步时会保留这一段。）
{USER_END}
"""


def _daily_note(day: dict, issue: int, records_by_id: dict, paper_targets: dict,
                folder: str) -> str:
    picks = day.get("picks") or []
    scored = [p for p in picks if p.get("scores")]
    total = round(sum(sum(p["scores"].values()) / len(p["scores"]) for p in scored) / len(scored), 2) if scored else None
    pref = day.get("preference") or {}
    focus = [DIMENSION_MAP[k]["label"] for k in pref.get("focus", []) if k in DIMENSION_MAP]
    fm = [
        "zhixin: true",
        "type: daily",
        f"date: {_yaml(day.get('date'))}",
        f"issue: {issue}",
        f"count: {len(picks)}",
        f"scored: {len(scored)}",
        f"average: {total if total is not None else 'null'}",
        _yaml_list("focus", focus),
        _yaml_list("tags", ["zhixin", "reading-diary"]),
        "generator: \"知新\"",
        f"generator_version: {_yaml(VERSION)}",
    ]
    sections = []
    for i, pick in enumerate(picks, 1):
        pid = pick.get("id") or ""
        title = pick.get("title") or "未命名文献"
        if pid in paper_targets:
            heading = f"### {i}. {_wikilink(paper_targets[pid], title)}"
        else:
            heading = f"### {i}. {title}"
        score = ""
        if pick.get("scores"):
            score = f"{sum(pick['scores'].values()) / len(pick['scores']):.1f} / 5"
        elif pick.get("total") is not None:
            score = f"{pick['total']:.1f} / 5"
        meta = f"- **期刊**：{pick.get('journal') or '未标注期刊'}"
        if pick.get("published"):
            meta += f" · **发表**：{pick['published']}"
        meta += f"\n- **评分**：{score or '待评分'}"
        meta += f"\n- **状态**：{READ_STATUS.get(pick.get('read_status') or 'unread', '待读')}"
        why = "；".join(pick.get("why") or [])
        if why:
            meta += f"\n- **入选理由**：{why}"
        sections.append(heading + "\n\n" + meta)
    if pref:
        pref_text = "、".join(focus) if focus else "均衡探索"
        if pref.get("note"):
            pref_text += f"\n\n{pref['note']}"
        pref_block = f"## 明日倾向\n\n{pref_text}"
    else:
        pref_block = "## 明日倾向\n\n_尚未设置。_"
    return _frontmatter(fm) + f"""# {day.get('date')} · 文献日报

> 第 {issue:02d} 期 · {day.get('date')} · 本期 {len(picks)} 篇，已评 {len(scored)} 篇。

## 今日选读

{chr(10).join(sections) if sections else '_今天没有推荐记录。_'}

{pref_block}

---

返回 {_wikilink(folder + '/索引', '知新阅读索引')}
"""


def _index_note(records: list, daily_items: list, folder: str) -> str:
    records = sorted(records, key=lambda r: (r["dates"][-1], r["pick"].get("title") or ""), reverse=True)
    rated = [r["total"] for r in records]
    avg = round(sum(rated) / len(rated), 2) if rated else None
    fm = [
        "zhixin: true",
        "type: index",
        f"count: {len(records)}",
        f"average: {avg if avg is not None else 'null'}",
        f"updated: {_yaml(datetime.now().astimezone().isoformat(timespec='seconds'))}",
        _yaml_list("tags", ["zhixin", "index"]),
        "generator: \"知新\"",
        f"generator_version: {_yaml(VERSION)}",
    ]
    rows = []
    for rec in records:
        pick = rec["pick"]
        fields = " / ".join(f.get("name") for f in (pick.get("fields") or []) if f.get("name"))
        rows.append(
            f"| {rec['dates'][-1]} | {_wikilink(rec['target'], pick.get('title') or '')} | "
            f"{_md_cell(pick.get('journal'))} | {rec['total']:.1f} | "
            f"{_md_cell(READ_STATUS.get(pick.get('read_status') or 'unread', '待读'))} | "
            f"{_md_cell(fields)} |"
        )
    table = "\n".join(rows) if rows else "| — | 还没有评分记录 | — | — | — | — |"
    field_counts = {}
    for rec in records:
        for field in rec["pick"].get("fields") or []:
            name = field.get("name") or ""
            if name:
                field_counts[name] = field_counts.get(name, 0) + 1
    field_lines = "\n".join(f"- {k}：{v} 篇" for k, v in sorted(field_counts.items(), key=lambda kv: -kv[1])) or "- 暂无"
    diary_lines = "\n".join(
        f"- {_wikilink(item['target'], item['date'] + ' 日报')}" for item in daily_items[-30:]
    ) or "- 暂无"
    return _frontmatter(fm) + f"""# 知新 · 阅读索引

> 自动维护的阅读档案，共 {len(records)} 篇已评文献。评分与札记来自知新；最后更新于 {datetime.now().astimezone().strftime('%Y-%m-%d %H:%M')}。

## 全部文献

| 最近推荐 | 文献 | 期刊 | 评分 | 状态 | 领域 |
|---|---|---|---:|---|---|
{table}

## 按领域

{field_lines}

## 最近日报

{diary_lines}

---

生成器：{APP_NAME} {VERSION} · 自动生成，请勿手动修改自动区。
"""


def sync_state(state: dict) -> dict:
    """把 state 中的阅读记录同步到 Obsidian，返回统计。

    只写入 profile 配置的 vault/子文件夹。文献笔记只针对已经评分的记录；
    日报每天生成；索引只包含已评分文献。
    """
    profile = state.get("profile") or {}
    if not profile.get("obsidian_enabled"):
        raise ObsidianError("Obsidian 同步尚未启用")
    raw_vault = (profile.get("obsidian_vault") or "").strip()
    if not raw_vault:
        raise ObsidianError("请先填写 Obsidian vault 路径")
    vault = Path(os.path.expanduser(os.path.expandvars(raw_vault)))
    if not vault.exists() or not vault.is_dir():
        raise ObsidianError("找不到 Obsidian vault 文件夹，请检查路径")
    vault = vault.resolve()
    folder = normalize_obsidian_folder(profile.get("obsidian_folder") or "知新")
    base = (vault / folder).resolve()
    if base != vault and vault not in base.parents:
        raise ObsidianError("Obsidian 子文件夹必须在 vault 内")
    try:
        base.mkdir(parents=True, exist_ok=True)
    except Exception as exc:
        raise ObsidianError(f"无法创建 Obsidian 子文件夹：{exc}") from exc
    if not base.is_dir():
        raise ObsidianError("Obsidian 子文件夹路径不是文件夹")

    days = state.get("days") or {}
    records: dict[str, dict] = {}
    for date_key in sorted(days):
        day = days[date_key]
        for pick in day.get("picks") or []:
            scores = pick.get("scores") or {}
            if not scores:
                continue
            pid = pick.get("id") or pick.get("doi") or pick.get("title") or ""
            if not pid:
                continue
            total = round(sum(scores.values()) / len(scores), 2)
            if pid not in records:
                records[pid] = {
                    "pick": pick,
                    "dates": [date_key],
                    "daily_targets": {},
                    "total": total,
                }
            else:
                rec = records[pid]
                if date_key not in rec["dates"]:
                    rec["dates"].append(date_key)
                old_score = rec["pick"].get("scored_at") or ""
                new_score = pick.get("scored_at") or ""
                if new_score >= old_score:
                    rec["pick"] = pick
                    rec["total"] = total

    paper_targets: dict[str, str] = {}
    papers_written = 0
    for pid, rec in records.items():
        rec["dates"] = sorted(set(rec["dates"]))
        rec["first_date"] = rec["dates"][0]
        year = _paper_year(rec["pick"], rec["first_date"])
        filename = _paper_filename(rec)
        target = f"{folder}/文献/{year}/{filename}"
        path = base / "文献" / year / (filename + ".md")
        rec["target"] = target
        paper_targets[pid] = target
        rec["daily_targets"] = {
            d: f"{folder}/日报/{d}" for d in rec["dates"]
        }
        if _write(path, _paper_note(rec, profile), preserve_user_block=True, base=base):
            papers_written += 1

    daily_written = 0
    daily_items = []
    for date_key in sorted(days):
        day = days[date_key]
        if not day.get("picks"):
            continue
        issue = sorted(days).index(date_key) + 1
        target = f"{folder}/日报/{date_key}"
        path = base / "日报" / (date_key + ".md")
        # 让文献笔记里的日报链接与当前日期一致
        if _write(path, _daily_note(day, issue, records, paper_targets, folder), base=base):
            daily_written += 1
        daily_items.append({"date": date_key, "target": target})

    index_path = base / "索引.md"
    index_written = _write(index_path, _index_note(list(records.values()), daily_items, folder), base=base)
    synced_at = datetime.now().astimezone().isoformat(timespec="seconds")
    return {
        "ok": True,
        "papers": papers_written,
        "daily": daily_written,
        "index": 1 if index_written else 0,
        "folder": str(base),
        "synced_at": synced_at,
    }
