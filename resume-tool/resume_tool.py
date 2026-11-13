#!/usr/bin/env python3
"""单机简历筛选工具（双引擎原型）.

定位：单用户本地的「简历批量管理 + 筛选模板 + 双引擎评分」工具。

引擎：
  A. keyword  本地关键词加权评分——离线、秒级、零成本（复用已验证的筛选模型）
  B. ai       OpenAI 兼容协议调用大模型 API——语义理解，多家可切换可自定义
  funnel      分级漏斗：keyword 全量初筛 → ai 只复核「待核实」名单（推荐，控成本）

简历库：SQLite 单文件（标准库 sqlite3）
格式支持：.txt/.md/.docx/.doc/.pdf
  - .doc 采用「伪装检测」策略：市面大量 .doc 实为 docx/RTF/HTML/纯文本，逐一探测；
    真二进制 .doc 走 Word COM 转换（需 pywin32 + 本机安装 Word，仅 Windows）
  - .pdf 需要 pypdf（pip install pypdf），缺失时跳过并告警

用法：
  python resume_tool.py import  --dir samples
  python resume_tool.py list
  python resume_tool.py screen  --engine keyword --template config/template.json
  python resume_tool.py screen  --engine funnel   --template config/template.json
  python resume_tool.py export  --out report.csv
  python resume_tool.py providers
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html as html_mod
import json
import re
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

try:
    from pypdf import PdfReader  # 可选依赖：PDF
except ImportError:
    PdfReader = None

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".docx", ".doc", ".pdf"}
DEFAULT_DB = "resume_tool.db"
DEFAULT_PROVIDERS = Path("config/providers.json")


# ============ 通用 ============

def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


# ============ 文本抽取 ============

_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030")


def _read_plain(path: Path) -> str:
    """按常见编码尝试读取纯文本（中文简历常见 GBK/GB18030）."""
    for enc in _TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return ""


def _extract_docx(path: Path) -> str:
    """标准库解析 docx（zip 内 XML），拼接 <w:t> 文本节点."""
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, KeyError):
        return ""
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))


def _strip_rtf(raw: str) -> str:
    """尽力而为的 RTF 文本提取：\\uN 优先，\\'hh 按 GBK 猜测，其余控制词清除."""
    text = re.sub(r"\\u(-?\d+)\s?\??", lambda m: chr(int(m.group(1)) % 65536), raw)

    def _hex_gb(m: re.Match[str]) -> str:
        try:
            return bytes([int(m.group(1), 16)]).decode("gbk", errors="ignore")
        except Exception:
            return ""

    text = re.sub(r"\\'([0-9a-fA-F]{2})", _hex_gb, text)
    text = re.sub(r"\\[a-zA-Z]+-?\d*\s?", "", text)  # 控制词
    text = text.replace("{", "").replace("}", "")
    return re.sub(r"\s{2,}", " ", text).strip()


def _strip_html(raw: str) -> str:
    text = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw, flags=re.I | re.S)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s{2,}", " ", html_mod.unescape(text)).strip()


def _doc_via_word_com(path: Path) -> str:
    """真二进制 .doc 的兜底：Word COM 转 docx 后解析.

    前提：Windows + pywin32 + 本机安装 Word。【依赖最终用户环境，缺失时返回空串】
    """
    if sys.platform != "win32":
        return ""
    try:
        import win32com.client  # type: ignore[import-not-found]
    except ImportError:
        return ""
    try:
        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        doc = word.Documents.Open(str(path.resolve()), ReadOnly=True)
        tmp = Path(tempfile.mkdtemp()) / (path.stem + ".docx")
        # FileFormat=16 对应 wdFormatDocumentDefault（docx）
        doc.SaveAs2(str(tmp), FileFormat=16)
        doc.Close(False)
        word.Quit()
        return _extract_docx(tmp)
    except Exception:
        return ""


def _looks_like_text(data: bytes) -> bool:
    """启发式：字节流是否大体是可打印文本（处理 .doc 实为 txt 的情况）."""
    if not data:
        return False
    if b"\x00" in data[:4096]:
        return False
    sample = data[:4096]
    for enc in ("utf-8", "gb18030"):
        try:
            decoded = sample.decode(enc)
        except UnicodeDecodeError:
            continue
        printable = sum(ch.isprintable() or ch in "\r\n\t" for ch in decoded)
        return printable / max(len(decoded), 1) > 0.85
    return False


def extract_doc(path: Path) -> str:
    """智能 .doc 处理：先识别伪装格式，真二进制才走 Word COM."""
    data = path.read_bytes()
    if data[:2] == b"PK":  # 实为 docx（zip 魔数）
        return _extract_docx(path)
    head = data[:512].lstrip().lower()
    if head.startswith(b"{\\rtf"):  # 实为 RTF
        return _strip_rtf(data.decode("latin-1", errors="ignore"))
    if head.startswith(b"<!doctype") or head.startswith(b"<html"):  # 实为 HTML
        return _strip_html(data.decode("utf-8", errors="ignore"))
    if _looks_like_text(data):  # 实为纯文本
        return data.decode("gb18030", errors="ignore")
    return _doc_via_word_com(path)  # 真二进制 .doc


def extract_text(path: Path) -> str:
    """按扩展名分发文本抽取；无法解析返回空串（由调用方跳过并告警）."""
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".markdown"):
        return _read_plain(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".doc":
        return extract_doc(path)
    if suffix == ".pdf":
        if PdfReader is None:
            print(f"[警告] 跳过 {path.name}: PDF 支持需要 pip install pypdf", file=sys.stderr)
            return ""
        try:
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:
            print(f"[警告] PDF 解析失败 {path.name}: {exc}", file=sys.stderr)
            return ""
    print(f"[警告] 跳过不支持的格式: {path.name}", file=sys.stderr)
    return ""


# ============ 引擎A：关键词筛选（复用已验证模型） ============

@dataclass(frozen=True)
class SkillRule:
    name: str
    weight: float
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Template:
    name: str
    required_skills: tuple[str, ...]
    min_years: int | None
    strict_years: bool
    core_skills: tuple[SkillRule, ...]
    nice_to_have: tuple[SkillRule, ...]


def load_template(path: Path) -> Template:
    """读取并校验筛选模板 JSON（与 resume-screener 的 requirements.json 同构）."""
    if not path.is_file():
        sys.exit(f"[模板错误] 找不到模板文件: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"[模板错误] JSON 解析失败: {exc}")

    gates = raw.get("hard_gates", {})
    core_raw: dict = raw.get("core_skills", {})
    nice_raw: dict = raw.get("nice_to_have", {})
    if not core_raw:
        sys.exit("[模板错误] core_skills 不能为空")

    def _rules(section: dict) -> tuple[SkillRule, ...]:
        return tuple(
            SkillRule(
                name=name,
                weight=float(spec.get("weight", 1)),
                aliases=tuple(spec.get("aliases", [name])) or (name,),
            )
            for name, spec in section.items()
        )

    required = [str(s) for s in gates.get("required_skills", [])]
    defined = set(core_raw) | set(nice_raw)
    unknown = [s for s in required if s not in defined]
    if unknown:
        sys.exit(f"[模板错误] required_skills 中的 {unknown} 未在 core_skills/nice_to_have 中定义")
    min_years = gates.get("min_years")
    if min_years is not None and (not isinstance(min_years, int) or min_years < 0):
        sys.exit("[模板错误] min_years 必须是非负整数或省略")

    return Template(
        name=str(raw.get("name", path.stem)),
        required_skills=tuple(required),
        min_years=min_years,
        strict_years=bool(gates.get("strict_years", False)),
        core_skills=_rules(core_raw),
        nice_to_have=_rules(nice_raw),
    )


def _count_keyword(text: str, alias: str) -> int:
    """统计别名出现次数；前后不得紧邻字母/数字，防 python 命中 pythonic."""
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)
    return len(pattern.findall(text))


def count_evidence(text: str, rule: SkillRule) -> int:
    return sum(_count_keyword(text, alias) for alias in rule.aliases)


def evidence_to_score(count: int) -> float:
    """证据次数 → 0-5 分：0=未提及 1=仅罗列 2-3=用过 4+=深度实践."""
    if count <= 0:
        return 0.0
    if count == 1:
        return 2.0
    if count <= 3:
        return 4.0
    return 5.0


_YEAR_PATTERNS = (
    re.compile(r"(?<!\d)(\d{1,2})\s*\+?\s*年(?![\d月])"),
    re.compile(r"(?<!\d)(\d{1,2})\s*\+?\s*years?\b", re.IGNORECASE),
)


def extract_years(text: str) -> int | None:
    hits: list[int] = []
    for pattern in _YEAR_PATTERNS:
        hits.extend(int(m) for m in pattern.findall(text))
    hits = [y for y in hits if 1 <= y <= 45]
    return max(hits) if hits else None


def screen_keyword(text: str, tpl: Template) -> dict:
    """引擎A：硬门槛 + 加权评分。返回 {score,status,reason,evidence}."""
    years = extract_years(text)
    all_rules = (*tpl.core_skills, *tpl.nice_to_have)
    evidence = {r.name: count_evidence(text, r) for r in all_rules}

    status, reasons = "通过", []
    missing = [n for n in tpl.required_skills if evidence.get(n, 0) == 0]
    if missing:
        reasons.append(f"缺少必备技能: {', '.join(missing)}")
        status = "未通过"
    if tpl.min_years is not None:
        if years is None:
            if tpl.strict_years:
                reasons.append("未声明工作年限(严格模式)")
                status = "未通过"
            elif status == "通过":
                status = "待核实"
                reasons.append("未声明工作年限，需人工确认")
        elif years < tpl.min_years:
            reasons.append(f"年限不足: {years}年 < 要求{tpl.min_years}年")
            status = "未通过"

    def _weighted(rules: tuple[SkillRule, ...]) -> float:
        total = sum(r.weight for r in rules)
        if total <= 0:
            return 0.0
        return sum(evidence_to_score(evidence[r.name]) / 5.0 * r.weight for r in rules) / total

    score = (
        round(_weighted(tpl.core_skills) * 90 + _weighted(tpl.nice_to_have) * 10, 1)
        if tpl.nice_to_have else round(_weighted(tpl.core_skills) * 100, 1)
    )
    return {
        "score": score,
        "status": status,
        "reason": "；".join(reasons) if reasons else "符合硬性门槛",
        "evidence": {k: v for k, v in evidence.items() if v > 0},
        "years": years,
    }


# ============ 引擎B：AI 筛选（OpenAI 兼容多厂商） ============

AI_SYSTEM_PROMPT = (
    "你是简历初筛助手。根据给定的筛选标准评估简历，只输出一个 JSON 对象，不要输出任何其他内容。\n"
    '输出格式：{"passed": true/false, "score": 0-100整数, "missing": ["缺失的必备项"], '
    '"reason": "50字内的中文判断理由"}\n'
    "评分口径：满足全部必备门槛且核心技能证据充分 80+；部分匹配 50-79；明显不符 <50。"
)


def load_providers(path: Path) -> dict:
    """读取 AI 厂商配置；文件不存在时给出可操作指引."""
    if not path.is_file():
        sys.exit(
            f"[配置错误] 找不到 {path}\n"
            "请创建该文件，示例结构：\n"
            '{"active": "deepseek", "providers": {"deepseek": '
            '{"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", '
            '"api_key": "你的Key", "model": "deepseek-chat"}}}'
        )
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"[配置错误] providers JSON 解析失败: {exc}")


def get_active_provider(providers: dict) -> dict | None:
    """返回已配置 Key 的当前厂商；未配置返回 None."""
    name = providers.get("active", "")
    spec = providers.get("providers", {}).get(name)
    if not spec:
        return None
    if not spec.get("api_key") or not spec.get("base_url") or not spec.get("model"):
        return None
    return {"name": name, **spec}


def _parse_ai_json(raw: str) -> dict | None:
    """防御式解析：先整体 loads，失败则抽取首个 {...} 块."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(0))
        except json.JSONDecodeError:
            return None
    return None


def call_llm(provider: dict, user_prompt: str, timeout_s: int = 90) -> str:
    """POST {base_url}/chat/completions（OpenAI 兼容协议，urllib 标准库实现）."""
    url = provider["base_url"].rstrip("/") + "/chat/completions"
    payload = {
        "model": provider["model"],
        "temperature": 0.1,
        "messages": [
            {"role": "system", "content": AI_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {provider['api_key']}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"HTTP {exc.code}: {body}") from exc
    except (urllib.error.URLError, KeyError, IndexError) as exc:
        raise RuntimeError(f"请求/解析失败: {exc}") from exc


def screen_ai(provider: dict, tpl: Template, text: str) -> dict:
    """引擎B：单份简历 AI 评估。返回与引擎A同构的结果."""
    criteria = {
        "必备门槛(required_skills)": list(tpl.required_skills),
        "最低年限(min_years)": tpl.min_years,
        "核心技能与权重": {r.name: r.weight for r in tpl.core_skills},
        "加分项": {r.name: r.weight for r in tpl.nice_to_have} or None,
    }
    user_prompt = (
        f"筛选标准：\n{json.dumps(criteria, ensure_ascii=False, indent=2)}\n\n"
        f"简历全文（超长截断）：\n{text[:6000]}"
    )
    result = _parse_ai_json(call_llm(provider, user_prompt))
    if result is None or "passed" not in result:
        raise RuntimeError("模型未返回可解析的 JSON 结果")
    return {
        "score": float(result.get("score", 0)),
        "status": "通过" if result.get("passed") else "未通过",
        "reason": str(result.get("reason", ""))[:200],
        "missing": list(result.get("missing", []) or []),
    }


# ============ 存储（SQLite） ============

_DDL = """
CREATE TABLE IF NOT EXISTS persons(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  pkey TEXT NOT NULL UNIQUE,
  name TEXT, phone TEXT, email TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS resumes(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  filename TEXT NOT NULL,
  path TEXT NOT NULL,
  sha256 TEXT NOT NULL UNIQUE,
  text TEXT NOT NULL,
  imported_at TEXT NOT NULL,
  deleted_at TEXT,
  person_id INTEGER REFERENCES persons(id)
);
CREATE TABLE IF NOT EXISTS screenings(
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  resume_id INTEGER NOT NULL REFERENCES resumes(id) ON DELETE CASCADE,
  engine TEXT NOT NULL,
  template TEXT NOT NULL,
  score REAL,
  status TEXT NOT NULL,
  detail TEXT,
  created_at TEXT NOT NULL
);
"""


_MIGRATED_DBS: set[str] = set()


def _migrate(conn: sqlite3.Connection, key: str = "") -> None:
    """老库补列与数据订正。同一库进程内只执行一次，且必定提交（避免写锁悬挂）."""
    if key and key in _MIGRATED_DBS:
        return
    cols = {row[1] for row in conn.execute("PRAGMA table_info(resumes)")}
    if "deleted_at" not in cols:
        conn.execute("ALTER TABLE resumes ADD COLUMN deleted_at TEXT")
        conn.commit()
    if "person_id" not in cols:
        conn.execute("ALTER TABLE resumes ADD COLUMN person_id INTEGER REFERENCES persons(id)")
        conn.commit()
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resumes_deleted ON resumes(deleted_at)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_resumes_person ON resumes(person_id)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_screenings_tpl ON screenings(template, resume_id)")

    # 状态枚举改名：淘汰 → 未通过（含 detail JSON 内的 status）
    if conn.execute("SELECT 1 FROM screenings WHERE status='淘汰' LIMIT 1").fetchone():
        conn.execute("UPDATE screenings SET status='未通过' WHERE status='淘汰'")
    if conn.execute(
            "SELECT 1 FROM screenings WHERE detail LIKE '%\"status\": \"淘汰\"%' LIMIT 1").fetchone():
        conn.execute(
            "UPDATE screenings SET detail=REPLACE(detail, '\"status\": \"淘汰\"',"
            " '\"status\": \"未通过\"')"
            " WHERE detail LIKE '%\"status\": \"淘汰\"%'")

    # 回填候选人归属（只处理还没归属的行）
    pending = conn.execute(
        "SELECT id, filename, text FROM resumes WHERE person_id IS NULL").fetchall()
    for r in pending:
        pkey, name, phone, email = person_info(r["filename"], r["text"])
        pid = ensure_person(conn, pkey, name, phone, email)
        conn.execute("UPDATE resumes SET person_id=? WHERE id=?", (pid, r["id"]))
    conn.commit()
    if key:
        _MIGRATED_DBS.add(key)


# ---------- 候选人识别（姓名 / 手机 / 邮箱） ----------

_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[-\s]?)?(1[3-9]\d{9})(?!\d)")
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_NAME_SPLIT = re.compile(r"[\s\-–—|｜,，:：/、()（）]+")


def _guess_name(filename: str, text: str) -> str | None:
    """姓名启发式：正文前几行的首个短 token，失败则退回文件名前缀."""
    for line in (text or "").splitlines()[:6]:
        line = re.sub(r"^(姓名|名字|name)\s*[:：]\s*", "", line.strip(), flags=re.IGNORECASE)
        if not line:
            continue
        first = _NAME_SPLIT.split(line)[0].replace("简历", "").strip()
        if 2 <= len(first) <= 12 and not re.search(r"\d", first):
            return first
    stem = Path(filename).stem
    first = _NAME_SPLIT.split(stem)[0].replace("简历", "").strip()
    return first if 2 <= len(first) <= 12 else None


def person_info(filename: str, text: str) -> tuple[str, str | None, str | None, str | None]:
    """返回 (去重键, 姓名, 手机, 邮箱)。有邮箱/手机优先用它们做键，否则用姓名。"""
    text = text or ""
    phone_m = _PHONE_RE.search(text)
    email_m = _EMAIL_RE.search(text)
    phone = phone_m.group(1) if phone_m else None
    email = email_m.group(0).lower() if email_m else None
    name = _guess_name(filename, text)
    if email:
        pkey = f"email:{email}"
    elif phone:
        pkey = f"phone:{phone}"
    elif name:
        pkey = f"name:{name}"
    else:
        pkey = f"file:{filename}"
    return pkey, name, phone, email


def ensure_person(conn: sqlite3.Connection, pkey: str, name: str | None,
                  phone: str | None, email: str | None) -> int:
    row = conn.execute("SELECT id FROM persons WHERE pkey=?", (pkey,)).fetchone()
    if row:
        conn.execute(
            "UPDATE persons SET name=COALESCE(name, ?), phone=COALESCE(phone, ?),"
            " email=COALESCE(email, ?) WHERE id=?", (name, phone, email, row["id"]))
        return int(row["id"])
    cur = conn.execute(
        "INSERT INTO persons(pkey, name, phone, email, created_at) VALUES(?,?,?,?,?)",
        (pkey, name, phone, email, now()))
    return int(cur.lastrowid)


def get_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path, timeout=10)
    conn.row_factory = sqlite3.Row  # 支持按列名取值 row["id"]
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")  # 并发写入时等待而不是立刻报 locked
    conn.executescript(_DDL)
    _migrate(conn, db_path)
    _ensure_fts(conn, db_path)
    return conn


# ============ 全文搜索（FTS5，中文按字切分） ============
# 中文没有空格分隔，unicode61 会把整句当一个 token。这里把 CJK 字符逐字前后加空格，
# 查询时同样切分并用短语匹配（"算 法"），即可实现中文精确词查询且支持英文单词。

_CJK_RE = re.compile(r"([\u3400-\u4dbf\u4e00-\u9fff\u3040-\u30ff\uac00-\ud7af\uf900-\ufaff])")
_FTS_STATE: dict[str, bool] = {}


def seg_text(text: str) -> str:
    """CJK 逐字加空格，便于 FTS5 分词；英文/数字保持原样。"""
    return _CJK_RE.sub(r" \1 ", text or "")


def _ensure_fts(conn: sqlite3.Connection, key: str) -> bool:
    """建 FTS 表并回填缺失索引；不支持 FTS5 时返回 False（调用方降级为 LIKE）。"""
    if key in _FTS_STATE:
        return _FTS_STATE[key]
    ok = True
    try:
        conn.execute(
            "CREATE VIRTUAL TABLE IF NOT EXISTS resume_fts"
            " USING fts5(filename, body, tokenize='unicode61')"
        )
        missing = conn.execute(
            "SELECT id, filename, text FROM resumes"
            " WHERE id NOT IN (SELECT rowid FROM resume_fts)"
        ).fetchall()
        for r in missing:
            conn.execute(
                "INSERT INTO resume_fts(rowid, filename, body) VALUES(?,?,?)",
                (r["id"], seg_text(r["filename"]), seg_text(r["text"])),
            )
        conn.commit()
    except sqlite3.OperationalError as exc:
        print(f"[提示] FTS5 不可用，全文搜索降级为 LIKE 匹配：{exc}")
        ok = False
    _FTS_STATE[key] = ok
    return ok


def index_resume(conn: sqlite3.Connection, resume_id: int, filename: str, text: str) -> None:
    """写入/更新单份简历的搜索索引（FTS 不可用时静默跳过）。"""
    try:
        conn.execute("DELETE FROM resume_fts WHERE rowid=?", (resume_id,))
        conn.execute(
            "INSERT INTO resume_fts(rowid, filename, body) VALUES(?,?,?)",
            (resume_id, seg_text(filename), seg_text(text)),
        )
    except sqlite3.OperationalError:
        pass


def _build_match(query: str) -> str:
    """把用户输入转成 FTS5 查询：每个词一组，中文用短语，多词 AND。"""
    parts: list[str] = []
    for term in re.split(r"\s+", query.strip()):
        tokens = seg_text(term).split()
        if not tokens:
            continue
        phrase = " ".join(t.replace('"', '""') for t in tokens)
        parts.append(f'"{phrase}"')
    return " AND ".join(parts)


def search_ids(conn: sqlite3.Connection, query: str) -> list[int]:
    """全文搜索命中的简历 ID（按相关度排序，排除回收站）。"""
    q = (query or "").strip()
    if not q:
        return []
    match = _build_match(q)
    if match:
        try:
            rows = conn.execute(
                "SELECT r.id FROM resume_fts JOIN resumes r ON r.id = resume_fts.rowid"
                " WHERE resume_fts MATCH ? AND r.deleted_at IS NULL"
                " ORDER BY bm25(resume_fts)",
                (match,),
            ).fetchall()
            return [r["id"] for r in rows]
        except sqlite3.OperationalError:
            pass  # FTS 不可用或查询语法异常 → 走 LIKE
    like = f"%{q}%"
    rows = conn.execute(
        "SELECT id FROM resumes WHERE deleted_at IS NULL"
        " AND (filename LIKE ? OR text LIKE ?) ORDER BY id DESC",
        (like, like),
    ).fetchall()
    return [r["id"] for r in rows]


def make_snippet(text: str, query: str, width: int = 80) -> str:
    """从原文截取命中位置附近的片段（用于结果列表提示为什么命中）。"""
    low = (text or "").lower()
    best, hit = -1, ""
    for term in re.split(r"\s+", (query or "").strip()):
        if not term:
            continue
        i = low.find(term.lower())
        if i >= 0 and (best < 0 or i < best):
            best, hit = i, term
    if best < 0:
        return (text or "")[:width].replace("\n", " ")
    start = max(0, best - width // 2)
    end = min(len(text), best + len(hit) + width // 2)
    s = text[start:end].replace("\n", " ")
    return ("…" if start > 0 else "") + s + ("…" if end < len(text) else "")


def _save_screening(conn: sqlite3.Connection, resume_id: int, engine: str,
                    tpl_name: str, result: dict) -> None:
    conn.execute(
        "INSERT INTO screenings(resume_id, engine, template, score, status, detail, created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (resume_id, engine, tpl_name, result["score"], result["status"],
         json.dumps(result, ensure_ascii=False), now()),
    )
    conn.commit()


def _fetch_resumes(conn: sqlite3.Connection, ids: list[int] | None = None) -> list[sqlite3.Row]:
    """取库内简历（默认排除回收站）."""
    if ids:
        marks = ",".join("?" * len(ids))
        return conn.execute(
            f"SELECT * FROM resumes WHERE id IN ({marks}) AND deleted_at IS NULL ORDER BY id", ids
        ).fetchall()
    return conn.execute(
        "SELECT * FROM resumes WHERE deleted_at IS NULL ORDER BY id"
    ).fetchall()


# ============ 命令实现 ============

def import_one(conn: sqlite3.Connection, path: Path) -> str:
    """导入单份简历（CLI 与 Web UI 共用）.

    Returns:
        "added" 新增 / "restored" 命中回收站记录并恢复 / "dup" 内容重复 / "empty" 无法解析文本。
    """
    text = extract_text(path)
    if not text.strip():
        return "empty"
    digest = hashlib.sha256(path.read_bytes()).hexdigest()

    # 同一内容已在库中：回收站里的直接恢复，正常记录按重复跳过
    exist = conn.execute(
        "SELECT id, deleted_at FROM resumes WHERE sha256=?", (digest,)
    ).fetchone()
    if exist:
        if exist["deleted_at"]:
            pkey, name, phone, email = person_info(path.name, text)
            pid = ensure_person(conn, pkey, name, phone, email)
            conn.execute(
                "UPDATE resumes SET deleted_at=NULL, path=?, filename=?, imported_at=?,"
                " person_id=? WHERE id=?",
                (str(path.resolve()), path.name, now(), pid, exist["id"]),
            )
            index_resume(conn, exist["id"], path.name, text)
            conn.commit()
            return "restored"
        return "dup"

    pkey, name, phone, email = person_info(path.name, text)
    pid = ensure_person(conn, pkey, name, phone, email)
    cur = conn.execute(
        "INSERT INTO resumes(filename, path, sha256, text, imported_at, person_id)"
        " VALUES(?,?,?,?,?,?)",
        (path.name, str(path.resolve()), digest, text, now(), pid),
    )
    index_resume(conn, int(cur.lastrowid), path.name, text)
    conn.commit()
    return "added"


def cmd_import(args: argparse.Namespace) -> int:
    src = Path(args.dir)
    if not src.is_dir():
        sys.exit(f"[参数错误] 目录不存在: {src}")
    files = sorted(
        p for p in src.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith("~$")
    )
    if not files:
        sys.exit(f"[参数错误] 目录中没有可处理的简历: {src}")

    conn = get_conn(args.db)
    counts = {"added": 0, "restored": 0, "dup": 0, "empty": 0}
    for p in files:
        counts[import_one(conn, p)] += 1
    print(f"[导入完成] 新增 {counts['added']} 份，从回收站恢复 {counts['restored']} 份，"
          f"重复跳过 {counts['dup']} 份，无法解析跳过 {counts['empty']} 份（库: {args.db}）")
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    conn = get_conn(args.db)
    rows = conn.execute(
        "SELECT r.id, r.filename, r.imported_at, LENGTH(r.text) AS chars,"
        " COUNT(s.id) AS screenings"
        " FROM resumes r LEFT JOIN screenings s ON s.resume_id = r.id"
        " WHERE r.deleted_at IS NULL"
        " GROUP BY r.id ORDER BY r.id"
    ).fetchall()
    if not rows:
        print("简历库为空，先执行: python resume_tool.py import --dir <简历目录>")
        return 0
    print(f"{'ID':<5}{'字符数':<8}{'筛选次数':<9}{'导入时间':<21}文件")
    print("-" * 80)
    for row in rows:
        print(f"{row['id']:<5}{row['chars']:<9}{row['screenings']:<10}{row['imported_at']:<21}{row['filename']}")
    print(f"\n共 {len(rows)} 份简历")
    return 0


def _require_provider(args: argparse.Namespace) -> dict:
    providers = load_providers(Path(args.providers))
    provider = get_active_provider(providers)
    if provider is None:
        active = providers.get("active", "(未设置)")
        sys.exit(
            f"[AI 未就绪] 当前厂商「{active}」缺少 api_key / base_url / model。\n"
            f"请编辑 {args.providers} 填入完整配置后重试；"
            "或先使用 --engine keyword 离线筛选。"
        )
    return provider


def cmd_screen(args: argparse.Namespace) -> int:
    tpl = load_template(Path(args.template))
    conn = get_conn(args.db)
    resumes = _fetch_resumes(conn, args.ids)
    if not resumes:
        sys.exit("[错误] 简历库为空或指定 ID 不存在")

    if args.engine == "keyword":
        print(f"[引擎A·关键词] 模板「{tpl.name}」 | 共 {len(resumes)} 份")
        for row in resumes:
            result = screen_keyword(row["text"], tpl)
            _save_screening(conn, row["id"], "keyword", tpl.name, result)
            years = result.get("years")
            print(f"  #{row['id']:<3} {result['status']:<4} {result['score']:<7}"
                  f"{('未声明' if years is None else str(years) + '年'):<6}{row['filename']}"
                  + (f"  ({result['reason']})" if result["status"] != "通过" else ""))
        return 0

    if args.engine == "ai":
        provider = _require_provider(args)
        print(f"[引擎B·AI] 模板「{tpl.name}」 | 厂商 {provider['name']} / {provider['model']} | 共 {len(resumes)} 份")
        print("  提示：AI 按次调用计费，批量筛选建议优先用 funnel 模式控制成本")
        for i, row in enumerate(resumes, 1):
            try:
                result = screen_ai(provider, tpl, row["text"])
            except RuntimeError as exc:
                print(f"  #{row['id']} {row['filename']} 失败: {exc}")
                continue
            _save_screening(conn, row["id"], "ai", tpl.name, result)
            print(f"  [{i}/{len(resumes)}] #{row['id']:<3} {result['status']:<4}"
                  f"{result['score']:<7}{row['filename']}  ({result['reason']})")
        return 0

    # funnel：keyword 全量 → AI 只复核「待核实」
    provider: dict | None
    try:
        provider = _require_provider(args)
    except SystemExit as exc:
        print(f"{exc}")
        provider = None
    print(f"[分级漏斗] 模板「{tpl.name}」 | 第1阶段：关键词全量初筛（{len(resumes)} 份）")
    review_ids: list[int] = []
    for row in resumes:
        result = screen_keyword(row["text"], tpl)
        _save_screening(conn, row["id"], "keyword", tpl.name, result)
        if result["status"] == "待核实":
            review_ids.append(row["id"])
        print(f"  #{row['id']:<3} {result['status']:<4} {result['score']:<7}{row['filename']}")
    if not review_ids:
        print("第2阶段：无「待核实」简历，AI 复核跳过")
        return 0
    if provider is None:
        print(f"第2阶段：{len(review_ids)} 份待核实，但 AI 未配置（见上方提示），请人工复核 ID: {review_ids}")
        return 0
    print(f"第2阶段：AI 复核待核实名单 {review_ids}")
    for rid in review_ids:
        row = conn.execute("SELECT * FROM resumes WHERE id=?", (rid,)).fetchone()
        try:
            result = screen_ai(provider, tpl, row["text"])
        except RuntimeError as exc:
            print(f"  #{rid} 失败: {exc}")
            continue
        _save_screening(conn, rid, "ai", tpl.name, result)
        print(f"  #{rid:<3} {result['status']:<4} {result['score']:<7}{row['filename']}  ({result['reason']})")
    return 0


def cmd_export(args: argparse.Namespace) -> int:
    conn = get_conn(args.db)
    resumes = conn.execute(
        "SELECT * FROM resumes WHERE deleted_at IS NULL ORDER BY id").fetchall()
    if not resumes:
        sys.exit("[错误] 简历库为空")
    # 每份简历取每个引擎的最新一次结果
    latest: dict[tuple[int, str], sqlite3.Row] = {}
    for s in conn.execute("SELECT * FROM screenings ORDER BY created_at, id").fetchall():
        latest[(s["resume_id"], s["engine"])] = s

    fields = ["ID", "文件", "关键词状态", "关键词分", "关键词结论",
              "AI状态", "AI分", "AI理由", "综合建议"]
    with open(args.out, "w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        for row in resumes:
            kw = latest.get((row["id"], "keyword"))
            ai = latest.get((row["id"], "ai"))
            kw_detail = json.loads(kw["detail"]) if kw else {}
            if ai is not None:
                suggestion = f"{ai['status']}(AI)"
            elif kw is None:
                suggestion = "未筛选"
            elif kw["status"] == "通过":
                suggestion = "通过，建议人工复核"
            elif kw["status"] == "待核实":
                suggestion = "建议 AI 复核(需配置 API)或人工"
            else:
                suggestion = "未通过"
            writer.writerow([
                row["id"], row["filename"],
                kw["status"] if kw else "-", kw["score"] if kw else "-",
                kw_detail.get("reason", "-") if kw else "-",
                ai["status"] if ai else "-", ai["score"] if ai else "-",
                json.loads(ai["detail"]).get("reason", "") if ai else "-",
                suggestion,
            ])
    print(f"[导出完成] {Path(args.out).resolve()}")
    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    path = Path(args.providers)
    providers = load_providers(path)
    active = providers.get("active", "(未设置)")
    print(f"配置文件: {path.resolve()}\n当前厂商: {active}\n")
    for name, spec in providers.get("providers", {}).items():
        key = spec.get("api_key", "")
        masked = (key[:4] + "***") if key else "(未配置)"
        ready = "✓ 就绪" if all([spec.get("api_key"), spec.get("base_url"), spec.get("model")]) else "✗ 缺配置"
        print(f"  [{name}] {spec.get('label', '')} | {ready} | key={masked} | "
              f"model={spec.get('model', '-')} | base_url={spec.get('base_url', '-')}")
    print("\n编辑该 JSON 切换厂商：改 active 字段并填好对应 api_key 即可。"
          "所有兼容 OpenAI chat/completions 协议的服务均可接入（含自建网关）。")
    return 0


def cmd_search(args: argparse.Namespace) -> int:
    conn = get_conn(args.db)
    ids = search_ids(conn, args.q)
    if not ids:
        print(f"没有命中「{args.q}」的简历")
        return 0
    marks = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in conn.execute(
        f"SELECT id, filename, text FROM resumes WHERE id IN ({marks})", ids).fetchall()}
    print(f"命中 {len(ids)} 份（按相关度排序）：\n")
    for rid in ids:
        row = rows.get(rid)
        if not row:
            continue
        print(f"  #{row['id']:<4} {row['filename']}")
        print(f"        {make_snippet(row['text'], args.q, 70)}")
    return 0


# ============ CLI ============

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="单机简历筛选工具（双引擎：关键词离线 + AI 在线可选）")
    parser.add_argument("--db", default=DEFAULT_DB, help=f"SQLite 库路径（默认 {DEFAULT_DB}）")
    parser.add_argument("--providers", default=str(DEFAULT_PROVIDERS), help="AI 厂商配置 JSON 路径")
    sub = parser.add_subparsers(dest="command", required=True)

    p_import = sub.add_parser("import", help="批量导入简历目录")
    p_import.add_argument("--dir", required=True, help="简历所在目录")

    sub.add_parser("list", help="列出简历库")

    p_screen = sub.add_parser("screen", help="运行筛选引擎")
    p_screen.add_argument("--engine", required=True, choices=["keyword", "ai", "funnel"],
                          help="keyword=离线关键词; ai=全量AI; funnel=关键词初筛+AI复核待核实")
    p_screen.add_argument("--template", required=True, help="筛选模板 JSON 路径")
    p_screen.add_argument("--ids", default=None, help="只筛选指定 ID，逗号分隔，如 1,3,5")

    p_export = sub.add_parser("export", help="导出筛选报告 CSV")
    p_export.add_argument("--out", default="report.csv", help="输出路径（默认 report.csv）")

    sub.add_parser("providers", help="查看 AI 厂商配置状态")

    p_search = sub.add_parser("search", help="全文搜索简历正文（中文按字切分 + FTS5）")
    p_search.add_argument("--q", required=True, help="关键词，多个词用空格分隔（AND 关系）")

    args = parser.parse_args(argv)
    if getattr(args, "ids", None):
        args.ids = [int(x) for x in re.split(r"[,\s]+", args.ids.strip()) if x]

    handlers = {"import": cmd_import, "list": cmd_list, "screen": cmd_screen,
                "export": cmd_export, "providers": cmd_providers, "search": cmd_search}
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
