"""网页通知源：把**公开发布**的通知列表页读成归一化消息。

存在的理由（`docs/网页通知源取数方案.md`）：VIGIL 需要一条**合规、自动、
可复现**的数据路径。公开通知页是三条都满足的那一条 ——
读的是学校对公众发布的信息，不需要任何凭据，评委也能自己跑。

⚠️ **只读公开页面，绝不登 OA / WebVPN。** 那是两条性质完全不同的事：
   · 通知页 = 学校主动向公众发布的
   · 一网通办 / 教务系统 = 学校**刻意设防**的内部系统（实测 302 跳 WebVPN）
用学号密码进后者，是"用你的身份进入学校设防的系统"，且**评委复现不了**。

⚠️ **零新增依赖**：用标准库 `urllib` + `html.parser`。项目核心依赖只有
fastapi + uvicorn，这是刻意维持的（"依赖极轻"是复现友好的卖点）。
引入 requests/bs4/lxml 会破坏它，而本场景用不上 —— 实测吉大本科生院的
通知列表**原始 HTML 里就有全量数据**，不需要 JS 渲染。

⚠️ **抓取产物可落盘成夹具**（`fetch_groups()` + `save_fixture()`）：
演示与复现从夹具读，不碰网络。不这么做的话，站点改版/评委不在校内网/
学校站点故障都会让复现失败 —— 而复数红线是取消资格级别的。
"""

from __future__ import annotations

import hashlib
import html
import json
import pathlib
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from html.parser import HTMLParser

from .config import Config, ConfigError, atomic_write_text
from .source import NON_TEXT, GroupRead, Sender, SourceMessage

# 网络请求超时。⚠️ 刻意不长：一个挂死的通知页不该让整条管线卡住，
# 而 `read_group` 的契约是**读不到就抛**，抛得越早越好。
_TIMEOUT = 20

# User-Agent。⚠️ 不伪装成浏览器（那是爬虫对抗的做法，而我们在读**公开页面**）；
# 但也不发 Python 默认 UA —— 有些站点对默认 UA 直接 403。用一条诚实且可联系的。
#
# ⚠️⚠️ **必须是纯 ASCII**（实测踩到）：HTTP 头按 latin-1 编码，
# 一个中文字符就会让 `urllib` 在 `putheader` 抛
# `UnicodeEncodeError: 'latin-1' codec can't encode characters` ——
# 而那是在**建连接之前**抛的，看起来像网络问题，实际是这一行字面量。
_UA = "VIGIL/0.2 (+https://github.com/Polaris-bit189/vigil)"


@dataclass(frozen=True)
class NotifySource:
    """一个通知源 = 下游视角里的一个「群」。

    ⚠️ `list` / `title` / `date` / `link` 是**选择器**，刻意支持得很小
    （标签名 / .class / 元素@属性）—— 自己实现完整 CSS 选择器引擎是
    另一个量级的工作，而高校网站群系统是同形的，简单的够了。
    真需要复杂选择器时应换实现方式，而不是把简单选择器慢慢养大。
    """

    name: str          # 源名（如「吉林大学本科生院·通知公告」）
    url: str           # 列表页 URL
    list: str          # 每一条通知所在的行（容器）
    title: str         # 行内取标题的元素
    date: str          # 行内取日期的元素
    link: str          # 行内取链接，形如 `a@href`
    max_pages: int = 1  # 翻页上限（默认 1 = 只抓首页）


# ── 选择器 ────────────────────────────────────────────────────

def _parse_selector(sel: str) -> tuple[str, str | None, str | None]:
    """`"a@href"` → `("a", None, "href")`；`"tr.listbottomline"` → `("tr", "listbottomline", None)`。

    ⚠️ 只支持这三种形态，**故意不做完整 CSS**（见 `NotifySource` 的说明）。
    解析不出来就抛 —— 静默忽略一个拼错的选择器会让"抓不到"看起来像
    "这个源本来就没内容"。
    """
    attr: str | None = None
    if "@" in sel:
        if sel.count("@") > 1:
            raise ConfigError(
                f"选择器 {sel!r} 里有多个 @ —— 去掉属性名（如 `a@href`）"
            )
        sel, attr = sel.split("@", 1)
        if not attr or not sel:
            raise ConfigError(f"选择器 {sel!r} 的 @ 前后都必须有内容（如 a@href）")
    cls: str | None = None
    if "." in sel:
        sel, cls = sel.split(".", 1)
    tag = sel.strip()
    if not tag:
        raise ConfigError(f"选择器里没有标签名：{sel!r}")
    return tag.lower(), cls, attr


def _attrs_of(tag: str, attrs: list[tuple[str, str | None]]) -> dict[str, str]:
    return {k: (v or "") for k, v in attrs}


def _matches(
    tag: str, attrs: dict[str, str], sel: tuple[str, str | None, str | None]
) -> bool:
    """`tag` 是否命中选择器 `sel`（`_parse_selector` 的三元组）。

    ⚠️ 只比对**标签名与 class**，**不比对 attr** —— `a@href` 里的 `href`
    是"要取哪个属性"，不是"要匹配的属性"。把两者混起来会让
    `link = "a@href"` 变成"只匹配带 href 的 a"，看似无害，
    但换成 `img@src` 之类的选择器就会静默漏掉一大批。
    """
    sel_tag, sel_cls, _attr = sel
    if tag != sel_tag:
        return False
    if sel_cls is None:
        return True
    return sel_cls in attrs.get("class", "").split()


class _RowParser(HTMLParser):
    """把列表页解析成 `[(日期, 标题, href)]`。

    ⚠️ **不用正则扫全文**：正则会被嵌套标签、注释、属性里的 `>` 骗到，
    而 `HTMLParser` 是标准库里的真解析器，代价只是多写一个子类。
    （项目里已有先例：`fetch.py` 走的就是 `html.parser`。）

    解析策略：先按 `list` 选择器找到每一行的**起止位置**，再在行内找
    `title` / `date` / `link` 三个元素。用「行内收集」而不是「全文收集后配对」——
    后者在两个列表混排的页面上会错配。
    """

    def __init__(self, src: NotifySource):
        super().__init__(convert_charrefs=True)
        self._list_sel = _parse_selector(src.list)
        self._title_sel = _parse_selector(src.title)
        self._date_sel = _parse_selector(src.date)
        self._link_sel = _parse_selector(src.link)
        self._row_tag = ""       # 进入行时的标签名（配对它就知道行尾在哪）
        self._in_row = False
        self._row: dict[str, str] = {}
        self._text_target: str | None = None  # 正在收集哪个字段的文本
        self._buf: list[str] = []
        self.rows: list[dict[str, str]] = []

    def handle_starttag(self, tag: str, attrs) -> None:
        a = _attrs_of(tag, attrs)
        if not self._in_row:
            if _matches(tag, a, self._list_sel):
                self._in_row = True
                # ⚠️⚠️ **记下"行标签"的名字与嵌套深度**（实测踩到的一个真 bug）：
                # 原实现用 `depth == 0` 判行结束，但**行元素自己**（`<li>`）
                # 进入时没被计数 ⇒ 内部 `<span>` 结束时 depth 从 0 变 1（该减却加），
                # 于是 `</li>` 时 depth 不对，行**永远收不了**。
                # 症状：`list = "li.x"` 这类选择器解析出 **0 条**（静默）。
                #
                # ⇒ 用"进入行时自己的标签名"配对，配到同名的结束标签就是行尾。
                # 这比数嵌套深度简单，且对 `<li>` 里嵌 `<li>` 的畸形 HTML 更稳
                # （那种页面本来就该跳过，不该让整个解析崩掉）。
                self._row_tag = tag
                self._row = {}
            return

        # 行内：找三个目标元素
        if _matches(tag, a, self._title_sel):
            self._text_target, self._buf = "title", []
            # ⚠️ `title` 属性**比显示文本完整**（显示的是截断版，带省略号）。
            # 实测吉大页面：`<a href="...">关于开展2025级"基础学科..."工作...</a>`
            # 而 `title=` 属性里是完整标题。优先取属性。
            if a.get("title"):
                self._row["title"] = a["title"]
        if _matches(tag, a, self._date_sel):
            self._text_target, self._buf = "date", []
        if _matches(tag, a, self._link_sel):
            self._row["href"] = a.get(self._link_sel[2] or "href", "")

    def handle_endtag(self, tag: str) -> None:
        if not self._in_row:
            return
        if self._text_target is not None:
            # ⚠️ 只看标签名，不比对选择器：目标元素可能带 id/class 变体，
            # 而"当前正在收集的字段"这个状态本身已经说明了我们在哪个元素里。
            if tag == self._target_tag():
                text = "".join(self._buf).strip()
                # ⚠️⚠️ **不要覆盖已有的值**（实测踩到，是个真 bug）：
                # `handle_starttag` 已经取了 `title` 属性（那才是**完整**标题），
                # 而显示文本是**截断版**。这里无条件赋值会把属性值盖掉 ——
                # 于是"优先取属性"那条注释与行为**相反**。
                #
                # ⇒ 只在还没有值时才用文本（`<span>` 日期走这支，它没有属性）。
                if not self._row.get(self._text_target):
                    self._row[self._text_target] = text
                self._text_target, self._buf = None, []
        if tag == self._row_tag:
            self._in_row = False
            if self._row.get("title") or self._row.get("href"):
                self.rows.append(self._row)

    def handle_data(self, data: str) -> None:
        if self._in_row and self._text_target is not None:
            self._buf.append(data)

    def _target_tag(self) -> str:
        """当前正在收集的字段对应的标签名。"""
        if self._text_target == "title":
            return self._title_sel[0]
        return self._date_sel[0]


# ── 抓取 ──────────────────────────────────────────────────────

def _http_get(url: str) -> str:
    """取一个页面。**失败一律抛 `SourceError`**（不返回空串）。

    ⚠️ 与 `read_group` 的契约一致：空串与"这页没有内容"完全同形，
    调用方会把抓取失败记成 0 条成功。
    """
    req = urllib.request.Request(url, headers={"User-Agent": _UA})
    try:
        with urllib.request.urlopen(req, timeout=_TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise SourceError(f"{url} 返回 HTTP {exc.code}") from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"{url} 连不上：{exc.reason}") from exc
    except (TimeoutError, OSError) as exc:
        raise SourceError(f"{url} 读取失败：{exc}") from exc
    # ⚠️ `errors="replace"` 而不是严格解码：站点用 GBK/UTF-8 混杂是常态，
    # 一个坏字节不该让整页读不了（那与我们**没有**在做的"校验"是两件事）。
    return raw.decode("utf-8", "replace")


class SourceError(RuntimeError):
    """抓取失败。与 `qqdb.QQDBError` 同族 —— 让调用方一眼看出是**源**的问题。"""


def _md5_int(text: str) -> int:
    """字符串 → 稳定的正整数 ID。

    ⚠️ **绝不能用内建 `hash()`** —— 它对字符串加了**每进程随机**的盐
    （`PYTHONHASHSEED`），同一个源两次跑会得到不同的 ID。
    项目在合成源里踩过这个坑并留了注释（`synth.py`）。这里取 md5 前 8 位。
    """
    return int(hashlib.md5(text.encode("utf-8")).hexdigest()[:8], 16)


def _article_id(href: str) -> int | None:
    """从 URL 里抠出文章 ID（吉大页面形如 `../info/1050/12635.htm`）。

    ⚠️ 有它就用它，因为它**跨次抓取稳定**（同一篇文章永远同一个 ID）。
    取不到才回落到 href 的 md5。
    """
    import re

    m = re.search(r"/(\d+)\.htm", href)
    return int(m.group(1)) if m else None


def parse_list_page(html_text: str, src: NotifySource) -> list[dict[str, str]]:
    """解析一页列表，返回 `[{date, title, href}, ...]`（**已补全 href**）。"""
    parser = _RowParser(src)
    parser.feed(html_text)
    parser.close()
    out = []
    for row in parser.rows:
        href = row.get("href", "").strip()
        title = html.unescape(row.get("title", "").strip())
        date = row.get("date", "").strip()
        if not href or not title:
            continue
        # ⚠️⚠️ **日期不合法就跳过这一行**（实测必需，不是可选优化）。
        #
        # `list = "li"` 这类选择器会把**导航栏**也匹配进来 —— 实测吉大页面上
        # `li` 命中 29 条，其中只有 15 条是真通知，另外 14 条是
        # 「首页 / 组织机构 / 校发文件…」。
        #
        # ⚠️ 为什么用"日期格式"当判据，而不是写一个更紧的选择器：
        # 导航项与通知项在**DOM 结构上是同形的**（都是 `li > a`），
        # 唯一的区别就是通知多一个 `<span>YYYY-MM-DD</span>`。
        # 换更紧的选择器（如 `li > span` 的父节点）需要 CSS 组合器支持，
        # 而那超出本模块刻意保持的小选择器集。
        #
        # ⇒ 判据是「这一行有没有一个能解析成日期的字段」。它**保守**：
        # 拿不准的行一律不当通知，而漏掉一条导航项无损失。
        if _date_ts(date) == 0:
            continue
        out.append(
            {"date": date, "title": title, "href": urllib.parse.urljoin(src.url, href)}
        )
    return out


# ── 源实现 ────────────────────────────────────────────────────

class WebSource:
    """`Source` 协议的一个实现：每个配置的通知源 = 一个「群」。

    ⚠️ **两种取数模式**（复现红线的关键）：
      · `pages` 为空 ⇒ **实时抓取**（真实使用）
      · `pages` 非空 ⇒ 从**夹具**读（演示与复现走这条）
    两者产出**同一种** `SourceMessage`，所以夹具能验证实时路径。
    """

    def __init__(
        self,
        sources: tuple[NotifySource, ...],
        *,
        pages: dict[str, list[dict[str, str]]] | None = None,
    ):
        if not sources:
            raise ConfigError(
                "网页源没有配置任何 [[sources]]。\n"
                "  请在 config/sources.toml 里至少写一个通知源（格式见文件头注释）。"
            )
        self._sources = sources
        self._group_ids = {s.name: _md5_int(s.url) for s in sources}
        # 夹具：{源名: [解析后的条目]}。非空 ⇒ 不联网。
        self._pages = pages or {}

    # ── 五个方法 ──────────────────────────────────────────────

    def list_groups(self) -> list[tuple[int, int, int]]:
        """[(群号, 消息数, 最新时间戳)]。

        ⚠️ 实时模式下这会**真的去抓每一页** —— 它被 `vigil groups` 调用，
        而那个命令的用途就是"看看有哪些源、各有多少条"。
        """
        out = []
        for s in self._sources:
            items = self._items_of(s)
            latest = max((_date_ts(i["date"]) for i in items), default=0)
            out.append((self._group_ids[s.name], len(items), latest))
        out.sort(key=lambda t: t[1], reverse=True)
        return out

    def group_names(self) -> dict[str, str]:
        return {str(self._group_ids[s.name]): s.name for s in self._sources}

    def read_group(self, gid: int) -> GroupRead:
        """读一个源的全部条目。⚠️ **抓不到必须抛**（契约见 `source.py`）。"""
        src = next((s for s in self._sources if self._group_ids[s.name] == gid), None)
        if src is None:
            raise SourceError(f"没有这个通知源（group_id={gid}）")

        items = self._items_of(src)
        messages = []
        for i, item in enumerate(items):
            aid = _article_id(item["href"])
            messages.append(
                SourceMessage(
                    msg_id=aid if aid is not None else _md5_int(item["href"]),
                    group_id=gid,
                    ts=_date_ts(item["date"]),
                    sender_uid=src.name,
                    content=_compose(item),
                )
            )
        return GroupRead(messages=messages)

    def sender_names(self) -> dict[tuple[int, str], Sender]:
        """每个源一个 Sender：以部门名为群昵称。

        ⚠️ 没有 QQ 号（uin=0）—— 通知页没有也不需要这个信息。
        `display_name` 会回落到 `group_nick`，所以下游拿到的仍是「本科生院」。
        """
        return {
            (self._group_ids[s.name], s.name): Sender(group_nick=s.name, in_group=True)
            for s in self._sources
        }

    def media_index(self) -> dict[str, pathlib.Path]:
        """通知页没有本地图片缓存。"""
        return {}

    # ── 内部 ──────────────────────────────────────────────────

    def _items_of(self, src: NotifySource) -> list[dict[str, str]]:
        """取一个源的全部条目（夹具优先，否则实时抓）。"""
        if src.name in self._pages:
            return self._pages[src.name]

        items: list[dict[str, str]] = []
        seen: set[str] = set()
        for page in range(1, max(1, src.max_pages) + 1):
            url = src.url if page == 1 else _page_url(src.url, page)
            page_items = parse_list_page(_http_get(url), src)
            if not page_items:
                # ⚠️ 一页解析出 0 条 ⇒ **抛**，不静默当"这页没内容"。
                # 选择器失效（站点改版）就是这个形状，而那必须响亮。
                raise SourceError(
                    f"{url} 解析出 0 条 —— 选择器可能已失效（站点改版？）。\n"
                    f"  当前选择器：list={src.list!r} title={src.title!r} date={src.date!r}"
                )
            for it in page_items:
                if it["href"] not in seen:
                    seen.add(it["href"])
                    items.append(it)
        return items


def _page_url(base: str, page: int) -> str:
    """列表页的翻页 URL。吉大规则：`.../tzgg.htm` → `.../tzgg/2.htm`。

    ⚠️ 这是**站点专有**的规则，所以它留在这里而不是 `Source` 协议里。
    别的网站群系统若规则不同，应在配置里给模板而不是改这里
    （YAGNI：目前只有一所学校的实测样本，先不为想象中的规则加抽象）。
    """
    stem = base[:-4] if base.endswith(".htm") else base
    return f"{stem}/{page}.htm"


def _date_ts(date: str) -> int:
    """`"2026-09-15"` → unix 秒（当天 **00:00**）。

    ⚠️ 列表页只给到日，**没有时刻** —— 用 00:00 是刻意的，不是"编一个时间"。
    这与项目「时刻必须有字面依据」的纪律一致（见 `deadline.start_supported`）。
    取不到日期返回 0（下游会当"没有时间信息"处理）。
    """
    import datetime as dt

    text = date.strip()
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d"):
        try:
            return int(dt.datetime.strptime(text, fmt).timestamp())
        except ValueError:
            continue
    return 0


def _compose(item: dict[str, str]) -> str:
    """把条目拼成 `content` 文本。

    ⚠️ `SourceMessage.content` 的契约是**非空文本**（`messages.content` 是
    `TEXT NOT NULL`）。标题为空时用 `NON_TEXT` 占位，与 QQ 源同一约定 ——
    下游的 prefilter 拿它判"这条要不要丢"。
    """
    title = item.get("title", "").strip()
    if not title:
        return NON_TEXT
    date = item.get("date", "").strip()
    return f"{title}\n发布: {date}" if date else title


# ── 配置读取 ──────────────────────────────────────────────────

def load_sources(path: pathlib.Path) -> tuple[NotifySource, ...]:
    """读 `config/sources.toml`。

    ⚠️ **文件不存在 ⇒ 抛**（与 `load_config` 同规，与 `load_persons` 相反）：
    选了 `source = "web"` 却没写这份文件是**配置错误**，不是"合法的空状态"——
    静默返回空会让 `vigil ingest` 跑出一个"0 个源"的成功。
    """
    import tomllib

    if not path.is_file():
        raise ConfigError(
            f"选了网页源但找不到 {path}。\n"
            "  请在 config/sources.toml 里写至少一个 [[sources]]（格式见方案文档）。"
        )
    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: TOML 语法错误——{exc}") from exc

    entries = raw.get("sources", [])
    if not isinstance(entries, list):
        raise ConfigError(f"{path}: `sources` 必须是一串表（每个写成 `[[sources]]`）")

    out: list[NotifySource] = []
    for i, e in enumerate(entries):
        where = f"{path}: 第 {i + 1} 个 [[sources]]"
        if not isinstance(e, dict):
            raise ConfigError(f"{where} 必须是键值表，实际是 {type(e).__name__}")
        for key in ("name", "url", "list", "title", "date", "link"):
            if not isinstance(e.get(key), str) or not e[key].strip():
                raise ConfigError(f"{where} 缺少必填的字符串字段 `{key}`")
        pages = e.get("max_pages", 1)
        if not isinstance(pages, int) or isinstance(pages, bool) or pages < 1:
            raise ConfigError(f"{where}: max_pages 必须是 ≥1 的整数，实际是 {pages!r}")
        # 选择器必须能解析 —— 早失败，别等到抓的时候才发现拼错
        for key in ("list", "title", "date", "link"):
            _parse_selector(e[key])
        out.append(
            NotifySource(
                name=e["name"].strip(),
                url=e["url"].strip(),
                list=e["list"].strip(),
                title=e["title"].strip(),
                date=e["date"].strip(),
                link=e["link"].strip(),
                max_pages=pages,
            )
        )

    names = [s.name for s in out]
    if len(set(names)) != len(names):
        raise ConfigError(f"{path}: 源名重复 —— {sorted(names)}")
    return tuple(out)


# ── 夹具（复现红线）──────────────────────────────────────────

FIXTURE_VERSION = 1


def save_fixture(paths: dict[str, list[dict[str, str]]], out: pathlib.Path) -> None:
    """把抓取结果落成夹具 JSON（**进仓库**，演示与复现从它读）。

    ⚠️ 存的是**解析后的条目**，不是原始 HTML：
      · 体积小得多（HTML 有几十 KB 的导航模板，条目只有几行）
      · **不存 HTML 就没有"站点长什么样"的痕迹**，也避免把页面里的
        无关内容（备案号、友情链接）带进仓库
      · 夹具的用途是"证明这条链路能跑通"，不是"存档那个网站"

    ⚠️ 带 `version` 与 `sources` 元信息：夹具格式变了要能识别出来
    （读到一个不认识的版本应当**响亮**，而不是静默当成空）。
    """
    payload = {
        "version": FIXTURE_VERSION,
        "note": "抓取快照（解析后）。演示与复现从此读，不联网。",
        "pages": paths,
    }
    atomic_write_text(out, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def load_fixture(path: pathlib.Path) -> dict[str, list[dict[str, str]]]:
    """读夹具。不存在 ⇒ 空 dict（回落到实时抓取）。"""
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"夹具 {path} 读不出来：{exc}") from exc
    version = data.get("version")
    if version != FIXTURE_VERSION:
        raise ConfigError(
            f"夹具 {path} 的版本是 {version!r}，本程序认的是 {FIXTURE_VERSION}。\n"
            "  （版本的用途正是让这种不匹配**响亮**，而不是被当成空夹具静默忽略。）"
        )
    pages = data.get("pages", {})
    if not isinstance(pages, dict):
        raise ConfigError(f"夹具 {path}: `pages` 必须是一张表")
    return pages
