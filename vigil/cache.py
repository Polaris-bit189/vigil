"""DCS(Redis) 缓存层。**三样东西，不多不少。**

    ① 日报列表 / 详情       TTL 300s
    ② 搜索结果              TTL  60s
    ③ 分布式写锁            TTL  30s（在 redis_lock.py）

---

## 为什么需要它

本机部署下，`api.py` **每个请求开一条新连接、无缓存** —— 那没问题，
本地 SQLite 的查询是微秒级。但云端 RDS 是**网络往返**，
而 `/api/digests`、`/api/items` 这类端点会被前端反复拉。

引入缓存是云端架构的真实收益之一 —— 不是为了凑数。

---

## `NullCache`：没有 Redis 时的替身

本地开发不装 Redis，所以必须有一个替身。**它是显式的、可观测的**：

  · 命中率恒为 0（`stats()` 能读到 ⇒ "没生效"这件事**看得见**）
  · 不静默降级（不会假装命中、不会吞掉 Redis 的错误）

⚠️ 为什么要强调"不静默"：缓存失效的典型事故是**它悄悄不生效了**
（连不上、key 前缀写错、TTL 配成 0），而系统照常工作 ——
直到某天压上来才发现"缓存从来没命中过"。把命中率做成可读的数字，
是让这件事**能被发现**的唯一办法。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

# 缓存 TTL（秒）。取值的依据：
#   · 日报 300s —— 它一天才变一次，5 分钟的新鲜度绰绰有余
#   · 搜索 60s  —— 搜索是高频只读，且用户对结果陈旧 1 分钟不敏感
CACHE_TTL_DIGESTS = 300
CACHE_TTL_SEARCH = 60


class Cache(Protocol):
    def get_json(self, key: str) -> Any | None: ...
    def set_json(self, key: str, value: Any, ttl: int) -> None: ...
    def bump(self, namespace: str) -> None: ...


@dataclass
class CacheStats:
    """命中率统计。**它存在的意义就是"让缓存失效可见"**（见模块 docstring）。"""

    hits: int = 0
    misses: int = 0

    @property
    def hit_rate(self) -> float:
        total = self.hits + self.misses
        return self.hits / total if total else 0.0


@dataclass
class NullCache:
    """没有 Redis 时的替身。**行为可观测**：命中率恒 0，不是静默。"""

    stats: CacheStats = field(default_factory=CacheStats)

    def get_json(self, key: str) -> Any | None:
        self.stats.misses += 1
        return None

    def set_json(self, key: str, value: Any, ttl: int) -> None:
        _ = (key, value, ttl)  # 显式忽略 —— 替身不存任何东西

    def bump(self, namespace: str) -> None:
        _ = namespace


class RedisCache:
    """真的 Redis（华为云 DCS）。"""

    def __init__(self, client) -> None:
        self._client = client
        self.stats = CacheStats()

    def get_json(self, key: str) -> Any | None:
        raw = self._client.get(key)
        if raw is None:
            self.stats.misses += 1
            return None
        self.stats.hits += 1
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            # 缓存里是垃圾（版本不兼容 / 手工写坏）⇒ 当作未命中，
            # 而不是抛异常把请求打挂。缓存坏掉不该让服务挂掉。
            self.stats.misses += 1
            return None

    def set_json(self, key: str, value: Any, ttl: int) -> None:
        self._client.setex(key, ttl, json.dumps(value, ensure_ascii=False))

    def bump(self, namespace: str) -> None:
        """版本号自增 —— 比 `DEL vigil:digests:*` 更可靠。

        ⚠️ 为什么不用通配删除：`KEYS`/`SCAN` 在云 Redis 上是**重活**，
        而且通配删除**会漏掉**"还没插入的 key"（那种 key 删除时不存在，
        但它一旦被写入就是旧数据）。版本号自增没有这两个问题 ——
        旧版本号的 key 自然过期，新写入的一律带新版本号。
        """
        self._client.incr(f"vigil:{namespace}:v")


def redis_url() -> str:
    """Redis 连接串。**从环境变量取，不硬编码**（与 RDS DSN 同一条规矩）。"""
    return os.environ.get("VIGIL_REDIS_URL", "")


def open_cache() -> Cache:
    """按环境取缓存。

    ⚠️ **不配 Redis 时返回 `NullCache`，不报错。**
    理由：本地开发与"只想跑通 demo"的场景不该被一个缓存拖住。
    而"缓存没生效"这件事由 `CacheStats.hit_rate` 暴露（见模块 docstring）。

    ⚠️ 但**配了却连不上**时，这里是**响亮报错**而不是静默退回 NullCache ——
    那是配置错误（地址写错、密码错、安全组没开），
    静默退回会让线上跑一个月都没有缓存而没人发现。
    """
    url = redis_url()
    if not url:
        return NullCache()

    try:
        import redis  # 可选依赖组 `cloud`
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError(
            "配了 VIGIL_REDIS_URL 但没有 redis 库。装它：\n"
            "    pip install 'vigil[cloud]'"
        ) from exc

    client = redis.from_url(url, decode_responses=True)
    client.ping()  # 连不上就在这里失败，不等到第一次 get
    return RedisCache(client)


def key_digest_list(version: int, limit: int, offset: int) -> str:
    return f"vigil:digests:v{version}:list:{limit}:{offset}"


def key_digest_detail(version: int, digest_id: int) -> str:
    return f"vigil:digests:v{version}:id:{digest_id}"


def key_search(query: str) -> str:
    """搜索缓存 key。

    ⚠️ 查询串要**哈希**而不是直接拼 —— 用户的查询里可能带空格、中文、
    甚至控制字符，直接拼成 key 会难看且可能撞车。sha1 取前 16 位足够
    （冲突概率在这个量级上可忽略，而即使撞了也只是返回别人的搜索结果 ——
    但那是**只读公开数据**，无隐私后果）。
    """
    digest = hashlib.sha1(query.encode("utf-8")).hexdigest()[:16]
    return f"vigil:search:{digest}"


def current_version(cache: Cache, namespace: str) -> int:
    """读当前版本号。读不到（首次运行）按 0 算。"""
    got = cache.get_json(f"vigil:{namespace}:v")
    return int(got) if isinstance(got, (int, float)) else 0
