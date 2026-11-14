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
import logging
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from flask import (
    Flask, abort, flash, g, has_app_context, redirect, render_template, request,
    send_file, url_for,
)
from werkzeug.exceptions import HTTPException

import resume_tool as rt

CODE_DIR = Path(__file__).resolve().parent
RESOURCE_BASE = Path(getattr(sys, "_MEIPASS", CODE_DIR))


def resolve_data_dir() -> Path:
    """可写数据目录：显式环境变量 > 打包 exe 的 LOCALAPPDATA > 源码项目目录."""
    configured = os.environ.get("RESUME_TOOL_DATA_DIR", "").strip()
    if configured:
        return Path(configured).expanduser().resolve()
    if getattr(sys, "frozen", False):
        local = os.environ.get("LOCALAPPDATA", "").strip()
        root = Path(local) if local else (Path.home() / ".resume-tool")
        return (root / "ResumeTool").resolve()
    return CODE_DIR


DATA_DIR = resolve_data_dir()
DB = str(DATA_DIR / "resume_tool.db")
CONFIG_DIR = DATA_DIR / "config"
TPL_DIR = CONFIG_DIR / "templates"
PROVIDERS_PATH = CONFIG_DIR / "providers.json"
APP_SETTINGS_PATH = CONFIG_DIR / "app_settings.json"
UPLOAD_DIR = DATA_DIR / "uploads"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
LOG_FILE = LOG_DIR / "resume-tool.log"
GITHUB_REPOSITORY = "Vanessa0712/vanessa-resume-tool"
GITHUB_LATEST_RELEASE_URL = (
    f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
)
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPOSITORY}/releases"


def _copy_file_if_missing(src: Path, dst: Path) -> None:
    if src.is_file() and not dst.exists():
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)


def _copy_dir_missing(src: Path, dst: Path) -> None:
    if src.is_dir():
        dst.mkdir(parents=True, exist_ok=True)
        shutil.copytree(src, dst, dirs_exist_ok=True)


def ensure_data_layout() -> None:
    """创建可写数据目录，并把旧源码目录/旧 exe 旁的数据复制过来（不删除旧数据）."""
    try:
        for directory in (DATA_DIR, CONFIG_DIR, TPL_DIR, UPLOAD_DIR, BACKUP_DIR, LOG_DIR):
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"无法创建数据目录 {DATA_DIR}：{exc}") from exc

    bundled_templates = RESOURCE_BASE / "config" / "templates"
    if bundled_templates.resolve() != TPL_DIR.resolve():
        for path in bundled_templates.glob("*.json"):
            if not path.name.startswith("."):
                _copy_file_if_missing(path, TPL_DIR / path.name)

    if DATA_DIR.resolve() == CODE_DIR.resolve():
        return

    candidates = [CODE_DIR]
    if getattr(sys, "frozen", False):
        candidates.insert(0, Path(sys.executable).resolve().parent)
    for legacy in candidates:
        if legacy.resolve() == DATA_DIR.resolve() or not (legacy / "resume_tool.db").is_file():
            continue
        for name in ("resume_tool.db", "resume_tool.db-wal", "resume_tool.db-shm"):
            _copy_file_if_missing(legacy / name, DATA_DIR / name)
        _copy_dir_missing(legacy / "uploads", UPLOAD_DIR)
        _copy_dir_missing(legacy / "backups", BACKUP_DIR)
        _copy_file_if_missing(legacy / "config" / "providers.json", PROVIDERS_PATH)
        _copy_file_if_missing(legacy / "config" / "app_settings.json", APP_SETTINGS_PATH)
        legacy_templates = legacy / "config" / "templates"
        if legacy_templates.is_dir():
            for path in legacy_templates.glob("*.json"):
                if not path.name.startswith("."):
                    _copy_file_if_missing(path, TPL_DIR / path.name)
        break


ensure_data_layout()

app = Flask(__name__)
app.secret_key = "resume-tool-local-dev"  # 本地原型，无跨用户场景
app.config["TEMPLATES_AUTO_RELOAD"] = True  # UI 迭代期模板改动即时生效，无需重启
# 只接受回环地址的 Host，阻断 DNS rebinding；本应用仅面向 127.0.0.1 单机使用。
app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost", "127.0.0.1:5000", "localhost:5000"]
app.config["MAX_CONTENT_LENGTH"] = 256 * 1024 * 1024  # 上传/恢复上限，兼顾批量简历与数据库备份


def configure_logging() -> None:
    """运行日志写入数据目录：打包成无控制台 exe 后仍能排查现场问题."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=5, encoding="utf-8")
    except OSError:
        return
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    handler.setLevel(logging.INFO)

    root = logging.getLogger()
    root.setLevel(logging.INFO)
    # 避免重复添加（测试或多处导入时）
    for existing in root.handlers:
        if isinstance(existing, RotatingFileHandler) and \
                Path(getattr(existing, "baseFilename", "")) == LOG_FILE:
            return
    root.addHandler(handler)
    app.logger.setLevel(logging.INFO)


@app.before_request
def protect_local_mutations():
    """本地写操作只接受同源页面发起，挡住跨站表单提交的基本路径."""
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    allowed = {request.host_url.rstrip("/"), "http://127.0.0.1:5000", "http://localhost:5000"}
    origin = (request.headers.get("Origin") or "").rstrip("/")
    if origin and origin not in allowed:
        abort(403)
    referer = request.headers.get("Referer") or ""
    if referer and not any(referer.startswith(base + "/") for base in allowed):
        abort(403)
    return None

# 进入页面时弹出一次；改版本号或日期即再次对全员弹出
CHANGELOG = [
    {
        "version": "ver1.2.3",
        "channel": "Beta",
        "date": "2026-11-14",
        "title": "筛选中心记住模板选择",
        "entries": [
            "筛选中心勾选的模板自动记住，重启程序或第二天打开自动恢复，不用反复重选",
            "勾选变化即时保存，不运行筛选也会记住；开始筛选时同样写入",
            "模板被删除或改名后自动从记忆中剔除，不会出现无效选择",
            "应用设置改为原子写入，避免异常退出留下半截配置文件",
        ],
    },
    {
        "version": "ver1.2.2",
        "channel": "Beta",
        "date": "2026-11-14",
        "title": "隐私清理、运行日志与诊断包",
        "entries": [
            "彻底删除简历时同步清理数据目录内的原始文件；数据目录之外的历史文件不会被擅自删除，只提示",
            "设置页新增「清理孤立原文件」：找出重复导入留下的副本并释放磁盘空间，已导入简历的文件不受影响",
            "运行日志写入数据目录 logs/，按 2MB 滚动保留 5 份，打包成无控制台程序后仍能排查现场问题",
            "页面异常显示错误编号并写入日志，便于按编号定位",
            "设置页新增「导出诊断包」：环境、数据规模统计、脱敏配置和最近日志，不含简历正文与 API Key",
        ],
    },
    {
        "version": "ver1.2.1",
        "channel": "Beta",
        "date": "2026-11-14",
        "title": "数据可移植与企业可用性",
        "entries": [
            "数据目录与安装目录分离：打包后默认使用 LOCALAPPDATA，支持环境变量指定并自动迁移旧数据",
            "目录导入与上传文件统一复制进数据目录，数据库改存 uploads 相对路径；备份换机后原文件仍可打开",
            "全新机器可直接从备份恢复，不再要求目标机器先有数据库",
            "设置页新增数据备份与恢复：一致性快照、可选原始文件、每日自动备份、恢复前备份",
            "每次导入生成批次记录，逐文件展示新增、恢复、重复、无法解析和异常原因",
            "AI/漏斗提交前确认预计调用量；修复导出重复、CSV 口径、配置锁死与后台任务卡死",
        ],
    },
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
    """请求内复用同一连接并随请求关闭；后台线程自行创建、自行关闭."""
    if not has_app_context():
        return rt.get_conn(DB)
    if "db_conn" not in g:
        g.db_conn = rt.get_conn(DB)
    return g.db_conn


@app.teardown_appcontext
def close_db(_exc=None) -> None:
    conn = g.pop("db_conn", None)
    if conn is not None:
        conn.close()


def stored_resume_path(raw: str) -> Path:
    """把数据库中的路径解析为当前机器路径；uploads/... 为相对数据目录."""
    path = Path(raw)
    return path if path.is_absolute() else (DATA_DIR / path).resolve()


def normalize_upload_paths(conn) -> int:
    """把历史绝对路径中的 uploads/... 片段改为相对路径，便于换机和恢复."""
    changed = 0
    rows = conn.execute("SELECT id, path FROM resumes WHERE path IS NOT NULL").fetchall()
    for rid, raw_path in rows:
        raw = str(raw_path or "")
        path = Path(raw)
        if not path.is_absolute():
            continue
        parts = list(path.parts)
        try:
            idx = len(parts) - 1 - parts[::-1].index("uploads")
        except ValueError:
            continue
        tail = parts[idx + 1:]
        if not tail:
            continue
        stored = Path("uploads", *tail).as_posix()
        conn.execute("UPDATE resumes SET path=? WHERE id=?", (stored, rid))
        changed += 1
    if changed:
        conn.commit()
    return changed


DEPT_ORDER = ["人事", "电气", "业务", "IT"]
TEMPLATE_SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]*\Z")


def dept_sort_key(dept: str) -> tuple:
    return (DEPT_ORDER.index(dept) if dept in DEPT_ORDER else len(DEPT_ORDER), dept)


def template_dest(slug: str) -> Path | None:
    """模板写入路径：只接受小写 slug，并确认最终路径仍在模板目录内."""
    if not TEMPLATE_SLUG_RE.fullmatch(slug or ""):
        return None
    base = TPL_DIR.resolve()
    path = (base / f"{slug}.json").resolve()
    return path if path.parent == base else None


def template_path(slug: str) -> Path | None:
    """已存在模板的安全路径；非法或越界一律返回 None."""
    path = template_dest(slug)
    return path if path is not None and path.is_file() else None


def list_templates() -> list[dict]:
    """扫描模板目录，返回模板元信息（含部门/子职业，供分组与搜索）."""
    items = []
    for p in sorted(TPL_DIR.glob("*.json")):
        if p.name.startswith("."):
            continue
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
        except (SystemExit, Exception):
            items.append({"slug": p.stem, "name": p.stem + "（配置有误）",
                          "department": "未分类", "role": "", "skills": [],
                          "required": "-", "min_years": None, "core_count": 0, "nice_count": 0})
    items.sort(key=lambda t: (dept_sort_key(t["department"]), t["name"]))
    return items


def get_favorites() -> list[str]:
    favs = load_app_settings().get("favorites", [])
    return [s for s in favs if isinstance(s, str) and template_path(s) is not None]


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


def get_screen_templates() -> list[str]:
    """上次在筛选中心勾选的模板；模板被删除后自动忽略."""
    saved = load_app_settings().get("screen_templates", [])
    if not isinstance(saved, list):
        return []
    out: list[str] = []
    for slug in saved:
        if isinstance(slug, str) and slug not in out and template_path(slug) is not None:
            out.append(slug)
    return out


def save_screen_templates(slugs: list[str]) -> list[str]:
    """记住筛选中心的模板选择，供下次启动自动恢复；返回实际保存的 slug."""
    valid: list[str] = []
    for slug in slugs:
        if slug not in valid and template_path(slug) is not None:
            valid.append(slug)
    cfg = load_app_settings()
    if cfg.get("screen_templates") != valid:
        cfg["screen_templates"] = valid
        save_app_settings(cfg)
    return valid


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


def provider_status() -> tuple[dict, dict | None, str]:
    """返回配置、当前可用厂商和可展示错误；配置问题不再终止 Web 进程."""
    try:
        providers = rt.ensure_providers(PROVIDERS_PATH)
    except (OSError, rt.ProviderConfigError) as exc:
        return rt.default_providers(), None, f"AI 配置读取失败：{exc}"
    try:
        return providers, rt.get_active_provider(providers), ""
    except (AttributeError, TypeError) as exc:
        return rt.default_providers(), None, f"AI 配置结构不正确：{exc}"


def load_app_settings() -> dict:
    defaults = {
        "preview_method": "default",
        "cloud_url": "https://docs.qq.com/desktop",
        "auto_check_updates": True,
        "auto_backup": True,
        "screen_templates": [],
    }
    if APP_SETTINGS_PATH.is_file():
        try:
            defaults.update(json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8")))
        except json.JSONDecodeError:
            pass
    return defaults


def save_app_settings(data: dict) -> None:
    """原子写入：先写临时文件再替换，避免断电/中断留下半截 JSON."""
    APP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = APP_SETTINGS_PATH.with_name(APP_SETTINGS_PATH.name + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(APP_SETTINGS_PATH)


# ---------- 备份与恢复 ----------

def _safe_backup_name(name: str) -> bool:
    return bool(re.fullmatch(r"resume-tool-(?:auto|manual|pre-restore)-\d{8}-\d{6}(?:-\d+)?\.zip", name or ""))


def list_backups() -> list[dict]:
    if not BACKUP_DIR.is_dir():
        return []
    items = []
    for path in BACKUP_DIR.glob("*.zip"):
        if not _safe_backup_name(path.name):
            continue
        stat = path.stat()
        kind = "自动" if "-auto-" in path.name else (
            "恢复前" if "-pre-restore-" in path.name else "手动")
        size_kb = stat.st_size / 1024
        size_text = (f"{size_kb:.0f} KB" if size_kb < 1024
                     else f"{size_kb / 1024:.1f} MB")
        items.append({
            "name": path.name,
            "kind": kind,
            "size_text": size_text,
            "created_at": datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
        })
    return sorted(items, key=lambda item: item["name"], reverse=True)


def _prune_backups() -> None:
    """自动备份保留 7 份、恢复前备份 3 份、手动备份 20 份."""
    groups = {"auto": 7, "pre-restore": 3, "manual": 20}
    for kind, keep in groups.items():
        files = sorted(BACKUP_DIR.glob(f"resume-tool-{kind}-*.zip"), reverse=True)
        for path in files[keep:]:
            try:
                path.unlink()
            except OSError:
                pass


def create_backup(label: str = "manual", include_uploads: bool = False) -> Path:
    """生成一致性 ZIP：数据库（SQLite backup API）+ 配置；可选原始文件."""
    if not Path(DB).is_file():
        raise ValueError("简历库尚未创建，暂无内容可备份")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    base = f"resume-tool-{label}-{stamp}"
    target = BACKUP_DIR / f"{base}.zip"
    n = 2
    while target.exists():
        target = BACKUP_DIR / f"{base}-{n}.zip"
        n += 1

    with tempfile.TemporaryDirectory(prefix="resume-tool-backup-") as td:
        tmp_db = Path(td) / "resume_tool.db"
        src = sqlite3.connect(DB)
        dst = sqlite3.connect(tmp_db)
        try:
            src.backup(dst)
            check = dst.execute("PRAGMA integrity_check").fetchone()
            if not check or check[0] != "ok":
                raise ValueError("数据库完整性检查未通过，未生成备份")
        finally:
            dst.close()
            src.close()

        counts = {}
        db_conn = sqlite3.connect(tmp_db)
        try:
            for table in ("resumes", "persons", "screenings"):
                counts[table] = db_conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        finally:
            db_conn.close()

        metadata = {
            "app_version": current_version(),
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "include_uploads": bool(include_uploads),
            "counts": counts,
        }
        try:
            with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as zf:
                zf.write(tmp_db, "resume_tool.db")
                for path, arc in (
                    (APP_SETTINGS_PATH, "config/app_settings.json"),
                    (PROVIDERS_PATH, "config/providers.json"),
                ):
                    if path.is_file():
                        zf.write(path, arc)
                for path in sorted(TPL_DIR.glob("*.json")):
                    if not path.name.startswith("."):
                        zf.write(path, f"config/templates/{path.name}")
                if include_uploads and UPLOAD_DIR.is_dir():
                    for path in UPLOAD_DIR.rglob("*"):
                        if path.is_file():
                            zf.write(path, "uploads/" + path.relative_to(UPLOAD_DIR).as_posix())
                zf.writestr("backup-meta.json",
                            json.dumps(metadata, ensure_ascii=False, indent=2))
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    _prune_backups()
    return target


def maybe_auto_backup() -> Path | None:
    """每天首次启动自动备份一次（不含原始文件，避免拖慢启动）."""
    if not Path(DB).is_file():
        return None
    today = datetime.now().strftime("%Y%m%d")
    if any(f"resume-tool-auto-{today}" in p.name for p in BACKUP_DIR.glob("*.zip")):
        return None
    return create_backup("auto", include_uploads=False)


def backup_file(name: str) -> Path | None:
    if not _safe_backup_name(name):
        return None
    path = (BACKUP_DIR / name).resolve()
    return path if path.parent == BACKUP_DIR.resolve() and path.is_file() else None


def _validate_restore_db(path: Path) -> None:
    conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("备份中的数据库完整性检查未通过")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        missing = {"resumes", "persons", "screenings"} - tables
        if missing:
            raise ValueError(f"备份不是有效的简历库，缺少表：{', '.join(sorted(missing))}")
    finally:
        conn.close()


def restore_backup(archive: Path) -> dict:
    """从备份 ZIP/DB 恢复；恢复前必定先生成 pre-restore 备份."""
    with JOBS_LOCK:
        active = any(j.get("state") in ("queued", "running") for j in JOBS.values())
    if active:
        raise ValueError("仍有筛选任务在运行，请等待完成或重启应用后再恢复")

    restored_uploads = 0
    with tempfile.TemporaryDirectory(prefix="resume-tool-restore-") as td:
        temp = Path(td)
        db_file = archive
        config_entries: dict[str, bytes] = {}
        upload_entries: list[tuple[str, zipfile.ZipInfo]] = []
        zf = None
        if archive.suffix.lower() == ".zip":
            zf = zipfile.ZipFile(archive)
            try:
                names = {info.filename: info for info in zf.infolist()}
                if "resume_tool.db" not in names:
                    raise ValueError("备份包中缺少 resume_tool.db")
                info = names["resume_tool.db"]
                if info.file_size > 1024 * 1024 * 1024:
                    raise ValueError("备份中的数据库超过 1GB，已拒绝恢复")
                db_file = temp / "resume_tool.db"
                with zf.open(info) as src, open(db_file, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                allowed_config = {"config/app_settings.json", "config/providers.json"}
                for name, info in names.items():
                    if name in allowed_config:
                        if info.file_size <= 5 * 1024 * 1024:
                            config_entries[name] = zf.read(info)
                    elif name.startswith("config/templates/") and name.endswith(".json"):
                        slug = name[len("config/templates/"):-len(".json")]
                        if re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) \
                                and info.file_size <= 5 * 1024 * 1024:
                            config_entries[name] = zf.read(info)
                    elif name.startswith("uploads/") and not name.endswith("/"):
                        rel = name[len("uploads/"):]
                        if (rel and ".." not in Path(rel).parts
                                and not Path(rel).is_absolute()
                                and info.file_size <= 256 * 1024 * 1024):
                            upload_entries.append((rel, info))
            finally:
                zf.close()

        _validate_restore_db(db_file)
        pre = (create_backup("pre-restore", include_uploads=False)
               if Path(DB).is_file() else None)
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()
        src = sqlite3.connect(db_file)
        dst = sqlite3.connect(DB)
        try:
            src.backup(dst)
        finally:
            dst.close()
            src.close()
        rt._MIGRATED_DBS.clear()
        rt._FTS_STATE.clear()
        path_conn = sqlite3.connect(DB)
        try:
            normalize_upload_paths(path_conn)
        finally:
            path_conn.close()

        for name, raw in config_entries.items():
            try:
                json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            try:
                if name == "config/providers.json":
                    rt.write_providers(PROVIDERS_PATH, json.loads(raw.decode("utf-8")))
                elif name == "config/app_settings.json":
                    APP_SETTINGS_PATH.parent.mkdir(parents=True, exist_ok=True)
                    APP_SETTINGS_PATH.write_bytes(raw)
                else:
                    dest = template_dest(Path(name).stem)
                    if dest is not None:
                        dest.write_bytes(raw)
            except OSError:
                pass  # 数据库已恢复成功，单个配置写入失败不应把整体判为失败

        if upload_entries and zf is not None:
            zf = zipfile.ZipFile(archive)
            try:
                for rel, info in upload_entries:
                    dest = (UPLOAD_DIR / Path(rel)).resolve()
                    if not dest.is_relative_to(UPLOAD_DIR.resolve()):
                        continue
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        with zf.open(info) as src, open(dest, "wb") as out:
                            shutil.copyfileobj(src, out)
                        restored_uploads += 1
                    except OSError:
                        continue
            finally:
                zf.close()
    return {"pre_backup": pre.name if pre else "", "uploads": restored_uploads}


# ---------- 版本更新（GitHub 最新正式 Release） ----------

_update_condition = threading.Condition()
_update_result: dict | None = None
_update_checking = False


def current_version() -> str:
    return CHANGELOG[0]["version"] if CHANGELOG else "ver0.0.0"


def version_tuple(version: str) -> tuple[int, ...]:
    """ver1.2.0 / 1.2 / v2 统一转数字元组，用于递增比较."""
    nums = [int(n) for n in re.findall(r"\d+", version or "")[:4]]
    return tuple((nums + [0, 0, 0, 0])[:4])


def _update_failure(message: str) -> dict:
    return {
        "ok": False,
        "current_version": current_version(),
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "error": message,
        "source": f"GitHub Release · {GITHUB_REPOSITORY}",
    }


def _read_github_latest_release() -> dict:
    """读取固定仓库的最新正式 Release，并适配为统一的更新结果字段."""
    req = urllib.request.Request(
        GITHUB_LATEST_RELEASE_URL,
        headers={
            "User-Agent": f"ResumeTool/{current_version()}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    with urllib.request.urlopen(req, timeout=8) as resp:
        raw = resp.read(1_000_001)
    if len(raw) > 1_000_000:
        raise ValueError("GitHub Release 响应超过 1MB，已停止读取")
    release = json.loads(raw.decode("utf-8"))
    if not isinstance(release, dict):
        raise ValueError("GitHub Release 响应格式不正确")
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("latest 接口返回了草稿或预发布版本，已忽略")
    body = str(release.get("body") or "")
    notes = [line.strip() for line in body.splitlines() if line.strip()]
    return {
        "version": str(release.get("tag_name") or "").strip(),
        "title": str(release.get("name") or release.get("tag_name") or ""),
        "date": str(release.get("published_at") or "")[:10],
        "notes": notes[:12],
        # 固定打开发布页，由用户选择源码包或维护者上传的安装包，不自动下载执行。
        "download_url": str(release.get("html_url") or GITHUB_RELEASES_URL),
    }


def check_for_update() -> dict:
    """检查 GitHub 最新正式 Release；失败不会中断应用或误报为已是最新."""
    current = current_version()
    base = {"current_version": current, "checked_at": datetime.now().isoformat(timespec="seconds")}
    try:
        manifest = _read_github_latest_release()
        latest = manifest["version"]
        if not latest or not re.search(r"\d", latest):
            raise ValueError("GitHub Release 缺少有效的 tag_name 版本号")
        return {
            **base,
            "ok": True,
            "latest_version": latest,
            "update_available": version_tuple(latest) > version_tuple(current),
            "title": manifest["title"],
            "date": manifest["date"],
            "notes": manifest["notes"],
            "download_url": manifest["download_url"],
            "source": f"GitHub Release · {GITHUB_REPOSITORY}",
        }
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            message = "检查失败：仓库尚未发布正式 Release（latest 不包含草稿和预发布版本）"
        elif exc.code in (403, 429):
            message = f"检查失败：GitHub API 访问受限或触发限流（HTTP {exc.code}），请稍后再试"
        else:
            message = f"检查失败：GitHub 返回 HTTP {exc.code}"
        return {**base, "ok": False, "error": message,
                "source": f"GitHub Release · {GITHUB_REPOSITORY}"}
    except (OSError, ValueError, json.JSONDecodeError, urllib.error.URLError) as exc:
        return {**base, "ok": False, "error": f"检查失败：{exc}",
                "source": f"GitHub Release · {GITHUB_REPOSITORY}"}


def get_update_status() -> dict:
    """返回进程内更新状态；查询状态本身不会访问 GitHub."""
    with _update_condition:
        state = "checking" if _update_checking else ("done" if _update_result else "idle")
        result = dict(_update_result) if _update_result else None
    return {"state": state, "result": result, "current_version": current_version()}


def _update_check_worker() -> None:
    global _update_result, _update_checking
    try:
        result = check_for_update()
    except Exception as exc:  # 后台线程兜底，任何检查异常都不能影响主程序
        result = _update_failure(f"检查失败：{exc}")
    with _update_condition:
        _update_result = result
        _update_checking = False
        _update_condition.notify_all()
    if result.get("update_available"):
        app.logger.info("发现新版本：%s", result.get("latest_version"))
    elif not result.get("ok"):
        app.logger.warning("%s", result.get("error"))


def start_update_check() -> bool:
    """启动一次后台检查；已有检查进行中时返回 False，不重复请求 GitHub."""
    global _update_result, _update_checking
    with _update_condition:
        if _update_checking:
            return False
        _update_checking = True
    try:
        threading.Thread(
            target=_update_check_worker,
            name="resume-tool-update-check",
            daemon=True,
        ).start()
    except Exception as exc:
        with _update_condition:
            _update_result = _update_failure(f"无法启动更新检查：{exc}")
            _update_checking = False
            _update_condition.notify_all()
    return True


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
        "data_dir": str(DATA_DIR),
    }


@app.errorhandler(HTTPException)
def handle_http_error(exc: HTTPException):
    code = exc.code or 500
    messages = {
        403: ("无权执行此操作", "该请求不是从本机页面发起的，已被安全策略拦截。"),
        404: ("页面或简历不存在", "链接可能已经失效，或对应记录已被删除。"),
        413: ("文件太大", "单次上传不能超过 256 MB，请拆分批次或压缩后重试。"),
    }
    title, message = messages.get(code, (exc.name or "请求失败", exc.description or "请返回后重试"))
    return render_template("error.html", code=code, title=title, message=message), code


@app.errorhandler(Exception)
def handle_unexpected_error(exc: Exception):
    trace_id = uuid.uuid4().hex[:8].upper()
    app.logger.exception("未处理的页面异常 [%s]", trace_id)
    return render_template(
        "error.html", code=500, title="应用遇到问题",
        message="本次操作没有完成。已有简历数据不会因此删除，请返回重试。",
        trace_id=trace_id,
    ), 500


def parse_slugs(raw: str) -> list[str]:
    """把 'a,b,c' 解析成存在的模板 slug 列表（去重保序）."""
    out: list[str] = []
    for s in re.split(r"[,\s]+", raw or ""):
        s = s.strip()
        if s and s not in out and template_path(s) is not None:
            out.append(s)
    return out


def tpl_name(slug: str) -> str:
    path = template_path(slug)
    if path is None:
        raise ValueError("模板不存在或名称不合法")
    return rt.load_template(path).name


def parse_selected_ids(values: list[str]) -> list[int]:
    """解析 ids（兼容重复参数和逗号分隔），去重并保持选择顺序."""
    out: list[int] = []
    seen: set[int] = set()
    for value in values:
        for raw in re.split(r"[,\s]+", value or ""):
            if not raw.isdigit():
                continue
            rid = int(raw)
            if rid not in seen:
                seen.add(rid)
                out.append(rid)
    return out


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
    """在后台线程执行筛选：逐份更新进度，任何异常都落到任务状态而不是让进度卡死。"""
    conn = None
    try:
        conn = db()
        _, provider, provider_error = provider_status()
        if engine == "ai" and provider is None:
            reason = provider_error or "请先在「设置 → AI 厂商配置」中填写 API Key"
            job_update(jid, state="error",
                       message=f"AI 未就绪：{reason}；也可改用关键词引擎")
            return

        # 先规划：每个模板要处理的简历集合，便于算出总进度
        plan = []
        for slug in slugs:
            path = template_path(slug)
            if path is None:
                raise ValueError(f"模板 {slug} 不存在或名称不合法")
            tpl = rt.load_template(path)
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
    except BaseException as exc:  # 包含配置/模板读取抛出的 SystemExit，避免任务永久卡住
        detail = str(exc) or exc.__class__.__name__
        job_update(jid, state="error", current="", message=f"筛选任务失败：{detail}")
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def start_job(engine: str, slugs: list[str], ids: list[int] | None, scope: str) -> str:
    jid = job_new(engine, slugs, scope)
    try:
        threading.Thread(target=run_screening_job, args=(jid, engine, slugs, ids, scope),
                         daemon=True).start()
    except Exception as exc:
        job_update(jid, state="error", message=f"无法启动筛选任务：{exc}")
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
    if tpl_filter != "all" and template_path(tpl_filter) is not None:
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


def _batch_start(conn, source: str, total: int) -> int:
    cur = conn.execute(
        "INSERT INTO import_batches(source, created_at, total) VALUES(?,?,?)",
        (source, datetime.now().isoformat(timespec="seconds"), total),
    )
    conn.commit()
    return int(cur.lastrowid)


def _batch_item(conn, batch_id: int, filename: str, result: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO import_batch_items(batch_id, filename, result, detail, created_at)"
        " VALUES(?,?,?,?,?)",
        (batch_id, filename, result, detail,
         datetime.now().isoformat(timespec="seconds")),
    )


def _batch_finish(conn, batch_id: int, counts: dict) -> None:
    conn.execute(
        "UPDATE import_batches SET finished_at=?, added=?, restored=?, dup=?, empty=?, errors=?"
        " WHERE id=?",
        (datetime.now().isoformat(timespec="seconds"), counts["added"],
         counts["restored"], counts["dup"], counts["empty"], counts["errors"], batch_id),
    )
    conn.commit()


def _import_summary(counts: dict) -> str:
    return (f"新增 {counts['added']}，恢复 {counts['restored']}，重复 {counts['dup']}，"
            f"无法解析 {counts['empty']}，异常 {counts['errors']}")


@app.post("/import-dir")
def import_dir():
    src = Path(request.form.get("dir", "").strip())
    if not src.is_dir():
        flash(f"目录不存在: {src}", "error")
        return redirect(url_for("library"))
    files = [p for p in sorted(src.iterdir())
             if p.is_file() and p.suffix.lower() in rt.SUPPORTED_SUFFIXES
             and not p.name.startswith("~$")]
    if not files:
        flash("该目录下没有可导入的简历文件", "error")
        return redirect(url_for("library"))
    conn = db()
    counts = {"added": 0, "restored": 0, "dup": 0, "empty": 0, "errors": 0}
    batch_id = _batch_start(conn, f"本机目录：{src}", len(files))
    copied_dir = UPLOAD_DIR / f"dir-{int(time.time() * 1000)}"
    copied_dir.mkdir(parents=True, exist_ok=True)
    for p in files:
        dest = copied_dir / f"{p.stem[:160]}{p.suffix}"
        n = 2
        while dest.exists():
            dest = copied_dir / f"{p.stem[:150]}-{n}{p.suffix}"
            n += 1
        try:
            shutil.copy2(p, dest)
            result, detail, _ = rt.import_one_detail(
                conn, dest, store_base=UPLOAD_DIR)
            if result in ("dup", "empty"):
                dest.unlink(missing_ok=True)  # 未产生新引用，不保留重复副本
        except Exception as exc:
            result, detail = "error", f"导入异常：{exc}"
        if result == "error":
            counts["errors"] += 1
        elif result in counts:
            counts[result] += 1
        _batch_item(conn, batch_id, p.name, result, detail)
    _batch_finish(conn, batch_id, counts)
    flash(f"导入完成：{_import_summary(counts)}")
    return redirect(url_for("import_detail", bid=batch_id))


@app.post("/import-files")
def import_files():
    files = [f for f in request.files.getlist("files") if f.filename]
    if not files:
        flash("未选择文件", "error")
        return redirect(url_for("library"))
    conn = db()
    counts = {"added": 0, "restored": 0, "dup": 0, "empty": 0, "errors": 0}
    batch_id = _batch_start(conn, f"上传：{len(files)} 个文件", len(files))
    batch = UPLOAD_DIR / str(int(time.time() * 1000))
    batch.mkdir(parents=True, exist_ok=True)
    for f in files:
        # 保留中文文件名，仅去路径分隔符（secure_filename 会把中文剥光）
        safe = Path(f.filename).name.replace("/", "_").replace("\\", "_")
        dest = batch / safe
        try:
            f.save(dest)
            result, detail, _ = rt.import_one_detail(conn, dest, store_base=UPLOAD_DIR)
        except Exception as exc:
            result, detail = "error", f"导入异常：{exc}"
        if result == "error":
            counts["errors"] += 1
        elif result in counts:
            counts[result] += 1
        _batch_item(conn, batch_id, f.filename, result, detail)
    _batch_finish(conn, batch_id, counts)
    flash(f"上传导入完成：{_import_summary(counts)}")
    return redirect(url_for("import_detail", bid=batch_id))


@app.get("/imports")
def imports_list():
    conn = db()
    batches = conn.execute(
        "SELECT * FROM import_batches ORDER BY id DESC LIMIT 100").fetchall()
    return render_template("imports.html", batches=batches, active="library")


@app.get("/imports/<int:bid>")
def import_detail(bid: int):
    conn = db()
    batch = conn.execute("SELECT * FROM import_batches WHERE id=?", (bid,)).fetchone()
    if batch is None:
        abort(404)
    items = conn.execute(
        "SELECT * FROM import_batch_items WHERE batch_id=? ORDER BY id", (bid,)).fetchall()
    return render_template("import_detail.html", batch=batch, items=items,
                           active="library")


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

    src = stored_resume_path(row["path"])
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
    src = stored_resume_path(row["path"])
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
    src = stored_resume_path(row["path"])
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


def resume_file_on_disk(raw: str) -> Path | None:
    """解析原文件路径；只有落在数据目录 uploads/ 内才视为可管理文件."""
    raw = str(raw or "").strip()
    if not raw:
        return None
    try:
        resolved = stored_resume_path(raw).resolve()
        root = UPLOAD_DIR.resolve()
    except OSError:
        return None
    return resolved if resolved.is_relative_to(root) else None


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def _delete_upload_files(raw_paths: list[str], keep_paths: set[str] | None = None) -> tuple[int, int]:
    """删除数据目录内的原始文件；返回 (已删除, 未处理).

    外部目录导入的历史文件不会被擅自删除，只计入未处理，由用户自行决定。
    """
    keep_paths = keep_paths or set()
    root = UPLOAD_DIR.resolve()
    deleted = kept = 0
    touched_dirs: set[Path] = set()
    for raw in raw_paths:
        if raw in keep_paths:
            kept += 1
            continue
        path = resume_file_on_disk(raw)
        if path is None:
            if str(raw or "").strip():
                kept += 1
            continue
        try:
            if path.is_file():
                path.unlink()
                deleted += 1
                touched_dirs.add(path.parent)
            else:
                kept += 1
        except OSError as exc:
            app.logger.warning("删除原文件失败 %s：%s", path, exc)
            kept += 1
    # 清掉空的批次目录，避免 uploads/ 里堆大量空文件夹
    for directory in touched_dirs:
        try:
            if directory != root and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass
    return deleted, kept


def _purge_message(filename: str, deleted: int, kept: int) -> str:
    if deleted and kept:
        return f"已彻底删除「{filename}」，其中 {deleted} 个原始文件已从数据目录移除；{kept} 个文件在数据目录之外，未删除"
    if deleted:
        return f"已彻底删除「{filename}」及其原始文件"
    if kept:
        return f"已彻底删除「{filename}」及其筛选记录；原始文件不在数据目录，未自动删除"
    return f"已彻底删除「{filename}」及其筛选记录"


def find_orphan_uploads(conn) -> tuple[list[Path], int]:
    """找出 uploads/ 中没有被任何简历引用的文件（含重复导入留下的副本）."""
    referenced: set[Path] = set()
    for (raw,) in conn.execute("SELECT path FROM resumes WHERE path IS NOT NULL"):
        path = resume_file_on_disk(raw)
        if path is not None:
            referenced.add(path)
    orphans: list[Path] = []
    total = 0
    if UPLOAD_DIR.is_dir():
        for path in UPLOAD_DIR.rglob("*"):
            if not path.is_file():
                continue
            try:
                resolved = path.resolve()
                if resolved in referenced:
                    continue
                total += path.stat().st_size
            except OSError:
                continue
            orphans.append(path)
    return orphans, total


@app.post("/trash/<int:rid>/purge")
def trash_purge(rid: int):
    conn = db()
    row = conn.execute("SELECT filename, path FROM resumes WHERE id=? AND deleted_at IS NOT NULL",
                       (rid,)).fetchone()
    if row is None:
        flash("该记录不在回收站中", "error")
        return redirect(url_for("trash"))
    conn.execute("DELETE FROM resumes WHERE id=?", (rid,))  # 筛选记录随外键级联删除
    _fts_forget(conn, [rid])
    _drop_orphan_persons(conn)
    conn.commit()
    remaining = {r[0] for r in conn.execute("SELECT path FROM resumes WHERE path IS NOT NULL")}
    deleted, kept = _delete_upload_files([row["path"]], keep_paths=remaining)
    flash(_purge_message(row["filename"], deleted, kept))
    return redirect(url_for("trash"))


@app.post("/trash/empty")
def trash_empty():
    conn = db()
    rows = conn.execute("SELECT id, path FROM resumes WHERE deleted_at IS NOT NULL").fetchall()
    ids = [r["id"] for r in rows]
    paths = [r["path"] for r in rows]
    conn.execute("DELETE FROM resumes WHERE deleted_at IS NOT NULL")
    _fts_forget(conn, ids)
    _drop_orphan_persons(conn)
    conn.commit()
    remaining = {r[0] for r in conn.execute("SELECT path FROM resumes WHERE path IS NOT NULL")}
    deleted, kept = _delete_upload_files(paths, keep_paths=remaining)
    msg = f"回收站已清空，彻底删除 {len(ids)} 份简历"
    if deleted:
        msg += f"，同时移除 {deleted} 个原始文件"
    if kept:
        msg += f"；{kept} 个原文件不在数据目录，未自动删除"
    flash(msg)
    return redirect(url_for("trash"))


@app.get("/export")
def export_csv():
    """导出 CSV：沿用当前搜索/模板口径，不再静默导出全库."""
    conn = db()
    q = request.args.get("q", "").strip()
    tpl_arg = request.args.get("template", "all")
    tpl_label = tpl_name(tpl_arg) if (
        tpl_arg != "all" and template_path(tpl_arg) is not None) else None
    picked = parse_selected_ids(request.args.getlist("ids"))
    if picked:
        ids = picked
    elif q:
        ids = rt.search_ids(conn, q)
    else:
        ids = None
    try:
        report = rt.build_report_csv(conn, ids, template=tpl_label)
    except ValueError as exc:
        flash(f"无法导出 CSV：{exc}", "error")
        return redirect(url_for("library", q=q or None, template=tpl_arg))
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    buf = io.BytesIO(report.encode("utf-8-sig"))
    return send_file(buf, as_attachment=True,
                     download_name=f"resume_report-{stamp}.csv",
                     mimetype="text/csv")


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
    tpl_label = tpl_name(tpl_arg) if (
        tpl_arg != "all" and template_path(tpl_arg) is not None) else None
    picked = parse_selected_ids(request.args.getlist("ids"))

    if picked:
        ids = picked
    elif q:
        ids = rt.search_ids(conn, q)
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM resumes WHERE deleted_at IS NULL ORDER BY id DESC").fetchall()]
    ids = list(dict.fromkeys(ids))
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
    remembered = False
    if not slugs:
        # 没有显式指定模板时，恢复上次的选择，避免每次重新勾选
        slugs = get_screen_templates()
        remembered = bool(slugs)
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

    _, provider, _ = provider_status()
    tpl_meta = [{"slug": t["slug"], "name": t["name"], "department": t["department"],
                 "role": t["role"], "required": t["required"], "skills": t["skills"]}
                for t in templates]
    return render_template("screen.html", templates=templates, engines=ENGINES,
                           results_by_tpl=results_by_tpl, slugs=slugs, resumes=resumes,
                           favorites=get_favorites(), tpl_meta=tpl_meta,
                           remembered=remembered, saved_templates=get_screen_templates(),
                           resume_total=len(resumes),
                           ai_ready=provider is not None, job=job_view, active="screen")


@app.post("/screen/templates")
def screen_templates_save():
    """前端勾选变化时即时记住，未运行筛选也能在下次打开时恢复."""
    raw = ",".join(request.form.getlist("templates")) or request.form.get("templates", "")
    return {"ok": True, "templates": save_screen_templates(parse_slugs(raw))}


@app.post("/screen/run")
def screen_run():
    slugs = parse_slugs(",".join(request.form.getlist("templates")))
    engine = request.form.get("engine", "keyword")
    if engine not in ENGINES:
        engine = "keyword"
    scope = request.form.get("scope", "all")
    if scope not in ("all", "unscreened", "selected"):
        scope = "all"
    ids_raw = request.form.get("ids", "").strip()
    ids = parse_selected_ids([ids_raw]) or None
    if not slugs:
        flash("请至少勾选一个筛选模板", "error")
        return redirect(url_for("screen"))
    if scope == "selected" and not ids:
        flash("已选择「指定简历」但未勾选任何简历，请重新选择范围", "error")
        return redirect(url_for("screen", templates=",".join(slugs)))

    save_screen_templates(slugs)  # 开始筛选即记住本次选择
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
    path = template_path(slug)
    if path is None:
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
    dest = template_dest(slug)
    if dest is None:
        flash("模板文件名只允许小写字母/数字/连字符", "error")
        return redirect(url_for("templates_page"))
    content = request.form.get("content", "")
    tmp = TPL_DIR / f".{slug}.validate.json"
    tmp.write_text(content, encoding="utf-8")
    try:
        rt.load_template(tmp)  # 校验 JSON 结构、必备技能引用等
    except (SystemExit, Exception) as exc:
        flash(f"校验失败，未保存：{exc}", "error")
        tmp.unlink(missing_ok=True)
        try:
            raw = json.loads(content)
        except json.JSONDecodeError:
            raw = None
        return render_template("template_edit.html", slug=slug, content=content,
                               raw=raw, active="templates")
    tmp.replace(dest)
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
    path = template_path(slug)
    if path is None:
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
    if template_path(slug) is None:
        return {"ok": False, "error": "模板不存在"}, 404
    on = toggle_favorite(slug)
    return {"ok": True, "slug": slug, "favorite": on, "favorites": get_favorites()}


# ---------- 路由：综合设置 ----------

@app.get("/settings")
def settings():
    providers, active, provider_error = provider_status()
    orphans, orphan_bytes = find_orphan_uploads(db())
    return render_template("settings.html", providers=providers,
                           active_provider=active, provider_error=provider_error,
                           current_version=current_version(),
                           backups=list_backups(),
                           orphan_count=len(orphans),
                           orphan_size=human_size(orphan_bytes),
                           log_file=str(LOG_FILE),
                           active="settings")


@app.post("/settings/backup/create")
def settings_backup_create():
    include_uploads = request.form.get("include_uploads") == "on"
    try:
        path = create_backup("manual", include_uploads=include_uploads)
    except (OSError, ValueError, sqlite3.DatabaseError) as exc:
        flash(f"备份失败：{exc}", "error")
    else:
        size = round(path.stat().st_size / 1024 / 1024, 1)
        flash(f"备份已生成：{path.name}（{size} MB）", "message")
    return redirect(url_for("settings", _anchor="backup"))


@app.get("/settings/backups/<name>")
def settings_backup_download(name: str):
    path = backup_file(name)
    if path is None:
        abort(404)
    return send_file(path, as_attachment=True, download_name=path.name,
                     mimetype="application/zip")


def _diagnostic_summary(conn) -> dict:
    """诊断摘要：只统计规模与环境，不读取简历正文."""
    def scalar(sql: str, default=0):
        try:
            row = conn.execute(sql).fetchone()
            return row[0] if row and row[0] is not None else default
        except sqlite3.DatabaseError:
            return default

    db_path = Path(DB)
    uploads, upload_bytes = find_orphan_uploads(conn)
    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "app_version": current_version(),
        "python": platform.python_version(),
        "platform": platform.platform(),
        "data_dir": str(DATA_DIR),
        "db_file": db_path.name,
        "db_bytes": db_path.stat().st_size if db_path.is_file() else 0,
        "counts": {
            "resumes": scalar("SELECT COUNT(*) FROM resumes WHERE deleted_at IS NULL"),
            "trash": scalar("SELECT COUNT(*) FROM resumes WHERE deleted_at IS NOT NULL"),
            "persons": scalar("SELECT COUNT(*) FROM persons"),
            "screenings": scalar("SELECT COUNT(*) FROM screenings"),
            "import_batches": scalar("SELECT COUNT(*) FROM import_batches"),
            "templates": len(list(TPL_DIR.glob("*.json"))) if TPL_DIR.is_dir() else 0,
        },
        "uploads": {
            "orphan_files": len(uploads),
            "orphan_bytes": upload_bytes,
            "backup_files": len(list_backups()),
        },
    }


def _redacted_providers() -> dict:
    """厂商配置去掉 Key，只保留是否已配置，便于排查又不泄露凭据."""
    try:
        data = json.loads(PROVIDERS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"error": "配置文件缺失或无法解析"}
    out = {"active": data.get("active", ""), "providers": {}}
    for name, spec in (data.get("providers") or {}).items():
        if not isinstance(spec, dict):
            continue
        out["providers"][name] = {
            "label": spec.get("label", ""),
            "base_url": spec.get("base_url", ""),
            "model": spec.get("model", ""),
            "api_key_configured": bool(str(spec.get("api_key", "")).strip()),
        }
    return out


@app.get("/settings/diagnostics")
def settings_diagnostics():
    """导出诊断包：环境、统计、脱敏配置和运行日志，默认不含简历正文与 API Key."""
    conn = db()
    summary = _diagnostic_summary(conn)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("summary.json", json.dumps(summary, ensure_ascii=False, indent=2))
        zf.writestr("config/providers.redacted.json",
                    json.dumps(_redacted_providers(), ensure_ascii=False, indent=2))
        if APP_SETTINGS_PATH.is_file():
            zf.writestr("config/app_settings.json",
                        APP_SETTINGS_PATH.read_text(encoding="utf-8"))
        logs = sorted(LOG_DIR.glob("resume-tool.log*"))[-3:] if LOG_DIR.is_dir() else []
        for path in logs:
            try:
                zf.write(path, f"logs/{path.name}")
            except OSError:
                continue
        zf.writestr(
            "README.txt",
            "简历工具诊断包\n"
            f"生成时间：{summary['generated_at']}\n"
            f"应用版本：{summary['app_version']}\n\n"
            "包含：环境与数据规模统计、脱敏后的 AI 配置、最近运行日志。\n"
            "不包含：简历正文、候选人个人信息、API Key、数据库文件。\n"
            "如需进一步排查，可另行提供备份文件（备份含个人信息，请走受控渠道）。\n",
        )
    buffer.seek(0)
    name = f"resume-tool-diagnostics-{datetime.now():%Y%m%d-%H%M}.zip"
    app.logger.info("已导出诊断包：%s", name)
    return send_file(buffer, as_attachment=True, download_name=name,
                     mimetype="application/zip")


@app.post("/settings/uploads/clean")
def settings_uploads_clean():
    """清理 uploads/ 中没有任何简历引用的文件（重复导入副本、失败上传残留）."""
    conn = db()
    orphans, total = find_orphan_uploads(conn)
    if not orphans:
        flash("没有需要清理的孤立文件")
        return redirect(url_for("settings", _anchor="diagnostics"))
    removed = 0
    touched: set[Path] = set()
    root = UPLOAD_DIR.resolve()
    for path in orphans:
        try:
            path.unlink()
            removed += 1
            touched.add(path.parent)
        except OSError as exc:
            app.logger.warning("清理孤立文件失败 %s：%s", path, exc)
    for directory in touched:
        try:
            if directory != root and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass
    app.logger.info("清理孤立文件 %s 个，释放 %s 字节", removed, total)
    flash(f"已清理 {removed} 个孤立文件，释放约 {human_size(total)}")
    return redirect(url_for("settings", _anchor="diagnostics"))


@app.post("/settings/backups/<name>/restore")
def settings_backup_restore_existing(name: str):
    path = backup_file(name)
    if path is None:
        abort(404)
    try:
        result = restore_backup(path)
    except (OSError, ValueError, zipfile.BadZipFile, sqlite3.DatabaseError) as exc:
        flash(f"恢复失败，原数据未改变：{exc}", "error")
    else:
        before = (f"恢复前数据备份为 {result['pre_backup']}"
                  if result["pre_backup"] else "目标是空数据目录，未生成恢复前备份")
        flash(f"已从 {name} 恢复；{before}", "message")
    return redirect(url_for("settings", _anchor="backup"))


@app.post("/settings/restore")
def settings_restore():
    upload = request.files.get("backup")
    if upload is None or not upload.filename:
        flash("请选择要恢复的备份文件（.zip 或 .db）", "error")
        return redirect(url_for("settings", _anchor="backup"))
    suffix = Path(upload.filename).suffix.lower()
    if suffix not in (".zip", ".db"):
        flash("备份格式不支持，请选择本应用生成的 .zip 或 .db 文件", "error")
        return redirect(url_for("settings", _anchor="backup"))
    try:
        with tempfile.TemporaryDirectory(prefix="resume-tool-upload-") as td:
            archive = Path(td) / f"restore{suffix}"
            upload.save(archive)
            result = restore_backup(archive)
    except (OSError, ValueError, zipfile.BadZipFile, sqlite3.DatabaseError) as exc:
        flash(f"恢复失败，原数据未改变：{exc}", "error")
    else:
        msg = (f"恢复完成；恢复前数据已备份为 {result['pre_backup']}"
               if result["pre_backup"] else "恢复完成；目标是空数据目录，未生成恢复前备份")
        if result["uploads"]:
            msg += f"，同时恢复了 {result['uploads']} 个原始文件"
        flash(msg, "message")
    return redirect(url_for("settings", _anchor="backup"))


@app.get("/settings/update-status")
def settings_update_status():
    return get_update_status()


@app.get("/settings/check-update")
def settings_check_update():
    start_update_check()
    return get_update_status()


@app.post("/settings")
def settings_save():
    try:
        data = rt.ensure_providers(PROVIDERS_PATH)
        repaired = ""
    except (OSError, rt.ProviderConfigError) as exc:
        # 原配置损坏时先保留取证副本，再用缺省结构接收本次表单，避免把修复入口锁死。
        backup = ""
        try:
            if PROVIDERS_PATH.is_file():
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
                broken = PROVIDERS_PATH.with_name(f"providers.broken-{stamp}.json")
                shutil.copy2(PROVIDERS_PATH, broken)
                backup = f"，原文件已备份为 {broken.name}"
        except OSError:
            pass
        data = rt.default_providers()
        repaired = f"原配置无法读取（{exc}）{backup}，已用缺省配置重建。"

    data["active"] = request.form.get("active", data.get("active", ""))
    for name, spec in data.get("providers", {}).items():
        for field in ("label", "base_url", "model"):
            spec[field] = request.form.get(f"{name}__{field}", spec.get(field, "")).strip()
        key = request.form.get(f"{name}__api_key", "").strip()
        if key:  # 留空表示不修改已保存的 Key
            spec["api_key"] = key
    rt.write_providers(PROVIDERS_PATH, data)
    flash(repaired + "AI 厂商配置已保存", "error" if repaired else "message")
    return redirect(url_for("settings"))


@app.post("/settings/preview")
def settings_preview():
    cfg = load_app_settings()
    method = request.form.get("preview_method", "default")
    cfg["preview_method"] = method if method in OPEN_METHODS else "default"
    cfg["cloud_url"] = request.form.get("cloud_url", cfg.get("cloud_url", "")).strip()
    cfg.pop("update_url", None)  # 旧版自定义清单地址已由固定 GitHub Release 源取代
    cfg["auto_check_updates"] = request.form.get("auto_check_updates") == "on"
    cfg["auto_backup"] = request.form.get("auto_backup") == "on"
    save_app_settings(cfg)
    flash("通用设置已保存")
    return redirect(url_for("settings"))


def main() -> None:
    configure_logging()
    app.logger.info("启动简历工具 %s；数据目录 %s", current_version(), DATA_DIR)
    cfg = load_app_settings()
    try:
        conn = rt.get_conn(DB)
        try:
            normalize_upload_paths(conn)
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        app.logger.warning("简历路径迁移失败：%s", exc)
    if cfg.get("auto_check_updates", True):
        start_update_check()
    if cfg.get("auto_backup", True):
        try:
            backup = maybe_auto_backup()
            if backup:
                app.logger.info("已生成每日自动备份：%s", backup.name)
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            app.logger.warning("自动备份失败，不影响应用启动：%s", exc)
    # 单机桌面用途：只绑定回环地址，关闭 reloader（打包 exe 的硬性要求）
    app.run(host="127.0.0.1", port=5000, debug=False, use_reloader=False, threaded=True)


if __name__ == "__main__":
    main()
