"""用 Chrome DevTools Protocol 截取交互态界面（开发工具，非运行时依赖）.

用法：python tools/screenshot.py   （需本地已启动 python app.py）
产物：ui-preview/v*.png
"""
import base64
import json
import shutil
import subprocess
import time
import urllib.parse
import urllib.request
from pathlib import Path

import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
OUT = Path(__file__).resolve().parent.parent / "ui-preview"
PROFILE = OUT / ".cdp-profile"
PORT = 9333


def start_chrome() -> subprocess.Popen:
    shutil.rmtree(PROFILE, ignore_errors=True)  # 空存储：保证公告首次弹出
    PROFILE.mkdir(parents=True, exist_ok=True)
    return subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
        "--no-sandbox", "--disable-dev-shm-usage",
        "--hide-scrollbars", "--window-size=1440,1100",
        f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
        f"--user-data-dir={PROFILE}", "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def ws_url() -> str:
    for _ in range(40):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json/list", timeout=1) as r:
                for t in json.load(r):
                    if t.get("type") == "page":
                        return t["webSocketDebuggerUrl"]
        except Exception:
            pass
        time.sleep(0.5)
    raise RuntimeError("chrome CDP 未就绪")


class Tab:
    def __init__(self, url: str):
        self.ws = websocket.create_connection(url, timeout=30)
        self.i = 0
        self.send("Page.enable")
        self.send("Runtime.enable")

    def send(self, method: str, params: dict | None = None) -> dict:
        self.i += 1
        self.ws.send(json.dumps({"id": self.i, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.ws.recv())
            if msg.get("id") == self.i:
                return msg

    def goto(self, url: str, wait: float = 1.6) -> None:
        self.send("Page.navigate", {"url": url})
        time.sleep(wait)

    def js(self, code: str, wait: float = 0.5) -> None:
        self.send("Runtime.evaluate", {"expression": code, "awaitPromise": True})
        time.sleep(wait)

    def shot(self, name: str) -> None:
        data = self.send("Page.captureScreenshot", {"format": "png"})["result"]["data"]
        path = OUT / name
        path.write_bytes(base64.b64decode(data))
        print(f"saved {name}  {path.stat().st_size // 1024} KB")


def main() -> None:
    OUT.mkdir(exist_ok=True)
    base = "http://127.0.0.1:5000"
    proc = start_chrome()
    try:
        tab = Tab(ws_url())
        tab.send("Emulation.setDeviceMetricsOverride",
                 {"width": 1440, "height": 1100, "deviceScaleFactor": 1, "mobile": False})

        # 1. 进入页面：公告弹窗 → 关闭 → 侧栏 NEW 标识 → 干净列表
        tab.goto(base + "/")
        tab.shot("announce.png")
        tab.js("document.getElementById('announce-ok').click();", wait=0.5)
        tab.shot("nav-dot.png")
        tab.js("document.getElementById('nav-changelog-dot').hidden = true;", wait=0.2)
        tab.shot("library.png")

        # 2. 更新日志（访问后 NEW 自动清除）
        tab.goto(base + "/changelog")
        tab.shot("changelog.png")

        # 3. 全文搜索：中文关键词 + 命中片段
        tab.goto(base + "/?q=" + urllib.parse.quote("数据"))
        tab.shot("search.png")

        # 4. 按模板查看
        tab.goto(base + "/?template=python-backend")
        tab.shot("library-template.png")

        # 5. 多选 + 批量删除确认弹窗
        tab.js("document.getElementById('multi-toggle').click();")
        tab.js("""
          const boxes=[...document.querySelectorAll('.row-check')].slice(0,2);
          boxes.forEach(c=>{c.checked=true; c.dispatchEvent(new Event('change',{bubbles:true}));});
          document.getElementById('batch-delete').click();
        """, wait=0.8)
        tab.shot("batch-modal.png")
        tab.js("document.querySelector('#batch-modal [data-close]').click();", wait=0.3)

        # 6. 筛选中心：模板多选面板 + 指定简历选择器
        tab.goto(base + "/screen")
        tab.js("""
          document.getElementById('tpl-toggle').click();
          const qa=document.querySelector('[data-slug="qa-engineer"]');
          if(qa) qa.click();
          const sel=document.querySelector('input[name="scope"][value="selected"]');
          sel.checked=true; sel.dispatchEvent(new Event('change',{bubbles:true}));
          const s=document.getElementById('picker-search'); s.value='docx';
          s.dispatchEvent(new Event('input',{bubbles:true}));
          window.scrollTo(0, 200);
        """, wait=0.9)
        tab.shot("screen-picker.png")

        # 7. 后台任务：全量跑两个模板（关键词），完成后保留结果摘要
        tab.goto(base + "/screen")
        tab.js("""
          document.querySelector('input[name="scope"][value="all"]').checked=true;
          document.querySelector('input[name="engine"][value="keyword"]').checked=true;
          document.getElementById('screen-submit').click();
        """, wait=3.5)
        tab.shot("job-done.png")
        tab.js("window.scrollTo(0, 520);", wait=0.6)
        tab.shot("screen-results.png")

        # 8. 回收站：临时删除一条 → 截图 → 恢复
        tab.js("fetch('/resume/5/delete',{method:'POST'})", wait=0.8)
        tab.goto(base + "/trash")
        tab.shot("trash.png")
        tab.js("fetch('/trash/5/restore',{method:'POST'})", wait=0.8)

        # 9. 图表视图
        tab.goto(base + "/charts")
        tab.shot("charts.png")

        # 10. 候选人聚合与其版本页
        tab.goto(base + "/persons")
        tab.shot("persons.png")
        tab.js("""
          const a=[...document.querySelectorAll('a.link')].find(x=>x.textContent.trim()==='张三');
          if(a) a.click();
        """, wait=1.6)
        tab.shot("person-versions.png")

        # 11. 简历详情：筛选历史分页（第 2 页）
        tab.goto(base + "/resume/2?page=2")
        tab.js("document.getElementById('history').scrollIntoView({block:'start'});", wait=0.7)
        tab.shot("history-page2.png")

        # 12. 模板可视化编辑 / 模板管理
        tab.goto(base + "/templates/qa-engineer")
        tab.shot("template-visual.png")
        tab.goto(base + "/templates")
        tab.shot("templates.png")

        # 13. 原文件预览：PDF 内嵌 / docx 回退
        tab.goto(base + "/resume/1")
        tab.js("document.getElementById('preview-open').click();", wait=1.6)
        tab.shot("resume-preview.png")
        tab.goto(base + "/resume/9")
        tab.js("document.getElementById('preview-open').click();", wait=1.2)
        tab.shot("resume-preview-docx.png")

    finally:
        proc.terminate()


if __name__ == "__main__":
    main()
