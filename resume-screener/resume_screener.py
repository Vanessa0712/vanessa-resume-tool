#!/usr/bin/env python3
"""简历技能批量筛选工具（resume_screener）.

筛选模型（两段式，与人工筛选漏斗一致）：
  1. 硬性门槛（Hard Gates）：必备技能缺失、年限不符 → 直接淘汰；
  2. 加权评分（Weighted Scoring）：核心技能按权重打分排序，加分项补分。

设计取舍（为什么这样做）：
  - 零依赖即可运行：txt/md/docx 全部标准库解析，简历少时拿来就用；
  - PDF 为可选依赖 pypdf：缺失时跳过并告警，不阻塞整体流程；
  - 证据 = 关键词出现次数：多次出现 ≈ 项目里真用过，只出现一次 ≈ 仅罗列；
  - 关键词匹配 ≠ 真实能力：本工具定位「初筛排序」，通过者仍需人工/笔试复核。

用法：
  python resume_screener.py --config requirements.json --dir resumes --out report.csv
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import zipfile
from dataclasses import dataclass
from pathlib import Path

# ---------- 可选依赖：PDF 支持（缺失时优雅降级） ----------
try:
    from pypdf import PdfReader  # type: ignore[import-untyped]
except ImportError:
    PdfReader = None


# ============ 数据模型 ============

@dataclass(frozen=True)
class SkillRule:
    """一条技能规则：名称 + 权重 + 触发别名（同义词/相关技术，命中任一即算证据）."""

    name: str
    weight: float
    aliases: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    """筛选配置：岗位名 + 硬门槛 + 核心技能 + 加分项."""

    position: str
    required_skills: tuple[str, ...]
    min_years: int | None
    strict_years: bool
    core_skills: tuple[SkillRule, ...]
    nice_to_have: tuple[SkillRule, ...]


@dataclass
class ScreeningResult:
    """单份简历的筛选结果."""

    file: Path
    years: int | None
    core_counts: dict[str, int]
    bonus_counts: dict[str, int]
    score: float  # 0-100
    status: str   # 通过 / 待核实 / 淘汰
    reason: str


# ============ 配置解析 ============

def load_config(path: Path) -> Config:
    """读取并校验筛选配置 JSON.

    Args:
        path: 配置文件路径。

    Returns:
        解析后的 Config 对象。

    Raises:
        SystemExit: 文件不存在、JSON 非法、必填缺失或字段校验失败时退出。
    """
    if not path.is_file():
        sys.exit(f"[配置错误] 找不到配置文件: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        sys.exit(f"[配置错误] JSON 解析失败: {exc}")

    gates = raw.get("hard_gates", {})
    core_raw: dict = raw.get("core_skills", {})
    nice_raw: dict = raw.get("nice_to_have", {})
    if not core_raw:
        sys.exit("[配置错误] core_skills 不能为空")

    def _rules(section: dict) -> tuple[SkillRule, ...]:
        """把 JSON 片段转成 SkillRule 元组；别名缺省为技能名自身."""
        rules = []
        for name, spec in section.items():
            aliases = tuple(spec.get("aliases", [name])) or (name,)
            rules.append(SkillRule(name=name, weight=float(spec.get("weight", 1)), aliases=aliases))
        return tuple(rules)

    required = [str(s) for s in gates.get("required_skills", [])]
    min_years = gates.get("min_years")
    if min_years is not None and (not isinstance(min_years, int) or min_years < 0):
        sys.exit("[配置错误] min_years 必须是非负整数或省略")

    # 硬门槛引用的技能必须先有定义，否则永远匹配不到（提前暴露配置错误）
    defined = set(core_raw) | set(nice_raw)
    unknown = [s for s in required if s not in defined]
    if unknown:
        sys.exit(f"[配置错误] required_skills 中的 {unknown} 未在 core_skills/nice_to_have 中定义")

    return Config(
        position=str(raw.get("position", "未命名岗位")),
        required_skills=tuple(required),
        min_years=min_years,
        strict_years=bool(gates.get("strict_years", False)),
        core_skills=_rules(core_raw),
        nice_to_have=_rules(nice_raw),
    )


# ============ 文本抽取 ============

# 中文简历常见 GBK/GB18030 编码，按序尝试
_TEXT_ENCODINGS = ("utf-8", "utf-8-sig", "gb18030")


def _read_plain(path: Path) -> str:
    """按常见编码尝试读取纯文本，全部失败返回空串."""
    for enc in _TEXT_ENCODINGS:
        try:
            return path.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
    return ""


def _extract_docx(path: Path) -> str:
    """用标准库解析 .docx（本质是 zip 内的 XML），拼接所有 <w:t> 文本节点.

    限制：忽略表格嵌套顺序与页眉页脚，对常规简历足够。
    """
    try:
        with zipfile.ZipFile(path) as zf:
            xml = zf.read("word/document.xml").decode("utf-8", errors="ignore")
    except (zipfile.BadZipFile, KeyError):
        return ""
    return "".join(re.findall(r"<w:t[^>]*>([^<]*)</w:t>", xml))


def extract_text(path: Path) -> str:
    """按扩展名分发文本抽取.

    Args:
        path: 简历文件路径，支持 .txt/.md/.docx/.pdf。

    Returns:
        抽取的纯文本；无法解析时返回空字符串（由调用方决定跳过）。
    """
    suffix = path.suffix.lower()
    if suffix in (".txt", ".md", ".markdown"):
        return _read_plain(path)
    if suffix == ".docx":
        return _extract_docx(path)
    if suffix == ".pdf":
        if PdfReader is None:
            print(f"[警告] 跳过 {path.name}: PDF 支持需要 pip install pypdf", file=sys.stderr)
            return ""
        try:
            reader = PdfReader(str(path))
            return "\n".join(page.extract_text() or "" for page in reader.pages)
        except Exception as exc:  # PDF 结构复杂多变，单文件损坏不中断整体
            print(f"[警告] PDF 解析失败 {path.name}: {exc}", file=sys.stderr)
            return ""
    print(f"[警告] 跳过不支持的格式: {path.name}", file=sys.stderr)
    return ""


# ============ 证据统计与打分 ============

def _count_keyword(text: str, alias: str) -> int:
    """统计别名出现次数.

    前后不得紧邻字母/数字：避免 python 误命中 pythonic、
    "2024 年" 里的 24 误命中 2 年——这是关键词匹配最常见的两类误报。
    """
    pattern = re.compile(rf"(?<![A-Za-z0-9]){re.escape(alias)}(?![A-Za-z0-9])", re.IGNORECASE)
    return len(pattern.findall(text))


def count_evidence(text: str, rule: SkillRule) -> int:
    """一条技能规则在文本中的证据次数（所有别名命中之和）."""
    return sum(_count_keyword(text, alias) for alias in rule.aliases)


def evidence_to_score(count: int) -> float:
    """证据次数 → 0-5 分评分.

    经验法则：0 次=未提及；1 次=仅罗列；2-3 次=用过；4+ 次=深度实践。
    阈值可按团队口径调整。
    """
    if count <= 0:
        return 0.0
    if count == 1:
        return 2.0
    if count <= 3:
        return 4.0
    return 5.0


# ============ 年限抽取 ============

# "2024年" 不应抽出 24：用 (?<!\d) 挡住前导数字；"X月" 不是年限：用 (?![\d月]) 挡住
_YEAR_PATTERNS = (
    re.compile(r"(?<!\d)(\d{1,2})\s*\+?\s*年(?![\d月])"),  # 5年 / 5年经验 / 3+年
    re.compile(r"(?<!\d)(\d{1,2})\s*\+?\s*years?\b", re.IGNORECASE),  # 5 years / 5+ years
)


def extract_years(text: str) -> int | None:
    """从简历文本抽取声明的工作年限（取所有命中中的最大值，向下取整）.

    Returns:
        年限数字；未找到返回 None（注意：中文数字"五年"不识别，属已知限制）。
    """
    hits: list[int] = []
    for pattern in _YEAR_PATTERNS:
        hits.extend(int(m) for m in pattern.findall(text))
    hits = [y for y in hits if 1 <= y <= 45]  # 过滤明显异常值
    return max(hits) if hits else None


# ============ 核心：单份简历筛选 ============

def screen_resume(path: Path, text: str, config: Config) -> ScreeningResult:
    """对单份简历执行「硬门槛过滤 + 加权评分」.

    Args:
        path: 简历文件路径。
        text: 简历纯文本。
        config: 筛选配置。

    Returns:
        ScreeningResult，含评分、状态（通过/待核实/淘汰）与原因。
    """
    years = extract_years(text)
    all_rules = (*config.core_skills, *config.nice_to_have)
    evidence = {rule.name: count_evidence(text, rule) for rule in all_rules}

    # ---- 第 1 段：硬性门槛 ----
    status = "通过"
    reasons: list[str] = []

    missing = [n for n in config.required_skills if evidence.get(n, 0) == 0]
    if missing:
        reasons.append(f"缺少必备技能: {', '.join(missing)}")
        status = "淘汰"

    if config.min_years is not None:
        if years is None:
            # 年限未声明：默认温和处理（待核实），strict_years=true 时直接淘汰
            if config.strict_years:
                reasons.append("未声明工作年限(严格模式)")
                status = "淘汰"
            elif status == "通过":
                status = "待核实"
                reasons.append("未声明工作年限，需人工确认")
        elif years < config.min_years:
            reasons.append(f"年限不足: {years}年 < 要求{config.min_years}年")
            status = "淘汰"

    # ---- 第 2 段：加权评分 ----
    def _weighted_score(rules: tuple[SkillRule, ...]) -> float:
        """归一化加权得分(0-1)：Σ(评分/5 × 权重) / Σ权重."""
        total_weight = sum(r.weight for r in rules)
        if total_weight <= 0:
            return 0.0
        return sum(evidence_to_score(evidence[r.name]) / 5.0 * r.weight for r in rules) / total_weight

    if config.nice_to_have:
        # 核心技能占 90 分、加分项占 10 分，保证总分始终在 0-100
        score = round(_weighted_score(config.core_skills) * 90 + _weighted_score(config.nice_to_have) * 10, 1)
    else:
        score = round(_weighted_score(config.core_skills) * 100, 1)

    return ScreeningResult(
        file=path,
        years=years,
        core_counts={r.name: evidence[r.name] for r in config.core_skills},
        bonus_counts={r.name: evidence[r.name] for r in config.nice_to_have},
        score=score,
        status=status,
        reason="；".join(reasons) if reasons else "符合硬性门槛",
    )


# ============ 报告输出 ============

def _fmt_evidence(counts: dict[str, int]) -> str:
    """把 {技能: 次数} 压缩成 'python:5 sql:3' 形式，0 次的不显示."""
    return " ".join(f"{k}:{v}" for k, v in counts.items() if v > 0) or "-"


def print_report(results: list[ScreeningResult], config: Config) -> None:
    """打印排名表到控制台：通过 > 待核实 > 淘汰，同状态按总分降序."""
    print(f"\n=== 岗位「{config.position}」简历筛选报告（共 {len(results)} 份）===\n")
    print(f"{'排名':<4} {'状态':<5} {'总分':<6} {'年限':<5} 文件")
    print("-" * 78)
    for idx, r in enumerate(results, start=1):
        years_txt = f"{r.years}年" if r.years is not None else "未声明"
        print(f"{idx:<4} {r.status:<5} {r.score:<7} {years_txt:<6} {r.file.name}")
        detail = f"      证据: {_fmt_evidence(r.core_counts)}"
        if r.bonus_counts:
            detail += f" | 加分: {_fmt_evidence(r.bonus_counts)}"
        print(detail)
        if r.status != "通过":
            print(f"      原因: {r.reason}")
    print()


def write_csv(results: list[ScreeningResult], config: Config, out: Path) -> None:
    """写 CSV 报告.

    utf-8-sig 编码：Windows Excel 双击直接打开不乱码（中文 CSV 的经典坑）。
    """
    core_names = [r.name for r in config.core_skills]
    nice_names = [r.name for r in config.nice_to_have]
    fields = ["排名", "文件", "状态", "总分", "声明年限", "门槛说明"]
    fields += [f"核心:{n}" for n in core_names]
    fields += [f"加分:{n}" for n in nice_names]

    with out.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(fields)
        for idx, r in enumerate(results, start=1):
            years_txt = r.years if r.years is not None else "未声明"
            writer.writerow(
                [idx, r.file.name, r.status, r.score, years_txt, r.reason]
                + [r.core_counts.get(n, 0) for n in core_names]
                + [r.bonus_counts.get(n, 0) for n in nice_names]
            )
    print(f"[完成] CSV 报告已写入: {out.resolve()}")


# ============ CLI 入口 ============

SUPPORTED_SUFFIXES = {".txt", ".md", ".markdown", ".docx", ".pdf"}


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：解析参数 → 批量筛选 → 排序 → 输出控制台报告 + CSV."""
    parser = argparse.ArgumentParser(
        description="简历技能批量筛选：硬性门槛过滤 + 核心技能加权评分排序",
    )
    parser.add_argument("--config", required=True, type=Path, help="技能要求配置 JSON 路径")
    parser.add_argument("--dir", required=True, type=Path, help="简历目录")
    parser.add_argument("--out", type=Path, default=Path("screening_report.csv"),
                        help="CSV 报告输出路径（默认 screening_report.csv）")
    args = parser.parse_args(argv)

    config = load_config(args.config)
    if not args.dir.is_dir():
        sys.exit(f"[参数错误] 简历目录不存在: {args.dir}")

    # 过滤临时文件（~$ 开头是 Word 打开时的锁文件）
    files = sorted(
        p for p in args.dir.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and not p.name.startswith("~$")
    )
    if not files:
        sys.exit(f"[参数错误] 目录中没有可处理的简历: {args.dir}")

    results: list[ScreeningResult] = []
    for path in files:
        text = extract_text(path)
        if not text.strip():
            print(f"[警告] 未提取到文本，跳过: {path.name}", file=sys.stderr)
            continue
        results.append(screen_resume(path, text, config))

    if not results:
        sys.exit("[错误] 没有任何简历被成功解析")

    # 排序：通过 > 待核实 > 淘汰；同状态内按总分降序
    status_order = {"通过": 0, "待核实": 1, "淘汰": 2}
    results.sort(key=lambda r: (status_order[r.status], -r.score))

    print_report(results, config)
    write_csv(results, config, args.out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
