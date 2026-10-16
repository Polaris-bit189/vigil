"""类目配置的测试。重点是校验失败路径——配置错要快速失败。"""

from __future__ import annotations

import pytest

from vigil import categories
from vigil.categories import Category, CategoryError


def _write(tmp_path, body: str):
    p = tmp_path / "categories.toml"
    p.write_text(body, encoding="utf-8")
    return p


def test_loads_real_project_config(shipped_categories_toml):
    """真实配置文件必须能加载，且正是用户裁定的七类。

    ⚠️ 传入 `shipped_categories_toml` 是**刻意的**（M6 T5 Fix loop 2）：
    conftest 从 FIX 2 起把 `DEFAULT_CATEGORIES` 挪到了一个**自带内容**的 tmp 文件，
    而**这一条**断言的是**仓库资产**（出厂那份必须合法、且就是那七类），
    不是"产品读得到什么"。不传的话它会在断言夹具，而名字还叫 real——
    那正是「名字与它实际测的东西不是一回事」。
    """
    cats = categories.load_categories(shipped_categories_toml)
    assert [c.slug for c in cats] == [
        "notice",
        "academic",
        "activity",
        "life",
        "secondhand",
        "lostfound",
        "job",
    ]
    assert all(c.label and c.desc for c in cats)


def test_rejects_duplicate_slug(tmp_path):
    p = _write(
        tmp_path,
        """
[[categories]]
slug = "notice"
label = "通知"
desc = "正式通知"

[[categories]]
slug = "notice"
label = "重复"
desc = "重复了"
""",
    )
    with pytest.raises(CategoryError, match="重复"):
        categories.load_categories(p)


@pytest.mark.parametrize(
    "bad", ["Notice", "NOTICE", "通知", "2notice", "no-tice", "no.tice", "", "notice "]
)
def test_rejects_non_lowercase_ascii_slug(tmp_path, bad):
    """slug 会进 items.kind 列、提示词、前端筛选键——必须是小写英文标识符。

    `str.isidentifier()` 不够：它放行 `Notice` 与 `通知`（W1 审查实测）。
    """
    p = _write(
        tmp_path,
        f"""
[[categories]]
slug = "{bad}"
label = "x"
desc = "y"
""",
    )
    with pytest.raises(CategoryError):
        categories.load_categories(p)


def test_accepts_underscore_and_digits_after_first_letter(tmp_path):
    p = _write(
        tmp_path,
        """
[[categories]]
slug = "part_time_job2"
label = "x"
desc = "y"
""",
    )
    assert categories.load_categories(p)[0].slug == "part_time_job2"


def test_rejects_discard_as_slug(tmp_path):
    """discard 是保留字（表示「丢弃」），不能当类目名。"""
    p = _write(
        tmp_path,
        """
[[categories]]
slug = "discard"
label = "丢弃"
desc = "假装是个类目"
""",
    )
    with pytest.raises(CategoryError, match="保留字"):
        categories.load_categories(p)


def test_rejects_missing_desc(tmp_path):
    """desc 不是可选的——提示词靠它告诉模型这一类收什么。"""
    p = _write(
        tmp_path,
        """
[[categories]]
slug = "notice"
label = "通知"
""",
    )
    with pytest.raises(CategoryError, match="desc"):
        categories.load_categories(p)


def test_rejects_slug_with_trailing_newline(tmp_path):
    """`^[a-z][a-z0-9_]*$` 的 `$` 允许结尾一个换行，所以 `"notice\\n"` 会被旧正则放行。

    ⚠️ 这里**必须用 TOML 多行字符串**把真换行喂进去：写成单行基本串里的
    `\\n` 转义会被 tomllib 判为非法字符（单行串不允许裸换行），
    测试就会绕过正则、在 TOML 解析层失败——**假绿**。
    所以本用例与下面的 `test_rejects_malformed_toml` 必须分开，
    各自只测一件事。
    """
    p = _write(
        tmp_path,
        '[[categories]]\nslug = """notice\n"""\nlabel = "x"\ndesc = "y"\n',
    )
    with pytest.raises(CategoryError, match="slug 必须匹配"):
        categories.load_categories(p)


@pytest.mark.parametrize(
    "body",
    [
        "[categories]\nslug = 'notice'\nlabel = 'x'\ndesc = 'y'\n",  # 单层方括号（常见笔误）
        "categories = 'oops'\n",  # 字符串
        "categories = [1, 2]\n",  # 是数组，但元素不是表
    ],
)
def test_rejects_wrong_shape_with_category_error(tmp_path, body):
    """配置形状写错也要走 CategoryError，不能甩 AttributeError。

    `[categories]` 与 `[[categories]]` 在 TOML 里只差一层方括号——最常见的笔误。
    不挡的话会去迭代 dict 的键（字符串），下一行 `.get()` 抛 AttributeError，
    同样绕过 cli 只捕 CategoryError 的契约。
    """
    p = _write(tmp_path, body)
    with pytest.raises(CategoryError, match="categories"):
        categories.load_categories(p)


def test_rejects_malformed_toml(tmp_path):
    """TOML 语法错也必须走 CategoryError，而不是甩原始 traceback。

    `tomllib.TOMLDecodeError` 继承自 ValueError 而非 RuntimeError——
    不包的话会绕过 cli 的错误处理契约（cli 只捕 CategoryError）。
    这条是 fix 审查实测抓出来的。
    """
    p = _write(tmp_path, '[[categories]]\nslug = "unclosed\n')
    with pytest.raises(CategoryError, match="TOML"):
        categories.load_categories(p)


def test_rejects_empty_config(tmp_path):
    p = _write(tmp_path, "[settings]\n")
    with pytest.raises(CategoryError, match="空"):
        categories.load_categories(p)


def test_prompt_block_lists_every_slug():
    cats = categories.load_categories()
    block = categories.prompt_block(cats)
    for c in cats:
        assert c.slug in block
        assert c.desc in block
    # 一行一个类目
    assert len(block.splitlines()) == len(cats)


def test_category_is_frozen():
    c = Category(slug="a", label="b", desc="c")
    with pytest.raises(Exception):
        c.slug = "x"  # type: ignore[misc]


# ── M6 T2：写侧 ──────────────────────────────────────────────────────


def test_save_categories_roundtrips(tmp_path):
    p = tmp_path / "categories.toml"
    cats = (
        categories.Category(slug="notice", label="通知", desc="正式通知", icon="📋"),
        categories.Category(slug="life", label="生活", desc="吃住行"),
    )
    categories.save_categories(cats, p)
    assert categories.load_categories(p) == cats


def test_save_categories_rejects_an_invalid_slug(tmp_path):
    """⭐ 写路径必须复用**读路径的同一个校验函数**。

    ⚠️ 变异反证：把 `save_categories` 里那句 `validate_categories(...)` 删掉
    ⇒ 本条**必红**（会静默写出一个 `load_categories` 拒绝的文件）。

    ⚠️ 后果为什么特别重：`categories.py` 是 **refine 与 api 的硬依赖**，
    一份校验不过的 `categories.toml` 会让**整个程序起不来**——
    而它是原子写的，**没有任何自动回滚**。
    """
    p = tmp_path / "categories.toml"
    with pytest.raises(categories.CategoryError):
        categories.save_categories(
            [categories.Category(slug="Notice", label="大写", desc="slug 不合法")], p
        )
    assert not p.exists(), "校验不过就不许落盘"


def test_save_categories_rejects_discard_as_slug(tmp_path):
    """`discard` 是保留字（`refine_runs` 用它记「这条消息被丢了」），写侧也要挡。"""
    p = tmp_path / "categories.toml"
    with pytest.raises(categories.CategoryError):
        categories.save_categories(
            [categories.Category(slug="discard", label="丢弃", desc="保留字")], p
        )


def test_save_categories_refuses_zero(tmp_path):
    """⚠️ 零个类目会被 `load_categories` 判为配置错误 ⇒ 拒绝写出（与 `save_config` 同理）。"""
    p = tmp_path / "categories.toml"
    with pytest.raises(categories.CategoryError, match="零个类目"):
        categories.save_categories((), p)
    assert not p.exists()


def test_save_categories_escapes_a_quote_in_desc(tmp_path):
    """`desc` 里带引号/换行也必须写出可解析的 TOML（`toml_str` 的全函数性）。"""
    p = tmp_path / "categories.toml"
    cats = (categories.Category(slug="x", label='带"引号"', desc="第一行\n第二行"),)
    categories.save_categories(cats, p)
    assert categories.load_categories(p) == cats
