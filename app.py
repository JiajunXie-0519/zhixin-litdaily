# -*- coding: utf-8 -*-
"""知新 · 文献日报 —— 本地 Web 服务。

运行：python app.py
默认地址：http://127.0.0.1:8765
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.parse
import urllib.request
import webbrowser
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from daily import (
    APP_EN_NAME,
    APP_NAME,
    APP_TAGLINE,
    BASE_DIR,
    DATA_DIR,
    DIMENSION_KEYS,
    DIMENSIONS,
    READ_STATUS,
    REPORT_DIR,
    SOURCE_CATALOG,
    SOURCE_KEYS,
    STATE_PATH,
    VERSION,
    Store,
    clean_text,
    default_profile,
    effective_weights,
    institution_url,
    parse_date,
    preference_for_date,
    public_profile,
    safe_template,
    today_str,
    validate_profile,
)
from recommend import (
    build_day,
    compute_stats,
    decorate_day,
    history_rows,
    personal_signals,
    prior_exclusions,
    total_of,
)
import zotero
from obsidian import ObsidianError, sync_state
from report import build_backup, build_csv, build_report_html
from sources import SourceError, fetch_candidates

WEB_DIR = BASE_DIR / "web"
GENERATE_LOCK = threading.Lock()
STARTED_AT = time.time()
RUNTIME_PATH = DATA_DIR / "runtime.json"
ZOTERO_CACHE_PATH = DATA_DIR / "zotero-cache.json"


def log(message: str):
    stamp = datetime.now().strftime("%H:%M:%S")
    line = f"[{stamp}] {message}"
    print(line, flush=True)
    # pythonw 启动时没有控制台；保留一份文件日志便于排查。
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        path = DATA_DIR / "server.log"
        if path.exists() and path.stat().st_size > 256 * 1024:
            try:
                os.replace(path, DATA_DIR / "server.log.1")
            except Exception:
                pass
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except Exception:
        pass


def is_configured(profile: dict) -> bool:
    return bool([f for f in (profile.get("fields") or []) if f.get("keywords")])


def validate_day_key(value: str) -> str:
    text = str(value or "").strip()
    if not re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        raise ValueError("日期格式应为 YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(text)
    except Exception as exc:
        raise ValueError("日期无效") from exc
    if parsed > date.today() + timedelta(days=1):
        raise ValueError("不能生成未来日期的推荐")
    return text


def latest_preference_hint(state: dict, date_str: str) -> dict:
    pref, source = preference_for_date(state, date_str)
    if pref:
        return {
            "focus": pref.get("focus") or [],
            "note": pref.get("note") or "",
            "source": source,
        }
    return {"focus": [], "note": "", "source": None}


def _read_zotero_cache() -> dict | None:
    if not ZOTERO_CACHE_PATH.exists():
        return None
    try:
        data = json.loads(ZOTERO_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def _write_zotero_cache(library: dict):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = ZOTERO_CACHE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(library, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    os.replace(tmp, ZOTERO_CACHE_PATH)


def _zotero_age_seconds(cache: dict | None) -> float:
    if not cache:
        return 10 ** 9
    try:
        then = datetime.fromisoformat(str(cache.get("fetched_at")))
        return max(0.0, (datetime.now().astimezone() - then).total_seconds())
    except Exception:
        return 10 ** 9


def refresh_zotero(store: Store, force: bool = False) -> dict | None:
    """读取 Zotero 本地 API；失败时退回上一次缓存。"""
    state = store.snapshot()
    profile = state.get("profile") or {}
    if not profile.get("zotero_enabled"):
        return None
    cache = _read_zotero_cache()
    if not force and cache and _zotero_age_seconds(cache) < 600:
        return {**cache, "ok": True, "cached": True, "error": ""}
    try:
        library = zotero.fetch_library(profile)
        _write_zotero_cache(library)
        error = ""
    except Exception as exc:
        error = str(exc)
        library = cache
        if not library:
            result = {"ok": False, "error": error, "fetched_at": None}
            def mutate(data):
                meta = data.get("zotero") or {}
                meta.update({"enabled": True, "error": error})
                data["zotero"] = meta
            store.update(mutate)
            return result
    result = {**library, "ok": True, "error": error, "cached": False, "stale": bool(error)}

    def mutate(data):
        data["zotero"] = {
            "enabled": True,
            "synced_at": result.get("fetched_at"),
            "total": result.get("total", 0),
            "counts": result.get("counts", {}),
            "top_tags": (result.get("top_tags") or [])[:8],
            "top_journals": (result.get("top_journals") or [])[:8],
            "error": error,
        }

    store.update(mutate)
    return result


def run_obsidian_sync(store: Store, force: bool = False) -> dict | None:
    """按配置把记录镜像到 Obsidian；失败只记录，不影响主流程。"""
    state = store.snapshot()
    profile = state.get("profile") or default_profile()
    if not profile.get("obsidian_enabled"):
        return None
    if not force and not profile.get("obsidian_auto_sync", True):
        return None
    started = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        result = sync_state(state)
    except ObsidianError as exc:
        result = {"ok": False, "error": str(exc), "synced_at": started}
    except Exception as exc:
        result = {"ok": False, "error": f"同步异常：{type(exc).__name__}: {exc}", "synced_at": started}
    if result.get("ok"):
        log(f"Obsidian 同步完成：{result.get('papers')} 篇文献，{result.get('daily')} 篇日报")
    else:
        log(f"Obsidian 同步失败：{result.get('error')}")

    def mutate(data):
        meta = data.get("obsidian") or {}
        meta["last_attempt"] = started
        meta["last_error"] = "" if result.get("ok") else result.get("error", "同步失败")
        if result.get("ok"):
            meta["last_sync"] = result.get("synced_at") or started
            meta["last_result"] = {
                "papers": result.get("papers", 0),
                "daily": result.get("daily", 0),
                "index": result.get("index", 1),
                "folder": result.get("folder", ""),
            }
        data["obsidian"] = meta

    store.update(mutate)
    return result


class Handler(BaseHTTPRequestHandler):
    server_version = f"Zhixin/{VERSION}"
    protocol_version = "HTTP/1.1"

    # ---------- 基础响应 ----------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict | None = None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        if extra:
            for key, value in extra.items():
                self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, status: int, payload: dict | list):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _error(self, status: int, message: str, detail: str = "", hint: str = ""):
        self._json(status, {"error": message, "detail": detail, "hint": hint})

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError:
            length = 0
        if length > 2 * 1024 * 1024:
            raise ValueError("请求内容过大")
        raw = self.rfile.read(length) if length else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
        except Exception as exc:
            raise ValueError("请求不是有效的 JSON") from exc
        if not isinstance(data, dict):
            raise ValueError("请求内容应为对象")
        return data

    def _check_origin(self) -> bool:
        origin = self.headers.get("Origin")
        if not origin:
            return True
        host = self.headers.get("Host", "")
        parsed = urllib.parse.urlparse(origin)
        return parsed.netloc == host and parsed.scheme in ("http", "https")

    def log_message(self, fmt, *args):
        # 静态资源不刷屏，保留 API 错误
        if self.path.startswith("/api/"):
            log(f"{self.command} {self.path} → {args[1] if len(args) > 1 else ''}")

    # ---------- 路由 ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        query = urllib.parse.parse_qs(parsed.query)
        try:
            if path in ("/", "/index.html"):
                return self._serve_static("index.html")
            if path.startswith("/assets/"):
                return self._serve_static(path[len("/assets/"):])
            if path == "/favicon.svg":
                return self._serve_static("favicon.svg")
            if path == "/api/bootstrap":
                return self.api_bootstrap()
            if path == "/api/day":
                return self.api_day(query)
            if path == "/api/history":
                return self.api_history(query)
            if path == "/api/stats":
                return self.api_stats()
            if path == "/api/report":
                return self.api_report(query)
            if path == "/api/export.csv":
                return self.api_export_csv()
            if path == "/api/export.json":
                return self.api_export_json()
            if path == "/api/backups":
                return self.api_backups()
            if path == "/api/health":
                return self._json(200, {"ok": True, "app": APP_NAME, "version": VERSION,
                                        "uptime": round(time.time() - STARTED_AT)})
            return self._error(404, "页面不存在")
        except ValueError as exc:
            return self._error(400, str(exc))
        except Exception as exc:
            log(f"[error] {self.command} {self.path}: {type(exc).__name__}: {exc}")
            return self._error(500, "服务器内部错误", f"{type(exc).__name__}: {exc}")

    def do_POST(self):
        parsed = urllib.parse.urlparse(self.path)
        path = parsed.path
        if not self._check_origin():
            return self._error(403, "拒绝跨站请求")
        try:
            payload = self._read_json()
            if path == "/api/profile":
                return self.api_profile(payload)
            if path == "/api/generate":
                return self.api_generate(payload)
            if path == "/api/score":
                return self.api_score(payload)
            if path == "/api/preference":
                return self.api_preference(payload)
            if path == "/api/obsidian/sync":
                return self.api_obsidian_sync()
            if path == "/api/backups/create":
                return self.api_backup_create()
            if path == "/api/backups/restore":
                return self.api_backup_restore(payload)
            if path == "/api/zotero/sync":
                return self.api_zotero_sync()
            if path == "/api/shutdown":
                return self.api_shutdown()
            if path == "/api/reset-day":
                return self.api_reset_day(payload)
            return self._error(404, "接口不存在")
        except ValueError as exc:
            return self._error(400, str(exc))
        except SourceError as exc:
            return self._source_error(exc)
        except Exception as exc:
            log(f"[error] {self.command} {self.path}: {type(exc).__name__}: {exc}")
            return self._error(500, "服务器内部错误", f"{type(exc).__name__}: {exc}")

    # ---------- 静态文件 ----------
    def _serve_static(self, name: str):
        safe = Path(name)
        if safe.is_absolute() or ".." in safe.parts:
            return self._error(404, "页面不存在")
        target = WEB_DIR / safe
        if not target.is_file():
            return self._error(404, "页面不存在")
        mime = {
            ".html": "text/html; charset=utf-8",
            ".js": "text/javascript; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
            ".ico": "image/x-icon",
            ".woff2": "font/woff2",
        }.get(target.suffix.lower(), "application/octet-stream")
        self._send(200, target.read_bytes(), mime)

    # ---------- 数据装配 ----------
    def _bundle(self, state: dict, day: dict | None = None) -> dict:
        profile_pub = public_profile(state.get("profile") or default_profile())
        today = today_str()
        today_day = day if day is not None else state["days"].get(today)
        decorated = decorate_day(today_day, state.get("profile") or {}, state.get("days") or {})
        return {
            "app": {
                "name": APP_NAME,
                "en": APP_EN_NAME,
                "tagline": APP_TAGLINE,
                "version": VERSION,
            },
            "today": today,
            "now": datetime.now().astimezone().isoformat(timespec="seconds"),
            "profile": profile_pub,
            "dimensions": DIMENSIONS,
            "sources": SOURCE_CATALOG,
            "read_status": READ_STATUS,
            "configured": is_configured(state.get("profile") or {}),
            "day": decorated,
            "needs_generation": bool(is_configured(state.get("profile") or {}) and not today_day),
            "preference_hint": latest_preference_hint(state, today),
            "stats": compute_stats(state),
            "history": history_rows(state, 30),
            "obsidian_status": state.get("obsidian") or {},
            "zotero_status": state.get("zotero") or {},
        }

    def api_bootstrap(self):
        state = self.store.snapshot()
        return self._json(200, self._bundle(state))

    def api_day(self, query):
        day_key = validate_day_key((query.get("date") or [today_str()])[0])
        state = self.store.snapshot()
        day = state["days"].get(day_key)
        if not day:
            return self._error(404, "该日期还没有推荐记录")
        return self._json(200, {"day": decorate_day(day, state.get("profile") or {}, state["days"]),
                                "stats": compute_stats(state)})

    def api_history(self, query):
        try:
            limit = max(1, min(200, int((query.get("limit") or ["60"])[0])))
        except Exception:
            limit = 60
        state = self.store.snapshot()
        return self._json(200, {"history": history_rows(state, limit), "stats": compute_stats(state)})

    def api_stats(self):
        state = self.store.snapshot()
        return self._json(200, {"stats": compute_stats(state), "history": history_rows(state, 30)})

    def _source_error(self, exc: SourceError):
        message = exc.message or "文献源暂时不可用"
        hint = "可稍后重试；若 OpenAlex 提示额度不足，可在设置中填写免费 API Key，或先启用 Crossref / arXiv / Europe PMC。"
        return self._error(502, message, exc.detail[:600] if exc.detail else "", hint)

    # ---------- 设置 ----------
    def api_profile(self, payload: dict):
        state = self.store.snapshot()
        profile = validate_profile(payload, state.get("profile") or default_profile())
        if not is_configured(profile):
            raise ValueError("请至少添加一个研究领域，并填写英文检索词")
        if not profile.get("sources"):
            raise ValueError("请至少启用一个文献数据源")
        was_obsidian = bool((state.get("profile") or {}).get("obsidian_enabled"))
        self.store.update(lambda data: data.__setitem__("profile", profile))
        log(f"设置已更新：{len(profile['fields'])} 个领域，{len(profile['sources'])} 个数据源")
        zotero_result = refresh_zotero(self.store, force=True) if profile.get("zotero_enabled") else None
        obsidian_result = run_obsidian_sync(self.store, force=not was_obsidian)
        payload = self._bundle(self.store.snapshot())
        if zotero_result:
            payload["zotero"] = {
                k: zotero_result.get(k) for k in (
                    "ok", "total", "counts", "top_tags", "top_journals", "fetched_at", "error"
                )
            }
        if obsidian_result:
            payload["obsidian"] = obsidian_result
        return self._json(200, payload)

    # ---------- 生成 ----------
    def api_generate(self, payload: dict):
        date_key = validate_day_key(payload.get("date") or today_str())
        force = bool(payload.get("force"))
        state = self.store.snapshot()
        profile = state.get("profile") or default_profile()
        if not is_configured(profile):
            raise ValueError("请先完成研究领域设置")
        existing = state["days"].get(date_key)
        if existing and not force:
            return self._json(200, {"day": decorate_day(existing, profile, state["days"]),
                                    "stats": compute_stats(state), "cached": True})
        if existing and force and any(p.get("scores") for p in existing.get("picks") or []):
            return self._error(409, "今天的文献已经打过分，不能再整批更换", hint="如需补选，可修改评分或明天再来。")

        with GENERATE_LOCK:
            # 等锁期间可能有另一个请求生成了同一天
            state = self.store.snapshot()
            existing = state["days"].get(date_key)
            if existing and not force:
                return self._json(200, {"day": decorate_day(existing, profile, state["days"]),
                                        "stats": compute_stats(state), "cached": True})
            profile = state.get("profile") or default_profile()
            pref, pref_source = preference_for_date(state, date_key)
            personal = personal_signals(state, date_key)
            exclude = prior_exclusions(state, date_key, days=45, include_today=force)
            zotero_excluded = 0
            zotero_status = None
            if profile.get("zotero_enabled"):
                zres = refresh_zotero(self.store, force=False)
                if zres and zres.get("ok"):
                    zitems = zres.get("items") or []
                    zex = zotero.exclusion_set(zitems, profile.get("zotero_exclude_mode", "read"))
                    exclude |= zex
                    zotero_excluded = len(zex)
                    if profile.get("zotero_use_preferences", True):
                        zpref = zotero.preference_profile(zitems, profile)
                        personal["liked"] = list(dict.fromkeys(
                            (personal.get("liked") or []) + zpref.get("terms", [])
                        ))
                        personal["journals"] = zpref.get("journals") or {}
                        personal["zotero_used"] = True
                    zotero_status = {
                        "enabled": True,
                        "total": zres.get("total", 0),
                        "counts": zres.get("counts", {}),
                        "excluded": zotero_excluded,
                        "synced_at": zres.get("fetched_at"),
                        "error": zres.get("error", ""),
                        "stale": bool(zres.get("stale")),
                    }
                else:
                    zotero_status = {
                        "enabled": True,
                        "error": (zres or {}).get("error") or "Zotero 读取失败",
                        "excluded": 0,
                    }
            log(f"开始生成 {date_key}：{len(profile.get('fields', []))} 个领域，"
                f"{len(profile.get('sources', []))} 个数据源")
            t0 = time.time()
            fetched = fetch_candidates(profile, date_key, exclude=exclude)
            day = build_day(fetched, profile, date_key, pref, pref_source, personal)
            if zotero_status:
                day["meta"]["zotero"] = zotero_status
                if zotero_status.get("error"):
                    day["meta"].setdefault("warnings", []).append(
                        "Zotero：" + zotero_status["error"]
                    )
            if force and existing:
                day["revision"] = int(existing.get("revision", 1)) + 1
            latest = self.store.snapshot()
            latest["days"][date_key] = day
            latest["last_meta"] = {
                "date": date_key,
                "generated_at": day["generated_at"],
                "candidate_count": day["meta"].get("candidate_count"),
                "sources": day["meta"].get("sources"),
                "warnings": day["meta"].get("warnings"),
            }
            self.store.save(latest)
            obsidian_result = run_obsidian_sync(self.store)
            elapsed = time.time() - t0
            log(f"生成完成 {date_key}：{len(day['picks'])} 篇，候选 {day['meta'].get('candidate_count')}，耗时 {elapsed:.1f}s")
            state = self.store.snapshot()
            payload = {
                "day": decorate_day(state["days"][date_key], profile, state["days"]),
                "stats": compute_stats(state),
                "history": history_rows(state, 30),
                "cached": False,
                "elapsed": round(elapsed, 1),
                "warnings": day["meta"].get("warnings", []),
            }
            if obsidian_result:
                payload["obsidian"] = obsidian_result
            return self._json(200, payload)

    # ---------- 打分 ----------
    def api_score(self, payload: dict):
        date_key = validate_day_key(payload.get("date") or today_str())
        pick_id = clean_text(payload.get("id"), 200)
        raw_scores = payload.get("scores")
        if not isinstance(raw_scores, dict):
            raise ValueError("评分数据缺失")
        scores = {}
        for key in DIMENSION_KEYS:
            try:
                value = int(raw_scores.get(key))
            except Exception:
                raise ValueError(f"请为「{key}」选择 1–5 分")
            if value < 1 or value > 5:
                raise ValueError("每项评分应在 1–5 之间")
            scores[key] = value
        note = clean_text(payload.get("score_note"), 1500)
        read_status = payload.get("read_status", "unread")
        if read_status not in READ_STATUS:
            read_status = "unread"
        state = self.store.snapshot()
        day = state["days"].get(date_key)
        if not day:
            raise ValueError("该日期还没有推荐记录")
        pick = next((p for p in day.get("picks") or [] if p.get("id") == pick_id), None)
        if not pick:
            raise ValueError("没有找到这篇文献")

        def mutate(data):
            current_day = data["days"].get(date_key)
            if not current_day:
                raise ValueError("该日期还没有推荐记录")
            current_pick = next((p for p in current_day.get("picks") or [] if p.get("id") == pick_id), None)
            if not current_pick:
                raise ValueError("没有找到这篇文献")
            current_pick["scores"] = scores
            current_pick["score_note"] = note
            current_pick["read_status"] = read_status
            current_pick["scored_at"] = datetime.now().astimezone().isoformat(timespec="seconds")
            current_pick["total"] = total_of(current_pick)

        self.store.update(mutate)
        obsidian_result = run_obsidian_sync(self.store)
        state = self.store.snapshot()
        payload = {
            "day": decorate_day(state["days"][date_key], state.get("profile") or {}, state["days"]),
            "stats": compute_stats(state),
            "history": history_rows(state, 30),
            "saved": True,
        }
        if obsidian_result:
            payload["obsidian"] = obsidian_result
        return self._json(200, payload)

    # ---------- 次日偏好 ----------
    def api_preference(self, payload: dict):
        date_key = validate_day_key(payload.get("date") or today_str())
        focus = payload.get("focus") or []
        if not isinstance(focus, list):
            raise ValueError("偏好格式无效")
        focus = [k for k in focus if k in DIMENSION_KEYS]
        note = clean_text(payload.get("note"), 500)
        state = self.store.snapshot()
        day = state["days"].get(date_key)
        if not day:
            raise ValueError("该日期还没有推荐记录")
        _, pct = effective_weights(state.get("profile") or {}, {"focus": focus})
        preference = {
            "focus": focus,
            "note": note,
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "for_date": (parse_date(date_key) + timedelta(days=1)).isoformat(),
            "effective_weights": pct,
        }

        def mutate(data):
            current_day = data["days"].get(date_key)
            if not current_day:
                raise ValueError("该日期还没有推荐记录")
            current_day["preference"] = preference

        self.store.update(mutate)
        obsidian_result = run_obsidian_sync(self.store)
        state = self.store.snapshot()
        payload = {
            "day": decorate_day(state["days"][date_key], state.get("profile") or {}, state["days"]),
            "preference_hint": latest_preference_hint(state, today_str()),
            "stats": compute_stats(state),
            "saved": True,
        }
        if obsidian_result:
            payload["obsidian"] = obsidian_result
        return self._json(200, payload)

    def api_zotero_sync(self):
        state = self.store.snapshot()
        profile = state.get("profile") or {}
        if not profile.get("zotero_enabled"):
            raise ValueError("请先在设置中启用 Zotero 联动")
        result = refresh_zotero(self.store, force=True)
        if not result or not result.get("ok"):
            message = (result or {}).get("error") or "Zotero 同步失败"
            return self._error(400, message)
        summary = {k: result.get(k) for k in (
            "total", "counts", "top_tags", "top_journals", "fetched_at", "cached", "stale", "error"
        )}
        summary["ok"] = True
        return self._json(200, {
            "ok": True,
            "zotero": summary,
            "zotero_status": self.store.snapshot().get("zotero") or {},
        })

    def api_obsidian_sync(self):
        state = self.store.snapshot()
        profile = state.get("profile") or {}
        if not profile.get("obsidian_enabled"):
            raise ValueError("请先在设置中启用 Obsidian 同步")
        result = run_obsidian_sync(self.store, force=True)
        if not result or not result.get("ok"):
            message = (result or {}).get("error") or "Obsidian 同步失败"
            return self._error(400, message)
        state = self.store.snapshot()
        return self._json(200, {
            "ok": True,
            "result": result,
            "obsidian_status": state.get("obsidian") or {},
        })

    def api_shutdown(self):
        def stop():
            time.sleep(0.45)
            log("收到退出请求，服务正在关闭")
            self.server.shutdown()

        threading.Thread(target=stop, daemon=True).start()
        return self._json(200, {"ok": True, "message": "服务正在退出"})

    def api_backups(self):
        return self._json(200, {"backups": self.store.list_backups(30)})

    def api_backup_create(self):
        name = self.store.create_backup()
        if not name:
            raise ValueError("还没有可备份的数据")
        return self._json(200, {"ok": True, "name": name, "backups": self.store.list_backups(30)})

    def api_backup_restore(self, payload: dict):
        name = clean_text(payload.get("name"), 200)
        if not name:
            raise ValueError("请选择要恢复的备份")
        self.store.restore_backup(name)
        obsidian_result = run_obsidian_sync(self.store, force=True)
        state = self.store.snapshot()
        result = self._bundle(state)
        result["restored"] = name
        if obsidian_result:
            result["obsidian"] = obsidian_result
        return self._json(200, result)

    def api_reset_day(self, payload: dict):
        date_key = validate_day_key(payload.get("date") or today_str())
        state = self.store.snapshot()
        day = state["days"].get(date_key)
        if not day:
            raise ValueError("该日期还没有推荐记录")
        if any(p.get("scores") for p in day.get("picks") or []):
            raise ValueError("已有评分，不能清空。可在设置中备份数据后手动编辑。")
        state["days"].pop(date_key, None)
        self.store.save(state)
        return self._json(200, self._bundle(self.store.snapshot()))

    # ---------- 报表/导出 ----------
    def api_report(self, query):
        scope = (query.get("scope") or ["day"])[0]
        state = self.store.snapshot()
        if scope == "day":
            day_key = validate_day_key((query.get("date") or [today_str()])[0])
            day = state["days"].get(day_key)
            if not day:
                return self._error(404, "该日期还没有推荐记录")
            decorated = decorate_day(day, state.get("profile") or {}, state["days"])
            html_text = build_report_html(decorated, state.get("profile") or {})
            download = (query.get("download") or ["0"])[0] == "1"
            extra = {}
            if download:
                filename = f"zhixin-report-{day_key}.html"
                extra["Content-Disposition"] = f'attachment; filename="{filename}"'
            return self._send(200, html_text.encode("utf-8"), "text/html; charset=utf-8", extra)
        return self._error(400, "未知的报表范围")

    def api_export_csv(self):
        state = self.store.snapshot()
        body = build_csv(state).encode("utf-8")
        filename = f"zhixin-scores-{today_str()}.csv"
        return self._send(200, body, "text/csv; charset=utf-8", {
            "Content-Disposition": f'attachment; filename="{filename}"'
        })

    def api_export_json(self):
        state = self.store.snapshot()
        body = build_backup(state).encode("utf-8")
        filename = f"zhixin-backup-{today_str()}.json"
        return self._send(200, body, "application/json; charset=utf-8", {
            "Content-Disposition": f'attachment; filename="{filename}"'
        })


def find_port(start: int, host: str = "127.0.0.1") -> int:
    for port in range(start, start + 30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
                return port
            except OSError:
                continue
    raise RuntimeError(f"从 {start} 起没有可用端口")


def existing_instance() -> str | None:
    """如果同一数据目录的知新已经在运行，返回它的 URL。"""
    if not RUNTIME_PATH.exists():
        return None
    try:
        info = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        url = str(info.get("url") or "")
        if not url.startswith(("http://127.0.0.1:", "http://localhost:")):
            return None
        with urllib.request.urlopen(url.rstrip("/") + "/api/health", timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if data.get("app") == APP_NAME:
            return url
    except Exception:
        return None
    return None


def write_runtime(port: int, host: str, url: str):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    info = {
        "app": APP_NAME,
        "version": VERSION,
        "pid": os.getpid(),
        "host": host,
        "port": port,
        "url": url,
        "started_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    RUNTIME_PATH.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")


def remove_runtime():
    try:
        if not RUNTIME_PATH.exists():
            return
        info = json.loads(RUNTIME_PATH.read_text(encoding="utf-8"))
        if int(info.get("pid", 0)) == os.getpid():
            RUNTIME_PATH.unlink(missing_ok=True)
    except Exception:
        pass


def main():
    parser = argparse.ArgumentParser(description=f"{APP_NAME} · 文献日报")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765，被占用时自动顺延）")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    parser.add_argument("--no-browser", action="store_true", help="启动后不自动打开浏览器")
    args = parser.parse_args()

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    running = existing_instance()
    if running:
        print(f"{APP_NAME} 已在运行：{running}")
        log(f"检测到已有实例：{running}")
        if not args.no_browser:
            webbrowser.open(running)
        return

    port = find_port(args.port, args.host)
    store = Store(STATE_PATH)
    Handler.store = store
    server = ThreadingHTTPServer((args.host, port), Handler)
    server.daemon_threads = True
    url = f"http://{args.host}:{port}"
    write_runtime(port, args.host, url)
    log(f"启动 {APP_NAME} {VERSION}：{url}")
    print()
    print(f"  {APP_NAME} · {APP_TAGLINE}")
    print(f"  {url}")
    print(f"  数据文件：{STATE_PATH}")
    print("  直接关闭浏览器不会丢数据；页面右上角电源键或 stop.bat 可退出服务。")
    print()
    if not args.no_browser:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
        log("收到 Ctrl+C，服务停止")
    finally:
        server.server_close()
        remove_runtime()
        log("服务已停止")


if __name__ == "__main__":
    main()
