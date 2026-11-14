#!/usr/bin/env python3
"""Resume Tool Web UI — 本地单用户桌面化界面（Flask，仅供 127.0.0.1 使用）.

页面：
  /                简历库（统计卡片 + 批量导入 + 简历列表 + 导出）
  /resume/<id>     简历详情（抽取文本 + 筛选历史）
  /screen          筛选中心（模板/引擎选择 + 结果排名）
  /templates       模板管理（列表 + JSON 编辑器）
  /settings        综合设置（通用选项 + AI 厂商 + 版本更新检查）

启动：python app.py  →  http://127.0.0.1:5000
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace

from flask import (
    Flask, abort, flash, redirect, render_template, request, send_file,
    url_for,
)

import resume_tool as rt

BASE = Path(__file__).parent
DB = str(BASE / "resume_tool.db")
TPL_DIR = BASE / "config" / "templates"
PROVIDERS_PATH = BASE / "config" / "providers.json"
APP_SETTINGS_PATH = BASE / "config" / "app_settings.json"
UPDATE_MANIFEST_PATH = BASE / "update-manifest.json"
UPLOAD_DIR = BASE / "uploads"

app = Flask(__name__)
app.secret_key = "resume-tool-local-dev"  # 本地原型，无跨用户场景
app.config["TEMPLATES_AUTO_RELOAD"] = True  # UI 迭代期模板改动即时生效，无需重启

# 进入页面时弹出一次；改版本号或日期即再次对全员弹出
CHANGELOG = [
    {
        "version": "ver1.2.0",
        "channel": "Beta",
        "date": "2026-11-14",
        "title": "综合设置与 WorkBuddy Agent 协作筛选",
        "entries": [
            "侧边栏「AI 设置」升级为综合「设置」，集中管理通用选项、AI 厂商与版本更新",
            "设置页新增「检查更新」：读取版本 manifest，比对版本并展示更新说明与安装包下载地址",
            "新增 Agent 协作筛选接口：导出脱敏批次给 WorkBuddy Agent 评估，再写回结果库，无需 API Key",
            "配套 resume-tool-agent 技能可一键驱动完整筛选流程，评估额度记在使用者自己的 WorkBuddy 账号",
        ],
    },
    {
        "version": "ver1.1.1",
        "channel": "Beta",
        "date": "2026-11-12",
        "title": "品牌区换新：新图标 + 引擎说明",
        "entries": [
            "侧边栏左上角换用新图标：纸面 + 陶土色镜片 + 通过对勾，浏览器标签页图标同步更新",
            "品牌文字精简为单行英文 RESUME TOOL，与图标视觉居中；原中文名与副标题移除",
            "侧边栏页脚改为引擎说明：「基于双引擎开发 / 本地关键词 · AI 语义评估」，与筛选中心引擎命名一致",
            "AI 设置页新增数据流向说明：关键词引擎全程离线，AI 引擎会把简历正文发送至所选厂商接口",
        ],
    },
    {
        "version": "ver1.1.0",
        "channel": "Beta",
        "date": "2026-11-12",
        "title": "视觉升级（A 阶段）：暖纸风格主题 + 内嵌中文字体",
        "entries": [
            "全站换用暖纸 + 珊瑚主色视觉语言：米白画布、白色圆角卡片、胶囊按钮、浅色侧边栏",
            "内嵌 Noto Serif SC 子集（6763 汉字 · 400/600 两档 · 约 2.7MB），标题不再回退宋体，离线可用",
            "图表配色统一为暖色系（草绿 / 琥珀 / 砖红 / 珊瑚），去掉荧光蓝紫",
            "修复筛选中心「提交前提示」始终显示「请至少勾选一个模板」的问题",
        ],
    },
    {
        "version": "ver1.0.3",
        "channel": "Beta",
        "date": "2026-11-12",
        "title": "筛选模板改为可搜索多选（部门分组 + 收藏）",
        "entries": [
            "模板选择从卡片墙改为可搜索多选框：输入模板名或技能即时过滤，已选项以 chip 展示、可单个移除",
            "模板支持「部门 + 子职业」两级归属（人事 / 电气 / 业务 / IT），下拉按部门分组展示",
            "支持 ★ 收藏，收藏项置顶显示；常用组合会记入「最近使用」",
            "已选模板旁显示 AI 调用量预估（模板数 × 简历数），避免误选过多模板",
            "支持键盘操作：↑↓ 移动、Enter 选中、Backspace 删除最后一个 chip、Esc 收起",
        ],
    },
    {
        "version": "ver1.0.2",
        "channel": "Beta",
        "date": "2026-11-12",
        "title": "新增更新日志入口与历史公告页",
        "entries": [
            "侧边栏新增「更新日志」入口，有未读版本时显示 NEW 标识，看完即消失",
            "更新日志页以卡片展示全部历史公告，可逐条展开/收起，当前版本默认展开",
            "进入时的公告弹窗底部新增「查看历史更新」，可直接跳到完整记录",
        ],
    },
    {
        "version": "ver1.0.1",
        "channel": "Beta",
        "date": "2026-11-12",
        "title": "Beta 迭代：候选人聚合与 xlsx 报告导出",
        "entries": [
            "同一人的多份简历自动归并为一个候选人（按 邮箱 → 手机 → 姓名 匹配），列表显示最新版本与版本数",
            "点候选人姓名进入版本页，可横向对比新旧版本、单独查看详情，并导出该候选人的 xlsx",
            "新增「导出 xlsx 报告」：姓名/联系方式/版本数/双引擎评分/命中技能/判定说明，带表头冻结、筛选与汇总页",
            "可「导出所选」，导出跟随当前搜索词与模板口径",
        ],
    },
    {
        "version": "ver1.0.0",
        "channel": "Beta",
        "date": "2026-11-12",
        "title": "Beta 首发：本地双引擎简历筛选工具",
        "entries": [
            "批量导入 doc / docx / pdf / txt，按内容哈希去重，自动识别姓名与联系方式",
            "双引擎筛选：本地关键词（离线、秒级）+ 多家 AI 接口（OpenAI 兼容协议），支持分级漏斗降本",
            "筛选模板可视化编辑、批量导入导出（JSON / ZIP）",
            "一次勾选多个模板做综合筛选，结果按模板分组；筛选在后台运行并显示进度",
            "正文全文搜索（中文按字切分），支持按模板查看与多条件叠加",
            "图表视图：状态分布、分数分布、模板通过率、技能命中、导入趋势",
            "回收站（删除可恢复）、主列表与筛选历史分页、原文件预览与多种打开方式",
        ],
    },
]

OPEN_METHODS = {
    "default": "系统默认程序",
    "wps": "WPS Office",
    "word": "Microsoft Word",
    "explorer": "在文件夹中定位",
    "browser": "浏览器内预览",
}

ENGINES = {
    "keyword": {"label": "本地关键词", "desc": "完全离线 · 秒级出结果 · 规则可解释", "need_ai": False},
    "ai": {"label": "AI 语义评估", "desc": "逐份调用大模型 · 理解换表述简历 · 按次计费", "need_ai": True},
    "funnel": {"label": "分级漏斗（推荐）", "desc": "关键词全量初筛 → AI 只复核待核实名单 · 成本可控", "need_ai": False},
}
STATUS_ORDER = {"通过": 0, "待核实": 1, "未通过": 2}


# ---------- 数据辅助 ----------

def db():
    return rt.get_conn(DB)


DEPT_ORDER = ["人事", "电气", "业务", "IT"]


def dept_sort_key(dept: str) -> tuple:
    return (DEPT_ORDER.index(dept) if dept in DEPT_ORDER else len(DEPT_ORDER), dept)


def list_templates() -> list[dict]:
    """扫描模板目录，返回模板元信息（含部门/子职业，供分组与搜索）."""
    items = []
    for p in sorted(TPL_DIR.glob("*.json")):
        try:
            tpl = rt.load_template(p)
            raw = json.loads(p.read_text(encoding="utf-8"))
            items.append({
                "slug": p.stem, "name": tpl.name,
                "department": (raw.get("department") or "未分类").strip(),
                "role": (raw.get("role") or "").strip(),
                "skills": [r.name for r in (*tpl.core_skills, *tpl.nice_to_have)],
                "required": ", ".join(tpl.required_skills) or "无",
                "min_years": tpl.min_years,
                "core_count": len(tpl.core_skills),
                "nice_count": len(tpl.nice_to_have),
            })
        except SystemExit:
            items.append({"slug": p.stem, "name": p.stem + "（配置有误）",
                          "department": "未分类", "role": "", "skills": [],
                          "required": "-", "min_years": None, "core_count": 0, "nice_count": 0})
    items.sort(key=lambda t: (dept_sort_key(t["department"]), t["name"]))
    return items


def get_favorites() -> list[str]:
    favs = load_app_settings().get("favorites", [])
    return [s for s in favs if isinstance(s, str) and (TPL_DIR / f"{s}.json").is_file()]


def toggle_favorite(slug: str) -> bool:
    cfg = load_app_settings()
    favs = [s for s in cfg.get("favorites", []) if isinstance(s, str)]
    if slug in favs:
        favs.remove(slug)
        on = False
    else:
        favs.append(slug)
        on = True
    cfg["favorites"] = favs
    save_app_settings(cfg)
    return on


def latest_map(conn, template: str | None = None) -> dict[int, dict[str, dict]]:
    """每份简历每个引擎的最新一次筛选结果（可选按模板过滤）."""
    sql = "SELECT * FROM screenings"
    params: tuple = ()
    if template:
        sql += " WHERE template = ?"
        params = (template,)
    sql += " ORDER BY created_at, id"
    result: dict[int, dict[str, dict]] = {}
    for row in conn.execute(sql, params).fetchall():
        result.setdefault(row["resume_id"], {})[row["engine"]] = dict(row)
    return result


def provider_status() -> tuple[dict, dict | None]:
    providers = rt.load_providers(PROVIDERS_PATH)
    return providers, rt.get_active_provider(providers)


def load_app_settings() -> dict:
    defaults = {
        "preview_method": "default",
        "cloud_url": "https://docs.qq.com/desktop",
        "update_url": "",  # 留空读取项目根 update-manifest.json；分发时填固定 HTTPS 地址
        "auto_check_updates": True,
    }
    if APP_SETTINGS_PATH.is_file():
        try:
            defaults.update(json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return defaults


def save_app_settings(data: dict) -> None:
    APP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    APP_SETTINGS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------- 版本更新（manifest 驱动） ----------

def current_version() -> str:
    return CHANGELOG[0]["version"] if CHANGELOG else "ver0.0.0"


def version_tuple(version: str) -> tuple[int, ...]:
    """ver1.2.0 / 1.2 / v2 统一转数字元组，用于递增比较."""
    nums = [int(n) for n in re.findall(r"\d+", version or "")[:4]]
    return tuple((nums + [0, 0, 0, 0])[:4])


def _read_update_manifest(source: str) -> tuple[dict, str]:
    """读取更新 manifest；支持 HTTPS/HTTP、本地路径，空值走项目根默认文件."""
    source = (source or "").strip()
    if not source:
        path = UPDATE_MANIFEST_PATH
        if not path.is_file():
            raise ValueError("未配置更新地址，且项目根目录没有 update-manifest.json")
        return json.loads(path.read_text(encoding="utf-8")), "本地更新清单"
    if source.startswith(("https://", "http://")):
        req = urllib.request.Request(
            source, headers={"User-Agent": f"ResumeTool/{current_version()}"}
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read(1_000_000).decode("utf-8")  # 限制 1MB，避免异常响应占内存
        return json.loads(raw), source
    path = Path(source[7:] if source.startswith("file://") else source)
    if not path.is_absolute():
        path = BASE / path
    return json.loads(path.read_text(encoding="utf-8")), str(path)


def check_for_update() -> dict:
    """拉取并比较版本；所有异常转为设置页可展示的结果，不让检查失败影响其他设置."""
    current = current_version()
    base = {"current_version": current, "checked_at": datetime.now().isoformat(timespec="seconds")}
    try:
        manifest, source = _read_update_manifest(load_app_settings().get("update_url", ""))
        latest = str(manifest.get("version", "")).strip()
        if not latest or not re.search(r"\d", latest):
            raise ValueError("更新清单缺少有效的 version 字段")
        notes = manifest.get("notes", [])
        return {
            **base,
            "ok": True,
            "latest_version": latest,
            "update_available": version_tuple(latest) > version_tuple(current),
            "title": str(manifest.get("title", "")),
            "date": str(manifest.get("date", "")),
            "notes": [str(n) for n in notes][:12] if isinstance(notes, list) else [],
            "download_url": str(manifest.get("download_url", "")),
            "source": source,
        }
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return {**base, "ok": False, "error": f"检查失败：{exc}"}


@app.context_processor
def inject_globals() -> dict:
    """模板全局变量：公告、打开方式、当前日期（供「今日不再提示」使用）."""
    latest = CHANGELOG[0] if CHANGELOG else None
    return {
        "announcement": latest,
        "announce_key": f"rt_announce_{latest['version']}_{latest['date']}" if latest else "",
        "today": date.today().isoformat(),
        "open_methods": OPEN_METHODS,
        "app_settings": load_app_settings(),
    }


def parse_slugs(raw: str) -> list[str]:
    """把 'a,b,c' 解析成存在的模板 slug 列表（去重保序）."""
    out: list[str] = []
    for s in re.split(r"[,\s]+", raw or ""):
        s = s.strip()
        if s and s not in out and (TPL_DIR / f"{s}.json").is_file():
            out.append(s)
    return out


def tpl_name(slug: str) -> str:
    return rt.load_template(TPL_DIR / f"{slug}.json").name


def unscreened_ids(conn, slug: str) -> list[int]:
    """对指定模板尚无任何筛选记录的在库简历 ID（不含回收站）."""
    name = tpl_name(slug)
    rows = conn.execute(
        "SELECT id FROM resumes WHERE deleted_at IS NULL AND id NOT IN"
        " (SELECT resume_id FROM screenings WHERE template = ?) ORDER BY id", (name,)
    ).fetchall()
    return [r["id"] for r in rows]


# ---------- 后台筛选任务（进度可轮询） ----------

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
JOB_KEEP = 30


def job_new(engine: str, slugs: list[str], scope: str) -> str:
    jid = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        if len(JOBS) >= JOB_KEEP:  # 只保留最近任务，避免内存无限增长
            for old in sorted(JOBS, key=lambda k: JOBS[k]["started"])[: len(JOBS) - JOB_KEEP + 1]:
                JOBS.pop(old, None)
        JOBS[jid] = {
            "id": jid, "state": "queued", "engine": engine, "slugs": slugs, "scope": scope,
            "total": 0, "done": 0, "current": "", "message": "", "errors": [],
            "started": datetime.now().isoformat(timespec="seconds"), "flashed": False,
        }
    return jid


def job_get(jid: str | None) -> dict | None:
    if not jid:
        return None
    with JOBS_LOCK:
        return JOBS.get(jid)


def job_update(jid: str, **kw) -> None:
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid].update(kw)


def job_inc(jid: str, field: str, n: int = 1) -> None:
    with JOBS_LOCK:
        if jid in JOBS:
            JOBS[jid][field] = JOBS[jid].get(field, 0) + n


def run_screening_job(jid: str, engine: str, slugs: list[str], ids: list[int] | None,
                      scope: str = "all") -> None:
    """在后台线程执行筛选：逐份更新进度，任何异常都记录到 job.errors 而不是中断。"""
    try:
        conn = db()
        _, provider = provider_status()
        if engine == "ai" and provider is None:
            job_update(jid, state="error",
                       message="AI 未配置：请先在「AI 设置」中填写 API Key，或改用关键词引擎")
            return

        # 先规划：每个模板要处理的简历集合，便于算出总进度
        plan = []
        for slug in slugs:
            tpl = rt.load_template(TPL_DIR / f"{slug}.json")
            target = ids if scope == "selected" else (
                unscreened_ids(conn, slug) if scope == "unscreened" else None)
            plan.append((tpl, rt._fetch_resumes(conn, target)))

        total = sum(len(rows) for _, rows in plan)
        job_update(jid, state="running", total=total)

        messages: list[str] = []
        for tpl, rows in plan:
            if not rows:
                messages.append(f"{tpl.name}：无可筛选简历")
                continue

            if engine in ("keyword", "funnel"):
                for row in rows:
                    job_update(jid, current=f"{tpl.name} · 关键词 #{row['id']}")
                    result = rt.screen_keyword(row["text"], tpl)
                    rt._save_screening(conn, row["id"], "keyword", tpl.name, result)
                    job_inc(jid, "done")

            if engine == "funnel":
                if provider is None:
                    messages.append(f"{tpl.name}：关键词初筛完成，AI 未配置，待核实名单请人工复核")
                    continue
                review = [r for r in rows
                          if _latest_status(conn, r["id"], tpl.name) == "待核实"]
                job_inc(jid, "total", len(review))
                for row in review:
                    job_update(jid, current=f"{tpl.name} · AI 复核 #{row['id']}")
                    try:
                        result = rt.screen_ai(provider, tpl, row["text"])
                    except RuntimeError as exc:
                        with JOBS_LOCK:
                            JOBS[jid]["errors"].append(f"{tpl.name} #{row['id']} AI 复核失败: {exc}")
                        job_inc(jid, "done")
                        continue
                    rt._save_screening(conn, row["id"], "ai", tpl.name, result)
                    job_inc(jid, "done")
                messages.append(f"{tpl.name}：初筛 {len(rows)} 份，AI 复核 {len(review)} 份")
                continue

            if engine == "ai":
                for row in rows:
                    job_update(jid, current=f"{tpl.name} · AI 评估 #{row['id']}")
                    try:
                        result = rt.screen_ai(provider, tpl, row["text"])
                    except RuntimeError as exc:
                        with JOBS_LOCK:
                            JOBS[jid]["errors"].append(f"{tpl.name} #{row['id']} AI 评估失败: {exc}")
                        job_inc(jid, "done")
                        continue
                    rt._save_screening(conn, row["id"], "ai", tpl.name, result)
                    job_inc(jid, "done")
                messages.append(f"{tpl.name}：AI 评估 {len(rows)} 份")
                continue

            messages.append(f"{tpl.name}：关键词筛选 {len(rows)} 份")

        head = f"已按 {len(slugs)} 个模板执行筛选。" if len(slugs) > 1 else ""
        job_update(jid, state="done", current="", message=head + "；".join(messages))
    except Exception as exc:  # 线程内不抛给 Flask，转成任务错误信息
        job_update(jid, state="error", current="", message=f"筛选任务异常：{exc}")


def start_job(engine: str, slugs: list[str], ids: list[int] | None, scope: str) -> str:
    jid = job_new(engine, slugs, scope)
    threading.Thread(target=run_screening_job, args=(jid, engine, slugs, ids, scope),
                     daemon=True).start()
    return jid


def _latest_status(conn, resume_id: int, tpl_name_: str) -> str | None:
    row = conn.execute(
        "SELECT status FROM screenings WHERE resume_id=? AND template=? AND engine='keyword'"
        " ORDER BY created_at DESC, id DESC LIMIT 1", (resume_id, tpl_name_)
    ).fetchone()
    return row["status"] if row else None


# ---------- 路由：简历库 ----------

@app.get("/")
def library():
    conn = db()
    templates = list_templates()
    tpl_filter = request.args.get("template", "all")
    tpl_label = None
    if tpl_filter != "all" and (TPL_DIR / f"{tpl_filter}.json").is_file():
        tpl_label = tpl_name(tpl_filter)
    else:
        tpl_filter = "all"
    q = request.args.get("q", "").strip()

    # 先取「范围内全部简历 ID」（搜索命中按相关度排序，否则按导入倒序）
    if q:
        ids = rt.search_ids(conn, q)
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM resumes WHERE deleted_at IS NULL ORDER BY id DESC").fetchall()]

    per_page = 15
    total = len(ids)
    pages = max(1, (total + per_page - 1) // per_page)
    page = request.args.get("page", 1, type=int) or 1
    page = min(max(1, page), pages)
    page_ids = ids[(page - 1) * per_page: page * per_page]

    # 统计基于全量匹配结果（不只看当前页），模板口径可选
    latest = latest_map(conn, template=tpl_label)
    stats = {"total": total, "通过": 0, "待核实": 0, "未通过": 0, "未筛选": 0}
    for rid in ids:
        best = (latest.get(rid, {}).get("ai") or latest.get(rid, {}).get("keyword"))
        stats[best["status"] if best else "未筛选"] += 1

    # 只回取当前页的行（含 text 用于生成命中片段，不传给模板）
    resumes = []
    if page_ids:
        marks = ",".join("?" * len(page_ids))
        rows = {r["id"]: r for r in conn.execute(
            f"SELECT id, filename, imported_at, LENGTH(text) AS chars, text FROM resumes"
            f" WHERE id IN ({marks})", page_ids).fetchall()}
        for rid in page_ids:
            r = rows.get(rid)
            if r is None:
                continue
            engines = latest.get(rid, {})
            best = engines.get("ai") or engines.get("keyword")
            item = dict(r) | {"keyword": engines.get("keyword"), "ai": engines.get("ai"),
                              "best_template": best["template"] if best else None,
                              "snippet": rt.make_snippet(r["text"], q) if q else ""}
            item.pop("text", None)  # 列表页不传全文
            resumes.append(item)

    trash_count = conn.execute(
        "SELECT COUNT(*) AS n FROM resumes WHERE deleted_at IS NOT NULL").fetchone()["n"]
    return render_template("library.html", resumes=resumes, stats=stats,
                           templates=templates, tpl_filter=tpl_filter,
                           tpl_label=tpl_label, trash_count=trash_count,
                           q=q, matched=len(ids),
                           pager_info={"page": page, "pages": pages, "total": total,
                                  "per_page": per_page,
                                  "window": _page_window(page, pages)},
                           active="library")


@app.post("/import-dir")
def import_dir():
    src = Path(request.form.get("dir", "").strip())
    if not src.is_dir():
        flash(f"目录不存在: {src}", "error")
        return redirect(url_for("library"))
    conn = db()
    counts = {"added": 0, "restored": 0, "dup": 0, "empty": 0}
    for p in sorted(src.iterdir()):
        if p.is_file() and p.suffix.lower() in rt.SUPPORTED_SUFFIXES and not p.name.startswith("~$"):
            counts[rt.import_one(conn, p)] += 1
    flash(f"导入完成：新增 {counts['added']}，从回收站恢复 {counts['restored']}，重复 {counts['dup']}，无法解析 {counts['empty']}")
    return redirect(url_for("library"))


@app.post("/import-files")
def import_files():
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("未选择文件", "error")
        return redirect(url_for("library"))
    conn = db()
    counts = {"added": 0, "restored": 0, "dup": 0, "empty": 0}
    batch = UPLOAD_DIR / str(int(time.time() * 1000))
    batch.mkdir(parents=True, exist_ok=True)
    for f in files:
        # 保留中文文件名，仅去路径分隔符（secure_filename 会把中文剥光）
        safe = Path(f.filename).name.replace("/", "_").replace("\\", "_")
        dest = batch / safe
        f.save(dest)
        counts[rt.import_one(conn, dest)] += 1
    flash(f"上传导入完成：新增 {counts['added']}，从回收站恢复 {counts['restored']}，重复 {counts['dup']}，无法解析 {counts['empty']}")
    return redirect(url_for("library"))


@app.get("/resume/<int:rid>")
def resume_detail(rid: int):
    conn = db()
    row = conn.execute("SELECT * FROM resumes WHERE id=?", (rid,)).fetchone()
    if row is None:
        abort(404)

    # 筛选历史分页：每页 10 条，底部提供翻页与页码跳转
    per_page = 10
    total = conn.execute("SELECT COUNT(*) AS n FROM screenings WHERE resume_id=?",
                         (rid,)).fetchone()["n"]
    pages = max(1, (total + per_page - 1) // per_page)
    page = request.args.get("page", 1, type=int) or 1
    page = min(max(1, page), pages)
    history = conn.execute(
        "SELECT * FROM screenings WHERE resume_id=? ORDER BY created_at DESC, id DESC"
        " LIMIT ? OFFSET ?", (rid, per_page, (page - 1) * per_page)
    ).fetchall()
    history = [dict(h) | {"detail": json.loads(h["detail"])} for h in history]

    src = Path(row["path"])
    ext = src.suffix.lower()
    file_info = {
        "exists": src.is_file(),
        "path": str(src),
        "ext": ext,
        # 浏览器能内嵌渲染的类型：PDF 原生，纯文本直接显示
        "inline": ext == ".pdf" or ext in {".txt", ".md", ".markdown"},
        "size_kb": round(src.stat().st_size / 1024, 1) if src.is_file() else 0,
    }
    return render_template("resume_detail.html", r=row, history=history,
                           file_info=file_info, deleted=bool(row["deleted_at"]),
                           pager_info={"page": page, "pages": pages, "total": total,
                                  "per_page": per_page,
                                  "window": _page_window(page, pages)},
                           active="library")


def _page_window(cur: int, pages: int, span: int = 2) -> list:
    """分页条要显示的页码：始终含首尾页，当前页附近 span 页，其余折叠为省略号."""
    out: list = []
    for p in range(1, pages + 1):
        if p == 1 or p == pages or abs(p - cur) <= span:
            out.append(p)
        elif out and out[-1] != "…":
            out.append("…")
    return out


@app.get("/resume/<int:rid>/raw")
def resume_raw(rid: int):
    """原文件字节流：浏览器预览用 inline，?download=1 时强制下载."""
    conn = db()
    row = conn.execute("SELECT filename, path FROM resumes WHERE id=?", (rid,)).fetchone()
    if row is None:
        abort(404)
    src = Path(row["path"])
    if not src.is_file():
        abort(404)
    return send_file(src, as_attachment=request.args.get("download") == "1",
                     download_name=row["filename"])


def _find_exe(keywords: list[str], roots: list[str]) -> str | None:
    """在常见安装目录里按名字找 exe（WPS / Office 版本号目录不固定）."""
    for kw in keywords:
        found = shutil.which(kw)
        if found:
            return found
    for root in roots:
        base = Path(root)
        if not base.is_dir():
            continue
        for exe_name in keywords:
            try:
                hits = sorted(base.glob(f"**/{exe_name}.exe"))
            except OSError:
                continue
            if hits:
                return str(hits[0])
    return None


def _wps_exe() -> str | None:
    roots = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
             os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)"),
             os.path.join(os.environ.get("LOCALAPPDATA", ""), "Kingsoft")]
    return _find_exe(["wps"], [os.path.join(r, "WPS Office") for r in roots] +
                     [os.path.join(os.environ.get("LOCALAPPDATA", ""), "Kingsoft", "WPS Office")])


def _word_exe() -> str | None:
    roots = [os.environ.get("PROGRAMFILES", r"C:\Program Files"),
             os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")]
    return _find_exe(["WINWORD"], [os.path.join(r, "Microsoft Office") for r in roots])


@app.post("/resume/<int:rid>/open")
def resume_open(rid: int):
    """用本机程序打开原文件（仅回环地址可访问，属本地桌面行为）."""
    conn = db()
    row = conn.execute("SELECT filename, path FROM resumes WHERE id=?", (rid,)).fetchone()
    if row is None:
        abort(404)
    src = Path(row["path"])
    if not src.is_file():
        flash(f"原文件不存在（可能已被移动或删除）：{src}", "error")
        return redirect(url_for("resume_detail", rid=rid))

    method = request.form.get("method", "default")
    try:
        if method == "explorer":
            subprocess.Popen(["explorer", f"/select,{src}"])
            flash("已在资源管理器中定位该文件")
        elif method == "wps":
            exe = _wps_exe()
            if not exe:
                flash("未找到 WPS Office，已改用系统默认程序打开", "error")
                os.startfile(str(src))  # type: ignore[attr-defined]
            else:
                subprocess.Popen([exe, str(src)])
                flash(f"已用 WPS 打开：{row['filename']}")
        elif method == "word":
            exe = _word_exe()
            if not exe:
                flash("未找到 Microsoft Word，已改用系统默认程序打开", "error")
                os.startfile(str(src))  # type: ignore[attr-defined]
            else:
                subprocess.Popen([exe, str(src)])
                flash(f"已用 Word 打开：{row['filename']}")
        elif method == "browser":
            return redirect(url_for("resume_raw", rid=rid))
        else:
            os.startfile(str(src))  # type: ignore[attr-defined]
            flash(f"已用系统默认程序打开：{row['filename']}")
    except (OSError, AttributeError) as exc:
        flash(f"打开失败：{exc}（可改用「在文件夹中定位」）", "error")
    return redirect(url_for("resume_detail", rid=rid))


@app.post("/resume/<int:rid>/delete")
def resume_delete(rid: int):
    """软删除：移入回收站，可在 /trash 恢复."""
    conn = db()
    conn.execute("UPDATE resumes SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                 (rt.now(), rid))
    conn.commit()
    flash(f"已将简历 #{rid} 移入回收站（可在回收站恢复）")
    return redirect(url_for("library"))


@app.post("/resume/batch-delete")
def resume_batch_delete():
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    if not ids:
        flash("未选择任何简历", "error")
        return redirect(url_for("library"))
    conn = db()
    marks = ",".join("?" * len(ids))
    conn.execute(f"UPDATE resumes SET deleted_at=? WHERE id IN ({marks}) AND deleted_at IS NULL",
                 [rt.now(), *ids])
    conn.commit()
    flash(f"已将 {len(ids)} 份简历移入回收站（可在回收站恢复）")
    return redirect(url_for("library"))


# ---------- 路由：回收站 ----------

@app.get("/trash")
def trash():
    conn = db()
    rows = conn.execute(
        "SELECT id, filename, imported_at, deleted_at, LENGTH(text) AS chars"
        " FROM resumes WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
    ).fetchall()
    return render_template("trash.html", rows=rows, active="trash")


@app.post("/trash/<int:rid>/restore")
def trash_restore(rid: int):
    conn = db()
    conn.execute("UPDATE resumes SET deleted_at=NULL WHERE id=?", (rid,))
    conn.commit()
    flash(f"已恢复简历 #{rid}")
    return redirect(url_for("trash"))


def _drop_orphan_persons(conn) -> None:
    """物理删除简历后，清理名下已无任何简历的候选人."""
    conn.execute("DELETE FROM persons WHERE NOT EXISTS"
                 " (SELECT 1 FROM resumes WHERE resumes.person_id = persons.id)")


def _fts_forget(conn, ids: list[int]) -> None:
    """物理删除时同步清理搜索索引（FTS 不可用时忽略）."""
    try:
        conn.executemany("DELETE FROM resume_fts WHERE rowid=?", [(i,) for i in ids])
    except Exception:
        pass


@app.post("/trash/<int:rid>/purge")
def trash_purge(rid: int):
    conn = db()
    row = conn.execute("SELECT filename FROM resumes WHERE id=? AND deleted_at IS NOT NULL",
                       (rid,)).fetchone()
    if row is None:
        flash("该记录不在回收站中", "error")
        return redirect(url_for("trash"))
    conn.execute("DELETE FROM resumes WHERE id=?", (rid,))  # 筛选记录随外键级联删除
    _fts_forget(conn, [rid])
    _drop_orphan_persons(conn)
    conn.commit()
    flash(f"已彻底删除「{row['filename']}」及其筛选记录（磁盘文件保留）")
    return redirect(url_for("trash"))


@app.post("/trash/empty")
def trash_empty():
    conn = db()
    rows = conn.execute("SELECT id FROM resumes WHERE deleted_at IS NOT NULL").fetchall()
    ids = [r["id"] for r in rows]
    conn.execute("DELETE FROM resumes WHERE deleted_at IS NOT NULL")
    _fts_forget(conn, ids)
    _drop_orphan_persons(conn)
    conn.commit()
    flash(f"回收站已清空，彻底删除 {len(ids)} 份简历（磁盘文件保留）")
    return redirect(url_for("trash"))


@app.get("/export")
def export_csv():
    out = BASE / "report.csv"
    rt.cmd_export(SimpleNamespace(db=DB, out=str(out)))
    return send_file(out, as_attachment=True, download_name="resume_report.csv")


# ---------- 路由：候选人（同人多版本聚合） ----------

def _person_groups(conn, q: str = "") -> list[dict]:
    """把在库简历按候选人聚合，返回按最新版本排序的分组列表."""
    rows = conn.execute(
        "SELECT r.id, r.person_id, r.filename, r.imported_at, LENGTH(r.text) AS chars,"
        " p.name, p.phone, p.email"
        " FROM resumes r LEFT JOIN persons p ON p.id = r.person_id"
        " WHERE r.deleted_at IS NULL"
        " ORDER BY r.imported_at DESC, r.id DESC"
    ).fetchall()

    matched_pids: set[int] | None = None
    order: dict[int, int] = {}
    if q:
        matched_pids = set()
        for idx, rid in enumerate(rt.search_ids(conn, q)):
            row = conn.execute("SELECT person_id FROM resumes WHERE id=?", (rid,)).fetchone()
            pid = (row["person_id"] if row else None) or 0
            matched_pids.add(pid)
            order.setdefault(pid, idx)

    groups: dict[int, dict] = {}
    for r in rows:
        pid = r["person_id"] or 0
        if matched_pids is not None and pid not in matched_pids:
            continue
        g = groups.setdefault(pid, {
            "person_id": pid, "name": r["name"] or "(未识别)",
            "phone": r["phone"], "email": r["email"], "versions": [],
        })
        g["versions"].append(dict(r))

    latest_all = latest_map(conn)
    items = []
    for pid, g in groups.items():
        latest = g["versions"][0]
        engines = latest_all.get(latest["id"], {})
        best = engines.get("ai") or engines.get("keyword")
        items.append({
            **g, "latest": latest, "count": len(g["versions"]),
            "keyword": engines.get("keyword"), "ai": engines.get("ai"),
            "status": best["status"] if best else "未筛选",
            "score": best["score"] if best else None,
            "template": best["template"] if best else None,
            "multi": len(g["versions"]) > 1,
        })
    if q:
        items.sort(key=lambda it: order.get(it["person_id"], 1 << 30))
    return items


@app.get("/persons")
def persons():
    conn = db()
    q = request.args.get("q", "").strip()
    items = _person_groups(conn, q)

    per_page = 15
    total = len(items)
    pages = max(1, (total + per_page - 1) // per_page)
    page = request.args.get("page", 1, type=int) or 1
    page = min(max(1, page), pages)
    page_items = items[(page - 1) * per_page: page * per_page]

    stats = {"persons": total, "multi": sum(1 for i in items if i["multi"]),
             "通过": 0, "待核实": 0, "未通过": 0, "未筛选": 0}
    for it in items:
        stats[it["status"]] += 1

    return render_template("persons.html", items=page_items, stats=stats, q=q,
                           pager_info={"page": page, "pages": pages, "total": total,
                                       "per_page": per_page,
                                       "window": _page_window(page, pages)},
                           active="persons")


@app.get("/person/<int:pid>")
def person_detail(pid: int):
    conn = db()
    p = conn.execute("SELECT * FROM persons WHERE id=?", (pid,)).fetchone()
    if p is None:
        abort(404)
    rows = conn.execute(
        "SELECT id, filename, imported_at, deleted_at, LENGTH(text) AS chars"
        " FROM resumes WHERE person_id=? ORDER BY imported_at DESC, id DESC", (pid,)
    ).fetchall()
    latest_all = latest_map(conn)
    versions = []
    for r in rows:
        engines = latest_all.get(r["id"], {})
        best = engines.get("ai") or engines.get("keyword")
        versions.append(dict(r) | {"keyword": engines.get("keyword"), "ai": engines.get("ai"),
                                   "best": best})
    return render_template("person.html", p=p, versions=versions,
                           alive=sum(1 for v in versions if not v["deleted_at"]),
                           active="persons")


@app.get("/export-xlsx")
def export_xlsx():
    """批量导出候选人报告（xlsx）：支持搜索/模板过滤与勾选导出."""
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
        from openpyxl.utils import get_column_letter
    except ImportError:
        flash("缺少 openpyxl，无法导出 xlsx：请执行 pip install openpyxl", "error")
        return redirect(url_for("library"))

    conn = db()
    q = request.args.get("q", "").strip()
    tpl_arg = request.args.get("template", "all")
    tpl_label = tpl_name(tpl_arg) if (tpl_arg != "all" and (TPL_DIR / f"{tpl_arg}.json").is_file()) else None
    ids_raw = ",".join(request.args.getlist("ids")) + "," + request.args.get("ids", "")
    picked = [int(x) for x in re.split(r"[,\s]+", ids_raw) if x.isdigit()]

    if picked:
        ids = picked
    elif q:
        ids = rt.search_ids(conn, q)
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM resumes WHERE deleted_at IS NULL ORDER BY id DESC").fetchall()]
    if not ids:
        flash("没有可导出的简历", "error")
        return redirect(url_for("library"))

    marks = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT r.*, p.name AS person_name, p.phone, p.email,"
        f" (SELECT COUNT(*) FROM resumes r2 WHERE r2.person_id = r.person_id"
        f"  AND r2.deleted_at IS NULL) AS versions"
        f" FROM resumes r LEFT JOIN persons p ON p.id = r.person_id"
        f" WHERE r.id IN ({marks})", ids).fetchall()}
    latest = latest_map(conn, template=tpl_label)

    wb = Workbook()
    ws = wb.active
    ws.title = "候选人报告"
    headers = ["ID", "姓名", "手机", "邮箱", "简历文件", "版本数", "结果模板",
               "关键词状态", "关键词分", "AI状态", "AI分", "命中技能",
               "判定说明", "导入时间"]
    widths = [6, 12, 14, 24, 34, 8, 18, 11, 10, 9, 8, 46, 52, 20]
    ws.append(headers)
    head_fill = PatternFill("solid", fgColor="4F46E5")
    for c, w in enumerate(widths, start=1):
        cell = ws.cell(row=1, column=c)
        cell.font = Font(bold=True, color="FFFFFF")
        cell.fill = head_fill
        cell.alignment = Alignment(horizontal="center", vertical="center")
        ws.column_dimensions[get_column_letter(c)].width = w
    ws.freeze_panes = "A2"

    fills = {"通过": PatternFill("solid", fgColor="DCFCE7"),
             "待核实": PatternFill("solid", fgColor="FEF3C7"),
             "未通过": PatternFill("solid", fgColor="FEE2E2")}
    counts = {"通过": 0, "待核实": 0, "未通过": 0, "未筛选": 0}
    for idx, rid in enumerate(ids, start=1):
        r = rows.get(rid)
        if r is None:
            continue
        engines = latest.get(rid, {})
        kw, ai = engines.get("keyword"), engines.get("ai")
        best = ai or kw
        status = best["status"] if best else "未筛选"
        counts[status] = counts.get(status, 0) + 1
        evidence = {}
        if kw:
            evidence = json.loads(kw["detail"]).get("evidence", {})
        skills = "、".join(f"{k}×{v}" for k, v in
                           sorted(evidence.items(), key=lambda kv: -kv[1])) or ""
        ws.append([
            rid, r["person_name"] or "", r["phone"] or "", r["email"] or "",
            r["filename"], r["versions"] or 1, best["template"] if best else "",
            kw["status"] if kw else "", kw["score"] if kw else "",
            ai["status"] if ai else "", ai["score"] if ai else "",
            skills,
            json.loads(best["detail"]).get("reason", "") if best else "",
            (r["imported_at"] or "").replace("T", " "),
        ])
        for col in (8, 10):
            cell = ws.cell(row=ws.max_row, column=col)
            if cell.value in fills:
                cell.fill = fills[cell.value]
        ws.cell(row=ws.max_row, column=12).alignment = Alignment(wrap_text=True, vertical="top")
        ws.cell(row=ws.max_row, column=13).alignment = Alignment(wrap_text=True, vertical="top")
    ws.auto_filter.ref = f"A1:N{ws.max_row}"

    summary = wb.create_sheet("汇总")
    summary.append(["项目", "值"])
    for c in range(1, 3):
        summary.cell(row=1, column=c).font = Font(bold=True, color="FFFFFF")
        summary.cell(row=1, column=c).fill = PatternFill("solid", fgColor="0F172A")
    meta = [
        ("生成时间", datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("筛选模板", tpl_label or "全部模板（取最新）"),
        ("搜索关键词", q or "—"),
        ("导出条数", len([i for i in ids if i in rows])),
        ("通过", counts.get("通过", 0)),
        ("待核实", counts.get("待核实", 0)),
        ("未通过", counts.get("未通过", 0)),
        ("未筛选", counts.get("未筛选", 0)),
    ]
    for k, v in meta:
        summary.append([k, v])
    summary.column_dimensions["A"].width = 16
    summary.column_dimensions["B"].width = 34

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(
        buf, as_attachment=True, download_name=f"候选人报告-{stamp}.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


# ---------- 路由：筛选中心 ----------

@app.get("/screen")
def screen():
    slugs = parse_slugs(request.args.get("templates", ""))
    templates = list_templates()
    conn = db()
    results_by_tpl = []
    for slug in slugs:
        name = tpl_name(slug)
        latest = latest_map(conn, template=name)
        filenames = {r["id"]: r["filename"] for r in conn.execute(
            "SELECT id, filename FROM resumes WHERE deleted_at IS NULL").fetchall()}
        items = []
        for rid, engines in latest.items():
            if rid in filenames:
                item = {"id": rid, "filename": filenames[rid], **engines}
                for eng in ("keyword", "ai"):
                    if eng in item:
                        item[eng]["reason"] = json.loads(item[eng]["detail"]).get("reason", "")
                items.append(item)
        items.sort(key=lambda x: (
            STATUS_ORDER.get((x.get("ai") or x.get("keyword") or {}).get("status", "未通过"), 3),
            -(x.get("keyword") or x.get("ai") or {}).get("score") or 0,
        ))
        counts = {"通过": 0, "待核实": 0, "未通过": 0}
        for it in items:
            best = it.get("ai") or it.get("keyword") or {}
            if best.get("status") in counts:
                counts[best["status"]] += 1
        results_by_tpl.append({"slug": slug, "name": name, "rows": items, "counts": counts})

    # 简历选择器数据：附带当前最佳状态，供前端按状态过滤
    resumes = []
    latest_all = latest_map(conn)
    for r in conn.execute(
        "SELECT id, filename, imported_at FROM resumes WHERE deleted_at IS NULL ORDER BY id DESC"
    ).fetchall():
        best = latest_all.get(r["id"], {})
        best_res = best.get("ai") or best.get("keyword")
        resumes.append({
            "id": r["id"], "filename": r["filename"],
            "status": best_res["status"] if best_res else "未筛选",
            "score": best_res["score"] if best_res else None,
        })

    # 后台任务：运行中显示进度，结束后保留结果摘要
    job = job_get(request.args.get("job"))
    job_view = None
    if job:
        total = job["total"] or 0
        pct = round(job["done"] / total * 100, 1) if total else (100 if job["state"] == "done" else 0)
        job_view = {"id": job["id"], "state": job["state"], "engine": job.get("engine"),
                    "done": job["done"], "total": total,
                    "pct": min(pct, 100), "current": job.get("current", ""),
                    "message": job.get("message", ""), "errors": job.get("errors", [])}

    _, provider = provider_status()
    tpl_meta = [{"slug": t["slug"], "name": t["name"], "department": t["department"],
                 "role": t["role"], "required": t["required"], "skills": t["skills"]}
                for t in templates]
    return render_template("screen.html", templates=templates, engines=ENGINES,
                           results_by_tpl=results_by_tpl, slugs=slugs, resumes=resumes,
                           favorites=get_favorites(), tpl_meta=tpl_meta,
                           resume_total=len(resumes),
                           ai_ready=provider is not None, job=job_view, active="screen")


@app.post("/screen/run")
def screen_run():
    slugs = [s for s in request.form.getlist("templates") if s]
    engine = request.form.get("engine", "keyword")
    scope = request.form.get("scope", "all")
    ids_raw = request.form.get("ids", "").strip()
    ids = [int(x) for x in re.split(r"[,\s]+", ids_raw) if x.isdigit()] if ids_raw else None
    if not slugs:
        flash("请至少勾选一个筛选模板", "error")
        return redirect(url_for("screen"))
    if scope == "selected" and not ids:
        flash("已选择「指定简历」但未勾选任何简历，请重新选择范围", "error")
        return redirect(url_for("screen", templates=",".join(slugs)))

    if engine in ("ai", "funnel") and len(slugs) > 1:
        flash(f"注意：AI 引擎将按「模板 × 简历」组合逐份调用，共 {len(slugs)} 个模板，请留意 API 用量")
    jid = start_job(engine, slugs, ids, scope)  # 立即返回，进度在页面轮询
    return redirect(url_for("screen", templates=",".join(slugs), job=jid))


@app.get("/screen/job/<jid>")
def screen_job(jid: str):
    job = job_get(jid)
    if not job:
        return {"state": "missing", "done": 0, "total": 0, "pct": 0,
                "current": "", "message": "任务不存在或已过期"}, 404
    total = job["total"] or 0
    pct = round(job["done"] / total * 100, 1) if total else (100 if job["state"] == "done" else 0)
    return {
        "id": jid, "state": job["state"], "done": job["done"], "total": total,
        "pct": min(pct, 100), "current": job.get("current", ""),
        "message": job.get("message", ""), "errors": job.get("errors", []),
    }


# ---------- 路由：模板管理 ----------

@app.get("/templates")
def templates_page():
    return render_template("templates.html", templates=list_templates(),
                           favorites=get_favorites(), active="templates")


@app.get("/templates/<slug>")
def template_edit(slug: str):
    path = TPL_DIR / f"{slug}.json"
    if not path.is_file():
        abort(404)
    content = path.read_text(encoding="utf-8")
    try:
        raw = json.loads(content)
    except json.JSONDecodeError:
        raw = None
    return render_template("template_edit.html", slug=slug, content=content,
                           raw=raw, active="templates")


@app.post("/templates/<slug>")
def template_save(slug: str):
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        flash("模板文件名只允许小写字母/数字/连字符", "error")
        return redirect(url_for("templates_page"))
    content = request.form.get("content", "")
    tmp = TPL_DIR / f".{slug}.validate.json"
    tmp.write_text(content, encoding="utf-8")
    try:
        rt.load_template(tmp)  # 校验 JSON 结构、必备技能引用等
    except SystemExit as exc:
        flash(f"校验失败，未保存：{exc}", "error")
        tmp.unlink(missing_ok=True)
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            raw = None
        return render_template("template_edit.html", slug=slug, content=content,
                               raw=raw, active="templates")
    tmp.replace(TPL_DIR / f"{slug}.json")
    flash("模板已保存并通过校验")
    return redirect(url_for("template_edit", slug=slug))


@app.post("/templates-new")
def template_new():
    slug = request.form.get("slug", "").strip()
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug):
        flash("模板文件名只允许小写字母/数字/连字符（如 data-analyst）", "error")
        return redirect(url_for("templates_page"))
    dest = TPL_DIR / f"{slug}.json"
    if dest.exists():
        flash("同名模板已存在", "error")
        return redirect(url_for("templates_page"))
    default = json.loads((TPL_DIR / "python-backend.json").read_text(encoding="utf-8"))
    default["name"] = slug
    dest.write_text(json.dumps(default, ensure_ascii=False, indent=2), encoding="utf-8")
    flash("已基于默认模板创建，请修改技能配置后保存")
    return redirect(url_for("template_edit", slug=slug))


@app.post("/templates/import")
def template_import():
    """批量导入模板：逐个校验，合法才写入；同名自动加序号避免覆盖."""
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("未选择模板文件（.json）", "error")
        return redirect(url_for("templates_page"))

    added, renamed, skipped = [], [], []
    for f in files:
        if not f.filename.lower().endswith(".json"):
            skipped.append(f"{f.filename}（非 JSON）")
            continue
        raw_bytes = f.read()
        try:
            data = json.loads(raw_bytes.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            skipped.append(f"{f.filename}（无法解析：{exc}）")
            continue

        base = re.sub(r"[^a-z0-9-]", "-", Path(f.filename).stem.lower()).strip("-") or "template"
        slug, n = base, 2
        while (TPL_DIR / f"{slug}.json").exists():
            slug, n = f"{base}-{n}", n + 1
        dest = TPL_DIR / f"{slug}.json"
        dest.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            rt.load_template(dest)  # 结构不合法则回滚文件
        except SystemExit as exc:
            dest.unlink(missing_ok=True)
            skipped.append(f"{f.filename}（校验失败：{exc}）")
            continue
        (renamed if slug != base else added).append(f"{f.filename} → {slug}.json")

    parts = []
    if added:
        parts.append(f"导入 {len(added)} 个：{', '.join(added)}")
    if renamed:
        parts.append(f"改名导入 {len(renamed)} 个：{', '.join(renamed)}")
    if skipped:
        parts.append(f"跳过 {len(skipped)} 个：{', '.join(skipped)}")
    flash("；".join(parts) or "没有可导入的模板",
          "error" if skipped and not (added or renamed) else "message")
    return redirect(url_for("templates_page"))


@app.get("/templates/<slug>/export")
def template_export(slug: str):
    path = TPL_DIR / f"{slug}.json"
    if not path.is_file():
        abort(404)
    return send_file(path, as_attachment=True, download_name=f"{slug}.json",
                     mimetype="application/json")


@app.get("/templates-export-all")
def templates_export_all():
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for p in sorted(TPL_DIR.glob("*.json")):
            zf.writestr(p.name, p.read_text(encoding="utf-8"))
    buf.seek(0)
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return send_file(buf, as_attachment=True, download_name=f"resume-templates-{stamp}.zip",
                     mimetype="application/zip")


# ---------- 路由：图表视图 ----------

def _donut(items: list[dict]) -> str:
    """把 [{label,count,color}] 转成 conic-gradient 字符串（纯 CSS 环形图，无依赖）."""
    total = sum(i["count"] for i in items)
    if not total:
        return "conic-gradient(#e2e8f0 0% 100%)"
    stops, acc = [], 0.0
    for it in items:
        start = acc / total * 100
        acc += it["count"]
        end = acc / total * 100
        stops.append(f"{it['color']} {start:.2f}% {end:.2f}%")
    return f"conic-gradient({', '.join(stops)})"


@app.get("/charts")
def charts():
    conn = db()
    rows = conn.execute(
        "SELECT id, filename, imported_at FROM resumes WHERE deleted_at IS NULL ORDER BY id"
    ).fetchall()
    latest_all = latest_map(conn)

    status_counts = {"通过": 0, "待核实": 0, "未通过": 0, "未筛选": 0}
    scores: list[float] = []
    ai_cnt = kw_cnt = 0
    agree = both = 0
    for r in rows:
        engines = latest_all.get(r["id"], {})
        best = engines.get("ai") or engines.get("keyword")
        status_counts[best["status"] if best else "未筛选"] += 1
        if best and best["score"] is not None:
            scores.append(float(best["score"]))
        if "ai" in engines:
            ai_cnt += 1
        if "keyword" in engines:
            kw_cnt += 1
        if "ai" in engines and "keyword" in engines:
            both += 1
            agree += 1 if engines["ai"]["status"] == engines["keyword"]["status"] else 0

    total = len(rows)
    donut = _donut([
        {"label": "通过", "count": status_counts["通过"], "color": "#5E8C61"},
        {"label": "待核实", "count": status_counts["待核实"], "color": "#C89B3C"},
        {"label": "未通过", "count": status_counts["未通过"], "color": "#BF4D43"},
        {"label": "未筛选", "count": status_counts["未筛选"], "color": "#C9C4B8"},
    ])

    # 分数分布（20 分一档）
    bins = [0] * 5
    for s in scores:
        bins[min(int(s // 20), 4)] += 1
    hist = [{"label": f"{i * 20}-{i * 20 + 20}", "count": bins[i]} for i in range(5)]
    hist_max = max(bins) or 1

    # 各模板通过率（仅统计在库简历）
    alive = {r["id"] for r in rows}
    tpl_stats = []
    for t in list_templates():
        name = t["name"]
        latest = latest_map(conn, template=name)
        c = {"通过": 0, "待核实": 0, "未通过": 0}
        sc: list[float] = []
        for rid, engines in latest.items():
            if rid not in alive:
                continue
            best = engines.get("ai") or engines.get("keyword")
            if best:
                if best["status"] in c:
                    c[best["status"]] += 1
                if best["score"] is not None:
                    sc.append(float(best["score"]))
        n = sum(c.values())
        tpl_stats.append({
            "slug": t["slug"], "name": name, "total": n, **c,
            "pass_rate": round(c["通过"] / n * 100, 1) if n else 0.0,
            "avg_score": round(sum(sc) / len(sc), 1) if sc else None,
        })
    tpl_stats.sort(key=lambda x: -x["pass_rate"])

    # 技能命中热度：所有关键词结果里各技能的累计证据次数（仅统计在库简历）
    skill_hits: dict[str, int] = {}
    for rid, engines in latest_all.items():
        if rid not in alive:
            continue
        kw = engines.get("keyword")
        if not kw:
            continue
        ev = json.loads(kw["detail"]).get("evidence", {})
        for k, v in ev.items():
            skill_hits[k] = skill_hits.get(k, 0) + int(v)
    top_skills = sorted(skill_hits.items(), key=lambda kv: -kv[1])[:10]
    skill_max = top_skills[0][1] if top_skills else 1

    # 导入趋势（按日）
    trend: dict[str, int] = {}
    for r in rows:
        day = (r["imported_at"] or "")[:10]
        trend[day] = trend.get(day, 0) + 1
    trend_items = [{"label": d, "count": c} for d, c in sorted(trend.items())]
    trend_max = max((i["count"] for i in trend_items), default=1) or 1

    kpi = {
        "total": total,
        "screened": total - status_counts["未筛选"],
        "screened_pct": round((total - status_counts["未筛选"]) / total * 100, 1) if total else 0.0,
        "pass_rate": round(status_counts["通过"] / total * 100, 1) if total else 0.0,
        "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
        "ai_cover": round(ai_cnt / total * 100, 1) if total else 0.0,
        "agree_rate": round(agree / both * 100, 1) if both else None,
    }
    trash_n = conn.execute(
        "SELECT COUNT(*) AS n FROM resumes WHERE deleted_at IS NOT NULL").fetchone()["n"]
    return render_template("charts.html", trash_n=trash_n, kpi=kpi, status_counts=status_counts,
                           donut=donut, hist=hist, hist_max=hist_max,
                           tpl_stats=tpl_stats, top_skills=top_skills,
                           skill_max=skill_max, trend_items=trend_items,
                           trend_max=trend_max, active="charts")


# ---------- 路由：更新日志（历史公告） ----------

@app.get("/changelog")
def changelog():
    return render_template("changelog.html", entries=CHANGELOG,
                           latest=CHANGELOG[0]["version"] if CHANGELOG else "",
                           active="changelog")


@app.post("/templates/<slug>/favorite")
def template_favorite(slug: str):
    """收藏/取消收藏模板（存 config/app_settings.json，跨浏览器有效）."""
    if not (TPL_DIR / f"{slug}.json").is_file():
        return {"ok": False, "error": "模板不存在"}, 404
    on = toggle_favorite(slug)
    return {"ok": True, "slug": slug, "favorite": on, "favorites": get_favorites()}


# ---------- 路由：综合设置 ----------

@app.get("/settings")
def settings():
    providers, active = provider_status()
    return render_template("settings.html", providers=providers,
                           active_provider=active, current_version=current_version(),
                           active="settings")


@app.get("/settings/check-update")
def settings_check_update():
    return check_for_update()


@app.post("/settings")
def settings_save():
    data = rt.load_providers(PROVIDERS_PATH)
    data["active"] = request.form.get("active", data.get("active", ""))
    for name, spec in data.get("providers", {}).items():
        for field in ("label", "base_url", "model"):
            spec[field] = request.form.get(f"{name}__{field}", spec.get(field, "")).strip()
        key = request.form.get(f"{name}__api_key", "").strip()
        if key:  # 留空表示不修改已保存的 Key
            spec["api_key"] = key
    PROVIDERS_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    flash("AI 厂商配置已保存")
    return redirect(url_for("settings"))


@app.post("/settings/preview")
def settings_preview():
    cfg = load_app_settings()
    method = request.form.get("preview_method", "default")
    cfg["preview_method"] = method if method in OPEN_METHODS else "default"
    cfg["cloud_url"] = request.form.get("cloud_url", cfg.get("cloud_url", "")).strip()
    cfg["update_url"] = request.form.get("update_url", cfg.get("update_url", "")).strip()
    cfg["auto_check_updates"] = request.form.get("auto_check_updates") == "on"
    save_app_settings(cfg)
    flash("通用设置已保存")
    return redirect(url_for("settings"))


if __name__ == "__main__":
    # 单机桌面用途：只绑定回环地址，关闭 reloader（打包 exe 的硬性要求）
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)
