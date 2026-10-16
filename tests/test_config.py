"""配置层：persons.toml 的加载、校验与原子写。"""

import pathlib

import pytest

from vigil import config


def _write(tmp_path, text):
    p = tmp_path / "persons.toml"
    p.write_text(text, encoding="utf-8")
    return p


def test_load_persons_missing_file_is_empty(tmp_path):
    """没有这个文件是**合法状态**（一个人都没监视），不是错误。"""
    assert config.load_persons(tmp_path / "nope.toml") == ()


def test_load_persons_roundtrip(tmp_path):
    p = _write(tmp_path, '[[persons]]\nuin = 10002\nlabel = "示例用户"\n')
    got = config.load_persons(p)
    assert got == (config.Person(uin=10002, label="示例用户", note=""),)


def test_load_persons_rejects_zero_uin(tmp_path):
    """⚠️ uin=0 是匿名哨兵（实测真库 98 行），必须拒绝——否则"监视匿名者"是个无意义操作。"""
    p = _write(tmp_path, '[[persons]]\nuin = 0\nlabel = "匿名"\n')
    with pytest.raises(config.ConfigError, match="匿名"):
        config.load_persons(p)


def test_load_persons_rejects_duplicate_uin(tmp_path):
    p = _write(
        tmp_path,
        '[[persons]]\nuin = 1\nlabel = "甲"\n\n[[persons]]\nuin = 1\nlabel = "乙"\n',
    )
    with pytest.raises(config.ConfigError, match="重复"):
        config.load_persons(p)


def test_load_persons_rejects_empty_label(tmp_path):
    p = _write(tmp_path, '[[persons]]\nuin = 1\nlabel = ""\n')
    with pytest.raises(config.ConfigError, match="label"):
        config.load_persons(p)


def test_save_persons_is_roundtrippable(tmp_path):
    p = tmp_path / "persons.toml"
    people = (config.Person(uin=111, label="甲", note="备注"),)
    config.save_persons(people, p)
    assert config.load_persons(p) == people


def test_save_persons_replaces_not_appends(tmp_path):
    """⚠️ 保存是**整体替换**——追加语义会让"删掉一个人"永远做不到。"""
    p = tmp_path / "persons.toml"
    config.save_persons((config.Person(uin=1, label="甲"),), p)
    config.save_persons((config.Person(uin=2, label="乙"),), p)
    assert config.load_persons(p) == (config.Person(uin=2, label="乙"),)


def test_atomic_write_leaves_no_temp_file(tmp_path):
    p = tmp_path / "x.txt"
    config.atomic_write_text(p, "hello")
    assert p.read_text(encoding="utf-8") == "hello"
    assert [f.name for f in tmp_path.iterdir()] == ["x.txt"]


def test_atomic_write_failure_leaves_original_intact(tmp_path, monkeypatch):
    """⚠️ 写失败时**原文件必须一个字都没变**——这是原子写的全部意义。"""
    p = tmp_path / "x.txt"
    config.atomic_write_text(p, "原始内容")

    import os as _os

    def boom(*a, **k):
        raise OSError("模拟磁盘满")

    monkeypatch.setattr(_os, "replace", boom)
    with pytest.raises(OSError):
        config.atomic_write_text(p, "新内容")
    assert p.read_text(encoding="utf-8") == "原始内容"


def test_atomic_write_flushes_before_fsync(tmp_path, monkeypatch):
    """⚠️ fsync **不是**「原理上不可观测」——在 `os.fsync` 回调里读同目录的临时文件即可。

    两条变异实测（都是删一行，见 task-7-report.md「Fix loop 第 1 轮」）：
    - 删掉 `os.fsync` ⇒ 回调一次都不触发 ⇒ 本用例红（持久性保证整个消失）；
    - 删掉 `f.flush()` ⇒ fsync 落在**空文件**上 ⇒ 本用例红
      （这是**真 bug** 不是仪式：崩电窗口里重开出来的是一份空配置）。

    ⚠️ 断言的是「fsync 那一刻正文已经在文件里」，不是「某行代码存在」——
    顺序反了（先 fsync 再 flush）同样红。
    """
    import os as _os

    at_fsync = []
    real_fsync = _os.fsync

    def spy(fd):
        at_fsync.append(
            [(t.name, t.read_text(encoding="utf-8")) for t in sorted(tmp_path.glob("*.tmp"))]
        )
        real_fsync(fd)

    monkeypatch.setattr(_os, "fsync", spy)
    p = tmp_path / "fsynced.txt"
    config.atomic_write_text(p, "正文")

    bodies = [[body for _, body in snapshot] for snapshot in at_fsync]
    assert bodies == [["正文"]], (
        f"fsync 必须恰好调用一次、且那一刻正文已经在同目录临时文件里"
        f"（fsync 被删/flush 被删/顺序反了都会红）；实测={at_fsync!r}"
    )
    assert p.read_text(encoding="utf-8") == "正文"


def test_atomic_write_replaces_within_same_directory(tmp_path, monkeypatch):
    """⚠️ `os.replace` **不跨卷**（实测 C:→D: 报 `WinError 17`）⇒ 临时文件必须与目标同目录。

    ⚠️ 只测行为是**盲的**：本机 pytest 的 `tmp_path` 与系统 temp **同卷**，把临时文件
    放到系统 temp 也照样能替换成功。所以本用例直接断言 `os.replace` 的实参
    `src.parent == dst.parent` —— 换写法后**本机即红**。
    """
    import os as _os

    calls = []
    real_replace = _os.replace

    def spy(src, dst, *a, **k):
        calls.append((pathlib.Path(src), pathlib.Path(dst)))
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(_os, "replace", spy)
    p = tmp_path / "same_dir.txt"
    config.atomic_write_text(p, "x")

    assert len(calls) == 1, f"os.replace 应当恰好调用一次；实测={calls!r}"
    src, dst = calls[0]
    assert dst == p
    assert src.parent == dst.parent, f"临时文件必须与目标同目录（不跨卷）：src={src}"


def test_atomic_write_cleans_temp_on_failure(tmp_path, monkeypatch):
    """⚠️ 失败路径必须清掉临时文件——`config/` 是要进 git 的目录，`.tmp` 垃圾会一直躺着。"""
    import os as _os

    def boom(*a, **k):
        raise OSError("模拟磁盘满")

    monkeypatch.setattr(_os, "replace", boom)
    p = tmp_path / "boom.txt"
    with pytest.raises(OSError):
        config.atomic_write_text(p, "新内容")

    assert [f.name for f in tmp_path.iterdir()] == [], "失败路径留下了临时文件"


def test_toml_str_is_total(tmp_path):
    """⚠️ `toml_str` 必须是**全函数**：任何字符串进去，`save` 的产物都 `load` 得回来。

    实测坏字符集（原样落进引号里会让 tomllib 解析失败）：
    **U+0000–U+0008、U+000A–U+001F（`\\t` 除外）、U+007F**。
    `\\t` / `]` / `#` / `=` / `\"\"\"` / U+2028 / C1 / NBSP / BOM 实测 round-trip 一致，不在此列。
    """
    bad = "".join(chr(c) for c in range(0x00, 0x20)) + "\x7f"
    people = (config.Person(uin=1, label="甲" + bad + "乙", note="备" + bad),)
    p = tmp_path / "persons.toml"
    config.save_persons(people, p)
    assert config.load_persons(p) == people


# --- M5 终审修复轮：拼错的段落不许静默吞掉（Minor-4）----------------


def test_load_persons_rejects_a_misspelled_section_name(tmp_path):
    """⭐ M5 终审 Minor-4：`[[person]]`（少个 s）**必须响亮**，不许静默读成空名单。

    实测的伤害链：`raw.get("persons", [])` 键名拼错时返回 `()`；而
    `save_persons` 是**整体覆盖**写（「删掉一个人」只能这么做）⇒
    下一次在界面上「加一个人」时 load → append → save，
    **手工加的人无声消失**，全程零报错。

    而文件头**自己邀请手工编辑**（「或手工往下面追加一段 `[[persons]]`」），
    也就是说这条路径一定会被走到——与「越早暴露越好」的模块立场相反。

    变异「把 `load_persons` 里的 `_check_persons_shape(...)` 那行去掉」⇒ 本条红。
    """
    p = _write(tmp_path, '[[person]]\nuin = 10002\nlabel = "示例用户"\n')
    with pytest.raises(config.ConfigError, match="persons"):
        config.load_persons(p)
    # 反向：改正之后就正常（不许把整类文件都拒掉）
    p.write_text('[[persons]]\nuin = 10002\nlabel = "示例用户"\n', encoding="utf-8")
    assert config.load_persons(p) == (
        config.Person(uin=10002, label="示例用户", note=""),
    )


def test_load_persons_rejects_a_wrong_bracket_shape(tmp_path):
    """`[persons]`（一层方括号）也要响亮：那是一张**表**，不是一串人。

    不拦的话 `for entry in raw["persons"]` 会去迭代字典的**键**（字符串），
    后面 `entry.get("uin")` 以一个看不懂的 `AttributeError` 收场——
    而"看不懂的异常"正是"这个文件写错了"最不该有的表现。
    """
    p = _write(tmp_path, '[persons]\nuin = 10002\nlabel = "示例用户"\n')
    with pytest.raises(config.ConfigError, match="数组表"):
        config.load_persons(p)


def test_load_persons_rejects_an_entry_that_is_not_a_table(tmp_path):
    """⭐ 与 `groups = [1, 2]` **完全同形**：`persons = [1, 2]` 也必须是 `ConfigError`。

    ⚠️ 上一条挡的是「段不是数组」（`[persons]` ⇒ dict，`entry` 是**键**）。
    本条挡的是「**元素**不是表」（TOML 允许数组装标量 ⇒ `entry` 是那个标量）。
    两者落在同一行 `entry.get("uin")` 上：修前实测
    `AttributeError: 'int' object has no attribute 'get'`——**绕过本模块只捕
    `ConfigError` 的契约**（`cli` / `api`），用户看到裸 traceback。

    ⚠️ 为什么它和 C6 一起收：`config/persons.toml` 的文件头**自己邀请手工编辑**，
    而 `/api/persons` 就在这条读路径上（一次手误 ⇒ 整页 500）。

    ⚠️ 变异反证：把 `load_persons` 循环里那句 `isinstance(entry, dict)`
    的守卫删掉 ⇒ 本条**必红**（抛的是 `AttributeError`）。
    """
    p = _write(tmp_path, "persons = [1, 2]\n")
    with pytest.raises(config.ConfigError, match="键值表"):
        config.load_persons(p)


def test_load_persons_empty_file_is_still_legal(tmp_path):
    """阳性对照：**空文件**是合法的（一个人都没监视），不许被上面两条顺带拒掉。"""
    assert config.load_persons(_write(tmp_path, "")) == ()
    assert config.load_persons(_write(tmp_path, "# 只有注释\n")) == ()


# ── M6 T2：写侧序列化层 ──────────────────────────────────────────────


def test_save_config_roundtrips(tmp_path):
    """`save_config` 的产物必须被 `load_config` 原样读回来。

    ⚠️ 这条钉的是「**全函数**」那一半：任何 `Config` 进去，产物都读得回来。
    但它**钉不住段序**——见下一条。
    """
    p = tmp_path / "groups.toml"
    cfg = config.Config(
        source_dir=tmp_path / "qqdb",
        output_db=config.REPO_ROOT / "data" / "vigil.db",
        groups=(
            config.Group(id=111, name="甲群", enabled=True, tier="high"),
            config.Group(id=222, name="乙群", enabled=False),
        ),
    )
    (tmp_path / "qqdb").mkdir()
    config.save_config(cfg, p)
    back = config.load_config(p)
    assert back.groups == cfg.groups
    assert back.output_db == cfg.output_db


def test_save_config_writes_settings_before_groups(tmp_path):
    """⭐ `[settings]` 必须在 `[[groups]]` **之前**——**约定锁，不是不变量**。

    ⚠️ 如实标注（M6 T2 复审，计划勘误 E4）：这只是**输出形状的约定**，理由只有
    一条——**与现有手写文件的形状一致、便于人读与 diff**。它**不是**不变量：
    原稿说它防「`[table]` 掉到 `[[array_of_tables]]` 之后会被解析成最后一个
    数组元素下的子表」——**实测不成立**（tomllib 里 `[settings]` 是**根相对**
    路径，两种顺序都落在顶层，`validate_config_raw` 也分辨不出顺序）。

    ⚠️ **它没有变异反证，如实说。** 把 `save_config` 里的 settings 块挪到
    groups 之后当然会让下面那条断言变红（实测 `assert 28 < 0`），但那只是
    「断言正在断言它自己说的那件事」——**空转**，不证明任何**行为**上的不变量。
    要给它真牙齿，得上 golden 产物（固定一份期望输出、逐字节比），那是以后的事。

    ⚠️ 为什么不删（复审裁定）：删了之后**再没有任何东西钉输出形状**。

    ⚠️ 实施修正（M6 T2）：原稿用的是 `text.index("[[groups]]")`，而
    `_GROUPS_HEADER` 的**正文里**有一句「按下面格式加一段 `[[groups]]`」
    ⇒ 子串搜索命中的是**注释里那个**（实测 643 vs 168），段序完全正确也红。
    改成**行锚定**比较，钉的仍是段头本身的先后。
    """
    p = tmp_path / "groups.toml"
    (tmp_path / "qqdb").mkdir()
    cfg = config.Config(
        source_dir=tmp_path / "qqdb",
        output_db=config.REPO_ROOT / "data" / "vigil.db",
        groups=(config.Group(id=111, name="甲群"),),
    )
    config.save_config(cfg, p)
    text = p.read_text(encoding="utf-8")
    lines = text.splitlines()
    assert lines.index("[settings]") < lines.index("[[groups]]")
    # 再机械确认一次：解析出来的顶层键就是那两个
    import tomllib
    assert set(tomllib.loads(text)) == {"settings", "groups"}


def test_save_config_refuses_zero_groups(tmp_path):
    """⚠️ 拒绝写出零个群。

    不是洁癖：`load_config` 把「一个群都没配」当成配置错误（`[[]] 段为空`），
    所以写出一份零群的配置 = **把程序改成起不来**——而且下一次启动才现形，
    那时用户已经在网页上点过保存、拿到了「成功」。

    ⚠️ 这同时**否掉了一个看起来更自然的设计**：「删掉最后一个群」。
    要退掉最后一个群，正确动作是 `enabled = false`（保留配置、保留历史）。
    """
    p = tmp_path / "groups.toml"
    (tmp_path / "qqdb").mkdir()
    cfg = config.Config(
        source_dir=tmp_path / "qqdb", output_db=config.REPO_ROOT / "data" / "vigil.db",
        groups=(),
    )
    with pytest.raises(config.ConfigError, match="零个群"):
        config.save_config(cfg, p)
    assert not p.exists(), "拒绝之后不许留下文件"


def test_save_config_keeps_repo_relative_paths_relative(tmp_path):
    """仓库内的路径写**相对**、仓库外的写**绝对**（`toml_path` 的契约）。

    两个方向各有一个具体的坏法：
      · 全写绝对 ⇒ 配置搬到别的目录时仍指回原仓库（T8 的导入导出就废了）；
      · 全写相对 ⇒ `C:/Users/...` 的 QQ 库路径会被 `load_config` 拼成
        `REPO_ROOT / "C:/Users/..."`，一个不存在的路径。

    ⚠️ 实施修正（M6 T2）：原稿写的是 `source_dir=pathlib.Path("C:/somewhere/nt_db")`
    ——一个**不存在**的路径，而 `save_config` 落盘前要把最终文本喂给
    `validate_config_raw`，那里有「QQ 数据库目录不存在」这条检查
    ⇒ 原稿在本平台**必红**（实测 `ConfigError: QQ 数据库目录不存在: C:\\somewhere\\nt_db`）。
    换成一个真实存在、且仍在 `REPO_ROOT` **之外**的绝对路径（本平台的 pytest
    `tmp_path` 就是 `C:/Users/...`，与 docstring 里那个坏法同形）：契约不变。
    """
    p = tmp_path / "groups.toml"
    outside = tmp_path / "nt_db"
    outside.mkdir()
    cfg = config.Config(
        source_dir=outside,
        output_db=config.REPO_ROOT / "data" / "vigil.db",
        groups=(config.Group(id=111, name="甲群"),),
    )
    config.save_config(cfg, p)
    text = p.read_text(encoding="utf-8")
    assert 'output_db = "data/vigil.db"' in text
    assert f'source_dir = "{outside.as_posix()}"' in text


def test_save_persons_rejects_a_non_int_uin(tmp_path):
    """⭐ M5 遗留 #19 + #22：`uin` 必须是**真正的整数**，且**拒绝 bool**。

    ⚠️ 旧实现是裸 f-string `f"uin = {p.uin}"`，两个坏法：
      · `uin="1\\n[[persons]]\\nlabel=\\"注入\\""` ⇒ 写出**第二个段头**，
        整份文件结构被改写（已复现）；
      · `uin=True` ⇒ 写出 `uin = true`，读回来是 `uin = 1`（`bool` ⊂ `int`）。

    ⚠️ `Person` 是 frozen dataclass，**不强制类型** ⇒ 产品路径今天够不到它。
    但 T8 的批量导入会让它够到，所以必须在此之前收掉（终审原话）。

    ⚠️ 变异反证：把 `save_persons` 里那行换回 `f"uin = {p.uin}"`
    ⇒ 本条**必红**。
    """
    for bad in ("1\n[[persons]]\nlabel=\"注入\"", True, 1.5, "10001"):
        with pytest.raises(config.ConfigError, match="uin"):
            config.save_persons(
                [config.Person(uin=bad, label="甲")], tmp_path / "persons.toml"  # type: ignore[arg-type]
            )
    assert not (tmp_path / "persons.toml").exists()


def test_saved_persons_never_contain_a_uid_key(tmp_path):
    """M5 遗留 #20：`uid` **不落配置**——`config/*.toml` 里不许出现它。

    ⚠️ 为什么要有这条：`Person` 的键是 `uin`，`uid` 只在**查询时**由
    `sender_names` 反查（spec §3.1：uid 与 uin 会脱节，落盘就等于把一份
    会过期的映射冻进配置）。今天靠「代码里没写 uid」保证，而
    **没有任何测试挡着后来者顺手加一列**。
    """
    p = tmp_path / "persons.toml"
    config.save_persons([config.Person(uin=10001, label="甲")], p)
    assert "uid" not in p.read_text(encoding="utf-8")


def test_load_config_wraps_a_toml_syntax_error(tmp_path):
    """⚠️ 本任务修掉的既有缺陷：语法错误必须包成 `ConfigError`。

    不包的话 `tomllib.TOMLDecodeError`（继承自 **ValueError**，不是
    RuntimeError）会**绕过 `cli._load_config_only` 的 `except ConfigError`**，
    用户看到的是**裸 traceback** 而不是一行可读的配置错误。
    `load_categories` 早就这么做了（`categories.py:51-55` 有完整论证），
    `load_config` 没有——M6 之后 Web 会写这份文件，撞上的概率从
    「人手打错字」升到「一次序列化缺陷」。

    ⚠️ 变异反证：去掉 `load_config` 里那个 `try/except tomllib.TOMLDecodeError`
    ⇒ 本条**必红**（抛的是 TOMLDecodeError）。
    """
    p = tmp_path / "groups.toml"
    p.write_text("[settings\nqq_db_dir = 1\n", encoding="utf-8")
    with pytest.raises(config.ConfigError, match="TOML"):
        config.load_config(p)


def test_toml_str_is_reachable_under_its_public_name():
    """`_toml_str` → `toml_str` 的改名必须真的发生（`categories.py` 要 import 它）。

    ⚠️ 这条看着像废话，但它挡的是一个具体的形状：改名改了一半——
    留个 `_toml_str = toml_str` 的兼容别名。别名会让两处引用**看起来都在用
    `toml_str`**，而实际上 `save_persons` 走的是别名那条路，于是
    「谁在调用它」这个问题永远数不清（本项目反复吃「看起来有、其实没人看」
    那个形状的亏，见 `daily.RunReport.counters` 的墓碑）。
    """
    assert hasattr(config, "toml_str")
    assert not hasattr(config, "_toml_str"), "别留兼容别名"


# ── Fix loop 1（T2 独立审查：Important-2 / FIX 2、FIX 3）──────────────


def test_load_config_rejects_a_wrong_bracket_shape(tmp_path):
    """⭐ `[groups]`（**单层**方括号）必须响亮——T2 复审 Important-2。

    ⚠️ 不挡的话：单层括号解析出来的是**一张表**（dict），而代码按**数组**用它
    （`for entry in ...` 去迭代 dict 的**键**，下一行 `entry.get("id")` 抛
    `AttributeError`）⇒ 绕过只捕 `ConfigError` 的契约（`cli._load_config_only`），
    用户看到的是**裸 traceback** 而不是一行可读的配置错误。

    ⚠️ 与 `categories.py` 对 `[[categories]]` 的守卫**同形**：只差一层括号，
    是极常见的笔误——但两处理由各自写清（`categories.py` 那处不为本节负责）。

    ⚠️ 变异反证：把 `validate_config_raw` 里那两个 `groups` 形状分支删回
    `raw.get("groups", [])` ⇒ 本条**必红**（抛的是 `AttributeError`）。
    """
    p = tmp_path / "groups.toml"
    p.write_text(
        f'[settings]\nsource_dir = "{tmp_path.as_posix()}"\noutput_db = "data/vigil.db"\n\n'
        "[groups]\nid = 111\nname = \"甲群\"\n",
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError, match="数组段"):
        config.load_config(p)


def test_load_config_rejects_a_group_entry_that_is_not_a_table(tmp_path):
    """⭐ `groups = [1, 2]` 必须响亮——T2 的 C6，T7 之前收（它挡着配置包）。

    ⚠️ 上一条（`test_load_config_rejects_a_wrong_bracket_shape`）挡的是
    「**段本身**不是数组」（`[groups]` ⇒ 解析成 dict）。本条挡的是**另一种形状**：
    段确实是数组，但**元素不是表**（`groups = [1, 2]`，TOML 允许数组装标量）。
    两种形状不同，却落在同一行代码上——`entry.get("id")`：
    段是 dict 时 `entry` 是**键**（str），元素不是表时 `entry` 是那个标量。

    ⚠️ 不挡的话：`entry` 是 `int` ⇒ `AttributeError` ⇒ **绕过只捕 `ConfigError`
    的契约**（`cli._load_config_only`），用户看到的是裸 traceback。
    实测（2026-09-17 探针，修前）：`AttributeError: 'int' object has no attribute 'get'`。

    ⚠️ **为什么这条必须挡在配置包（T7）之前**：`configpack.parse` 要消费
    `validate_config_raw`，而它接的是**外部文件**（用户拷来的包）。形状错的外部
    输入撞上 `AttributeError` ⇒ **500 而不是 400**，且排障方向完全错：报错指向
    服务端，真相是包写错了。

    ⚠️ `groups = [1, 2]` 写在 `[settings]` **之前**是承重的：TOML 里落在
    `[settings]` 之后的裸键是 `settings.groups`，那样探不到顶层这一支
    （T2 报告记过这个自己踩的坑）。
    """
    p = tmp_path / "groups.toml"
    p.write_text(
        "groups = [1, 2]\n\n"
        f'[settings]\nsource_dir = "{tmp_path.as_posix()}"\noutput_db = "data/vigil.db"\n',
        encoding="utf-8",
    )
    with pytest.raises(config.ConfigError, match="键值表"):
        config.load_config(p)


def test_save_config_tolerates_a_missing_qq_db_dir(tmp_path):
    """⭐ 写路径**不查外部路径存不存在**——T2 复审 FIX 3（controller 裁决）。

    ⚠️ `qq_db_dir` 的**外部存在性**是**环境事实**，不是配置属性：可移动盘没挂载、
    换了机器、目录临时改名，都不该让「网页上保存一个群」整页失败
    （而 `save_config` 的落盘前自校会调 `validate_config_raw`，所以写路径必须是
    `require_paths_exist=False` 那一侧）。

    ⚠️ 反过来，**读路径仍然要查**（默认 `True`）：`load_config` 马上要用那个目录，
    不存在就该当场报错。这条用例把两侧一起钉住。

    ⚠️ 变异反证：把 `require_paths_exist` 参数去掉、恢复无条件 `.is_dir()`
    ⇒ 本条**必红**（`save_config` 抛 `ConfigError`）。
    """
    p = tmp_path / "groups.toml"
    missing = tmp_path / "not_mounted_yet"  # ⚠️ 故意**不建**这个目录
    cfg = config.Config(
        source_dir=missing,
        output_db=config.REPO_ROOT / "data" / "vigil.db",
        groups=(config.Group(id=111, name="甲群"),),
        # ⚠️ `source="qq"` 是**必需的**：存在性检查只对 qq 源做。
        # 合成源不用 `source_dir`（语料由代码生成），要求那个目录存在是错的 ——
        # 一个演示用户不该为了跑 `vigil demo` 先去建一个空目录。
        # 本条要钉的是"qq 源下读路径仍然响亮"，所以必须带上它。
        source="qq",
    )
    config.save_config(cfg, p)  # 不许抛
    assert not missing.exists(), "用例自己不许把这个目录建出来"
    # 读路径这一半：仍然响亮（默认 require_paths_exist=True）
    with pytest.raises(config.ConfigError, match="不存在"):
        config.load_config(p)


def test_synthetic_source_does_not_require_the_source_dir(tmp_path):
    """⚠️ 合成源**不查** `source_dir` 存不存在。

    那个目录的语义随 source 变。合成源的语料由代码生成、根本不用它 ——
    要求一个演示用户先建空目录才能跑 `vigil demo`，是把"环境事实"
    错当成了"配置前提"。
    """
    p = tmp_path / "groups.toml"
    missing = tmp_path / "never_created"
    cfg = config.Config(
        source_dir=missing,
        output_db=config.REPO_ROOT / "data" / "vigil.db",
        groups=(config.Group(id=111, name="甲群"),),
        source="synthetic",
    )
    config.save_config(cfg, p)
    got = config.load_config(p)  # 不许抛
    assert got.source == "synthetic"
    assert not missing.exists()


# ── 夹具的判据（M6 T7 Fix loop 1 / FIX 2）────────────────────────────


def test_default_persons_path_is_isolated_by_conftest():
    """⭐ **夹具的判据**（不是夹具本身）：`DEFAULT_PERSONS` 必须被挪出仓库、且自带内容。

    ⚠️ 为什么要有这条：`vigil config import` 是本项目**第一条会写
    `DEFAULT_PERSONS` 的产品路径**（`save_persons` 是整体替换写）。夹具一旦被人
    删掉 / 改名 / 换写法，**不会有任何东西变红**——直到某次全量跑完，用户的真实
    `config/persons.toml`（真数据、要进 git）被测试整体覆盖。
    这正是 Global Constraint 9 与勘误 E6 说的那件事：
    **新增的可写路径必须被夹具隔离，而"隔离了"这件事本身要有判据。**

    ⚠️ 三条断言各挡一种退化，缺一条就是空守卫：
      · **路径在仓库外** —— 挡"夹具被删掉"（这条同时是 FIX 2 的变异反证落点）；
      · **文件存在** —— 挡"改成指向不存在的路径"（见 conftest 里那段：
        "文件不存在"与"名单为空"在断言层面同形）；
      · **读出来非空** —— 挡"内容写成空文件"（那也会让上面两条全绿）。

    ⚠️ 本用例**只读**，任何情况下都不会往真实名单里写一个字
    （即使夹具坏了：只读一条真名单，不改它）。

    ⚠️ 变异反证：把 `conftest.py` 的 `_isolate_persons_toml_path` 改成空转
    （只留一条 docstring）⇒ 本条**必红**（第一条断言）。
    只跑本条，**绝不许跑全量**——夹具一撤，别的用例就可能写进真仓库。
    """
    target = config.DEFAULT_PERSONS
    repo = config.REPO_ROOT.resolve()
    assert not target.resolve().is_relative_to(repo), (
        f"`DEFAULT_PERSONS` 还在仓库里：{target}——省 path 的 `save_persons` 会"
        "整体覆盖用户的真实名单（要进 git 的真数据）"
    )
    assert target.is_file(), (
        f"夹具必须**自带内容**（{target} 不存在）：'文件不存在'是 `load_persons` 的"
        "合法语义（空名单），会让'夹具没生效'与'夹具生效且名单为空'变得不可区分"
    )
    assert config.load_persons(), "夹具自带的那份必须是**非空**名单，否则上一句等于没写"
