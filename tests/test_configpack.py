"""配置包 —— M6 T7（spec §4.2 ① 的导入导出）。"""

from __future__ import annotations

import pathlib

import pytest

from vigil import categories, config, configpack, overrides


@pytest.fixture
def roots(tmp_path):
    """两个**互不相干**的数据根，用来验「另一个目录能导入并生效」（出口判据 4）。

    ⚠️ 每个根下都有自己的 `config/` 与 `data/`，并且**都不碰真仓库**。
    """
    class Roots:
        def __init__(self, base: pathlib.Path):
            self.base = base
            self.config_dir = base / "config"
            self.config_dir.mkdir(parents=True)
            self.qq = base / "qqdb"
            self.qq.mkdir()
            self.groups = self.config_dir / "groups.toml"
            self.cats = self.config_dir / "categories.toml"
            self.persons = self.config_dir / "persons.toml"

        def make_config(self, groups):
            return config.Config(
                source_dir=self.qq,
                output_db=self.base / "data" / "vigil.db",
                groups=tuple(groups),
            )

    a = Roots(tmp_path / "A")
    b = Roots(tmp_path / "B")
    return a, b


def _seed(root):
    config.save_config(
        root.make_config([
            config.Group(id=111, name="甲群", tier="high"),
            config.Group(id=222, name="乙群", enabled=False),
        ]),
        root.groups,
    )
    categories.save_categories(
        (categories.Category(slug="notice", label="通知", desc="正式通知", icon="📋"),),
        root.cats,
    )
    config.save_persons(
        (config.Person(uin=10001, label="张老师"),), root.persons
    )


def _open_overlay():
    """可写的 overlay 连接（表先备好——`connect()` 自己不建表）。"""
    overrides.ensure_schema()
    return overrides.connect()


def test_export_then_import_into_another_root(roots):
    """⭐ **出口判据 4 的机械形式**。"""
    a, b = roots
    _seed(a)

    pack_path = a.base / "vigil-config.toml"
    configpack.write_pack(
        pack_path,
        config=config.load_config(a.groups),
        cats=categories.load_categories(a.cats),
        persons=config.load_persons(a.persons),
        aliases=(("life_old", "life"),),
    )

    parsed = configpack.parse(pack_path.read_text(encoding="utf-8"), pack_path)
    report = configpack.import_pack(
        parsed,
        keep_settings=False,
        paths=configpack.Paths(
            groups=b.groups, categories=b.cats, persons=b.persons
        ),
    )
    assert report.groups == 2
    assert report.categories == 1
    assert report.persons == 1
    assert report.aliases == 1

    # ⭐ "生效" = 用**新根**重新读一遍，拿到的就是包里的东西
    assert [g.id for g in config.load_config(b.groups).groups] == [111, 222]
    assert [c.slug for c in categories.load_categories(b.cats)] == ["notice"]
    assert [p.uin for p in config.load_persons(b.persons)] == [10001]


def test_keep_settings_leaves_the_local_paths_alone(roots):
    """⭐ `--keep-settings` 不是可选糖：`qq_db_dir` 是**机器相关**的。

    ⚠️ 把别人的包原样导入，`load_config` 会报「QQ 数据库目录不存在」——
    响亮，但对用户毫无帮助（他想要的只是那几个群和类目）。
    """
    a, b = roots
    _seed(a)
    # B 根上先有一份自己的配置
    config.save_config(b.make_config([config.Group(id=999, name="本机群")]), b.groups)
    local_qq = config.load_config(b.groups).source_dir

    pack_path = a.base / "pack.toml"
    configpack.write_pack(
        pack_path, config=config.load_config(a.groups),
        cats=categories.load_categories(a.cats),
        persons=config.load_persons(a.persons), aliases=(),
    )
    parsed = configpack.parse(pack_path.read_text(encoding="utf-8"), pack_path)
    report = configpack.import_pack(
        parsed, keep_settings=True,
        paths=configpack.Paths(groups=b.groups, categories=b.cats, persons=b.persons),
    )
    assert report.settings_skipped is True
    assert config.load_config(b.groups).source_dir == local_qq, "本机路径不许被覆盖"
    assert [g.id for g in config.load_config(b.groups).groups] == [111, 222], (
        "群清单仍然来自包——keep-settings 只保留 [settings]"
    )


def test_the_pack_never_contains_a_secret(roots, monkeypatch):
    """⭐ **密钥永远不进配置文件**（spec §3.2）——本条检查的是**产物**。

    ⚠️ 判据是「产物里没有那些**值**」，而不是「代码里没写」。做法是把
    两个密钥塞进环境变量，再导出一次，断言它们的字面量不出现在包里。
    只 grep 字段名（`api_key`）是不够的：一个拼错的键名照样会漏。
    """
    a, _ = roots
    _seed(a)
    secret_db = "SUPER-SECRET-DB-KEY-1234567890"
    secret_llm = "sk-SUPER-SECRET-LLM-KEY"
    monkeypatch.setenv("VIGIL_DB_KEY", secret_db)
    monkeypatch.setenv("SILICONFLOW_API_KEY", secret_llm)

    text = configpack.build(
        config=config.load_config(a.groups),
        cats=categories.load_categories(a.cats),
        persons=config.load_persons(a.persons), aliases=(),
    )
    assert secret_db not in text
    assert secret_llm not in text
    assert "key" not in text.lower() or "密钥" in text, (
        "包里出现了 key 字样——先确认它不是任何一个密钥的值"
    )


def test_the_pack_has_exactly_the_six_expected_top_level_keys(roots):
    """⭐ 上面那条「包里没有密钥」**本身没有牙**（它今天不可能红：`build` 从不读环境）。

    ⇒ 有牙的形式是钉住**产物的顶层键集合**：多出一个 `[secrets]` / `[llm]`
    这类段（或某一段被写成了嵌套的子表）都会立刻红。这也正是本项目
    「一个说多了的理由比没有理由更糟」那条判据的机械面——
    一条永远为真的断言看起来像守卫，实际是装饰（勘误 E7 的形态 1）。

    ⚠️ 变异反证：在 `build` 里加一行 `lines += ["[secrets]", 'key = "x"', ""]`
    ⇒ 本条**必红**（顶层多出 `secrets`）。

    ⚠️ `tomllib` 判据是**顶层键**：`[settings]` 若被写成 `[groups.settings]`
    这类穿过数组的显式路径，顶层键会少一个 `settings` ⇒ 同样会红。
    """
    import tomllib

    a, _ = roots
    _seed(a)
    text = configpack.build(
        config=config.load_config(a.groups),
        cats=categories.load_categories(a.cats),
        persons=config.load_persons(a.persons),
        aliases=(("life_old", "life"),),
    )
    raw = tomllib.loads(text)
    assert set(raw) == {
        "schema", "settings", "groups", "categories", "persons", "kind_aliases"
    }, f"配置包的顶层键有意外的东西：{sorted(raw)}"


def test_a_pack_from_a_newer_schema_is_refused(roots):
    """版本闸：形状变了就必须**拒绝**，不许静默错读。"""
    a, _ = roots
    _seed(a)
    text = configpack.build(
        config=config.load_config(a.groups),
        cats=categories.load_categories(a.cats),
        persons=config.load_persons(a.persons), aliases=(),
    )
    bumped = text.replace(
        f"schema = {configpack.PACK_SCHEMA}", f"schema = {configpack.PACK_SCHEMA + 1}"
    )
    assert bumped != text, "包头部必须有一个可识别的 schema 行"
    with pytest.raises(config.ConfigError, match="版本"):
        configpack.parse(bumped, pathlib.Path("x.toml"))


def test_import_is_all_or_nothing(roots):
    """⭐ **导入是全有或全无**：校验不过 ⇒ 一个文件都不许被写。

    ⚠️ 不这么做的后果是**最坏的一种**：类目写进去了、群没写进去，
    而用户看到的是「导入失败」——他会以为什么什么都没发生，实际配置已经半新半旧。
    校验（`validate_config_raw` / `validate_categories`）在**任何写入之前**
    全部跑完，这一条才成立。
    """
    a, b = roots
    _seed(a)
    # 一份**类目合法、群非法**（零个群）的包
    bad = configpack.build(
        config=config.Config(
            source_dir=a.qq, output_db=a.base / "data" / "vigil.db", groups=()
        ),
        cats=categories.load_categories(a.cats),
        persons=config.load_persons(a.persons), aliases=(),
    )
    with pytest.raises(config.ConfigError):
        configpack.parse(bad, pathlib.Path("bad.toml"))
    assert not b.cats.exists(), "解析失败时 b 根上不许有任何文件"
    assert not b.groups.exists()


def test_parse_refuses_a_malformed_external_pack_with_configerror(roots):
    """⭐ 形状错的外部输入必须是 `ConfigError`，**不是 `AttributeError`**。

    ⚠️ 这条钉的是 C6（T2 的 deferred，T7 之前收）：`configpack.parse` 消费
    `validate_config_raw`，而它接的是**用户拷来拷去的文件**。一份
    `groups = [1, 2]` 的包在修前会抛 `AttributeError` ⇒ 端点回 **500**、
    排障方向完全错（去看服务端，而真相是包写错了）。

    ⚠️ 变异反证：把 `config.validate_config_raw` 里那句
    `isinstance(entry, dict)` 的守卫删掉 ⇒ 本条**必红**（抛的是 `AttributeError`）。
    """
    a, _ = roots
    _seed(a)
    # ⚠️ `groups = [111, 222]` **必须写在 `[settings]` 之前**：TOML 里落在
    # `[settings]` 之后的裸键是 `settings.groups`（顶层根本没有 `groups`），
    # 那样探的是**另一条**分支（「段为空（根本没写）」），C6 的守卫压根不在路径上
    # ——那正是 E7 的形态 5「输入到不了被测分支」。
    broken = (
        "schema = 1\n"
        "groups = [111, 222]\n"
        "\n"
        "[settings]\n"
        f'source_dir = "{a.qq.as_posix()}"\n'
        f'output_db = "{(a.base / "data" / "vigil.db").as_posix()}"\n'
        "\n"
        "[[categories]]\n"
        'slug = "notice"\n'
        'label = "通知"\n'
        'desc = "正式通知"\n'
    )
    with pytest.raises(config.ConfigError, match="键值表"):
        configpack.parse(broken, pathlib.Path("broken.toml"))


def test_import_prunes_an_alias_that_shadows_a_live_slug(roots):
    """⭐ 导入必须维持 T5 的别名不变量：表里**不许有 `old_kind ∈ 现存 slug` 的行**。

    场景是真实会发生的（不是构造出来的）：本机把 `life` 改名成 `life_v2`
    ⇒ 表里有 `life→life_v2`；然后导入一份**把 `life` 带回来**的包。
    不剪的话，`store._EFFECTIVE_KIND` 里那个无条件的
    `LEFT JOIN ov.kind_alias ka2 ON ka2.old_kind = ost.kind` 会把界面上
    手工选的**合法**类目 `life` 顶成 `life_v2`——正是 `prune_kind_aliases`
    docstring 里那个实测反例（spec §4.2 ④ 要禁的状态）。

    ⚠️ 标量断言（`"life" not in aliases`）**不够**：真正的不变量是
    「表里没有 old_kind ∈ 现存 slug 的行」，下面按它判。

    ⚠️ 变异反证：把 `import_pack` 里那次 `prune_kind_aliases` 调用删掉
    ⇒ 本条**必红**（`life→life_v2` 留在表里）。
    """
    a, b = roots
    _seed(a)
    # 本机（B）先欠着一条指向 life_v2 的别名
    ov = _open_overlay()
    try:
        overrides.set_kind_alias(ov, old_kind="life", new_kind="life_v2")
    finally:
        ov.close()

    pack = configpack.parse(
        configpack.build(
            config=config.load_config(a.groups),
            cats=(
                categories.Category(slug="life", label="生活", desc="生活"),
                categories.Category(slug="notice", label="通知", desc="正式通知"),
            ),
            persons=(),
            aliases=(("life", "life_v2"),),
        ),
        pathlib.Path("p.toml"),
    )
    report = configpack.import_pack(
        pack, keep_settings=False,
        paths=configpack.Paths(groups=b.groups, categories=b.cats, persons=b.persons),
    )
    assert report.aliases == 1, "包里的那条别名确实写进去了（剪枝发生在写入之后）"

    live = {c.slug for c in categories.load_categories(b.cats)}
    assert "life" in live, "包的类目确实生效了（否则下面的断言会因为别的原因通过）"
    ov = _open_overlay()
    try:
        aliases = overrides.kind_aliases(ov)
    finally:
        ov.close()
    assert not (set(aliases) & live), (
        f"别名表里有 old_kind ∈ 现存 slug 的行：{sorted(set(aliases) & live)}——"
        "`ka2` 会把用户手工选的合法类目顶掉"
    )


@pytest.mark.parametrize("shape", ["missing", "corrupt"])
def test_keep_settings_says_so_when_the_local_settings_cannot_be_kept(roots, shape):
    """⭐ FIX 1（controller 裁定）：`--keep-settings` **保不住**本机设置时**必须出声**。

    ⚠️ 被裁定掉的写法是"静默采用包里的 `[settings]`"：用户勾了「保留本机路径」，
    拿到的却是**别台机器的 `qq_db_dir`**，而**全程零提示**——他会在下一次
    `vigil export` 才撞上「QQ 数据库目录不存在」，而且**看不出为什么**。
    （本项目的立场：宁可响亮，不许静默退化；"只说自己有资格说的话"。）

    ⚠️ **但也不许改成"拒绝导入"**（那是另一条更容易走错的路）：包里的
    群/类目/人物仍然是用户想要的。**要的是把话说出来，不是把事拦住**——
    所以下面同时断言「它确实写了包里的 settings」（行为不变）。

    ⚠️ 两种形状一起参数化，因为有**两句**不同的原因必须都能说出来：
      · `missing` —— 第一次导入，本机还没有那份文件；
      · `corrupt` —— 手改笔误（`[groups]` 单层方括号是极常见的一种）。
    只测其中一种的话，另一种可以退化成"没有理由的一句话"而不被发现。

    ⚠️ 变异反证：把 `import_pack` 那一支里的 `on_progress(...)` 删掉 ⇒ 本条**必红**。
    """
    a, b = roots
    _seed(a)
    assert not b.groups.exists(), "B 根上**故意**没有 groups.toml（shape=missing）"
    if shape == "corrupt":
        # 单层方括号：一份"语法合法、形状错"的本机配置（真机上极常见的笔误）
        b.groups.write_text(
            f'[settings]\nsource_dir = "{b.qq.as_posix()}"\n'
            f'output_db = "{(b.base / "data" / "vigil.db").as_posix()}"\n\n'
            '[groups]\nid = 999\nname = "本机群"\n',
            encoding="utf-8",
        )

    pack_path = a.base / "pack.toml"
    configpack.write_pack(
        pack_path, config=config.load_config(a.groups),
        cats=categories.load_categories(a.cats),
        persons=config.load_persons(a.persons), aliases=(),
    )
    parsed = configpack.parse(pack_path.read_text(encoding="utf-8"), pack_path)

    lines: list[str] = []
    report = configpack.import_pack(
        parsed, keep_settings=True,
        paths=configpack.Paths(groups=b.groups, categories=b.cats, persons=b.persons),
        on_progress=lines.append,
    )

    assert report.settings_skipped is False, (
        "⭐ I4：`settings_skipped` 只有在**真的跳过**了本机 [settings] 时才为 True。"
        "这一支明明把包里的（外机的）settings 写进去了，报 True 是**一句假话**"
        "——用户会以为「保留本机路径」生效了。"
    )
    assert report.warnings, (
        "那句话必须**同时**进 `ImportReport`：CLI 靠 on_progress 看见它，"
        "而 API 一侧没有 stdout，只能靠这个字段回到响应体"
    )
    text = "\n".join(lines)
    assert "保留" in text or "没能" in text, f"一句话都不说：{text!r}"
    assert a.qq.as_posix() in text or str(a.qq) in text, (
        f"必须**点名**它到底采用了哪个 qq_db_dir（那是导出那台机器的）——实际：{text!r}"
    )
    assert "手工" in text or "修好" in text, (
        f"措辞要能让人行动（怎么才能用上本机路径）——实际：{text!r}"
    )
    # ⚠️ **摘要行也不许说假话**（Fix loop 2 / FIX 2）：它从前用的是「用户勾了框」
    # （`if keep_settings`）而不是「真的跳过了」（`report.settings_skipped`），
    # 于是这一支的摘要会跟着打「（保留本机 [settings]）」——而它**恰恰没保留**。
    # 那句话还会作为 `notes` 回到 API 响应里给前端显示。
    assert "（保留本机 [settings]）" not in lines[-1], (
        f"摘要行在说谎：本机设置没保住，却打了「保留本机 [settings]」——{lines[-1]!r}"
    )

    # 行为不变：本机那份读不出来 ⇒ 只能整份用包里的（**不是**拒绝导入）
    assert config.load_config(b.groups).source_dir == a.qq
    assert [g.id for g in config.load_config(b.groups).groups] == [111, 222]


# ── Fix loop 1：I1 / I2 —— parse 的对外契约（**任何问题都抛 ConfigError**）──
#
# ⚠️ 这几条钉的是同一件事的三个入口：`parse` 接的是**用户拷来拷去的文件**，
# 所以它的每一种"看不懂"都必须以 `ConfigError` 收场——那是 `api_import_pack`
# （→400）与 `cmd_config_import`（→一行 `[配置错误]`）唯一认得的东西。
# 这正是 C6 的形状（groups 已经 400 了，categories / 顶层键 / 别名环没跟上）。


def _pack_text(root, *, categories=(), aliases=(), top_extra="", persons_key="persons"):
    """拼一份**手写**的包文本（不走 `build`——本组要造的是 build 造不出的坏形状）。"""
    lines = ["schema = 1"]
    if top_extra:
        lines.append(top_extra)
    lines += [
        "",
        "[settings]",
        f'source_dir = "{root.qq.as_posix()}"',
        f'output_db = "{(root.base / "data" / "vigil.db").as_posix()}"',
        "",
        "[[groups]]",
        "id = 111",
        'name = "甲群"',
        "enabled = true",
        'tier = "normal"',
        "",
    ]
    for slug, label, desc in categories:
        lines += [
            "[[categories]]", f'slug = "{slug}"', f'label = "{label}"', f'desc = "{desc}"', ""
        ]
    if persons_key is not None:
        lines += [f"[[{persons_key}]]", "uin = 10001", 'label = "甲"', ""]
    for old, new in aliases:
        lines += ["[[kind_aliases]]", f'old_kind = "{old}"', f'new_kind = "{new}"', ""]
    return "\n".join(lines)


def test_parse_wraps_a_category_problem_in_configerror(roots):
    """⭐ I1：坏 slug 的包 ⇒ `ConfigError`（**不是** `CategoryError`）。

    ⚠️ 不转的后果（reviewer 实测）：`CategoryError` 不是 `ConfigError` 的子类，
    而两个调用方**只捕 `ConfigError`** ⇒ API **500**、CLI **裸 traceback**。
    这与 C6 是**同一个缺陷的另一个入口**：`groups` 已经修成 400 了，
    `categories` 没跟上——而 `parse` 自己的 docstring 写着「任何问题都抛 ConfigError」。

    ⚠️ 变异反证：把 `parse` 里那句 `except CategoryError` 的转换删掉 ⇒ 本条**必红**
    （抛出来的会是 `CategoryError`）。
    """
    a, _ = roots
    broken = _pack_text(a, categories=(("Notice", "通知", "大写 slug 不合法"),))
    with pytest.raises(config.ConfigError, match="类目"):
        configpack.parse(broken, pathlib.Path("cats.toml"))


def test_parse_wraps_a_bad_alias_cycle_in_configerror(roots):
    """⭐ I1 同族：`[[kind_aliases]]` **成环**的包必须在 `parse` 就被挡下。

    ⚠️ 不挡的后果：`import_pack` 走到 `set_kind_alias` 时抛 `OverlayError`
    ⇒ API 500 / CLI 裸 traceback。**而环是可静态检测的**——不需要真的去写一次表
    （写一次还更糟：那会先把半张表写进去）。

    ⚠️ 变异反证：把 `parse` 里那段环检测删掉 ⇒ 本条**必红**
    （parse 会放行，而下面的 `import_pack` 抛 `OverlayError`）。
    """
    a, _ = roots
    cyclic = _pack_text(
        a,
        categories=(("life", "生活", "生活"),),
        aliases=(("a_kind", "b_kind"), ("b_kind", "a_kind")),
    )
    with pytest.raises(config.ConfigError, match="环"):
        configpack.parse(cyclic, pathlib.Path("cyclic.toml"))
    # 它不是"写的时候才炸"：这一句之后**没有**任何 overlay 写入发生
    ov = _open_overlay()
    try:
        assert overrides.kind_aliases(ov) == {}
    finally:
        ov.close()


def test_parse_rejects_an_unknown_top_level_key(roots):
    """⭐ I2：`[[person]]`（少个 s）⇒ **响亮**，不许静默读成 0 人。

    ⚠️ 实测的伤害链（reviewer 2026-09-18）：`parse` 只认 `raw.get("persons", [])`
    ⇒ 键名拼错时读成**空元组** ⇒ `import_pack` 把本机的 `persons.toml`
    **整体覆盖成 0 人**（`save_persons` 是整体替换写），而**全程零报错**。
    这与 M5 那条 `_check_persons_shape`（`config.py`）是**同一个形态**，
    而那份文件里早就立过这条规矩并写了完整论证——照它的写法与理由做。

    ⚠️ 变异反证：把 `parse` 里那段顶层键校验删掉 ⇒ 本条**必红**
    （parse 返回 `persons=()` 而不报错）。
    """
    a, _ = roots
    typo = _pack_text(
        a, categories=(("life", "生活", "生活"),), persons_key="person"
    )
    with pytest.raises(config.ConfigError, match="person"):
        configpack.parse(typo, pathlib.Path("typo.toml"))


def test_parse_rejects_a_single_bracket_section(roots):
    """⭐ I2 的另一半：`[persons]`（**单层**方括号）必须与 `[[persons]]` 分得开。

    ⚠️ 与 `config._check_persons_shape` 同形：只差一层括号，解析结果一个是 dict、
    一个是 list；不挡的话 `for entry in …` 会去迭代 dict 的**键**（字符串），
    下一行以一个看不懂的异常收场。

    ⚠️ 但**段不在**仍然合法（"这份包不带人物"是 `build` 对空名单的正常产物，
    见 `test_export_then_import_into_another_root` 之外的几个用例）——本条的阳性
    对照就在 `test_import_prunes_an_alias_that_shadows_a_live_slug`（`persons=()`）。

    ⚠️ **本条买到的是"话说得对"，不是"400 vs 500"**（Fix loop 2 / FIX 3，
    reviewer 指出）：去掉 `_array_section` 那道形状检查，逐条那句
    `isinstance(entry, dict)` 照样抛 `ConfigError` ⇒ 端点**仍然是 400**，
    只是文案变成一句指向错误方向的"每个 [[persons]] 段都必须是键值表"。
    ⇒ 所以**这条判据的红落在 message 正则上**（上面 `match="数组表"`），
    而"端点真的是 400"由
    `test_api.py::test_api_import_pack_returns_400_for_a_single_bracket_section` 钉住。
    """
    a, _ = roots
    # ⚠️ 类目**必须合法**：`validate_categories` 在 persons 之前跑，喂空类目的话
    # 这条包会死在「[[categories]] 段为空」上——**红得对但不是这条断言红**
    # （勘误 E7 的形态 5：输入到不了被测分支）。
    single = _pack_text(a, categories=(("life", "生活", "生活"),)).replace(
        "[[persons]]\n", "[persons]\n", 1
    )
    assert "[persons]" in single
    with pytest.raises(config.ConfigError, match="数组表"):
        configpack.parse(single, pathlib.Path("single.toml"))


# ── Fix loop 2 / FIX 1：跨包别名冲突（Important-a）──────────────────────
#
# ⚠️ 背景（reviewer 实测，推翻了我上一轮的论证）：我说过「成环已静态挡下，
# 剩下的 OverlayError 只可能来自本机 overlay 故障」——**那是错的**。
# 本机那条别名恰恰是**上一次导入**写进去的 ⇒ **两份各自 parse 通过的正常包，
# 先后导入就撞车**：API 500、CLI 裸 traceback，而且逐条 `set_kind_alias`
# 各自 commit ⇒ 失败前写的行**留在表里**（「全有或全无」被破）。


def _alias_table():
    ov = _open_overlay()
    try:
        return overrides.kind_aliases(ov)
    finally:
        ov.close()


def _two_valid_packs_that_conflict(roots):
    """造两份**各自合法**的包：第一份建 `life_old→notice`，第二份要 `notice→life_old`。

    ⚠️ 别名两端要用**包里真实存在的 slug**（`notice`）当靶子：`new_kind` 指向一个
    不存在的类目，那条别名会被导入第 ③ 步的 `prune_kind_aliases` **当场剪掉**
    （"指向不存在的目标"），于是第二份包撞不上冲突——用例会以**假绿**收场
    （第一版就是这么写的：实测 `assert {} == {'a_kind': 'b_kind'}`）。
    """
    a, b = roots
    _seed(a)
    cfg, cats, people = (
        config.load_config(a.groups),
        categories.load_categories(a.cats),
        config.load_persons(a.persons),
    )
    first = configpack.parse(
        configpack.build(
            config=cfg, cats=cats, persons=people, aliases=(("life_old", "notice"),)
        ),
        pathlib.Path("first.toml"),
    )
    # ⚠️ 第二份包里**故意有两条**：第一条（`z_kind→notice`）**是合法的**，
    # 第二条才冲突。这正是 reviewer 实测出「半张表」的形状——逐条
    # `set_kind_alias` 各自 commit 时，第一条已经落库、第二条才炸。
    second = configpack.parse(
        configpack.build(
            config=cfg, cats=cats, persons=people,
            aliases=(("z_kind", "notice"), ("notice", "life_old")),
        ),
        pathlib.Path("second.toml"),
    )
    paths = configpack.Paths(groups=b.groups, categories=b.cats, persons=b.persons)
    return first, second, paths


def test_a_second_pack_conflicting_with_the_first_is_refused(roots):
    """⭐ FIX 1a/1b：第二份包必须**响亮拒绝**，且**一个字节都不写**。

    判据三条一起（缺一条就是一个空守卫）：
      · 抛的是 `ConfigError`（⇒ CLI 一行报错、API 400）；
      · 消息里**两边的映射都印出来**（用户才能自己判断怎么办——只说"冲突了"
        等于把排查丢回给他）；
      · **别名表与三份配置文件都没变**（这是"全有或全无"的机械形式）。

    ⚠️ 变异反证：把 `import_pack` 的别名写入打回「逐条 `set_kind_alias`、
    不先做合并校验」⇒ 本条**必红**（`OverlayError` 冒出来 + 表里留下 `{'b_kind': 'a_kind'}`）。
    """
    first, second, paths = _two_valid_packs_that_conflict(roots)

    configpack.import_pack(first, keep_settings=False, paths=paths)
    after_first = _alias_table()
    assert after_first == {"life_old": "notice"}, (
        f"第一份包应当正常落库（实测拿到 {after_first}）"
    )
    snap = {p: p.read_text(encoding="utf-8") for p in (paths.groups, paths.categories, paths.persons)}

    with pytest.raises(config.ConfigError, match="冲突") as excinfo:
        configpack.import_pack(second, keep_settings=False, paths=paths)

    message = str(excinfo.value)
    assert "life_old" in message and "notice" in message, (
        f"两边的映射都要印出来（本机现有 / 包里要写）：{message!r}"
    )
    assert _alias_table() == after_first, (
        "冲突时别名表不许被改动——逐条 set_kind_alias 各自 commit 会留下半张表"
    )
    for p, text in snap.items():
        assert p.read_text(encoding="utf-8") == text, f"{p} 被改了：破了「全有或全无」"


def test_merge_aliases_matches_the_write_path(roots):
    """⭐ 防漂移：纯函数算出的**最终表**，必须与逐条 `set_kind_alias` 落库的结果一致。

    ⚠️ 为什么需要它：FIX 1b 把别名写入改成「**静态算出最终表** + 一次整表写」，
    于是「成环判定 + 压平语义」在**两处**各有一份实现。两份一旦漂移，症状是
    "静态检查说没问题、写进去却炸"（或反过来），而那种缺陷只在**罕见形状**下现形
    （正是本项目最贵的那类）。这条用例把两条路径并排跑，用同一组输入逐字节比。

    ⚠️ 环的形状两边都要**抛**：一条抛 `ConfigError`（导入路径的契约）、一条抛
    `OverlayError`（T5 的写路径契约）——不是同一个异常，但必须**同时发生**。
    """
    cases = [
        ({}, [("x", "y")]),
        ({"x": "y"}, [("z", "x")]),                 # 链式：z → x → y ⇒ z→y
        ({"x": "y"}, [("y", "z")]),                 # 传播：旧行 x→y 也要走到 z
        ({"a": "b", "x": "y"}, [("c", "a"), ("d", "x")]),
        ({"a": "b"}, [("b", "c"), ("c", "d")]),     # 两条串起来
    ]
    for local, wanted in cases:
        ov = _open_overlay()
        try:
            overrides.replace_kind_aliases(ov, local)
            for old, new in wanted:
                overrides.set_kind_alias(ov, old_kind=old, new_kind=new)
            from_db = overrides.kind_aliases(ov)
        finally:
            ov.close()
        assert configpack._merge_aliases(local, wanted) == from_db, (
            f"静态合并与写路径不一致：local={local} wanted={wanted}"
        )

    # 环：两边都抛
    ov = _open_overlay()
    try:
        overrides.replace_kind_aliases(ov, {"a": "b"})
        with pytest.raises(overrides.OverlayError):
            overrides.set_kind_alias(ov, old_kind="b", new_kind="a")
    finally:
        ov.close()
    with pytest.raises(config.ConfigError, match="冲突"):
        configpack._merge_aliases({"a": "b"}, [("b", "a")])


def test_the_summary_line_claims_kept_settings_when_they_were_kept(roots):
    """⭐ FIX 2 的**阳性对照**：真的保住了，摘要就该说保住（否则上面那条一删就全绿）。

    ⚠️ 没有这一条的话，"摘要行永远不打那句话"也能让上面那条绿——而它意味着
    用户**永远**看不到"本机路径保住了"的确认，两种状态在输出上不可区分。
    """
    a, b = roots
    _seed(a)
    config.save_config(b.make_config([config.Group(id=999, name="本机群")]), b.groups)

    pack = configpack.parse(
        configpack.build(
            config=config.load_config(a.groups),
            cats=categories.load_categories(a.cats),
            persons=config.load_persons(a.persons),
            aliases=(),
        ),
        pathlib.Path("pack.toml"),
    )
    lines: list[str] = []
    report = configpack.import_pack(
        pack, keep_settings=True,
        paths=configpack.Paths(groups=b.groups, categories=b.cats, persons=b.persons),
        on_progress=lines.append,
    )
    assert report.settings_skipped is True
    assert "（保留本机 [settings]）" in lines[-1], lines[-1]


def test_a_conflicting_import_never_leaves_a_partial_alias_table(roots):
    """⭐ FIX 1b：冲突时**别名表不许留下写了一半的行**（reviewer 实测的「半张表」）。

    ⚠️ 与上一条**刻意分工**：上一条钉**异常类型与文案**（400 的契约），
    这一条只钉**表的最终状态** ⇒ 所以它**故意不挑异常类**（`except Exception`）——
    否则"抛了别的异常"会让它提前离场，而这半条判据就**永远执行不到**
    （勘误 E7 的形态 2）。

    ⚠️ 场景里第二份包有**两条别名**：`z_kind→notice`（合法）与
    `notice→life_old`（与本机冲突）。逐条 `set_kind_alias` 各自 commit 的写法下，
    第一条**已经落库**、第二条才炸 ⇒ 表里留下 `{'life_old': 'notice', 'z_kind': 'notice'}`
    ——那正是 reviewer 实测的 `{'b':'a','x':'y'}` 形状。

    ⚠️ 变异反证：把别名写入打回逐条 `set_kind_alias` ⇒ 本条**必红**
    （表里多出 `z_kind`）。
    """
    first, second, paths = _two_valid_packs_that_conflict(roots)
    configpack.import_pack(first, keep_settings=False, paths=paths)
    after_first = _alias_table()

    try:
        configpack.import_pack(second, keep_settings=False, paths=paths)
    except Exception:  # noqa: BLE001 —— 见 docstring：这一条只管表的最终状态
        pass

    assert _alias_table() == after_first, (
        f"表被写了一半：{_alias_table()} —— 与 before 的 {after_first} 不同"
    )
