#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""build.py 纯函数层的回归测试。运行：python -m unittest discover tests -v

零依赖（unittest 是标准库）。这些用例来自 2026-09 代码审查时实测过的
解析器边界：TOML 类型三连雷、ch1-2 卷章命名、无连字符版本标签、
锚点碰撞、防泄漏兜底——改动解析逻辑前先跑一遍。
"""

import importlib.util
import io
import json
import re
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

BASE = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("build", BASE / "build.py")
build = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(build)


def write(tmp, name, text):
    p = Path(tmp) / name
    p.write_text(text, encoding="utf-8")
    return p


class ParseTomlTest(unittest.TestCase):
    def test_escaped_quote_kept(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "book.toml", 'title = "他说\\"hi\\"了"\n')
            self.assertEqual(build.parse_toml(p)["title"], '他说"hi"了')

    def test_int_and_float(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "book.toml", "order = 3\nratio = 1.5\n")
            d = build.parse_toml(p)
            self.assertEqual(d["order"], 3)
            self.assertIsInstance(d["order"], int)
            self.assertEqual(d["ratio"], 1.5)
            self.assertIsInstance(d["ratio"], float)

    def test_bool_case_insensitive(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "book.toml", "adult = False\nflag = TRUE\n")
            d = build.parse_toml(p)
            self.assertIs(d["adult"], False)
            self.assertIs(d["flag"], True)

    def test_table_headers_skipped_not_flattened(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "book.toml",
                      'title = "书名"\n[author]\ntitle = "盖掉"\nname = "x"\n')
            buf = io.StringIO()
            with redirect_stdout(buf):
                d = build.parse_toml(p)
            self.assertEqual(d["title"], "书名")
            self.assertNotIn("name", d)
            self.assertIn("暂不支持", buf.getvalue())

    def test_comment_after_string_value(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "book.toml", 'tagline = "简介" # 这是注释\n')
            self.assertEqual(build.parse_toml(p)["tagline"], "简介")

    def test_hash_inside_string_is_not_comment(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = write(tmp, "book.toml", 'tagline = "标题 #1"\n')
            self.assertEqual(build.parse_toml(p)["tagline"], "标题 #1")

    def test_load_meta_order_type_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            write(d, "book.toml", 'order = "abc"\n')
            buf = io.StringIO()
            with redirect_stdout(buf):
                meta = build.load_meta(d)
            self.assertEqual(meta["order"], 99)

    def test_missing_file_returns_empty(self):
        self.assertEqual(build.parse_toml(Path("某处/不存在.toml")), {})


class ChapterStemTest(unittest.TestCase):
    def test_plain(self):
        self.assertEqual(build.parse_chapter_stem("ch001"), (None, 1, ""))

    def test_branch_tag(self):
        self.assertEqual(build.parse_chapter_stem("ch014-刀版"), (None, 14, "刀版"))
        self.assertEqual(build.parse_chapter_stem("ch015-美版"), (None, 15, "美版"))

    def test_branch_tag_without_hyphen(self):
        # 曾被静默丢成第 14 章默认版
        self.assertEqual(build.parse_chapter_stem("ch14美版"), (None, 14, "美版"))

    def test_volume_chapter_naming(self):
        # README 契约：ch1-2 =「卷1第2章」，最后一段数字是章号
        self.assertEqual(build.parse_chapter_stem("ch1-2"), (1, 2, ""))
        self.assertEqual(build.parse_chapter_stem("ch2-014"), (2, 14, ""))

    def test_volume_chapter_with_tag(self):
        self.assertEqual(build.parse_chapter_stem("ch1-2-刀版"), (1, 2, "刀版"))

    def test_no_digits(self):
        self.assertEqual(build.parse_chapter_stem("ch备注"), (None, None, ""))

    def test_branch_of_tag(self):
        self.assertEqual(build.branch_of_tag("刀版"), ("dao", "刀"))
        self.assertEqual(build.branch_of_tag("美版"), ("mei", "美"))
        self.assertEqual(build.branch_of_tag("")[0], "mei")


class InlineTest(unittest.TestCase):
    def test_bold_italic_nesting(self):
        # 曾产出交叉嵌套的 <strong><em>…</strong></em>
        self.assertEqual(
            build.inline("***加粗斜体***"),
            "<strong><em>加粗斜体</em></strong>",
        )

    def test_bold_and_italic(self):
        self.assertEqual(build.inline("**粗** *斜*"), "<strong>粗</strong> <em>斜</em>")

    def test_html_escaped(self):
        self.assertEqual(build.inline("<script>&"), "&lt;script&gt;&amp;")


class ExtractBodyTest(unittest.TestCase):
    def test_with_body_marker(self):
        text = "# 第 1 章 测试\n> 状态：草稿\n## 本章目标\n秘密\n## 正文\n正文A\n## 章后笔记\n后记"
        self.assertEqual(build.extract_body(text).strip(), "正文A")

    def test_fallback_truncates_at_first_h2(self):
        # 无 ## 正文 时，h2 视作笔记开头，防整段笔记发布
        text = "# 第 1 章 测试\n正文行\n## 本章目标\n这行绝不能出现"
        body = build.extract_body(text)
        self.assertIn("正文行", body)
        self.assertNotIn("本章目标", body)
        self.assertNotIn("绝不能出现", body)

    def test_no_title_no_body_raises(self):
        with self.assertRaises(ValueError):
            build.extract_body("## 本章目标\n全是笔记，没有章标题也没有正文分隔")

    def test_no_space_marker_also_matches(self):
        text = "# 第 1 章 x\n##正文\n内容"
        self.assertIn("内容", build.extract_body(text))

    def test_no_space_h2_note_truncated(self):
        # 回归：起始侧认 ##正文，终止侧却要求 ## 后有空白 → 无空格笔记整段泄漏
        text = "# 第 1 章 x\n## 正文\n正文A\n##本章目标\n绝不能出现\n更多笔记"
        body = build.extract_body(text)
        self.assertIn("正文A", body)
        self.assertNotIn("绝不能出现", body)
        self.assertNotIn("本章目标", body)

    def test_h3_prose_title_not_section(self):
        # 回归：编号后必须有分隔符或括号，散文式 h3 不能被拆成编号+标题
        with tempfile.TemporaryDirectory() as tmp:
            ch = build.parse_chapter(write(
                tmp, "ch001.md", "# 第 1 章 x\n## 正文\n### 十年后的信\n内容"))
            self.assertFalse([b for b in ch["blocks"] if b[0] == "section"])
            ch = build.parse_chapter(write(
                tmp, "ch002.md", "# 第 2 章 x\n## 正文\n### （一）落地\n内容"))
            sections = [b for b in ch["blocks"] if b[0] == "section"]
            self.assertEqual(sections, [("section", ("一", "落地"))])


class ParseChapterTest(unittest.TestCase):
    def _parse(self, tmp, name, text):
        return build.parse_chapter(write(tmp, name, text))

    def test_h3_arabic_section(self):
        # ### 1、重逢 曾被静默丢弃
        with tempfile.TemporaryDirectory() as tmp:
            ch = self._parse(tmp, "ch001.md", "# 第 1 章 x\n## 正文\n### 1、重逢\n内容")
            sections = [b for b in ch["blocks"] if b[0] == "section"]
            self.assertEqual(sections, [("section", ("1", "重逢"))])

    def test_h3_cn_section_with_parens(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = self._parse(tmp, "ch001.md", "# 第 1 章 x\n## 正文\n### （一）落地\n内容")
            sections = [b for b in ch["blocks"] if b[0] == "section"]
            self.assertEqual(sections, [("section", ("一", "落地"))])

    def test_stem_without_digits_and_no_title_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                self._parse(tmp, "ch备注.md", "正文没有标题")

    def test_hundred_section_mark(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = self._parse(tmp, "ch001.md", "# 第 1 章 x\n## 正文\n一百\n内容")
            sections = [b for b in ch["blocks"] if b[0] == "section"]
            self.assertEqual(len(sections), 1)

    def test_bom_file_title_recognized(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ch001.md"
            p.write_bytes(b"\xef\xbb\xbf# \xe7\xac\xac 1 \xe7\xab\xa0 \xe6\xb5\x8b\n"
                          b"## \xe6\xad\xa3\xe6\x96\x87\n\xe5\x86\x85\xe5\xae\xb9")
            ch = build.parse_chapter(p)
            self.assertEqual(ch["num"], 1)
            self.assertEqual(ch["title"], "测")

    def test_bodyless_flag(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = self._parse(tmp, "ch001.md", "# 第 1 章 x\n正文")
            self.assertTrue(ch["bodyless"])


class CleanTitleTest(unittest.TestCase):
    def test_strips_trailing_tag(self):
        self.assertEqual(build.clean_display_title("像相爱那样（原版·美）"), "像相爱那样")
        self.assertEqual(build.clean_display_title("像过去一样（刀版）"), "像过去一样")

    def test_keeps_mid_title_word(self):
        # （美）在词中间是正常用词，不剥
        self.assertEqual(build.clean_display_title("挑战（美）丽的"), "挑战（美）丽的")


class CnNumTest(unittest.TestCase):
    def test_cases(self):
        for s, n in [("12", 12), ("一", 1), ("十", 10), ("十三", 13),
                     ("二十三", 23), ("一百", 100), ("零", 0)]:
            self.assertEqual(build.cn_num(s), n, s)


class LoadBookAnchorsTest(unittest.TestCase):
    """锚点唯一性：分卷独立章号、同章多版本不得产生重复 DOM id。"""

    def _make_book(self, tmp, files, meta='title = "测试书"\n'):
        d = Path(tmp) / "书"
        (d / "章节").mkdir(parents=True)
        (d / "book.toml").write_text(meta, encoding="utf-8")
        for name, text in files.items():
            p = d / "章节" / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return d

    CH = "# 第 {} 章 测\n## 正文\n内容\n"

    def test_two_volumes_same_chapter_number(self):
        # 卷一与卷二各放 ch001.md：曾生成两个重复的 id="ch-1"
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_book(tmp, {
                "卷一/ch001.md": self.CH.format(1),
                "卷二/ch001.md": self.CH.format(1),
            })
            buf = io.StringIO()
            with redirect_stdout(buf):
                book = build.load_book(d)
            anchors = [c["anchor"] for c in book["chapters"]]
            self.assertEqual(len(anchors), len(set(anchors)))
            self.assertEqual(book["chapter_count"], 2)
            self.assertEqual([c["volume"] for c in book["chapters"]], ["卷一", "卷二"])

    def test_same_branch_duplicate_versions_warns(self):
        # ch014.md 与 ch014-美版.md 同属美线：警告 + 锚点不重复
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_book(tmp, {
                "ch014.md": self.CH.format(14),
                "ch014-美版.md": self.CH.format(14),
            })
            buf = io.StringIO()
            with redirect_stdout(buf):
                book = build.load_book(d)
            anchors = [c["anchor"] for c in book["chapters"]]
            self.assertEqual(len(anchors), len(set(anchors)))
            self.assertIn("疑似重复保存", buf.getvalue())

    def test_volume_chapter_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_book(tmp, {
                "ch1-2.md": self.CH.format(2),
                "ch1-3.md": self.CH.format(3),
            })
            buf = io.StringIO()
            with redirect_stdout(buf):
                book = build.load_book(d)
            self.assertEqual([c["num"] for c in book["chapters"]], [2, 3])
            self.assertEqual({c["volume"] for c in book["chapters"]}, {"卷1"})
            self.assertEqual(book["chapter_count"], 2)

    def test_unnamed_chapter_file_skipped_with_warning(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = self._make_book(tmp, {
                "ch001.md": self.CH.format(1),
                "序章.md": "# 序\n正文",
            })
            buf = io.StringIO()
            with redirect_stdout(buf):
                book = build.load_book(d)
            self.assertEqual(book["chapter_count"], 1)
            self.assertIn("未按 chNNN 命名", buf.getvalue())


class ForkInfoTest(unittest.TestCase):
    def test_labels_and_anchors_derived_from_chapters(self):
        book = {
            "chapters": [
                {"num": 13, "branch": None, "anchor": "ch-13", "title": "a",
                 "branch_label": "", "blocks": [], "volume": ""},
                {"num": 14, "branch": "mei", "anchor": "ch-14-mei", "title": "b",
                 "branch_label": "美", "blocks": [], "volume": ""},
                {"num": 14, "branch": "dao", "anchor": "ch-14-dao", "title": "c",
                 "branch_label": "刀", "blocks": [], "volume": ""},
            ],
            "raw_meta": {"fork_label": "顶峰双线",
                         "line_note_mei": "像相爱", "line_note_dao": "来电处刑"},
            "fork_label": "顶峰双线",
        }
        info = build.fork_info(book)
        self.assertEqual(info["label"], "顶峰双线")
        self.assertEqual(info["range"], "14")
        self.assertEqual(info["lines"][0], ("mei", "美", "ch-14-mei", "像相爱"))
        self.assertEqual(info["lines"][1], ("dao", "刀", "ch-14-dao", "来电处刑"))
        card = build.render_path_pick(info)
        self.assertIn("顶峰双线", card)
        self.assertIn('data-go="ch-14-dao"', card)

    def test_unknown_branch_order_matches_default_path(self):
        """未知标签（甲/乙）：选线卡首线必须等于 default_path，且与集合顺序无关。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "书"
            (d / "章节").mkdir(parents=True)
            (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
            for tag in ("乙版", "甲版"):
                (d / "章节" / f"ch001-{tag}.md").write_text(
                    f"# 第 1 章 {tag}\n## 正文\n{tag}正文", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                book = build.load_book(d)
            info = build.fork_info(book)
            keys = [k for k, *_ in info["lines"]]
            self.assertEqual(keys, sorted(keys, key=build.branch_order_key))
            self.assertEqual(keys[0], book["default_path"])


class ChapterSourceTest(unittest.TestCase):
    """章节源文件随章记录：双线同章号不能串到另一条线的文件。"""

    def _fork_book(self, tmp):
        d = Path(tmp) / "books" / "书"
        (d / "章节" / "卷一").mkdir(parents=True)
        (d / "章节" / "卷二").mkdir()
        (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
        (d / "章节" / "卷一" / "ch001.md").write_text(
            "# 第 1 章 一\n## 正文\n卷一正文", encoding="utf-8")
        (d / "章节" / "卷二" / "ch001-甲版.md").write_text(
            "# 第 1 章 甲\n## 正文\n甲线正文", encoding="utf-8")
        (d / "章节" / "卷二" / "ch001-乙版.md").write_text(
            "# 第 1 章 乙\n## 正文\n乙线正文", encoding="utf-8")
        return d

    def test_export_platform_md_uses_default_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = build.BASE
            try:
                build.BASE = Path(tmp)
                d = self._fork_book(tmp)
                with redirect_stdout(io.StringIO()):
                    book = build.load_book(d)
                # 故意选排序在后的那条线，证明不是「第一个文件」碰巧对上
                keys = sorted({c["branch"] for c in book["chapters"] if c["branch"]},
                              key=build.branch_order_key)
                self.assertEqual(keys, ["乙版", "甲版"])
                book["default_path"] = "甲版"
                html_path, md_path = build.export_platform(book)
                md = md_path.read_text(encoding="utf-8")
                self.assertIn("甲线正文", md)
                self.assertNotIn("乙线正文", md)
                self.assertIn("甲线正文", html_path.read_text(encoding="utf-8"))
            finally:
                build.BASE = saved

    def test_rss_mtime_per_volume_and_line(self):
        import os
        with tempfile.TemporaryDirectory() as tmp:
            saved = (build.SITE_DIR,)
            try:
                build.SITE_DIR = Path(tmp) / "site"
                build.SITE_DIR.mkdir()
                d = self._fork_book(tmp)
                os.utime(d / "章节" / "卷一" / "ch001.md", (1_000_000_000, 1_000_000_000))
                os.utime(d / "章节" / "卷二" / "ch001-甲版.md",
                         (1_700_000_000, 1_700_000_000))
                with redirect_stdout(io.StringIO()):
                    book = build.load_book(d)
                book["file"] = "书.html"
                build.build_chapters_site([book], site_url="https://x.example.com")
                rss = (build.SITE_DIR / "chapters" / "书" / "feed.xml").read_text(
                    encoding="utf-8")
                # 卷一 ch001 保持自己的 2001 年时间，不被卷二的改动顶掉
                self.assertIn("2001", rss)
            finally:
                (build.SITE_DIR,) = saved


class ServeSafetyTest(unittest.TestCase):
    def test_rejects_drive_letter_traversal(self):
        # Windows 上 /C:/Windows/win.ini 曾能穿透 site/ 读任意文件
        self.assertIsNone(build.resolve_under_site("/C:/Windows/win.ini"))
        self.assertIsNone(build.resolve_under_site("/../../etc/passwd"))
        self.assertIsNone(build.resolve_under_site("/..%2F..%2Fbooks/设定/x.md"))

    def test_accepts_site_files(self):
        target = build.resolve_under_site("/index.html")
        self.assertIsNotNone(target)
        self.assertEqual(target.name, "index.html")
        self.assertIsNotNone(build.resolve_under_site("/"))

    def test_host_allowlist(self):
        self.assertTrue(build.host_allowed("127.0.0.1:8765"))
        self.assertTrue(build.host_allowed("localhost:8765"))
        self.assertFalse(build.host_allowed("evil.example.com"))
        self.assertFalse(build.host_allowed(""))
        self.assertFalse(build.host_allowed(None))


class ExportTest(unittest.TestCase):
    """EPUB / TXT 导出：结构合规、不含创作笔记、双线书只取默认线。"""

    def _book(self, tmp, files, meta='title = "导出书"\ntagline = "简介"\n'):
        d = Path(tmp) / "bk"
        (d / "章节").mkdir(parents=True)
        (d / "book.toml").write_text(meta, encoding="utf-8")
        for name, text in files.items():
            (d / "章节" / name).write_text(text, encoding="utf-8")
        buf = io.StringIO()
        with redirect_stdout(buf):
            return build.load_book(d)

    CH = ("# 第 {n} 章 标{nn}\n> 状态：草稿\n## 本章目标\n绝密目标\n"
          "## 正文\n第{nn}段正文。\n> 消息气泡\n（第 {n} 章 · 终）\n")

    def test_txt_excludes_notes(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self._book(tmp, {
                "ch001.md": self.CH.format(n=1, nn="一"),
                "ch002.md": self.CH.format(n=2, nn="二"),
            })
            txt = build.export_txt(book)
            self.assertIn("第 1 章", txt)
            self.assertIn("第一段正文。", txt)
            self.assertIn("消息气泡", txt)
            self.assertNotIn("本章目标", txt)
            self.assertNotIn("状态：草稿", txt)
            self.assertIn("（第 2 章 · 终）", txt)

    def test_txt_escapes_markdown(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self._book(tmp, {
                "ch001.md": "# 第 1 章 x\n## 正文\n**粗**和*斜*\n"})
            txt = build.export_txt(book)
            self.assertIn("粗", txt)
            self.assertNotIn("**", txt)
            self.assertNotIn("<strong>", txt)

    def test_epub_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self._book(tmp, {
                "ch001.md": self.CH.format(n=1, nn="一"),
                "ch002.md": self.CH.format(n=2, nn="二"),
            })
            out = Path(tmp) / "book.epub"
            build.export_epub(book, out)
            with zipfile.ZipFile(out) as z:
                names = z.namelist()
                self.assertEqual(names[0], "mimetype")
                self.assertEqual(z.getinfo("mimetype").compress_type, zipfile.ZIP_STORED)
                self.assertEqual(z.read("mimetype").decode(), "application/epub+zip")
                opf = z.read("OEBPS/content.opf").decode()
                self.assertIn("urn:xhw:", opf)
                self.assertIn("<dc:language>zh-CN</dc:language>", opf)
                self.assertEqual(len(re.findall(r"<itemref", opf)), 3)  # nav + 2 章
                nav = z.read("OEBPS/nav.xhtml").decode()
                self.assertEqual(nav.count("<li>"), 2)
                self.assertIn("properties=\"nav\"", opf)
                ch1 = z.read("OEBPS/ch001.xhtml").decode()
                self.assertNotIn("绝密目标", ch1)
                self.assertIn("<h2>", ch1)
                self.assertNotIn("<br>", ch1)  # XHTML 必须自闭合

    def test_epub_dual_line_takes_default_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            book = self._book(tmp, {
                "ch014.md": "# 第 14 章 标\n## 正文\n美线内容",
                "ch014-刀版.md": "# 第 14 章 标\n## 正文\n刀线内容",
            })
            out = Path(tmp) / "book.epub"
            build.export_epub(book, out)
            with zipfile.ZipFile(out) as z:
                all_xhtml = "".join(
                    z.read(n).decode() for n in z.namelist() if n.endswith(".xhtml")
                )
            self.assertIn("美线内容", all_xhtml)
            self.assertNotIn("刀线内容", all_xhtml)

    def test_export_field_parsing(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp)
            (d / "book.toml").write_text("export = \"none\"\n", encoding="utf-8")
            self.assertEqual(build.load_meta(d)["export"], "none")


class MiscTest(unittest.TestCase):
    def test_safe_filename_reserved_names(self):
        self.assertEqual(build.safe_filename("CON"), "_CON")
        self.assertEqual(build.safe_filename("a/b:c"), "a_b_c")
        self.assertEqual(build.safe_filename(""), "未命名")

    def test_author_leak_covers_section_subtitle(self):
        book = {"title": "测试", "chapters": [
            {"num": 1, "blocks": [("section", ("一", "本章目标"))]}]}
        buf = io.StringIO()
        with redirect_stdout(buf):
            build.check_author_leak(book)
        self.assertIn("疑似泄漏", buf.getvalue())

    def test_volume_order(self):
        self.assertEqual(build.volume_order("卷一"), 1)
        self.assertEqual(build.volume_order("卷2"), 2)
        self.assertEqual(build.volume_order("第三卷"), 3)
        self.assertEqual(build.volume_order("番外"), 9999)


class WritingAidsTest(unittest.TestCase):
    """批次三/四：lint 检查函数、素材索引、脚手架、写作上下文。"""

    def _make_book(self, tmp, name="书", files=None, meta='title = "书"\n'):
        d = Path(tmp) / "books" / name
        (d / "章节").mkdir(parents=True)
        (d / "设定").mkdir()
        (d / "笔记").mkdir()
        (d / "book.toml").write_text(meta, encoding="utf-8")
        for rel, text in (files or {}).items():
            p = d / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text, encoding="utf-8")
        return d

    CH = "# 第 {} 章 标{}\n> 状态：草\n## 本章目标\n- 目标{}\n## 正文\n正文{}\n"

    def test_profile_names_filters_structure_headings(self):
        text = """# 人物设定
## 总览
## 女主：林知夏
### 外貌细项（写作锚点）
### 许宴眼里的她
## 男主 · 许宴
## 主要人物总表（10 人）
## 为什么配
"""
        self.assertEqual(build.profile_names(text), ["林知夏", "许宴"])

    def test_timeline_max_chapter(self):
        text = "- ch010 中点\n- 第 16 章 决战\n- ch020 终"
        self.assertEqual(build.timeline_max_chapter(text), 20)

    def test_parse_foreshadow_rows(self):
        text = """| 编号 | 埋点 | 状态 |
|------|------|------|
| F1 | 公约 | 已回收 |
| F2 | 床垫 | 待埋 |
"""
        rows = build.parse_foreshadow_rows(text)
        self.assertEqual(rows, [("F1", "已回收"), ("F2", "待埋")])

    def test_lint_flags_inconsistent_book(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = self._make_book(root, "坏书", files={
                "章节/ch001.md": "# 第 1 章 标\n## 本章目标\n- 目标\n"
                                 "## 正文\n丙丁出场了\n",
                "设定/人物.md": "## 主角：甲乙\n## 丙丁",
            }, meta='title = "坏书"\nstatus = "写完了"\n')
            buf = io.StringIO()
            with redirect_stdout(buf):
                errors = build.lint(books_dir=root / "books")
            out = buf.getvalue()
            self.assertGreaterEqual(errors, 1)
            self.assertIn("status", out)
            self.assertIn("甲乙", out)  # 正文没有的名字应被点名
            self.assertNotIn("丙丁", out)  # 正文出现过的名字不报

    def test_lint_issues_structured_and_print_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self._make_book(root, "坏书", files={
                "章节/ch001.md": "# 第 1 章 标\n## 正文\n正文\n",
            }, meta='title = "坏书"\nstatus = "写完了"\n')
            buf = io.StringIO()
            with redirect_stdout(buf):
                errors = build.lint(books_dir=root / "books", materials_base=root / "素材")
            issues = build.lint_issues(books_dir=root / "books",
                                       materials_base=root / "素材")
            self.assertEqual(sum(1 for _, lv, _ in issues if lv == "err"), errors)
            self.assertTrue(any(s == "坏书" and lv == "err" and "status" in m
                                for s, lv, m in issues))
            # 每条结构化问题都在打印输出里出现过
            for _, _, m in issues:
                self.assertIn(m, buf.getvalue())

    def test_render_dashboard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = self._make_book(root, "看板书", files={
                "章节/ch001.md": "# 第 1 章 标\n> 字数目标：4k–6k\n## 正文\n短\n",
                "笔记/伏笔.md": "| 编号 | 状态 |\n|---|---|\n| F1<b> | 未回收 |\n| F2 | 已收 |\n",
                "笔记/待改.md": "- [ ] 第一章开头太慢 <script>\n- [x] 已改好的\n",
            }, meta='title = "看板书"\n')
            with redirect_stdout(io.StringIO()):
                book = build.load_book(d)
            book["file"] = "看板书.html"
            history = {"books": {"2026-09-01": {"看板书": 100},
                                 "2026-09-02": {"看板书": 900}}}
            out = build.render_dashboard(
                [book], [("看板书", "warn", "示例警告"), ("素材库", "err", "素材坏了")],
                history)
            self.assertIn("未回收伏笔 · 1 / 2", out)
            self.assertIn("F1&lt;b&gt;", out)            # 账本内容已转义
            self.assertIn("开头太慢 &lt;script&gt;", out)  # 待改原文已转义
            self.assertIn("待改 · 未完成 1 · 已勾 1", out)
            self.assertNotIn("已改好的", out)             # 勾掉的不再摊开
            self.assertNotIn("<script>\n", out.split("<main")[1].split("</main>")[0])
            self.assertIn("目标 4000–6000", out)           # 字数目标偏差
            self.assertIn('class="spark"', out)            # 两个快照点出折线
            self.assertIn("+800 字", out)
            self.assertIn("示例警告", out)
            self.assertIn("素材坏了", out)
            self.assertIn('name="robots" content="noindex"', out)

    def test_dash_route_is_not_a_site_file(self):
        self.assertTrue(build.is_dash_route("/_dash"))
        self.assertTrue(build.is_dash_route("/_dash/?t=1"))
        self.assertFalse(build.is_dash_route("/_dash.html"))
        self.assertFalse(build.is_dash_route("/index.html"))
        # 看板路由不经过 site/ 解析，也不会让 resolve_under_site 放行越界路径
        self.assertIsNone(build.resolve_under_site("/_dash/../../build.py"))

    def test_lint_flags_unresolved_and_half_foreshadow(self):
        """「未回收」不含子串「未收」，「半收」是模板状态——完本书都要报。"""
        for state in ("已埋 · 未回收", "半收"):
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                self._make_book(root, "完本", files={
                    "章节/ch001.md": "# 第 1 章 标\n## 正文\n正文\n",
                    "笔记/伏笔.md": f"| 编号 | 状态 |\n|---|---|\n| F1 | {state} |\n",
                }, meta='title = "完本"\nstatus = "已完结"\n')
                buf = io.StringIO()
                with redirect_stdout(buf):
                    build.lint(books_dir=root / "books", materials_base=root / "素材")
                self.assertIn("完本书仍有 1 条伏笔", buf.getvalue(), state)

    def test_new_book_creates_skeleton_without_chapters(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            books = root / "books"
            books.mkdir()
            templates = root / "templates"
            templates.mkdir()
            (templates / "book.toml").write_text(
                'title = "书名"\nadult = false\n', encoding="utf-8")
            for n in ("大纲", "人物", "时间线"):
                (templates / f"{n}模板.md").write_text(f"# {n}模板\n", encoding="utf-8")
            for n in ("伏笔", "待改", "灵感"):
                (templates / f"{n}模板.md").write_text(f"# {n}模板\n", encoding="utf-8")
            saved = (build.BASE, build.BOOKS_DIR)
            try:
                build.BASE = root
                build.BOOKS_DIR = books
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = build.new_book("新书", adult=True)
                self.assertTrue(ok)
                self.assertTrue((books / "新书" / "book.toml").exists())
                toml = (books / "新书" / "book.toml").read_text(encoding="utf-8")
                self.assertIn('title = "新书"', toml)
                self.assertIn("adult = true", toml)
                self.assertTrue((books / "新书" / "章节").is_dir())
                self.assertFalse(list((books / "新书" / "章节").glob("*.md")))
                # 重复创建要失败
                self.assertFalse(build.new_book("新书"))
            finally:
                build.BASE, build.BOOKS_DIR = saved

    def test_material_index_roundtrip(self):
        cards = [
            {"cat": "调研", "name": "20260728-a.md", "file": "调研/20260728-a.md",
             "date": "2026-07-28", "title": "风向", "state": "已消化",
             "heat": "", "prio": "高"},
            {"cat": "题材", "name": "20260728-b.md", "file": "题材/20260728-b.md",
             "date": "2026-07-28", "title": "卡B", "state": "已用（《X》）",
             "heat": "爆", "prio": ""},
            {"cat": "人物", "name": "20260728-c.md", "file": "人物/20260728-c.md",
             "date": "2026-07-28", "title": "角色C", "state": "待看",
             "heat": "", "prio": ""},
        ]
        md = build.render_material_index(cards)
        self.assertIn("| 2026-07-28 | 风向 | 高 | 已消化 |", md)
        self.assertIn("已用（《X》）", md)
        self.assertIn("### 可用", md)
        self.assertIn("角色C", md)

    def test_collect_material_cards_skips_templates(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = build.BASE
            try:
                build.BASE = Path(tmp)
                mats = Path(tmp) / "素材" / "题材"
                mats.mkdir(parents=True)
                (mats / "20260728-x.md").write_text(
                    "# 题材 · X\n> 状态：待看\n", encoding="utf-8")
                (mats / "_模板.md").write_text("# 模板\n> 状态：待看\n", encoding="utf-8")
                cards = build.collect_material_cards()
                self.assertEqual([c["name"] for c in cards], ["20260728-x.md"])
                self.assertEqual(cards[0]["state"], "待看")
            finally:
                build.BASE = saved

    def test_build_context_assembles_bible(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = (build.BASE, build.BOOKS_DIR)
            try:
                build.BASE = root
                build.BOOKS_DIR = root / "books"
                self._make_book(root, " Context 书", files={
                    "章节/ch001.md": self.CH.format(1, "一", "一", "一"),
                    "章节/ch002.md": self.CH.format(2, "二", "二", "二"),
                    "设定/大纲.md": "# 大纲\n三幕",
                    "笔记/伏笔.md": "| F1 | 床垫 | 已回收 |",
                }, meta='title = "Context 书"\ntagline = "简介"\n')
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = build.build_context("Context 书")
                self.assertTrue(ok)
                out = (root / "素材" / "上下文" / "Context 书"
                       / "ch003-上下文.md").read_text(encoding="utf-8")
                self.assertIn("第 3 章", out)
                self.assertIn("三幕", out)
                self.assertIn("已回收", out)
                self.assertIn("目标二", out)
                self.assertIn("正文二", out)
                self.assertNotIn("状态：草", out)  # 章头元信息不该进上下文
            finally:
                build.BASE, build.BOOKS_DIR = saved


class ReviewFixTest(unittest.TestCase):
    """2026-09 评审修复的回归测试。"""

    def test_host_allowed_ipv6(self):
        self.assertTrue(build.host_allowed("[::1]:8765"))
        self.assertTrue(build.host_allowed("::1"))
        self.assertFalse(build.host_allowed("[::1"))  # 括号未闭合，仍拒

    def test_record_history_merges_and_survives_corruption(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = build.HISTORY_FILE
            hist = Path(tmp) / "进度.json"
            build.HISTORY_FILE = hist
            try:
                hist.write_text('{"days": {"2026-09-24": 100}}', encoding="utf-8")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    data = build.record_history([{"chars": 250}])
                self.assertEqual(data["days"]["2026-09-24"], 100)
                # 当日快照应为本次构建值
                today = max(k for k in data["days"] if k != "2026-09-24")
                self.assertEqual(data["days"][today], 250)
                # 损坏文件：备份后重建，不清掉旧数据前先落 .bak
                hist.write_text("{broken", encoding="utf-8")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    data = build.record_history([{"chars": 9}])
                self.assertTrue(Path(str(hist) + ".bak").exists())
                self.assertIn("备份", buf.getvalue())
                # 结构不对（数组）同样兜底
                hist.write_text("[1,2]", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    data = build.record_history([{"chars": 9}])
                self.assertIsInstance(data["days"], dict)
            finally:
                build.HISTORY_FILE = saved

    def test_daily_net_ignores_imported_and_removed_books(self):
        data = {
            "days": {"2026-09-01": 1000, "2026-09-02": 51800, "2026-09-03": 800},
            "books": {
                "2026-09-01": {"甲": 1000},
                # 甲 +800，同时导入写好的乙（5 万字）：净增只算 800
                "2026-09-02": {"甲": 1800, "乙": 50000},
                # 删掉乙、甲不变：净增 0，而不是 -51000
                "2026-09-03": {"甲": 1800},
            },
        }
        net = build.daily_net(data)
        self.assertEqual(net, {"2026-09-01": 0, "2026-09-02": 800, "2026-09-03": 0})

    def test_daily_net_falls_back_to_totals_for_old_format(self):
        net = build.daily_net({"days": {"2026-09-01": 100, "2026-09-02": 350}})
        self.assertEqual(net, {"2026-09-01": 0, "2026-09-02": 250})

    def test_record_history_upgrades_old_file_and_skips_noop_writes(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = build.HISTORY_FILE
            hist = Path(tmp) / "进度.json"
            build.HISTORY_FILE = hist
            try:
                hist.write_text('{"days": {"2026-09-24": 100}}', encoding="utf-8")
                books = [{"title": "甲", "chars": 300}]
                data = build.record_history(books)
                today = max(data["books"])
                self.assertEqual(data["books"][today], {"甲": 300})
                self.assertEqual(data["days"]["2026-09-24"], 100)
                before = hist.read_bytes()
                build.record_history(books)  # 字数没变：不写盘
                self.assertEqual(hist.read_bytes(), before)
            finally:
                build.HISTORY_FILE = saved

    def test_material_index_has_all_state_buckets(self):
        cards = [
            {"cat": "人物", "name": "a.md", "file": "人物/a.md", "date": "2026-01-01",
             "title": "甲", "state": "暂缓", "heat": "", "prio": ""},
            {"cat": "人物", "name": "b.md", "file": "人物/b.md", "date": "2026-01-01",
             "title": "乙", "state": "弃用", "heat": "", "prio": ""},
            {"cat": "人物", "name": "c.md", "file": "人物/c.md", "date": "2026-01-01",
             "title": "带|竖线", "state": "待看", "heat": "", "prio": ""},
        ]
        md = build.render_material_index(cards)
        self.assertIn("### 暂缓", md)
        self.assertIn("### 弃用", md)
        self.assertIn("甲", md.split("### 暂缓")[1].split("###")[0])
        self.assertIn("带/竖线", md)  # 竖线不能切断表格

    def test_lint_flags_export_enum_and_uses_materials_base(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "books" / "书" / "章节").mkdir(parents=True)
            (root / "books" / "书" / "book.toml").write_text(
                'title = "书"\nexport = "pdf"\n', encoding="utf-8")
            (root / "books" / "书" / "章节" / "ch001.md").write_text(
                "# 第 1 章 x\n## 正文\n正文", encoding="utf-8")
            (root / "素材").mkdir()
            (root / "素材" / "人物.md").write_text(
                "# 人物卡\n> 状态：待看\n", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                errors = build.lint(books_dir=root / "books",
                                    materials_base=root / "素材")
            out = buf.getvalue()
            self.assertGreaterEqual(errors, 1)
            self.assertIn("export", out)

    def test_fork_label_escaped_in_meta_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "书"
            (d / "章节").mkdir(parents=True)
            (d / "book.toml").write_text(
                'title = "书"\nfork_label = "<img src=x>"\n', encoding="utf-8")
            for name in ("ch001.md", "ch001-刀版.md"):
                (d / "章节" / name).write_text(
                    "# 第 1 章 标\n## 正文\n各自内容", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                book = build.load_book(d)
            html_out = build.render_reader(book, has_shelf=False)
            self.assertNotIn("<img src=x>", html_out)
            self.assertIn("&lt;img src=x&gt;", html_out)

    def test_fork_chapter_end_and_nav_carry_branch(self):
        """分叉章的章末横线、导航在 <section> 外，也要带 data-branch 随线显隐。"""
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "书"
            (d / "章节").mkdir(parents=True)
            (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
            for name in ("ch001.md", "ch002.md", "ch002-刀版.md", "ch003.md"):
                (d / "章节" / name).write_text(
                    f"# 第 {name[4]} 章 标\n## 正文\n内容", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                book = build.load_book(d)
            out = build.render_reader(book, has_shelf=False)
            self.assertEqual(out.count('<div class="ch-end" data-branch="dao">'), 1)
            self.assertEqual(out.count('<div class="ch-end" data-branch="mei">'), 1)
            self.assertIn('<nav class="ch-nav" data-branch="dao">', out)
            self.assertIn("body[data-path=dao] .ch-end[data-branch=dao]", out)
            # 共用章的横线不带线标记
            self.assertEqual(out.count('<div class="ch-end">'), 2)

    def test_reader_toolbar_has_prefs_panel_and_reading_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "书"
            (d / "章节").mkdir(parents=True)
            (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
            (d / "章节" / "ch001.md").write_text(
                "# 第 1 章 标\n## 正文\n" + "字" * 2000, encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                book = build.load_book(d)
            out = build.render_reader(book, has_shelf=True)
            self.assertIn('id="prefs-btn"', out)
            self.assertIn('data-set-theme="black"', out)
            self.assertNotIn('id="font"', out)  # 旧的字号按钮已并入面板
            self.assertIn('<span class="rt">约 5 分钟</span>', out)  # 2000 / 400
            self.assertIn('class="back-txt"', out)

    def test_cover_pattern_is_deterministic_and_small(self):
        import xml.dom.minidom
        a = build.cover_pattern_svg("远观", "#8c6f4a", 116, 174)
        self.assertEqual(a, build.cover_pattern_svg("远观", "#8c6f4a", 116, 174))
        self.assertNotEqual(a, build.cover_pattern_svg("出差", "#8c6f4a", 116, 174))
        for title in ("远观", "出差", "三十天", "灵鹫之后", "我们的那些年", "高中生的秘密",
                      "没被发现的那部分", "a", "书名很长很长很长很长"):
            svg = build.cover_pattern_svg(title, "#4a6670", 116, 174)
            xml.dom.minidom.parseString(svg)
            # 书架页要内联全部封面：单本纹样控制在 2 KB 内
            self.assertLess(len(svg.encode()), 2048, title)

    def test_shelf_card_carries_resume_data(self):
        books = [{
            "title": "书", "file": "书.html", "status": "已完结", "adult": False,
            "tagline": "", "subtitle": "", "accent": "#123456", "cover": "",
            "chars": 100, "default_path": "mei", "has_fork": False,
            "chapters": [{"num": 1, "title": "章一", "anchor": "ch-1", "branch": None,
                          "branch_label": ""}],
        }]
        out = build.render_shelf(books)
        self.assertIn('data-book="书"', out)
        self.assertIn("data-toc=", out)
        self.assertIn("&quot;ch-1&quot;", out)
        self.assertIn('class="resume" hidden', out)
        self.assertIn("--pat:url(data:image/svg+xml;base64,", out)

    def test_custom_branch_default_path(self):
        # -甲版/-乙版 这类自定义线：字数、导出跟随首线，而不是写死的 mei
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "书"
            (d / "章节").mkdir(parents=True)
            (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
            (d / "章节" / "ch001-甲版.md").write_text(
                "# 第 1 章 标\n## 正文\n甲线正文", encoding="utf-8")
            (d / "章节" / "ch001-乙版.md").write_text(
                "# 第 1 章 标\n## 正文\n乙线正文", encoding="utf-8")
            buf = io.StringIO()
            with redirect_stdout(buf):
                book = build.load_book(d)
            # 首线 = (BRANCH_SORT, 线名) 排序的最小者；甲/乙都是未知标签，
            # 按码位"乙"在前——契约是确定且可预期的，不是写死 mei
            first = sorted({"甲版", "乙版"})[0]
            self.assertEqual(book["default_path"], first)
            self.assertGreater(book["chars"], 0)
            txt = build.export_txt(book)
            expect = "甲线正文" if first == "甲版" else "乙线正文"
            other = "乙线正文" if first == "甲版" else "甲线正文"
            self.assertIn(expect, txt)
            self.assertNotIn(other, txt)

    def test_new_book_sanitizes_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            books = root / "books"
            books.mkdir()
            templates = root / "templates"
            templates.mkdir()
            (templates / "book.toml").write_text('title = "书名"\n', encoding="utf-8")
            saved = (build.BASE, build.BOOKS_DIR)
            try:
                build.BASE = root
                build.BOOKS_DIR = books
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = build.new_book('a"b/../c')
                self.assertTrue(ok)
                # 不会逃出 books/（没有路径分隔符），引号被清洗
                created = [p.name for p in books.iterdir()]
                # safe_filename 只清洗 Windows 非法字符，点号合法保留
                self.assertEqual(created, [build.safe_filename('a"b/../c')])
                self.assertNotIn("/", created[0])
                self.assertNotIn("\\", created[0])
            finally:
                build.BASE, build.BOOKS_DIR = saved


class PlatformTest(unittest.TestCase):
    """升级轮：endmark 收紧、EPUB 封面、分章站、RSS/sitemap、投稿导出。"""

    def test_endmark_needs_marker_context(self):
        # 台词「（完了）」不再是章末标记；「（第 2 章 · 终）」仍是
        with tempfile.TemporaryDirectory() as tmp:
            ch = build.parse_chapter(write(
                tmp, "ch001.md", "# 第 1 章 x\n## 正文\n（完了）他说\n（第 1 章 · 终）"))
            endmarks = [b[1] for b in ch["blocks"] if b[0] == "endmark"]
            paras = [b[1] for b in ch["blocks"] if b[0] == "para"]
            self.assertEqual(len(endmarks), 1)
            self.assertIn("（完了）他说", paras[0])

    def test_endmark_weiwandaixu_only_at_line_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            ch = build.parse_chapter(write(
                tmp, "ch001.md", "# 第 1 章 x\n## 正文\n故事未完待续下回分解这是正文\n未完待续"))
            endmarks = [b[1] for b in ch["blocks"] if b[0] == "endmark"]
            self.assertEqual(len(endmarks), 1)

    def test_epub_cover_generated_and_embedded(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "书"
            (d / "章节").mkdir(parents=True)
            (d / "book.toml").write_text('title = "封面书"\naccent = "#123456"\n',
                                         encoding="utf-8")
            (d / "章节" / "ch001.md").write_text("# 第 1 章 x\n## 正文\n内容",
                                                 encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                book = build.load_book(d)
            out = Path(tmp) / "b.epub"
            build.export_epub(book, out)
            with zipfile.ZipFile(out) as z:
                self.assertIn("OEBPS/cover.svg", z.namelist())
                # 生成式封面带纹样，且整张 SVG 仍是合法 XML
                import xml.dom.minidom
                xml.dom.minidom.parseString(z.read("OEBPS/cover.svg"))
                opf = z.read("OEBPS/content.opf").decode()
                self.assertIn('properties="cover-image"', opf)
                self.assertIn("#123456", z.read("OEBPS/cover.svg").decode())

    def test_epub_real_cover_image_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = Path(tmp) / "书"
            (d / "章节").mkdir(parents=True)
            (d / "book.toml").write_text(
                'title = "书"\ncover = "my-cover.png"\n', encoding="utf-8")
            (d / "章节" / "ch001.md").write_text("# 第 1 章 x\n## 正文\n内容",
                                                 encoding="utf-8")
            (d / "my-cover.png").write_bytes(b"\x89PNG fake")
            with redirect_stdout(io.StringIO()):
                book = build.load_book(d)
            out = Path(tmp) / "b.epub"
            build.export_epub(book, out)
            with zipfile.ZipFile(out) as z:
                self.assertIn("OEBPS/cover.png", z.namelist())
                self.assertEqual(z.read("OEBPS/cover.png"), b"\x89PNG fake")

    def test_epub_control_chars_stripped(self):
        text = build._epub_clean("正\x00文\x0b清\x1f理")
        self.assertEqual(text, "正文清理")

    def test_chapters_site_pages_and_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = (build.BASE, build.BOOKS_DIR, build.SITE_DIR)
            try:
                root = Path(tmp)
                build.BASE = root
                build.BOOKS_DIR = root / "books"
                build.SITE_DIR = root / "site"
                # refresh_assets 会从 BASE/templates/assets 读样式脚本
                shutil.copytree(saved[0] / "templates" / "assets",
                                root / "templates" / "assets")
                d = root / "books" / "书"
                (d / "章节").mkdir(parents=True)
                (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
                for n in (1, 2):
                    (d / "章节" / f"ch00{n}.md").write_text(
                        f"# 第 {n} 章 标\n## 正文\n第{n}章内容", encoding="utf-8")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = build.build()
                self.assertTrue(ok)
                base = root / "site" / "chapters"
                self.assertTrue((base / "style.css").exists())
                self.assertTrue((base / "index.html").exists())
                self.assertTrue((base / "书" / "ch001.html").exists())
                book_index = (base / "书" / "index.html").read_text(encoding="utf-8")
                self.assertIn('href="ch001.html"', book_index)
                self.assertIn('href="ch002.html"', book_index)
                self.assertNotIn('href="001.html"', book_index)
                page = (base / "书" / "ch001.html").read_text(encoding="utf-8")
                self.assertIn('href="ch002.html"', page)  # 前后章链接带 ch 前缀
                self.assertIn("单文件版", page)
                # 无 site.toml → 不出 RSS/sitemap
                self.assertFalse((base / "书" / "feed.xml").exists())
            finally:
                build.BASE, build.BOOKS_DIR, build.SITE_DIR = saved

    def test_volume_chapter_pages_have_distinct_links_and_feed_urls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            d = root / "书"
            (d / "章节" / "卷一").mkdir(parents=True)
            (d / "章节" / "卷二").mkdir(parents=True)
            (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
            for volume, body in (("卷一", "第一卷正文"), ("卷二", "第二卷正文")):
                (d / "章节" / volume / "ch001.md").write_text(
                    f"# 第 1 章 标\n## 正文\n{body}", encoding="utf-8")
            with redirect_stdout(io.StringIO()):
                book = build.load_book(d)
            book["file"] = "书.html"
            chapters = book["chapters"]
            names = [build.chapter_page_name(ch) for ch in chapters]
            self.assertEqual(len(set(names)), 2)
            saved_site = build.SITE_DIR
            try:
                build.SITE_DIR = root / "site"
                build.SITE_DIR.mkdir()
                build.build_chapters_site([book], site_url="https://example.test")
                book_dir = build.SITE_DIR / "chapters" / "书"
                index = (book_dir / "index.html").read_text(encoding="utf-8")
                for name, body in zip(names, ("第一卷正文", "第二卷正文")):
                    self.assertTrue((book_dir / name).exists())
                    self.assertIn(f'href="{name}"', index)
                    self.assertIn(body, (book_dir / name).read_text(encoding="utf-8"))
                    self.assertIn(name, (book_dir / "feed.xml").read_text(encoding="utf-8"))
                    self.assertIn(name, (build.SITE_DIR / "sitemap.xml").read_text(encoding="utf-8"))
                first = (book_dir / names[0]).read_text(encoding="utf-8")
                self.assertIn(f'href="{names[1]}"', first)
            finally:
                build.SITE_DIR = saved_site

    def test_adult_chapter_page_requires_shared_age_confirmation(self):
        book = {"title": "书", "file": "书.html", "tagline": "", "adult": True}
        ch = {"num": 1, "title": "标", "volume": "", "branch": None,
              "blocks": [("para", "正文内容")]}
        page = build.render_chapter_page(book, ch, None, None)
        self.assertIn('data-adult="1"', page)
        self.assertIn('class="age-gate"', page)
        self.assertIn("localStorage.getItem('xhw-adult-gate')", page)
        self.assertIn("localStorage.setItem('xhw-adult-gate','1')", page)
        self.assertIn("正文内容", page)
        self.assertIn('body[data-adult="1"] .age-gate{display:flex}', build.CHAPTERS_CSS)
        book["adult"] = False
        self.assertNotIn('class="age-gate"',
                         build.render_chapter_page(book, ch, None, None))

    def test_failed_book_keeps_last_successful_site(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = (build.BASE, build.BOOKS_DIR, build.SITE_DIR,
                     build.HISTORY_FILE)
            try:
                build.BASE = root
                build.BOOKS_DIR = root / "books"
                build.SITE_DIR = root / "site"
                build.HISTORY_FILE = root / "进度.json"
                shutil.copytree(saved[0] / "templates" / "assets",
                                root / "templates" / "assets")
                for name in ("好书", "坏书"):
                    d = build.BOOKS_DIR / name
                    (d / "章节").mkdir(parents=True)
                    (d / "book.toml").write_text(f'title = "{name}"\n', encoding="utf-8")
                    (d / "章节" / "ch001.md").write_text(
                        "# 第 1 章 标\n## 正文\n正文", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(build.build())
                snapshots = {
                    name: (build.SITE_DIR / name).read_bytes()
                    for name in ("index.html", "坏书.html", ".build-manifest", ".build-id")
                }
                (build.BOOKS_DIR / "坏书" / "章节" / "ch001.md").write_text(
                    "## 本章目标\n缺章标题", encoding="utf-8")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    self.assertFalse(build.build())
                for name, before in snapshots.items():
                    self.assertEqual((build.SITE_DIR / name).read_bytes(), before)
            finally:
                (build.BASE, build.BOOKS_DIR, build.SITE_DIR,
                 build.HISTORY_FILE) = saved

    def test_public_build_skips_adult_books(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = (build.BASE, build.BOOKS_DIR, build.SITE_DIR,
                     build.HISTORY_FILE)
            try:
                build.BASE = root
                build.BOOKS_DIR = root / "books"
                build.SITE_DIR = root / "site"
                build.HISTORY_FILE = root / "进度.json"
                shutil.copytree(saved[0] / "templates" / "assets",
                                root / "templates" / "assets")
                (root / "site.toml").write_text('url = "https://x.example.com"\n',
                                                encoding="utf-8")
                for name, adult in (("清书", "false"), ("艳书", "true")):
                    d = build.BOOKS_DIR / name
                    (d / "章节").mkdir(parents=True)
                    (d / "book.toml").write_text(
                        f'title = "{name}"\nadult = {adult}\n', encoding="utf-8")
                    (d / "章节" / "ch001.md").write_text(
                        "# 第 1 章 标\n## 正文\n正文", encoding="utf-8")
                # 先全量构建一次，再公开构建：成人向产物要被清掉
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(build.build())
                self.assertTrue((build.SITE_DIR / "艳书.html").exists())
                build.HISTORY_FILE.unlink()
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(build.build(public=True))
                site = build.SITE_DIR
                self.assertTrue((site / "清书.html").exists())
                for name in ("艳书.html", "艳书.epub", "艳书.txt"):
                    self.assertFalse((site / name).exists(), name)
                self.assertFalse((site / "chapters" / "艳书" / "ch001.html").exists())
                self.assertNotIn("%E8%89%B3", (site / "sitemap.xml").read_text(
                    encoding="utf-8"))  # 「艳」的 URL 编码
                shelf = (site / "index.html").read_text(encoding="utf-8")
                self.assertNotIn("艳书", shelf)
                self.assertFalse(build.HISTORY_FILE.exists())  # 不碰私人进度
            finally:
                (build.BASE, build.BOOKS_DIR, build.SITE_DIR,
                 build.HISTORY_FILE) = saved

    def test_renamed_book_with_space_is_cleaned(self):
        """书名带空格：manifest 按行读，改名后旧产物要被清理。"""
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            saved = (build.BASE, build.BOOKS_DIR, build.SITE_DIR,
                     build.HISTORY_FILE)
            try:
                build.BASE = root
                build.BOOKS_DIR = root / "books"
                build.SITE_DIR = root / "site"
                build.HISTORY_FILE = root / "进度.json"
                shutil.copytree(saved[0] / "templates" / "assets",
                                root / "templates" / "assets")
                d = build.BOOKS_DIR / "书"
                (d / "章节").mkdir(parents=True)
                (d / "章节" / "ch001.md").write_text(
                    "# 第 1 章 标\n## 正文\n正文", encoding="utf-8")
                (d / "book.toml").write_text('title = "A B"\n', encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(build.build())
                self.assertTrue((build.SITE_DIR / "A B.html").exists())
                (d / "book.toml").write_text('title = "新名"\n', encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    self.assertTrue(build.build())
                self.assertFalse((build.SITE_DIR / "A B.html").exists())
                self.assertFalse((build.SITE_DIR / "A B.epub").exists())
                self.assertFalse(
                    (build.SITE_DIR / "chapters" / "A B" / "ch001.html").exists())
            finally:
                (build.BASE, build.BOOKS_DIR, build.SITE_DIR,
                 build.HISTORY_FILE) = saved

    def test_site_config_is_watched_when_created_or_changed(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = (build.BASE, build.BOOKS_DIR)
            try:
                build.BASE = Path(tmp)
                build.BOOKS_DIR = build.BASE / "books"
                before = build.source_fingerprint()
                config = build.BASE / "site.toml"
                config.write_text('url = "https://example.test"\n', encoding="utf-8")
                created = build.source_fingerprint()
                self.assertNotEqual(before, created)
                self.assertIn(config, list(build.iter_watch_files()))
                config.write_text('url = "https://example.test/new"\n', encoding="utf-8")
                self.assertNotEqual(created, build.source_fingerprint())
            finally:
                build.BASE, build.BOOKS_DIR = saved

    def test_rss_and_sitemap_with_site_url(self):
        books = [{
            "title": "书", "file": "书.html", "status": "已完结", "adult": False,
            "tagline": "简介", "subtitle": "", "accent": "#123456",
            "default_path": "mei", "dir": Path("."),
            "chapters": [
                {"num": 1, "title": "章一", "branch": None, "anchor": "ch-1",
                 "volume": "", "branch_label": "", "blocks": [], "bodyless": False,
                 "_src_mtime": 1700000000},
            ],
        }]
        rss = build.render_rss(books[0],
                               books[0]["chapters"], "https://x.example.com")
        self.assertIn("https://x.example.com/chapters/%E4%B9%A6/ch001.html", rss)
        self.assertIn("<pubDate>", rss)
        sm = build.render_sitemap(books, "https://x.example.com")
        self.assertIn("<loc>https://x.example.com/chapters/%E4%B9%A6/ch001.html</loc>", sm)

    def test_export_platform_outputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = build.BASE
            try:
                root = Path(tmp)
                build.BASE = root
                d = root / "books" / "书"
                (d / "章节").mkdir(parents=True)
                (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
                (d / "章节" / "ch001.md").write_text(
                    "# 第 1 章 标\n> 时间点：第一天\n## 本章目标\n- 目标\n"
                    "## 正文\n**粗**正文\n> 气泡", encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    book = build.load_book(d)
                html_path, md_path = build.export_platform(book)
                html_doc = html_path.read_text(encoding="utf-8")
                self.assertIn("font-size:15px", html_doc)  # 样式内联
                self.assertIn("<strong", html_doc)
                md = md_path.read_text(encoding="utf-8")
                self.assertIn("## 第 1 章 · 标", md)
                self.assertIn("**粗**正文", md)  # 知乎版保留 markdown
                self.assertNotIn("本章目标", md)
            finally:
                build.BASE = saved

    def test_parse_char_goal(self):
        self.assertEqual(build.parse_char_goal("> 字数目标：4k–6k"), (4000, 6000))
        self.assertEqual(build.parse_char_goal("> 字数目标：1.2万"), (12000, 12000))
        self.assertIsNone(build.parse_char_goal("> 字数目标："))
        self.assertIsNone(build.parse_char_goal("> 视角人物：林知夏"))

    def test_person_name_groups_alias(self):
        text = "## 女主：梦姑 / 李清露\n## 男主 · 虚竹\n### 外貌细项"
        self.assertEqual(build.person_name_groups(text),
                         [["梦姑", "李清露"], ["虚竹"]])

    def test_compress_ranges(self):
        self.assertEqual(build._compress_ranges([1, 2, 3, 7, 8]), "1–3, 7–8")
        self.assertEqual(build._compress_ranges([]), "—")

    def test_build_matrix_prints_table(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = (build.BASE, build.BOOKS_DIR)
            try:
                root = Path(tmp)
                build.BASE = root
                build.BOOKS_DIR = root / "books"
                d = root / "books" / "书"
                (d / "章节").mkdir(parents=True)
                (d / "设定").mkdir()
                (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
                (d / "设定" / "人物.md").write_text("## 甲乙\n## 丙丁", encoding="utf-8")
                (d / "章节" / "ch001.md").write_text(
                    "# 第 1 章 x\n## 正文\n甲乙和丙丁见面\n", encoding="utf-8")
                (d / "章节" / "ch002.md").write_text(
                    "# 第 2 章 x\n## 正文\n只有甲乙\n", encoding="utf-8")
                buf = io.StringIO()
                with redirect_stdout(buf):
                    ok = build.build_matrix("书")
                self.assertTrue(ok)
                out = buf.getvalue()
                self.assertIn("| 甲乙 | — | 2 | ch001 | 1–2 |", out)
                self.assertIn("| 丙丁 | — | 1 | ch001 | 1 |", out)
            finally:
                build.BASE, build.BOOKS_DIR = saved

    def test_context_includes_timeline_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            saved = (build.BASE, build.BOOKS_DIR)
            try:
                root = Path(tmp)
                build.BASE = root
                build.BOOKS_DIR = root / "books"
                d = root / "books" / "书"
                (d / "章节").mkdir(parents=True)
                (d / "book.toml").write_text('title = "书"\n', encoding="utf-8")
                (d / "章节" / "ch001.md").write_text(
                    "# 第 1 章 x\n> 时间点：合租第 0 天\n## 正文\n内容",
                    encoding="utf-8")
                with redirect_stdout(io.StringIO()):
                    ok = build.build_context("书", 2)
                self.assertTrue(ok)
                out = (root / "素材" / "上下文" / "书" / "ch002-上下文.md").read_text(
                    encoding="utf-8")
                self.assertIn("时间线草稿", out)
                self.assertIn("合租第 0 天", out)
            finally:
                build.BASE, build.BOOKS_DIR = saved


if __name__ == "__main__":
    unittest.main()
