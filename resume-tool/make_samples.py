#!/usr/bin/env python3
"""生成测试样本简历：覆盖 txt / 真docx / 三种伪装.doc / PDF 共 8 份.

用法：python make_samples.py  （生成到 ./samples/）
"""

import zipfile
from pathlib import Path

SAMPLES = Path(__file__).parent / "samples"
SAMPLES.mkdir(exist_ok=True)

# ---------- 1) 纯文本简历 ×2 ----------
(SAMPLES / "张三-资深后端.txt").write_text(
    """张三 — Python 后端开发工程师

工作年限：6年后端开发经验

技能：精通 Python / Python3，asyncio 异步编程与 async/await；
FastAPI + uvicorn 高并发服务；MySQL / PostgreSQL 复杂 SQL 调优；
Docker / Kubernetes 容器化，GitLab CI/CD，pytest 单元测试。

项目：交易平台 FastAPI 重构 QPS 800→3000；PostgreSQL 分库分表；
Python 异步管道对接 Kafka，Redis 缓存命中率 92%，AWS 部署。
""", encoding="utf-8")

(SAMPLES / "李四-前端转岗.txt").write_text(
    """李四 — 前端工程师

工作年限：4年前端开发经验

技能：JavaScript / TypeScript / React / Vue / Webpack / Vite。
了解 Node.js。希望转全栈方向。
""", encoding="utf-8")

# ---------- 2) 真 docx（zip + document.xml） ----------
docx_xml = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
    "<w:body>"
    "<w:p><w:r><w:t>钱七 - 数据工程师</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>工作年限：4年数据开发经验</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>技能：Python, SQL, MySQL, PostgreSQL, Docker, pytest, CI/CD</w:t></w:r></w:p>"
    "<w:p><w:r><w:t>项目：Python ETL 管道日处理亿级数据；SQL 与存储过程优化；Docker 部署，pytest 覆盖核心链路。</w:t></w:r></w:p>"
    "</w:body></w:document>"
)
with zipfile.ZipFile(SAMPLES / "钱七-数据工程师.docx", "w") as zf:
    zf.writestr("word/document.xml", docx_xml)

# ---------- 3) 伪装 .doc ×3 ----------
# 3a) 实为 docx（招聘平台导出常见）
with zipfile.ZipFile(SAMPLES / "孙八-平台导出实为docx.doc", "w") as zf:
    zf.writestr("word/document.xml", docx_xml.replace("钱七", "孙八").replace("数据工程师", "后端工程师"))

# 3b) 实为 HTML（51job/智联导出常见）
(SAMPLES / "周九-平台导出实为html.doc").write_text(
    """<!DOCTYPE html><html><head><meta charset="utf-8"><title>简历</title></head>
<body><h1>周九 - Python 开发</h1>
<p>工作年限：5年 Python 开发经验</p>
<p>技能：Python, Django, FastAPI, SQL, MySQL, Docker</p>
<p>项目：Django 电商后台，FastAPI 微服务，SQL 报表优化，Docker 上线。</p>
</body></html>""", encoding="utf-8")

# 3c) 实为 RTF（\u 转义编码中文，验证解析路径）


def rtf_escape(text: str) -> str:
    out = []
    for ch in text:
        code = ord(ch)
        out.append(ch if code < 128 else f"\\u{code}?")
    return "".join(out)


rtf_body = rtf_escape(
    "吴十 - Python 工程师\n工作年限：5年开发经验\n"
    "技能：Python, Flask, SQL, PostgreSQL, Redis, Docker, pytest\n"
    "项目：Flask 数据平台，SQL 优化，Redis 缓存，Docker 部署。"
).replace("\n", "\\par ")
(SAMPLES / "吴十-rtf格式.doc").write_text(
    "{\\rtf1\\ansi\\deff0{" + rtf_body + "}}", encoding="latin-1")

# ---------- 4) PDF（手工构造最小合法 PDF，Type1 字体仅支持 ASCII 文本） ----------


def make_pdf(path: Path, text: str) -> None:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET"
    objs = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        "/Resources << /Font << /F1 5 0 R >> >> /Contents 4 0 R >>",
        f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream",
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    pdf = "%PDF-1.4\n"
    offsets = []
    for i, body in enumerate(objs, 1):
        offsets.append(len(pdf.encode("latin-1")))
        pdf += f"{i} 0 obj\n{body}\nendobj\n"
    xref_pos = len(pdf.encode("latin-1"))
    pdf += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n"
    for off in offsets:
        pdf += f"{off:010d} 00000 n \n"
    pdf += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref_pos}\n%%EOF"
    path.write_bytes(pdf.encode("latin-1"))


make_pdf(
    SAMPLES / "Zheng-EnglishResume.pdf",
    "Zheng Shi - Python Developer. 5 years experience. "
    "Skills: Python, FastAPI, SQL, MySQL, Docker, pytest, Redis.",
)

print(f"样本生成完成: {SAMPLES.resolve()}")
for p in sorted(SAMPLES.iterdir()):
    print(f"  {p.name}  ({p.stat().st_size} bytes)")
