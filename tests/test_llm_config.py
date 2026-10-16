"""LLM 端点配置（`config/llm.toml`）—— M6 T3。

⚠️ 本文件**不联网**：所有 `urlopen` 都被换成一个假的响应对象，
但**报文本身是真的**——本文件最值钱的断言恰恰是「实际发出去的 JSON 里
有没有 `enable_thinking` 这个键」。
"""

from __future__ import annotations

import json
import pathlib

import pytest

from vigil import config, llm


def _patch_existing(monkeypatch, tmp_path, text: str) -> pathlib.Path:
    """让 `load_llm_settings()` 的**默认路径**指向一份临时文件。

    ⚠️ 必须是改 `DEFAULT_LLM_TOML` 而不是传 `path=`：本文件要验的正是
    「CLI / daily 不传 path 时读到什么」，传 path 会把被测的那段绕过去。
    """
    p = tmp_path / "llm.toml"
    p.write_text(text, encoding="utf-8")
    monkeypatch.setattr(config, "DEFAULT_LLM_TOML", p)
    return p


# ── 读取：三态 ──────────────────────────────────────────────────────


def test_missing_file_yields_defaults_and_warns(tmp_path, monkeypatch):
    """⭐ 文件不存在 ⇒ 全内置默认，且**必须出声**。

    ⚠️ 这一声是承重的：默认里 `enable_thinking=None` = **不发该字段**，
    而实测「不发」会让 Qwen3.5-35B-A3B 单批从 2.8s 涨到 **111.5s**
    （11,124 输出 token）。删掉一个配置文件换来 40 倍慢，如果**没有任何提示**，
    那就是本项目反复禁止的「静默退化」。

    ⚠️ 变异反证：把 `on_warn` 那行删掉 ⇒ 本条**必红**。
    """
    # ⚠️ **偏离 brief（见报告 §偏差 D1）**：brief 这里写的是 `tmp_path / "nope.toml"`，
    # 而它下面那条断言要求提示里出现 `llm.toml` —— 二者**不能同时成立**
    # （`"llm.toml" not in "...\\nope.toml"`）。断言才是承重的那条（提示必须点名
    # 那个文件，用户才知道去建哪一个），所以改的是夹具路径：仍然不存在，
    # 但仍叫 `llm.toml`。
    monkeypatch.setattr(config, "DEFAULT_LLM_TOML", tmp_path / "gone" / "llm.toml")
    warnings: list[str] = []
    s = config.load_llm_settings(on_warn=warnings.append)
    assert s.base_url == llm.DEFAULT_BASE
    assert s.model == llm.DEFAULT_MODEL
    assert s.enable_thinking is None
    assert len(warnings) == 1
    assert "llm.toml" in warnings[0]
    assert "enable_thinking" in warnings[0], "提示里必须点名这个字段——它才是慢 40 倍的原因"


def test_file_without_the_key_means_do_not_send(tmp_path, monkeypatch):
    """文件存在但没写 `enable_thinking` ⇒ `None`（= 不发），**且不警告**。

    ⚠️ 不警告是刻意的：用户正在写一份自定义端点配置，他**没提**这个字段。
    这时候的每一个提醒都是噪音，而噪音会让真正的提醒失效。
    """
    _patch_existing(monkeypatch, tmp_path, '[llm]\nbase_url = "https://x/v1/chat/completions"\n')
    warnings: list[str] = []
    s = config.load_llm_settings(on_warn=warnings.append)
    assert s.enable_thinking is None
    assert warnings == []
    assert s.base_url == "https://x/v1/chat/completions"
    assert s.model == llm.DEFAULT_MODEL, "没写的字段逐项回落，不是整段回落"


def test_explicit_false_is_not_collapsed_to_none(tmp_path, monkeypatch):
    """⭐ `= false` 与「没写」必须是**两个不同的值**。

    ⚠️ 这是本任务最容易写错的一行：`section.get("enable_thinking")` 对
    「键不存在」和「值是 false」返回的东西**不同**（`None` vs `False`），
    所以 `if value:` 这种写法会把 `false` 静默吞成"没写" ⇒ 切到自建端点后
    行为**悄悄变了**。必须显式判 `not in`。

    ⚠️ 变异反证：把 `if "enable_thinking" not in section` 改成
    `if not section.get("enable_thinking")` ⇒ 本条**必红**。
    """
    _patch_existing(monkeypatch, tmp_path, "[llm]\nenable_thinking = false\n")
    assert config.load_llm_settings().enable_thinking is False

    _patch_existing(monkeypatch, tmp_path, "[llm]\nenable_thinking = true\n")
    assert config.load_llm_settings().enable_thinking is True


def test_a_non_bool_enable_thinking_is_rejected(tmp_path, monkeypatch):
    """`enable_thinking = "false"`（带引号）必须**响亮报错**，不许静默当真。

    ⚠️ 与本模块的立场一致：「配置写错是我们自己的问题，越早暴露越好。不猜、不兜底」。
    """
    _patch_existing(monkeypatch, tmp_path, '[llm]\nenable_thinking = "false"\n')
    with pytest.raises(config.ConfigError, match="enable_thinking"):
        config.load_llm_settings()


def test_malformed_llm_toml_is_wrapped(tmp_path, monkeypatch):
    _patch_existing(monkeypatch, tmp_path, "[llm\nbase_url = 1\n")
    with pytest.raises(config.ConfigError, match="TOML"):
        config.load_llm_settings()


def test_a_wrong_section_shape_is_rejected(tmp_path, monkeypatch):
    """`[[llm]]`（双层）会读成 list ⇒ 必须响亮（与 `[[person]]` 同一条判据）。"""
    _patch_existing(monkeypatch, tmp_path, '[[llm]]\nbase_url = "x"\n')
    with pytest.raises(config.ConfigError, match="llm"):
        config.load_llm_settings()


# ── base_url / model 的类型与空串（re-review Minor-3）────────────────────
#
# ⚠️ 原实现是 `str(section.get("base_url", "")).strip() or llm.DEFAULT_BASE`：
# **任何类型都能进来**。同一份文件里 `enable_thinking` 是响亮报错的，
# 两者待遇相反——而 `base_url` 写错的代价更靠后（要等到请求发出去才现形）。


@pytest.mark.parametrize(
    ("toml_value", "field"),
    [("123", "base_url"), ("1.5", "model"), ("true", "model"), ('["a"]', "base_url")],
    ids=["base_url=123", "model=1.5", "model=true", "base_url=数组"],
)
def test_a_non_string_endpoint_field_is_rejected(tmp_path, monkeypatch, toml_value, field):
    """非字符串 ⇒ `ConfigError`，**不许静默 `str()` 一下**。

    ⚠️ re-review Minor-3 实测：`base_url = 123` 会被转成 `'123'`，而它**真的会被
    拿去发请求**（`urllib` 会抛一个与被测逻辑无关的错）；`model = 1.5` 变 `'1.5'`，
    一路作为模型名发出去。契约是「字符串」，不是「能转成字符串的东西」
    ——与 `toml_int` 拒绝 `"10001"` 是同一条判据。
    """
    _patch_existing(monkeypatch, tmp_path, f"[llm]\n{field} = {toml_value}\n")
    with pytest.raises(config.ConfigError, match=field):
        config.load_llm_settings()


def test_an_empty_endpoint_field_is_rejected_on_both_sides(tmp_path, monkeypatch):
    """⭐ 空串**两侧都拒**——它原来会静默回落成内置默认（round-trip 不保值）。

    ⚠️ re-review Minor-3 实测：`save_llm_settings(LLMSettings(base_url=""))`
    写出 `base_url = ""`，而 `load_llm_settings` 读回来是**内置默认**。
    存进去的和读出来的不是一回事，而界面上看不出区别——正是本项目反复
    禁止的「静默回落」。

    选「拒」而不是「保值往返」的理由：`base_url = ""` 是一个**发不出去的 URL**，
    与 `enable_thinking = "false"` 同一类——**不猜、不兜底，当场说清楚**。
    """
    _patch_existing(monkeypatch, tmp_path, '[llm]\nbase_url = ""\n')
    with pytest.raises(config.ConfigError, match="base_url"):
        config.load_llm_settings()

    _patch_existing(monkeypatch, tmp_path, '[llm]\nmodel = "   "\n')
    with pytest.raises(config.ConfigError, match="model"):
        config.load_llm_settings()

    with pytest.raises(config.ConfigError, match="base_url"):
        config.save_llm_settings(config.LLMSettings(base_url=""), tmp_path / "out.toml")
    with pytest.raises(config.ConfigError, match="model"):
        config.save_llm_settings(config.LLMSettings(model="  "), tmp_path / "out.toml")


@pytest.mark.parametrize(
    "settings",
    [
        config.LLMSettings(base_url="https://x/v1", model="m"),
        config.LLMSettings(base_url="  https://x/v1  ", model="m"),
        config.LLMSettings(base_url="https://x/v1", model='名字带"引号"'),
        config.LLMSettings(base_url="https://x/v1", model="m", enable_thinking=False),
        config.LLMSettings(base_url="https://x/v1", model="m", enable_thinking=True),
        config.LLMSettings(base_url="https://x/v1", model="m", enable_thinking=None),
    ],
)
def test_save_then_load_roundtrips_any_accepted_value(tmp_path, settings):
    """⭐ **写出去的那个值，就是读回来的那个值**（load/save 之间的契约）。

    ⚠️ 判据是 `load(save(x)) == x` 的**归一化形态**：`base_url` / `model` 两侧
    都 `strip`（写侧先 strip，读侧再 strip），所以带首尾空白的输入落盘后是
    strip 过的值——**这是刻意的归一化，不是丢失**：它保证"你看到的文件内容
    = 读回来的值"。`enable_thinking` 的三态则必须**逐字**往返（`None` 那一态
    在文件里表现为**整行不写**，见 `save_llm_settings` 的 docstring）。
    """
    p = tmp_path / "llm.toml"
    config.save_llm_settings(settings, p)
    back = config.load_llm_settings(p)
    assert back.base_url == settings.base_url.strip()
    assert back.model == settings.model.strip()
    assert back.enable_thinking is settings.enable_thinking


# ── 写侧 ────────────────────────────────────────────────────────────


def test_save_then_load_roundtrips(tmp_path, monkeypatch):
    p = tmp_path / "llm.toml"
    s = config.LLMSettings(
        base_url="https://api.deepseek.com/v1/chat/completions",
        model="deepseek-chat",
        enable_thinking=None,
    )
    config.save_llm_settings(s, p)
    assert config.load_llm_settings(p).base_url == s.base_url
    assert config.load_llm_settings(p).model == s.model
    # ⭐ 「不发」这一态必须能穿过一次 round-trip —— 否则界面上选了它、
    # 保存后却变成 false，而用户从响应里看不出区别。
    assert config.load_llm_settings(p).enable_thinking is None


@pytest.mark.parametrize(
    "bad",
    ["false", "true", "关", 0, 1],
    ids=["'false'", "'true'", "'关'", "0", "1"],
)
def test_a_non_bool_enable_thinking_is_rejected_on_the_save_side(tmp_path, bad):
    """⭐ 写侧对 `enable_thinking` 的类型守卫（T3 deferred，**T9 之前必须收**）。

    ⚠️ **实测的静默反转**（修复前，本文件跑过原始输出）：

        save_llm_settings(LLMSettings(base_url="https://x/v1", model="m",
                                      enable_thinking='false'))
        ⇒ 产物里是 `enable_thinking = true`，读回来 `True`

    因为落盘那一行是 `'true' if settings.enable_thinking else 'false'`——
    **非空字符串是 truthy**，于是"关"被写成"开"。而**读侧是严格的**
    （`test_a_non_bool_enable_thinking_is_rejected` 就是它）：同一个文件里
    两侧待遇相反，写侧的坏值要等下一次读才现形、且现形的是一句
    "文件里的 enable_thinking 写错了"，方向完全不对。

    ⚠️ **为什么赶在 T9 之前**：T9 的配置界面走 JSON，
    `body.get("enable_thinking")` 拿到字符串是完全可能的，而后果是
    **用户选了「关」，存进去变成「开」**——比报错糟得多（本项目反复禁止
    「静默改语义」）。`0` / `1` 那一档是同一个形状的另一半：
    **falsy 的非布尔**（`0`）会被静默写成 `false`，同样不是用户的意思。

    ⚠️ 判据是 `ConfigError` 且**一个字节都不落盘**：先写文件再报错的话，
    磁盘上会留下一份"报错之前"的半成品，而调用方以为它失败了。
    """
    out = tmp_path / "llm.toml"
    with pytest.raises(config.ConfigError, match="enable_thinking"):
        config.save_llm_settings(
            config.LLMSettings(base_url="https://x/v1", model="m", enable_thinking=bad),
            out,
        )
    assert not out.exists(), "报错之前就落盘了——失败路径上不许留下半成品"


def test_saved_llm_toml_contains_no_key():
    """⭐ **密钥永远不进配置文件**（spec §3.2）。

    ⚠️ 这条不是"检查代码写没写"——它检查的是**产物**：`save_llm_settings`
    接受的 `LLMSettings` 里根本没有放密钥的字段，但这条测试挡的是
    将来有人"顺手"加一个 `api_key` 字段进来。密钥走 `.env`
    （`config.load_llm_key`），落进 `config/*.toml` 就等于**进了 git**。
    """
    import dataclasses
    assert "api_key" not in {f.name for f in dataclasses.fields(config.LLMSettings)}


# ── ⭐ 端到端：从配置文件到真实报文（R12 的全部意义）────────────────


def test_base_url_reaches_the_request(tmp_path, monkeypatch):
    """⭐ 出口判据 3 的最小机械形式：`base_url` 真的到了 URL 上（不是只存了个字段）。"""
    urls: list[str] = []

    class _Response:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return json.dumps(
                {"choices": [{"message": {"content": "{}"}}], "usage": {}}
            ).encode("utf-8")

    def fake_urlopen(request, timeout=None):
        urls.append(request.full_url)
        return _Response()

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)

    _patch_existing(
        monkeypatch, tmp_path,
        '[llm]\nbase_url = "https://my-own-host.example/v1/chat/completions"\n',
    )
    s = config.load_llm_settings()
    llm.chat_json(
        llm.LLMConfig(api_key="k", base_url=s.base_url), system="s", user="u",
        sleep=lambda _: None,
    )
    assert urls == ["https://my-own-host.example/v1/chat/completions"]


def test_the_autouse_fixture_isolates_the_default_llm_toml():
    """⭐ 钉住 `tests/conftest.py::_isolate_llm_toml_path`（勘误 E6）。

    ⚠️ **只写夹具不验夹具 = 空守卫**：夹具写错（名字拼错、忘了 `autouse`、
    指到了仓库内）时它是**静默**的——所有用例照样绿，而地雷原样还在。

    ⚠️ 地雷的实测形态（T3，2026-09-17）：把仓库里 `config/llm.toml` 的
    `model` 改成一个别的值（**那正是 M6 邀请用户做的事**）⇒
    `test_daily.py::test_stage_arguments_are_wired_from_the_day_window` 与
    `test_refine.py::test_cli_refine_model_falls_back_to_default` 两条
    **与 LLM 配置无关**的用例同时变红（`2 failed, 636 passed`）。
    它们绿只是因为出厂文件恰好等于内置默认——那是**环境相关的绿**。

    判据从**被测代码的视角**问（不是问夹具自身写没写对）：
    `load_llm_settings()` **不传 path** 时读的是哪个文件。两条合起来才成立：
    ① 那个路径**不在仓库里**；② 它**真的不存在**（于是读回内置默认，
    那是不关心 LLM 配置的用例所依赖的基线）。
    """
    assert not config.DEFAULT_LLM_TOML.is_relative_to(config.REPO_ROOT), (
        f"默认路径还指着仓库内的 {config.DEFAULT_LLM_TOML}——改那份文件"
        "（T3/M6 的核心用法）会红掉两条无关用例"
    )
    assert not config.DEFAULT_LLM_TOML.is_file(), (
        "隔离路径不该是一个已存在的文件：load_llm_settings 会去解析它，"
        "而空文件会抛「缺少 [llm] 段」（那些用例要的是内置默认）"
    )
    s = config.load_llm_settings()
    assert (s.base_url, s.model, s.enable_thinking) == (
        llm.DEFAULT_BASE, llm.DEFAULT_MODEL, None
    )


# ── ⭐ CLI 的三级优先级：命令行 > `config/llm.toml` > 内置默认（FIX B）──────
#
# ⚠️ 这三条在 T3 首轮里**一条判据都没有**：`cmd_refine` / `cmd_digest` /
# `cmd_repass` 里的 `base_url=args.base_url or llm_settings.base_url` 与
# `model=args.model or llm_settings.model` 全都不在既有用例的断言里
# （`tests/test_cli.py` 里那几个替身只钉 `apply` / `on_progress`）。
# 判据读的是**替身收到的 kwargs**，不是 `cli.py` 的源码文本。


def _wire_cli(monkeypatch, tmp_path, seen: dict) -> None:
    """把 `cli.main` 的外部依赖全接上——**一件都不许落到真实环境**。

    范式照抄 `tests/test_cli.py::cli_env` 与
    `tests/test_refine.py::test_cli_refine_model_falls_back_to_default`：
    `_load_config_only` / `_require_export_db` / `load_llm_key` 换替身，
    三个阶段换假的（只记下它们**收到了什么**）。
    锁不用接：`conftest.py` 的 autouse 夹具已经把锁路径挪到 tmp_path。
    """
    from vigil import cli
    from vigil.digest import DigestStats
    from vigil.refine import RefineStats
    from vigil.repass import RepassPlan, RepassStats

    cfg = type("_Cfg", (), {"output_db": tmp_path / "export.db"})()
    monkeypatch.setattr(cli, "_load_config_only", lambda: cfg)
    monkeypatch.setattr(cli, "_require_export_db", lambda c: tmp_path / "export.db")
    # ⚠️ patch 的**目标必须是 `cli` 的命名空间**，不是 `config` 的：
    # `cli.py` 用的是 `from .config import load_llm_key`（把函数对象绑进了
    # 自己的模块），所以改 `config.load_llm_key` 对它**毫无影响** ——
    # 实测：`cli.load_llm_key is config.load_llm_key` 在 patch 后仍是 False，
    # 于是真实的那个被调用、返回空串、硬闸拦下。
    monkeypatch.setattr(cli, "load_llm_key", lambda *a, **k: "k-llm")

    def fake_refine(c, **kw):
        seen["refine"] = kw
        return RefineStats()

    def fake_digest(c, **kw):
        seen["digest"] = kw
        return DigestStats()

    def fake_repass(c, **kw):
        seen["repass"] = kw
        return RepassPlan(), RepassStats()

    monkeypatch.setattr("vigil.refine.refine", fake_refine)
    monkeypatch.setattr("vigil.digest.digest", fake_digest)
    monkeypatch.setattr("vigil.repass.repass", fake_repass)


def _run_all_three() -> None:
    """三条命令各跑一次（repass 走默认的"只出清单"那条路，不写库）。"""
    from vigil import cli

    assert cli.main(["refine"]) == 0
    assert cli.main(["digest", "--date", "2026-09-16"]) == 0
    assert cli.main(["repass"]) == 0


def test_cli_reads_the_endpoint_from_the_file(monkeypatch, tmp_path, capsys):
    """⭐⭐ 不给 flag ⇒ **文件里**的 `base_url` / `model` 传下去。

    这条挡的正是 T3 的出口判据 3（「`refine` 与 `digest` 两处都生效」）在
    CLI 这一层的那一半：网页改了端点，命令行跑的那次必须跟着改。
    """
    _patch_existing(
        monkeypatch, tmp_path,
        '[llm]\nbase_url = "https://from-file/v1/chat/completions"\n'
        'model = "file-model"\nenable_thinking = false\n',
    )
    seen: dict = {}
    _wire_cli(monkeypatch, tmp_path, seen)

    _run_all_three()
    capsys.readouterr()

    for stage in ("refine", "digest", "repass"):
        assert seen[stage]["base_url"] == "https://from-file/v1/chat/completions", (
            f"{stage} 没拿到文件里的端点——网页上改了端点，{stage} 还在打老地址"
        )
        assert seen[stage]["model"] == "file-model", f"{stage} 没拿到文件里的模型"


def test_cli_flag_beats_the_file(monkeypatch, tmp_path, capsys):
    """⭐⭐ 给了 `--base-url` / `--model` ⇒ **命令行赢**。

    命令行优先是刻意的：排障要能"先试一下别的端点"而不用动文件。
    ⚠️ 变异「把 `args.base_url or llm_settings.base_url` 改成
    `llm_settings.base_url`（无条件用文件）」⇒ 本条**必红**。
    """
    _patch_existing(
        monkeypatch, tmp_path,
        '[llm]\nbase_url = "https://from-file/v1/chat/completions"\n'
        'model = "file-model"\n',
    )
    seen: dict = {}
    _wire_cli(monkeypatch, tmp_path, seen)
    from vigil import cli

    assert cli.main(["refine", "--base-url", "https://from-flag/v1", "--model", "flag-model"]) == 0
    assert cli.main(["digest", "--date", "2026-09-16", "--base-url", "https://from-flag/v1",
                     "--model", "flag-model"]) == 0
    assert cli.main(["repass", "--base-url", "https://from-flag/v1", "--model", "flag-model"]) == 0
    capsys.readouterr()

    for stage in ("refine", "digest", "repass"):
        assert seen[stage]["base_url"] == "https://from-flag/v1"
        assert seen[stage]["model"] == "flag-model"


def test_cli_falls_back_to_the_builtin_when_the_file_is_missing(monkeypatch, tmp_path, capsys):
    """⭐⭐ 文件不存在 ⇒ **内置默认**（不是 `None`、不是空串）。

    ⚠️ `None` 会一路传到 `LLMConfig`/`httpx`，而空串会变成一个**看起来像
    URL 的东西**去发请求——两条都是静默的坏。判据是逐字段等于 `llm.DEFAULT_*`。
    """
    monkeypatch.setattr(config, "DEFAULT_LLM_TOML", tmp_path / "gone" / "llm.toml")
    seen: dict = {}
    _wire_cli(monkeypatch, tmp_path, seen)

    _run_all_three()
    capsys.readouterr()

    for stage in ("refine", "digest", "repass"):
        assert seen[stage]["base_url"] == llm.DEFAULT_BASE
        assert seen[stage]["model"] == llm.DEFAULT_MODEL


@pytest.mark.parametrize("sub", ["refine", "digest", "repass"])
def test_cli_reports_a_broken_llm_toml_as_a_config_error(monkeypatch, tmp_path, capsys, sub):
    """⭐ 配置文件写错 ⇒ 一句 `[配置错误] …`，**不是裸 traceback**。

    ⚠️ re-review Important-3：`main()` 是裸的 `return args.func(args)`，**没有
    顶层兜底**。三个 `cmd_*` 只把 `ValueError` 转成 `[参数错误]`，于是
    `ConfigError` 会**一路穿透到用户面前**——与本仓 `_load_config_only()`
    的 `[配置错误]` 约定不一致（T2 刚为 `load_config` 的 `TOMLDecodeError`
    修过同一件事，见 `config.load_config` 里那段注释）。

    判据是 `SystemExit`：配置都读不对时，**一个阶段都不该跑起来**
    （`seen == {}` 那一条同时挡住"先跑再报错"）。
    """
    from vigil import cli

    _patch_existing(monkeypatch, tmp_path, "[llm]\nbase_url = 123\n")
    seen: dict = {}
    _wire_cli(monkeypatch, tmp_path, seen)

    with pytest.raises(SystemExit) as exc:
        cli.main([sub])
    message = str(exc.value)
    assert "[配置错误]" in message, f"`{sub}` 没把它转成一句人话：{message!r}"
    assert "base_url" in message, "错误里必须点名是哪个键写错了"
    assert seen == {}, f"配置都没读对，`{sub}` 却已经把阶段跑起来了"


def test_cli_think_flag_never_swallows_the_files_value(monkeypatch, tmp_path, capsys):
    """⭐ 「没给 `--think` 时**文件说了算**」——三个消费口径钉死。

    ⚠️ **实测修正（M6 T3 re-review，Minor-1）**：这条 docstring 原来抄了 brief
    的一句论证——「`args.think or llm_settings.enable_thinking` **碰巧**正确，
    它依赖 `args.think` 的默认值是 `False` 而不是 `None`」。**那句是错的**：
    `None or X` ≡ `False or X`（两者都是 falsy），所以两种写法对 `args.think`
    的**一切取值语义相同**——**根本不依赖默认值**。已删掉，不留错论证
    （本项目最贵的那条陈规：**错的理由会让后来者以为自己理解了，于是不去验证**）。

    ⚠️ 实现用显式写法（`True if args.think else …`）**不是为了更正确**——
    两者等价；它只是**更好读**，并且将来若加 `--no-think`（第三态）时不必
    再想一遍 `or` 链。**那是审美与前瞻，不是正确性。** 这里钉的是**要保住的
    语义**，三种情形一个都不许错：

      ① 文件 `= true`、没给 `--think` ⇒ `True`（**文件说了算**，不是"默认关"）
      ② 文件 `= false`、给了 `--think` ⇒ `True`（命令行赢）
      ③ 文件**没写**、没给 `--think` ⇒ `None`（**不发**，R12）
    """
    from vigil import cli

    seen: dict = {}
    _wire_cli(monkeypatch, tmp_path, seen)

    _patch_existing(monkeypatch, tmp_path, "[llm]\nenable_thinking = true\n")
    assert cli.main(["refine"]) == 0
    assert seen["refine"]["enable_thinking"] is True, (
        "文件里写了 true 却被吞成关闭——`--think` 的默认值一旦变成 None，"
        "`args.think or …` 那类写法就会在这里静默反过来"
    )

    _patch_existing(monkeypatch, tmp_path, "[llm]\nenable_thinking = false\n")
    assert cli.main(["refine", "--think"]) == 0
    assert seen["refine"]["enable_thinking"] is True, "给了 --think 必须赢过文件"

    _patch_existing(monkeypatch, tmp_path, '[llm]\nbase_url = "https://x/v1"\n')
    assert cli.main(["refine"]) == 0
    assert seen["refine"]["enable_thinking"] is None, "文件没写 = 不发这个字段"
    capsys.readouterr()


# ── ⭐ 三个消费点各自把配置传到请求层（R12 / 出口判据 3）────────────────────
#
# ⚠️ **re-review Important-1**：首轮这里只有 `refine` 一条判据，`digest` 与
# `repass` 里同样那两行（`base_url=base_url` / `enable_thinking=enable_thinking`）
# **删掉或改掉都 645 passed、一条不红**——正是 E5 那个形状（接线在、没人守）。
# 所以判据必须是「**真调那个消费函数**」+「读它**真正交给 `chat_json` 的那个 cfg**」，
# 既不是源码文本，也不是手工构造 `LLMConfig`（那正是首轮那条假反证的病根）。
#
# 三个 driver 各自造一份**最小可跑**的库，形状照抄各自的测试文件：
#   refine → `tests/test_refine.py::seeded` / `StubConfig`
#   digest → `tests/test_digest.py::seeded`
#   repass → `tests/test_repass.py::con` / `_MESSAGE_TABLES_DDL`
#
# ⚠️ 同批**删掉**了原来那条 `test_the_end_to_end_wire_format_omits_the_field`
# （re-review Minor-2 点它"名字叫端到端、其实不是"）：它手工构造 `LLMConfig`
# 之后断言报文，而报文那一层**已经由 `tests/test_llm.py` 的
# `test_sends_thinking_false_when_configured`（发 false）与
# `test_omits_thinking_field_by_default`（不发）各钉一条**，它只是重复；
# 它真正唯一想守的是「上一层有没有把值传下来」，而那正是下面这条参数化用例
# 在做的事（而且是真的调消费函数）。**去重而不是改名**，是因为留着一条
# 与别处重复的用例，只会让人误以为那一层有两条判据。

_MARK_BASE = "https://wired.example/v1/chat/completions"
_MARK_MODEL = "wired-model"

_MESSAGES_DDL = """
CREATE TABLE messages (
    msg_id INTEGER PRIMARY KEY, group_id INTEGER NOT NULL,
    ts INTEGER NOT NULL, sender_uid TEXT, content TEXT NOT NULL
);
CREATE TABLE sender_names (
    group_id   INTEGER NOT NULL,
    uid        TEXT    NOT NULL,
    group_nick TEXT,
    qq_nick    TEXT,
    uin        INTEGER,
    in_group   INTEGER,
    PRIMARY KEY (group_id, uid)
);
"""


def _new_conn():
    """内存库 + `messages` / `sender_names`（这两张表不由 `ensure_schema` 建）。"""
    import sqlite3

    conn = sqlite3.connect(":memory:", uri=True)
    conn.executescript(_MESSAGES_DDL)
    return conn


class _Cfg:
    """最小 `Config` 替身：`tier_of`（三个消费函数都要）+ `groups`（digest 要）。

    `tier_of` 一律 `"high"`，与 `tests/test_refine.py::StubConfig` 同款——
    本文件要验的是**参数有没有传下去**，不是预筛口径；档位调高保证候选非空。
    """

    def __init__(self, groups=()):
        from vigil.config import Config, Group

        self.groups = tuple(Group(id=g, name=n) for g, n in groups)
        # 刻意保留一个真 Config 只为 `tier_of` 的语义不漂移（本替身不用它，
        # 但写在这里提醒：真身一改，这条替身要跟着看一眼）。
        self._real = Config(
            source_dir=pathlib.Path(), output_db=pathlib.Path(), groups=self.groups
        )

    def tier_of(self, gid: int) -> str:
        return "high"


def _cfg_seen_by_refine(monkeypatch, *, enable_thinking):
    from vigil import refine, store
    from vigil.llm import LLMResult

    conn = _new_conn()
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        (1, 100, 1000, "u_a", "明天下午有讲座，地点西太湖报告厅"),
    )
    conn.commit()
    store.ensure_schema(conn)

    seen: list[llm.LLMConfig] = []

    def fake_chat_json(cfg, *, system, user, sleep=None):
        seen.append(cfg)
        return LLMResult(payload={"items": []}, input_tokens=1, output_tokens=1)

    monkeypatch.setattr(refine, "chat_json", fake_chat_json)
    refine.refine(
        _Cfg(), api_key="k", conn=conn, batch_size=1,
        base_url=_MARK_BASE, model=_MARK_MODEL, enable_thinking=enable_thinking,
        on_progress=lambda *a, **k: None,
    )
    return seen


def _cfg_seen_by_digest(monkeypatch, *, enable_thinking):
    from vigil import digest, store
    from vigil.llm import LLMResult

    conn = _new_conn()
    since, until = digest.day_window("2026-09-13")
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        (1, 100, since + 60, "u_a", "通知：明天交体检表"),
    )
    store.ensure_schema(conn)
    # 窗口内至少要有一条 item，否则 digest 走「空窗日不调模型」那一支，
    # `chat_json` 一次都不会被调用——那时本条测试是**空守卫**。
    conn.execute(
        "INSERT INTO items (item_id, kind, title, detail, event_ts, deadline_ts,"
        " group_id, actor_uid, place, links, amount, confidence, model,"
        " prompt_ver, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (1, "notice", "体检表", "10月8日前交", since + 60, None, 100, None,
         None, "[]", None, 0.9, "m", "v2", 1),
    )
    conn.commit()

    seen: list[llm.LLMConfig] = []

    def fake_chat_json(cfg, *, system, user, sleep=None):
        seen.append(cfg)
        return LLMResult(payload={"lines": []}, input_tokens=1, output_tokens=1)

    monkeypatch.setattr(digest, "chat_json", fake_chat_json)
    digest.digest(
        _Cfg([(100, "班级群")]), api_key="k", conn=conn, since=since, until=until,
        day_label="2026-09-13", base_url=_MARK_BASE, model=_MARK_MODEL,
        enable_thinking=enable_thinking, write_file=False, output_dir=None,
        on_progress=lambda *a, **k: None,
    )
    return seen


def _cfg_seen_by_repass(monkeypatch, *, enable_thinking):
    from vigil import repass, store
    from vigil.llm import LLMResult

    conn = _new_conn()
    store.ensure_schema(conn)
    content = "校园卡办理，需要的联系我"
    conn.execute(
        "INSERT INTO messages VALUES (?,?,?,?,?)",
        (1, 100, 1_700_000_000, "u_1", content),
    )
    cur = conn.execute(
        "INSERT INTO items (kind, title, detail, event_ts, deadline_ts, group_id,"
        " actor_uid, place, links, amount, confidence, model, prompt_ver,"
        " created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("notice", content, None, 1_700_000_000, None, 100, "u_1", None,
         "[]", None, 0.9, "m", "v2", 1_700_000_000),
    )
    conn.execute(
        "INSERT INTO item_sources (item_id, msg_id) VALUES (?,?)",
        (cur.lastrowid, 1),
    )
    conn.commit()

    seen: list[llm.LLMConfig] = []

    def fake_chat_json(cfg, *, system, user, sleep=None):
        seen.append(cfg)
        return LLMResult(payload={"items": []}, input_tokens=1, output_tokens=1)

    monkeypatch.setattr(repass, "chat_json", fake_chat_json)
    repass.repass(
        _Cfg(), api_key="k", conn=conn,
        base_url=_MARK_BASE, model=_MARK_MODEL, enable_thinking=enable_thinking,
        on_progress=lambda *a, **k: None,
    )
    return seen


@pytest.mark.parametrize(
    "driver",
    [_cfg_seen_by_refine, _cfg_seen_by_digest, _cfg_seen_by_repass],
    ids=["refine", "digest", "repass"],
)
@pytest.mark.parametrize(
    "thinking", [None, False, True], ids=["不发(None)", "发 false", "发 true"]
)
def test_every_consumer_forwards_the_configured_endpoint(
    driver, thinking, monkeypatch
):
    """⭐⭐ `base_url` / `model` / `enable_thinking` 必须**真的**到达请求层。

    判据读的是**消费函数真正交给 `chat_json` 的那个 `LLMConfig`**——
    三档 `enable_thinking` 各来一遍，因为三态是本任务的全部意义（R12）：
    `None`（不发，给通用 OpenAI 端点）、`False`（发 false，给 Qwen 混合思考
    模型，不发就慢 40 倍）、`True`（显式要思考）。少测任何一档，
    「上一层把它统一成 `False`」这类退化就有一半是看不见的。

    ⚠️ 变异反证：分别删掉 `digest.py` / `repass.py` 构造点里的
    `base_url=base_url` 或 `enable_thinking=enable_thinking` ⇒ 对应那一档**必红**。
    """
    seen = driver(monkeypatch, enable_thinking=thinking)
    assert seen, "一次都没跑到 `chat_json`——本条测试是空守卫，先查 driver"
    for cfg in seen:
        assert cfg.base_url == _MARK_BASE, "端点没传到请求层"
        assert cfg.model == _MARK_MODEL, "模型名没传到请求层"
        assert cfg.enable_thinking is thinking, (
            f"`enable_thinking` 被上一层改写了：传进去的是 {thinking!r}，"
            f"到请求层的却是 {cfg.enable_thinking!r}"
        )
