# -*- coding: utf-8 -*-
"""可独立打开 / 打印的日报，以及 CSV 导出。"""
from __future__ import annotations

import csv
import html
import io
import json
import urllib.parse
from datetime import datetime

from daily import (
    APP_NAME,
    APP_EN_NAME,
    APP_TAGLINE,
    DIMENSIONS,
    DIMENSION_MAP,
    READ_STATUS,
    SOURCE_MAP,
    VERSION,
    period_for,
    public_profile,
)
from recommend import decorate_day, issue_number, total_of


def e(value) -> str:
    return html.escape(str(value if value is not None else ""), quote=True)


def _score_dots(value) -> str:
    if not value:
        return '<span class="pending">待评分</span>'
    dots = "".join(f'<i class="{"on" if i <= int(value) else ""}"></i>' for i in range(1, 6))
    return f'<span class="dots">{dots}</span><b>{int(value)}</b><small>/5</small>'


def _stars_total(total) -> str:
    if total is None:
        return '<span class="pending">待评分</span>'
    return f'<b class="avg">{total:.1f}</b><small>/5</small>'


def report_css() -> str:
    return """
    :root{--paper:#f7f2e8;--ink:#1d2823;--muted:#6e756b;--line:#d8d0c1;--accent:#b24e36;--green:#2f5d50;--gold:#9a7b45}
    *{box-sizing:border-box}
    body{margin:0;background:#e9e2d5;color:var(--ink);font-family:"Noto Serif SC","Songti SC","STSong",SimSun,Georgia,serif;line-height:1.65}
    .sheet{width:210mm;min-height:297mm;margin:20px auto;background:var(--paper);padding:18mm 17mm 16mm;box-shadow:0 18px 50px rgba(38,31,20,.18);position:relative;overflow:hidden}
    .sheet:before{content:"";position:absolute;inset:0;pointer-events:none;background:radial-gradient(circle at 12% 10%,rgba(255,255,255,.55),transparent 38%),repeating-linear-gradient(0deg,rgba(80,60,30,.018) 0 1px,transparent 1px 4px)}
    .top{display:flex;justify-content:space-between;align-items:flex-start;border-bottom:2px solid var(--ink);padding-bottom:12px;position:relative;z-index:1}
    .brand{font-size:30px;letter-spacing:.18em;font-weight:700;line-height:1.1}
    .brand span{font-size:10px;letter-spacing:.28em;color:var(--accent);display:block;margin-top:5px;font-family:Arial,sans-serif}
    .issue{text-align:right;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:11px;color:var(--muted);line-height:1.9}
    .issue b{font-family:Georgia,serif;font-size:18px;color:var(--ink);font-weight:400}
    .headline{display:flex;justify-content:space-between;align-items:flex-end;margin:24px 0 8px;position:relative;z-index:1}
    h1{font-size:30px;margin:0;letter-spacing:.04em;font-weight:600}
    .date{font-family:Arial,"Microsoft YaHei",sans-serif;color:var(--muted);font-size:12px;letter-spacing:.06em}
    .lede{font-size:12.5px;color:#4d554d;margin:4px 0 18px;max-width:155mm;position:relative;z-index:1}
    .meta-strip{display:flex;flex-wrap:wrap;gap:8px 18px;border-top:1px solid var(--line);border-bottom:1px solid var(--line);padding:8px 0;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:10.5px;color:var(--muted);margin-bottom:18px;position:relative;z-index:1}
    .meta-strip b{color:var(--ink);font-weight:600;margin-left:3px}
    .meta-strip span+span::before{content:"·";color:#b7ad9d;margin-right:12px}
    .paper{padding:16px 0 18px;border-bottom:1px solid var(--line);position:relative;z-index:1;break-inside:avoid}
    .paper:last-of-type{border-bottom:0}
    .paper-head{display:flex;gap:14px;align-items:flex-start}
    .num{font-family:Georgia,serif;font-size:42px;line-height:1;color:var(--accent);min-width:38px;font-style:italic}
    .paper-body{flex:1;min-width:0}
    h2{font-size:20px;line-height:1.48;margin:0 0 8px;font-weight:600}
    .byline{font-size:11.5px;color:#4d554d;margin-bottom:4px}
    .venue{font-family:Arial,"Microsoft YaHei",sans-serif;font-size:10.5px;color:var(--muted);line-height:1.7}
    .venue a{color:var(--accent);text-decoration:none;border-bottom:1px dotted #b24e36}
    .chips{display:flex;gap:5px;flex-wrap:wrap;margin:8px 0 0}
    .chip{font-family:Arial,"Microsoft YaHei",sans-serif;font-size:9px;letter-spacing:.03em;border:1px solid #cfc6b6;padding:1px 6px;color:#4d554d;border-radius:99px}
    .abstract{font-size:11.8px;color:#3c453e;margin:10px 0 0;text-align:justify;line-height:1.78}
    .abstract.dim{color:#7c8178}
    .scorebox{margin-top:11px;border:1px solid #d3caba;background:rgba(255,255,255,.35);padding:9px 11px}
    .score-head{display:flex;justify-content:space-between;align-items:baseline;font-family:Arial,"Microsoft YaHei",sans-serif;margin-bottom:4px}
    .score-head .label{font-size:9.5px;letter-spacing:.16em;color:var(--muted);text-transform:uppercase}
    .score-table{width:100%;border-collapse:collapse}
    .score-table td{padding:3.5px 0;vertical-align:middle;font-family:Arial,"Microsoft YaHei",sans-serif;font-size:10.5px;border-bottom:1px dotted #e0d8ca}
    .score-table tr:last-child td{border-bottom:0}
    .score-table .dim{width:58px;font-weight:600}
    .score-table .hint{color:#7a8077;font-size:9.5px;line-height:1.35}
    .dots{display:inline-flex;gap:3px;vertical-align:middle;margin-right:4px}
    .dots i{display:inline-block;width:7px;height:7px;border-radius:50%;background:#ded6c8}
    .dots i.on{background:var(--green)}
    .pending{font-family:Arial,"Microsoft YaHei",sans-serif;color:#a69c8c;font-size:10px}
    .avg{font-family:Georgia,serif;font-size:20px;font-weight:500;color:var(--accent)}
    .score-table small{font-family:Arial,sans-serif;color:var(--muted);font-size:9px}
    .note{margin-top:8px;font-size:10.5px;color:#565e55;border-left:2px solid var(--gold);padding-left:8px;font-style:italic}
    .pref{margin-top:18px;border:1px solid #cdc4b4;padding:12px 14px;background:rgba(255,255,255,.35);position:relative;z-index:1;break-inside:avoid}
    .pref .k{font-family:Arial,"Microsoft YaHei",sans-serif;font-size:9px;letter-spacing:.18em;color:var(--accent);text-transform:uppercase}
    .pref p{margin:5px 0 0;font-size:11.5px}
    .empty{padding:35px 0;text-align:center;color:var(--muted);font-size:12px}
    footer{display:flex;justify-content:space-between;margin-top:22px;padding-top:9px;border-top:1px solid var(--line);font-family:Arial,"Microsoft YaHei",sans-serif;font-size:9px;color:#8b8172;position:relative;z-index:1}
    .toolbar{position:fixed;right:24px;bottom:24px;display:flex;gap:8px;z-index:10;font-family:Arial,"Microsoft YaHei",sans-serif}
    .toolbar button,.toolbar a{border:0;background:var(--ink);color:#fff;padding:10px 16px;font-size:12px;text-decoration:none;cursor:pointer;letter-spacing:.04em}
    .toolbar button:hover,.toolbar a:hover{background:var(--accent)}
    @media print{body{background:#fff}.sheet{box-shadow:none;margin:0;width:auto;min-height:0;padding:14mm}.toolbar{display:none}@page{size:A4;margin:0}}
    @media(max-width:800px){.sheet{width:auto;margin:0;padding:22px}.headline{display:block}h1{font-size:26px}.date{margin-top:8px}}
    """


def build_report_html(day: dict, profile: dict, generated_only: bool = False) -> str:
    date_str = day.get("date", "")
    issue = day.get("issue") or 1
    meta = day.get("meta") or {}
    picks = day.get("picks") or []
    pref = day.get("preference") or {}
    focus = [DIMENSION_MAP[k]["label"] for k in pref.get("focus", []) if k in DIMENSION_MAP]
    snapshot_fields = day.get("field_snapshot") or meta.get("fields") or profile.get("fields") or []
    field_names = "、".join(f.get("name", "") for f in snapshot_fields) or "未设置"
    source_names = "、".join(s.get("name", "") for s in meta.get("sources", []) if s.get("ok")) or "—"
    preference_source = meta.get("preference_source")
    gate_threshold = float(meta.get("min_venue_citedness") or 0)
    gate_html = ""
    if gate_threshold > 0:
        missing_label = "保留" if meta.get("venue_missing_policy") == "include" else "排除"
        gate_html = (
            f'<span>期刊门槛 <b>2 年均被引 ≥ {gate_threshold:g}</b></span>'
            f'<span>过滤 <b>{meta.get("venue_filtered", 0)}</b> 篇（缺失{missing_label}）</span>'
        )
    lead = f"本期从 {meta.get('candidate_count', 0)} 篇候选文献中选读 {len(picks)} 篇。"
    if preference_source:
        lead += f"排序参考了 {preference_source} 的阅读偏好。"
    else:
        lead += "这是你的第一期选读，排序采用均衡权重。"

    cards = []
    for idx, pick in enumerate(picks, 1):
        scores = pick.get("scores") or {}
        total = total_of(pick)
        fields = "".join(f'<span class="chip">{e(f.get("name"))}</span>' for f in (pick.get("fields") or []))
        sources = "".join(
            f'<span class="chip">{e(SOURCE_MAP.get(s, {}).get("name", s))}</span>'
            for s in (pick.get("sources") or [])
        )
        abstract = pick.get("abstract") or ""
        abstract_html = (
            f'<p class="abstract">{e(abstract[:1100])}{"…" if len(abstract) > 1100 else ""}</p>'
            if abstract else '<p class="abstract dim">该条记录暂未提供摘要，建议通过 DOI 或机构入口查看原文。</p>'
        )
        rows = []
        for dim in DIMENSIONS:
            value = scores.get(dim["key"])
            signal = (pick.get("signals") or {}).get(dim["key"], "")
            rows.append(
                f'<tr><td class="dim" style="color:{dim["color"]}">{e(dim["label"])}</td>'
                f'<td>{_score_dots(value)}</td>'
                f'<td class="hint">{e(signal)}</td></tr>'
            )
        note = clean_note(pick.get("score_note"))
        note_html = f'<div class="note">阅读札记：{e(note)}</div>' if note else ""
        read_status = READ_STATUS.get(pick.get("read_status", "unread"), "待读")
        doi = pick.get("doi") or ""
        venue_bits = [pick.get("journal") or "未标注期刊"]
        if pick.get("year"):
            venue_bits.append(str(pick["year"]))
        if pick.get("published"):
            venue_bits.append(pick["published"])
        doi_html = f'DOI <a href="{e(pick.get("doi_url") or "")}">{e(doi)}</a>' if doi else "DOI 暂缺"
        access_html = ""
        if pick.get("access_url"):
            access_html = f' · <a href="{e(pick["access_url"])}">机构入口</a>'
        elif pick.get("oa_url"):
            access_html = f' · <a href="{e(pick["oa_url"])}">开放全文</a>'
        scholar = "https://scholar.google.com/scholar?q=" + urllib.parse.quote(pick.get("title") or "")
        access_html += f' · <a href="{e(scholar)}">Google Scholar</a>'
        cards.append(f"""
        <article class="paper">
          <div class="paper-head">
            <div class="num">{idx:02d}</div>
            <div class="paper-body">
              <h2>{e(pick.get("title"))}</h2>
              <div class="byline">{e(pick.get("author_line") or "作者信息暂缺")}</div>
              <div class="venue">{e(" · ".join(venue_bits))} &nbsp;|&nbsp; {doi_html}{access_html}</div>
              <div class="chips">{fields}{sources}<span class="chip">{e(read_status)}</span></div>
              {abstract_html}
              <div class="scorebox">
                <div class="score-head"><span class="label">Reading Score · 我的评分</span><span>{_stars_total(total)}</span></div>
                <table class="score-table">{"".join(rows)}</table>
                {note_html}
              </div>
            </div>
          </div>
        </article>""")

    pref_html = ""
    if focus or pref.get("note"):
        pref_html = f"""
        <div class="pref">
          <div class="k">Next Issue · 明日倾向</div>
          <p><b>{"、".join(focus) if focus else "均衡探索"}</b>{("　" + e(pref.get("note"))) if pref.get("note") else ""}</p>
        </div>"""
    elif not generated_only:
        pref_html = """
        <div class="pref"><div class="k">Next Issue · 明日倾向</div>
        <p>尚未设置。完成评分后，可在「知新」中选择下一期更看重的维度。</p></div>"""

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{APP_NAME} · {e(date_str)} 文献日报</title>
<style>{report_css()}</style></head>
<body>
<div class="sheet">
  <div class="top">
    <div class="brand">{APP_NAME}<span>{APP_EN_NAME} · DAILY READING</span></div>
    <div class="issue">第 <b>{issue:02d}</b> 期<br>{e(period_for(date_str))}<br>{e(date_str)}</div>
  </div>
  <div class="headline">
    <h1>今日选读</h1>
    <div class="date">{e(profile.get("display_name") or "研究者")} · {e(field_names)}</div>
  </div>
  <p class="lede">{e(lead)}</p>
  <div class="meta-strip">
    <span>研究领域 <b>{e(field_names)}</b></span>
    <span>数据源 <b>{e(source_names)}</b></span>
    <span>候选 <b>{meta.get("candidate_count", 0)}</b></span>
    {gate_html}
    <span>生成于 <b>{e((day.get("generated_at") or "")[:16].replace("T", " "))}</b></span>
  </div>
  {''.join(cards) if cards else '<div class="empty">今日尚无推荐记录</div>'}
  {pref_html}
  <footer><span>{APP_TAGLINE} · 由 {APP_NAME} {VERSION} 生成</span><span>数据仅保存在本机 · 评分仅代表个人阅读判断</span></footer>
</div>
<div class="toolbar"><button onclick="window.print()">打印 / 存为 PDF</button></div>
</body></html>"""


def clean_note(value) -> str:
    text = str(value or "").strip()
    return text[:1200]


def build_csv(state: dict) -> str:
    output = io.StringIO()
    output.write("\ufeff")
    writer = csv.writer(output)
    writer.writerow([
        "日期", "期号", "研究领域", "标题", "作者", "期刊", "年份", "DOI", "发表日期",
        "相关性", "专业性", "可靠性", "创新性", "启发性", "均分", "阅读状态", "阅读札记",
        "明日倾向", "备注",
    ])
    days = state.get("days") or {}
    for date_key in sorted(days):
        day = days[date_key]
        pref = day.get("preference") or {}
        focus = "、".join(DIMENSION_MAP[k]["label"] for k in pref.get("focus", []) if k in DIMENSION_MAP)
        for pick in day.get("picks") or []:
            scores = pick.get("scores") or {}
            total = total_of(pick)
            writer.writerow([
                date_key,
                issue_number(days, date_key),
                "、".join(f.get("name", "") for f in pick.get("fields") or []),
                pick.get("title", ""),
                pick.get("author_line", ""),
                pick.get("journal", ""),
                pick.get("year", ""),
                pick.get("doi", ""),
                pick.get("published", ""),
                scores.get("relevance", ""),
                scores.get("expertise", ""),
                scores.get("reliability", ""),
                scores.get("novelty", ""),
                scores.get("inspiration", ""),
                total if total is not None else "",
                READ_STATUS.get(pick.get("read_status", "unread"), "待读"),
                pick.get("score_note", ""),
                focus,
                pref.get("note", ""),
            ])
    return output.getvalue()


def build_backup(state: dict) -> str:
    safe = json.loads(json.dumps(state, ensure_ascii=False))
    profile = safe.get("profile") or {}
    profile.pop("openalex_api_key", None)
    safe["exported_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
    safe["app_version"] = VERSION
    return json.dumps(safe, ensure_ascii=False, indent=2)
