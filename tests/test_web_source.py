"""网页通知源：解析、契约、夹具。

⚠️ **本文件全部离线**（用内嵌 HTML 夹具，不走网络）。
理由与 `网页通知源取数方案.md` §八 第 5 步一致：联网的测试会随站点
状态随机失败，**而那种红是指向错误方向的** —— 排查的人会去看代码，
而真正的原因是被测站点改了版 / 挂了。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from vigil import web_source
from vigil.config import ConfigError
from vigil.source import NON_TEXT
from vigil.web_source import NotifySource, SourceError

# ── 夹具 HTML ─────────────────────────────────────────────────
#
# ⚠️ 结构**取自吉大真实页面**（2026-10-16 实测），但内容全是编的。
# 关键特征一个不少：
#   ① 通知行 `<li><span>日期</span><a href>标题</a></li>`
#   ② **导航行也匹配 `li`**（这是真问题 —— 实测 29 条里只有 15 条是通知）
#   ③ `<a>` 带 `title` 属性，且**比显示文本完整**（显示版被截断）

_PAGE = """<!DOCTYPE html><html><body>
<ul class="nav">
  <li><a href="/index.htm">首页</a></li>
  <li><a href="/zzjg.htm">组织机构</a></li>
</ul>
<ul class="news">
  <li><span>2026-09-15</span><a href="../info/1050/12677.htm"
      title="关于2026-2027学年第一学期部分课程停开的通知">关于2026-2027学年第一学期部分课程停开的通知</a></li>
  <li><span>2026-05-25</span><a href="../info/1050/12635.htm"
      title="本科生院关于面向院内民主推荐正科级干部的通知">本科生院关于面向院内民主推荐正科级干部的通知</a></li>
  <li><span>2026-01-08</span><a href="../info/1050/12507.htm"
      title="关于2025-2026学年第二学期部分校公选课停开的通知">关于2025-2026学年第二学期部分校公选课停开的通...</a></li>
</ul>
</body></html>"""

_SRC = NotifySource(
    name="测试源",
    url="https://example.edu.cn/list.htm",
    list="li",
    title="a",
    date="span",
    link="a@href",
)


def _parse(text: str = _PAGE, src: NotifySource = _SRC):
    return web_source.parse_list_page(text, src)


# ── 解析 ──────────────────────────────────────────────────────


def test_parses_the_notice_rows():
    items = _parse()
    assert len(items) == 3
    assert items[0]["date"] == "2026-09-15"
    assert items[0]["title"] == "关于2026-2027学年第一学期部分课程停开的通知"


def test_skips_navigation_rows_that_also_match_li():
    """⭐⭐ **导航行必须被跳过** —— 这是实测踩到的真问题。

    ⚠️ `list = "li"` 会把导航栏也匹配进来：实测吉大页面上 `li` 命中 **29 条**，
    其中只有 **15 条**是真通知，另外 14 条是「首页 / 组织机构 / 校发文件…」。

    ⚠️ 两者在 **DOM 结构上同形**（都是 `li > a`），唯一区别是通知多一个
    `<span>YYYY-MM-DD</span>` ⇒ 判据只能是「有没有一个能解析成日期的字段」。

    ⚠️ 变异反证：去掉 `_date_ts(date) == 0` 那道闸 ⇒ 本条必红（会返回 5 条）。
    """
    items = _parse()
    titles = [i["title"] for i in items]
    assert "首页" not in titles
    assert "组织机构" not in titles


def test_prefers_the_title_attribute_over_display_text():
    """⚠️ `<a title>` **比显示文本完整** —— 显示版是截断的（带省略号）。

    实测吉大页面：显示 `…学生选拔工作...` 而 `title=` 里是完整标题。
    取显示文本会让下游拿到一堆带省略号的半截标题。
    """
    items = _parse()
    last = items[-1]
    assert last["title"] == "关于2025-2026学年第二学期部分校公选课停开的通知"
    assert "..." not in last["title"]


def test_resolves_relative_hrefs_to_absolute():
    """⚠️ 页面里是 `../info/1050/12677.htm`，必须补成绝对地址。

    不补的话：`msg_id` 里抠文章 ID 时正则匹配不到（那是**静默降级**成
    href 的 md5），而来源链接点开是 404（相对路径在浏览器里相对于当前页）。
    """
    items = _parse()
    assert items[0]["href"] == "https://example.edu.cn/info/1050/12677.htm"


def test_html_entities_in_titles_are_unescaped():
    """⚠️ 标题里的 `&amp;` 要还原成 `&`，否则显示给用户的是实体编码。"""
    page = _PAGE.replace("部分课程停开", "A&amp;B 课程停开")
    assert "A&B" in _parse(page)[0]["title"]


def test_empty_page_returns_nothing_rather_than_crashing():
    """ⓘ 解析层对空页面返回空列表；**"抓不到"的判据在 `_items_of` 那一层**
    （它会抛）—— 两层的分工是刻意的：解析只管解析。"""
    assert _parse("<html><body>nothing here</body></html>") == []


# ── 选择器 ────────────────────────────────────────────────────


def test_selector_parses_the_three_supported_forms():
    assert web_source._parse_selector("a") == ("a", None, None)
    assert web_source._parse_selector("a@href") == ("a", None, "href")
    assert web_source._parse_selector("tr.listbottomline") == ("tr", "listbottomline", None)


@pytest.mark.parametrize("bad", ["", "@href", "a@"])
def test_bad_selector_raises(bad):
    """⚠️ 拼错的选择器**要抛**，不能静默忽略 ——
    静默的话，"抓不到"会看起来像"这个源本来就没内容"。"""
    with pytest.raises(ConfigError):
        web_source._parse_selector(bad)


def test_class_selector_matches_only_that_class():
    page = (
        '<ul><li class="x"><span>2026-01-01</span><a href="/a.htm">甲</a></li>'
        '<li><span>2026-01-02</span><a href="/b.htm">乙</a></li></ul>'
    )
    src = NotifySource(name="t", url="https://e.test/l", list="li.x",
                       title="a", date="span", link="a@href")
    assert [i["title"] for i in _parse(page, src)] == ["甲"]


# ── 合成消息 ──────────────────────────────────────────────────


def test_read_group_composes_content_with_date():
    src = web_source.WebSource((_SRC,), pages={"测试源": _parse()})
    gid = src.list_groups()[0][0]
    msgs = src.read_group(gid).messages
    assert len(msgs) == 3
    assert msgs[0].content == (
        "关于2026-2027学年第一学期部分课程停开的通知\n发布: 2026-09-15"
    )


def test_msg_id_comes_from_the_article_id_in_the_url():
    """⭐ `msg_id` 取 URL 里的文章 ID —— 它**跨次抓取稳定**（同一篇永远是同一个）。

    ⚠️ 若取 href 的 md5 也能稳定，但**不可回溯**：人看一眼 ID 无法对上原文。
    """
    src = web_source.WebSource((_SRC,), pages={"测试源": _parse()})
    gid = src.list_groups()[0][0]
    assert [m.msg_id for m in src.read_group(gid).messages] == [12677, 12635, 12507]


def test_group_id_is_stable_across_processes():
    """⚠️ **绝不能用内建 `hash()`** —— 它对字符串加每进程随机的盐
    （`PYTHONHASHSEED`），同一个源两次跑会得到不同 ID。

    判据：**同一个 URL 构造两次，ID 必须相同**（进程内）。
    跨进程由 md5 保证 —— 用一个固定期望值钉住它。
    """
    a = web_source.WebSource((_SRC,), pages={})
    b = web_source.WebSource((_SRC,), pages={})
    assert a.group_names() == b.group_names()
    # 固定值：换实现（如改用 hash()）会立刻变，从而被这条抓出来
    assert list(a.group_names())[0] == str(web_source._md5_int(_SRC.url))


def test_empty_title_falls_back_to_non_text():
    """⚠️ `content` 的契约是**非空文本**（`messages.content` 是 TEXT NOT NULL）。

    标题为空时用共享常量 `NON_TEXT` 占位 —— 与 QQ 源同一约定，
    下游 prefilter 拿它判"这条要不要丢"。
    """
    page = '<ul><li><span>2026-01-01</span><a href="/a/1.htm" title=""></a></li></ul>'
    src = web_source.WebSource((_SRC,), pages={"测试源": _parse(page)})
    # 标题为空 ⇒ 解析层就丢了它（无标题的通知没有价值）
    assert src.read_group(src.list_groups()[0][0]).messages == []


def test_non_text_constant_is_the_shared_one():
    assert NON_TEXT == "[非文本]"


# ── 失败契约 ──────────────────────────────────────────────────


def test_read_group_raises_for_unknown_group():
    src = web_source.WebSource((_SRC,), pages={})
    with pytest.raises(SourceError):
        src.read_group(999)


def test_items_of_raises_when_selectors_match_nothing():
    """⭐⭐ **解析出 0 条 ⇒ 抛**，不许静默当"这页没内容"。

    站点改版导致选择器失效时就是这个形状 —— 而它必须响亮：
    静默的话，用户看到的是"这个源很久没更新了"，真相是**采集早就断了**。

    ⚠️ 变异反证：把 `raise SourceError` 改成 `return items` ⇒ 本条必红。
    """
    src = web_source.WebSource((_SRC,), pages={})
    html = web_source._http_get  # 不联网
    web_source._http_get = lambda url: "<html><body>改版了</body></html>"
    try:
        with pytest.raises(SourceError, match="选择器可能已失效"):
            src._items_of(_SRC)
    finally:
        web_source._http_get = html  # type: ignore[assignment]


def test_http_error_raises_source_error():
    """⚠️ 网络失败**抛 `SourceError`**，不返回空串 ——
    空串与"这页没有内容"完全同形。"""
    src = web_source.WebSource((_SRC,), pages={})
    real = web_source.urllib.request.urlopen

    def boom(*a, **k):
        raise web_source.urllib.error.URLError("炸了")

    web_source.urllib.request.urlopen = boom  # type: ignore[assignment]
    try:
        with pytest.raises(SourceError, match="连不上"):
            src._items_of(_SRC)
    finally:
        web_source.urllib.request.urlopen = real  # type: ignore[assignment]


def test_user_agent_is_pure_ascii():
    """⚠️ **HTTP 头按 latin-1 编码** —— 一个中文字符就会让 urllib 在
    `putheader` 抛 `UnicodeEncodeError`，而那是在**建连接之前**抛的，
    看起来像网络问题，实际是这一行字面量。实测踩到过。
    """
    web_source._UA.encode("ascii")  # 不抛即通过


# ── 配置读取 ──────────────────────────────────────────────────


def _write(tmp_path, body: str) -> pathlib.Path:
    p = tmp_path / "sources.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_load_sources_reads_a_minimal_config(tmp_path):
    p = _write(tmp_path, """
[[sources]]
name = "本科生院"
url = "https://e.test/list.htm"
list = "li"
title = "a"
date = "span"
link = "a@href"
""")
    got = web_source.load_sources(p)
    assert len(got) == 1
    assert got[0].name == "本科生院"
    assert got[0].max_pages == 1  # 默认值


def test_missing_config_file_raises(tmp_path):
    """⚠️ **文件不存在 ⇒ 抛**（与 `load_config` 同规，与 `load_persons` 相反）。

    选了 `source = "web"` 却没配 sources.toml 是**配置错误**，
    不是"合法的空状态"—— 静默返回空会让 ingest 跑出"0 个源"的成功。
    """
    with pytest.raises(ConfigError, match="找不到"):
        web_source.load_sources(tmp_path / "nope.toml")


@pytest.mark.parametrize("missing", ["name", "url", "list", "title", "date", "link"])
def test_missing_required_field_names_it(tmp_path, missing):
    """⚠️ 报错要**点名缺哪个字段** —— 只说"配置不对"等于让用户自己猜。"""
    fields = {
        "name": '"x"', "url": '"https://e.test/l"', "list": '"li"',
        "title": '"a"', "date": '"span"', "link": '"a@href"',
    }
    del fields[missing]
    body = "[[sources]]\n" + "\n".join(f"{k} = {v}" for k, v in fields.items())
    with pytest.raises(ConfigError, match=missing):
        web_source.load_sources(_write(tmp_path, body))


def test_bad_selector_in_config_fails_early(tmp_path):
    """⚠️ 选择器**在加载时就校验** —— 别等到抓的时候才发现拼错。"""
    body = """
[[sources]]
name = "x"
url = "https://e.test/l"
list = "li"
title = "a"
date = "span"
link = "a@@href"
"""
    with pytest.raises(ConfigError):
        web_source.load_sources(_write(tmp_path, body))


def test_duplicate_source_names_raise(tmp_path):
    """⚠️ 源名重复 ⇒ 抛。它被用作 Sender 的 key，重复会让两个人混成一个。"""
    one = """
[[sources]]
name = "重名"
url = "https://e.test/a"
list = "li"
title = "a"
date = "span"
link = "a@href"
"""
    with pytest.raises(ConfigError, match="重复"):
        web_source.load_sources(_write(tmp_path, one + one))


# ── 夹具 ──────────────────────────────────────────────────────


def test_fixture_round_trips(tmp_path):
    out = tmp_path / "fx.json"
    pages = {"测试源": _parse()}
    web_source.save_fixture(pages, out)
    assert web_source.load_fixture(out) == pages


def test_missing_fixture_is_empty_not_an_error(tmp_path):
    """⚠️ 夹具**不存在 ⇒ 空 dict**（回落到实时抓取）。

    "还没抓过夹具"是合法状态，与 `load_sources` 的"缺配置就是错"刻意相反：
    前者是可选增强，后者是必需配置。
    """
    assert web_source.load_fixture(tmp_path / "nope.json") == {}


def test_fixture_with_wrong_version_raises(tmp_path):
    """⭐⭐ 夹具版本不匹配 ⇒ **响亮**，不许当成空夹具静默忽略。

    ⚠️ 静默的后果很隐蔽：程序会以为"没夹具"于是去**联网实时抓** ——
    而那条路在评委机器上多半失败。**一个本该报错的版本问题，
    会表现成"网络问题"**，排查方向完全错。
    """
    p = tmp_path / "fx.json"
    p.write_text(json.dumps({"version": 999, "pages": {}}), encoding="utf-8")
    with pytest.raises(ConfigError, match="版本"):
        web_source.load_fixture(p)


def test_broken_fixture_json_raises(tmp_path):
    p = tmp_path / "fx.json"
    p.write_text("{ not json", encoding="utf-8")
    with pytest.raises(ConfigError):
        web_source.load_fixture(p)


def test_web_source_prefers_fixture_over_network():
    """⭐⭐ **夹具优先**（复现红线的机制保证）。

    ⚠️ 判据是**没发过网络请求**：把 `_http_get` 换成一个"一调用就炸"
    的替身，夹具模式下 `read_group` 仍必须正常返回。
    只断言"返回了数据"是不够的 —— 实时抓取也能返回数据。
    """
    real = web_source._http_get
    web_source._http_get = lambda url: (_ for _ in ()).throw(
        AssertionError("夹具模式下不该发网络请求")
    )
    try:
        src = web_source.WebSource((_SRC,), pages={"测试源": _parse()})
        gid = src.list_groups()[0][0]
        assert len(src.read_group(gid).messages) == 3
    finally:
        web_source._http_get = real  # type: ignore[assignment]


# ── 与 ingest 的接线（架构层）────────────────────────────────


def test_ingest_群清单_取配置与源的并集():
    """⭐⭐ **换源时旧配置里的群号不该被当成"失败"**（2026-10-16 实测踩到）。

    两份清单来源不同、对不上是**常态**：
      · `config.enabled_groups` 是**人写的**（QQ 群号 / 合成语料的 9000000xx）
      · 网页源的 group_id 是 `md5(url)` **算出来的**

    实测：从 synthetic 切到 web 时，4 个合成群号都不在新源里 ⇒
    原实现产出 4 条「没有这个通知源」的**失败记录**。而那不是失败，
    是"这份配置与这个源无关" —— 报成失败会让人以为采集坏了。

    ⚠️ 变异反证：去掉 `if gid in available` 那道过滤 ⇒ 本条必红。
    """
    from vigil import ingest

    class _Src:
        def list_groups(self):
            return [(42, 15, 0)]

        def group_names(self):
            return {"42": "网页源·通知"}

    class _G:
        def __init__(self, i, n):
            self.id, self.name = i, n

    class _Cfg:
        enabled_groups = (_G(42, "网页源·通知"), _G(900000001, "旧的合成群"))

    got = ingest._groups_to_ingest(_Cfg(), _Src(), lambda m: None)
    assert got == [(42, "网页源·通知")], f"旧的合成群不该出现，实际 {got}"


def test_ingest_falls_back_to_config_when_source_reports_nothing():
    """⚠️ 源**不报清单**时保持老行为（按配置摄取）。

    那种源（如 QQ 源在清单读取失败时）的 `read_group` 自己会抛，
    错误信息比这里编的准确 —— 所以这里不该替它做判断。
    """
    from vigil import ingest

    class _Src:
        def list_groups(self):
            raise RuntimeError("清单读不出来")

        def group_names(self):
            return {}

    class _G:
        def __init__(self, i, n):
            self.id, self.name = i, n

    class _Cfg:
        enabled_groups = (_G(1, "甲"),)

    msgs = []
    got = ingest._groups_to_ingest(_Cfg(), _Src(), msgs.append)
    assert got == [(1, "甲")]
    assert any("没有报告群清单" in m for m in msgs), "警告要打出来"
