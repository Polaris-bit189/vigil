"""类目配置：从 config/categories.toml 读，校验后供预筛与提示词共用。

为什么不硬编码成常量：类目是产品决策，用户已经改过一次
（把「二手」「失物招领」从「生活」里拆出来）。写死在代码里，
每次调整都要改代码、改测试、重发版。
"""

from __future__ import annotations

import pathlib
import re
import tomllib
from collections.abc import Sequence
from dataclasses import dataclass

from .config import REPO_ROOT, atomic_write_text, toml_str

DEFAULT_CATEGORIES = REPO_ROOT / "config" / "categories.toml"

# 保留字：被丢弃的消息在 refine_runs 里记这个值，所以不能用作类目名
DISCARD = "discard"

# slug 会被写进 items.kind 列、渲染进提示词、当作 CLI 与前端的筛选键，
# 所以必须是小写英文标识符。
# ⚠️ 只查 str.isidentifier() 不够——它放行 'Notice' 和 '通知'，
# 而两者都会在下游造成麻烦（W1 审查实测）。用正则收紧。
# ⚠️ 用 \Z 而不是 $：Python 里 $ 允许结尾有一个换行，
# 于是 "notice\n" 会被放行（fix 审查实测）。
_SLUG_RE = re.compile(r"^[a-z][a-z0-9_]*\Z")


class CategoryError(RuntimeError):
    """类目配置有问题——快速失败。"""


@dataclass(frozen=True)
class Category:
    slug: str
    label: str
    desc: str
    icon: str = ""


def load_categories(path: pathlib.Path | None = None) -> tuple[Category, ...]:
    path = path or DEFAULT_CATEGORIES
    if not path.is_file():
        raise CategoryError(f"找不到类目配置: {path}")

    try:
        with open(path, "rb") as f:
            raw = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        # 必须包成 CategoryError：TOMLDecodeError 继承自 ValueError 而不是
        # RuntimeError，不包的话会绕过 cli 的错误处理契约，用户看到的是
        # 原始 traceback 而不是一行可读的配置错误。
        raise CategoryError(f"{path}: TOML 语法错误——{exc}") from exc

    return validate_categories(raw.get("categories"), path)


def validate_categories(entries: object, path: pathlib.Path) -> tuple[Category, ...]:
    """把**已解析**的 `categories` 段校验成 `Category` 元组。

    ⚠️ **读路径与写路径共用这一个函数**（M6 T2 抽出来的）。写路径若另写一套
    校验，两份会漂移——而漂移的后果在这里尤其重：`save_categories` 能写出
    `load_categories` 拒绝的文件 ⇒ 一份校验不过的 `categories.toml` 永久落盘
    ⇒ **整个程序起不来**（本模块是 refine 与 api 的硬依赖）。

    ⚠️ 参数是 `entries` **而不是 `raw`**：段序/键名的问题在上游（`raw.get`），
    本函数只管这一段里面的形状。把两件事混在一个签名里，会让写路径
    不得不伪造一个 `raw` 字典才能调用它。
    """
    # ↓↓↓ 以下为原 `load_categories` 从 `if entries is None:`
    #     到 `return tuple(out)` 的**逐字搬移**，一行不改 ↓↓↓
    # ⚠️ 三个分支不能合并。`[categories]`（单层方括号）与 `[[categories]]` 在 TOML 里
    # 只差一层括号，是极常见的笔误，但解析结果一个是 dict、一个是 list。
    # 不挡的话 `for entry in entries` 会去迭代 dict 的键（字符串），
    # 下一行 `entry.get(...)` 抛 AttributeError——**同样绕过 cli 只捕
    # CategoryError 的契约**，用户看到的是原始 traceback。
    # ⚠️ 顺序也不能反：先判 None 再判类型。反过来的话「根本没写」会被
    # 归到「类型不对」，把「段为空」的专用报错变成不可达代码。
    if entries is None:
        raise CategoryError(f"{path}: [[categories]] 段为空（根本没写）")
    if not isinstance(entries, list):
        raise CategoryError(
            f"{path}: 需要 [[categories]] 数组段（注意是双层方括号），"
            f"实际是 {type(entries).__name__}"
        )
    if not entries:
        raise CategoryError(f"{path}: [[categories]] 段为空（写了个空数组）")

    out: list[Category] = []
    seen: set[str] = set()

    for entry in entries:
        if not isinstance(entry, dict):
            raise CategoryError(
                f"{path}: 每个 [[categories]] 段都必须是键值表，"
                f"实际是 {type(entry).__name__}"
            )
        slug = str(entry.get("slug", ""))
        if not _SLUG_RE.match(slug):
            raise CategoryError(
                f"{path}: slug 必须匹配 {_SLUG_RE.pattern}，实际是 {slug!r}"
            )
        if slug in seen:
            raise CategoryError(f"{path}: slug 重复: {slug}")
        if slug == DISCARD:
            raise CategoryError(f"{path}: {DISCARD!r} 是保留字，不能作类目")
        seen.add(slug)

        desc = str(entry.get("desc", "")).strip()
        if not desc:
            raise CategoryError(f"{path}: 类目 {slug} 缺少 desc（提示词要用它）")

        out.append(
            Category(
                slug=slug,
                label=str(entry.get("label", slug)),
                desc=desc,
                icon=str(entry.get("icon", "")),
            )
        )

    return tuple(out)


def as_dicts(cats: Sequence[Category]) -> list[dict[str, str]]:
    """`Category` 序列 → `tomllib` 形状的字典列表。

    ⚠️ 存在的理由是**字段名只能有一份**：`slug`/`label`/`desc`/`icon` 这四个
    名字同时出现在读侧（`validate_categories` 的取值）、写侧（`save_categories`
    的 f-string）与 API 侧（`_category_from_body`）。让 API 侧手搓第三个
    字典字面量，就是给「改了三个漏一个」留位置。

    ⚠️ 它**不负责校验**——喂给 `validate_categories` 才有校验。
    叫 `as_dicts` 而不是 `to_raw` 是刻意的：后者听起来像"已经验过了"。
    """
    return [
        {"slug": c.slug, "label": c.label, "desc": c.desc, "icon": c.icon} for c in cats
    ]


def prompt_block(cats: tuple[Category, ...]) -> str:
    """渲染成提示词里的类目表——改 categories.toml 即改提示词。"""
    width = max(len(c.slug) for c in cats)
    return "\n".join(f"{c.slug.ljust(width)}  {c.label}：{c.desc}" for c in cats)


_CATEGORIES_HEADER = """\
# VIGIL 信息类目
#
# ── 怎么改 ─────────────────────────────────────────────
#   desc 不是注释，它会被逐字渲染进 LLM 的提示词里。
#   想让模型把某类信息分对，就把判据说清楚——写得含糊会导致误分类。
#   改完不需要动代码，下次 vigil refine 即生效。
#
# ── 怎么加类目 ─────────────────────────────────────────
#   复制一段 [[categories]]，slug 用小写英文标识符（唯一、不能是 discard）。
#   加完要重跑 vigil refine --redo 才能对历史消息生效。
#
# ⚠️ **本文件由程序整体重写**（网页「配置」页保存时会覆盖它）：
#    手工改也行，但**手工加的注释会在下一次网页保存时消失**。
"""


def save_categories(cats: Sequence[Category], path: pathlib.Path | None = None) -> None:
    """整体替换写出类目配置（原子）。

    ⚠️ **在落盘之前**先自己解析、再喂给 `validate_categories`。顺序是承重的：
    反过来的话，一份不合法的配置已经原子落盘了才发现不对，
    **没有任何自动回滚**，而 `categories.toml` 是 refine 与 api 的硬依赖
    ⇒ 整个程序起不来。

    ⚠️ 校验的是**最终文本**（而不是内存里的 `Category`）：只有这样才能抓到
    序列化本身的缺陷（漏转义的引号），而不只是数据的问题。
    """
    target = path or DEFAULT_CATEGORIES

    if not cats:
        raise CategoryError(
            "拒绝写出零个类目：读回来会被判成「[[categories]] 段为空」的配置错误，"
            "而 refine 的提示词要用它渲染类目表 ⇒ 整个程序起不来。"
            "确实一个类目都不想要时，请手工编辑本文件并自行承担后果。"
        )

    lines = [_CATEGORIES_HEADER, ""]
    for c in cats:
        lines.append("[[categories]]")
        lines.append(f"slug = {toml_str(c.slug)}")
        lines.append(f"label = {toml_str(c.label)}")
        if c.icon:
            lines.append(f"icon = {toml_str(c.icon)}")
        lines.append(f"desc = {toml_str(c.desc)}")
        lines.append("")

    text = "\n".join(lines)
    try:
        raw = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise CategoryError(f"内部错误：为 {target} 生成的 TOML 解析不了——{exc}") from exc
    validate_categories(raw.get("categories"), target)

    atomic_write_text(target, text)
