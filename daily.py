# -*- coding: utf-8 -*-
"""知新 · 文献日报 —— 数据、抓取、排序与报表。

纯 Python 标准库，无第三方依赖。数据默认保存在 data/state.json。
"""
from __future__ import annotations

import copy
import csv
import hashlib
import html
import io
import json
import math
import os
import re
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta
from pathlib import Path

APP_NAME = "知新"
APP_EN_NAME = "ZHIXIN"
APP_TAGLINE = "每天几篇，读有所得"
VERSION = "1.1.0"
STATE_VERSION = 1

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.environ.get("ZHIXIN_DATA_DIR", str(BASE_DIR / "data"))).expanduser()
REPORT_DIR = BASE_DIR / "reports"
STATE_PATH = DATA_DIR / "state.json"

DIMENSIONS = [
    {
        "key": "relevance",
        "label": "相关性",
        "short": "贴题",
        "hint": "与你的研究方向、关键词的贴合程度",
        "low": "偏离方向",
        "high": "正中靶心",
        "color": "#2F5D50",
    },
    {
        "key": "expertise",
        "label": "专业性",
        "short": "专业",
        "hint": "方法深度、论证完整度与领域门槛",
        "low": "泛泛而谈",
        "high": "硬核扎实",
        "color": "#3F6B8A",
    },
    {
        "key": "reliability",
        "label": "可靠性",
        "short": "可靠",
        "hint": "证据强度、发表渠道与结果可核验性",
        "low": "证据薄弱",
        "high": "可以采信",
        "color": "#8A6A3F",
    },
    {
        "key": "novelty",
        "label": "创新性",
        "short": "新颖",
        "hint": "新问题、新方法或新发现的程度",
        "low": "重复已知",
        "high": "耳目一新",
        "color": "#B24E36",
    },
    {
        "key": "inspiration",
        "label": "启发性",
        "short": "启发",
        "hint": "能否带来跨领域联想或新的选题",
        "low": "无甚波澜",
        "high": "打开思路",
        "color": "#6C5A86",
    },
]
DIMENSION_KEYS = [d["key"] for d in DIMENSIONS]
DIMENSION_MAP = {d["key"]: d for d in DIMENSIONS}

SOURCE_CATALOG = [
    {
        "key": "openalex",
        "name": "OpenAlex",
        "scope": "全学科",
        "note": "开放学术图谱，覆盖 2.5 亿+ 作品与摘要",
        "color": "#2F5D50",
        "default": True,
    },
    {
        "key": "crossref",
        "name": "Crossref",
        "scope": "DOI 元数据",
        "note": "全球 DOI 注册库，补齐期刊与出版信息",
        "color": "#B24E36",
        "default": True,
    },
    {
        "key": "arxiv",
        "name": "arXiv",
        "scope": "预印本",
        "note": "数理、计算机、工程等前沿预印本",
        "color": "#A33B33",
        "default": True,
    },
    {
        "key": "europepmc",
        "name": "Europe PMC",
        "scope": "生命医学",
        "note": "PubMed / PMC 生命科学文献",
        "color": "#3F6B8A",
        "default": True,
    },
]
SOURCE_KEYS = [s["key"] for s in SOURCE_CATALOG]
SOURCE_MAP = {s["key"]: s for s in SOURCE_CATALOG}

DEFAULT_WEIGHTS = {
    "relevance": 30,
    "expertise": 15,
    "reliability": 20,
    "novelty": 18,
    "inspiration": 17,
}

READ_STATUS = {
    "unread": "待读",
    "skimmed": "已略读",
    "read": "已精读",
    "archived": "已归档",
}

METHOD_TERMS = [
    "method", "methodology", "experiment", "experimental", "dataset", "data set",
    "analysis", "analytical", "model", "models", "modeling", "simulation",
    "benchmark", "evaluation", "evaluate", "cohort", "trial", "randomized",
    "longitudinal", "survey", "interview", "ethnograph", "case study",
    "regression", "identification", "causal", "ablation", "framework",
    "algorithm", "theorem", "proof", "derivation", "measurement", "validation",
    "replication", "systematic review", "meta-analysis", "qualitative",
    "quantitative", "mixed methods",
]
NOVELTY_TERMS = [
    "novel", "new", "first", "emerging", "unprecedented", "rethinking",
    "reconceptual", "breakthrough", "paradigm", "frontier", "next-generation",
    "innovative", "advance", "challenge", "beyond",
]
INSPIRATION_TERMS = [
    "implication", "implications", "future", "interdisciplinary", "cross-disciplinary",
    "perspective", "framework", "agenda", "opportunity", "challenge", "insight",
    "theory", "conceptual", "rethink", "bridge", "translation", "open question",
]
GENERIC_TERMS = {
    "a", "an", "the", "and", "or", "of", "for", "in", "on", "to", "with",
    "study", "research", "paper", "review", "analysis", "using", "based",
}

USER_AGENT = f"ZhixinLitDaily/{VERSION} (personal daily literature digest)"

_ARXIV_LOCK = threading.Lock()
_ARXIV_LAST = 0.0


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def today_str() -> str:
    return date.today().isoformat()


def parse_date(value) -> date | None:
    if not value:
        return None
    if isinstance(value, date) and not isinstance(value, datetime):
        return value
    text = str(value).strip()
    for fmt in ("%Y-%m-%d", "%Y-%m", "%Y"):
        try:
            return datetime.strptime(text[: len(fmt) + 2], fmt).date()
        except Exception:
            continue
    m = re.search(r"(19|20)\d{2}-\d{2}-\d{2}", text)
    if m:
        try:
            return datetime.strptime(m.group(0), "%Y-%m-%d").date()
        except Exception:
            pass
    return None


def clamp(value: float, low: float = 0, high: float = 100) -> float:
    return max(low, min(high, value))


def safe_int(value, default=0) -> int:
    try:
        return int(float(value))
    except Exception:
        return default


def clean_text(value, limit: int = 10000) -> str:
    if value is None:
        return ""
    text = html.unescape(str(value))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def normalize_doi(value) -> str:
    doi = clean_text(value, 300).lower().strip()
    doi = re.sub(r"^(https?://(dx\.)?doi\.org/|doi:\s*)", "", doi)
    doi = doi.strip(" .;,")
    if not re.match(r"^10\.\d{4,9}/\S+$", doi):
        return ""
    return doi


def doi_url(doi: str) -> str:
    return "https://doi.org/" + urllib.parse.quote(doi, safe="/:;()<>[]")


def title_key(title: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", clean_text(title).lower())[:120]


def make_id(doi: str, title: str) -> str:
    if doi:
        return "doi:" + doi
    digest = hashlib.sha1(title_key(title).encode("utf-8")).hexdigest()[:16]
    return "title:" + digest


def split_terms(raw) -> list[str]:
    if isinstance(raw, list):
        parts = raw
    else:
        parts = re.split(r"[,，;；\n]+", str(raw or ""))
    result, seen = [], set()
    for part in parts:
        term = clean_text(part, 80).strip(" .。")
        if not term:
            continue
        key = term.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(term)
    return result


def is_ascii(text: str) -> bool:
    return all(ord(ch) < 128 for ch in text)


def term_pattern(term: str) -> re.Pattern:
    escaped = re.escape(term.strip())
    escaped = escaped.replace(r"\ ", r"\s+")
    if is_ascii(term):
        return re.compile(r"(?<![a-z0-9])" + escaped + r"(?![a-z0-9])", re.I)
    return re.compile(escaped, re.I)


def contains_term(haystack: str, term: str) -> bool:
    if not haystack or not term or len(term.strip()) < 2:
        return False
    return bool(term_pattern(term).search(haystack))


def matched_terms(text: str, terms: list[str]) -> list[str]:
    return [t for t in terms if contains_term(text, t)]


def unique_keep(items):
    seen, out = set(), []
    for item in items:
        key = str(item).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def safe_template(value: str) -> str:
    text = clean_text(value, 500)
    if not text:
        return ""
    if not text.lower().startswith(("https://", "http://")):
        return ""
    return text


def normalize_zotero_api(value: str) -> str:
    text = clean_text(value, 300).strip().rstrip("/") or "http://127.0.0.1:23119/api/users/0"
    parsed = urllib.parse.urlparse(text)
    if parsed.scheme not in ("http", "https") or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("Zotero 本地 API 地址必须是本机的 http://127.0.0.1:23119/…")
    path = parsed.path.rstrip("/")
    if path in ("", "/"):
        path = "/api/users/0"
    elif path == "/api":
        path = "/api/users/0"
    return urllib.parse.urlunparse((parsed.scheme, parsed.netloc, path, "", "", ""))


def normalize_obsidian_folder(value: str) -> str:
    """只允许 vault 内的相对子目录，禁止盘符、.. 和 Windows 非法字符。"""
    text = clean_text(value, 100).strip().replace("\\", "/").strip("/")
    if not text:
        return "知新"
    parts = [p.strip() for p in text.split("/") if p.strip()]
    if not parts:
        return "知新"
    if len(parts) > 4:
        raise ValueError("Obsidian 子文件夹层级不要超过 4 层")
    for part in parts:
        if part in (".", "..") or part.startswith("."):
            raise ValueError("Obsidian 子文件夹不能以 . 开头，也不能包含 ..")
        if ":" in part or re.search(r'[<>"|?*\x00-\x1f]', part):
            raise ValueError("Obsidian 子文件夹名包含 Windows 不允许的字符")
    result = "/".join(parts)
    if len(result) > 80:
        raise ValueError("Obsidian 子文件夹名太长")
    return result


def institution_url(template: str, doi: str) -> str:
    if not template or not doi:
        return ""
    target = doi_url(doi)
    if "{doi}" in template:
        return template.replace("{doi}", urllib.parse.quote(doi, safe="/:;()<>[]"))
    if "{url}" in template:
        return template.replace("{url}", urllib.parse.quote(target, safe=""))
    return template + ("" if template.endswith("=") else "") + urllib.parse.quote(target, safe="")


def period_for(date_str: str) -> str:
    d = parse_date(date_str) or date.today()
    return f"{d.year} 年 {d.month} 月 {d.day} 日"


def issue_number(days: dict, date_str: str) -> int:
    if date_str in days:
        return sorted(days.keys()).index(date_str) + 1
    return len([d for d in days if d < date_str]) + 1


# ---------------------------------------------------------------------------
# 数据存储
# ---------------------------------------------------------------------------

def default_profile() -> dict:
    return {
        "display_name": "",
        "fields": [],
        "daily_count": 2,
        "min_venue_citedness": 0.0,
        "venue_missing_policy": "exclude",
        "sources": [s["key"] for s in SOURCE_CATALOG if s.get("default", True)],
        "ranking_weights": dict(DEFAULT_WEIGHTS),
        "institution_template": "",
        "contact_email": "",
        "openalex_api_key": "",
        "lookback_years": 3,
        "obsidian_enabled": False,
        "obsidian_vault": "",
        "obsidian_folder": "知新",
        "obsidian_auto_sync": True,
        "zotero_enabled": False,
        "zotero_api": "http://127.0.0.1:23119/api/users/0",
        "zotero_read_tags": "/done, done, read, 已读",
        "zotero_reading_tags": "/reading, reading, 在读",
        "zotero_unread_tags": "/unread, unread, 未读",
        "zotero_exclude_mode": "read",
        "zotero_use_preferences": True,
    }


def default_state() -> dict:
    return {
        "version": STATE_VERSION,
        "created_at": now_iso(),
        "updated_at": now_iso(),
        "profile": default_profile(),
        "days": {},
        "last_meta": {},
    }


class Store:
    """极轻量的 JSON 存储；原子写入、单进程内加锁、每次保存前自动备份。"""

    BACKUP_LIMIT = 100

    def __init__(self, path: Path = STATE_PATH):
        self.path = path
        self.backup_dir = self.path.parent / "backups"
        self._lock = threading.RLock()
        self._last_backup_hash = ""
        self.state = self._load()

    def _normalize(self, data: dict) -> dict:
        if not isinstance(data, dict):
            raise ValueError("state is not an object")
        base = default_state()
        base.update(data)
        base["profile"] = {**default_profile(), **(data.get("profile") or {})}
        base["profile"]["ranking_weights"] = {
            **DEFAULT_WEIGHTS,
            **(base["profile"].get("ranking_weights") or {}),
        }
        base.setdefault("days", {})
        return base

    def _backup_raw(self, raw: bytes, force: bool = False) -> str | None:
        if not raw:
            return None
        digest = hashlib.sha256(raw).hexdigest()
        if not force and digest == self._last_backup_hash:
            return None
        self.backup_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        target = self.backup_dir / f"state-{stamp}-{digest[:8]}.json"
        tmp = target.with_name(target.name + ".tmp")
        try:
            tmp.write_bytes(raw)
            os.replace(tmp, target)
        finally:
            try:
                if tmp.exists():
                    tmp.unlink()
            except Exception:
                pass
        self._last_backup_hash = digest
        self._prune_backups()
        return target.name

    def _prune_backups(self):
        try:
            files = sorted(self.backup_dir.glob("state-*.json"), key=lambda p: p.name)
            for old in files[:-self.BACKUP_LIMIT]:
                old.unlink(missing_ok=True)
        except Exception as exc:
            print(f"[warn] 清理旧备份失败：{exc}", file=sys.stderr)

    def _load(self) -> dict:
        if not self.path.exists():
            # 状态文件被误删时，优先恢复最近一份自动备份，而不是直接开空档
            candidates = sorted(self.backup_dir.glob("state-*.json"), key=lambda p: p.name)
            if candidates:
                try:
                    raw = candidates[-1].read_bytes()
                    restored = self._normalize(json.loads(raw.decode("utf-8")))
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    tmp = self.path.with_suffix(".restore.tmp")
                    tmp.write_bytes(raw)
                    os.replace(tmp, self.path)
                    self._last_backup_hash = hashlib.sha256(raw).hexdigest()
                    print(f"[warn] state.json 不存在，已从最近备份恢复：{candidates[-1].name}", file=sys.stderr)
                    return restored
                except Exception as exc:
                    print(f"[warn] 尝试从备份恢复失败：{exc}", file=sys.stderr)
            return default_state()
        raw = b""
        try:
            raw = self.path.read_bytes()
            try:
                self._backup_raw(raw)
            except Exception as exc:
                print(f"[warn] 启动备份失败：{exc}", file=sys.stderr)
            return self._normalize(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            # 保留损坏文件，避免静默覆盖
            backup = self.path.with_suffix(f".corrupt-{int(time.time())}.json")
            try:
                if raw:
                    self._backup_raw(raw, force=True)
                self.path.replace(backup)
            except Exception:
                pass
            print(f"[warn] 无法读取数据文件，已备份到 {backup}: {exc}", file=sys.stderr)
            return default_state()

    def snapshot(self) -> dict:
        with self._lock:
            return copy.deepcopy(self.state)

    def save(self, state: dict | None = None) -> dict:
        with self._lock:
            if state is not None:
                self.state = state
            self.state["updated_at"] = now_iso()
            self.path.parent.mkdir(parents=True, exist_ok=True)
            if self.path.exists():
                try:
                    self._backup_raw(self.path.read_bytes())
                except Exception as exc:
                    raise RuntimeError(f"自动备份失败，已取消本次保存：{exc}") from exc
            tmp = self.path.with_suffix(".tmp")
            payload = json.dumps(self.state, ensure_ascii=False, indent=2).encode("utf-8")
            tmp.write_bytes(payload)
            os.replace(tmp, self.path)
            return copy.deepcopy(self.state)

    def update(self, fn):
        with self._lock:
            data = copy.deepcopy(self.state)
            result = fn(data)
            self.save(data)
            return result

    def list_backups(self, limit: int = 30) -> list[dict]:
        if not self.backup_dir.exists():
            return []
        items = []
        for path in sorted(self.backup_dir.glob("state-*.json"), key=lambda p: p.name, reverse=True)[:limit]:
            stat = path.stat()
            items.append({
                "name": path.name,
                "size": stat.st_size,
                "created_at": datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds"),
            })
        return items

    def create_backup(self) -> str | None:
        with self._lock:
            if not self.path.exists():
                return None
            return self._backup_raw(self.path.read_bytes(), force=True)

    def restore_backup(self, name: str) -> dict:
        safe_name = Path(str(name or "")).name
        if not safe_name.startswith("state-") or not safe_name.endswith(".json"):
            raise ValueError("备份文件名无效")
        target = self.backup_dir / safe_name
        if not target.is_file():
            raise ValueError("找不到这个备份")
        raw = target.read_bytes()
        try:
            restored = self._normalize(json.loads(raw.decode("utf-8")))
        except Exception as exc:
            raise ValueError("备份文件已损坏，无法恢复") from exc
        with self._lock:
            if self.path.exists():
                self._backup_raw(self.path.read_bytes(), force=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".restore.tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, self.path)
            self.state = restored
            self._last_backup_hash = hashlib.sha256(raw).hexdigest()
        return copy.deepcopy(self.state)


# ---------------------------------------------------------------------------
# 资料校验
# ---------------------------------------------------------------------------

def normalize_field(raw: dict, index: int = 0) -> dict:
    raw = raw or {}
    name = clean_text(raw.get("name"), 60).strip()
    keywords = split_terms(raw.get("keywords"))[:10]
    if not name:
        name = keywords[0] if keywords else f"研究领域 {index + 1}"
    if not keywords and is_ascii(name):
        keywords = [name]
    field_id = clean_text(raw.get("id"), 40)
    if not re.match(r"^[a-zA-Z0-9_-]{3,40}$", field_id or ""):
        field_id = "f_" + secrets.token_hex(4)
    color = clean_text(raw.get("color"), 20)
    if not re.match(r"^#[0-9a-fA-F]{6}$", color or ""):
        palette = ["#2F5D50", "#B24E36", "#3F6B8A", "#8A6A3F", "#6C5A86", "#5C6B3A"]
        color = palette[index % len(palette)]
    return {
        "id": field_id,
        "name": name[:60],
        "keywords": keywords,
        "color": color,
        "weight": round(clamp(float(raw.get("weight", 1) or 1), 0.2, 3.0), 2),
    }


def validate_profile(payload: dict, previous: dict | None = None) -> dict:
    previous = previous or default_profile()
    profile = default_profile()
    profile.update({k: previous.get(k) for k in profile if k in previous})

    profile["display_name"] = clean_text(payload.get("display_name", previous.get("display_name")), 40).strip()

    raw_fields = payload.get("fields")
    if isinstance(raw_fields, list):
        fields = [normalize_field(f, i) for i, f in enumerate(raw_fields[:6]) if f]
        profile["fields"] = [f for f in fields if f["keywords"]][:6]
    else:
        profile["fields"] = previous.get("fields", [])

    try:
        profile["daily_count"] = int(clamp(float(payload.get("daily_count", previous.get("daily_count", 2))), 1, 5))
    except Exception:
        profile["daily_count"] = 2

    try:
        profile["min_venue_citedness"] = round(clamp(
            float(payload.get("min_venue_citedness", previous.get("min_venue_citedness", 0)) or 0), 0, 50
        ), 2)
    except Exception:
        profile["min_venue_citedness"] = 0.0
    missing_policy = payload.get("venue_missing_policy", previous.get("venue_missing_policy", "exclude"))
    profile["venue_missing_policy"] = "include" if missing_policy == "include" else "exclude"

    sources = payload.get("sources", previous.get("sources", SOURCE_KEYS))
    if not isinstance(sources, list):
        sources = SOURCE_KEYS
    profile["sources"] = [s for s in SOURCE_KEYS if s in sources] or ["openalex"]

    raw_weights = payload.get("ranking_weights", previous.get("ranking_weights", DEFAULT_WEIGHTS))
    weights = {}
    for key in DIMENSION_KEYS:
        try:
            weights[key] = int(clamp(float(raw_weights.get(key, DEFAULT_WEIGHTS[key])), 1, 60))
        except Exception:
            weights[key] = DEFAULT_WEIGHTS[key]
    profile["ranking_weights"] = weights

    template = payload.get("institution_template", previous.get("institution_template", ""))
    profile["institution_template"] = safe_template(template)
    profile["contact_email"] = clean_text(payload.get("contact_email", previous.get("contact_email", "")), 120).strip()

    # 空密钥 = 保留原值；用户明确清除时才删除
    incoming_key = payload.get("openalex_api_key")
    if payload.get("clear_openalex_key"):
        profile["openalex_api_key"] = ""
    elif isinstance(incoming_key, str) and incoming_key.strip() and "•" not in incoming_key:
        profile["openalex_api_key"] = incoming_key.strip()[:200]

    try:
        profile["lookback_years"] = int(clamp(float(payload.get("lookback_years", previous.get("lookback_years", 3))), 1, 5))
    except Exception:
        profile["lookback_years"] = 3

    profile["obsidian_enabled"] = bool(payload.get("obsidian_enabled", previous.get("obsidian_enabled", False)))
    profile["obsidian_auto_sync"] = bool(payload.get("obsidian_auto_sync", previous.get("obsidian_auto_sync", True)))
    profile["obsidian_folder"] = normalize_obsidian_folder(
        payload.get("obsidian_folder", previous.get("obsidian_folder", "知新"))
    )
    raw_vault = clean_text(payload.get("obsidian_vault", previous.get("obsidian_vault", "")), 500).strip()
    if profile["obsidian_enabled"]:
        if not raw_vault:
            raise ValueError("启用 Obsidian 同步前，请先填写 vault 文件夹路径")
        expanded = os.path.expanduser(os.path.expandvars(raw_vault))
        vault_path = Path(expanded)
        if not vault_path.exists() or not vault_path.is_dir():
            raise ValueError("找不到 Obsidian vault 文件夹，请检查路径是否正确")
        try:
            profile["obsidian_vault"] = str(vault_path.resolve())
        except Exception as exc:
            raise ValueError("无法解析 Obsidian vault 路径") from exc
    else:
        profile["obsidian_vault"] = raw_vault

    profile["zotero_enabled"] = bool(payload.get("zotero_enabled", previous.get("zotero_enabled", False)))
    profile["zotero_use_preferences"] = bool(
        payload.get("zotero_use_preferences", previous.get("zotero_use_preferences", True))
    )
    try:
        profile["zotero_api"] = normalize_zotero_api(
            payload.get("zotero_api", previous.get("zotero_api", "http://127.0.0.1:23119/api/users/0"))
        )
    except ValueError:
        if profile["zotero_enabled"]:
            raise
        profile["zotero_api"] = "http://127.0.0.1:23119/api/users/0"
    profile["zotero_read_tags"] = clean_text(
        payload.get("zotero_read_tags", previous.get("zotero_read_tags", "/done, done, read, 已读")), 300
    )
    profile["zotero_reading_tags"] = clean_text(
        payload.get("zotero_reading_tags", previous.get("zotero_reading_tags", "/reading, reading, 在读")), 300
    )
    profile["zotero_unread_tags"] = clean_text(
        payload.get("zotero_unread_tags", previous.get("zotero_unread_tags", "/unread, unread, 未读")), 300
    )
    mode = payload.get("zotero_exclude_mode", previous.get("zotero_exclude_mode", "read"))
    profile["zotero_exclude_mode"] = mode if mode in ("read", "all", "off") else "read"
    return profile


def public_profile(profile: dict) -> dict:
    out = copy.deepcopy(profile)
    key = out.pop("openalex_api_key", "")
    out["has_openalex_key"] = bool(key)
    return out


def preference_for_date(state: dict, date_str: str) -> tuple[dict | None, str | None]:
    candidates = []
    for day_key, day in (state.get("days") or {}).items():
        if day_key < date_str and day.get("preference"):
            candidates.append((day_key, day["preference"]))
    if not candidates:
        return None, None
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1], candidates[-1][0]


def effective_weights(profile: dict, preference: dict | None) -> tuple[dict, dict]:
    """把次日偏好转换成排序权重，返回 (原始权重, 归一化百分比)。"""
    weights = {k: float(profile.get("ranking_weights", DEFAULT_WEIGHTS).get(k, DEFAULT_WEIGHTS[k])) for k in DIMENSION_KEYS}
    focus = (preference or {}).get("focus") or []
    for key in focus:
        if key in weights:
            weights[key] *= 1.9
    total = sum(weights.values()) or 1
    pct = {k: round(weights[k] / total * 100, 1) for k in DIMENSION_KEYS}
    return weights, pct
