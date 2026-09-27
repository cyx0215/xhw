#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 books/ 下的作品编译成可分享的单文件 HTML 阅读页，外加一个书架首页。

用法：
  python build.py              # 构建一次
  python build.py --watch      # 监视 books/，改完自动重建
  python build.py --serve      # 本地预览 + 自动重建 + 浏览器热刷新（默认 :8765）
  python build.py --serve 9000 # 指定端口

产物：site/index.html（书架）+ site/<书名>.html（每本书一个，可单独发出）

零依赖，只用标准库。各书的元信息写在 books/<书名>/book.toml，
新增作品无需改本脚本；正文书写约定见 README。
"""

import base64
import hashlib
import html
import io
import json
import re
import shutil
import sys
import time
import zipfile
from contextlib import redirect_stdout
from pathlib import Path

BASE = Path(__file__).parent
BOOKS_DIR = BASE / "books"
SITE_DIR = BASE / "site"
HISTORY_FILE = BASE / "进度.json"

# 模板骨架，不是作品
EXCLUDE = {"示例作品"}

DEFAULT_ACCENT = "#8c6f4a"
SECTION_MARKS = set("一二三四五六七八九十百")

# 作者侧字样：正文里绝不该出现，用于构建时自检
AUTHOR_ONLY = ["本章目标", "章后笔记", "字数目标", "视角人物", "伏笔", "自查"]


# ---------------------------------------------------------------- 元信息

def parse_toml(path):
    """极简 TOML 子集解析：key = "字符串" / '字面量' / 数字 / true|false，# 为注释。

    标准库 tomllib 要 Python 3.11+，这里只需这几种标量，自己解析以保持零依赖。
    双引号串支持 \\" \\\\ \\n \\t 转义；不支持多行字符串。遇到 [表] 头会警告并
    跳过整段——拍平进顶层会静默覆盖同名键（如 [author] 里的 title 盖掉书名）。
    """
    data = {}
    if not path.exists():
        return data
    in_table = False
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("[") and line.endswith("]"):
            in_table = True
            print(f"  ⚠ {path.name}：暂不支持 {line} 表，该表整段已忽略")
            continue
        if in_table:
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        if not key:
            continue
        # 字符串值：引号内 # 不是注释；先找闭合引号再处理转义
        if val[:1] in ('"', "'"):
            quote = val[0]
            body, i, n = [], 1, len(val)
            closed = False
            while i < n:
                c = val[i]
                if quote == '"' and c == "\\" and i + 1 < n:
                    nxt = val[i + 1]
                    body.append({"n": "\n", "t": "\t", '"': '"', "\\": "\\"}.get(nxt, "\\" + nxt))
                    i += 2
                    continue
                if c == quote:
                    closed = True
                    break
                body.append(c)
                i += 1
            if not closed:
                print(f"  ⚠ {path.name}：{key} 的值没有闭合引号，按原文保留")
                data[key] = val[1:]
            else:
                data[key] = "".join(body)
            continue
        # 非字符串：去掉行尾注释再判定类型（true/false 不区分大小写，防止误配）
        val = val.split("#")[0].strip()
        low = val.lower()
        if low in ("true", "false"):
            data[key] = low == "true"
        else:
            try:
                data[key] = int(val)
            except ValueError:
                try:
                    data[key] = float(val)
                except ValueError:
                    data[key] = val
    return data


def _as_str(value, field, book_name):
    """book.toml 标量误写成数字/布尔时的兜底：转成字符串并提醒。

    不做这步，subtitle = 123 会在渲染的 html.escape(int) 处崩掉整次构建。
    """
    if value is None or isinstance(value, str):
        return value or ""
    print(f"  ⚠ {book_name}：book.toml 里 {field}={value!r} 不是字符串，已按文本处理")
    return str(value)


def load_meta(book_dir):
    m = parse_toml(book_dir / "book.toml")
    accent = m.get("accent") or DEFAULT_ACCENT
    if not valid_hex(accent):
        print(f"  ⚠ {book_dir.name}：book.toml 里 accent={accent!r} 不是合法颜色，"
              f"回退为默认色 {DEFAULT_ACCENT}")
        accent = DEFAULT_ACCENT
    order = m.get("order", 99)
    if isinstance(order, bool) or not isinstance(order, (int, float)):
        print(f"  ⚠ {book_dir.name}：order={order!r} 不是数字，回退为 99")
        order = 99
    s = lambda field, default: _as_str(m.get(field, default), field, book_dir.name)
    return {
        "title": s("title", "") or book_dir.name,
        "subtitle": s("subtitle", ""),
        "tagline": s("tagline", ""),
        "epigraph": s("epigraph", ""),
        "status": s("status", "") or "连载中",
        "adult": bool(m.get("adult", False)),
        "accent": accent,
        "cover": s("cover", ""),
        "order": order,
        # 导出开关："epub+txt"（默认）/ "epub" / "txt" / "none"
        "export": str(m.get("export", "epub+txt")).lower(),
        # 双线书的选线卡文案（可选）；line_note_<线key> 从 m 里按需读
        "fork_label": s("fork_label", ""),
        "fork_desc": s("fork_desc", ""),
        "raw_meta": m,
        "dir": book_dir,
    }


# ---------------------------------------------------------------- 解析正文

CN_DIGITS = {"零": 0, "一": 1, "二": 2, "三": 3, "四": 4,
             "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def cn_num(s):
    """「12」「一」「十」「二十三」→ int，用于兼容汉字章号。"""
    s = s.strip()
    if s.isdigit():
        return int(s)
    total, section = 0, 0
    for ch in s:
        if ch in CN_DIGITS:
            section = CN_DIGITS[ch]
        elif ch == "十":
            section = (section or 1) * 10
            total += section
            section = 0
        elif ch == "百":
            section = (section or 1) * 100
            total += section
            section = 0
    return total + section


def inline(text):
    """行内标记：转义 -> 粗斜体 -> 粗体 -> 斜体。粗斜体必须先做，
    否则 ***x*** 会被拆成交叉嵌套的 <strong><em>…</strong></em>。
    """
    text = html.escape(text)
    text = re.sub(r"\*\*\*(.+?)\*\*\*", r"<strong><em>\1</em></strong>", text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def extract_body(text):
    """切出读者可见的正文，丢弃创作笔记。

    有 `## 正文` 就只取它到下一个 `##` 之前——这一步同时天然剔除了
    `## 本章目标`、`## 章后笔记`、`## 全文完` 以及标题下方的 `> 元信息块`。
    h2 终止符不要求 `##` 后有空白：起始侧认 `##正文`（无空格），终止侧就
    必须同样认 `##本章目标`，否则无空格写法的笔记会整段泄漏进网页。
    `(?!#)` 排除 h3——`##` 是 `###` 的前缀，不加会把正文在第一个
    `### 小节` 处截断。
    没有 `## 正文` 的（如远观、高中生的秘密）则取 `# 章标题` 之后、
    **第一个 `##` 标题之前**的内容：没写 `## 正文` 时任何 h2 都视作笔记区
    开头，避免整段创作笔记被原样发布。
    连 `# 章标题` 都没有的文件不是可发布的章节——抛错阻断构建，而不是
    把全文（多半是创作笔记）静默发上网。
    """
    m = re.search(r"^##(?!#)\s*正文\s*$(.*?)(?=^##(?!#)|\Z)", text, re.S | re.M)
    if m:
        return m.group(1)
    m = re.match(r"\A.*?^(#\s.*?)(?=^##(?!#)|\Z)", text, re.S | re.M)
    if m:
        return m.group(1)
    raise ValueError("既没有 `# 章标题` 也没有 `## 正文` 分隔，"
                     "疑似创作笔记，为防泄漏已拒绝发布该章")


def parse_chapter(path):
    """返回 {'num', 'title', 'blocks', 'bodyless'}；blocks 为 (类型, 内容) 列表。

    bodyless=True 表示该章没写 `## 正文`、靠 `# 章标题` 到第一个 h2 的兜底
    切分——能发布，但构建时会提醒补上约定分隔。
    """
    text = path.read_text(encoding="utf-8-sig")

    num, title = None, None
    m = re.search(r"^#\s+(.+)$", text, re.M)
    if m:
        head = m.group(1).strip()
        # 兼容三种章标题：
        #   第 1 章 · 传说与风      （阿拉伯数字 + 间隔号）
        #   第一章　邻座            （汉字数字 + 全角空格）
        #   第 1 章：标题 / 第一章 标题
        t = re.match(r"^第\s*([0-9]+|[一二三四五六七八九十百零]+)\s*[章回节]"
                     r"[\s·:：、.　]*(.*)$", head)
        if t:
            num = cn_num(t.group(1))
            title = t.group(2).strip() or f"第 {num} 章"
        else:
            title = head
    if num is None:
        m = re.search(r"\d+", path.stem)
        if not m:
            raise ValueError(
                f"{path.name}：文件名里没有章号，正文里也没有 `# 第 N 章` 标题")
        num = int(m.group())

    body = extract_body(text)
    bodyless = not re.search(r"^##\s*正文\s*$", text, re.M)

    blocks = []
    buf = []
    quote_buf = []

    def flush_para():
        if buf:
            blocks.append(("para", "<br>".join(buf)))
            buf.clear()

    def flush_quote():
        if quote_buf:
            blocks.append(("quote", list(quote_buf)))
            quote_buf.clear()

    for raw in body.splitlines():
        line = raw.rstrip()
        s = line.strip()

        if not s:
            flush_para()
            flush_quote()
            continue

        # `### 一、落地` / `### 1、重逢` / `### （一）落地` 形式的分节符。
        # 编号后必须有分隔（、.·）或整体加括号，防止 `### 十年后的信`
        # 这类散文标题被误拆成「十 + 年后的信」。
        h3 = re.match(
            r"^#{3,}\s*(?:[（(]\s*([一二三四五六七八九十百零]{1,4}|\d{1,3})\s*[）)]"
            r"|([一二三四五六七八九十百零]{1,4}|\d{1,3})\s*[、.·])\s*(.*)$", s)
        if h3:
            flush_para()
            flush_quote()
            blocks.append(("section", (h3.group(1) or h3.group(2),
                                       h3.group(3).strip())))
            continue

        # 其他标题行一律丢弃（正文里不该出现，防御性处理）
        if s.startswith("#"):
            flush_para()
            flush_quote()
            continue

        # 分隔线：正文里只作装饰（分节符自带横线），丢弃
        if re.fullmatch(r"[-*_]{3,}", s):
            flush_para()
            flush_quote()
            continue

        # 引用块 = 手机消息气泡
        if s.startswith(">"):
            flush_para()
            quote_buf.append(inline(s.lstrip(">").strip()))
            continue
        flush_quote()

        # 独占一行的汉字数字 = 分节符（远观 / 那些年）
        if len(s) <= 3 and all(c in SECTION_MARKS for c in s):
            flush_para()
            blocks.append(("section", (s, "")))
            continue

        # 章末标记：（第 N 章 · 终）/（全书 · 终）/ 第 N 章 · 完 / …未完待续
        # 括号式必须有「章/书/文/数字」语境——台词「（完了）」不是章末标记；
        # 「未完待续」只认行尾短句，句中提及算正文。
        if (re.fullmatch(r"[（(][^）)]{0,14}[终完][）)]", s)
                and re.search(r"[章书文\d]", s)
                or re.fullmatch(r"第\s*\d+\s*章\s*[·:：]\s*[终完]", s)
                or re.fullmatch(r"全[书文]\s*[·:：]?\s*[终完]", s)
                or (s.endswith("未完待续") and len(s) <= 16)):
            flush_para()
            blocks.append(("endmark", inline(s)))
            continue

        # 正文行；行尾两个空格 = 段内换行
        buf.append(inline(s))
        if not raw.endswith("  "):
            flush_para()

    flush_para()
    flush_quote()
    return {"num": num, "title": title or f"第 {num} 章",
            "blocks": blocks, "bodyless": bodyless}


def volume_order(dir_name):
    """卷目录名 → 排序用数字。「卷一」「卷2」「第三卷」都认，认不出的排最后。"""
    m = re.search(r"([0-9]+|[一二三四五六七八九十百零]+)", dir_name)
    return cn_num(m.group(1)) if m else 9999


# 同章多版本：ch014.md / ch014-刀版.md / ch014-美版.md
# key 写入 data-branch，与阅读页 body[data-path] 对应
BRANCH_TAGS = {
    "": ("mei", "美"),
    "美": ("mei", "美"),
    "美版": ("mei", "美"),
    "原版": ("mei", "美"),
    "刀": ("dao", "刀"),
    "刀版": ("dao", "刀"),
}
DEFAULT_PATH = "mei"
BRANCH_SORT = {"mei": 0, "dao": 1}


def branch_order_key(key):
    """阅读线排序键：已知线按 BRANCH_SORT，未知线再按线名，保证顺序稳定。

    load_book 的 default_path 与 fork_info 的选线顺序必须用同一个键，
    否则 -甲版/-乙版 这类未知标签会让阅读页首线与导出线不一致。
    """
    return (BRANCH_SORT.get(key, 9), key)


def parse_chapter_stem(stem):
    """章节文件名 → (卷号或 None, 章号或 None, 版本标签)。

    ch014        → (None, 14, "")      ch014-刀版  → (None, 14, "刀版")
    ch14美版     → (None, 14, "美版")  （连字符可省）
    ch1-2        → (1, 2, "")          （「卷1第2章」，最后一段数字是章号）
    ch1-2-刀版   → (1, 2, "刀版")
    ch备注       → (None, None, "")    （认不出章号，调用方应跳过并警告）
    """
    m = re.match(r"^ch(\d+)(?:-(\d+))?(?:-?([^0-9\-].*))?$", stem, re.I)
    if not m:
        return None, None, ""
    first = int(m.group(1))
    tag = (m.group(3) or "").strip()
    if m.group(2):
        return first, int(m.group(2)), tag
    return None, first, tag


def branch_of_tag(tag):
    """版本标签 → (branch_key, 短标签)。未知标签原样用作 key。"""
    if tag in BRANCH_TAGS:
        return BRANCH_TAGS[tag]
    if not tag:
        return BRANCH_TAGS[""]
    return (re.sub(r"[^\w\u4e00-\u9fff]+", "", tag) or "alt", tag)


def clean_display_title(title):
    """去掉章名结尾的（原版·美）（刀版）等作者标注，网页只显示故事标题。

    只认结尾的标注——作者习惯把版本写在末尾（如「像相爱那样（原版·美）」）；
    章名中段的（美）等普通用词（如「挑战（美）丽的」）不受影响。
    """
    title = (title or "").strip()
    t = re.sub(
        r"\s*[（(]\s*(原版|刀版|美版)?\s*[·・]?\s*(美|刀)?\s*[）)]\s*$",
        "",
        title,
    ).strip()
    return t or title


def iter_chapter_files(book_dir):
    """收集一本书的章节文件，返回 [(排序键, 卷名, 路径), ...]。

    支持：
      章节/ch001.md                直接放章节（卷名为 ""）
      章节/ch014-刀版.md           同章双线版本（连字符可省：ch14美版.md）
      章节/卷一/ch001.md           分卷；卷序取目录名里的数字
      章节/ch1-2.md                「卷1第2章」，卷名取文件名里的数字
    排序键 (卷序, 章号, 版本序)：美线在前，刀线在后。
    认不出章号的 ch*.md 跳过并警告；未按 ch*.md 命名的 .md 也警告，
    避免章节被静默丢弃。
    """
    ch_dir = book_dir / "章节"
    items = []

    def collect(vol_name, vo, paths):
        for p in paths:
            vol_num, seq, tag = parse_chapter_stem(p.stem)
            if seq is None:
                print(f"  ⚠ {book_dir.name}：章节/{p.name} 认不出章号，已跳过")
                continue
            if vol_num and not vol_name:
                vol_name_eff, vo_eff = f"卷{vol_num}", vol_num
            else:
                vol_name_eff, vo_eff = vol_name, vo
            key, _ = branch_of_tag(tag)
            items.append(((vo_eff, seq, BRANCH_SORT.get(key, 9)), vol_name_eff, p))

    collect("", 0, ch_dir.glob("ch*.md"))
    ignored = []
    for sub in ch_dir.iterdir():
        if not sub.is_dir():
            if sub.is_file() and sub.suffix == ".md" and not sub.stem.lower().startswith("ch"):
                ignored.append(sub.name)
            continue
        vo = volume_order(sub.name)
        collect(sub.name, vo, sub.glob("ch*.md"))
        ignored += [
            p.name for p in sub.glob("*.md")
            if not p.stem.lower().startswith("ch")
        ]
    if ignored:
        print(f"  ⚠ {book_dir.name}：以下文件未按 chNNN 命名，未收录："
              f"{', '.join(sorted(ignored))}")
    items.sort(key=lambda t: t[0])
    return items


def path_chapters(chapters, path=DEFAULT_PATH):
    """当前阅读线下的章节序列（共用章 + 该线专属章）。"""
    return [c for c in chapters if not c.get("branch") or c["branch"] == path]


def load_book(book_dir):
    """读一本书：元信息 + 全部章节（含双线版本）+ 字数统计。"""
    meta = load_meta(book_dir)
    entries = iter_chapter_files(book_dir)

    # 先解析，再按 (卷, 章号) 分组判断是否分叉
    parsed = []
    for _, vol, path in entries:
        ch = parse_chapter(path)
        _, seq, tag = parse_chapter_stem(path.stem)
        # 文件名序优先保证 ch014-刀版 归到 14；标题里的章号作校验
        if ch.get("num") is None:
            ch["num"] = seq
        elif ch["num"] != seq and seq:
            # 标题与文件名不一致时以文件名为准（避免刀版标题干扰）
            ch["num"] = seq
        ch["volume"] = vol
        ch["file_tag"] = tag
        # 源文件随章记录：投稿稿 / 上下文 / RSS 时间 / 字数目标都按本章本线取，
        # 不再用 (卷, 章号) 反查——双线同章号会查到另一条线的文件
        ch["src"] = path
        ch["title"] = clean_display_title(ch.get("title") or f"第 {ch['num']} 章")
        parsed.append(ch)

    from collections import defaultdict
    # 分组键必须带卷维度：卷一和卷二可以各有 ch001.md，互不分叉
    by_key = defaultdict(list)
    for ch in parsed:
        by_key[(ch["volume"] or "", ch["num"])].append(ch)

    chapters = []
    seen_anchors = set()

    def take_anchor(base):
        """锚点全局唯一：撞车时追加 -2、-3。"""
        a, i = base, 2
        while a in seen_anchors:
            a = f"{base}-{i}"
            i += 1
        seen_anchors.add(a)
        return a

    def group_sort_key(vk):
        return (volume_order(vk[0]) if vk[0] else -1, vk[1])

    for volkey in sorted(by_key, key=group_sort_key):
        group = by_key[volkey]
        forked = len(group) > 1
        branch_keys = [branch_of_tag(c["file_tag"])[0] for c in group]
        if forked and len(set(branch_keys)) < len(branch_keys):
            dupes = {tag: k for tag, k in
                     ((c["file_tag"], branch_of_tag(c["file_tag"])[0]) for c in group)
                     if k}
            print(f"  ⚠ {meta['title']} 第 {volkey[1]} 章：同一条阅读线有多个版本文件"
                  f"（{', '.join(sorted(dupes))}），疑似重复保存")
        for ch in group:
            tag = ch.pop("file_tag", "")
            if forked:
                key, label = branch_of_tag(tag)
                ch["branch"] = key
                ch["branch_label"] = label
                ch["anchor"] = take_anchor(f"ch-{ch['num']}-{key}")
            else:
                ch["branch"] = None
                ch["branch_label"] = ""
                # 锚点用章号，避免双线书插入版本后共用章 id 漂移
                ch["anchor"] = take_anchor(f"ch-{ch['num']}")
            chapters.append(ch)

    meta["chapters"] = chapters
    meta["has_fork"] = any(c.get("branch") for c in chapters)
    # 阅读线首线：双线书可能不是 mei/dao（如 -甲版/-乙版），字数、导出、
    # 初始 data-path 都跟随首线而不是写死 mei。
    # 次键用线名本身，保证未知标签的先后顺序稳定（set 无序）。
    branch_keys = sorted({c["branch"] for c in chapters if c.get("branch")},
                         key=branch_order_key)
    meta["default_path"] = branch_keys[0] if branch_keys else DEFAULT_PATH
    meta["chapter_count"] = len(by_key)  # 逻辑章数（卷章独立、双线不重复计）
    bodyless = [c["num"] for c in chapters if c.get("bodyless")]
    if bodyless:
        print(f"  ⚠ {meta['title']}：第 {', '.join(map(str, bodyless))} 章没有 "
              f"`## 正文` 分隔（按约定建议补上，笔记靠 h2 截断兜底）")
    # 字数按默认线统计，避免美+刀重复灌水
    meta["chars"] = sum(chapter_chars(c)
                        for c in path_chapters(chapters, meta["default_path"]))
    check_author_leak(meta)
    return meta


def check_author_leak(book):
    """构建期自检：extract_body 理应把创作笔记全部剔除，这里兜底检查一遍，
    确保 AUTHOR_ONLY 里的作者侧字样没有随正文泄漏到网页里。
    """
    for ch in book["chapters"]:
        for kind, content in ch["blocks"]:
            if kind == "section":
                parts = [str(content[0]), str(content[1])]  # (mark, sub) 也要查
            else:
                parts = content if isinstance(content, list) else [content]
            for p in parts:
                if not isinstance(p, str):
                    continue
                for word in AUTHOR_ONLY:
                    if word in p:
                        print(f"  ⚠ {book['title']} 第 {ch['num']} 章疑似泄漏创作笔记"
                              f"「{word}」，检查是否漏加 ## 正文 分隔")


def chapter_chars(ch):
    """章正文字数（去标签、去空白）。

    只统计 para / quote：section 本身是 (mark, sub) 元组已天然被排除，
    但 endmark（如「（第 10 章 · 终）」「未完待续」）是字符串会被误计入，
    这里显式跳过，避免章末标记的十几个字混进字数统计。
    """
    n = 0
    for kind, content in ch["blocks"]:
        if kind == "endmark":
            continue
        parts = content if isinstance(content, list) else [content]
        for p in parts:
            if isinstance(p, str):
                n += len(re.sub(r"\s", "", re.sub(r"<[^>]+>", "", p)))
    return n


READ_CHARS_PER_MIN = 400  # 中文小说常见阅读速度（每分钟 300–500 字）取中


def reading_minutes(ch):
    """章节预计阅读分钟数，至少 1 分钟。"""
    return max(1, round(chapter_chars(ch) / READ_CHARS_PER_MIN))


def fmt_wan(n):
    """字数写成「约 N 万字」或「约 N 千字」。"""
    if n >= 10000:
        return f"约 {round(n / 10000, 1)} 万字"
    if n <= 0:
        return "暂无正文"
    return f"约 {max(1, round(n / 1000))} 千字"


def safe_filename(title):
    """书名直接拿来当文件名，含 / \\ : * ? " < > | 会在磁盘上炸掉，清洗成 _。

    Windows 保留设备名（CON、PRN、COM1…）不能作文件名，加前缀绕开。
    """
    name = re.sub(r'[\\/:*?"<>|]', "_", title).strip() or "未命名"
    if re.fullmatch(r"(?i)(con|prn|aux|nul|com[1-9]|lpt[1-9])(\..*)?", name):
        name = "_" + name
    return name


def escape_epigraph(text):
    """转义题引文本，但放行 <br> 换行——书写约定允许用它分行。"""
    escaped = html.escape(text)
    return escaped.replace("&lt;br&gt;", "<br>")


def favicon_data_uri(accent):
    """用书的主色画一个纯色圆点当 favicon，内联成 data URI——
    不依赖外部文件，保持单文件可分享的设计。
    """
    return svg_data_uri(
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<circle cx="16" cy="16" r="14" fill="{accent}"/></svg>'
    )


def svg_data_uri(svg):
    """SVG 字符串 → base64 data URI（无引号、无空格，可直接放进 CSS url()）。"""
    return "data:image/svg+xml;base64," + base64.b64encode(svg.encode("utf-8")).decode("ascii")


# 纹样坐标系：200×300（2:3，与书架封面 116×174、EPUB 封面 800×1200 同比例）
_PAT_W, _PAT_H = 200, 300


def _cover_pattern_elems(title, color):
    """按书名 sha256 做种子，确定性生成一组几何纹样元素（200×300 坐标系）。

    四种母题随种子选：同心弧 / 斜线 / 点阵 / 水波。参数（位置、间距、
    角度、疏密）也来自种子，所以同名书每次一致、不同书一眼能分开。
    只用细线与低不透明度，书名叠在上面仍清楚。
    """
    import random
    seed = int(hashlib.sha256(title.encode("utf-8")).hexdigest(), 16)
    rnd = random.Random(seed)
    f = lambda v: f"{v:.1f}".rstrip("0").rstrip(".")
    kind = rnd.randrange(4)
    op = f(0.16 + rnd.random() * 0.08)
    # 重复单元一律走 <pattern> 平铺：逐个画 <circle>/<line> 时点阵一本书
    # 就要 13 KB，书架页内联 7 本会翻几倍；平铺后每本约 1 KB
    pid = "p" + hashlib.sha256(title.encode("utf-8")).hexdigest()[:8]
    stroke = f'stroke="{color}" stroke-opacity="{op}" stroke-width="1.1" fill="none"'
    fill_all = f'<rect width="{_PAT_W}" height="{_PAT_H}" fill="url(#{pid})"/>'
    if kind == 0:  # 同心弧：圆心落在某个角外侧（半径各异，不能平铺，但数量有限）
        cx = rnd.choice((-20, _PAT_W + 20)) + rnd.uniform(-30, 30)
        cy = rnd.choice((-30, _PAT_H + 30)) + rnd.uniform(-40, 40)
        step = rnd.uniform(14, 20)
        rings = "".join(f'<circle cx="{f(cx)}" cy="{f(cy)}" r="{f(step * i)}"/>'
                        for i in range(1, int(380 / step)))
        return f'<g {stroke}>{rings}</g>'
    if kind == 1:  # 斜线：统一角度的平行线，一半的书成对
        ang = rnd.uniform(25, 65) * rnd.choice((1, -1))
        gap = rnd.uniform(9, 16)
        pair = (f'<line x1="3" y1="0" x2="3" y2="{f(gap)}"/>'
                if rnd.random() < 0.5 else "")
        return (f'<defs><pattern id="{pid}" width="{f(gap)}" height="{f(gap)}" '
                f'patternUnits="userSpaceOnUse" patternTransform="rotate({f(ang)})">'
                f'<g {stroke}><line x1="0" y1="0" x2="0" y2="{f(gap)}"/>{pair}</g>'
                f'</pattern></defs>{fill_all}')
    if kind == 2:  # 点阵：交错排布，靠渐变蒙版从一角淡出
        gap = rnd.uniform(11, 16)
        rr = f(rnd.uniform(1.2, 2.0))
        x1, y1 = rnd.choice(((0, 0), (1, 0), (0, 1), (1, 1)))
        return (f'<defs><pattern id="{pid}" width="{f(gap)}" height="{f(gap * 1.74)}" '
                f'patternUnits="userSpaceOnUse">'
                f'<g fill="{color}" fill-opacity="{op}">'
                f'<circle cx="{f(gap / 2)}" cy="{f(gap * 0.435)}" r="{rr}"/>'
                f'<circle cx="0" cy="{f(gap * 1.305)}" r="{rr}"/>'
                f'<circle cx="{f(gap)}" cy="{f(gap * 1.305)}" r="{rr}"/></g></pattern>'
                f'<linearGradient id="{pid}g" x1="{x1}" y1="{y1}" x2="{1 - x1}" y2="{1 - y1}">'
                f'<stop offset="0" stop-color="#fff"/>'
                f'<stop offset="1" stop-color="#fff" stop-opacity="0"/></linearGradient>'
                f'<mask id="{pid}m"><rect width="{_PAT_W}" height="{_PAT_H}" '
                f'fill="url(#{pid}g)"/></mask></defs>'
                f'<g mask="url(#{pid}m)">{fill_all}</g>')
    # 水波：一个波长的曲线横向平铺，纵向错行
    amp = rnd.uniform(6, 14)
    wl = rnd.uniform(38, 70)
    gap = rnd.uniform(12, 18)
    return (f'<defs><pattern id="{pid}" width="{f(wl)}" height="{f(gap)}" '
            f'patternUnits="userSpaceOnUse">'
            f'<path {stroke} d="M0 {f(gap / 2)} q{f(wl / 4)} {f(-amp)} {f(wl / 2)} 0 '
            f't{f(wl / 2)} 0"/></pattern></defs>{fill_all}')


def cover_pattern_svg(title, accent, w, h):
    """独立的纹样 SVG（透明底），用作书架封面 .cv 的背景层。"""
    color = lighten(accent, 0.6)
    return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" '
            f'viewBox="0 0 {_PAT_W} {_PAT_H}" preserveAspectRatio="xMidYMid slice">'
            f'{_cover_pattern_elems(title, color)}</svg>')


def valid_hex(hexcolor):
    """校验 #rgb / #rrggbb 格式，book.toml 里的 accent 手填最容易出错。"""
    return bool(re.fullmatch(r"#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})", hexcolor or ""))


def lighten(hexcolor, amount=0.22):
    """把颜色向白色插值，用于夜间模式提亮 accent 以保证暗底对比度。

    非法颜色不应让整个构建崩掉：回退到默认色。
    """
    if not valid_hex(hexcolor):
        print(f"  ⚠ 非法颜色 {hexcolor!r}，回退为默认色 {DEFAULT_ACCENT}")
        hexcolor = DEFAULT_ACCENT
    h = hexcolor.lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    f = lambda c: min(255, round(c + (255 - c) * amount))
    return f"#{f(r):02x}{f(g):02x}{f(b):02x}"


# ---------------------------------------------------------------- 样式

ASSET_FILES = ("base.css", "reader.css", "shelf.css", "theme.js", "reader.js",
               "shelf.js", "shelf-prepaint.js", "reader-prepaint.js")


def _asset(fname):
    """读 templates/assets/ 下的样式与脚本。

    站点产物仍是内联全部样式的单文件 HTML；拆成独立资源只是让
    编辑器能 lint / 高亮，不再在几百行字符串里数大括号。
    """
    return (BASE / "templates" / "assets" / fname).read_text(encoding="utf-8")


def refresh_assets():
    """每次构建前重读资源文件。

    --watch/--serve 是长驻进程：模块级常量只在 import 时读一次，改
    reader.css 后重建出来的还是旧样式。按构建现读 + iter_watch_files
    监视资源目录，改完资源约 1 秒内自动生效。
    """
    global BASE_CSS, READER_CSS, SHELF_CSS, THEME_JS, READER_JS, SHELF_JS, \
        SHELF_PREPAINT_JS, READER_PREPAINT_JS
    BASE_CSS = _asset("base.css")
    READER_CSS = _asset("reader.css")
    SHELF_CSS = _asset("shelf.css")
    THEME_JS = _asset("theme.js")
    READER_JS = _asset("reader.js")
    SHELF_JS = _asset("shelf.js")
    SHELF_PREPAINT_JS = _asset("shelf-prepaint.js")
    READER_PREPAINT_JS = _asset("reader-prepaint.js")


BASE_CSS = _asset("base.css")

READER_CSS = _asset("reader.css")

SHELF_CSS = _asset("shelf.css")


# ---------------------------------------------------------------- 脚本

# 主题偏好：书架与所有阅读页共用同一个 key，切换后跨页保持
THEME_JS = _asset("theme.js")

READER_JS = _asset("reader.js")

SHELF_JS = _asset("shelf.js")

# 预绘制：在 <head> 内同步执行，赶在首次渲染前把主题 / 字号 / 侧栏状态
# 钉到 <html> 上，避免先刷默认态再被 </body> 前的脚本跳变。
SHELF_PREPAINT_JS = _asset("shelf-prepaint.js")

READER_PREPAINT_JS = _asset("reader-prepaint.js")


# ---------------------------------------------------------------- 导出

EPUB_CSS = """body{margin:5%;line-height:1.9;font-family:serif}
h2{font-weight:400;letter-spacing:.1em}
h3{font-weight:400;letter-spacing:.15em;margin-top:2.5em}
p{margin:.2em 0;text-indent:2em;text-align:justify}
blockquote{margin:1em 8%;font-size:.92em}
p.end{text-align:center;text-indent:0;letter-spacing:.2em;margin-top:3em}"""

_EPUB_CONTAINER = """<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/></rootfiles>
</container>
"""


def _plain(text):
    """inline() 产出的 HTML 还原成纯文本（TXT 导出用）。"""
    t = text.replace("<br>", "\n")
    return html.unescape(re.sub(r"<[^>]+>", "", t))


def chapter_label(ch):
    """「卷一 · 第 2 章 · 标题」式的章节标签，阅读页/EPUB/TXT 共用。"""
    label = f"第 {ch['num']} 章"
    if ch.get("volume"):
        label = f"{ch['volume']} · " + label
    if ch.get("title"):
        label += f" · {ch['title']}"
    return label


def export_txt(book):
    """整本 TXT：章标题 + 空行分段，网文平台（番茄等）投稿的通用格式。

    取默认阅读线（book.toml 可配置的双线首线），已按 extract_body 剔除创作笔记。
    """
    lines = [book["title"]]
    if book.get("subtitle"):
        lines.append(book["subtitle"])
    lines.append("")
    for ch in path_chapters(book["chapters"], book.get("default_path", DEFAULT_PATH)):
        lines.append(chapter_label(ch))
        lines.append("")
        for kind, content in ch["blocks"]:
            if kind == "section":
                mark, sub = content
                lines.append(f"{mark}、{sub}" if sub else str(mark))
                lines.append("")
            elif kind == "quote":
                for msg in content:
                    lines.append(_plain(msg))
                    lines.append("")
            elif kind == "endmark":
                lines.append(_plain(content))
                lines.append("")
            else:
                lines.append(_plain(content))
                lines.append("")
    return "\n".join(lines).rstrip() + "\n"


def _epub_clean(text):
    """去掉 XML 非法控制字符（\x00-\x08 等）——html.escape 不管它们，
    留着会让部分阅读器拒开整本书。
    """
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)


def _epub_chapter_xhtml(ch):
    label = html.escape(chapter_label(ch), quote=False)
    parts = [
        '<?xml version="1.0" encoding="utf-8"?>',
        "<!DOCTYPE html>",
        '<html xmlns="http://www.w3.org/1999/xhtml"'
        ' xmlns:epub="http://www.idpf.org/2007/ops"'
        ' xml:lang="zh-CN" lang="zh-CN">',
        "<head><title>" + label + "</title>"
        '<link rel="stylesheet" type="text/css" href="style.css"/></head>',
        "<body>",
        f"<section epub:type=\"chapter\">",
        f"<h2>{label}</h2>",
    ]
    for kind, content in ch["blocks"]:
        if kind == "section":
            mark, sub = content
            txt = html.escape(f"{mark}、{sub}" if sub else str(mark), quote=False)
            parts.append(f"<h3>{txt}</h3>")
        elif kind == "quote":
            msgs = "".join(f"<p>{_epub_clean(m).replace('<br>', '<br/>')}</p>"
                           for m in content)
            parts.append(f"<blockquote>{msgs}</blockquote>")
        elif kind == "endmark":
            parts.append(f'<p class="end">'
                         f"{_epub_clean(content).replace('<br>', '<br/>')}</p>")
        else:
            parts.append(f"<p>{_epub_clean(content).replace('<br>', '<br/>')}</p>")
    parts.append("</section>\n</body>\n</html>\n")
    return "\n".join(parts)


_COVER_MIME = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
               ".gif": "image/gif", ".svg": "image/svg+xml", ".webp": "image/webp"}


def _epub_cover_svg(book):
    """用主色 + 书名生成一张 800×1200 的 SVG 封面（cover 字段为空时）。"""
    accent, lit = book["accent"], lighten(book["accent"], 0.55)
    title = html.escape(book["title"], quote=False)
    sub = html.escape(book.get("subtitle") or "", quote=False)
    n = max(1, len(book["title"]))
    fs = max(46, min(96, int(1500 / n)))
    sub_el = (f'<text x="400" y="985" text-anchor="middle" fill="{lit}" '
              f'font-size="30" letter-spacing="10">{sub}</text>') if sub else ""
    return f"""<svg xmlns="http://www.w3.org/2000/svg" width="800" height="1200" viewBox="0 0 800 1200">
<rect width="800" height="1200" fill="{accent}"/>
<g transform="scale(4)">{_cover_pattern_elems(book["title"], lighten(book["accent"], 0.6))}</g>
<rect x="46" y="46" width="708" height="1108" fill="none" stroke="{lit}" stroke-width="2" opacity="0.55"/>
<line x1="330" y1="240" x2="470" y2="240" stroke="{lit}" stroke-width="2" opacity="0.8"/>
<text x="400" y="560" text-anchor="middle" fill="#ffffff" font-size="{fs}"
 font-family="'Songti SC','SimSun',serif" letter-spacing="14">{title}</text>
<line x1="330" y1="920" x2="470" y2="920" stroke="{lit}" stroke-width="2" opacity="0.8"/>
{sub_el}
</svg>
"""


def _epub_cover(book):
    """封面 (文件名, 字节, mime)：cover 字段有图用真图，否则生成式 SVG。"""
    cover = book.get("cover") or ""
    if cover:
        src = book["dir"] / cover
        if src.exists() and src.suffix.lower() in _COVER_MIME:
            return (f"cover{src.suffix.lower()}", src.read_bytes(),
                    _COVER_MIME[src.suffix.lower()])
        print(f"  ⚠ {book['title']}：封面图不可用（{cover}），改用生成式封面")
    svg = _epub_cover_svg(book)
    return ("cover.svg", svg.encode("utf-8"), "image/svg+xml")


def export_epub(book, path):
    """零依赖导出 EPUB 3：EPUB 本质是 zip + 固定骨架。

    唯一硬性要求：mimetype 必须第一个写入且不压缩（ZIP_STORED）。
    取默认阅读线；骨架四件：container.xml / content.opf /
    nav.xhtml / 章节 xhtml，现代阅读器（Apple Books、Calibre、微信读书）
    以 nav 为目录，不需要 EPUB2 的 NCX。
    """
    chapters = path_chapters(book["chapters"], book.get("default_path", DEFAULT_PATH))
    slug = re.sub(r"[^\w\u4e00-\u9fff]+", "-", book["title"]).strip("-") or "book"
    modified = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    cover_name, cover_bytes, cover_mime = _epub_cover(book)
    opf_items, spine, nav_items, files = [], [], [], []
    for i, ch in enumerate(chapters, 1):
        href = f"ch{i:03d}.xhtml"
        opf_items.append(
            f'<item id="c{i}" href="{href}" media-type="application/xhtml+xml"/>'
        )
        spine.append(f'<itemref idref="c{i}"/>')
        nav_items.append(
            f"<li><a href=\"{href}\">{html.escape(chapter_label(ch), quote=False)}</a></li>"
        )
        files.append((href, _epub_chapter_xhtml(ch)))
    opf = f"""<?xml version="1.0" encoding="utf-8"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="bookid" xml:lang="zh-CN">
  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
    <dc:identifier id="bookid">urn:xhw:{html.escape(slug)}</dc:identifier>
    <dc:title>{html.escape(book["title"], quote=False)}</dc:title>
    <dc:language>zh-CN</dc:language>
    {f'<dc:description>{html.escape(book["tagline"], quote=False)}</dc:description>' if book.get("tagline") else ""}
    <meta name="cover" content="cover-image"/>
    <meta property="dcterms:modified">{modified}</meta>
  </metadata>
  <manifest>
    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
    <item id="css" href="style.css" media-type="text/css"/>
    <item id="cover-image" href="{cover_name}" media-type="{cover_mime}" properties="cover-image"/>
    {chr(10).join('    ' + it for it in opf_items)}
  </manifest>
  <spine>
    <itemref idref="nav"/>
    {chr(10).join('    ' + it for it in spine)}
  </spine>
</package>
"""

    nav = """<?xml version="1.0" encoding="utf-8"?>
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops" xml:lang="zh-CN" lang="zh-CN">
<head><title>目录</title></head>
<body><nav epub:type="toc" id="toc"><h1>目录</h1><ol>
""" + "\n".join(nav_items) + "\n</ol></nav></body>\n</html>\n"

    with zipfile.ZipFile(path, "w") as z:
        z.writestr(zipfile.ZipInfo("mimetype"), "application/epub+zip",
                   zipfile.ZIP_STORED)
        z.writestr(f"OEBPS/{cover_name}", cover_bytes, zipfile.ZIP_DEFLATED)
        z.writestr("META-INF/container.xml", _EPUB_CONTAINER, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/content.opf", opf, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/nav.xhtml", nav, zipfile.ZIP_DEFLATED)
        z.writestr("OEBPS/style.css", EPUB_CSS, zipfile.ZIP_DEFLATED)
        for href, xhtml in files:
            z.writestr(f"OEBPS/{href}", xhtml, zipfile.ZIP_DEFLATED)
    return path


# ---------------------------------------------------------------- 分章站点
#
# site/chapters/：每章一个 URL 的轻量静态站（对外部署用，SEO/微信打开友好）。
# 单文件 HTML 继续保留——两套产物互为补充：分章站给线上，单文件给分享。
# RSS 与 sitemap 需要知道部署域名：工作区根目录放 site.toml（url = "https://…"），
# 没有就只出 HTML，不出 feed。

CHAPTERS_CSS = """:root{--ink:#3a3a3a;--faint:#9a938a;--rule:#e4e0da;--bg:#fdfcfa}
@media (prefers-color-scheme:dark){:root{--ink:#c9c4bc;--faint:#7d7770;
  --rule:#33312d;--bg:#181614}}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--ink);
  font-family:"Songti SC","Noto Serif CJK SC","SimSun",serif}
.crumbs{max-width:42em;margin:0 auto;padding:18px 22px 0;font-family:var(--sans,sans-serif);
  font-size:12px;letter-spacing:.12em}
.crumbs a{color:var(--faint);text-decoration:none}
.crumbs a:hover{color:var(--ink)}
main{max-width:42em;margin:0 auto;padding:26px 22px 60px}
h1{font-weight:400;font-size:22px;letter-spacing:.14em;margin:0 0 8px;text-align:center}
h1 small{display:block;margin-top:10px;font-size:12px;letter-spacing:.28em;color:var(--faint)}
.rule{width:34px;height:1px;background:var(--rule);margin:26px auto}
h3{font-weight:400;font-size:14px;letter-spacing:.24em;color:var(--faint);
  text-align:center;margin:2.6em 0 1.4em}
p{margin:0 0 1.1em;font-size:16.5px;line-height:2;text-indent:2em;text-align:justify}
blockquote{margin:1.6em 4%;padding:12px 16px;background:color-mix(in srgb,var(--ink) 5%,transparent);
  border-left:2px solid var(--rule);border-radius:2px 10px 10px 10px}
blockquote p{font-size:14.5px;line-height:1.85;color:color-mix(in srgb,var(--ink) 78%,var(--bg));
  text-indent:0;margin:0 0 .4em}
p.end{text-align:center;text-indent:0;letter-spacing:.22em;color:var(--faint);margin-top:3em}
.pn{max-width:42em;margin:0 auto;padding:0 22px 30px;display:flex;
  justify-content:space-between;gap:14px;font-family:var(--sans,sans-serif);
  font-size:12.5px;letter-spacing:.06em}
.pn a{color:var(--ink);text-decoration:none;border-bottom:1px solid var(--rule);padding-bottom:2px}
.pn a:hover{border-color:var(--ink)}
footer{max-width:42em;margin:0 auto;padding:0 22px 60px;text-align:center;
  font-family:var(--sans,sans-serif);font-size:11px;letter-spacing:.18em;color:var(--faint)}
footer a{color:var(--faint);text-decoration:none;margin:0 .8em}
footer a:hover{color:var(--ink)}
.toc{list-style:none;margin:20px 0 0;padding:0}
.toc li{border-bottom:1px solid var(--rule)}
.toc a{display:flex;justify-content:space-between;padding:12px 2px;color:var(--ink);
  text-decoration:none;font-size:15px}
.toc a:hover{color:var(--faint)}
.toc .n{color:var(--faint);font-size:12px;letter-spacing:.1em}
.cover{width:150px;height:auto;display:block;margin:8px auto 22px;border:1px solid var(--rule)}
.age-gate{display:none;position:fixed;inset:0;z-index:1000;background:var(--bg);
  align-items:center;justify-content:center;padding:24px}
.age-card{max-width:30em;text-align:center;border:1px solid var(--rule);padding:32px 26px}
.age-card p{text-indent:0;text-align:center}
.age-card button{font:inherit;padding:10px 18px;cursor:pointer;color:var(--ink);
  background:var(--bg);border:1px solid var(--rule)}
.age-card a{display:inline-block;margin-left:18px;color:var(--faint)}
html:not([data-adult-ok="1"]) body[data-adult="1"] > :not(.age-gate):not(script){visibility:hidden}
html:not([data-adult-ok="1"]) body[data-adult="1"] .age-gate{display:flex}
"""


def _chapters_blocks_html(ch):
    """分章页正文（HTML，无需 XHTML 自闭合）。"""
    out = []
    for kind, content in ch["blocks"]:
        if kind == "section":
            mark, sub = content
            label = html.escape(f"{mark} · {sub}" if sub else str(mark), quote=False)
            out.append(f"<h3>{label}</h3>")
        elif kind == "quote":
            msgs = "".join(f"<p>{m}</p>" for m in content)
            out.append(f"<blockquote>{msgs}</blockquote>")
        elif kind == "endmark":
            out.append(f'<p class="end">{content}</p>')
        else:
            out.append(f"<p>{content}</p>")
    return "\n".join(out)


def _giscus_html(site_cfg):
    """giscus 评论块（GitHub Discussions 后端）。三个参数都配了才渲染——
    repo / repo_id / category_id 从 giscus.app 生成，属于部署者自己的仓库。
    """
    repo = str(site_cfg.get("giscus_repo") or "").strip()
    repo_id = str(site_cfg.get("giscus_repo_id") or "").strip()
    category_id = str(site_cfg.get("giscus_category_id") or "").strip()
    if not (repo and repo_id and category_id):
        return ""
    return f"""<section class="giscus-wrap"><script src="https://giscus.app/client.js"
 data-repo="{html.escape(repo, quote=True)}"
 data-repo-id="{html.escape(repo_id, quote=True)}"
 data-category="Announcements" data-category-id="{html.escape(category_id, quote=True)}"
 data-mapping="pathname" data-strict="0" data-reactions-enabled="1"
 data-emit-metadata="0" data-input-position="top" data-theme="preferred_color_scheme"
 data-lang="zh-CN" data-loading="lazy" crossorigin="anonymous" async></script></section>"""


def chapter_page_name(ch):
    """分章页的稳定文件名；卷号和阅读线参与命名，避免同章号覆盖。"""
    name = f"ch{ch['num']:03d}"
    volume = ch.get("volume") or ""
    if volume:
        name += "-v" + hashlib.sha256(volume.encode("utf-8")).hexdigest()[:12]
    if ch.get("branch"):
        name += "-" + ch["branch"]
        anchor_base = f"ch-{ch['num']}-{ch['branch']}"
        anchor = ch.get("anchor") or ""
        if anchor.startswith(anchor_base + "-"):
            name += "-" + anchor[len(anchor_base) + 1:]
    return name + ".html"


def render_chapter_page(book, ch, prev_ch, next_ch, giscus=""):
    """单章静态页：样式外链共享一份；成人向首次阅读需确认。"""
    label = html.escape(chapter_label(ch), quote=False)
    title = html.escape(book["title"], quote=False)
    prev_a = (f'<a href="{html.escape(chapter_page_name(prev_ch), quote=True)}">← 上一章</a>'
              if prev_ch else "<span></span>")
    next_a = (f'<a href="{html.escape(chapter_page_name(next_ch), quote=True)}">下一章 →</a>'
              if next_ch else "<span></span>")
    adult_head = """
<script>try{if(localStorage.getItem('xhw-adult-gate'))
document.documentElement.dataset.adultOk='1'}catch(e){}</script>""" if book["adult"] else ""
    adult_body = """
<div class="age-gate" role="dialog" aria-modal="true" aria-label="成人内容提示">
  <div class="age-card"><h2>成人向内容</h2>
    <p>本书包含成人向描写。继续阅读即表示你已年满 18 周岁，并愿意接触此类内容。</p>
    <button type="button" id="age-ok">我已满 18 岁 · 继续</button>
    <a href="../index.html">离开</a>
    <noscript><p>请启用 JavaScript 后确认年龄。</p></noscript>
  </div>
</div>
<script>document.getElementById('age-ok').onclick=function(){
  try{localStorage.setItem('xhw-adult-gate','1')}catch(e){}
  document.documentElement.dataset.adultOk='1';
};</script>""" if book["adult"] else ""
    adult_attr = ' data-adult="1"' if book["adult"] else ""
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{label} · {title}</title>
<meta name="description" content="{html.escape(book.get('tagline') or title, quote=True)}">
<link rel="stylesheet" href="../style.css">
{adult_head}
</head>
<body{adult_attr}>
<nav class="crumbs"><a href="../index.html">← 书架</a> · <a href="index.html">本书目录</a> · <a href="../../{html.escape(book['file'], quote=True)}">单文件版</a></nav>
<main>
<h1>{label}<small>{title}</small></h1>
<div class="rule"></div>
{_chapters_blocks_html(ch)}
</main>
<nav class="pn">{prev_a}{next_a}</nav>
{giscus}
<footer><a href="../../index.html">返回书架</a>{'<a href="feed.xml">RSS 订阅</a>' if book.get('_has_feed') else ''}</footer>
{adult_body}
</body>
</html>
"""


def render_chapters_book_index(book, chapters):
    """分章站的书内目录页。"""
    title = html.escape(book["title"], quote=False)
    items = "\n".join(
        f'<li><a href="{html.escape(chapter_page_name(c), quote=True)}"><span>{html.escape(c["title"], quote=False)}</span>'
        f'<span class="n">第 {c["num"]} 章</span></a></li>'
        for c in chapters)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title} · 分章目录</title>
<meta name="description" content="{html.escape(book.get('tagline') or title, quote=True)}">
<link rel="stylesheet" href="../style.css">
</head>
<body>
<nav class="crumbs"><a href="../index.html">← 书架</a> · <a href="../../{html.escape(book['file'], quote=True)}">单文件版</a></nav>
<main>
<h1>{title}<small>{html.escape(book.get("subtitle") or "全 " + str(len(chapters)) + " 章", quote=False)}</small></h1>
<div class="rule"></div>
<ul class="toc">
{items}
</ul>
</main>
<footer><a href="../../index.html">返回书架</a></footer>
</body>
</html>
"""


def render_chapters_shelf(books):
    """分章站的顶层目录。"""
    items = "\n".join(
        f'<li><a href="{html.escape(Path(b["file"]).stem, quote=True)}/index.html">'
        f'<span>{html.escape(b["title"], quote=False)}</span>'
        f'<span class="n">{b.get("chapter_count") or len(b["chapters"])} 章 · '
        f'{html.escape(b["status"], quote=False)}</span></a></li>'
        for b in books)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>书架 · 分章阅读</title>
<link rel="stylesheet" href="style.css">
</head>
<body>
<nav class="crumbs"><a href="../index.html">单文件书架</a></nav>
<main>
<h1>分章阅读<small>每章一个地址，适合线上阅读与分享</small></h1>
<div class="rule"></div>
<ul class="toc">
{items}
</ul>
</main>
<footer><a href="../index.html">单文件版书架</a></footer>
</body>
</html>
"""


def _rss_item(link, title, timestamp):
    from email.utils import formatdate
    return (f"<item><title>{html.escape(title, quote=False)}</title>"
            f"<link>{html.escape(link, quote=True)}</link>"
            f"<guid isPermaLink=\"true\">{html.escape(link, quote=True)}</guid>"
            f"<pubDate>{formatdate(timestamp, usegmt=True)}</pubDate></item>")


def render_rss(book, chapters, site_url):
    """按章 mtime 出 RSS 2.0——读者订阅追更的最轻方案。"""
    from urllib.parse import quote
    slug = quote(Path(book["file"]).stem)
    base = site_url.rstrip("/")
    items = []
    for ch in chapters[-20:]:
        src = ch.get("_src_mtime")
        link = f"{base}/chapters/{slug}/{quote(chapter_page_name(ch))}"
        items.append(_rss_item(link, chapter_label(ch), src or time.time()))
    title = html.escape(book["title"], quote=False)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<rss version="2.0"><channel>
<title>{title}</title>
<link>{html.escape(f'{base}/chapters/{slug}/index.html', quote=True)}</link>
<description>{html.escape(book.get('tagline') or title, quote=False)}</description>
<language>zh-CN</language>
{chr(10).join(items)}
</channel></rss>
"""


def render_sitemap(books, site_url):
    """全站 URL 清单：分章页 + 各书单文件 + 书架。"""
    from urllib.parse import quote
    base = site_url.rstrip("/")
    urls = [f"{base}/", f"{base}/chapters/"]
    for b in books:
        slug = quote(Path(b["file"]).stem)
        urls.append(f"{base}/{quote(b['file'])}")
        urls.append(f"{base}/chapters/{slug}/index.html")
        for ch in path_chapters(b["chapters"], b.get("default_path", DEFAULT_PATH)):
            urls.append(f"{base}/chapters/{slug}/{quote(chapter_page_name(ch))}")
    body = "\n".join(
        f"  <url><loc>{html.escape(u, quote=True)}</loc></url>" for u in urls)
    return ('<?xml version="1.0" encoding="utf-8"?>\n'
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n'
            + body + "\n</urlset>\n")


def build_chapters_site(books, site_url="", site_cfg=None):
    """写 site/chapters/ 全套：style + 书架 + 每书目录/章节页（+ feed/sitemap/评论）。

    返回写入的相对路径集合（进 .build-manifest，改名/删书后可被清理）。
    """
    from urllib.parse import quote
    site_cfg = site_cfg or {}
    giscus = _giscus_html(site_cfg)
    chapters_dir = SITE_DIR / "chapters"
    chapters_dir.mkdir(exist_ok=True)
    (chapters_dir / "style.css").write_text(CHAPTERS_CSS, encoding="utf-8")
    written = {"chapters/style.css"}
    if giscus:
        (chapters_dir / "style.css").write_text(
            CHAPTERS_CSS + "\n.giscus-wrap{max-width:42em;margin:0 auto 30px;padding:0 22px}\n",
            encoding="utf-8")

    for b in books:
        slug = quote(Path(b["file"]).stem)
        book_dir = chapters_dir / Path(b["file"]).stem
        book_dir.mkdir(exist_ok=True)
        chapters = path_chapters(b["chapters"], b.get("default_path", DEFAULT_PATH))
        names = [chapter_page_name(ch) for ch in chapters]
        if len(names) != len(set(names)):
            raise ValueError(f"{b['title']}：分章页文件名重复，请检查同卷同线的重名章节")
        # 本章本线源文件的 mtime 作 RSS 更新时间（不按章号匹配，
        # 否则卷二 ch001 / 刀线的改动会顶掉卷一 ch001 / 美线的时间）
        for ch in chapters:
            ch["_src_mtime"] = 0
            src = ch.get("src")
            if src:
                try:
                    ch["_src_mtime"] = src.stat().st_mtime
                except OSError:
                    pass
        b["_has_feed"] = bool(site_url)
        book_index = render_chapters_book_index(b, chapters)
        (book_dir / "index.html").write_text(book_index, encoding="utf-8")
        written.add(f"chapters/{Path(b['file']).stem}/index.html".replace("\\", "/"))
        for i, ch in enumerate(chapters):
            prev_ch = chapters[i - 1] if i > 0 else None
            next_ch = chapters[i + 1] if i + 1 < len(chapters) else None
            page = render_chapter_page(b, ch, prev_ch, next_ch, giscus=giscus)
            name = chapter_page_name(ch)
            (book_dir / name).write_text(page, encoding="utf-8")
            written.add(f"chapters/{Path(b['file']).stem}/{name}".replace("\\", "/"))
        if site_url:
            (book_dir / "feed.xml").write_text(
                render_rss(b, chapters, site_url), encoding="utf-8")
            written.add(f"chapters/{Path(b['file']).stem}/feed.xml".replace("\\", "/"))

    (chapters_dir / "index.html").write_text(render_chapters_shelf(books),
                                             encoding="utf-8")
    written.add("chapters/index.html")
    if site_url:
        (SITE_DIR / "sitemap.xml").write_text(render_sitemap(books, site_url),
                                              encoding="utf-8")
        written.add("sitemap.xml")
        (SITE_DIR / "robots.txt").write_text(
            f"User-agent: *\nAllow: /\nSitemap: {site_url.rstrip('/')}/sitemap.xml\n",
            encoding="utf-8")
        written.add("robots.txt")
    for b in books:
        b.pop("_has_feed", None)
        for ch in b["chapters"]:
            ch.pop("_src_mtime", None)
    return written


# ---------------------------------------------------------------- 平台投稿

_WECHAT_P = ("font-size:15px;line-height:1.75;color:#3f3f3f;letter-spacing:.5px;"
             "text-align:justify;margin:0 0 16px;")


def export_platform(book):
    """生成投稿稿：公众号富文本 HTML（全内联样式）+ 知乎 Markdown。

    公众号编辑器不认 <style>，样式必须逐标签内联；浏览器打开 HTML 全选
    复制即可粘贴。知乎走「导入文件」，Markdown 原样即可。
    输出到 素材/投稿/（不进网页）。返回 (html_path, md_path)。
    """
    chapters = path_chapters(book["chapters"], book.get("default_path", DEFAULT_PATH))
    body = []
    for ch in chapters:
        label = html.escape(chapter_label(ch), quote=False)
        body.append(
            f'<h2 style="text-align:center;font-size:17px;font-weight:600;'
            f'color:#1a1a1a;letter-spacing:1px;margin:40px 0 24px;">{label}</h2>'
        )
        for kind, content in ch["blocks"]:
            if kind == "section":
                mark, sub = content
                txt = html.escape(f"{mark} · {sub}" if sub else str(mark), quote=False)
                body.append(f'<p style="text-align:center;letter-spacing:.35em;'
                            f'color:#8a8a8a;font-size:14px;margin:30px 0;">{txt}</p>')
            elif kind == "quote":
                msgs = []
                for m in content:
                    t = m.replace("<strong>", '<strong style="color:#444">')
                    msgs.append(f'<p style="font-size:14px;line-height:1.8;color:#666;'
                                f'margin:0 0 6px;text-indent:0;">{t}</p>')
                body.append(f'<blockquote style="margin:16px 4%;padding:10px 14px;'
                            f'background:#f7f7f7;border-left:3px solid #dcdcdc;'
                            f'border-radius:2px 10px 10px 2px;">{"".join(msgs)}</blockquote>')
            elif kind == "endmark":
                body.append(f'<p style="text-align:center;letter-spacing:.3em;'
                            f'color:#999;font-size:13px;margin:34px 0;">{content}</p>')
            else:
                t = content.replace("<strong>", '<strong style="color:#111">')
                body.append(f'<p style="{_WECHAT_P}">{t}</p>')

    title = html.escape(book["title"], quote=False)
    html_doc = f"""<!DOCTYPE html>
<html lang="zh-CN">
<head><meta charset="utf-8"><title>{title} · 公众号版</title></head>
<body style="margin:0;background:#ffffff;">
<section style="max-width:677px;margin:0 auto;padding:28px 16px 60px;
 font-family:-apple-system,'PingFang SC','Microsoft YaHei',sans-serif;">
<h1 style="text-align:center;font-size:20px;color:#1a1a1a;margin:16px 0 8px;">{title}</h1>
<p style="text-align:center;color:#999;font-size:13px;margin:0 0 30px;">{html.escape(book.get('subtitle') or '', quote=False)}</p>
{chr(10).join(body)}
</section>
</body>
</html>
"""
    md_parts = [f"# {book['title']}"]
    if book.get("subtitle"):
        md_parts.append(f"> {book['subtitle']}")
    for ch in chapters:
        src = ch.get("src")
        md_parts.append(f"\n## {chapter_label(ch)}\n")
        if src:
            md_parts.append(extract_body(src.read_text(encoding="utf-8-sig")).strip())
        else:
            md_parts.append(_plain("\n".join(
                p if isinstance(p, str) else " ".join(p)
                for k, p in ch["blocks"] if k == "para")))

    out_dir = BASE / "素材" / "投稿"
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = safe_filename(book["title"])
    html_path = out_dir / f"{stem}-公众号.html"
    md_path = out_dir / f"{stem}-知乎.md"
    html_path.write_text(html_doc, encoding="utf-8")
    md_path.write_text("\n".join(md_parts) + "\n", encoding="utf-8")
    return html_path, md_path


# ---------------------------------------------------------------- 渲染

def render_chapter(ch, first, prev_ch=None, next_ch=None, nav_alts=None):
    """渲染一章。first=True 表示是全书第一章，其内容不做滚动入场（首屏可见）。
    prev_ch / next_ch 用于章末的上一章/下一章导航。

    nav_alts：双线书的共用章传 {线key: (上一章, 下一章)}——各渲染一条
    导航，CSS 按当前阅读线显隐，切线不用改 DOM。

    返回 (html, sections)：sections 是带小标题的分节符列表 [(id, label), ...]，
    只有像「出差」这种单章多分节、每节带小标题的情况才非空，用于目录里的二级
    导航——没有小标题的纯装饰性「一/二/三」不值得占目录条目。
    """
    vol = ch.get("volume") or ""
    branch = ch.get("branch") or ""
    br_label = ch.get("branch_label") or ""
    ch_label = (f"{vol} · " if vol else "") + f'第 {ch["num"]} 章'
    if br_label:
        ch_label += f'<span class="br">{html.escape(br_label)}</span>'
    ch_label += f'<span class="rt">约 {reading_minutes(ch)} 分钟</span>'
    branch_attr = f' data-branch="{html.escape(branch)}"' if branch else ""
    out = [
        f'<section class="chapter" id="{ch["anchor"]}"'
        f' data-num="{ch["num"]}" data-title="{html.escape(ch["title"])}"'
        f' data-anchor="{ch["anchor"]}" data-volume="{html.escape(vol)}"'
        f'{branch_attr}>',
        '  <header class="ch-head">',
        f'    <div class="ch-num">{ch_label}</div>',
        f'    <h2 class="ch-title">{html.escape(ch["title"])}</h2>',
        '    <div class="ch-line"></div>',
        "  </header>",
    ]
    # 首章前几段已在首屏，不加 .rise 以免打开瞬间空白
    plain = 6 if first else 0
    seen = 0
    sections = []
    for kind, content in ch["blocks"]:
        seen += 1
        r = "" if seen <= plain else " rise"
        if kind == "section":
            mark, sub = content
            label = html.escape(mark)
            if sub:
                sec_id = f'{ch["anchor"]}-sec{len(sections) + 1}'
                sections.append((sec_id, sub))
                label += f"<em>{html.escape(sub)}</em>"
                out.append(f'  <div class="sec-mark" id="{sec_id}"><b>{label}</b></div>')
            else:
                out.append(f'  <div class="sec-mark"><b>{label}</b></div>')
        elif kind == "quote":
            msgs = "".join(f"<p>{m}</p>" for m in content)
            out.append(f'  <blockquote class="msg{r}">{msgs}</blockquote>')
        elif kind == "endmark":
            out.append(f'  <p class="end-mark{r}">{content}</p>')
        else:
            out.append(f'  <p class="p{r}">{content}</p>')
    out.append("</section>")
    # 章末横线与导航在 <section> 之外：分叉章也要带上 data-branch，
    # 否则切到另一条线时，隐藏章的横线和「上一章/下一章」仍会露出来
    out.append(f'<div class="ch-end"{branch_attr}></div>')

    def nav_html(prev_c, next_c):
        if not prev_c and not next_c:
            return ""
        prev_a = (
            f'<a href="#{prev_c["anchor"]}">← {html.escape(prev_c["title"])}</a>'
            if prev_c else '<span></span>'
        )
        next_a = (
            f'<a href="#{next_c["anchor"]}">{html.escape(next_c["title"])} →</a>'
            if next_c else '<span></span>'
        )
        return f"{prev_a}{next_a}"

    if nav_alts and len(nav_alts) > 1:
        for key, (p_c, n_c) in nav_alts.items():
            inner = nav_html(p_c, n_c)
            if inner:
                out.append(
                    f'<nav class="ch-nav nav-{html.escape(key)}" data-nav>{inner}</nav>'
                )
    elif prev_ch or next_ch:
        out.append(f'<nav class="ch-nav"{branch_attr}>{nav_html(prev_ch, next_ch)}</nav>')
    return "\n".join(out), sections


def fork_info(book):
    """双线书的分叉信息：label、各线 (key, 展示名, 入口锚点)、分叉章号范围。

    展示名与入口锚点从实际章节推导，不再写死某本书；每条线的选线卡
    一句话简介从 book.toml 的 line_note_<key> 读（可省）。
    """
    branches = sorted(
        {c["branch"] for c in book["chapters"] if c.get("branch")},
        key=branch_order_key,
    )
    labels, entries = {}, {}
    for c in book["chapters"]:
        if c.get("branch") and c["branch"] not in entries:
            entries[c["branch"]] = c["anchor"]
            labels[c["branch"]] = c.get("branch_label") or c["branch"]
    nums = sorted({c["num"] for c in book["chapters"] if c.get("branch")})
    if not nums:
        rng = ""
    elif nums[0] == nums[-1]:
        rng = str(nums[0])
    else:
        rng = f"{nums[0]}–{nums[-1]}"
    raw = book.get("raw_meta") or {}
    lines = [
        (k, labels.get(k, k), entries.get(k, ""), raw.get(f"line_note_{k}", ""))
        for k in branches
    ]
    label = book.get("fork_label") or "双线"
    return {
        "label": label,
        "lines": lines,
        "range": rng,
        "desc": book.get("fork_desc")
        or "分叉处有两条版本，请选一条读完再继续，两条线不要交叉混读。",
    }


def render_path_pick(fork):
    """选线卡（插在首个分叉章之前）。文案由 book.toml 的 fork_label /
    fork_desc / line_note_<key> 驱动，缺省给通用文案。
    """
    btns = []
    for key, name, anchor, note in fork["lines"]:
        note_html = f"<span>{html.escape(note)}</span>" if note else "<span></span>"
        btns.append(
            f'    <button type="button" class="path-btn" data-path="{html.escape(key)}" '
            f'data-go="{html.escape(anchor)}">\n'
            f"      <b>{html.escape(name)}线</b>\n"
            f"      {note_html}\n"
            f"    </button>"
        )
    return (
        '<aside class="path-pick rise" id="path-pick" aria-label="选择阅读线">\n'
        f'  <p class="kicker">{html.escape(fork["label"])}</p>\n'
        f'  <p class="desc">{html.escape(fork["desc"])}</p>\n'
        '  <div class="btns">\n' + "\n".join(btns) + "\n  </div>\n</aside>"
    )


def render_reader(book, has_shelf):
    """渲染单本书的阅读页（单文件，内联全部样式与脚本）。"""
    chapters = book["chapters"]
    accent, lit = book["accent"], lighten(book["accent"])
    has_fork = book.get("has_fork", False)
    fork = fork_info(book) if has_fork else None
    default_path = book.get("default_path", DEFAULT_PATH)

    def neighbors(ch, path):
        chain = path_chapters(chapters, path)
        try:
            i = next(j for j, c in enumerate(chain) if c["anchor"] == ch["anchor"])
        except StopIteration:
            return None, None
        prev_c = chain[i - 1] if i > 0 else None
        next_c = chain[i + 1] if i + 1 < len(chain) else None
        return prev_c, next_c

    rendered = []
    for i, c in enumerate(chapters):
        # 共用章在双线书里：各线各渲染一条章末导航，CSS 按当前线显隐
        if has_fork and not c.get("branch") and fork:
            alts = {key: neighbors(c, key) for key, *_ in fork["lines"]}
            if len(alts) > 1:
                rendered.append(
                    render_chapter(c, i == 0, nav_alts=alts)
                )
                continue
        path_for_nav = c.get("branch") or default_path
        prev_c, next_c = neighbors(c, path_for_nav)
        rendered.append(
            render_chapter(c, i == 0, prev_ch=prev_c, next_ch=next_c)
        )

    # 正文：在首个分叉章前插入选线卡
    body_parts = []
    pick_inserted = False
    for i, c in enumerate(chapters):
        if has_fork and not pick_inserted and c.get("branch"):
            body_parts.append(render_path_pick(fork))
            pick_inserted = True
        body_parts.append(rendered[i][0])
    body = "\n\n".join(body_parts)

    toc_parts = []
    cur_vol = None
    fork_marked = False
    for i, c in enumerate(chapters):
        vol = c.get("volume") or ""
        if vol != cur_vol:
            cur_vol = vol
            if vol:
                toc_parts.append(f'    <div class="toc-vol">{html.escape(vol)}</div>')
        if has_fork and not fork_marked and c.get("branch"):
            rng = f" · {fork['range']}" if fork["range"] else ""
            toc_parts.append(
                f'    <div class="toc-fork">{html.escape(fork["label"])}{rng}</div>'
            )
            fork_marked = True
        br = c.get("branch") or ""
        br_attr = f' data-branch="{html.escape(br)}"' if br else ""
        br_badge = (
            f'<span class="br">{html.escape(c["branch_label"])}</span>'
            if c.get("branch_label") else ""
        )
        toc_parts.append(
            f'    <a href="#{c["anchor"]}" data-ch="{c["anchor"]}"{br_attr} '
            f'style="--d:{i * 26}ms">'
            f'<i>{c["num"]:02d}</i>{html.escape(c["title"])}{br_badge}</a>'
        )
        for sec_id, sub in rendered[i][1]:
            toc_parts.append(f'    <a href="#{sec_id}" class="sub-toc">{html.escape(sub)}</a>')
    toc_items = "\n".join(toc_parts)

    title = html.escape(book["title"])
    sub = html.escape(book["subtitle"])
    back = ('  <a id="back" href="index.html" aria-label="返回书架">'
            '←<span class="back-txt"> 书架</span></a>\n') if has_shelf else ""
    foot_link = (
        f'  <a href="index.html">← 返回书架</a>' if has_shelf else f"{title}"
    )
    epi = (
        f'  <p class="epi">{escape_epigraph(book["epigraph"])}</p>\n'
        if book["epigraph"] else ""
    )
    sub_el = f'  <div class="sub">{sub}</div>\n' if sub else ""
    page_title = f"{title}{' · ' + sub if sub else ''}"
    desc = html.escape(book["tagline"]) if book["tagline"] else f"{title}，在线阅读"
    favicon = favicon_data_uri(accent)
    n_logic = book.get("chapter_count") or len(chapters)
    meta_line = f"全 {n_logic} 章"
    if has_fork and fork:
        names = " / ".join(html.escape(name) for _, name, _, _ in fork["lines"])
        meta_line += f" · {html.escape(fork['label'])}（{names}）"
    meta_line += f" · {fmt_wan(book['chars'])}"
    body_class = "has-fork" if has_fork else ""
    body_attrs = f' data-adult="1"' if book["adult"] else ""
    if has_shelf:
        body_attrs += ' data-shelf="1"'
    switch_label = ""
    if has_fork and fork and fork["lines"]:
        switch_label = f"线 · {fork['lines'][0][1]}"
    path_switch = (
        '<button type="button" class="path-switch" id="path-switch" '
        f'aria-label="切换阅读线">{switch_label}</button>\n'
        if has_fork else ""
    )
    first_anchor = chapters[0]["anchor"] if chapters else "ch-1"

    # 页脚的 EPUB / TXT 下载（只在书架语境下有意义；单发 HTML 文件时链接会失效）
    export_links = " · ".join(
        f'<a class="dl" href="{html.escape(n)}">下载 {n.rsplit(".", 1)[-1].upper()}</a>'
        for n in book.get("exports", [])
    )

    # 成人向内容门：首次访问需确认，选择记入 localStorage
    gate_html = ""
    if book["adult"]:
        gate_html = """<div id="gate" role="dialog" aria-modal="true" aria-label="成人内容提示">
  <div class="gate-card">
    <p class="kicker">成人向内容</p>
    <p class="desc">本书包含成人向描写。继续阅读即表示你已年满 18 周岁，并愿意接触此类内容。</p>
    <div class="btns">
      <button type="button" id="gate-ok">我已满 18 岁 · 继续</button>
      <button type="button" id="gate-no">离开</button>
    </div>
  </div>
</div>"""

    # 双线的显隐规则按本书实际线生成，支持 mei/dao 之外的任意线 key
    fork_css = ""
    branch_js = ""
    if has_fork and fork:
        rules = []
        for key, _, _, _ in fork["lines"]:
            rules.append(
                f"body[data-path={key}] .chapter[data-branch={key}],"
                f"body[data-path={key}] .ch-end[data-branch={key}],"
                f"body[data-path={key}] #toc a[data-branch={key}]{{display:block}}"
            )
            rules.append(f"body[data-path={key}] .ch-nav.nav-{key},"
                         f"body[data-path={key}] .ch-nav[data-branch={key}]{{display:flex}}")
            rules.append(
                "body[data-path=" + key + "] .path-btn[data-path=" + key + "]{"
                "border-color:color-mix(in srgb,var(--accent) 55%,var(--rule));"
                "background:color-mix(in srgb,var(--accent) 8%,var(--bg))}"
            )
        fork_css = "\n/* 双线显隐（按本书实际线生成） */\n" + "\n".join(rules)
        branches = [key for key, _, _, _ in fork["lines"]]
        labels = {key: name for key, name, _, _ in fork["lines"]}
        branch_js = (
            f"<script>window.XHW_BRANCHES={json.dumps(branches)};"
            f"window.XHW_BRANCH_LABELS={json.dumps(labels, ensure_ascii=False)};</script>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN" data-toc="off"
      style="--accent:{accent};--accent-lit:{lit}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="theme-color" content="{accent}">
<meta name="description" content="{desc}">
<meta property="og:title" content="{page_title}">
<meta property="og:description" content="{desc}">
<meta property="og:type" content="book">
<link rel="icon" href="{favicon}">
<title>{page_title}</title>
<script data-book="{title}">{READER_PREPAINT_JS}</script>
{branch_js}
<style>{BASE_CSS}{READER_CSS}{fork_css}</style>
</head>
<body class="{body_class}" data-book="{title}" data-path="{default_path}"{body_attrs}>
<div id="bar"></div>

<nav id="tools">
{back}  <button class="tbtn" id="menu" aria-label="目录">目录</button>
  <button class="tbtn" id="search" aria-label="搜索全文">搜索</button>
  <span class="now" id="now"></span>
  <button class="tbtn" id="prefs-btn" aria-label="阅读设置" aria-haspopup="dialog"
          aria-expanded="false" aria-controls="prefs">Aa</button>
</nav>

<div id="prefs" role="dialog" aria-label="阅读设置" hidden>
  <div class="pf-row"><span class="pf-k">字号</span>
    <div class="pf-step">
      <button type="button" class="pf-b" id="font-dec" aria-label="字号减小">A−</button>
      <span id="font-val" aria-live="polite">2 / 4</span>
      <button type="button" class="pf-b" id="font-inc" aria-label="字号增大">A+</button>
    </div></div>
  <div class="pf-row"><span class="pf-k">行距</span>
    <div class="pf-seg" role="group" aria-label="行距">
      <button type="button" class="pf-b" data-set-lh="tight">紧凑</button>
      <button type="button" class="pf-b" data-set-lh="">标准</button>
      <button type="button" class="pf-b" data-set-lh="loose">宽松</button>
    </div></div>
  <div class="pf-row"><span class="pf-k">背景</span>
    <div class="pf-seg pf-swatches" role="group" aria-label="背景">
      <button type="button" class="pf-sw" data-set-theme="day" aria-label="日间"><i></i></button>
      <button type="button" class="pf-sw" data-set-theme="sepia" aria-label="米黄"><i></i></button>
      <button type="button" class="pf-sw" data-set-theme="green" aria-label="护眼"><i></i></button>
      <button type="button" class="pf-sw" data-set-theme="night" aria-label="夜间"><i></i></button>
      <button type="button" class="pf-sw" data-set-theme="black" aria-label="纯黑"><i></i></button>
    </div></div>
  <div class="pf-row"><span class="pf-k">字体</span>
    <div class="pf-seg" role="group" aria-label="字体">
      <button type="button" class="pf-b" data-set-font="">宋体</button>
      <button type="button" class="pf-b pf-sans" data-set-font="sans">黑体</button>
    </div></div>
</div>

<div id="scrim"></div>
<aside id="toc" aria-label="章节目录">
  <h3>目 录</h3>
{toc_items}
</aside>

<div id="search-box" hidden>
  <input id="search-input" type="search" placeholder="搜索当前阅读线…" autocomplete="off">
  <div id="search-results" aria-live="polite"></div>
  <div class="s-hint">Esc 关闭 · Ctrl K 呼出</div>
</div>

<header id="cover">
  <div class="rule"></div>
  <h1>{title}</h1>
{sub_el}{epi}  <div class="meta">{meta_line}</div>
  <a class="go" href="#{first_anchor}">开始阅读 <span>→</span></a>
</header>

<main>
{body}
</main>

<footer>
  {title}{' · ' + sub if sub else ''}<br>{foot_link}{'<br>' + export_links if has_shelf and export_links else ''}
</footer>

{path_switch}<button id="top" aria-label="回到顶部">↑</button>
<noscript><style>.rise,.ch-head,.sec-mark{{opacity:1!important;transform:none!important}}
#toc{{transform:none!important}}#scrim{{display:none!important}}
.chapter[data-branch],.ch-end[data-branch]{{display:block!important}}
.ch-nav[data-branch]{{display:flex!important}}
#toc a[data-branch]{{display:block!important}}</style></noscript>
{gate_html}
<script>{THEME_JS}{READER_JS}</script>
</body>
</html>
"""


def render_shelf(books, hist_json="{}"):
    """渲染书架首页。hist_json 是每日总字数快照（码字日历数据源）。"""
    cards = []
    for b in books:
        accent, lit = b["accent"], lighten(b["accent"])
        title = html.escape(b["title"])
        # 竖排书名越长字号越小，保证单列排下不折行
        # 按字数反算字号，让长短书名的竖排块都控制在封面高度的 ~75% 内。
        # 实测：竖排单字实际占高 ≈ 字号 × 1.5（含字距），外加边框上下 padding 24px。
        # 封面 174px，目标总高 ≤ 130px，故可用文字高 ≈ 106px。
        n = max(1, len(b["title"]))
        fs = max(12, min(18, int(106 / (n * 1.5))))
        cover_inner = (
            f'<img src="{html.escape(b["cover"])}" alt="{title} 封面">'
            if b["cover"] else f'<em style="--cvfs:{fs}px">{title}</em>'
        )
        sub = f'      <div class="sub">{html.escape(b["subtitle"])}</div>\n' if b["subtitle"] else ""
        tag = f'      <p class="tag">{html.escape(b["tagline"])}</p>\n' if b["tagline"] else ""
        done = " done" if b["status"] == "已完结" else ""
        badge = '<span class="badge">成人向</span>' if b["adult"] else ""
        # 下载角标成组：窄屏换行时 EPUB / TXT 一起走，不会把 TXT 单独甩到下一行
        dls = "".join(
            f'<span class="dl" data-href="{html.escape(n)}" '
            f'title="下载 {html.escape(n)}">{n.rsplit(".", 1)[-1].upper()}</span>'
            for n in b.get("exports", [])
        )
        if dls:
            dls = f'<span class="dls">{dls}</span>'
        # 「继续阅读」数据：书架读 localStorage 的 xhw-pos-<书名>（阅读页的存档），
        # 用章节表把锚点翻译成「第 N 章 · 标题」和全书进度。[锚点, 章号, 标题, 线]
        toc = [[c["anchor"], c["num"], c["title"], c.get("branch") or "",
                c.get("branch_label") or ""]
               for c in b["chapters"]]
        toc_attr = html.escape(json.dumps(toc, ensure_ascii=False), quote=True)
        # 生成式封面：按书名确定性生成的纹样，叠在 CSS 主色渐变之上
        cv_style = ""
        if not b["cover"]:
            pat = svg_data_uri(cover_pattern_svg(b["title"], accent, 116, 174))
            cv_style = f' style="--pat:url({pat})"'
        cards.append(
            f'''    <a class="card rise" href="{html.escape(b["file"])}"
       style="--bk:{accent};--bk-lit:{lit}" data-book="{title}"
       data-path="{html.escape(b.get("default_path", DEFAULT_PATH))}" data-toc="{toc_attr}">
      {badge}<div class="cv"{cv_style}>{cover_inner}</div>
      <div class="info">
        <h2>{title}</h2>
{sub}{tag}        <div class="resume" hidden><span class="resume-t"></span><i class="resume-bar"><b></b></i></div>
        <div class="foot">
          <span class="pill{done}">{html.escape(b["status"])}</span>
          <span>{b.get("chapter_count") or len(b["chapters"])} 章{" · 双线" if b.get("has_fork") else ""}</span>
          <span>{fmt_wan(b["chars"])}</span>
          {dls}<span class="arrow">→</span>
        </div>
      </div>
    </a>'''
        )

    total = sum(b["chars"] for b in books)
    desc = f"{len(books)} 部作品 · {fmt_wan(total)}，" + "、".join(b["title"] for b in books[:5])
    desc = html.escape(desc)
    favicon = favicon_data_uri(books[0]["accent"] if books else DEFAULT_ACCENT)
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="light dark">
<meta name="description" content="{desc}">
<meta property="og:title" content="书架">
<meta property="og:description" content="{desc}">
<link rel="icon" href="{favicon}">
<title>书架</title>
<script>{SHELF_PREPAINT_JS}</script>
<style>{BASE_CSS}{SHELF_CSS}</style>
</head>
<body>
<nav id="shelf-tools">
  <button class="tbtn" id="theme" aria-label="切换日间夜间">夜间</button>
</nav>

<header class="hero">
  <h1>书架</h1>
  <div class="line"></div>
  <div class="meta">{len(books)} 部作品 · {fmt_wan(total)}</div>
</header>

<div class="books">
{chr(10).join(cards)}
</div>

<section class="cal-wrap" style="--accent:{DEFAULT_ACCENT}" aria-label="码字日历">
  <h2>码字 · 近 17 周</h2>
  <div class="cal" id="cal"></div>
  <div class="cal-note" id="cal-note">数据来自每次构建的每日字数快照（进度.json）</div>
</section>

<div class="foot-note">点开任意一本开始阅读<br>每本书都是独立页面，可单独分享</div>

<noscript><style>.rise{{opacity:1!important;transform:none!important}}</style></noscript>
<script type="application/json" id="word-history">{hist_json}</script>
<script>{THEME_JS}{SHELF_JS}</script>
</body>
</html>
"""


# ---------------------------------------------------------------- 写作辅助
#
# 全库体检（--lint）、素材索引重建（--index）、新书脚手架（--new）、
# 写作上下文生成（--context）。都不写 site/，产物进仓库或 stdout。

NOTE_NAMES = ("伏笔", "待改", "灵感")
STATUSES = ("连载中", "已完结")
DETAIL_CATS = ("人物", "场景", "对白", "情节", "感官", "摘录", "结构", "金句")


def _book_text(book):
    """一本书的正文纯文本（默认线），供人物覆盖等检查用。"""
    chunks = []
    for ch in path_chapters(book["chapters"], book.get("default_path", DEFAULT_PATH)):
        for kind, content in ch["blocks"]:
            if kind == "section":
                continue
            parts = content if isinstance(content, list) else [content]
            for p in parts:
                chunks.append(_plain(p) if isinstance(p, str) else "")
    return "\n".join(chunks)


_PROFILE_STOP = ("人物", "主角", "男主", "女主", "配角", "反派", "总览", "总表",
                 "弧线", "动机", "目标", "信息", "边界", "外貌", "细项", "锚点",
                 "备忘", "关系", "背景", "钩子", "欲望", "新角色", "轻触", "原著",
                 "设定", "备注", "其他", "说明", "模板", "轻量", "为什么", "称呼",
                 "功能", "句式", "节奏", "清单")


def profile_names(text):
    """从 人物.md 提取角色名（## / ### 标题行）。

    启发式：剥掉括号注记，按间隔符切分（「女主：梦姑 / 李清露」→ 两个名字），
    保留 2–4 个汉字、且不含结构性关键词（外貌细项、人物弧线这类栏目标题）
    的词。会漏报复杂写法，宁可少报不误报。
    """
    names = []
    for head in re.findall(r"^#{2,4}\s+(.+)$", text, re.M):
        head = re.sub(r"[（(].*?[)）]", "", head)
        for token in re.split(r"[·：:/、,，\s]+", head.strip()):
            token = token.strip()
            if not (2 <= len(token) <= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", token)):
                continue
            if any(k in token for k in _PROFILE_STOP):
                continue
            names.append(token)
    return list(dict.fromkeys(names))


def timeline_max_chapter(text):
    """时间线里提到的最大章号（ch012 / 第 12 章 两种写法都认）。"""
    nums = [int(a or b) for a, b in
            re.findall(r"ch\s*(\d{1,3})|第\s*(\d{1,3})\s*章", text)]
    return max(nums) if nums else 0


def stale_profiles(names, body_text):
    """人物档案里有、正文里一次都没出现的名字（档案过期或改名）。"""
    return [n for n in names if n not in body_text]


# 伏笔状态词。「未回收」不含子串「未收」，要单列；「半收」是模板约定的未闭环状态
FORESHADOW_OPEN = ("待埋", "未写", "未收", "未埋", "未回收", "半收")
FORESHADOW_DONE = ("已收", "已回收", "已揭")


def foreshadow_open(state):
    """状态字段是否表示伏笔还没闭环。"""
    return any(k in state for k in FORESHADOW_OPEN)


def parse_foreshadow_rows(text):
    """伏笔账本的表格行 → [(内容, 状态字段)]；跳过表头与分隔行。"""
    rows = []
    for line in text.splitlines():
        s = line.strip()
        if not s.startswith("|"):
            continue
        cells = [c.strip() for c in s.strip("|").split("|")]
        if len(cells) < 2 or set("".join(cells)) <= set("-— :："):
            continue
        if any(h in cells[0] for h in ("编号", "伏笔", "钩子", "内容")):
            continue
        rows.append((cells[0], cells[-1]))
    return rows


def parse_char_goal(text):
    """章头「> 字数目标：4k–6k」→ (下限, 上限)；没写或认不出返回 None。"""
    m = re.search(r"^>\s*字数目标[：:]\s*(.+)$", text, re.M)
    if not m:
        return None
    nums = re.findall(r"(\d+(?:\.\d+)?)\s*([kKwW千万]?)", m.group(1))
    vals = []
    for n, unit in nums:
        v = float(n)
        if unit in ("k", "K", "千"):
            v *= 1000
        elif unit in ("w", "W", "万"):
            v *= 10000
        vals.append(int(v))
    if not vals:
        return None
    return min(vals), max(vals)


def person_name_groups(text):
    """人物.md 的标题 → [[主名, 别名…], …]（同一标题行里按分隔符切分）。

    「女主：梦姑 / 李清露」→ [["梦姑", "李清露"]]。供出场矩阵把同人别名
    归为一行，也把「正文两种称呼并存」的数据直接摊给作者看。
    """
    groups = []
    for head in re.findall(r"^#{2,4}\s+(.+)$", text, re.M):
        head = re.sub(r"[（(].*?[)）]", "", head)
        names = []
        for token in re.split(r"[·：:/、,，\s]+", head.strip()):
            token = token.strip()
            if not (2 <= len(token) <= 4 and re.fullmatch(r"[\u4e00-\u9fff]+", token)):
                continue
            if any(k in token for k in _PROFILE_STOP):
                continue
            names.append(token)
        if names:
            groups.append(names)
    return groups


def _compress_ranges(nums):
    """[1,2,3,7,8] → '1–3, 7–8'（出场矩阵的章列表）。"""
    if not nums:
        return "—"
    spans, start, prev = [], nums[0], nums[0]
    for n in nums[1:]:
        if n == prev + 1:
            prev = n
            continue
        spans.append(f"{start}–{prev}" if prev > start else str(start))
        start = prev = n
    spans.append(f"{start}–{prev}" if prev > start else str(start))
    return ", ".join(spans)


def build_matrix(key):
    """人物 × 章节出场矩阵（打印到 stdout）。

    数据面：每个档案角色在默认线各章的出现次数、别名是否同时在用——
    称呼是否统一由作者判断（梦姑/李清露这类剧情性双称呼是合法的）。
    """
    d = find_book_dir(key)
    if not d:
        print(f"✗ 找不到书：{key}", file=sys.stderr)
        return False
    profile = d / "设定" / "人物.md"
    if not profile.exists():
        print(f"✗ {d.name}：没有 设定/人物.md", file=sys.stderr)
        return False
    buf = io.StringIO()
    with redirect_stdout(buf):
        book = load_book(d)
    chain = path_chapters(book["chapters"], book.get("default_path", DEFAULT_PATH))
    per_chapter = []
    for ch in chain:
        chunks = []
        for kind, content in ch["blocks"]:
            if kind == "section":
                continue
            parts = content if isinstance(content, list) else [content]
            chunks.extend(_plain(p) for p in parts if isinstance(p, str))
        per_chapter.append((ch["num"], "\n".join(chunks)))
    print(f"\n人物出场矩阵 · 《{book['title']}》（默认线 {len(chain)} 章）\n")
    print("| 人物 | 别名/称呼 | 次数 | 首次 | 出场章 |")
    print("| --- | --- | --- | --- | --- |")
    for names in person_name_groups(profile.read_text(encoding="utf-8-sig")):
        counts, present = 0, []
        for num, text in per_chapter:
            n = sum(text.count(name) for name in names)
            if n:
                counts += n
                present.append(num)
        alias_note = "、".join(
            f"{name}×{sum(t.count(name) for _, t in per_chapter)}"
            for name in names if sum(t.count(name) for _, t in per_chapter)
        ) if len(names) > 1 else "—"
        first = f"ch{present[0]:03d}" if present else "—"
        print(f"| {names[0]} | {alias_note} | {counts} | {first} "
              f"| {_compress_ranges(present)} |")
    print("\n（别名两种写法都在正文出现时，是否统一由你定夺——剧情性双称呼是合法的）")
    return True


def lint(books_dir=None, materials_base=None, issues=None):
    """全库体检。返回错误数；>0 时调用方应以退出码 1 结束。

    books_dir / materials_base 可指到临时目录供测试隔离；默认查真实仓库。
    级别：✗ 会实际出问题的（元信息非法、防泄漏风险），⚠ 值得处理的欠账
    （账本过期、档案与正文脱节、骨架缺失）。load_book 自带的解析警告
    （bodyless、重名版本、跳过文件）会照常打印。
    issues：传入列表时，每条问题另记为 (书目录名 / "素材库", "err"|"warn", 消息)，
    供作者看板结构化展示；打印行为不变。
    """
    errors = warnings = 0
    scope = [""]

    def err(msg):
        nonlocal errors
        errors += 1
        print(f"  ✗ {msg}")
        if issues is not None:
            issues.append((scope[0], "err", msg))

    def warn(msg):
        nonlocal warnings
        warnings += 1
        print(f"  ⚠ {msg}")
        if issues is not None:
            issues.append((scope[0], "warn", msg))

    bdir = books_dir or BOOKS_DIR
    dirs = [d for d in sorted(bdir.iterdir())
            if d.is_dir() and d.name not in EXCLUDE] if bdir.is_dir() else []
    if not dirs:
        print(f"未在 {bdir} 下找到作品目录")
        return 1

    for d in dirs:
        scope[0] = d.name
        print(f"\n【{d.name}】")
        toml = d / "book.toml"
        if not toml.exists():
            err("缺少 book.toml")
            continue
        meta = load_meta(d)
        if meta["status"] not in STATUSES:
            err(f"status={meta['status']!r} 不在约定枚举（{'/'.join(STATUSES)}）里，"
                f"书架筛选会踩坑")
        mode = meta["export"]
        if not all(t in ("epub", "txt", "none") for t in mode.split("+")):
            err(f"export={mode!r} 不合法（可用 epub / txt / none 组合）")
        ch_dir = d / "章节"
        if not ch_dir.is_dir():
            warn("没有 章节/ 目录")
            continue
        try:
            buf = io.StringIO()
            with redirect_stdout(buf):
                book = load_book(d)
            inner = buf.getvalue().strip()
            if inner:
                print("  " + inner.replace("\n", "\n  "))
        except Exception as e:
            err(f"构建会失败：{e}")
            continue
        if not book["chapters"]:
            warn("章节/ 下还没有 ch*.md（骨架刚建？）")
        # 章节命名：三位数字约定（ch12.md 能编过但与 README 不一致）
        for p in ch_dir.glob("ch*.md"):
            m = re.fullmatch(r"(?i)ch(\d+)", p.stem)
            if m and len(m.group(1)) != 3:
                warn(f"章节/{p.name} 未用三位章号（建议 ch{int(m.group(1)):03d}.md）")
        # 笔记骨架
        if not (d / "笔记").is_dir():
            warn("笔记/ 目录缺失（伏笔、待改无处登记）")
        else:
            for n in NOTE_NAMES:
                if not (d / "笔记" / f"{n}.md").exists():
                    warn(f"笔记/{n}.md 缺失")
        # 伏笔账本新鲜度
        fs = d / "笔记" / "伏笔.md"
        if fs.exists():
            rows = parse_foreshadow_rows(fs.read_text(encoding="utf-8-sig"))
            open_kw = FORESHADOW_OPEN
            pending = [c for c, s in rows if foreshadow_open(s)]
            closed = [c for c, s in rows if any(k in s for k in FORESHADOW_DONE)]
            tail = f"（共 {len(rows)} 条 · 已收 {len(closed)}）"
            if rows and pending and book.get("chapter_count"):
                if meta["status"] == "已完结":
                    warn(f"完本书仍有 {len(pending)} 条伏笔挂着{open_kw[0]}/{open_kw[1]}"
                         f"状态 {tail}：{('、'.join(p[:18] for p in pending[:3]))}…")
                else:
                    warn(f"{len(pending)} 条伏笔未回收 {tail}")
        # 人物档案 vs 正文
        pc = d / "设定" / "人物.md"
        if pc.exists():
            body = _book_text(book)
            ghosts = stale_profiles(profile_names(pc.read_text(encoding="utf-8-sig")), body)
            if ghosts:
                warn(f"档案中这些名字正文从未出现（别名/改名/档案过期）："
                     f"{'、'.join(ghosts[:6])}")
        # 时间线新鲜度
        tl = d / "设定" / "时间线.md"
        if tl.exists() and book.get("chapter_count"):
            nums = [c["num"] for c in path_chapters(
                book["chapters"], book.get("default_path", DEFAULT_PATH))]
            if nums:
                lag = max(nums) - timeline_max_chapter(tl.read_text(encoding="utf-8-sig"))
                if lag > 0:
                    warn(f"时间线落后正文 {lag} 章")
        # 大纲-正文对齐：完本书的大纲该覆盖到最后一章
        ol = d / "设定" / "大纲.md"
        if ol.exists() and book.get("chapter_count"):
            nums = [c["num"] for c in path_chapters(
                book["chapters"], book.get("default_path", DEFAULT_PATH))]
            if nums and meta["status"] == "已完结":
                omax = timeline_max_chapter(ol.read_text(encoding="utf-8-sig"))
                if omax and omax < max(nums):
                    warn(f"大纲最大章 ch{omax} 落后正文 ch{max(nums)}（完本书建议回写大纲）")
        # 字数目标核对：章头写了目标却严重漂移的章
        # 逐个版本文件核对（双线各版本各有字数目标）
        for ch in book["chapters"]:
            src = ch.get("src")
            if not src:
                continue
            goal = parse_char_goal(src.read_text(encoding="utf-8-sig"))
            if not goal:
                continue
            actual = chapter_chars(ch)
            low, high = goal
            if actual < low * 0.5 or actual > high * 1.5:
                br = f"（{ch['branch_label']}）" if ch.get("branch_label") else ""
                warn(f"第 {ch['num']} 章{br}实际约 {actual} 字，偏离目标 {low}–{high} 字")
        if not (d / "设定" / "大纲.md").exists():
            warn("设定/大纲.md 缺失（拆成多个文件可接受，列出即可）")

    # 素材库登记一致性（跨作品，只读检查）
    mats = materials_base or (BASE / "素材")
    scope[0] = "素材库"
    print("\n【素材库】")
    cards = collect_material_cards(mats)
    idx = mats / "索引.md"
    idx_text = idx.read_text(encoding="utf-8-sig") if idx.exists() else ""
    for c in cards:
        if not c["state"]:
            warn(f"{c['file']} 缺「> 状态：」行")
        elif c["name"] not in idx_text:
            warn(f"{c['file']} 状态为「{c['state'][:10]}」但索引.md 未登记")
    if not cards:
        print("  （空）")
    print(f"\n— lint 完成：✗ {errors} · ⚠ {warnings} —")
    return errors


def lint_issues(books_dir=None, materials_base=None):
    """静默跑一遍 lint，返回 [(书目录名, 级别, 消息), ...]（作者看板用）。"""
    issues = []
    with redirect_stdout(io.StringIO()):
        lint(books_dir, materials_base, issues=issues)
    return issues


def record_history(books):
    """把本次构建的字数并入每日快照（进度.json，按天取最后一次构建值）。

    days[日期] 是全库总数（旧格式，保留兼容）；books[日期] 是按书快照，
    供 daily_net 排除新导入/删除的书。

    日期用本地时区（写作者看的是本地日历）；文件损坏时先备份再重建，
    原子写入避免 --serve 重建与手动构建并发时留下半截 JSON。
    """
    today = time.strftime("%Y-%m-%d")
    data = {"days": {}}
    if HISTORY_FILE.exists():
        try:
            loaded = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            if not isinstance(loaded, dict) or not isinstance(
                    loaded.get("days", {}), dict):
                raise ValueError("结构不对")
            data = loaded
        except (ValueError, OSError) as e:
            backup = HISTORY_FILE.with_suffix(".json.bak")
            print(f"  ⚠ 进度.json 无法读取（{e}），已备份到 {backup.name} 并重建")
            try:
                shutil.copy2(HISTORY_FILE, backup)
            except OSError:
                pass
            data = {"days": {}}
    if not isinstance(data.get("books", {}), dict):
        data["books"] = {}
    days = data.setdefault("days", {})
    per_book = data.setdefault("books", {})
    total = sum(b["chars"] for b in books)
    snap = {b.get("title") or f"#{i}": b["chars"] for i, b in enumerate(books)}
    # 内容没变就不写盘：进度.json 进版本库，不能每次构建都留一笔 diff
    if days.get(today) == total and per_book.get(today) == snap and HISTORY_FILE.exists():
        return data
    days[today] = total
    per_book[today] = snap
    data["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    tmp = HISTORY_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1),
                   encoding="utf-8")
    tmp.replace(HISTORY_FILE)
    return data


def daily_net(data):
    """每日净增字数 {日期: 字数}。

    有按书快照的日子：只累加「本日与上一快照日都在」的书的差值——新导入
    一本写好的书、或删掉一本，都不算当天码字。只有旧格式总数的日子退回
    总数差。第一个快照日没有参照，记 0。
    """
    days = data.get("days") or {}
    per_book = data.get("books") or {}
    net, prev_day = {}, None
    for day in sorted(set(days) | set(per_book)):
        if prev_day is None:
            net[day] = 0
        else:
            cur, old = per_book.get(day), per_book.get(prev_day)
            if isinstance(cur, dict) and isinstance(old, dict):
                net[day] = sum(v - old[k] for k, v in cur.items()
                               if k in old and isinstance(v, int)
                               and isinstance(old[k], int))
            elif day in days and prev_day in days:
                net[day] = days[day] - days[prev_day]
            else:
                net[day] = 0
        prev_day = day
    return net


def history_payload(data):
    """书架码字日历的注入数据：总数快照 + 已算好的每日净增。"""
    return {"days": data.get("days") or {}, "net": daily_net(data)}


def collect_material_cards(mats_base=None):
    """扫描素材库全部卡片（调研/题材/细节八类），供 --index 与 --lint 共用。"""
    mats = mats_base or (BASE / "素材")
    cards = []
    for cat in ("调研", "题材", *DETAIL_CATS):
        folder = mats / cat
        if not folder.is_dir():
            continue
        for p in sorted(folder.glob("*.md")):
            if p.name.startswith("_"):
                continue
            text = p.read_text(encoding="utf-8-sig")

            def grab(kw, t=text):
                m = re.search(rf"^>\s*{kw}：(.*)$", t, re.M)
                return m.group(1).strip() if m else ""

            m = re.match(r"(\d{8})", p.name)
            cards.append({
                "cat": cat,
                "name": p.name,
                "file": f"{cat}/{p.name}",
                "date": (f"{m.group(1)[:4]}-{m.group(1)[4:6]}-{m.group(1)[6:]}"
                         if m else ""),
                "title": (re.search(r"^#\s+(.+)$", text, re.M).group(1).strip()
                          if re.search(r"^#\s+(.+)$", text, re.M) else p.stem),
                "state": grab("状态"),
                "heat": grab("热度"),
                "prio": grab("优先级"),
            })
    return cards


_INDEX_HEAD = """# 素材索引 · 调研台

写新文章前先扫这一页。
**调研简报**看风向，**题材卡**决定写不写，细节卡（人物/场景…）开写时再拆。

> 本文件由 `python build.py --index` 从各卡片的 `> 状态：` 行自动重建，
> 请改卡片本身的状态行，不要手改表格。

## 状态约定

| 状态 | 含义 |
| --- | --- |
| 待看 | 刚调研/刚收录，等你拍板 |
| 可用 | 你点头了，随时可开书 |
| 暂缓 | 有意思，但现在不碰 |
| 已用 | 写进某本书（写书名） |
| 弃用 | 不要了 |

"""

_INDEX_TAIL = """---

## 开写前检查清单

- [ ] 读过对应**调研简报**，知道市场在买什么情绪
- [ ] 题材卡状态为**可用**
- [ ] 主角欲望清楚（想要什么、怕失去什么）
- [ ] 关系张力站得住（为何现在不能轻易在一起）
- [ ] 至少有一个可拍的名场面
- [ ] 尺度与年龄边界想过（未成年性描写：不做）
- [ ] 微创新写明：不写什么、改成什么

---

## 快捷指令（复制给 AI）

```text
帮我调研：最近「破镜重圆+职场」还有哪些能写的切口，输出调研简报+3张题材卡
```

```text
刷新调研：最近那份风向，补充短剧已拍烂的桥段，避免撞车
```

```text
把「<题材卡名>」扩成 8 章大纲，准备新建 books/
```
"""


def _state_bucket(state):
    for k in ("待看", "可用", "已用", "弃用", "暂缓"):
        if state.startswith(k):
            return k
    return "未登记"


def _cell(text):
    """单元格里的竖线会切断 markdown 表格，统一换掉。"""
    return (text or "").replace("|", "/").strip()


def render_material_index(cards):
    """从卡片状态行重建 素材/索引.md 的三张表。"""
    lines = [_INDEX_HEAD]
    briefs = [c for c in cards if c["cat"] == "调研"]
    lines.append("## 调研简报\n")
    if briefs:
        lines.append("| 日期 | 主题 | 优先级 | 状态 | 文件 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for c in briefs:
            lines.append(f"| {c['date']} | {_cell(c['title'])} | {_cell(c['prio']) or '—'} | "
                         f"{_cell(c['state']) or '未登记'} | [{c['file']}]({c['file']}) |")
    else:
        lines.append("（空）")
    lines.append("\n## 题材库（爆火 / 可写方向）\n")
    topics = [c for c in cards if c["cat"] == "题材"]
    if topics:
        lines.append("| 日期 | 热度 | 标题 | 状态 | 文件 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for c in topics:
            lines.append(f"| {c['date']} | {_cell(c['heat']) or '—'} | {_cell(c['title'])} | "
                         f"{_cell(c['state']) or '未登记'} | [{c['file']}]({c['file']}) |")
    else:
        lines.append("（空）")
    lines.append("\n## 细节素材（人物 / 情节 / 场景…）\n")
    details = [c for c in cards if c["cat"] in DETAIL_CATS]
    # 五个状态桶全输出：只写"待看/可用/已用"三桶的话，标了"暂缓/弃用"的
    # 卡会从索引里消失，--lint 又会报"索引未登记"，永远清不干净。
    for bucket in ("待看", "可用", "已用", "暂缓", "弃用"):
        rows = [c for c in details if _state_bucket(c["state"]) == bucket]
        lines.append(f"### {bucket}\n")
        if rows:
            lines.append("| 日期 | 分类 | 标题 | 状态 | 卡片 |")
            lines.append("| --- | --- | --- | --- | --- |")
            for c in rows:
                lines.append(f"| {c['date']} | {c['cat']} | {_cell(c['title'])} | "
                             f"{_cell(c['state'])} | [{c['file']}]({c['file']}) |")
        else:
            lines.append("（空）")
        lines.append("")
    lines.append(_INDEX_TAIL)
    return "\n".join(lines) + ("\n" if not lines[-1].endswith("\n") else "")


def rebuild_material_index():
    """重建 素材/索引.md；内容无变化时不写盘。返回是否写入。"""
    cards = collect_material_cards()
    content = render_material_index(cards)
    idx = BASE / "素材" / "索引.md"
    old = idx.read_text(encoding="utf-8-sig") if idx.exists() else ""
    if old == content:
        print(f"索引已是最新的（{len(cards)} 张卡），无需重建")
        return False
    idx.write_text(content, encoding="utf-8")
    print(f"✓ 已重建 素材/索引.md（{len(cards)} 张卡）")
    return True


_TEMPLATES = ("大纲", "人物", "时间线")


def new_book(name, adult=False):
    """从 templates/ 复制出新书骨架。章节留空目录——
    占位章一旦复制会被当成真章节发布，所以只建目录不建文件。
    """
    cleaned = safe_filename(name)
    if cleaned != name:
        print(f"  ⚠ 书名含特殊字符，已清洗：{name!r} → {cleaned!r}")
        name = cleaned
    d = BOOKS_DIR / name
    if d.exists():
        print(f"✗ books/{name} 已存在", file=sys.stderr)
        return False
    (d / "设定").mkdir(parents=True)
    (d / "笔记").mkdir()
    (d / "章节").mkdir()
    (d / "章节" / ".gitkeep").write_text("", encoding="utf-8")
    toml = (BASE / "templates" / "book.toml").read_text(encoding="utf-8")
    toml = toml.replace('title = "书名"', f'title = "{name}"')
    if adult:
        toml = toml.replace("adult = false", "adult = true")
    (d / "book.toml").write_text(toml, encoding="utf-8")
    created = [f"books/{name}/book.toml"]
    for sub, names in (("设定", _TEMPLATES), ("笔记", NOTE_NAMES)):
        for n in names:
            src = BASE / "templates" / f"{n}模板.md"
            dst = d / sub / f"{n}.md"
            if src.exists():
                dst.write_text(src.read_text(encoding="utf-8-sig"), encoding="utf-8")
                created.append(f"{dst.relative_to(BASE)}")
    print("✓ 新书骨架已创建：")
    for c in created:
        print(f"  {c}")
    print("\n下一步：先填 设定/大纲.md 与 人物.md；写第一章时新建 章节/ch001.md"
          "（记得 ## 正文 分隔）。建好章节跑 python build.py 即可上书架。")
    return True


def find_book_dir(key):
    """按书名或目录名找书目录；找不到返回 None。"""
    for d in iter_book_dirs():
        if key in (d.name,):
            return d
        meta = load_meta(d)
        if meta["title"] == key:
            return d
    return None


def chapter_note_sections(path):
    """提取章节源文件里的 ## 本章目标 / ## 章后笔记 原文（上下文生成用）。"""
    text = path.read_text(encoding="utf-8-sig")
    out = {}
    for name in ("本章目标", "章后笔记"):
        m = re.search(rf"^##\s*{name}\s*$(.*?)(?=^##\s|\Z)", text, re.S | re.M)
        if m and m.group(1).strip():
            out[name] = m.group(1).strip()
    return out


def build_context(key, target_num=None):
    """生成某本书下一章的写作上下文：设定 + 账本 + 章节速览 + 最近一章全文。

    这是「story bible + 滚动记忆」的静态版：把散在各文件里、动笔第 N 章时
    需要的东西聚合到一个文件，直接投喂给 AI 或自己通读。
    """
    d = find_book_dir(key)
    if not d:
        print(f"✗ 找不到书：{key}", file=sys.stderr)
        return False
    meta = load_meta(d)
    entries = list(iter_chapter_files(d))
    if not entries:
        print(f"✗ {d.name}：还没有章节", file=sys.stderr)
        return False
    buf = io.StringIO()
    with redirect_stdout(buf):
        book = load_book(d)
    chain = path_chapters(book["chapters"], book.get("default_path", DEFAULT_PATH))
    if not chain:
        print(f"✗ {d.name}：默认线上没有章节", file=sys.stderr)
        return False
    nums = [c["num"] for c in chain]
    if target_num is None:
        target_num = max(nums) + 1

    def src_of(ch):
        # 默认线上的章，取的就是该线的版本文件
        return ch.get("src")

    def read(rel):
        p = d / rel
        return p.read_text(encoding="utf-8-sig") if p.exists() else ""

    parts = [
        f"# 写作上下文 · 《{meta['title']}》 · 第 {target_num} 章",
        "",
        f"> 机器生成（{time.strftime('%Y-%m-%d %H:%M')}），"
        f"由 python build.py --context 重建；本文件不会进网页。",
        f"> 书籍状态：{meta['status']} · 全 {len(chain)} 章（默认线）· "
        f"{fmt_wan(book['chars'])}",
    ]
    if meta["tagline"]:
        parts += ["", f"**一句话简介**：{meta['tagline']}"]
    if meta["epigraph"]:
        parts += ["", f"**题引**：{meta['epigraph']}"]
    for rel in ("设定/大纲.md", "设定/人物.md", "设定/世界观.md",
                "设定/时间线.md", "笔记/伏笔.md"):
        text = read(rel).strip()
        if text:
            label = rel.split("/")[-1][:-3]
            parts += ["", f"## {label}", "", text]
    # 章节速览：每章标题 + 本章目标首行（作者的滚动记忆）
    parts += ["", "## 章节速览", ""]
    for ch in chain:
        src = src_of(ch)
        goal = ""
        if src:
            notes = chapter_note_sections(src)
            if "本章目标" in notes:
                goal = notes["本章目标"].splitlines()[0].strip()
        parts.append(f"- 第 {ch['num']} 章 {ch['title']}"
                     + (f"｜目标：{goal}" if goal else ""))
    # 时间线草稿：各章章头的「> 时间点：」行聚合——写了时间点的书白捡一份草稿
    tl_rows = []
    for ch in chain:
        src = src_of(ch)
        if not src:
            continue
        m = re.search(r"^>\s*时间点[：:]\s*(.+)$",
                      src.read_text(encoding="utf-8-sig"), re.M)
        if m:
            tl_rows.append(f"- 第 {ch['num']} 章｜{m.group(1).strip()}")
    if tl_rows:
        parts += ["", "## 时间线草稿（按章头时间点聚合）", ""] + tl_rows
    # 最近一章全文
    last = chain[-1]
    last_src = src_of(last)
    if last_src:
        parts += ["", f"## 上一章全文（第 {last['num']} 章 {last['title']}）", ""]
        notes = chapter_note_sections(last_src)
        if "章后笔记" in notes:
            parts += [f"> 章后笔记：{notes['章后笔记']}", ""]
        parts.append(extract_body(last_src.read_text(encoding="utf-8-sig")).strip())
    out_dir = BASE / "素材" / "上下文" / safe_filename(meta["title"])
    out_dir.mkdir(parents=True, exist_ok=True)
    content = "\n".join(parts) + "\n"
    out = out_dir / f"ch{max(1, target_num):03d}-上下文.md"
    out.write_text(content, encoding="utf-8")
    print(f"✓ 上下文已生成：素材/上下文/{safe_filename(meta['title'])}/ch{max(1, target_num):03d}-上下文.md"
          f"（约 {len(content)} 字符）")
    return True


# ---------------------------------------------------------------- 作者看板
#
# 只在本地 --serve 下由 /_dash 现场渲染，不写进 site/，也就不会被部署或分享。
# 把散在 CLI 输出里的账本、待改、字数目标、lint 结果聚到一页。

DASH_DAYS = 30


def _book_series(history, title, days=DASH_DAYS):
    """某本书近 N 个快照日的字数 [(日期, 字数)]，按日期升序。"""
    per_book = (history or {}).get("books") or {}
    pts = [(day, snap[title]) for day, snap in sorted(per_book.items())
           if isinstance(snap, dict) and isinstance(snap.get(title), int)]
    return pts[-days:]


def _sparkline_svg(points, accent, w=220, h=44):
    """字数折线（内联 SVG）；少于两个点时返回说明文字。"""
    if len(points) < 2:
        return '<span class="dim">快照不足两天，明天起出现走势</span>'
    vals = [v for _, v in points]
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    step = w / (len(vals) - 1)
    xy = [(i * step, h - 4 - (v - lo) / span * (h - 8)) for i, v in enumerate(vals)]
    path = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f} {y:.1f}" for i, (x, y) in enumerate(xy))
    lx, ly = xy[-1]
    label = html.escape(f"{points[0][0]} → {points[-1][0]}：{lo} → {vals[-1]} 字", quote=True)
    return (f'<svg class="spark" viewBox="0 0 {w} {h}" width="{w}" height="{h}" '
            f'role="img" aria-label="{label}"><title>{label}</title>'
            f'<path d="{path}" fill="none" stroke="{accent}" stroke-width="1.6" '
            f'stroke-linejoin="round" stroke-linecap="round"/>'
            f'<circle cx="{lx:.1f}" cy="{ly:.1f}" r="2.6" fill="{accent}"/></svg>')


def book_dashboard_data(book):
    """一本书在看板上要展示的作者侧数据（只读，不改任何文件）。"""
    d = book["dir"]
    fs = d / "笔记" / "伏笔.md"
    rows = parse_foreshadow_rows(fs.read_text(encoding="utf-8-sig")) if fs.exists() else []
    todo = d / "笔记" / "待改.md"
    todo_text = todo.read_text(encoding="utf-8-sig").strip() if todo.exists() else ""
    # 用了 - [ ] / - [x] 勾选清单的，只摊开还没勾的；没用清单的原文照录
    open_todos = re.findall(r"^\s*[-*]\s*\[\s\]\s*(.+)$", todo_text, re.M)
    done_todos = re.findall(r"^\s*[-*]\s*\[[xX]\]", todo_text, re.M)
    drift = []
    for ch in book["chapters"]:
        src = ch.get("src")
        if not src:
            continue
        goal = parse_char_goal(src.read_text(encoding="utf-8-sig"))
        if not goal:
            continue
        actual = chapter_chars(ch)
        low, high = goal
        if actual < low * 0.5 or actual > high * 1.5:
            drift.append((ch, actual, low, high))
    return {
        "foreshadow_total": len(rows),
        "foreshadow_open": [(c, s) for c, s in rows if foreshadow_open(s)],
        "todo": todo_text,
        "todo_open": open_todos,
        "todo_done": len(done_todos),
        "drift": drift,
    }


DASH_CSS = """
.dash{max-width:980px;margin:0 auto;padding:70px 22px 80px;font-family:var(--sans)}
.dash h1{margin:0 0 6px;font-weight:400;font-size:22px;letter-spacing:.24em;
  font-family:var(--serif)}
.dash .lead{margin:0 0 30px;font-size:12px;letter-spacing:.1em;color:var(--ink-faint)}
.dash .lead b{font-weight:400;color:var(--ink-soft)}
.bk{margin:0 0 22px;padding:20px 22px;background:var(--bg-elev);border:1px solid var(--rule);
  border-left:3px solid var(--bk);border-radius:4px;box-shadow:var(--shadow)}
.bk-h{display:flex;flex-wrap:wrap;align-items:baseline;gap:6px 14px;margin-bottom:14px}
.bk-h h2{margin:0;font-weight:400;font-size:18px;letter-spacing:.1em;font-family:var(--serif)}
.bk-h h2 a{text-decoration:none}
.bk-h h2 a:hover{color:var(--bk)}
.bk-h .m{font-size:11.5px;letter-spacing:.08em;color:var(--ink-faint)}
.bk-g{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:16px 26px}
.bk-g section h3{margin:0 0 8px;font-weight:400;font-size:11px;letter-spacing:.22em;
  color:var(--ink-faint)}
.bk-g ul{margin:0;padding:0;list-style:none;font-size:13px;line-height:1.75;color:var(--ink-soft)}
.bk-g li{padding:3px 0;border-bottom:1px dashed var(--rule)}
.bk-g li:last-child{border-bottom:0}
.bk-g li s{text-decoration:none;color:var(--ink-faint);font-size:11.5px;margin-left:.5em}
.lv-err{color:#b3452c}
html[data-tone=dark] .lv-err{color:#e0856c}
.lv-err::before{content:"✗ "}
.lv-warn::before{content:"⚠ ";color:var(--ink-faint)}
.todo{margin:0;max-height:13em;overflow:auto;white-space:pre-wrap;font-size:12.5px;
  line-height:1.75;color:var(--ink-soft);font-family:var(--sans)}
.dim{font-size:12px;color:var(--ink-faint)}
.ok{font-size:12.5px;color:var(--ink-faint)}
.spark{display:block;max-width:100%;height:auto}
.foot-mats{margin-top:30px;font-size:12.5px;color:var(--ink-soft)}
"""


def render_dashboard(books, issues, history):
    """作者看板 HTML：每本书一块——状态/字数走势/未回收伏笔/待改/字数偏差/lint。

    纯函数：books 用 load_book 的结果（需已带 file），issues 用 lint_issues()，
    history 用 进度.json 的内容。
    """
    by_scope = {}
    for scope, level, msg in issues:
        by_scope.setdefault(scope, []).append((level, msg))
    n_err = sum(1 for _, lv, _ in issues if lv == "err")
    n_warn = len(issues) - n_err
    blocks = []
    for b in books:
        data = book_dashboard_data(b)
        accent = b["accent"]
        pts = _book_series(history, b["title"])
        delta = ""
        if len(pts) >= 2:
            dv = pts[-1][1] - pts[0][1]
            delta = f" · 近 {len(pts)} 个快照日 {'+' if dv >= 0 else ''}{dv} 字"

        def li(items, empty):
            return ("<ul>" + "".join(items) + "</ul>") if items else f'<p class="ok">{empty}</p>'

        fs_items = [f"<li>{html.escape(c)}<s>{html.escape(s)}</s></li>"
                    for c, s in data["foreshadow_open"]]
        fs_head = f"未回收伏笔 · {len(data['foreshadow_open'])} / {data['foreshadow_total']}"
        drift_items = [
            f"<li>{html.escape(chapter_label(ch))}"
            f"{'（' + html.escape(ch['branch_label']) + '）' if ch.get('branch_label') else ''}"
            f"<s>{actual} 字 · 目标 {low}–{high}</s></li>"
            for ch, actual, low, high in data["drift"]]
        lint_items = [f'<li class="lv-{lv}">{html.escape(m)}</li>'
                      for lv, m in by_scope.get(b["dir"].name, [])]
        todo_head = "待改"
        if data["todo_open"] or data["todo_done"]:
            todo_head = f"待改 · 未完成 {len(data['todo_open'])} · 已勾 {data['todo_done']}"
            todo_html = li([f"<li>{html.escape(t)}</li>" for t in data["todo_open"]],
                           "清单全部勾完了")
        elif data["todo"]:
            todo_html = f'<pre class="todo">{html.escape(data["todo"])}</pre>'
        else:
            todo_html = '<p class="ok">笔记/待改.md 为空或缺失</p>'
        blocks.append(f"""<article class="bk" style="--bk:{accent}">
  <div class="bk-h"><h2><a href="{html.escape(b['file'], quote=True)}">{html.escape(b['title'])}</a></h2>
    <span class="m">{html.escape(b['status'])} · {b.get('chapter_count') or len(b['chapters'])} 章 · {fmt_wan(b['chars'])}{delta}</span></div>
  <div class="bk-g">
    <section><h3>字数走势</h3>{_sparkline_svg(pts, lighten(accent, 0.1))}</section>
    <section><h3>{fs_head}</h3>{li(fs_items, "没有挂着的伏笔" if data["foreshadow_total"] else "伏笔账本为空或缺失")}</section>
    <section><h3>{todo_head}</h3>{todo_html}</section>
    <section><h3>字数目标偏差</h3>{li(drift_items, "没有偏离目标的章")}</section>
    <section><h3>体检</h3>{li(lint_items, "无问题")}</section>
  </div>
</article>""")
    mats = [f'<li class="lv-{lv}">{html.escape(m)}</li>' for lv, m in by_scope.get("素材库", [])]
    mats_html = (f'<div class="foot-mats"><h3 class="dim">素材库</h3><ul>{"".join(mats)}</ul></div>'
                 if mats else "")
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="robots" content="noindex">
<title>作者看板</title>
<script>{SHELF_PREPAINT_JS}</script>
<style>{BASE_CSS}{SHELF_CSS}{DASH_CSS}</style>
</head>
<body>
<nav id="shelf-tools">
  <a class="tbtn" href="index.html" style="margin-right:auto;text-decoration:none">← 书架</a>
  <button class="tbtn" id="theme" aria-label="切换日间夜间">夜间</button>
</nav>
<main class="dash">
  <h1>作者看板</h1>
  <p class="lead">仅本地预览可见 · 不写入 site/ · <b>{len(books)} 部作品</b> · 体检 ✗ {n_err} · ⚠ {n_warn} · 生成于 {time.strftime('%H:%M:%S')}</p>
{chr(10).join(blocks)}
{mats_html}
</main>
<script>{THEME_JS}document.getElementById('theme').onclick=toggleTheme;</script>
</body>
</html>
"""


def dashboard_html():
    """现场收集全部数据并渲染看板（--serve 的 /_dash 调用）。"""
    refresh_assets()
    books = []
    with redirect_stdout(io.StringIO()):
        for d in iter_book_dirs():
            try:
                b = load_book(d)
            except Exception:
                continue  # 构建失败的书由 lint 列出原因
            b["file"] = f"{safe_filename(b['title'])}.html"
            books.append(b)
    books.sort(key=lambda b: (b["order"], b["title"]))
    history = {}
    if HISTORY_FILE.exists():
        try:
            history = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (ValueError, OSError):
            history = {}
    return render_dashboard(books, lint_issues(), history)


# ---------------------------------------------------------------- 主流程

def iter_book_dirs():
    """正式作品目录（排除模板、且有 章节/）。"""
    if not BOOKS_DIR.is_dir():
        return []
    return sorted(
        d for d in BOOKS_DIR.iterdir()
        if d.is_dir() and d.name not in EXCLUDE and (d / "章节").is_dir()
    )


def iter_watch_files():
    """影响网页产物的源文件：生成器、站点配置、资源、书籍元信息与正文。"""
    yield Path(__file__).resolve()
    site_config = BASE / "site.toml"
    if site_config.is_file():
        yield site_config
    assets = BASE / "templates" / "assets"
    if assets.is_dir():
        yield from sorted(assets.iterdir())
    for d in iter_book_dirs():
        toml = d / "book.toml"
        if toml.exists():
            yield toml
            cover = parse_toml(toml).get("cover")
            if cover:
                cp = d / cover
                if cp.exists():
                    yield cp
        ch_dir = d / "章节"
        if ch_dir.is_dir():
            yield from sorted(ch_dir.rglob("*.md"))


def source_fingerprint():
    """源文件签名：路径 + mtime + size。用于判断是否需要重建。"""
    items = []
    for p in iter_watch_files():
        try:
            st = p.stat()
            rel = str(p.relative_to(BASE)) if p.is_relative_to(BASE) else str(p)
            items.append((rel, st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    return tuple(items)


def build(quiet=False, public=False):
    """编译全部作品到 site/。成功返回 True。

    public=True 是公开部署（CI → GitHub Pages）用的：跳过 adult 书，
    不读写进度.json，书架不带码字日历。本地构建与单文件分享仍是全量。
    """
    refresh_assets()
    dirs = iter_book_dirs()
    books = []
    failed = False
    skipped_adult = []
    for d in dirs:
        try:
            b = load_book(d)
        except Exception as e:
            print(f"  ✗ {d.name} 构建失败：{e}", file=sys.stderr)
            failed = True
            continue
        if not b["chapters"]:
            if not quiet:
                print(f"  跳过 {d.name}：章节/ 下没有 ch*.md")
            continue
        if public and b["adult"]:
            skipped_adult.append(b["title"])
            continue
        b["file"] = f"{safe_filename(b['title'])}.html"
        books.append(b)
    # 先解析完所有作品再写任何产物；坏章节不能让旧书页被清理，也不能让
    # CI 把缺书的站点当作成功构建部署。
    if failed:
        return False
    if not books and not skipped_adult:
        print(f"未在 {BOOKS_DIR} 下找到任何作品", file=sys.stderr)
        return False
    if skipped_adult and not quiet:
        print(f"  公开构建：跳过 {len(skipped_adult)} 本成人向（{'、'.join(skipped_adult)}）")

    books.sort(key=lambda b: (b["order"], b["title"]))

    # 每日字数快照（进度.json）：书架页的码字日历数据源；公开构建不碰私人进度
    if public:
        hist_json = "{}"
    else:
        hist_data = record_history(books)
        hist_json = json.dumps(history_payload(hist_data), ensure_ascii=False,
                               sort_keys=True)
    # 部署域名（可选）：工作区根目录 site.toml 里写 url = "https://…"
    # 有了它才出 RSS / sitemap / robots.txt；配 giscus 三件套再加评论
    site_cfg = parse_toml(BASE / "site.toml")
    site_url = str(site_cfg.get("url") or "").strip()

    SITE_DIR.mkdir(exist_ok=True)
    has_shelf = len(books) > 1

    # 同名书会互相覆盖产物，先报出来
    by_file = {}
    for b in books:
        if b["file"] in by_file:
            print(f"  ⚠ 两本书都叫《{b['title']}》：产物 {b['file']} 会互相覆盖，"
                  f"请改掉 book.toml 里的 title")
        by_file[b["file"]] = b

    # 上次构建写入的文件清单：只清理清单里的陈旧产物，
    # 手工放进 site/ 的 CNAME、404.html 等不受影响。
    manifest = SITE_DIR / ".build-manifest"
    prev_generated = set()
    if manifest.exists():
        # 一行一个路径；不能用 split()——书名可含空格（「A B.html」）
        prev_generated = {line for line in
                          manifest.read_text(encoding="utf-8").splitlines() if line}

    keep = {"index.html", ".build-id", ".build-manifest"}
    for b in books:
        stem = Path(b["file"]).stem
        mode = b.get("export") or "epub+txt"
        b["exports"] = []
        if "none" not in mode:
            if "epub" in mode:
                b["exports"].append(f"{stem}.epub")
            if "txt" in mode:
                b["exports"].append(f"{stem}.txt")
        (SITE_DIR / b["file"]).write_text(render_reader(b, has_shelf), encoding="utf-8")
        keep.add(b["file"])
        # 整本导出：EPUB 给阅读器，TXT 给网文平台投稿
        for name in b["exports"]:
            if name.endswith(".epub"):
                export_epub(b, SITE_DIR / name)
            else:
                (SITE_DIR / name).write_text(export_txt(b), encoding="utf-8")
            keep.add(name)
        # 有封面图则拷进 site/，保持单目录可整体分享
        if b["cover"]:
            src = b["dir"] / b["cover"]
            cover_name = Path(b["cover"]).name
            keep.add(cover_name)
            if src.exists():
                shutil.copy2(src, SITE_DIR / cover_name)
            else:
                print(f"  ⚠ {b['title']}：封面图不存在 {src}")

    (SITE_DIR / "index.html").write_text(
        render_shelf(books, hist_json=hist_json), encoding="utf-8")

    # 分章站点变体：每章一个 URL（部署用），含 RSS / sitemap（需 site.toml）
    keep.update(build_chapters_site(books, site_url=site_url, site_cfg=site_cfg))

    # 清理陈旧产物：改书名 / 删书后，旧 HTML（含旧封面图）不会自动消失，
    # 残留在 site/ 里还会被一起分享出去。
    for stale in sorted(prev_generated - keep):
        p = SITE_DIR / stale
        if p.is_file():
            p.unlink()
            if not quiet:
                print(f"  🗑 清理陈旧产物 {stale}")
    manifest.write_text("\n".join(sorted(keep)), encoding="utf-8")

    # 供本地预览热刷新探测；分享 site/ 时无害，可忽略
    stamp = str(time.time_ns())
    (SITE_DIR / ".build-id").write_text(stamp, encoding="utf-8")

    if not quiet:
        n_epub = sum(1 for b in books for n in b.get("exports", []) if n.endswith(".epub"))
        n_txt = sum(1 for b in books for n in b.get("exports", []) if n.endswith(".txt"))
        print(f"✓ site/  {len(books)} 部作品 · {fmt_wan(sum(b['chars'] for b in books))}"
              + (f" · EPUB×{n_epub} TXT×{n_txt}" if (n_epub or n_txt) else ""))
        for b in books:
            size = (SITE_DIR / b["file"]).stat().st_size // 1024
            nch = b.get("chapter_count") or len(b["chapters"])
            fork = " · 双线" if b.get("has_fork") else ""
            print(f"  {b['title']:<8} {b['status']:<4} {nch:>2} 章{fork} · "
                  f"{fmt_wan(b['chars']):<9} · {size:>3} KB · {b['accent']}")
        print(f"  index.html  {(SITE_DIR / 'index.html').stat().st_size // 1024} KB")
    return True


def safe_build(label=""):
    """构建并吞掉异常，避免监视循环因一次写半截文件而退出。"""
    prefix = f"[{time.strftime('%H:%M:%S')}] "
    try:
        ok = build()
        if ok and label:
            print(f"{prefix}{label}")
        return ok
    except Exception as e:
        print(f"{prefix}构建失败：{e}", file=sys.stderr)
        return False


def watch_loop(interval=0.5, settle=0.35):
    """轮询源文件变更；稳定一小段时间后再触发重建，避免编辑器连写多次。"""
    last = source_fingerprint()
    print(f"监视 books/（{len(list(iter_watch_files()))} 个文件）· Ctrl+C 结束")
    try:
        while True:
            time.sleep(interval)
            cur = source_fingerprint()
            if cur == last:
                continue
            # 等写入落稳
            time.sleep(settle)
            cur = source_fingerprint()
            if cur == last:
                continue
            last = cur
            print(f"\n[{time.strftime('%H:%M:%S')}] 检测到更新，正在重建…")
            safe_build()
    except KeyboardInterrupt:
        print("\n已停止监视")


# 仅注入到本地预览响应，不会写进 site/*.html 分享文件
_LIVE_RELOAD_JS = r"""
<script>
/* xhw live-reload — 仅本地 --serve 注入 */
(function(){
  var id=null, fail=0;
  function tick(){
    fetch('/.build-id?t='+Date.now(),{cache:'no-store'}).then(function(r){
      if(!r.ok) throw new Error(r.status);
      return r.text();
    }).then(function(t){
      fail=0;
      t=(t||'').trim();
      if(!t) return;
      if(id===null) id=t;
      else if(id!==t) location.reload();
    }).catch(function(){
      fail++;
      if(fail>8) return; // 服务停了就别刷了
    });
  }
  setInterval(tick, 1000);
  // 书架工具条加「看板」入口（只在本地预览出现，分享出去的书架没有）
  var tb=document.getElementById('shelf-tools');
  if(tb && !document.getElementById('dash-link') && location.pathname.indexOf('/_dash')!==0){
    var a=document.createElement('a');
    a.id='dash-link'; a.className='tbtn'; a.href='/_dash'; a.textContent='看板';
    a.style.textDecoration='none';
    tb.insertBefore(a, tb.firstChild);
  }
})();
</script>
"""

# 作者看板的本地路由：请求时现场渲染，不落盘
DASH_ROUTE = "/_dash"


def is_dash_route(raw_path):
    """请求路径是否是作者看板（容忍结尾斜杠与查询串）。"""
    from urllib.parse import unquote, urlsplit
    return unquote(urlsplit(raw_path).path).rstrip("/") == DASH_ROUTE


def resolve_under_site(raw_path):
    """把请求路径解析到 site/ 内的绝对路径；越界（含 Windows 盘符词如
    /C:/Windows/win.ini）返回 None。纯函数，供 serve 与测试共用。
    """
    from urllib.parse import unquote, urlsplit
    path = unquote(urlsplit(raw_path).path)
    rel = path.lstrip("/")
    target = (SITE_DIR / rel) if rel else SITE_DIR
    try:
        target = target.resolve()
        target.relative_to(SITE_DIR.resolve())
    except (ValueError, OSError):
        return None
    return target


def host_allowed(host_header):
    """只放行本机回环 Host，杜绝 DNS rebinding 把预览服务变远程文件读。"""
    host = (host_header or "").lower().strip()
    if host.startswith("["):  # [::1]:8765
        if "]" not in host:
            return False  # 括号不闭合，格式非法
        name = host[1:host.index("]")]
    elif host.count(":") > 1:  # 裸 IPv6（::1）没有端口段
        name = host
    else:
        name = host.rsplit(":", 1)[0] if host else ""
    return name in ("127.0.0.1", "localhost", "::1")


def serve(port=8765, open_browser=True):
    """本地 HTTP 预览：自动监视重建，HTML 响应注入热刷新脚本。"""
    import threading
    import webbrowser
    from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import unquote, urlsplit

    if not safe_build("首次构建完成"):
        raise SystemExit(1)

    class Handler(SimpleHTTPRequestHandler):
        extensions_map = {
            **getattr(SimpleHTTPRequestHandler, "extensions_map", {}),
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".json": "application/json; charset=utf-8",
            ".txt": "text/plain; charset=utf-8",
            ".md": "text/markdown; charset=utf-8",
            ".toml": "text/plain; charset=utf-8",
            ".svg": "image/svg+xml",
        }

        def __init__(self, *args, **kwargs):
            super().__init__(*args, directory=str(SITE_DIR), **kwargs)

        def log_message(self, fmt, *args):
            # 热刷新轮询太吵，压掉 .build-id
            if args and ".build-id" in str(args[0]):
                return
            super().log_message(fmt, *args)

        def end_headers(self):
            # 预览时禁止缓存，保证刷新就是最新稿
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            super().end_headers()

        def do_GET(self):
            if not host_allowed(self.headers.get("Host")):
                return self.send_error(403)
            if is_dash_route(self.path):
                return self._send_dashboard()
            if resolve_under_site(self.path) is None:
                return self.send_error(403)
            path = unquote(urlsplit(self.path).path)
            if path.endswith(".html") or path in ("/", ""):
                return self._send_html_with_reload(path)
            return super().do_GET()

        def do_HEAD(self):
            # 与 GET 同样过 Host/路径校验；非 HTML 走基类（正确的不带 body）
            if not host_allowed(self.headers.get("Host")):
                return self.send_error(403)
            if is_dash_route(self.path):
                return self._send_dashboard(head_only=True)
            if resolve_under_site(self.path) is None:
                return self.send_error(403)
            path = unquote(urlsplit(self.path).path)
            if path.endswith(".html") or path in ("/", ""):
                self._send_html_with_reload(path, head_only=True)
            else:
                super().do_HEAD()

        def _send_dashboard(self, head_only=False):
            # 现场渲染：数据随章节/笔记改动即时更新；热刷新脚本同样注入
            try:
                body = dashboard_html()
            except Exception as e:
                # 状态行只能是 latin-1，中文原因放 explain（进响应体）
                self.send_error(500, "Dashboard failed", f"看板生成失败：{e}")
                return
            body = body.replace("</body>", _LIVE_RELOAD_JS + "\n</body>", 1)
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

        def _send_html_with_reload(self, path, head_only=False):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            # 防目录穿越（do_GET 已校验过一次，这里独立再校验以单独可用）
            target = resolve_under_site("/" + rel)
            if target is None:
                self.send_error(403)
                return
            if not target.is_file():
                self.send_error(404)
                return
            body = target.read_text(encoding="utf-8-sig")
            if "</body>" in body:
                body = body.replace("</body>", _LIVE_RELOAD_JS + "\n</body>", 1)
            else:
                body += _LIVE_RELOAD_JS
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if not head_only:
                self.wfile.write(data)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit(f"无法监听端口 {port}：{e}\n可换：python build.py --serve {port + 1}")

    url = f"http://127.0.0.1:{port}/"
    print(f"预览地址：{url}")
    print(f"作者看板：{url.rstrip('/')}{DASH_ROUTE}（仅本地，不进 site/）")
    print("改 books/ 下的章节或 book.toml 会自动重建，浏览器约 1 秒内刷新")
    print("Ctrl+C 结束\n")

    t = threading.Thread(target=watch_loop, kwargs={"interval": 0.5}, daemon=True)
    t.start()

    if open_browser:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止预览服务")
    finally:
        httpd.server_close()


def main(argv=None):
    import argparse

    parser = argparse.ArgumentParser(
        description="把 books/ 编译成 site/ 阅读页（零依赖）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="示例：\n"
               "  python build.py\n"
               "  python build.py --watch\n"
               "  python build.py --serve\n"
               "  python build.py --serve 9000 --no-open\n",
    )
    parser.add_argument(
        "--watch", action="store_true",
        help="监视 books/ 变更并自动重建（不启动网页服务）",
    )
    parser.add_argument(
        "--serve", nargs="?", const=8765, type=int, metavar="PORT",
        help="本地预览 + 自动重建 + 浏览器热刷新（默认端口 8765）",
    )
    parser.add_argument(
        "--public", action="store_true",
        help="公开部署用：跳过成人向书，不读写 进度.json（CI 用）",
    )
    parser.add_argument(
        "--no-open", action="store_true",
        help="配合 --serve：不自动打开浏览器",
    )
    parser.add_argument(
        "--lint", action="store_true",
        help="全库体检：骨架完整性、命名、账本新鲜度、素材登记一致性",
    )
    parser.add_argument(
        "--index", action="store_true",
        help="从素材卡片的状态行重建 素材/索引.md",
    )
    parser.add_argument(
        "--new", metavar="书名",
        help="按 templates/ 新建 books/<书名>/ 骨架",
    )
    parser.add_argument(
        "--adult", action="store_true",
        help="配合 --new：新书标记为成人向",
    )
    parser.add_argument(
        "--context", nargs="+", metavar=("书名", "章号"),
        help="生成某本书（下一章或指定章）的写作上下文到 素材/上下文/",
    )
    parser.add_argument(
        "--wechat", metavar="书名",
        help="生成公众号富文本 + 知乎 Markdown 投稿稿到 素材/投稿/",
    )
    parser.add_argument(
        "--matrix", metavar="书名",
        help="打印人物 × 章节出场矩阵（含别名出现次数）",
    )
    args = parser.parse_args(argv)

    # 控制台被重定向到 GBK 管道时，emoji 输出会导致 UnicodeEncodeError
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

    if args.new:
        if not new_book(args.new, adult=args.adult):
            raise SystemExit(1)
        return
    if args.lint:
        raise SystemExit(1 if lint() > 0 else 0)
    if args.index:
        rebuild_material_index()
        return
    if args.context:
        target = None
        if len(args.context) > 2:
            parser.error("--context 最多接两个参数：书名 [章号]")
        if len(args.context) == 2:
            try:
                target = int(args.context[1])
            except ValueError:
                parser.error("章号必须是数字")
        if not build_context(args.context[0], target):
            raise SystemExit(1)
        return
    if args.wechat:
        d = find_book_dir(args.wechat)
        if not d:
            print(f"✗ 找不到书：{args.wechat}", file=sys.stderr)
            raise SystemExit(1)
        buf = io.StringIO()
        with redirect_stdout(buf):
            book = load_book(d)
        html_path, md_path = export_platform(book)
        print(f"✓ 公众号稿：{html_path.relative_to(BASE)}（浏览器打开全选复制）")
        print(f"✓ 知乎稿：{md_path.relative_to(BASE)}（知乎专栏「导入文件」）")
        return
    if args.matrix:
        if not build_matrix(args.matrix):
            raise SystemExit(1)
        return

    if args.serve is not None:
        serve(port=args.serve, open_browser=not args.no_open)
        return

    if args.watch:
        if not safe_build("首次构建完成"):
            raise SystemExit(1)
        watch_loop()
        return

    if not build(public=args.public):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
