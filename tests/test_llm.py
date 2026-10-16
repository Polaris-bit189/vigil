"""LLM 客户端测试。**全部用假 transport，绝不联网。**"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from vigil.llm import LLMConfig, LLMError, chat_json, sanitize_for_llm


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False


def _envelope(content: str, *, prompt_tokens=10, completion_tokens=5) -> bytes:
    return json.dumps(
        {
            "model": "fake",
            "choices": [{"message": {"content": content}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
            },
        }
    ).encode("utf-8")


@pytest.fixture
def cfg():
    return LLMConfig(api_key="k", max_retries=3)


def test_parses_json_payload(monkeypatch, cfg):
    calls = []

    def fake_urlopen(req, timeout=None):
        calls.append(req)
        return FakeResponse(_envelope('{"items": [{"msg_id": 1}]}'))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = chat_json(cfg, system="s", user="u", sleep=lambda _: None)

    assert result.payload == {"items": [{"msg_id": 1}]}
    assert result.input_tokens == 10
    assert result.output_tokens == 5
    assert len(calls) == 1


def test_sends_auth_header_and_model(monkeypatch, cfg):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(_envelope("{}"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    chat_json(cfg, system="sys", user="usr", sleep=lambda _: None)

    assert captured["headers"]["Authorization"] == "Bearer k"
    assert captured["body"]["model"] == cfg.model
    assert captured["body"]["response_format"] == {"type": "json_object"}
    assert captured["body"]["messages"][0] == {"role": "system", "content": "sys"}


def test_sends_thinking_false_when_configured(monkeypatch):
    """显式关思考时，请求体里必须带上这个字段。

    实测依据：Qwen3.5-35B-A3B 不传该字段要 111.5s / 11,124 输出 token，
    传 false 只要 2.8s / 220 token。少了这个字段就是 40 倍的代价。
    """
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(_envelope("{}"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    chat_json(
        LLMConfig(api_key="k", enable_thinking=False),
        system="s",
        user="u",
        sleep=lambda _: None,
    )

    assert captured["body"]["enable_thinking"] is False


def test_omits_thinking_field_by_default(monkeypatch, cfg):
    """默认（None）**不发**这个字段——不是发 false。

    这条和上一条是一对，必须都在：只测「发了 false」的话，
    把 `if cfg.enable_thinking is not None` 写成「永远发」也能通过，
    而 Qwen2.5 这类老模型收到的就是它们没约定的字段。
    """
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(_envelope("{}"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    chat_json(cfg, system="s", user="u", sleep=lambda _: None)

    assert "enable_thinking" not in captured["body"]


def test_tolerates_markdown_fence(monkeypatch, cfg):
    """模型有时会把 JSON 包在 ```json 围栏里。"""
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: FakeResponse(_envelope('```json\n{"a": 1}\n```')),
    )
    assert chat_json(cfg, system="s", user="u", sleep=lambda _: None).payload == {"a": 1}


def test_retries_on_429_then_succeeds(monkeypatch, cfg):
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise urllib.error.HTTPError(
                "u", 429, "rate limited", {}, io.BytesIO(b"slow down")
            )
        return FakeResponse(_envelope('{"items": []}'))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    result = chat_json(cfg, system="s", user="u", sleep=lambda _: None)
    assert attempts["n"] == 2
    assert result.payload == {"items": []}


def test_does_not_retry_on_401(monkeypatch, cfg):
    """认证错重试一百次也还是错——白等。"""
    attempts = {"n": 0}

    def fake_urlopen(req, timeout=None):
        attempts["n"] += 1
        raise urllib.error.HTTPError(
            "u", 401, "unauthorized", {}, io.BytesIO(b"bad key")
        )

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    with pytest.raises(LLMError, match="401"):
        chat_json(cfg, system="s", user="u", sleep=lambda _: None)
    assert attempts["n"] == 1


def test_raises_after_exhausting_retries(monkeypatch, cfg):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(OSError("network down")),
    )
    with pytest.raises(LLMError, match="3"):
        chat_json(cfg, system="s", user="u", sleep=lambda _: None)


def test_raises_on_unparsable_content(monkeypatch, cfg):
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: FakeResponse(_envelope("这不是 JSON")),
    )
    with pytest.raises(LLMError, match="解析"):
        chat_json(cfg, system="s", user="u", sleep=lambda _: None)


@pytest.mark.parametrize(
    "raw_content",
    [
        None,  # 模型返回工具调用 / 被内容过滤时，content 就是 null
        12345,  # 数字
        "[1, 2, 3]",  # 裸数组
    ],
)
def test_rejects_non_object_content(monkeypatch, cfg, raw_content):
    """⚠️ 契约必须挡住非字符串 content 与非对象顶层（W2 审查实测抓出）。

    这两种都会**穿透 `chat_json` 的契约**，而不是变成 LLMError：
    * `content = None` → `None.strip()` 抛 `AttributeError`
    * 裸数组 → `_loads` 返回 list，下游 `payload.get()` 抛 `AttributeError`

    调用方按常理只 `except LLMError`，所以两种都会崩掉整轮 refine。
    """
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: FakeResponse(_envelope(raw_content)),
    )
    with pytest.raises(LLMError, match="解析"):
        chat_json(cfg, system="s", user="u", sleep=lambda _: None)


def test_backoff_is_exponential(monkeypatch, cfg):
    delays = []
    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: (_ for _ in ()).throw(OSError("x")),
    )
    with pytest.raises(LLMError):
        chat_json(cfg, system="s", user="u", sleep=delays.append)
    assert delays == [1, 2]


# ── max_tokens 与字符归一化（M2 Task 2）────────────────────


def test_max_tokens_omitted_by_default(monkeypatch, cfg):
    """默认不发 max_tokens——M1 的 refine 行为必须一字不变。"""
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(_envelope("{}"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    chat_json(cfg, system="s", user="u", sleep=lambda _: None)

    assert "max_tokens" not in captured["body"]


def test_max_tokens_sent_when_set(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse(_envelope("{}"))

    monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
    chat_json(
        LLMConfig(api_key="k", max_tokens=2000),
        system="s", user="u", sleep=lambda _: None,
    )

    assert captured["body"]["max_tokens"] == 2000


def test_sanitize_replaces_fullwidth_quotes():
    """实测：全角引号会让模型退化成无限空格循环（85s 未收尾）。"""
    assert sanitize_for_llm("昵称“某某网盘”") == "昵称「某某网盘」"


def test_sanitize_leaves_other_text_alone():
    """只修有证据的那一处，别的标点不许顺手改。"""
    assert sanitize_for_llm("【通知】9月14日…") == "【通知】9月14日…"
