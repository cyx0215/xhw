#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""阅读页 / 书架的前端冒烟测试（真浏览器）。

可选依赖：需要 Python 版 playwright。没装时整个模块跳过，不影响零依赖的
主测试套件，也不进 CI。运行：

  pip install playwright==<版本>   # 任意 Python 环境
  python -m unittest discover tests/e2e -v

浏览器按 Chromium → 系统 Edge → 系统 Chrome 的顺序找，都没有就跳过；
所以装了 playwright 包、不跑 `playwright install` 也能用系统浏览器。

每个用例用独立的浏览器上下文（localStorage 互不串），站点是临时目录里
现构建的两本小书：一本双线成人向、一本三章长文。
"""

import importlib.util
import io
import shutil
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote

try:
    from playwright.sync_api import sync_playwright
except ImportError:  # 可选依赖
    sync_playwright = None

BASE = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("build", BASE / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)

FORK = "双线书"   # adult + 美/刀双线
PLAIN = "长文书"  # 三章长文，用于位置恢复 / 搜索 / 继续阅读

PARA = "她把雨伞收起来，站在门廊下等了很久，街灯一盏一盏亮起来。"


def _chapter(num, title, body_lines):
    return f"# 第 {num} 章 · {title}\n\n## 正文\n\n" + "\n\n".join(body_lines) + "\n"


def _make_books(root):
    books = root / "books"
    fork = books / FORK / "章节"
    fork.mkdir(parents=True)
    (books / FORK / "book.toml").write_text(
        f'title = "{FORK}"\nadult = true\nfork_label = "顶峰双线"\n', encoding="utf-8")
    fork.joinpath("ch001.md").write_text(
        _chapter(1, "共用开头", [PARA] * 30), encoding="utf-8")
    fork.joinpath("ch002.md").write_text(
        _chapter(2, "分叉", ["美线独有的一段。"] + [PARA] * 30), encoding="utf-8")
    fork.joinpath("ch002-刀版.md").write_text(
        _chapter(2, "分叉", ["刀线独有的一段。"] + [PARA] * 30), encoding="utf-8")
    fork.joinpath("ch003.md").write_text(
        _chapter(3, "合流", [PARA] * 30), encoding="utf-8")

    plain = books / PLAIN / "章节"
    plain.mkdir(parents=True)
    (books / PLAIN / "book.toml").write_text(f'title = "{PLAIN}"\n', encoding="utf-8")
    for n, t in ((1, "起"), (2, "承"), (3, "转")):
        lines = [PARA] * 60
        if n == 3:
            lines[40] = "这一段藏着独一无二的暗号琥珀灯塔。"
        plain.joinpath(f"ch00{n}.md").write_text(_chapter(n, t, lines), encoding="utf-8")


class _QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@unittest.skipIf(sync_playwright is None, "未安装 Python playwright（可选依赖）")
class ReaderSmokeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp = tempfile.TemporaryDirectory()
        root = Path(cls._tmp.name)
        cls._saved = (build.BASE, build.BOOKS_DIR, build.SITE_DIR, build.HISTORY_FILE)
        build.BASE = root
        build.BOOKS_DIR = root / "books"
        build.SITE_DIR = root / "site"
        build.HISTORY_FILE = root / "进度.json"
        shutil.copytree(cls._saved[0] / "templates" / "assets",
                        root / "templates" / "assets")
        _make_books(root)
        with redirect_stdout(io.StringIO()):
            assert build.build(), "临时站点构建失败"

        handler = partial(_QuietHandler, directory=str(build.SITE_DIR))
        cls._httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=cls._httpd.serve_forever, daemon=True).start()
        cls.url = f"http://127.0.0.1:{cls._httpd.server_address[1]}/"

        cls._pw = sync_playwright().start()
        cls._browser = None
        for kw in ({}, {"channel": "msedge"}, {"channel": "chrome"}):
            try:
                cls._browser = cls._pw.chromium.launch(**kw)
                break
            except Exception:
                continue
        if cls._browser is None:
            cls._teardown()
            raise unittest.SkipTest("找不到可用浏览器（Chromium / Edge / Chrome）")

    @classmethod
    def _teardown(cls):
        if getattr(cls, "_browser", None):
            cls._browser.close()
        if getattr(cls, "_pw", None):
            cls._pw.stop()
        cls._httpd.shutdown()
        cls._httpd.server_close()
        (build.BASE, build.BOOKS_DIR, build.SITE_DIR, build.HISTORY_FILE) = cls._saved
        cls._tmp.cleanup()

    @classmethod
    def tearDownClass(cls):
        cls._teardown()

    def setUp(self):
        # 独立上下文：localStorage 互不影响；手机宽度，覆盖主要场景
        self.ctx = self._browser.new_context(viewport={"width": 390, "height": 844},
                                             reduced_motion="reduce")
        self.page = self.ctx.new_page()
        self.errors = []
        self.page.on("pageerror", lambda e: self.errors.append(str(e)))

    def tearDown(self):
        self.ctx.close()
        self.assertEqual(self.errors, [], "页面脚本报错")

    # ---- 工具
    def book_url(self, title, hash_=""):
        return self.url + quote(f"{title}.html") + hash_

    def open_book(self, title, hash_="", adult_ok=True):
        if adult_ok:
            # 先落到同源页面再写 localStorage（file 以外的 origin 才有存储）
            self.page.goto(self.url + "index.html")
            self.page.evaluate("localStorage.setItem('xhw-adult-gate','1')")
        self.page.goto(self.book_url(title, hash_))
        self.page.wait_for_load_state("load")

    def js(self, expr):
        return self.page.evaluate(expr)

    # ---- 用例
    def test_adult_gate_remembered_after_confirm(self):
        self.open_book(FORK, adult_ok=False)
        self.assertTrue(self.js("document.getElementById('gate').classList.contains('on')"))
        self.page.click("#gate-ok")
        self.assertFalse(self.js("document.getElementById('gate').classList.contains('on')"))
        self.page.reload()
        self.page.wait_for_load_state("load")
        self.assertFalse(self.js("document.getElementById('gate').classList.contains('on')"))

    def test_chosen_path_survives_reload(self):
        self.open_book(FORK)
        self.assertEqual(self.js("document.body.dataset.path"), "mei")
        self.page.click(".path-btn[data-path=dao]")
        self.assertEqual(self.js("document.body.dataset.path"), "dao")
        self.page.reload()
        self.page.wait_for_load_state("load")
        self.assertEqual(self.js("document.body.dataset.path"), "dao")
        # 当前线只露出刀线分叉章，美线那章（含章外的横线与导航）全部隐藏
        visible = self.js("""[...document.querySelectorAll('.chapter')]
            .filter(c=>getComputedStyle(c).display!=='none').map(c=>c.id)""")
        self.assertEqual(visible, ["ch-1", "ch-2-dao", "ch-3"])
        ends = self.js("""[...document.querySelectorAll('.ch-end')]
            .filter(e=>getComputedStyle(e).display!=='none').length""")
        self.assertEqual(ends, 3)

    def test_reading_position_restored(self):
        self.open_book(PLAIN)
        # 跳到第 2 章中段
        self.js("""(()=>{const c=document.getElementById('ch-2');
            scrollTo(0, c.offsetTop + c.offsetHeight*0.5);})()""")
        self.page.wait_for_timeout(300)
        before = self.js("scrollY")
        self.page.reload()  # pagehide 时存档
        self.page.wait_for_load_state("load")
        self.page.wait_for_timeout(400)
        self.assertIn("第 2 章", self.js("document.getElementById('now').textContent"))
        self.assertLess(abs(self.js("scrollY") - before), 120)

    def test_search_jumps_to_hit(self):
        self.open_book(PLAIN)
        self.page.keyboard.press("Control+k")
        self.page.keyboard.type("琥珀灯塔")
        self.page.wait_for_selector(".sr:not(.none)")
        self.assertEqual(self.js("document.querySelectorAll('.sr:not(.none)').length"), 1)
        self.assertIn("第 3 章", self.js("document.querySelector('.sr-ch').textContent"))
        self.page.click(".sr")
        self.page.wait_for_timeout(400)
        # 命中段落要落在工具条（46px）以下的可视区，而不是被工具条盖住
        in_view = self.js("""(()=>{const p=[...document.querySelectorAll('.p')]
            .find(p=>p.textContent.includes('琥珀灯塔'));
            const r=p.getBoundingClientRect(),
                  bar=document.getElementById('tools').getBoundingClientRect().bottom;
            return r.top>=bar && r.bottom<=innerHeight;})()""")
        self.assertTrue(in_view)
        self.assertTrue(self.js("document.getElementById('search-box').hidden"))

    def test_prefs_panel_persists(self):
        self.open_book(PLAIN)
        self.page.click("#prefs-btn")
        self.assertFalse(self.js("document.getElementById('prefs').hidden"))
        self.page.click("[data-set-theme=sepia]")
        self.page.click("[data-set-lh=loose]")
        self.page.click("[data-set-font=sans]")
        self.page.click("#font-inc")
        self.page.keyboard.press("Escape")
        self.assertTrue(self.js("document.getElementById('prefs').hidden"))
        self.page.reload()
        self.page.wait_for_load_state("load")
        got = self.js("""(()=>{const h=document.documentElement;
            return [h.dataset.theme, h.dataset.tone, h.dataset.lh, h.dataset.font,
                    h.style.getPropertyValue('--font-scale')];})()""")
        self.assertEqual(got, ["sepia", "light", "loose", "sans", "1.14"])
        # 背景主题是全站的：书架也跟着变
        self.page.goto(self.url + "index.html")
        self.assertEqual(self.js("document.documentElement.dataset.theme"), "sepia")

    def test_shelf_shows_resume_progress(self):
        self.open_book(PLAIN)
        self.js("""(()=>{const c=document.getElementById('ch-2');
            scrollTo(0, c.offsetTop + 10);})()""")
        self.page.wait_for_timeout(300)
        self.page.goto(self.url + "index.html")  # 离开阅读页时存档
        self.page.wait_for_load_state("load")
        text = self.js(f"""(()=>{{const c=document.querySelector('.card[data-book="{PLAIN}"]');
            const r=c.querySelector('.resume'); return r.hidden?'':r.textContent;}})()""")
        self.assertIn("读到第 2 章 · 承", text)
        # 没读过的书不显示
        other = self.js(f"""document.querySelector('.card[data-book="{FORK}"] .resume').hidden""")
        self.assertTrue(other)


if __name__ == "__main__":
    unittest.main()
