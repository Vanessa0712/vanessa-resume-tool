"""分页验收：临时库 + 独立端口（5001），不触碰正式数据（开发自检脚本）.

用法：python tools/_pagination_check.py
"""
import shutil
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools"))

import app as A            # noqa: E402
import resume_tool as rt   # noqa: E402
from screenshot import Tab, start_chrome, ws_url  # noqa: E402
from werkzeug.serving import make_server           # noqa: E402

TMP_DB = ROOT / "ui-preview" / ".demo-pagination.db"
PORT = 5001


def seed() -> int:
    TMP_DB.parent.mkdir(exist_ok=True)
    TMP_DB.unlink(missing_ok=True)
    shutil.copy(ROOT / "resume_tool.db", TMP_DB)
    conn = rt.get_conn(str(TMP_DB))
    roles = ["后端", "前端", "数据", "测试", "算法", "运维"]
    for i in range(1, 24):
        name = f"演示候选人{i:02d}-{roles[i % len(roles)]}.txt"
        text = (f"演示候选人{i:02d} - {roles[i % len(roles)]}工程师\n"
                f"工作年限：{i % 8 + 1}年\n"
                "技能：Python, SQL, Docker\n项目：内部数据平台\n")
        conn.execute(
            "INSERT INTO resumes(filename, path, sha256, text, imported_at)"
            " VALUES(?,?,?,?,?)",
            (name, str(ROOT / "samples" / name), f"demo-{i:03d}", text, rt.now()),
        )
    conn.commit()
    total = conn.execute("SELECT COUNT(*) AS n FROM resumes WHERE deleted_at IS NULL"
                         ).fetchone()["n"]
    conn.close()
    return total


def main() -> None:
    total = seed()
    A.DB = str(TMP_DB)
    server = make_server("127.0.0.1", PORT, A.app)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{PORT}"
    for _ in range(20):
        try:
            urllib.request.urlopen(base + "/", timeout=1)
            break
        except Exception:
            time.sleep(0.4)

    def body(url: str) -> str:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.read().decode("utf-8")

    print(f"临时库简历总数: {total}，每页 15 → 预计 {(total + 14) // 15} 页")
    checks = [
        ("/", 15, "第 1–15 份 / 共"),
        ("/?page=2", 15, "第 16–30 份 / 共"),
        ("/?page=3", 0, "共"),            # 仅 30 条 → 第 3 页应被收敛为第 2 页
        ("/?page=99", 0, "共"),
        ("/?q=" + urllib.parse.quote("数据"), 0, "份"),
    ]
    for path, _rows, must in checks:
        html = body(base + path)
        rows = html.count('class="row-check"')
        tail = [x for x in html.split('class="pager"')[1:2]]
        summary = ""
        if tail:
            seg = tail[0].split("</span>")[0]
            summary = seg.split(">")[-1].strip()
        print(f"{path:28} 行数={rows:2}  分页='{summary}'  含'{must}'={must in html}")

    # 截图：第 2 页（证明分页条与页码高亮）
    proc = start_chrome()
    try:
        tab = Tab(ws_url())
        tab.send("Emulation.setDeviceMetricsOverride",
                 {"width": 1440, "height": 1100, "deviceScaleFactor": 1, "mobile": False})
        tab.goto(base + "/")
        tab.js("document.getElementById('announce-ok')?.click();", wait=0.4)
        tab.js("document.getElementById('list').scrollIntoView({block:'start'});", wait=0.6)
        tab.shot("v7-01-library-page1.png")
        tab.goto(base + "/?page=2")
        tab.js("document.getElementById('list').scrollIntoView({block:'start'});", wait=0.6)
        tab.shot("v7-02-library-page2.png")
    finally:
        proc.terminate()
        server.shutdown()
        import gc
        gc.collect()
        for _ in range(5):  # SQLite 文件句柄回收有延迟，重试几次
            try:
                TMP_DB.unlink(missing_ok=True)
                break
            except OSError:
                time.sleep(0.5)
        shutil.rmtree(ROOT / "ui-preview" / ".cdp-profile", ignore_errors=True)
        print("临时库已删除，正式数据未改动")


if __name__ == "__main__":
    main()
