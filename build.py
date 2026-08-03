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
import html
import re
import shutil
import sys
import time
from pathlib import Path

BASE = Path(__file__).parent
BOOKS_DIR = BASE / "books"
SITE_DIR = BASE / "site"

# 模板骨架，不是作品
EXCLUDE = {"示例作品"}

DEFAULT_ACCENT = "#8c6f4a"
SECTION_MARKS = set("一二三四五六七八九十")

# 作者侧字样：正文里绝不该出现，用于构建时自检
AUTHOR_ONLY = ["本章目标", "章后笔记", "字数目标", "视角人物", "伏笔", "自查"]


# ---------------------------------------------------------------- 元信息

def parse_toml(path):
    """极简 TOML 子集解析：key = "字符串" / 数字 / true|false，# 为注释。

    标准库 tomllib 要 Python 3.11+，这里只需这几种标量，自己解析以保持零依赖。
    """
    data = {}
    if not path.exists():
        return data
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip()
        # 去掉值后面的行尾注释（字符串里的 # 已被引号保护）
        if val.startswith('"'):
            end = val.find('"', 1)
            val = val[1:end] if end > 0 else val.strip('"')
        else:
            val = val.split("#")[0].strip()
            if val in ("true", "false"):
                val = val == "true"
            else:
                try:
                    val = int(val)
                except ValueError:
                    pass
        data[key] = val
    return data


def load_meta(book_dir):
    m = parse_toml(book_dir / "book.toml")
    accent = m.get("accent") or DEFAULT_ACCENT
    if not valid_hex(accent):
        print(f"  ⚠ {book_dir.name}：book.toml 里 accent={accent!r} 不是合法颜色，"
              f"回退为默认色 {DEFAULT_ACCENT}")
        accent = DEFAULT_ACCENT
    return {
        "title": m.get("title") or book_dir.name,
        "subtitle": m.get("subtitle", ""),
        "tagline": m.get("tagline", ""),
        "epigraph": m.get("epigraph", ""),
        "status": m.get("status", "连载中"),
        "adult": bool(m.get("adult", False)),
        "accent": accent,
        "cover": m.get("cover", ""),
        "order": m.get("order", 99),
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
    """行内标记：转义 -> 粗体 -> 斜体。"""
    text = html.escape(text)
    text = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", text)
    text = re.sub(r"(?<!\*)\*([^*\n]+)\*(?!\*)", r"<em>\1</em>", text)
    return text


def extract_body(text):
    """切出读者可见的正文，丢弃创作笔记。

    有 `## 正文` 就只取它到下一个 `##` 之前——这一步同时天然剔除了
    `## 本章目标`、`## 章后笔记`、`## 全文完` 以及标题下方的 `> 元信息块`。
    没有 `## 正文` 的（如远观）则取 `# 章标题` 之后的全部内容。
    """
    m = re.search(r"^##\s*正文\s*$(.*?)(?=^##\s|\Z)", text, re.S | re.M)
    if m:
        return m.group(1)
    return re.sub(r"\A.*?^#\s.*?$", "", text, count=1, flags=re.S | re.M)


def parse_chapter(path):
    """返回 {'num', 'title', 'blocks'}；blocks 为 (类型, 内容) 列表。"""
    text = path.read_text(encoding="utf-8")

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
        num = int(re.search(r"\d+", path.stem).group())

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

    for raw in extract_body(text).splitlines():
        line = raw.rstrip()
        s = line.strip()

        if not s:
            flush_para()
            flush_quote()
            continue

        # `### 一、落地` 形式的分节符（出差）
        h3 = re.match(r"^#{3,}\s*([一二三四五六七八九十]{1,3})\s*[、.·]?\s*(.*)$", s)
        if h3:
            flush_para()
            flush_quote()
            blocks.append(("section", (h3.group(1), h3.group(2).strip())))
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
        if (re.fullmatch(r"[（(].{0,12}[终完][）)]", s)
                or re.fullmatch(r"第\s*\d+\s*章\s*[·:：]\s*[终完]", s)
                or re.fullmatch(r"全[书文]\s*[·:：]?\s*[终完]", s)
                or "未完待续" in s):
            flush_para()
            blocks.append(("endmark", inline(s)))
            continue

        # 正文行；行尾两个空格 = 段内换行
        buf.append(inline(s))
        if not raw.endswith("  "):
            flush_para()

    flush_para()
    flush_quote()
    return {"num": num, "title": title or f"第 {num} 章", "blocks": blocks}


def volume_order(dir_name):
    """卷目录名 → 排序用数字。「卷一」「卷2」「第三卷」都认，认不出的排最后。"""
    m = re.search(r"([0-9]+|[一二三四五六七八九十百零]+)", dir_name)
    return cn_num(m.group(1)) if m else 9999


def iter_chapter_files(book_dir):
    """收集一本书的章节文件，返回 [(排序键, 卷名, 路径), ...]。

    支持两种结构，可混用：
      章节/ch001.md                直接放章节（卷名为 ""）
      章节/卷一/ch001.md           分卷；卷序取目录名里的数字
    卷内章号相同的不同卷不会相撞，因为排序键是 (卷序, 章号)。
    """
    ch_dir = book_dir / "章节"
    items = []
    for p in ch_dir.glob("ch*.md"):
        items.append(((0, chapter_seq(p)), "", p))
    for sub in ch_dir.iterdir():
        if not sub.is_dir():
            continue
        vo = volume_order(sub.name)
        for p in sub.glob("ch*.md"):
            items.append(((vo, chapter_seq(p)), sub.name, p))
    items.sort(key=lambda t: t[0])
    return items


def chapter_seq(path):
    """章节文件名 → 章内序号。ch001 → 1；ch1-2（卷内第2章）→ 2。"""
    nums = re.findall(r"\d+", path.stem)
    if not nums:
        return 0
    # 有两段数字时（如 ch1-2）后一段才是章号，前一段是卷号
    return int(nums[-1]) if len(nums) > 1 else int(nums[0])


def load_book(book_dir):
    """读一本书：元信息 + 全部章节 + 字数统计。"""
    meta = load_meta(book_dir)
    entries = iter_chapter_files(book_dir)
    chapters = []
    for i, (_, vol, path) in enumerate(entries, 1):
        ch = parse_chapter(path)
        ch["volume"] = vol
        # 锚点用全书序号而非章号：分卷时各卷章号会重复（卷一/卷二都有 ch001），
        # 直接用 num 做 id 会撞。num 仍用于显示「第 N 章」。
        ch["anchor"] = f"ch{i}"
        chapters.append(ch)
    meta["chapters"] = chapters
    meta["chars"] = sum(chapter_chars(c) for c in chapters)
    check_author_leak(meta)
    return meta


def check_author_leak(book):
    """构建期自检：extract_body 理应把创作笔记全部剔除，这里兜底检查一遍，
    确保 AUTHOR_ONLY 里的作者侧字样没有随正文泄漏到网页里。
    """
    for ch in book["chapters"]:
        for kind, content in ch["blocks"]:
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


def fmt_wan(n):
    """字数写成「约 N 万字」或「约 N 千字」。"""
    if n >= 10000:
        return f"约 {round(n / 10000, 1)} 万字"
    return f"约 {max(1, round(n / 1000))} 千字"


def safe_filename(title):
    """书名直接拿来当文件名，含 / \\ : * ? " < > | 会在磁盘上炸掉，清洗成 _。"""
    return re.sub(r'[\\/:*?"<>|]', "_", title).strip() or "未命名"


def escape_epigraph(text):
    """转义题引文本，但放行 <br> 换行——书写约定允许用它分行。"""
    escaped = html.escape(text)
    return escaped.replace("&lt;br&gt;", "<br>")


def favicon_data_uri(accent):
    """用书的主色画一个纯色圆点当 favicon，内联成 data URI——
    不依赖外部文件，保持单文件可分享的设计。
    """
    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 32 32">'
        f'<circle cx="16" cy="16" r="14" fill="{accent}"/></svg>'
    )
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


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

BASE_CSS = r"""
:root{
  --bg:#f4f1ea; --bg-elev:#fbf9f4; --ink:#2b2724; --ink-soft:#6f665e;
  --ink-faint:#a89e94; --rule:#ddd5c8;
  --accent:#8c6f4a; --accent-lit:#8c6f4a;
  --shadow:0 1px 2px rgba(43,39,36,.04),0 8px 24px -12px rgba(43,39,36,.14);
  --shadow-lift:0 2px 4px rgba(43,39,36,.05),0 20px 44px -16px rgba(43,39,36,.22);
  --font-scale:1;
  --ease:cubic-bezier(.4,0,.2,1);
  /* 「Noto Serif SC」必须排在「Source Han Serif SC」之前：两者是同一套字形，
     但 Windows 上思源宋体常只装了 Heavy 单字重，命中它会让正文按 400 声明
     却渲染成 Heavy（实测墨量与 900 完全相同）。Noto Serif SC 是可变字体，
     400 才是正常正文字重。Songti SC 仅 macOS/iOS 有，留作首选无副作用。 */
  --serif:"Songti SC","Noto Serif SC","Source Han Serif SC","Noto Serif CJK SC",
          "Source Han Serif CN","STSong","SimSun",Georgia,"Times New Roman",serif;
  --sans:"PingFang SC","Microsoft YaHei","Hiragino Sans GB","Noto Sans CJK SC",
         -apple-system,"Segoe UI",sans-serif;
}
html[data-theme=night]{
  --bg:#16181a; --bg-elev:#1e2124; --ink:#cfc9c0; --ink-soft:#948d84;
  --ink-faint:#645d55; --rule:#2e3236;
  --accent:var(--accent-lit);
  --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px -12px rgba(0,0,0,.6);
  --shadow-lift:0 2px 6px rgba(0,0,0,.36),0 22px 48px -16px rgba(0,0,0,.7);
}
/* 深底浅字会视觉膨胀（halation），中文笔画密更明显。字重已是 400 不宜再降
   （很多中文字体只有单字重，降了也无效），改用极轻的负字距抵消 */
html[data-theme=night] main{letter-spacing:.015em}
*{box-sizing:border-box}
html{scroll-behavior:smooth}
body{
  margin:0; background:var(--bg); color:var(--ink);
  font-family:var(--serif); -webkit-font-smoothing:antialiased;
  /* 中文字体缺字重时别让浏览器合成伪粗体：CJK 笔画密，合成会糊成一团 */
  font-synthesis:none;
  transition:background .4s var(--ease),color .4s var(--ease);
}
::selection{background:color-mix(in srgb,var(--accent) 24%,transparent)}
a{color:inherit}
:focus-visible{outline:2px solid var(--accent);outline-offset:2px;border-radius:2px}

/* 通用入场：加 .rise 的元素初始下沉透明，进入视口时由 JS 挂 .in */
.rise{opacity:0;transform:translateY(18px);
  transition:opacity .7s var(--ease),transform .7s var(--ease)}
.rise.in{opacity:1;transform:none}

.tbtn{border:0;background:none;color:var(--ink-soft);font-family:var(--sans);font-size:13px;
  cursor:pointer;padding:7px 10px;border-radius:6px;line-height:1;
  transition:color .25s var(--ease),background .25s var(--ease)}
.tbtn:hover{color:var(--ink);background:color-mix(in srgb,var(--ink) 7%,transparent)}
.tbtn:active{transform:scale(.94)}

@media (prefers-reduced-motion:reduce){
  *{transition:none!important;animation:none!important;scroll-behavior:auto!important}
  .rise{opacity:1!important;transform:none!important}
}
"""

READER_CSS = r"""
/* 顶部进度条 */
#bar{position:fixed;top:0;left:0;height:2px;width:0;z-index:60;
  background:linear-gradient(90deg,color-mix(in srgb,var(--accent) 55%,transparent),var(--accent));
  transition:width .12s linear}

/* 工具条 */
#tools{position:fixed;top:0;right:0;left:0;height:46px;z-index:50;display:flex;
  align-items:center;gap:2px;padding:0 12px;
  background:color-mix(in srgb,var(--bg) 86%,transparent);
  backdrop-filter:blur(12px) saturate(1.4);border-bottom:1px solid transparent;
  transition:border-color .4s var(--ease),background .4s var(--ease)}
#tools.lifted{border-bottom-color:var(--rule)}
#tools .now{flex:1;min-width:0;font-family:var(--sans);font-size:12.5px;color:var(--ink-soft);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;
  opacity:0;transform:translateY(3px);
  transition:opacity .4s var(--ease),transform .4s var(--ease)}
#tools.lifted .now{opacity:1;transform:none}
#back{font-family:var(--sans);font-size:12.5px;color:var(--ink-soft);text-decoration:none;
  padding:7px 10px;border-radius:6px;white-space:nowrap;
  transition:color .25s var(--ease),background .25s var(--ease)}
#back:hover{color:var(--accent);background:color-mix(in srgb,var(--ink) 7%,transparent)}

/* 侧栏目录 */
#toc{position:fixed;top:0;left:0;bottom:0;width:238px;z-index:55;padding:58px 0 24px;
  background:var(--bg-elev);border-right:1px solid var(--rule);overflow-y:auto;
  transform:translateX(-100%);transition:transform .38s var(--ease)}
#toc.open{transform:none}
#toc h3{margin:0 0 10px;padding:0 22px;font-family:var(--sans);font-size:11px;
  letter-spacing:.22em;color:var(--ink-faint);font-weight:400}
#toc a{display:block;padding:9px 22px;color:var(--ink-soft);text-decoration:none;
  font-family:var(--sans);font-size:13.5px;letter-spacing:.03em;
  border-left:2px solid transparent;
  transition:color .25s var(--ease),border-color .25s var(--ease),
             background .25s var(--ease),padding-left .25s var(--ease)}
#toc a:hover{color:var(--ink);padding-left:26px}
#toc a.on{color:var(--accent);border-left-color:var(--accent);
  background:color-mix(in srgb,var(--accent) 8%,transparent)}
#toc a i{font-style:normal;color:var(--ink-faint);font-size:11.5px;margin-right:8px;
  font-variant-numeric:tabular-nums}
#toc a.on i{color:var(--accent)}
/* 带小标题的分节符生成的二级目录项：单章多分节（如出差）时给出导航 */
#toc a.sub-toc{padding:6px 22px 6px 40px;font-size:12px;color:var(--ink-faint)}
#toc a.sub-toc:hover{padding-left:44px}
/* 分卷书的卷标题：只在 章节/卷X/ 结构下出现 */
.toc-vol{margin:18px 0 6px;padding:0 22px;font-family:var(--sans);font-size:10.5px;
  letter-spacing:.2em;color:var(--ink-faint);opacity:.75}
.toc-vol:first-child{margin-top:0}
/* 抽屉打开时条目阶梯淡入 */
#toc.open a{animation:tocIn .42s var(--ease) both;animation-delay:var(--d)}
@keyframes tocIn{from{opacity:0;transform:translateX(-10px)}to{opacity:1;transform:none}}
#scrim{position:fixed;inset:0;z-index:54;background:rgba(0,0,0,.34);opacity:0;
  pointer-events:none;transition:opacity .38s var(--ease);
  -webkit-backdrop-filter:blur(2px);backdrop-filter:blur(2px)}
#scrim.on{opacity:1;pointer-events:auto}

/* 封面 */
#cover{min-height:100svh;display:flex;flex-direction:column;align-items:center;
  justify-content:center;text-align:center;padding:60px 24px;
  transition:padding-left .38s var(--ease)}
#cover>*{will-change:transform,opacity}
#cover .rule{width:1px;height:56px;
  background:linear-gradient(var(--rule),color-mix(in srgb,var(--accent) 40%,transparent));
  animation:dropIn .9s var(--ease) both}
#cover h1{margin:26px 0 0;font-size:clamp(52px,15vw,86px);font-weight:400;
  letter-spacing:.3em;text-indent:.3em;line-height:1.15;
  animation:titleIn 1.1s var(--ease) .1s both}
#cover .sub{margin-top:16px;font-family:var(--sans);font-size:12px;letter-spacing:.42em;
  text-indent:.42em;color:var(--ink-faint);animation:fadeUp .9s var(--ease) .34s both}
#cover .epi{margin-top:46px;font-size:15px;line-height:2.1;color:var(--ink-soft);
  letter-spacing:.06em;animation:fadeUp .9s var(--ease) .5s both}
#cover .meta{margin-top:52px;font-family:var(--sans);font-size:11.5px;letter-spacing:.16em;
  color:var(--ink-faint);animation:fadeUp .9s var(--ease) .64s both}
#cover .go{margin-top:34px;display:inline-flex;align-items:center;gap:9px;
  font-family:var(--sans);font-size:12.5px;letter-spacing:.2em;color:var(--ink-soft);
  text-decoration:none;padding:12px 28px;border:1px solid var(--rule);border-radius:2px;
  animation:fadeUp .9s var(--ease) .78s both;
  transition:color .3s var(--ease),border-color .3s var(--ease),gap .3s var(--ease)}
#cover .go:hover{color:var(--accent);border-color:var(--accent);gap:14px}
#cover .go span{transition:transform .3s var(--ease)}
#cover .go:hover span{transform:translateX(2px)}
@keyframes titleIn{from{opacity:0;transform:translateY(20px);letter-spacing:.52em}
  to{opacity:1;transform:none}}
@keyframes fadeUp{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes dropIn{from{opacity:0;transform:scaleY(0);transform-origin:top}
  to{opacity:1;transform:none}}

/* 正文 */
main{max-width:34em;margin:0 auto;padding:0 26px 30vh;
  font-size:calc(17.5px * var(--font-scale));line-height:2.05;letter-spacing:.02em;
  /* 避头尾：不让逗号句号右括号落在行首。中文长文必需，浏览器默认的 auto 较宽松 */
  line-break:strict;word-break:normal;overflow-wrap:break-word}
.chapter{padding-top:14vh}
.ch-head{margin-bottom:9vh;text-align:center}
.ch-num{font-family:var(--sans);font-size:11.5px;letter-spacing:.34em;text-indent:.34em;
  color:var(--accent);opacity:.85}
.ch-title{margin:14px 0 0;font-size:1.5em;font-weight:400;letter-spacing:.12em;
  text-indent:.12em}
.ch-line{width:26px;height:1px;background:var(--rule);margin:26px auto 0;
  transform:scaleX(0);transition:transform .8s var(--ease) .2s}
.ch-head.in .ch-line{transform:none}
/* hanging-punctuation 目前基本只有 Safari 生效，text-wrap:pretty 主要 Chromium 生效；
   两者都是渐进增强，不支持的浏览器忽略即可，无副作用 */
.p{margin:0 0 1.15em;text-indent:2em;text-align:justify;hanging-punctuation:allow-end;
  text-wrap:pretty}
strong{font-weight:600;color:var(--ink)}
em{font-style:italic;color:var(--ink-soft)}

/* 分节符 */
.sec-mark{display:flex;align-items:center;gap:18px;margin:3.2em 0 2.4em;color:var(--accent)}
.sec-mark::before,.sec-mark::after{content:"";flex:1;height:1px;background:var(--rule);
  transform:scaleX(0);transition:transform .9s var(--ease)}
.sec-mark::before{transform-origin:right}
.sec-mark::after{transform-origin:left}
.sec-mark.in::before,.sec-mark.in::after{transform:none}
.sec-mark b{font-weight:400;font-size:1em;line-height:1;letter-spacing:.2em;
  text-indent:.2em;opacity:.9;white-space:nowrap}
.sec-mark b em{font-style:normal;color:var(--ink-soft);font-size:.86em;
  letter-spacing:.1em;margin-left:.7em}
/* 章首第一个分节符紧贴标题，省掉横线（隐藏后需自行居中） */
.ch-head + .sec-mark{margin-top:0;justify-content:center}
.ch-head + .sec-mark::before,.ch-head + .sec-mark::after{display:none}

/* 手机消息 */
.msg{margin:1.9em 0;padding:14px 18px;border-radius:3px 12px 12px 12px;
  background:var(--bg-elev);border:1px solid var(--rule);
  border-left:2px solid color-mix(in srgb,var(--accent) 60%,transparent);
  box-shadow:var(--shadow)}
.msg p{margin:0 0 .5em;font-family:var(--sans);font-size:.88em;line-height:1.85;
  color:var(--ink-soft);text-indent:0}
.msg p:last-child{margin-bottom:0}

.end-mark{margin:4em 0 0;text-align:center;font-family:var(--sans);font-size:.8em;
  letter-spacing:.22em;color:var(--ink-faint);text-indent:0}
.ch-end{width:34px;height:1px;background:var(--rule);margin:4.5em auto 0}

.ch-nav{max-width:34em;margin:2.4em auto 0;padding:0 26px;display:flex;
  justify-content:space-between;gap:16px;font-family:var(--sans);font-size:12.5px;
  letter-spacing:.05em;color:var(--ink-faint)}
.ch-nav a{color:var(--ink-soft);text-decoration:none;transition:color .25s var(--ease)}
.ch-nav a:hover{color:var(--accent)}
.ch-nav a:last-child{text-align:right}

footer{max-width:34em;margin:0 auto;padding:16vh 26px 12vh;text-align:center;
  font-family:var(--sans);font-size:11.5px;letter-spacing:.2em;color:var(--ink-faint);
  line-height:2.4}
footer a{color:var(--ink-soft);text-decoration:none;transition:color .25s var(--ease)}
footer a:hover{color:var(--accent)}

/* 回顶 */
#top{position:fixed;right:20px;bottom:24px;z-index:40;width:38px;height:38px;
  border-radius:50%;background:var(--bg-elev);color:var(--ink-soft);
  border:1px solid var(--rule);cursor:pointer;font-size:14px;box-shadow:var(--shadow);
  opacity:0;transform:translateY(8px) scale(.9);pointer-events:none;
  transition:opacity .35s var(--ease),transform .35s var(--ease),color .25s var(--ease)}
#top.on{opacity:1;transform:none;pointer-events:auto}
#top:hover{color:var(--accent);transform:translateY(-2px)}

@media (min-width:1080px){
  #toc{transform:none;background:transparent;border-right:0}
  #toc.hidden{transform:translateX(-100%)}
  #scrim{display:none}
  /* 侧栏常驻时整页让开：用 body 内缩而非 main 的 padding，
     因为 main 是 border-box，加内边距会压缩字栏宽度 */
  html[data-toc=on] body{padding-left:238px}
  html[data-toc=on] #tools{left:238px}
}
@media (max-width:600px){
  main{font-size:calc(17px * var(--font-scale));line-height:1.98;padding:0 20px 30vh}
  .chapter{padding-top:9vh}
  .ch-head{margin-bottom:6.5vh}
  #cover h1{letter-spacing:.22em;text-indent:.22em}
  #toc{width:76vw;padding-top:66px}
  #top{right:14px;bottom:18px}
  .ch-nav{padding:0 20px;font-size:12px}
}
@media print{
  #tools,#toc,#scrim,#bar,#top,#cover .go,#back,.ch-nav{display:none}
  body{background:#fff;color:#000;padding-left:0!important}
  /* 首章紧跟封面，不需要再分页——否则会多出一张空白页 */
  .chapter{page-break-before:always;padding-top:0}
  .chapter:first-of-type{page-break-before:avoid}
  .rise{opacity:1!important;transform:none!important}
}
"""

SHELF_CSS = r"""
body{padding:0}
#shelf-tools{position:fixed;top:0;right:0;left:0;height:46px;z-index:50;display:flex;
  align-items:center;justify-content:flex-end;gap:2px;padding:0 14px;
  background:color-mix(in srgb,var(--bg) 86%,transparent);
  backdrop-filter:blur(12px) saturate(1.4);
  border-bottom:1px solid transparent;transition:border-color .4s var(--ease)}
#shelf-tools.lifted{border-bottom-color:var(--rule)}

.hero{padding:20vh 26px 12vh;text-align:center}
.hero h1{margin:0;font-size:clamp(34px,7vw,52px);font-weight:400;letter-spacing:.34em;
  text-indent:.34em;animation:titleIn 1.1s var(--ease) both}
.hero .line{width:1px;height:44px;margin:26px auto 0;
  background:linear-gradient(var(--rule),transparent);
  animation:dropIn .9s var(--ease) .3s both}
.hero .meta{margin-top:22px;font-family:var(--sans);font-size:11.5px;letter-spacing:.2em;
  color:var(--ink-faint);animation:fadeUp .9s var(--ease) .42s both}
@keyframes titleIn{from{opacity:0;transform:translateY(18px);letter-spacing:.54em}
  to{opacity:1;transform:none}}
@keyframes fadeUp{from{opacity:0;transform:translateY(14px)}to{opacity:1;transform:none}}
@keyframes dropIn{from{opacity:0;transform:scaleY(0);transform-origin:top}
  to{opacity:1;transform:none}}

.books{max-width:720px;margin:0 auto;padding:0 26px 14vh;
  display:flex;flex-direction:column;gap:26px}

/* 卡片自带的 transition 会整体覆盖 .rise 的，所以这里必须带上 opacity，
   否则入场只会有位移没有淡入 */
.card{display:flex;gap:26px;padding:22px;text-decoration:none;color:inherit;
  background:var(--bg-elev);border:1px solid var(--rule);border-radius:4px;
  box-shadow:var(--shadow);position:relative;overflow:hidden;
  transition:opacity .55s var(--ease),transform .55s var(--ease),
             box-shadow .4s var(--ease),border-color .4s var(--ease)}
/* 悬停时该书主色从左缘渗入 */
.card::before{content:"";position:absolute;inset:0 auto 0 0;width:2px;
  background:var(--bk);transform:scaleY(0);transform-origin:center;
  transition:transform .45s var(--ease)}
/* 用 .rise.in.card 提高特异性，压过 .rise.in 的 transform:none */
.rise.in.card:hover{transform:translateY(-4px);box-shadow:var(--shadow-lift);
  border-color:color-mix(in srgb,var(--bk) 42%,var(--rule))}
.rise.in.card:active{transform:translateY(-1px) scale(.995)}
.card:hover::before{transform:none}

/* 生成式封面：主色纸纹 + 竖排书名 */
.cv{flex:0 0 116px;height:174px;border-radius:2px;overflow:hidden;position:relative;
  display:flex;align-items:center;justify-content:center;
  background:linear-gradient(155deg,
    color-mix(in srgb,var(--bk) 88%,#000 4%),
    color-mix(in srgb,var(--bk) 58%,#2b2724));
  box-shadow:inset 0 0 0 1px rgba(255,255,255,.12),
             inset -7px 0 12px -8px rgba(0,0,0,.55);
  transition:transform .5s var(--ease)}
.card:hover .cv{transform:scale(1.035)}
.cv img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover}
/* 竖排书名：不换列，长书名自动缩字号，避免折成两列挤满封面 */
.cv em{font-style:normal;writing-mode:vertical-rl;white-space:nowrap;
  font-size:var(--cvfs,17px);letter-spacing:.14em;line-height:1;
  color:#fff;opacity:.94;text-shadow:0 1px 10px rgba(0,0,0,.4);
  border:1px solid rgba(255,255,255,.28);padding:12px 5px;border-radius:1px}

.info{flex:1;min-width:0;display:flex;flex-direction:column}
/* 右侧留出「成人向」浮标的位置 */
.info h2{margin:2px 56px 0 0;font-size:22px;font-weight:400;letter-spacing:.1em}
.info .sub{margin-top:7px;font-family:var(--sans);font-size:11px;letter-spacing:.24em;
  color:var(--ink-faint)}
.info .tag{margin:13px 0 0;font-size:13.5px;line-height:1.95;color:var(--ink-soft);
  text-align:justify;
  display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}
.info .foot{margin-top:auto;padding-top:15px;display:flex;align-items:center;
  flex-wrap:wrap;gap:8px;font-family:var(--sans);font-size:11px;letter-spacing:.1em;
  color:var(--ink-faint)}
/* 箭头始终占一行的末尾，元信息换行时不被挤走 */
.info .foot .arrow{order:9}
.pill{padding:3px 9px;border-radius:999px;border:1px solid var(--rule);line-height:1.6}
.pill.done{color:var(--bk);border-color:color-mix(in srgb,var(--bk) 40%,transparent);
  background:color-mix(in srgb,var(--bk) 9%,transparent)}
/* 夜间背景变暗后书的原色文字对比度不够，换成提亮版 */
html[data-theme=night] .pill.done{color:var(--bk-lit)}
/* 成人向做成卡片右上角浮标，不占元信息那行的宽度 */
.badge{position:absolute;top:0;right:0;z-index:1;
  font-family:var(--sans);font-size:9.5px;letter-spacing:.14em;color:var(--ink-faint);
  padding:5px 10px 6px;border-bottom-left-radius:4px;
  background:color-mix(in srgb,var(--ink) 5%,transparent);
  border-left:1px solid var(--rule);border-bottom:1px solid var(--rule)}
.arrow{margin-left:auto;color:var(--ink-faint);font-size:13px;
  transition:transform .4s var(--ease),color .4s var(--ease)}
.card:hover .arrow{transform:translateX(4px);color:var(--bk)}
html[data-theme=night] .card:hover .arrow{color:var(--bk-lit)}

.foot-note{padding:0 26px 14vh;text-align:center;font-family:var(--sans);
  font-size:11.5px;letter-spacing:.18em;color:var(--ink-faint);line-height:2.2}

@media (max-width:600px){
  .hero{padding:14vh 22px 9vh}
  .books{padding:0 18px 12vh;gap:20px}
  .card{gap:18px;padding:17px}
  /* 手机上封面按比例缩小，但字号只压一点点，太小会看不清 */
  .cv{flex-basis:92px;height:140px}
  .cv em{font-size:calc(var(--cvfs,17px) * .92);padding:9px 4px}
  .info h2{font-size:19px}
  .info .tag{font-size:12.5px;-webkit-line-clamp:4}
  /* 窄屏元信息压缩到一行，箭头省掉（整卡可点，不需要指示） */
  .info .foot{gap:6px;font-size:10.5px;padding-top:12px}
  .pill{padding:3px 7px}
  .info .foot .arrow{display:none}
  .badge{font-size:9px;padding:4px 8px 5px}
}
@media print{#shelf-tools{display:none}.card{box-shadow:none}}
"""


# ---------------------------------------------------------------- 脚本

# 主题偏好：书架与所有阅读页共用同一个 key，切换后跨页保持
THEME_JS = r"""
var TK='xhw-theme';
var root=document.documentElement;
function readTheme(){
  try{ var v=localStorage.getItem(TK); if(v) return v; }catch(e){}
  return matchMedia('(prefers-color-scheme: dark)').matches?'night':'day';
}
function paintTheme(t){
  root.dataset.theme=t;
  var b=document.getElementById('theme');
  if(b) b.textContent=(t==='night'?'日间':'夜间');
}
paintTheme(readTheme());
function toggleTheme(){
  var t=root.dataset.theme==='night'?'day':'night';
  try{ localStorage.setItem(TK,t); }catch(e){}
  // 支持时用 View Transitions 做交叉淡化
  if(document.startViewTransition && !matchMedia('(prefers-reduced-motion: reduce)').matches){
    document.startViewTransition(function(){ paintTheme(t); });
  }else{ paintTheme(t); }
}
// 观察器：元素进入视口后加 .in，一次性触发不回退。
// 构造或调用失败（旧浏览器 polyfill 缺陷等）时退化为直接全部显示，
// 避免因为动画层出错而导致正文停留在 opacity:0 的不可见状态。
var reveal=(function(){
  var showAll=function(els){ [].forEach.call(els,function(e){ e.classList.add('in'); }); };
  if(!window.IntersectionObserver || matchMedia('(prefers-reduced-motion: reduce)').matches){
    return showAll;
  }
  try{
    var io=new IntersectionObserver(function(es){
      es.forEach(function(e){
        if(e.isIntersecting){ e.target.classList.add('in'); io.unobserve(e.target); }
      });
    },{rootMargin:'0px 0px -12% 0px',threshold:.02});
    return function(els){
      try{ [].forEach.call(els,function(e){ io.observe(e); }); }
      catch(e){ showAll(els); }
    };
  }catch(e){ return showAll; }
})();
"""

READER_JS = r"""
(function(){
  // 关掉浏览器自带的滚动位置恢复，避免和下面按「章号+比例」算出的位置打架
  if('scrollRestoration' in history) history.scrollRestoration='manual';
  var PK='xhw-pos-'+document.body.dataset.book;
  var tools=document.getElementById('tools'), toc=document.getElementById('toc'),
      scrim=document.getElementById('scrim'), bar=document.getElementById('bar'),
      now=document.getElementById('now'), top=document.getElementById('top'),
      cover=document.getElementById('cover');
  var links=[].slice.call(toc.querySelectorAll('a[data-ch]'));
  var chapters=[].slice.call(document.querySelectorAll('.chapter'));
  var wide=function(){return window.innerWidth>=1080};
  var calm=matchMedia('(prefers-reduced-motion: reduce)').matches;

  // ---- 偏好
  var st={scale:1,ch:null,ratio:0};
  try{ st=Object.assign(st,JSON.parse(localStorage.getItem(PK)||'{}')); }catch(e){}
  var save=function(){ try{localStorage.setItem(PK,JSON.stringify(st))}catch(e){} };
  var applyScale=function(){ document.documentElement.style.setProperty('--font-scale',st.scale); };
  applyScale();

  document.getElementById('theme').onclick=toggleTheme;
  var STEPS=[.88,1,1.14,1.3], fontBtn=document.getElementById('font');
  var paintFont=function(){
    var i=STEPS.indexOf(st.scale); fontBtn.textContent='字号 '+((i<0?1:i+1))+'/'+STEPS.length;
  };
  paintFont();
  fontBtn.onclick=function(){
    var i=STEPS.indexOf(st.scale); st.scale=STEPS[(i<0?1:i+1)%STEPS.length];
    applyScale(); paintFont(); save();
  };

  // ---- 目录
  function setToc(open){
    toc.classList.toggle('open',open);
    if(wide()){
      toc.classList.toggle('hidden',!open);
      document.documentElement.dataset.toc=open?'on':'off';
      scrim.classList.remove('on');
    }else{
      scrim.classList.toggle('on',open);
      document.documentElement.dataset.toc='off';
    }
  }
  document.getElementById('menu').onclick=function(){ setToc(!toc.classList.contains('open')); };
  scrim.onclick=function(){ setToc(false); };
  toc.addEventListener('click',function(e){ if(e.target.closest('a')&&!wide()) setToc(false); });
  setToc(wide());
  document.addEventListener('keydown',function(e){
    if(e.key==='Escape'&&!wide()) setToc(false);
  });

  // ---- 入场动画
  reveal(document.querySelectorAll('.rise,.ch-head,.sec-mark'));

  // ---- 进度 / 当前章 / 封面视差
  var cur=-1, raf=0, vh=innerHeight;
  function tick(){
    raf=0;
    var y=window.scrollY, h=document.body.scrollHeight-vh;
    bar.style.width=(h>0?Math.min(y/h*100,100):0)+'%';
    tools.classList.toggle('lifted',y>vh*.55);
    top.classList.toggle('on',y>vh*1.2);

    // 封面随滚动缓慢上移淡出，只在首屏范围内计算
    if(cover && !calm){
      if(y<vh){
        var k=y/vh;
        cover.style.transform='translateY('+(y*.28)+'px)';
        cover.style.opacity=String(Math.max(0,1-k*1.35));
      }else if(cover.style.opacity!=='0'){
        cover.style.opacity='0';
      }
    }

    var probe=y+vh*.3, idx=-1;
    for(var i=0;i<chapters.length;i++) if(chapters[i].offsetTop<=probe) idx=i;
    if(idx!==cur){
      cur=idx;
      var c=chapters[idx];
      // 按 anchor 匹配而不是按下标：目录里混有 .sub-toc 二级项，下标对不上章节
      var curAnchor=c?c.dataset.anchor:null;
      links.forEach(function(a){ a.classList.toggle('on',a.dataset.ch===curAnchor); });
      if(c){
        var vol=c.dataset.volume;
        now.textContent=(vol?vol+' · ':'')+'第 '+c.dataset.num+' 章 · '+c.dataset.title;
      }else{ now.textContent=''; }
    }
    // 记录「章号 + 章内比例」而非绝对像素，字号/窗宽变化后仍能精确还原
    if(idx>=0){
      var chTop=chapters[idx].offsetTop;
      var chBottom=(idx+1<chapters.length)?chapters[idx+1].offsetTop:document.body.scrollHeight;
      st.ch=chapters[idx].dataset.anchor;
      st.ratio=Math.min(1,Math.max(0,(y-chTop)/Math.max(1,chBottom-chTop)));
    }
  }
  var onScroll=function(){ if(!raf) raf=requestAnimationFrame(tick); };
  addEventListener('scroll',onScroll,{passive:true});
  addEventListener('resize',function(){
    vh=innerHeight;
    setToc(wide()?!toc.classList.contains('hidden'):false);
    onScroll();
  });

  top.onclick=function(){ scrollTo({top:0,behavior:calm?'auto':'smooth'}); };

  // ---- 恢复进度（有 hash 时以 hash 为准）：按「章号 + 章内比例」重新定位，
  // 字号、窗宽变化甚至换设备后依然对得上。用 html.scroll-behavior 会让这一跳
  // 变成平滑滚过全书，恢复前临时关掉。
  if(st.ch!=null && !location.hash){
    var target=null;
    for(var i=0;i<chapters.length;i++) if(chapters[i].dataset.anchor===String(st.ch)){ target=chapters[i]; break; }
    if(target){
      var restore=function(){
        var chTop=target.offsetTop;
        var chBottom=(chapters.indexOf(target)+1<chapters.length)
          ? chapters[chapters.indexOf(target)+1].offsetTop : document.body.scrollHeight;
        var prevBehavior=document.documentElement.style.scrollBehavior;
        document.documentElement.style.scrollBehavior='auto';
        scrollTo(0, chTop+st.ratio*(chBottom-chTop));
        document.documentElement.style.scrollBehavior=prevBehavior;
        tick();
      };
      addEventListener('load',restore);
      setTimeout(function(){ if(!window.scrollY) restore(); },60);
    }
  }
  tick();
  addEventListener('beforeunload',save);
  addEventListener('pagehide',save);
  document.addEventListener('visibilitychange',function(){
    if(document.visibilityState==='hidden') save();
  });
  setInterval(save,4000);

  // ---- 左右方向键翻章
  document.addEventListener('keydown',function(e){
    if(e.target.closest('input,textarea')) return;
    var d = (e.key==='ArrowRight'||e.key==='j') ? 1 : ((e.key==='ArrowLeft'||e.key==='k') ? -1 : 0);
    if(!d) return;
    var n=Math.max(0,Math.min(chapters.length-1,(cur<0?0:cur)+d));
    chapters[n].scrollIntoView({behavior:calm?'auto':'smooth'});
  });
})();
"""

SHELF_JS = r"""
(function(){
  document.getElementById('theme').onclick=toggleTheme;
  var cards=[].slice.call(document.querySelectorAll('.card'));
  cards.forEach(function(c,i){ c.style.transitionDelay=(i*60)+'ms'; });
  reveal(cards);
  // 入场延迟只用于首次，避免之后悬停也跟着延迟
  setTimeout(function(){ cards.forEach(function(c){ c.style.transitionDelay=''; }); },
    260+cards.length*60);

  // 滚动后工具条显出分隔线
  var tb=document.getElementById('shelf-tools'), raf=0;
  addEventListener('scroll',function(){
    if(raf) return;
    raf=requestAnimationFrame(function(){
      raf=0; tb.classList.toggle('lifted',scrollY>10);
    });
  },{passive:true});
})();
"""

# 预绘制：在 <head> 内同步执行，赶在首次渲染前把主题 / 字号 / 侧栏状态
# 钉到 <html> 上，避免先刷默认态再被 </body> 前的脚本跳变。
SHELF_PREPAINT_JS = r"""(function(){
var r=document.documentElement;
try{
  var t=localStorage.getItem('xhw-theme');
  if(!t) t=matchMedia('(prefers-color-scheme: dark)').matches?'night':'day';
  r.dataset.theme=t;
}catch(e){}
})();"""

READER_PREPAINT_JS = r"""(function(){
var r=document.documentElement, book=document.currentScript.dataset.book;
try{
  var t=localStorage.getItem('xhw-theme');
  if(!t) t=matchMedia('(prefers-color-scheme: dark)').matches?'night':'day';
  r.dataset.theme=t;
}catch(e){}
try{
  var st=JSON.parse(localStorage.getItem('xhw-pos-'+book)||'{}');
  if(st.scale) r.style.setProperty('--font-scale',st.scale);
}catch(e){}
if(matchMedia('(min-width:1080px)').matches) r.dataset.toc='on';
})();"""


# ---------------------------------------------------------------- 渲染

def render_chapter(ch, first, prev_ch=None, next_ch=None):
    """渲染一章。first=True 表示是全书第一章，其内容不做滚动入场（首屏可见）。
    prev_ch / next_ch 用于章末的上一章/下一章导航。

    返回 (html, sections)：sections 是带小标题的分节符列表 [(id, label), ...]，
    只有像「出差」这种单章多分节、每节带小标题的情况才非空，用于目录里的二级
    导航——没有小标题的纯装饰性「一/二/三」不值得占目录条目。
    """
    vol = ch.get("volume") or ""
    ch_label = (f"{vol} · " if vol else "") + f'第 {ch["num"]} 章'
    out = [
        f'<section class="chapter" id="{ch["anchor"]}"'
        f' data-num="{ch["num"]}" data-title="{html.escape(ch["title"])}"'
        f' data-anchor="{ch["anchor"]}" data-volume="{html.escape(vol)}">',
        '  <header class="ch-head">',
        f'    <div class="ch-num">{html.escape(ch_label)}</div>',
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
    out.append('<div class="ch-end"></div>')
    if prev_ch or next_ch:
        prev_a = (
            f'<a href="#{prev_ch["anchor"]}">← {html.escape(prev_ch["title"])}</a>'
            if prev_ch else '<span></span>'
        )
        next_a = (
            f'<a href="#{next_ch["anchor"]}">{html.escape(next_ch["title"])} →</a>'
            if next_ch else '<span></span>'
        )
        out.append(f'<nav class="ch-nav">{prev_a}{next_a}</nav>')
    return "\n".join(out), sections


def render_reader(book, has_shelf):
    """渲染单本书的阅读页（单文件，内联全部样式与脚本）。"""
    chapters = book["chapters"]
    accent, lit = book["accent"], lighten(book["accent"])

    rendered = [
        render_chapter(
            c, i == 0,
            prev_ch=chapters[i - 1] if i > 0 else None,
            next_ch=chapters[i + 1] if i + 1 < len(chapters) else None,
        )
        for i, c in enumerate(chapters)
    ]
    body = "\n\n".join(html_ for html_, _ in rendered)

    toc_parts = []
    cur_vol = None
    for i, c in enumerate(chapters):
        vol = c.get("volume") or ""
        # 分卷的书在目录里插入卷标题；不分卷时 vol 恒为 "" 不会触发
        if vol != cur_vol:
            cur_vol = vol
            if vol:
                toc_parts.append(f'    <div class="toc-vol">{html.escape(vol)}</div>')
        toc_parts.append(
            f'    <a href="#{c["anchor"]}" data-ch="{c["anchor"]}" style="--d:{i * 26}ms">'
            f'<i>{c["num"]:02d}</i>{html.escape(c["title"])}</a>'
        )
        for sec_id, sub in rendered[i][1]:
            toc_parts.append(f'    <a href="#{sec_id}" class="sub-toc">{html.escape(sub)}</a>')
    toc_items = "\n".join(toc_parts)

    title = html.escape(book["title"])
    sub = html.escape(book["subtitle"])
    back = '  <a id="back" href="index.html">← 书架</a>\n' if has_shelf else ""
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
<style>{BASE_CSS}{READER_CSS}</style>
</head>
<body data-book="{title}">
<div id="bar"></div>

<nav id="tools">
{back}  <button class="tbtn" id="menu" aria-label="目录">目录</button>
  <span class="now" id="now"></span>
  <button class="tbtn" id="font" aria-label="调整字号">字号</button>
  <button class="tbtn" id="theme" aria-label="切换日间夜间">夜间</button>
</nav>

<div id="scrim"></div>
<aside id="toc" aria-label="章节目录">
  <h3>目 录</h3>
{toc_items}
</aside>

<header id="cover">
  <div class="rule"></div>
  <h1>{title}</h1>
{sub_el}{epi}  <div class="meta">全 {len(chapters)} 章 · {fmt_wan(book['chars'])}</div>
  <a class="go" href="#{chapters[0]['anchor']}">开始阅读 <span>→</span></a>
</header>

<main>
{body}
</main>

<footer>
  {title}{' · ' + sub if sub else ''}<br>{foot_link}
</footer>

<button id="top" aria-label="回到顶部">↑</button>
<noscript><style>.rise,.ch-head,.sec-mark{{opacity:1!important;transform:none!important}}
#toc{{transform:none!important}}#scrim{{display:none!important}}</style></noscript>
<script>{THEME_JS}{READER_JS}</script>
</body>
</html>
"""


def render_shelf(books):
    """渲染书架首页。"""
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
        cards.append(
            f'''    <a class="card rise" href="{html.escape(b["file"])}"
       style="--bk:{accent};--bk-lit:{lit}">
      {badge}<div class="cv">{cover_inner}</div>
      <div class="info">
        <h2>{title}</h2>
{sub}{tag}        <div class="foot">
          <span class="pill{done}">{html.escape(b["status"])}</span>
          <span>{len(b["chapters"])} 章</span>
          <span>{fmt_wan(b["chars"])}</span>
          <span class="arrow">→</span>
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

<div class="foot-note">点开任意一本开始阅读<br>每本书都是独立页面，可单独分享</div>

<noscript><style>.rise{{opacity:1!important;transform:none!important}}</style></noscript>
<script>{THEME_JS}{SHELF_JS}</script>
</body>
</html>
"""


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
    """影响网页产物的源文件：生成器本身、book.toml、章节、封面图。"""
    yield Path(__file__).resolve()
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


def build(quiet=False):
    """编译全部作品到 site/。成功返回 True。"""
    dirs = iter_book_dirs()
    books = []
    for d in dirs:
        b = load_book(d)
        if not b["chapters"]:
            if not quiet:
                print(f"  跳过 {d.name}：章节/ 下没有 ch*.md")
            continue
        b["file"] = f"{safe_filename(b['title'])}.html"
        books.append(b)
    if not books:
        print(f"未在 {BOOKS_DIR} 下找到任何作品", file=sys.stderr)
        return False

    books.sort(key=lambda b: (b["order"], b["title"]))

    SITE_DIR.mkdir(exist_ok=True)
    has_shelf = len(books) > 1

    keep = {"index.html", ".build-id"}
    for b in books:
        (SITE_DIR / b["file"]).write_text(render_reader(b, has_shelf), encoding="utf-8")
        keep.add(b["file"])
        # 有封面图则拷进 site/，保持单目录可整体分享
        if b["cover"]:
            src = b["dir"] / b["cover"]
            cover_name = Path(b["cover"]).name
            keep.add(cover_name)
            if src.exists():
                shutil.copy2(src, SITE_DIR / cover_name)
            else:
                print(f"  ⚠ {b['title']}：封面图不存在 {src}")

    (SITE_DIR / "index.html").write_text(render_shelf(books), encoding="utf-8")

    # 清理陈旧产物：改书名 / 删书后，旧 HTML（含旧封面图）不会自动消失，
    # 残留在 site/ 里还会被一起分享出去。
    for stale in SITE_DIR.iterdir():
        if stale.is_file() and stale.name not in keep:
            stale.unlink()
            if not quiet:
                print(f"  🗑 清理陈旧产物 {stale.name}")

    # 供本地预览热刷新探测；分享 site/ 时无害，可忽略
    stamp = str(time.time_ns())
    (SITE_DIR / ".build-id").write_text(stamp, encoding="utf-8")

    if not quiet:
        print(f"✓ site/  {len(books)} 部作品 · {fmt_wan(sum(b['chars'] for b in books))}")
        for b in books:
            size = (SITE_DIR / b["file"]).stat().st_size // 1024
            print(f"  {b['title']:<8} {b['status']:<4} {len(b['chapters']):>2} 章 · "
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


def watch_loop(on_change=None, interval=0.5, settle=0.35):
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
            if safe_build():
                if on_change:
                    on_change()
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
})();
</script>
"""


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
            ".build-id": "text/plain; charset=utf-8",
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
            path = unquote(urlsplit(self.path).path)
            if path.endswith(".html") or path in ("/", ""):
                return self._send_html_with_reload(path)
            return super().do_GET()

        def _send_html_with_reload(self, path):
            rel = "index.html" if path in ("/", "") else path.lstrip("/")
            # 防目录穿越
            target = (SITE_DIR / rel).resolve()
            try:
                target.relative_to(SITE_DIR.resolve())
            except ValueError:
                self.send_error(403)
                return
            if not target.is_file():
                self.send_error(404)
                return
            body = target.read_text(encoding="utf-8")
            if "</body>" in body:
                body = body.replace("</body>", _LIVE_RELOAD_JS + "\n</body>", 1)
            else:
                body += _LIVE_RELOAD_JS
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    try:
        httpd = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    except OSError as e:
        raise SystemExit(f"无法监听端口 {port}：{e}\n可换：python build.py --serve {port + 1}")

    url = f"http://127.0.0.1:{port}/"
    print(f"预览地址：{url}")
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
        "--no-open", action="store_true",
        help="配合 --serve：不自动打开浏览器",
    )
    args = parser.parse_args(argv)

    if args.serve is not None:
        serve(port=args.serve, open_browser=not args.no_open)
        return

    if args.watch:
        if not safe_build("首次构建完成"):
            raise SystemExit(1)
        watch_loop()
        return

    if not build():
        raise SystemExit(1)


if __name__ == "__main__":
    main()
