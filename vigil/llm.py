"""SiliconFlow 客户端（OpenAI 兼容接口）。

只用标准库 urllib —— 项目依赖表目前只有 sqlcipher3，
为一个 POST 请求引入 httpx 不划算。

契约由 _probe_refine.py 用真实消息实测确定，三处易错点刻在下面，
改提示词的人务必先读：

  1. ``response_format={"type": "json_object"}`` 会让模型返回**对象**
     而非裸数组。所以提示词必须要求 ``{"items": [...]}`` 这种外层包裹，
     不能要求返回数组。
  2. 不告诉模型「今天」是哪天，它会把「9月7号」猜成过去的年份
     （实测猜成了 2023）。所以 user 消息里必须注入当前日期。
  3. 模型倾向只处理第一条消息就停下（实测输出仅 88 token）。
     所以提示词必须明确「无价值的消息直接不出现在结果里」。
  4. **全角引号会让模型退化成无限空格循环。** 2026-09-15 实测：同一份 10 条
     item 的日报请求，某条 detail 含 ``“ ”`` 时 85s 未收尾、已吐 8,896 字且
     仍在继续；把该处换成 ``「」`` 后 6.2s 正常返回。发往模型前请过
     ``sanitize_for_llm``。
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

DEFAULT_BASE = "https://api.siliconflow.cn/v1/chat/completions"

# 2026-09-14 六模型对照实验（5,604 条 / 41 批 / 同提示词 / 关思考）选定。
# 选它的理由只有一条但足够硬：**总延迟 64s，其余模型 179–259s**，快 3–4 倍，
# 而机械指标（引用匹配率 98%、询问类 2%）并列最好，¥0.4/¥3.2 还是新线里最便宜的。
#
# 已知代价，别当它完美：同一批来源里「9.11左右截止」它归成「编辑部」，
# 而上下文里的发信人明明是另一个部门——**它会补出原文没有的实体**。
# （完整评估材料是本地过程产物，未随仓库分发。）
#
# 历史：Qwen2.5-7B 曾因"与 32B 质量持平但便宜一个数量级"当选，后来在同一批
# 样本上被实测抓出纯幻觉（标题与来源完全无关），该结论已作废。
DEFAULT_MODEL = "Qwen/Qwen3.5-35B-A3B"

# 模型在 chat 模板里遇到全角引号会退化成**无限空格循环**——2026-09-15 实测：
#
#   原样发出（detail 含 “某某网盘”）  → 85s 未收尾，已吐 8,896 字且仍在继续
#   把该处 “ ” 换成 「」              → 6.2s 正常返回
#   全部条目的 “ ” 都换掉            → 5.8s 正常返回
#
# 实测分布：items 270 条里 4 条命中（1.5%）、messages 47,719 条里 25 条命中。
# 频率不高，但第一次拿真实数据试就撞上了。
#
# ⚠️ **只修有证据的这一处。** 【】『』… 没有实测过会退化，不要顺手加进来——
# 加了没有依据，日后也没法追溯为什么这么写。真要加，先做实验。
_LLM_UNSAFE = str.maketrans({"“": "「", "”": "」"})


def sanitize_for_llm(text: str) -> str:
    """把会让模型退化的字符换成安全等价物。发往模型的文本都要过这一道。"""
    return text.translate(_LLM_UNSAFE)


_FENCE = re.compile(r"^```[a-zA-Z]*\s*|\s*```$")


class LLMError(RuntimeError):
    """调用失败——重试耗尽，或响应无法解析。"""


@dataclass(frozen=True)
class LLMConfig:
    # 空串 = **不发 Authorization 头**。本地 Ollama 走这条
    # （它对空的 `Bearer ` 会直接拒绝）。
    #
    # ⚠️ 默认值是空串而不是必填：`api_key` 是第一个字段，必填的话所有调用点
    # 都得传 —— 而"本地模型不需要密钥"是常态，不是例外。且它是 dataclass 的
    # 第一个字段，加默认值不会影响后面字段的默认值合法性（都在它之后）。
    api_key: str = ""
    model: str = DEFAULT_MODEL
    base_url: str = DEFAULT_BASE
    timeout: int = 180
    max_retries: int = 3
    temperature: float = 0.1
    # None = 不发这个字段。混合思考模型（Qwen3/Qwen3.5）**默认是思考的**，
    # 实测 Qwen3.5-35B-A3B 不传该字段要 111.5s / 11,124 输出 token，
    # 传 false 只要 2.8s / 220 token——40 倍差距，且思考会让延迟撞穿超时
    # （Qwen3.5-4B 那 30 条 `read operation timed out` 就是这么来的）。
    # 留 None 而不是 False 做默认：老模型（Qwen2.5）不一定认这个字段，
    # 「不发」才是对它们最安全的形状。
    enable_thinking: bool | None = None
    # 输出 token 上限。None = **不发这个字段**（老模型不一定认）。
    # 默认 None 是为了让 M1 的 refine 行为一字不变；日报显式传 2000。
    # 为什么必须有：模型一旦退化成上面的空格循环，唯一能拦住它的就是超时
    # ——180s × 3 次重试 ≈ 9 分钟空转。实测加 max_tokens=1200 后同样的失控
    # 请求 23s 就停（虽被截断，但兜住了）。
    max_tokens: int | None = None


@dataclass(frozen=True)
class LLMResult:
    payload: dict
    input_tokens: int
    output_tokens: int


def _loads(text: str) -> dict:
    """稳健解析模型输出：容忍 ``` 围栏与前后缀噪声。

    ⚠️ 两道闸都不能省——W2 审查实测抓出，两者都会**穿透 chat_json 的契约**：

    * **content 可能不是字符串**（`null` 或数字）。模型返回工具调用、或被内容
      过滤时就是 `null`，此时 `None.strip()` 抛 `AttributeError`；
      调用方按常理 `except LLMError`，会被崩掉整轮 refine。
    * **顶层可能是裸数组**。直接返回 list 的话，下游 `payload.get("items")`
      会抛 `AttributeError`——同样穿透契约。本函数声明的是 `-> dict`，
      就必须保证真的是 dict。
    """
    if not isinstance(text, str):
        raise ValueError(f"content 不是字符串，而是 {type(text).__name__}")

    stripped = _FENCE.sub("", text.strip())
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        start, end = stripped.find("{"), stripped.rfind("}")
        if start < 0 or end <= start:
            raise
        parsed = json.loads(stripped[start : end + 1])

    if not isinstance(parsed, dict):
        raise ValueError(f"顶层不是 JSON 对象，而是 {type(parsed).__name__}")
    return parsed


def chat_json(
    cfg: LLMConfig,
    *,
    system: str,
    user: str,
    sleep=time.sleep,
) -> LLMResult:
    """发一次对话请求，返回解析后的 JSON 与 token 用量。

    sleep 可注入，测试里传 ``lambda _: None`` 就能免去真实等待。
    """
    request_body: dict = {
        "model": cfg.model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": cfg.temperature,
        "response_format": {"type": "json_object"},
    }
    if cfg.enable_thinking is not None:
        request_body["enable_thinking"] = cfg.enable_thinking
    if cfg.max_tokens is not None:
        request_body["max_tokens"] = cfg.max_tokens
    body = json.dumps(request_body).encode("utf-8")

    last_error: Exception | None = None

    for attempt in range(cfg.max_retries):
        headers = {"Content-Type": "application/json"}
        # ⚠️ **只在有密钥时才发 Authorization**。本地 Ollama（`auth = "none"`）
        # 拿到空的 `Bearer ` 会直接拒绝请求 —— 而"本地不需要密钥"这件事
        # 不能靠"传个假的占位 key"绕过，那会让"到底有没有鉴权"变得不可读。
        if cfg.api_key:
            headers["Authorization"] = f"Bearer {cfg.api_key}"
        request = urllib.request.Request(cfg.base_url, data=body, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=cfg.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:300]
            # 4xx（限流除外）重试不会有不同结果——立刻失败，别白等
            if 400 <= exc.code < 500 and exc.code != 429:
                raise LLMError(f"HTTP {exc.code}: {detail}") from exc
            last_error = LLMError(f"HTTP {exc.code}: {detail}")
        except Exception as exc:  # noqa: BLE001 — 网络层什么都可能抛
            last_error = exc

        if attempt < cfg.max_retries - 1:
            sleep(2**attempt)
    else:
        raise LLMError(f"重试 {cfg.max_retries} 次仍失败: {last_error}")

    try:
        content = raw["choices"][0]["message"]["content"]
        payload = _loads(content)
    except (KeyError, IndexError, TypeError, ValueError) as exc:
        # ⚠️ 用 ValueError 而不是 json.JSONDecodeError：_loads 的两道
        # 「类型闸」抛的是 ValueError，而 JSONDecodeError 本就是它的子类，
        # 所以这一条同时覆盖两者。少 Catch 一种就会让它穿透契约。
        raise LLMError(f"响应解析失败: {type(exc).__name__}: {exc}") from exc

    usage = raw.get("usage") or {}
    return LLMResult(
        payload=payload,
        input_tokens=int(usage.get("prompt_tokens") or 0),
        output_tokens=int(usage.get("completion_tokens") or 0),
    )
