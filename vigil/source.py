"""数据源抽象 —— 群消息从哪来。

**唯一的契约**：`Source` 的实现负责把「某个地方的消息」读成归一化的
``SourceMessage``，由 ``ingest`` 落成三张派生表（`messages` / `sender_names`
/ `msg_media`）。下游（`store` / `reader` / `digest` / `api`）只认那三张表，
**不认数据来自哪里** —— 这正是本模块存在的意义：换数据源不必改下游。

为什么让源返回**归一化**的 ``SourceMessage``、而不是像早年的 ``export``
那样返回原始行 tuple：

    QQ 源的行是 38 列纯数字列名，正文是 protobuf BLOB，图片要从 BLOB 里
    正则扫文件名。这三件知识**全是 QQ 专有的**。若让 ingest 里的代码去写
    `row[17]` / `text.readable(...)` / `media.refs(...)`，那套知识就从源
    实现漏进了编排层，换源时得连编排层一起改。

    归一化之后：**源专注"怎么取"，ingest 专注"怎么落"**。

代价是源实现多做一点转换工作 —— 但那本来就是它该做的。
"""

from __future__ import annotations

import pathlib
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from .config import Config, ConfigError

# 一条消息**没有可读正文**时的占位串。
#
# 为什么是共享常量而不是各源自定义：它是个**跨层契约** ——
#   · 源侧：QQ 源里那些只有图片/表情/撤回提示的消息没有文本
#   · 预筛层（prefilter）：拿它判"这条要不要丢"
# 三方各写一个字面量的话，改一处就会静默漏掉另一处。
#
# ⚠️ 早年它在 `vigil/text.py`（QQ 的 protobuf 解析模块）里。随源层重构
#    搬来这里 —— 它是**源的产出约定**，不是 QQ 专有物。
NON_TEXT = "[非文本]"


@dataclass(frozen=True)
class SourceMessage:
    """一条消息，已归一化。**与 QQ 的 38 列结构无关。**

    ``content`` 必须是**已抽成文本**的正文（不是 BLOB）—— 这是 normalize
    的主要产出，也是 `messages.content` 列的类型要求。
    """

    msg_id: int
    group_id: int
    ts: int
    sender_uid: str
    content: str
    # 这条消息引用的图片：[(md5, 扩展名)]。空列表 = 这条没图。
    media: tuple[tuple[str, str], ...] = ()


@dataclass(frozen=True)
class Sender:
    """一个人的三种称呼。群昵称优先，因为它才是群里认得出的那个名字。

    （原在 ``senders.py``，随数据源层重构搬来这里 —— 它是**跨源共享的形状**，
    不是 QQ 专有物：任何数据源都得回答"这个人怎么称呼"。）
    """

    group_nick: str = ""
    qq_nick: str = ""
    uin: int = 0
    in_group: bool = True

    @property
    def display_name(self) -> str:
        return self.group_nick or self.qq_nick or (str(self.uin) if self.uin else "未知")


@dataclass
class GroupRead:
    """读一个群的全部消息。

    ``skipped_ranges`` 非空 = **已上报的降级**（例如 QQ 库的坏页），
    调用方会把它记进 ``skipped_by_group``。它与 ``failed_groups`` 是
    两件事，别混：前者是"读到了大部分"，后者是"整群没读到"。
    """

    messages: list[SourceMessage]
    skipped_ranges: list[tuple[int, int]] = field(default_factory=list)

    @property
    def skipped_count(self) -> int:
        return sum(n for _, n in self.skipped_ranges)


@runtime_checkable
class Source(Protocol):
    """一个数据源。

    方法集刻意与 ingest 的四个取数动作一一对应 —— 少一个都落不出三张表。
    """

    def list_groups(self) -> list[tuple[int, int, int]]:
        """[(群号, 消息数, 最新时间戳)]，按消息数降序。

        用于刷新 Web 侧的群清单快照。**必须覆盖全部群**，不是只列已纳群 ——
        那个页面的用途正是"从里面挑一个还没纳的群加进来"。
        """
        ...

    def group_names(self) -> dict[str, str]:
        """{群号(字符串): 群名}。取不到就是空 dict —— 群名是锦上添花。"""
        ...

    def read_group(self, gid: int) -> GroupRead:
        """读一个群的全部消息。

        ⚠️ **读不到必须抛异常**，不许返回空的 ``GroupRead`` ——
        空返回值与"这个群本来就没消息"**完全同形**，调用方会把整群读不到
        记成 0 条成功，于是日报会以"这天很安静"的口气叙述一个整个群没读进来的
        日子。（这个坑在 QQ 源里被实测过一次，见 ingest 的失败契约测试。）
        """
        ...

    def sender_names(self) -> dict[tuple[int, str], Sender]:
        """{(群号, uid): Sender}。取不到返回空 dict。"""
        ...

    def media_index(self) -> dict[str, pathlib.Path]:
        """{md5: 本地文件路径}。决定哪些图片"本地可取"。"""
        ...


def open_source(name: str, config: Config) -> Source:
    """按名字取数据源。

    未知名字**快速失败**（ConfigError）而不是回退到某个默认值 ——
    静默回退会让一个拼错的配置跑出"看起来正常"的结果。
    """
    if name == "synthetic":
        from . import synth  # 延迟导入：合成源依赖较重，且不是每条路径都要

        return synth.SyntheticSource(synth.load_spec(synth.default_spec_path(config)))

    if name == "qq":
        # ⚠️ 这一支在**公开仓库里不存在** —— qqdb / media / text 三个模块
        # 是 QQ 专有物，不随开源版本分发（见 .gitignore 与 README 的"边界"节）。
        # 留这个分支是为了让私有部署能接回来：把三个模块放回 vigil/ 即可。
        try:
            from . import qq_source
        except ImportError as exc:  # pragma: no cover —— 公开仓库走不到
            raise ConfigError(
                "数据源 'qq' 需要 qq_source.py（QQ 专有，未随开源版本分发）。\n"
                "  演示请用 source = \"synthetic\"（默认）。"
            ) from exc
        return qq_source.QQSource(config)

    if name == "web":
        # 网页通知源：只读**公开发布**的页面（合规 + 可复现那条路）。
        # ⚠️ 配置缺失时**快速失败**（不是回退到合成源）——
        # 回退会让人以为"web 源跑起来了"，而其实读的是假数据。
        from . import web_source

        # ⚠️⚠️ **夹具优先**（复现红线）：`data/sources-fixture.json` 存在时
        # 就从它读，**一次网络请求都不发**。
        #
        # 这不是"缓存优化"，是**让评委能跑起来**：实时抓取依赖
        # 学校站点在线、没改版、且评委能访问 —— 三个条件缺一个复现就失败，
        # 而复现失败是取消资格级别（赛题第⑦条）。
        #
        # ⚠️ 夹具是**可选**的：没有它就走实时抓取（真实使用场景）。
        # 两条路产出同一种 `SourceMessage`，所以夹具能验证实时路径。
        return web_source.WebSource(
            web_source.load_sources(config.sources_path),
            pages=web_source.load_fixture(config.fixture_path) or None,
        )

    raise ConfigError(
        f"未知的数据源 {name!r}。可用：'synthetic'（演示）、'web'（公开通知页）、"
        "'qq'（需私有模块）。"
    )
