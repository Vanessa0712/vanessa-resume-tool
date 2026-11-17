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

import copy
import io
import json
import logging
import multiprocessing
import os
import platform
import re
import secrets
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
import webbrowser
import zipfile
from datetime import date, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from urllib.parse import urlsplit

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
TRASH_DIR = DATA_DIR / ".trash"
TRASH_RETENTION_DAYS = 30
# 恢复备份的解压护栏：上传限制只管压缩包本身（256MB），高压缩比的 zip 可把小包
# 解成几十 GB。条目数与累计字节双限，且以「实际解压字节」为准——ZipInfo.file_size
# 是包头里的声明值，可被伪造，不能作为唯一依据。
RESTORE_MAX_ENTRIES = 5000
RESTORE_MAX_SINGLE_BYTES = 256 * 1024 * 1024
RESTORE_MAX_UPLOAD_BYTES = 1024 * 1024 * 1024
RESTORE_CHUNK_BYTES = 1024 * 1024
RESTORE_STAGE_NAME = ".restore-uploads-tmp"
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
        for directory in (DATA_DIR, CONFIG_DIR, TPL_DIR, UPLOAD_DIR, BACKUP_DIR, LOG_DIR, TRASH_DIR):
            directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RuntimeError(f"无法创建数据目录 {DATA_DIR}：{exc}") from exc

    # 恢复流程异常中止会留下解压暂存区；启动时兜底清理（此刻不可能有恢复在跑）
    shutil.rmtree(DATA_DIR / RESTORE_STAGE_NAME, ignore_errors=True)

    bundled_templates = RESOURCE_BASE / "config" / "templates"
    if bundled_templates.resolve() != TPL_DIR.resolve():
        for path in bundled_templates.glob("*.json"):
            if not path.name.startswith("."):
                _copy_file_if_missing(path, TPL_DIR / path.name)

    if os.environ.get("RESUME_TOOL_SKIP_MIGRATION") == "1":
        return  # 测试环境：只建目录与内置模板，不把本机旧数据搬进来

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


def _load_or_create_secret_key(path: Path) -> str:
    """读取或生成持久化随机会话密钥；文件不进入备份和诊断包."""
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as exc:
        raise RuntimeError(f"无法读取会话密钥文件 {path}：{exc}") from exc
    if len(existing) >= 32:
        return existing

    value = secrets.token_urlsafe(48)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(value, encoding="utf-8")
        try:
            os.chmod(tmp, 0o600)
        except OSError:
            pass  # Windows ACL 由用户配置；原子替换仍然有效
        os.replace(tmp, path)
    except OSError as exc:
        raise RuntimeError(f"无法写入随机会话密钥 {path}：{exc}") from exc
    return value


app = Flask(__name__,
            template_folder=str(RESOURCE_BASE / "templates"),
            static_folder=str(RESOURCE_BASE / "static"))
# 打包成 exe 后 __name__ 为 __main__，Flask 推断的 root_path 不可靠；
# 显式指向 RESOURCE_BASE（源码运行=项目目录，PyInstaller 运行=_MEIPASS）。
_configured_secret = os.environ.get("RESUME_TOOL_SECRET_KEY", "").strip()
if _configured_secret and len(_configured_secret) < 32:
    raise RuntimeError("RESUME_TOOL_SECRET_KEY 至少需要 32 个字符")
app.secret_key = (_configured_secret or
                  _load_or_create_secret_key(CONFIG_DIR / ".flask_secret_key"))
app.config["TEMPLATES_AUTO_RELOAD"] = True  # UI 迭代期模板改动即时生效，无需重启
# 只接受回环地址的 Host，阻断 DNS rebinding；本应用仅面向 127.0.0.1 单机使用。
# 实际监听端口在 main() 里可能顺延，届时会重写这份白名单。
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
    """本地写操作只接受同源页面发起，挡住跨站表单提交的基本路径.

    注意 Fetch 规范的一条细节：请求的 referrer policy 为 no-referrer 时，
    浏览器会把同源表单 POST 的 Origin 序列化成字面量 "null"。因此不能只认
    Origin/Referer，还要接受 Sec-Fetch-Site: same-origin 作为同源证据——
    该头同样由浏览器写入且页面脚本无法伪造；跨站场景下它的值是
    cross-site，伪造不出 same-origin。
    """
    if request.method in ("GET", "HEAD", "OPTIONS"):
        return None
    allowed = {request.host_url.rstrip("/"), "http://127.0.0.1:5000", "http://localhost:5000"}
    origin = (request.headers.get("Origin") or "").rstrip("/")
    referer = request.headers.get("Referer") or ""
    fetch_site = (request.headers.get("Sec-Fetch-Site") or "").strip().lower()

    # "null" 是浏览器在 no-referrer 策略下对同源表单序列化的占位值，不具判定力，
    # 按「未提供」处理；真正的同源证据交给下面的 Sec-Fetch-Site 兜底。
    has_origin = bool(origin) and origin != "null"
    if has_origin and origin not in allowed:
        abort(403)
    if referer and not any(referer.startswith(base + "/") for base in allowed):
        abort(403)
    if has_origin or referer:
        return None
    if fetch_site == "same-origin":
        return None
    # Origin 缺失或为 null、Referer 也没有、且无同源标记：不再默认放行。
    abort(403)


_URL_QUERY_VALUE_RE = re.compile(r"([?&][A-Za-z_][A-Za-z0-9_]*=)[^&\s\"']+")


def redact_log_text(text: str) -> str:
    """打包日志前脱敏：清掉查询参数值、手机、邮箱、身份证与银行卡.

    诊断包对外承诺不含候选人个人信息，而日志里的搜索词可能正是姓名
    （例如 `GET /?q=张三`），因此必须先在文本层清掉再写入压缩包。
    """
    out = _URL_QUERY_VALUE_RE.sub(r"\1***", text or "")
    out = rt._PHONE_RE.sub("[手机]", out)
    out = rt._EMAIL_RE.sub("[邮箱]", out)
    out = rt._ID_CARD_RE.sub("[证件]", out)   # 先于银行卡：身份证也是 18 位数字
    out = rt._BANK_CARD_RE.sub("[卡号]", out)
    return out


@app.after_request
def log_access(response):
    """记录每次请求：waitress 不提供访问日志，用户报问题时可回溯操作.

    查询参数只记名称不记取值——搜索词往往是候选人姓名，落盘即泄露。
    """
    if not app.config.get("TESTING") and not request.path.startswith("/static/"):
        keys = ",".join(sorted(request.args.keys()))
        suffix = f"?{keys}" if keys else ""
        app.logger.info("%s %s%s -> %s", request.method, request.path, suffix,
                        response.status_code)
    return response


@app.after_request
def set_security_headers(response):
    """本机页面同样按不可嵌入处理，切断点击劫持到敏感写操作的链路."""
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Content-Security-Policy", "frame-ancestors 'none'")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    # 必须是 same-origin 而不是 no-referrer：后者会让浏览器按 Fetch 规范把
    # 同源表单 POST 的 Origin 置为 "null"，同源校验将无法通过（上传等写操作 403）。
    # same-origin 只对本站发送完整 Referer，跨站一律不发，不泄露路径。
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response

# 进入页面时弹出一次；改版本号或日期即再次对全员弹出
CHANGELOG = [
    {
        "version": "ver1.1.0",
        "channel": "Beta",
        "date": "2026-11-17",
        "title": "Beta 1.1.0：改为独立桌面窗口",
        "entries": [
            "界面从浏览器标签页改为独立应用窗口：有独立任务栏图标与标题栏，不再占地浏览器标签、也不会误关到别处",
            "关闭窗口即退出程序，不再需要在设置页手动退出；后台服务随窗口一起结束",
            "筛选任务运行中关窗会先弹确认框，避免误点关闭把跑到一半的任务掐断",
            "同一时间只允许运行一个实例：重复双击会直接切换到已打开的窗口，避免两个进程同时写同一份简历库",
            "系统缺少 WebView2 运行时时自动回退到浏览器打开，功能不受影响；也可用 --browser 参数主动使用浏览器模式",
            "导出 CSV / xlsx 报告改用系统「另存为」对话框选择保存位置，导出明细、批量报告与模板导出均可用",
            "设置页「退出程序」在桌面模式下改为关闭窗口，与点窗口关闭按钮走同一条路径",
        ],
    },
    {
        "version": "ver1.0.2",
        "channel": "Beta",
        "date": "2026-11-17",
        "title": "Beta 1.0.2：修复表单提交被误判为 403",
        "entries": [
            "修复 1.0.1 中上传等表单提交一律报 403 的问题：Referrer-Policy 由 no-referrer 改为 same-origin，浏览器不会再按 Fetch 规范把同源表单的 Origin 置为 null",
            "同源校验补充 Sec-Fetch-Site: same-origin 作为兜底证据；跨站来源仍然一律拒绝，安全性不变",
            "受影响的操作已恢复：简历上传与目录导入、模板导入、设置保存、备份恢复、批量删除、清空历史、退出程序",
            "附带改善：同源 Referer 恢复正常后，删除等操作会回到来源页并保留搜索词与页码",
        ],
    },
    {
        "version": "ver1.0.1",
        "channel": "Beta",
        "date": "2026-11-17",
        "title": "Beta 1.0.1：恢复备份解压护栏与 Windows 安装包",
        "entries": [
            "恢复备份新增解压护栏：条目数上限 5000、单文件 256MB、累计解压上限 1GB；按实际解压字节计数，伪造大小声明的压缩炸弹同样被拦截",
            "超限时在写入上传目录之前中止，恢复失败不会留下半截文件；启动时自动清理上次残留的暂存目录",
            "新增 Windows 安装包：中文安装向导，默认以当前用户身份安装、无需管理员权限，卸载不会删除数据目录中的简历与备份",
            "新增程序图标与 exe 版本信息，文件属性页可查看产品名与版本号",
            "设置页新增「退出程序」入口，筛选任务运行中会拒绝退出以免中断任务",
        ],
    },
    {
        "version": "ver1.0.0",
        "channel": "Beta",
        "date": "2026-11-16",
        "title": "Beta 1.0.0：安全加固与筛选口径更新",
        "entries": [
            "本地双引擎：关键词离线秒筛（提及即视为具备，核心技能全覆盖不再被压到 36 分）+ AI 语义评估，支持分级漏斗降本",
            "批量导入 doc / docx / pdf / txt，按内容哈希去重，自动识别姓名、手机与邮箱；同一人多版本自动聚合",
            "模板可视化编辑与批量导入导出，一次勾选多个模板后台筛选；全文搜索、图表视图与 CSV / xlsx 报告导出",
            "简历库新增「建议复核」：同一模板下两引擎分差 ≥ 20 分时行内标注，详情页给出双方理由，便于区分漏判与推断过头",
            "AI 地址仅允许 HTTPS 与官方域名（自定义地址必须是公网端点），实际请求前复查 DNS 并禁止重定向；修改地址必须重填 Key",
            "页面禁止被 iframe 嵌入（XFO + CSP frame-ancestors），会话密钥改为随机持久化，写请求校验同源来源",
            "启动裁剪历史前强制生成独立 pre-trim 快照，快照失败不删除任何记录；恢复备份时先备份现存原件再覆盖",
            "导出 CSV / xlsx 统一转义 = + - @ 等公式前缀；清除筛选历史先存档 JSON 快照（保留 30 天），任务运行中拒绝清除",
            "简历库列表由三次全表扫描合并为一次并补 created_at 索引，3 万条筛选记录下列表约 0.14 秒返回",
            "数据目录与安装目录分离，支持一致性备份、每日自动备份与恢复；删除移入 .trash 暂存 30 天可找回",
            "设置页新增「退出程序」入口：打包后不必再用任务管理器结束进程，筛选任务运行时会拒绝退出",
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


def stored_resume_path(raw: str) -> Path | None:
    """解析数据库中的路径；只接受数据目录 uploads/ 内的文件.

    数据库可能来自备份包，属于不可信输入：绝对路径、`..` 穿越都必须最终落在
    UPLOAD_DIR 之内，否则一律视为不可用，避免被用来读走本机任意文件。
    """
    text = str(raw or "").strip()
    if not text:
        return None
    path = Path(text)
    if not path.is_absolute():
        path = DATA_DIR / path
    try:
        resolved = path.resolve()
        upload_root = UPLOAD_DIR.resolve()
    except OSError:
        return None
    return resolved if resolved.is_relative_to(upload_root) else None


def normalize_upload_paths(conn) -> int:
    """把数据目录内的绝对路径改为 uploads/... 相对路径，便于换机和恢复.

    只改写真正落在本机 UPLOAD_DIR 内的路径；外部目录里的 `.../uploads/...`
    不再被误改写（那会导致预览 404，甚至指向另一份同名文件）。
    """
    changed = 0
    try:
        root = UPLOAD_DIR.resolve()
    except OSError:
        return 0
    rows = conn.execute("SELECT id, path FROM resumes WHERE path IS NOT NULL").fetchall()
    for rid, raw_path in rows:
        raw = str(raw_path or "")
        path = Path(raw)
        if not path.is_absolute():
            continue
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(root):
            continue
        stored = Path("uploads", resolved.relative_to(root)).as_posix()
        conn.execute("UPDATE resumes SET path=? WHERE id=?", (stored, rid))
        changed += 1
    if changed:
        conn.commit()
    return changed


def sanitize_resume_paths(conn) -> int:
    """恢复后净化路径：只保留数据目录 uploads/ 内的文件，其余清空.

    备份包里的数据库可能被人为写入 `C:\\Windows\\...` 之类的路径，
    恢复后若原样留着，详情页与下载接口就会成为任意文件读取入口。
    """
    changed = 0
    rows = conn.execute("SELECT id, path FROM resumes WHERE path IS NOT NULL").fetchall()
    for rid, raw in rows:
        if str(raw or "").strip() and stored_resume_path(raw) is None:
            conn.execute("UPDATE resumes SET path='' WHERE id=?", (rid,))
            changed += 1
    if changed:
        conn.commit()
    return changed


def adopt_legacy_files(conn) -> int:
    """把数据目录内、但不在 uploads/ 的原文件复制进 uploads 并改为相对路径.

    历史记录可能直接引用项目目录里的 samples/ 文件；收紧路径信任后这些记录
    会失去预览。这里只处理确实位于 DATA_DIR 内且存在的文件，不碰用户目录之外。
    """
    changed = 0
    try:
        data_root = DATA_DIR.resolve()
        upload_root = UPLOAD_DIR.resolve()
    except OSError:
        return 0
    rows = conn.execute("SELECT id, path FROM resumes WHERE path IS NOT NULL").fetchall()
    for rid, raw in rows:
        text = str(raw or "").strip()
        if not text:
            continue
        path = Path(text)
        if not path.is_absolute():
            path = DATA_DIR / path
        try:
            resolved = path.resolve()
        except OSError:
            continue
        if resolved.is_relative_to(upload_root) or not resolved.is_relative_to(data_root):
            continue
        if not resolved.is_file():
            continue
        dest_dir = upload_root / "legacy"
        try:
            dest_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            continue
        dest = dest_dir / resolved.name
        try:
            if not dest.exists():
                shutil.copy2(resolved, dest)
            elif dest.stat().st_size != resolved.stat().st_size:
                n = 2
                while True:
                    candidate = dest_dir / f"{resolved.stem}-{n}{resolved.suffix}"
                    if not candidate.exists():
                        shutil.copy2(resolved, candidate)
                        dest = candidate
                        break
                    n += 1
        except OSError as exc:
            app.logger.warning("迁移历史原文件失败 %s：%s", resolved, exc)
            continue
        conn.execute("UPDATE resumes SET path=? WHERE id=?",
                     (Path("uploads", "legacy", dest.name).as_posix(), rid))
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


def list_deleted_templates() -> list[dict]:
    """暂存区里可恢复的模板（`.trash/templates-*`），供模板管理页展示.

    删除模板不直接 unlink：误删一个做了很久的岗位模板，重做成本很高。
    """
    items: list[dict] = []
    try:
        folders = sorted(TRASH_DIR.glob("templates-*"), reverse=True)
    except OSError:
        return items
    for folder in folders:
        if not folder.is_dir():
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            items.append({
                "slug": path.stem,
                "name": path.name,
                "folder": folder.name,
                "deleted_at": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M"),
            })
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


def latest_map(conn, template: str | None = None,
               snapshot: dict | None = None) -> dict[int, dict[str, dict]]:
    """每份简历每个引擎的最新一次筛选结果（可选按模板过滤）."""
    if snapshot is not None:
        if not template:
            return snapshot["latest"]
        out: dict[int, dict[str, dict]] = {}
        for rid, per in snapshot["groups"].items():
            engines = per.get(template)
            if engines:
                out[rid] = engines
        return out
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
    return bool(re.fullmatch(
        r"resume-tool-(?:auto|manual|pre-restore|pre-trim)-\d{8}-\d{6}(?:-\d+)?\.zip",
        name or ""))


def list_backups() -> list[dict]:
    if not BACKUP_DIR.is_dir():
        return []
    items = []
    for path in BACKUP_DIR.glob("*.zip"):
        if not _safe_backup_name(path.name):
            continue
        stat = path.stat()
        kind = ("自动" if "-auto-" in path.name else
                "恢复前" if "-pre-restore-" in path.name else
                "裁剪前" if "-pre-trim-" in path.name else "手动")
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
    """自动备份保留 7 份、恢复前/裁剪前各 3 份、手动备份 20 份."""
    groups = {"auto": 7, "pre-restore": 3, "pre-trim": 3, "manual": 20}
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


def trim_history_with_backup() -> tuple[int, str]:
    """启动裁剪前先做独立快照；快照失败直接抛错，不删除任何历史.

    不能依赖每日自动备份：它可能已被关闭，或当天早已生成；这里仅在确实
    存在超限记录时创建 pre-trim 备份，保证被裁掉的记录始终可回滚。
    """
    if not Path(DB).is_file():
        return 0, ""
    conn = rt.get_conn(DB)
    try:
        needed = rt.history_trim_needed(conn)
    finally:
        conn.close()
    if not needed:
        return 0, ""

    archive = create_backup("pre-trim", include_uploads=False)
    conn = rt.get_conn(DB)
    try:
        trimmed = rt.trim_all_history(conn)
    finally:
        conn.close()
    return trimmed, archive.name


def backup_file(name: str) -> Path | None:
    if not _safe_backup_name(name):
        return None
    path = (BACKUP_DIR / name).resolve()
    return path if path.parent == BACKUP_DIR.resolve() and path.is_file() else None


def _validate_restore_db(path: Path) -> None:
    """校验备份中的数据库：可读、完整、具备必需表；异常统一转为可展示的错误."""
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"备份中的数据库无法打开：{exc}") from exc
    try:
        check = conn.execute("PRAGMA integrity_check").fetchone()
        if not check or check[0] != "ok":
            raise ValueError("备份中的数据库完整性检查未通过")
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
        missing = {"resumes", "persons", "screenings"} - tables
        if missing:
            raise ValueError(f"备份不是有效的简历库，缺少表：{', '.join(sorted(missing))}")
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"备份中的数据库无法读取或已损坏：{exc}") from exc
    finally:
        conn.close()


def restore_backup(archive: Path) -> dict:
    """从备份 ZIP/DB 恢复：先校验并落到临时库，成功后再原子替换.

    全程不在现库上直接写：任何一步失败，当前数据库都保持原样；只有最后的
    `os.replace` 是原子动作。配置与原始文件在替换前准备就绪，失败只记录警告。
    """
    with JOBS_LOCK:
        active = any(j.get("state") in ("queued", "running") for j in JOBS.values())
    if active:
        raise ValueError("仍有筛选任务在运行，请等待完成或重启应用后再恢复")

    db_path = Path(DB)
    staging = db_path.with_name(db_path.name + ".restore-tmp")
    # 暂存区放在 uploads 的同级（同盘 rename，且不在 uploads 内，不会被备份/孤儿扫描误收）
    stage_dir = UPLOAD_DIR.parent / RESTORE_STAGE_NAME
    warnings: list[str] = []
    restored_uploads = 0
    upload_failed = 0
    pre = None
    swapped = False

    try:
        with tempfile.TemporaryDirectory(prefix="resume-tool-restore-") as td:
            temp = Path(td)
            db_file = archive
            config_entries: dict[str, bytes] = {}
            upload_entries: list[tuple[str, zipfile.ZipInfo]] = []

            if archive.suffix.lower() == ".zip":
                with zipfile.ZipFile(archive) as zf:
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
                    declared_uploads = 0
                    for name, item in names.items():
                        if name in allowed_config:
                            if item.file_size <= 5 * 1024 * 1024:
                                config_entries[name] = zf.read(item)
                        elif name.startswith("config/templates/") and name.endswith(".json"):
                            slug = name[len("config/templates/"):-len(".json")]
                            if re.fullmatch(r"[a-z0-9][a-z0-9-]*", slug) \
                                    and item.file_size <= 5 * 1024 * 1024:
                                config_entries[name] = zf.read(item)
                        elif name.startswith("uploads/") and not name.endswith("/"):
                            rel = name[len("uploads/"):]
                            if not (rel and ".." not in Path(rel).parts
                                    and not Path(rel).is_absolute()):
                                continue
                            if item.file_size > RESTORE_MAX_SINGLE_BYTES:
                                continue  # 单文件超限：跳过，不因此中断整包
                            if len(upload_entries) >= RESTORE_MAX_ENTRIES:
                                raise ValueError(
                                    f"备份包中原始文件超过 {RESTORE_MAX_ENTRIES} 个，"
                                    "已拒绝恢复；当前数据未改动")
                            declared_uploads += item.file_size
                            if declared_uploads > RESTORE_MAX_UPLOAD_BYTES:
                                raise ValueError(
                                    "备份包声明的原始文件总量超过 1GB，已拒绝恢复；"
                                    "当前数据未改动")
                            upload_entries.append((rel, item))

            # 1) 校验通过之前不碰现库
            _validate_restore_db(db_file)

            # 1.5) 原始文件先解压到数据目录内的暂存区，按「实际字节」累计校验。
            #      包头里的声明值可被伪造，只有真实解压量才能挡住压缩炸弹；任何
            #      超限都在触碰 uploads 之前中止，现有原件保持原样。
            if upload_entries:
                shutil.rmtree(stage_dir, ignore_errors=True)
                written = 0
                try:
                    with zipfile.ZipFile(archive) as zf:
                        stage_root = stage_dir.resolve()
                        for rel, info in upload_entries:
                            target = (stage_dir / Path(rel)).resolve()
                            if not target.is_relative_to(stage_root):
                                continue
                            target.parent.mkdir(parents=True, exist_ok=True)
                            with zf.open(info) as src, open(target, "wb") as out:
                                while True:
                                    chunk = src.read(RESTORE_CHUNK_BYTES)
                                    if not chunk:
                                        break
                                    written += len(chunk)
                                    if written > RESTORE_MAX_UPLOAD_BYTES:
                                        raise ValueError(
                                            "备份包解压后的原始文件总量超过 1GB"
                                            "（按实际解压量计），已拒绝恢复；"
                                            "当前数据未改动")
                                    out.write(chunk)
                except (zipfile.BadZipFile, OSError) as exc:
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    raise ValueError(
                        f"备份包中的原始文件无法解压，已取消恢复：{exc}") from exc
                except ValueError:
                    shutil.rmtree(stage_dir, ignore_errors=True)
                    raise

            # 2) 新库先落到同盘临时文件，再在临时库上做路径归一与净化
            if staging.exists():
                staging.unlink()
            db_path.parent.mkdir(parents=True, exist_ok=True)
            UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
            src = sqlite3.connect(db_file)
            dst = None
            try:
                dst = sqlite3.connect(staging)
                src.backup(dst)
            finally:
                if dst is not None:
                    dst.close()
                src.close()
            stage_conn = sqlite3.connect(staging)
            try:
                normalize_upload_paths(stage_conn)
                sanitize_resume_paths(stage_conn)  # 备份包可能携带任意绝对路径
            finally:
                stage_conn.close()

            # 3) 配置先写临时文件并校验，替换留到数据库切换之后
            config_stage: list[tuple[Path, Path]] = []
            for name, raw in config_entries.items():
                try:
                    json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    warnings.append(f"{name} 不是有效 JSON，已跳过")
                    continue
                if name == "config/app_settings.json":
                    dest = APP_SETTINGS_PATH
                elif name == "config/providers.json":
                    dest = PROVIDERS_PATH
                else:
                    dest = template_dest(Path(name).stem)
                    if dest is None:
                        continue
                try:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_name(dest.name + ".restore-tmp")
                    tmp.write_bytes(raw)
                    if name == "config/providers.json":
                        restored_providers = rt.read_providers(tmp)  # 结构不合法就拒绝覆盖
                        for provider_name, spec in restored_providers["providers"].items():
                            # 备份可能来自不可信来源：地址可保留供人工核对，但 Key 必须重新输入。
                            spec["api_key"] = ""
                            restored_url = spec.get("base_url", "")
                            if restored_url:
                                try:
                                    spec["base_url"] = rt.validate_provider_base_url(
                                        restored_url, provider_name)
                                except rt.ProviderConfigError:
                                    spec["base_url"] = ""
                        rt.write_providers(tmp, restored_providers)
                        warnings.append(
                            "AI 配置已恢复，但所有 API Key 已清空；请在设置页核对地址并重新输入")
                except (OSError, ValueError, rt.ProviderConfigError) as exc:
                    warnings.append(f"{name} 未恢复：{exc}")
                    continue
                config_stage.append((tmp, dest))

            # 4) 恢复前快照必须早于任何 uploads 写入：zip 内含现库与现存原件。
            #    若先覆盖再备份，pre-restore 里留下的是「已被备份包覆盖」的文件，
            #    同一备份恢复两次就会把两次之间改动的原件永久吃掉。
            if db_path.is_file():
                try:
                    pre = create_backup("pre-restore", include_uploads=bool(upload_entries))
                except (OSError, ValueError, sqlite3.DatabaseError) as exc:
                    raise ValueError(f"恢复前备份失败，已取消恢复，现有数据未改动：{exc}") from exc

            # 5) 暂存的原始文件落到 uploads（同名覆盖）。这里只做同盘 rename，
            #    不再解压，因此不会因中途异常留下写了一半的原件。
            if upload_entries:
                upload_root = UPLOAD_DIR.resolve()
                for rel, _info in upload_entries:
                    src_path = stage_dir / Path(rel)
                    if not src_path.is_file():
                        continue
                    dest = (UPLOAD_DIR / Path(rel)).resolve()
                    if not dest.is_relative_to(upload_root):
                        continue
                    try:
                        dest.parent.mkdir(parents=True, exist_ok=True)
                        os.replace(src_path, dest)
                        restored_uploads += 1
                    except OSError as exc:
                        upload_failed += 1
                        app.logger.warning("恢复原文件失败 %s：%s", rel, exc)

            # 6) 关连接 + 去掉旁路文件，最后一步原子替换
            conn = g.pop("db_conn", None)
            if conn is not None:
                conn.close()
            rt._MIGRATED_DBS.clear()
            rt._FTS_STATE.clear()
            for suffix in ("-wal", "-shm"):
                side = db_path.with_name(db_path.name + suffix)
                try:
                    if side.exists():
                        side.unlink()
                except OSError as exc:
                    app.logger.warning("清理 %s 失败：%s", side, exc)
            os.replace(staging, DB)
            swapped = True

            # 7) 数据库已切换，再落配置；配置失败不回滚数据库，但要说清楚
            for tmp, dest in config_stage:
                try:
                    os.replace(tmp, dest)
                except OSError as exc:
                    warnings.append(f"{dest.name} 未恢复：{exc}")
    finally:
        if not swapped and staging.exists():
            try:
                staging.unlink()
            except OSError:
                pass
        # 暂存区失败必清；成功时文件已被 rename 走，剩下的是被跳过的条目
        shutil.rmtree(stage_dir, ignore_errors=True)

    if upload_failed:
        warnings.append(f"{upload_failed} 个原始文件未能恢复，详情见运行日志")
    return {
        "pre_backup": pre.name if pre else "",
        "uploads": restored_uploads,
        "warnings": warnings,
    }

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
    """登记新任务；已有排队/运行中的任务时拒绝，避免并发写库与重复计费."""
    jid = uuid.uuid4().hex[:10]
    with JOBS_LOCK:
        if any(j.get("state") in ("queued", "running") for j in JOBS.values()):
            raise RuntimeError("已有筛选任务在运行，请等待完成或重启应用后再试")
        if len(JOBS) >= JOB_KEEP:  # 只保留最近任务，避免内存无限增长
            finished = [k for k, j in JOBS.items()
                        if j.get("state") not in ("queued", "running")]
            excess = len(JOBS) - JOB_KEEP + 1
            for old in sorted(finished, key=lambda k: JOBS[k]["started"])[:excess]:
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
        job = JOBS.get(jid)
        return copy.deepcopy(job) if job is not None else None


def active_job() -> dict | None:
    """当前排队/运行中的筛选任务；用于删除类操作的前置互斥检查."""
    with JOBS_LOCK:
        for job in JOBS.values():
            if job.get("state") in ("queued", "running"):
                return dict(job)
    return None


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


def start_job(engine: str, slugs: list[str], ids: list[int] | None, scope: str) -> str | None:
    """启动后台筛选；已有任务在运行时返回 None，由调用方提示用户."""
    try:
        jid = job_new(engine, slugs, scope)
    except RuntimeError as exc:
        app.logger.warning("拒绝重复筛选任务：%s", exc)
        return None
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


REVIEW_GAP = 20.0
"""双引擎分差达到该值即标注「建议复核」。

关键词分衡量技能覆盖率（有分母、缺项归零），AI 分衡量整体合格度
（无分母、通常压缩在 68-92）。两者是两把尺子，算术平均会把真实缺项
稀释成「中等」——例如覆盖率 52、AI 75 平均成 63.5 就会掩盖缺 35% 核心技能。
所以这里只做分歧提示，不合并分数。
"""


def diff_flag(engines: dict, gap: float = REVIEW_GAP) -> dict | None:
    """判断同一模板下双引擎结果的分歧程度；缺任一引擎或分差不足返回 None.

    调用方必须保证两个结果来自同一模板——跨模板比较（如 A 模板的关键词分
    对上 B 模板的 AI 分）会把口径差异和岗位差异混在一起，得到的提示没有意义。
    """
    kw, ai = engines.get("keyword"), engines.get("ai")
    if not kw or not ai:
        return None
    if kw.get("template") and ai.get("template") and kw["template"] != ai["template"]:
        return None
    try:
        k, a = float(kw["score"]), float(ai["score"])
    except (KeyError, TypeError, ValueError):
        return None
    if a - k >= gap:
        return {"gap": round(a - k, 1), "higher": "ai"}
    if k - a >= gap:
        return {"gap": round(k - a, 1), "higher": "keyword"}
    return None


def screening_snapshot(conn) -> dict[str, dict]:
    """一次全表扫描，同时产出「按(简历,模板)分组」与「每引擎最新」两份视图.

    列表页原先分别调用 latest_map / template_groups / divergence_map，同一张
    screenings 表被完整扫三遍（3 万条时约 0.5 s）。这里只扫一次，三个派生物都
    取自同一份快照，排序语义保持 `ORDER BY created_at, id`——后写覆盖先写，等价
    于「按时间取最新」，也避免两次扫描之间任务写入导致口径不一致。
    """
    groups: dict[int, dict[str, dict[str, dict]]] = {}
    latest: dict[int, dict[str, dict]] = {}
    for row in conn.execute("SELECT * FROM screenings ORDER BY created_at, id").fetchall():
        item = dict(row)
        rid = item["resume_id"]
        groups.setdefault(rid, {}).setdefault(item["template"], {})[item["engine"]] = item
        latest.setdefault(rid, {})[item["engine"]] = item
    return {"groups": groups, "latest": latest}


def template_groups(conn, snapshot: dict | None = None) -> dict[int, dict[str, dict[str, dict]]]:
    """{简历ID: {模板名: {引擎: 该模板下最新一次结果}}}.

    列表页在「全部模板」视图下必须按模板成对取数：关键词分来自 A 模板、
    AI 分来自 B 模板，会把岗位差异混进口径差异，行内两列也就失去可比性。
    """
    if snapshot is not None:
        return snapshot["groups"]
    out: dict[int, dict[str, dict[str, dict]]] = {}
    for row in conn.execute("SELECT * FROM screenings ORDER BY created_at, id").fetchall():
        out.setdefault(row["resume_id"], {}).setdefault(row["template"], {})[row["engine"]] = dict(row)
    return out


def _newest_template(per: dict[str, dict[str, dict]]) -> str | None:
    """多个模板都有结果时，取最近一条筛选记录所属的模板作为展示口径."""
    best_tpl, best_id = None, -1
    for tpl_key, engines in per.items():
        for result in engines.values():
            if result["id"] > best_id:
                best_id, best_tpl = result["id"], tpl_key
    return best_tpl


def _display_template(per: dict[str, dict[str, dict]], flag: dict | None) -> str | None:
    """「全部模板」视图下这份简历该展示哪个模板的结果.

    优先级：分歧所在模板（保证行内数字与气泡一致）→ 双引擎齐全的模板（信息最完整）
    → 最近一次筛选的模板。只按「最新」取会踩坑：批量重算多个模板后，恰好最后
    跑的那个模板会覆盖全部行，后端简历显示成测试岗的分数。
    """
    if flag and flag.get("template"):
        return flag["template"]
    both = [t for t, e in per.items() if "keyword" in e and "ai" in e]
    if both:
        return max(both, key=lambda t: max(x["id"] for x in per[t].values()))
    return _newest_template(per)


def divergence_map(conn, template: str | None = None,
                   snapshot: dict | None = None) -> dict[int, dict]:
    """每份简历最严重的一次双引擎分歧，按 (简历, 模板) 分组后各取最新一次.

    列表页「建议复核」用它：先按模板分组，再比同一模板下的两个引擎，
    避免「全部模板」视图把 A 模板的关键词分和 B 模板的 AI 分凑成一对。
    """
    out: dict[int, dict] = {}
    for rid, per in template_groups(conn, snapshot=snapshot).items():
        for tpl_key, engines in per.items():
            if template and tpl_key != template:
                continue
            flag = diff_flag(engines)
            if flag and flag["gap"] > out.get(rid, {}).get("gap", -1):
                # 带上产生分歧的那一对分数：「全部模板」视图下行内两列可能来自别的模板
                out[rid] = {**flag, "template": tpl_key,
                            "kw_score": engines["keyword"]["score"],
                            "ai_score": engines["ai"]["score"]}
    return out


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
    # 一次全表扫描，供「每引擎最新」「按模板分组」「分歧判定」共用
    snapshot = screening_snapshot(conn)
    # 每份简历每引擎的最新结果：统计口径（不区分展示模板）
    latest = latest_map(conn, template=tpl_label, snapshot=snapshot)
    # 展示与分歧判定按 (简历, 模板) 成对取数，避免两列来自不同模板
    groups = snapshot["groups"]
    flags = divergence_map(conn, template=tpl_label, snapshot=snapshot)

    # 先取「范围内全部简历 ID」（搜索命中按相关度排序，否则按导入倒序）
    if q:
        ids = rt.search_ids(conn, q)
    else:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM resumes WHERE deleted_at IS NULL ORDER BY id DESC").fetchall()]

    # 双引擎分歧清单：计数基于全量匹配结果，「只看建议复核」只收窄列表不动统计
    review_ids = [rid for rid in ids if rid in flags]
    all_ids = ids
    review_only = request.args.get("review") == "1"
    if review_only:
        ids = review_ids

    per_page = 15
    total = len(ids)
    pages = max(1, (total + per_page - 1) // per_page)
    page = request.args.get("page", 1, type=int) or 1
    page = min(max(1, page), pages)
    page_ids = ids[(page - 1) * per_page: page * per_page]

    # 统计基于全量匹配结果（不只看当前页，也不受「只看建议复核」过滤影响），模板口径可选
    stats = {"total": len(all_ids), "通过": 0, "待核实": 0, "未通过": 0, "未筛选": 0}
    for rid in all_ids:
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
            per = groups.get(rid, {})
            if tpl_label:
                per = {tpl_label: per[tpl_label]} if tpl_label in per else {}
            # 展示口径：优先分歧模板，其次双引擎齐全的模板（详见 _display_template）
            picked = _display_template(per, flags.get(rid))
            engines = per.get(picked, {})
            item = dict(r) | {"keyword": engines.get("keyword"), "ai": engines.get("ai"),
                              "best_template": picked,
                              "review": flags.get(rid),
                              "snippet": rt.make_snippet(r["text"], q) if q else ""}
            item.pop("text", None)  # 列表页不传全文
            resumes.append(item)

    trash_count = conn.execute(
        "SELECT COUNT(*) AS n FROM resumes WHERE deleted_at IS NOT NULL").fetchone()["n"]
    return render_template("library.html", resumes=resumes, stats=stats,
                           templates=templates, tpl_filter=tpl_filter,
                           tpl_label=tpl_label, trash_count=trash_count,
                           q=q, matched=len(ids),
                           review_only=review_only, review_count=len(review_ids),
                           review_gap=REVIEW_GAP,
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
        part = dest.with_name(dest.name + ".partial")
        try:
            shutil.copy2(p, part)
            os.replace(part, dest)  # 先写 .partial 再原子改名，避免被并发清理误删
            result, detail, _ = rt.import_one_detail(
                conn, dest, store_base=UPLOAD_DIR)
            if result in ("dup", "empty"):
                dest.unlink(missing_ok=True)  # 未产生新引用，不保留重复副本
        except Exception as exc:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
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
        part = dest.with_name(dest.name + ".partial")
        try:
            f.save(part)
            os.replace(part, dest)  # 原子落盘：避免半截文件被当成孤立文件
            result, detail, _ = rt.import_one_detail(conn, dest, store_base=UPLOAD_DIR)
            if result in ("dup", "empty"):
                dest.unlink(missing_ok=True)  # 与目录导入一致，未引用不留副本
        except Exception as exc:
            try:
                part.unlink(missing_ok=True)
            except OSError:
                pass
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

    # 双引擎分歧：按模板分组取各自最新一次，供详情页解释「为什么建议复核」
    review_flags = []
    by_tpl: dict[str, dict] = {}
    for s in conn.execute(
        "SELECT * FROM screenings WHERE resume_id=? ORDER BY created_at, id", (rid,)
    ).fetchall():
        by_tpl.setdefault(s["template"], {})[s["engine"]] = dict(s)
    for tpl_key, engines in by_tpl.items():
        flag = diff_flag(engines)
        if flag:
            kw_detail = json.loads(engines["keyword"]["detail"])
            ai_detail = json.loads(engines["ai"]["detail"])
            review_flags.append({
                "template": tpl_key, **flag,
                "keyword": engines["keyword"]["score"],
                "ai": engines["ai"]["score"],
                "kw_reason": kw_detail.get("reason", ""),
                "ai_reason": ai_detail.get("reason", ""),
            })
    review_flags.sort(key=lambda x: -x["gap"])

    src = stored_resume_path(row["path"])
    ext = (src.suffix if src else Path(row["filename"] or "").suffix).lower()
    file_info = {
        "exists": bool(src and src.is_file()),
        "path": str(src) if src else "",
        "ext": ext,
        # 浏览器能内嵌渲染的类型：PDF 原生，纯文本直接显示
        "inline": ext == ".pdf" or ext in {".txt", ".md", ".markdown"},
        "size_kb": round(src.stat().st_size / 1024, 1) if src and src.is_file() else 0,
    }
    return render_template("resume_detail.html", r=row, history=history,
                           file_info=file_info, deleted=bool(row["deleted_at"]),
                           review_flags=review_flags,
                           review_gap=REVIEW_GAP,
                           score_ruleset=rt.SCORE_RULESET,
                           trash_days=TRASH_RETENTION_DAYS,
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
    """原文件字节流：浏览器预览用 inline，?download=1 时强制下载.

    只允许在库（未进回收站）且路径在数据目录内的简历，避免回收站数据被继续下载。
    """
    conn = db()
    row = conn.execute("SELECT filename, path FROM resumes"
                       " WHERE id=? AND deleted_at IS NULL", (rid,)).fetchone()
    if row is None:
        abort(404)
    src = stored_resume_path(row["path"])
    if src is None or not src.is_file():
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
    row = conn.execute("SELECT filename, path FROM resumes"
                       " WHERE id=? AND deleted_at IS NULL", (rid,)).fetchone()
    if row is None:
        abort(404)
    src = stored_resume_path(row["path"])
    if src is None or not src.is_file():
        flash("原文件不可用（可能已移动、删除，或不在数据目录内）", "error")
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


def _same_origin_back(fallback: str) -> str:
    """操作完成后回到来源页，保留搜索词、模板筛选与页码.

    只接受同源 Referer 的 path+query，不跟随外部站点，避免开放重定向。
    """
    ref = request.referrer or ""
    if ref:
        parsed = urlsplit(ref)
        if parsed.netloc == request.host:
            target = parsed.path + (f"?{parsed.query}" if parsed.query else "")
            if target.startswith("/") and not target.startswith("//"):
                return target
    return fallback


@app.post("/resume/<int:rid>/delete")
def resume_delete(rid: int):
    """软删除：移入回收站，可在 /trash 恢复."""
    conn = db()
    cur = conn.execute("UPDATE resumes SET deleted_at=? WHERE id=? AND deleted_at IS NULL",
                       (rt.now(), rid))
    conn.commit()
    back = _same_origin_back(url_for("library"))
    if cur.rowcount:
        flash(f"已将简历 #{rid} 移入回收站（可在回收站恢复）")
    elif conn.execute("SELECT 1 FROM resumes WHERE id=?", (rid,)).fetchone() is None:
        # 记录已不存在（可能已被彻底删除或另一个标签页处理过），不能提示"已删除"
        flash(f"简历 #{rid} 已不存在，可能已被彻底删除；列表已刷新", "error")
        back = url_for("library")
    else:
        flash(f"简历 #{rid} 已在回收站中，无需重复删除", "error")
    return redirect(back)


@app.post("/resume/<int:rid>/history/clear")
def resume_history_clear(rid: int):
    """清除该简历的筛选历史：先存档快照，再删除（简历本体与原始文件不受影响）.

    只删 screenings，不动 resumes：历史可由重新筛选重建，而简历文本和文件不可重建。
    快照写失败时坚决不删；有任务在跑时直接拒绝，避免把正在写入的结果删掉一半。
    """
    conn = db()
    row = conn.execute("SELECT filename FROM resumes WHERE id=?", (rid,)).fetchone()
    if row is None:
        abort(404)
    back = _same_origin_back(url_for("resume_detail", rid=rid)) + "#history"
    if active_job() is not None:
        flash("仍有筛选任务在运行，历史可能正在写入；请等待任务结束后再清除", "error")
        return redirect(back)
    rows = conn.execute(
        "SELECT * FROM screenings WHERE resume_id=? ORDER BY created_at, id", (rid,)).fetchall()
    if not rows:
        flash(f"#{rid} 本就没有筛选历史，无需清除", "error")
        return redirect(back)
    try:
        snapshot = save_history_snapshot(rid, row["filename"], rows)
    except OSError as exc:
        app.logger.warning("历史快照写入失败，已取消清除：%s", exc)
        flash(f"未能生成历史快照，已取消清除（历史未改动）：{exc}", "error")
        return redirect(back)
    cur = conn.execute("DELETE FROM screenings WHERE resume_id=?", (rid,))
    conn.commit()
    flash(f"已清除 #{rid} 的 {cur.rowcount} 条筛选历史；快照存于 .trash/{snapshot.name}/"
          f"{HISTORY_SNAPSHOT_FILE}（保留 {TRASH_RETENTION_DAYS} 天，简历本体未受影响）")
    return redirect(back)


@app.post("/resume/batch-delete")
def resume_batch_delete():
    ids = [int(x) for x in request.form.getlist("ids") if x.isdigit()]
    back = _same_origin_back(url_for("library"))
    if not ids:
        flash("未选择任何简历", "error")
        return redirect(back)
    conn = db()
    marks = ",".join("?" * len(ids))
    cur = conn.execute(
        f"UPDATE resumes SET deleted_at=? WHERE id IN ({marks}) AND deleted_at IS NULL",
        [rt.now(), *ids])
    conn.commit()
    if cur.rowcount:
        flash(f"已将 {cur.rowcount} 份简历移入回收站（可在回收站恢复）")
    else:
        flash("所选简历已不在库中（可能已在回收站或已被彻底删除）", "error")
    return redirect(back)


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
    """可安全访问的原文件；与 stored_resume_path 共用同一边界."""
    return stored_resume_path(raw)


def human_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.0f} KB"
    if size < 1024 * 1024 * 1024:
        return f"{size / 1024 / 1024:.1f} MB"
    return f"{size / 1024 / 1024 / 1024:.2f} GB"


def _trash_stamp() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _retire_paths(paths: list[Path]) -> tuple[int, int]:
    """把 uploads/ 内的文件移入 .trash/<时间戳>/ 暂存；返回 (已移入, 未处理).

    直接 unlink 一旦误点就无法挽回，先移入暂存区、保留 30 天再清理。
    """
    root = UPLOAD_DIR.resolve()
    try:
        trash_root = TRASH_DIR.resolve()
    except OSError:
        trash_root = TRASH_DIR
    stamp = _trash_stamp()
    moved = failed = 0
    touched_dirs: set[Path] = set()
    for path in paths:
        try:
            rel = path.resolve().relative_to(root)
        except (OSError, ValueError):
            failed += 1
            continue
        if rel.parts and rel.parts[0] == "..":
            failed += 1
            continue
        dest = trash_root / stamp / rel
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                dest = dest.with_name(f"{dest.stem}-{uuid.uuid4().hex[:6]}{dest.suffix}")
            shutil.move(str(path), str(dest))
            moved += 1
            touched_dirs.add(path.parent)
        except OSError as exc:
            app.logger.warning("移入暂存区失败 %s：%s", path, exc)
            failed += 1
    # 清掉空的批次目录，避免 uploads/ 里堆大量空文件夹
    for directory in touched_dirs:
        try:
            if directory != root and directory.is_dir() and not any(directory.iterdir()):
                directory.rmdir()
        except OSError:
            pass
    return moved, failed


def _delete_upload_files(raw_paths: list[str], keep_paths: set[str] | None = None) -> tuple[int, int]:
    """把数据目录内的原始文件移入暂存区；返回 (已移入, 未处理).

    外部目录导入的历史文件不会被擅自移动，只计入未处理，由用户自行决定。
    """
    keep_paths = keep_paths or set()
    targets: list[Path] = []
    kept = 0
    for raw in raw_paths:
        if raw in keep_paths:
            kept += 1
            continue
        path = resume_file_on_disk(raw)
        if path is None:
            if str(raw or "").strip():
                kept += 1
            continue
        if not path.is_file():
            kept += 1
            continue
        targets.append(path)
    moved, failed = _retire_paths(targets)
    return moved, kept + failed


def purge_old_trash(days: int = TRASH_RETENTION_DAYS) -> tuple[int, int]:
    """清理超过保留期的暂存文件；返回 (删除文件数, 释放字节数)."""
    if not TRASH_DIR.is_dir():
        return 0, 0
    cutoff = time.time() - days * 86400
    removed = freed = 0
    for entry in TRASH_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            for path in entry.rglob("*"):
                if path.is_file():
                    try:
                        freed += path.stat().st_size
                        path.unlink()
                        removed += 1
                    except OSError:
                        continue
            shutil.rmtree(entry, ignore_errors=True)
        except OSError as exc:
            app.logger.warning("清理暂存区失败 %s：%s", entry, exc)
    return removed, freed


HISTORY_SNAPSHOT_FILE = "screenings.json"


def save_history_snapshot(rid: int, filename: str, rows) -> Path:
    """清除历史前，把待删记录写入暂存区快照；写盘失败抛 OSError.

    目录与模板删除的 `.trash/templates-*` 同构，因此沿用 `purge_old_trash` 的
    保留期自动清理。调用方必须先拿到快照、再执行 DELETE——快照失败就放弃删除，
    避免「报告只导最新一条、中间历史删掉即无从恢复」。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = TRASH_DIR / f"history-{rid}-{stamp}"
    n = 2
    while target.exists():
        target = TRASH_DIR / f"history-{rid}-{stamp}-{n}"
        n += 1
    target.mkdir(parents=True)
    payload = {
        "resume_id": rid,
        "filename": filename,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "count": len(rows),
        "screenings": [dict(r) for r in rows],
    }
    path = target / HISTORY_SNAPSHOT_FILE
    tmp = path.with_name(path.name + ".tmp")
    try:
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        shutil.rmtree(target, ignore_errors=True)
        raise
    return target


def _purge_message(filename: str, moved: int, kept: int) -> str:
    if moved and kept:
        return (f"已彻底删除「{filename}」；{moved} 个原始文件移入暂存区 .trash"
                f"（{TRASH_RETENTION_DAYS} 天内可恢复），{kept} 个不在数据目录，未处理")
    if moved:
        return (f"已彻底删除「{filename}」；原始文件移入暂存区 .trash"
                f"（{TRASH_RETENTION_DAYS} 天内可恢复）")
    if kept:
        return f"已彻底删除「{filename}」及其筛选记录；原始文件不在数据目录，未自动处理"
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
    cutoff = time.time() - 600  # 正在导入的文件可能尚未写库，10 分钟内一律跳过
    if UPLOAD_DIR.is_dir():
        for path in UPLOAD_DIR.rglob("*"):
            if not path.is_file():
                continue
            if path.name.endswith(".partial"):
                continue
            try:
                resolved = path.resolve()
                if resolved in referenced:
                    continue
                stat = path.stat()
                if stat.st_mtime >= cutoff:
                    continue
                total += stat.st_size
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
    moved, kept = _delete_upload_files(paths, keep_paths=remaining)
    msg = f"回收站已清空，彻底删除 {len(ids)} 份简历"
    if moved:
        msg += (f"；{moved} 个原始文件移入暂存区 .trash"
                f"（{TRASH_RETENTION_DAYS} 天内可在数据目录恢复）")
    if kept:
        msg += f"；{kept} 个原文件不在数据目录，未自动处理"
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
        f" WHERE r.id IN ({marks}) AND r.deleted_at IS NULL", ids).fetchall()}
    if not rows:
        flash("所选简历不存在或已移入回收站", "error")
        return redirect(url_for("library"))
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
        line = [
            rid, r["person_name"] or "", r["phone"] or "", r["email"] or "",
            r["filename"], r["versions"] or 1, best["template"] if best else "",
            kw["status"] if kw else "", kw["score"] if kw else "",
            ai["status"] if ai else "", ai["score"] if ai else "",
            skills,
            json.loads(best["detail"]).get("reason", "") if best else "",
            (r["imported_at"] or "").replace("T", " "),
        ]
        # 文件名/AI 理由都是外部输入：以 = + - @ 开头会被 Excel 当公式执行
        ws.append([rt.sanitize_spreadsheet_cell(v) for v in line])
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
        summary.append([k, rt.sanitize_spreadsheet_cell(v)])
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
    if jid is None:
        flash("已有筛选任务正在进行，请等待完成后再提交，避免重复调用与并发写库", "error")
        return redirect(url_for("screen", templates=",".join(slugs)))
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
                           favorites=get_favorites(),
                           deleted_templates=list_deleted_templates(),
                           trash_days=TRASH_RETENTION_DAYS, active="templates")


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


@app.post("/templates/<slug>/delete")
def template_delete(slug: str):
    """删除模板：移入 `.trash/templates-<时间戳>/`，30 天内可在模板管理页恢复."""
    path = template_path(slug)
    if path is None:
        flash("模板不存在或已被删除", "error")
        return redirect(url_for("templates_page"))

    dest_dir = TRASH_DIR / f"templates-{datetime.now():%Y%m%d-%H%M%S}"
    try:
        dest_dir.mkdir(parents=True, exist_ok=True)
        shutil.move(str(path), str(dest_dir / path.name))
    except OSError as exc:
        app.logger.warning("删除模板失败 %s：%s", path, exc)
        flash(f"删除失败：{exc}", "error")
        return redirect(url_for("templates_page"))

    # 收藏与筛选中心的记忆里若还留着这个 slug，会指向已不存在的模板
    cfg = load_app_settings()
    changed = False
    for key in ("favorites", "screen_templates"):
        values = cfg.get(key)
        if isinstance(values, list) and slug in values:
            cfg[key] = [s for s in values if s != slug]
            changed = True
    if changed:
        save_app_settings(cfg)

    app.logger.info("删除模板 %s（移入 %s）", slug, dest_dir.name)
    flash(f"已删除模板「{slug}」；历史筛选记录保留，但不再按该模板查看。"
          f"如需找回，在下方「最近删除」里恢复（{TRASH_RETENTION_DAYS} 天内有效）")
    return redirect(url_for("templates_page"))


@app.post("/templates/<slug>/restore")
def template_restore(slug: str):
    """从暂存区恢复被删除的模板；不覆盖同名现有模板."""
    folder = request.form.get("folder", "")
    if not re.fullmatch(r"templates-\d{8}-\d{6}", folder or ""):
        flash("恢复参数无效", "error")
        return redirect(url_for("templates_page"))
    dest = template_dest(slug)
    if dest is None:
        flash("模板文件名只允许小写字母/数字/连字符", "error")
        return redirect(url_for("templates_page"))
    src = TRASH_DIR / folder / f"{slug}.json"
    if not src.is_file():
        flash("该模板已不在暂存区，可能已被自动清理", "error")
        return redirect(url_for("templates_page"))
    if dest.exists():
        flash(f"{slug}.json 已存在，未覆盖；可先重命名现有模板再恢复", "error")
        return redirect(url_for("templates_page"))
    try:
        shutil.move(str(src), str(dest))
    except OSError as exc:
        app.logger.warning("恢复模板失败 %s：%s", src, exc)
        flash(f"恢复失败：{exc}", "error")
        return redirect(url_for("templates_page"))
    flash(f"已恢复模板「{slug}」")
    return redirect(url_for("templates_page"))


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
                           desktop_mode=SHUTDOWN_HOOK is not None,
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
                # 日志先脱敏再入包：历史日志里可能留着搜索词等个人信息
                zf.writestr(f"logs/{path.name}",
                            redact_log_text(path.read_text(encoding="utf-8", errors="replace")))
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
    """把 uploads/ 中没有任何简历引用的文件移入暂存区（重复导入副本、失败上传残留）."""
    conn = db()
    orphans, total = find_orphan_uploads(conn)
    if not orphans:
        flash("没有需要清理的孤立文件")
        return redirect(url_for("settings", _anchor="diagnostics"))
    moved, failed = _retire_paths(orphans)
    app.logger.info("孤立文件移入暂存区 %s 个（约 %s 字节）", moved, total)
    msg = f"已将 {moved} 个孤立文件（约 {human_size(total)}）移入暂存区 .trash"
    msg += f"；{TRASH_RETENTION_DAYS} 天后自动清理，期间可从数据目录恢复"
    if failed:
        msg += f"；{failed} 个文件处理失败，详情见日志"
    flash(msg)
    return redirect(url_for("settings", _anchor="diagnostics"))


@app.post("/settings/backups/<name>/restore")
def settings_backup_restore_existing(name: str):
    path = backup_file(name)
    if path is None:
        abort(404)
    try:
        result = restore_backup(path)
    except (OSError, ValueError, zipfile.BadZipFile, sqlite3.DatabaseError) as exc:
        flash(f"恢复失败，当前数据库未被替换：{exc}", "error")
    else:
        before = (f"恢复前数据备份为 {result['pre_backup']}"
                  if result["pre_backup"] else "目标是空数据目录，未生成恢复前备份")
        msg = f"已从 {name} 恢复；{before}"
        if result["uploads"]:
            msg += f"，同时恢复了 {result['uploads']} 个原始文件"
        if result["warnings"]:
            msg += "；部分内容未恢复：" + "；".join(result["warnings"][:3])
        flash(msg, "message")
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
        flash(f"恢复失败，当前数据库未被替换：{exc}", "error")
    else:
        msg = (f"恢复完成；恢复前数据已备份为 {result['pre_backup']}"
               if result["pre_backup"] else "恢复完成；目标是空数据目录，未生成恢复前备份")
        if result["uploads"]:
            msg += f"，同时恢复了 {result['uploads']} 个原始文件"
        if result["warnings"]:
            msg += "；部分内容未恢复：" + "；".join(result["warnings"][:3])
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
    try:
        for name, spec in data.get("providers", {}).items():
            old_url = spec.get("base_url", "")
            submitted_url = request.form.get(
                f"{name}__base_url", old_url).strip()
            base_url = (rt.validate_provider_base_url(submitted_url, name)
                        if submitted_url else "")
            key = request.form.get(f"{name}__api_key", "").strip()
            if base_url != old_url and spec.get("api_key") and not key:
                raise rt.ProviderConfigError(
                    f"厂商 {name} 修改 Base URL 后必须重新输入 API Key，配置未保存")
            for field in ("label", "model"):
                spec[field] = request.form.get(
                    f"{name}__{field}", spec.get(field, "")).strip()
            spec["base_url"] = base_url
            if key:  # 地址未变时留空才不修改；地址变化已在上方强制重填
                spec["api_key"] = key
    except rt.ProviderConfigError as exc:
        flash(f"检测到不安全的 AI 厂商配置，未保存：{exc}", "error")
        return redirect(url_for("settings"))
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


def has_active_jobs() -> bool:
    """是否有排队中/运行中的筛选任务；关窗与退出入口都以此为准."""
    with JOBS_LOCK:
        return any(j.get("state") in ("queued", "running") for j in JOBS.values())


def _exit_process() -> None:
    """真正终止进程；单独抽成函数，便于测试替身（否则测试会杀掉自己）."""
    try:
        logging.shutdown()  # 先把日志缓冲落盘，再切断进程
    finally:
        os._exit(0)


# 桌面模式下由窗口壳注册：退出改为「关闭窗口」，让关窗逻辑只有一条路径
SHUTDOWN_HOOK = None


def _do_shutdown() -> None:
    """实际执行退出：桌面模式关窗口，浏览器模式直接结束进程."""
    hook = SHUTDOWN_HOOK
    if hook is not None:
        try:
            hook()
            return
        except Exception:
            app.logger.exception("桌面退出回调失败，改为直接结束进程")
    _exit_process()


def schedule_shutdown(delay_s: float = 0.8) -> None:
    """先让「已退出」页面回到界面，再执行退出.

    延迟是必要的：立即退出会让页面拿不到响应，用户看到的是连接被重置，
    分不清是「已退出」还是「程序崩了」。
    """
    timer = threading.Timer(delay_s, _do_shutdown)
    timer.daemon = True
    timer.start()


@app.post("/settings/shutdown")
def settings_shutdown():
    """退出程序：打包后既无控制台也无窗口，用户需要一个显式的退出入口."""
    if has_active_jobs():
        flash("仍有筛选任务在运行，请等待完成后再退出", "error")
        return redirect(url_for("settings", _anchor="shutdown"))
    app.logger.info("收到退出请求，即将退出")
    schedule_shutdown()
    return render_template("shutdown.html", desktop_mode=SHUTDOWN_HOOK is not None)


def _assert_flask_version() -> None:
    """TRUSTED_HOSTS 是 Flask 3.1 才引入的；低于该版本 Host 防护会静默失效."""
    from importlib.metadata import PackageNotFoundError, version as pkg_version
    try:
        raw = pkg_version("flask")
    except PackageNotFoundError as exc:
        raise RuntimeError("未安装 Flask，请先执行 pip install -r requirements.txt") from exc
    nums = [int(n) for n in re.findall(r"\d+", raw)[:2]]
    if len(nums) >= 2 and (nums[0] < 3 or (nums[0] == 3 and nums[1] < 1)):
        raise RuntimeError(
            f"Flask {raw} 版本过低：Host 白名单与同源校验需要 3.1 以上，请升级后再启动")


def pick_listen_port(host: str = "127.0.0.1", preferred: int = 5000,
                     span: int = 20) -> int:
    """挑一个可绑定的回环端口：优先 preferred，被占用则顺延，兜底交给系统分配."""
    for port in range(preferred, preferred + span):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind((host, port))
            except OSError:
                continue
        return port
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((host, 0))
        return int(sock.getsockname()[1])


def main() -> None:
    # 打包后必须最先调用：否则任何子进程都会重新拉起整个应用（任务管理器残留多份）
    multiprocessing.freeze_support()
    configure_logging()
    _assert_flask_version()
    app.logger.info("启动简历工具 %s；数据目录 %s", current_version(), DATA_DIR)
    cfg = load_app_settings()
    try:
        conn = rt.get_conn(DB)
        try:
            normalize_upload_paths(conn)
            adopted = adopt_legacy_files(conn)
            if adopted:
                app.logger.info("已将 %s 份历史原文件复制进数据目录", adopted)
        finally:
            conn.close()
    except (OSError, sqlite3.DatabaseError) as exc:
        app.logger.warning("简历路径迁移失败：%s", exc)
    try:
        purged, freed = purge_old_trash()
        if purged:
            app.logger.info("清理暂存区 %s 个过期文件，释放 %s", purged, human_size(freed))
    except OSError as exc:
        app.logger.warning("清理暂存区失败，不影响启动：%s", exc)
    if cfg.get("auto_check_updates", True):
        start_update_check()
    if cfg.get("auto_backup", True):
        try:
            backup = maybe_auto_backup()
            if backup:
                app.logger.info("已生成每日自动备份：%s", backup.name)
        except (OSError, ValueError, sqlite3.DatabaseError) as exc:
            app.logger.warning("自动备份失败，不影响应用启动：%s", exc)
    try:
        trimmed, pre_trim = trim_history_with_backup()
        if trimmed:
            app.logger.info(
                "筛选历史按每简历 %s 条收敛，清理 %s 条；裁剪前快照 %s",
                rt.HISTORY_WINDOW, trimmed, pre_trim)
    except Exception as exc:  # 启动维护不可阻断应用；备份失败时历史仍未删除
        app.logger.warning(
            "历史裁剪未执行，超限记录已保留（不影响启动）：%s", exc)
    # 单机桌面用途：只绑定回环地址，关闭 reloader（打包 exe 的硬性要求）
    port = pick_listen_port()
    # 端口可能顺延：Host 白名单必须与实际端口一致，否则 Flask 3.1 直接返回 400
    app.config["TRUSTED_HOSTS"] = ["127.0.0.1", "localhost",
                                   f"127.0.0.1:{port}", f"localhost:{port}"]
    url = f"http://127.0.0.1:{port}"

    want_window = _desktop_requested()
    if want_window:
        status = _desktop_preflight()
        if status == "already-running":
            return  # 已激活原窗口，本进程不再启动服务
        if status != "ok":
            want_window = False  # 缺组件：按浏览器模式继续，功能不受影响

    # 服务统一放后台线程：窗口模式下主线程要留给窗口事件循环
    server = threading.Thread(target=_serve_forever, args=(port,),
                              name="http-server", daemon=True)
    server.start()
    if not _wait_http_ready(url):
        app.logger.error("HTTP 服务在 20 秒内未就绪")

    if want_window and _DESKTOP_SHELL is not None:
        if _DESKTOP_SHELL.open_window(url, has_active_jobs, _register_shutdown_hook):
            app.logger.info("窗口已关闭，进程退出")
            return
        # 窗口起不来（WebView2 初始化失败等）：退回浏览器，保证功能可用
        app.logger.warning("桌面窗口不可用，回退到浏览器打开")
        webbrowser.open(url)
    elif getattr(sys, "frozen", False) and not os.environ.get("RESUME_TOOL_NO_BROWSER"):
        # 浏览器模式：无控制台时双击看不到任何提示，启动后拉起默认浏览器
        threading.Timer(1.2, lambda: webbrowser.open(url)).start()

    server.join()


def _desktop_requested() -> bool:
    """是否尝试桌面窗口；--browser 或 RESUME_TOOL_NO_BROWSER 强制回浏览器."""
    if os.environ.get("RESUME_TOOL_NO_BROWSER"):
        return False
    return "--browser" not in sys.argv[1:]


_DESKTOP_SHELL = None


def _desktop_preflight() -> str:
    """窗口模式前置检查，返回 ok / already-running / unavailable.

    任何一项不满足都退回浏览器——宁可界面朴素，也不能双击打不开。
    """
    global _DESKTOP_SHELL
    if sys.platform != "win32":
        return "unavailable"
    try:
        import desktop_shell
    except Exception:
        app.logger.warning("未安装 pywebview，改用浏览器模式", exc_info=True)
        return "unavailable"
    if not desktop_shell.webview2_runtime_installed():
        app.logger.warning("未检测到 WebView2 运行时，改用浏览器模式")
        return "unavailable"
    if not desktop_shell.acquire_single_instance():
        if desktop_shell.activate_existing_window():
            app.logger.info("已有实例在运行，已切换到该窗口")
        else:
            app.logger.warning("已有实例在运行，但未找到它的窗口")
        return "already-running"
    _DESKTOP_SHELL = desktop_shell
    return "ok"


def _register_shutdown_hook(destroy) -> None:
    """桌面模式：退出入口从「杀进程」改为「关闭窗口」，让退出只有一条路径."""
    global SHUTDOWN_HOOK
    SHUTDOWN_HOOK = destroy
    app.logger.info("桌面模式：设置页退出入口已改为关闭窗口")


def _wait_http_ready(url: str, timeout_s: float = 20.0) -> bool:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(url + "/", timeout=1) as resp:
                if resp.status < 500:
                    return True
        except (urllib.error.URLError, OSError):
            time.sleep(0.15)
    return False


def _serve_forever(port: int) -> None:
    try:
        from waitress import serve
    except ImportError:
        app.logger.warning("未安装 waitress，回退到 Flask 开发服务器（不建议分发给办公人员）")
        app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False, threaded=True)
    else:
        app.logger.info("以 waitress 启动：http://127.0.0.1:%s", port)
        serve(app, host="127.0.0.1", port=port, threads=8)


if __name__ == "__main__":
    main()
