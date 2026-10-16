import { useEffect, useRef, useState } from 'react'
import { bulkSetKind, bulkUndo, fetchCategories, fetchItems } from '../api.ts'
import ItemCard from '../components/ItemCard.tsx'
import PersonPicker from '../components/PersonPicker.tsx'
import { isOnline, subscribeConnectivity } from '../connectivity.ts'
import { fmtDateTime } from '../format.ts'
import { readSnapshot } from '../snapshot.ts'
import type { Category, Item } from '../types.ts'

const PAGE = 50

/**
 * 离线 + 有筛选时，在**本地**对 `snap.feed` 做与后端同口径的筛选。
 *
 * ⚠️ 口径（与 `vigil/store.py` 的 `_item_filters` 对齐，逐维度说明）：
 *   · `kinds`：`item.kind` ∈ kinds（**维度内并集**）；空数组 = 不过滤。
 *     后端筛的是**覆盖后**的类目（`_EFFECTIVE_KIND`），而快照里的 `item.kind`
 *     正是 `item_out` 用同一张覆盖表算出来的值 ⇒ 两者同源。
 *   · `persons`：`item.actor_uin` ∈ persons；空数组 = 不过滤。
 *     后端走 `sn.uin`（实名消息才有）⇒ 匿名条目 `actor_uin === null` 必不匹配。
 *   · `q`：**不分词、子串包含**、**大小写敏感**、与后端的**短查询**那一支
 *     （`len(q) < 3` 的 `LIKE '%q%'`）**同形**。
 *     ⚠️ 后端对 `len(q) >= 3` 走 FTS5 trigram：它**不分大小写**（ASCII）、
 *     按 3-gram 匹配（对中文与短串的边界行为**与子串并不完全一致**）。
 *     ⇒ 本地这一支对长查询会**比后端更严格**（可能少给条目，**不会**多给）。
 *     宁可少给：筛不出来时用户看到的是"当前筛选下还没有条目"，
 *     而不是一条**本来不该在这里**的条目（假阳比假阴危险，M2 同一条判据）。
 *
 * ⚠️ 三个维度之间是 **AND**（交集），与后端及 spec §四 #4 一致。
 * ⚠️ 不剥控制字符 / 不转义 LIKE 通配符：这里不是 SQL，`%` 就是字面量 `%`
 * ——后端剥控制字符是为了不让 NUL 把 pattern 截断成 `%`（那是 SQL 的问题），
 * 本地没有这回事。刻意**不**把后端的净化逻辑照抄过来（抄错了反而引入差异）。
 */
function filterFeed(
  items: Item[],
  kinds: string[],
  persons: number[],
  q: string,
): Item[] {
  return items.filter((it) => {
    if (kinds.length > 0 && !kinds.includes(it.kind)) return false
    if (persons.length > 0 && !persons.includes(it.actor_uin ?? NaN)) return false
    if (q !== '') {
      const hay = `${it.title}\n${it.detail ?? ''}`
      if (!hay.includes(q)) return false
    }
    return true
  })
}

// 屏幕上这批数据，连同「它属于哪个查询」一起存。
// 拆成四个 state 的话，请求在飞的那段时间会拼出「新筛选的标签 + 上一次查询的数字」
// 这种半新半旧的句子——而计数行存在的意义恰恰是交代这个数字覆盖的范围，
// 拼错比不显示更糟。所以数据、总数、筛选条件三者同生同死。
type Shown = {
  items: Item[]
  total: number
  /** ⚠️ 存成 join 后的字符串：数组恒不等于自身，`switching` 的比较会永远为真。 */
  kindsKey: string
  personsKey: string
  q: string
  /**
   * 这批数据来自快照时的同步时刻；实时数据时为 null。
   * ⚠️ 它必须与 `items` 同生同死（同一个 `Shown`）：拆成两个 state 就会出现
   * 「快照的数据 + 实时数据的结论」这种半新半旧的句子（与顶部那条注释同一个理由）。
   */
  snapshotAt: number | null
}

export default function Feed({
  kinds,
  onKinds,
  persons,
  onPersons,
  reloadSignal,
}: {
  kinds: string[]
  onKinds: (k: string[]) => void
  persons: number[]
  onPersons: (p: number[]) => void
  /** 外部（App 的「撤销」）要求重取。变一次 = 重跑首屏一次。 */
  reloadSignal: number
}) {
  const [cats, setCats] = useState<Category[]>([])
  const [input, setInput] = useState('')
  const [q, setQ] = useState('')
  // null = 还没读到过（首屏请求在飞 / 读取失败已清空）。
  // 它必须与「读到了、结果是 0 条」分开：前者只能说不确定，后者才配说 0。
  const [shown, setShown] = useState<Shown | null>(null)
  // 初值 true：首帧 effect 还没 flush，先按「正在读」画，不许先画一句总数
  const [loading, setLoading] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  // 重跑首屏的手动扳机：kind/q 都没变时也要能重新发请求。
  // 没有它，首屏失败后用户就没有任何可见的恢复入口（见下方 retry）。
  const [reloadTick, setReloadTick] = useState(0)
  // 请求序号：慢的旧请求晚于新请求返回时，不许覆盖新结果
  const seq = useRef(0)

  // ── 在线/离线（T3 的状态机是单一真相源）────────────────────────────────
  //
  // ⚠️ effect **返回**退订函数（`useEffect(() => subscribeConnectivity(...), [])`）：
  // 这既是"卸载时摘掉订阅"，也是 `StrictMode`（挂→卸→挂）下净剩**一个**订阅的原因
  // ——「卸」把第一个摘掉、「挂」再装一个。
  const [online, setOnline] = useState(isOnline)
  useEffect(() => subscribeConnectivity(() => setOnline(isOnline())), [])

  /**
   * ⚠️ 由 `online` 派生的"重取一次"扳机，**专供下面那个 effect 的依赖数组**。
   *
   * 为什么不直接把 `online` 写进依赖：`online` 从 true→false 也会让那个 effect
   * 重跑——那是**离线时白发一次必然失败的请求**（还会 `noteFetchResult(false)`
   * 把状态机再按一次离线）。
   * 为什么不写成「订阅回调里直接 `setReloadTick`」：那样**首帧**的 `isOnline()`
   * 初值就不生效了——初值本身是"挂载那一刻的真相"，而下面那个 effect 的依赖
   * 变化是"之后每一次变化"；两个来源各管一段，才不会漏。
   *
   * ⚠️ 判据里 **`online` 是承重的**：`seenOnline.current` 记的是"上一次算这个值时
   * 在不在线"，所以只有**真的从离线变成在线**才会 `+1`。少了这个判据，
   * 任何一次无关的重渲染（父组件传下来的新函数、点一下复选框）都会把
   * `online` 重新算成 `true` 并 `+1` ⇒ 每次渲染白拉一次列表。
   */
  const [onlineEpoch, setOnlineEpoch] = useState(0)
  const seenOnline = useRef(online)
  useEffect(() => {
    const wasOnline = seenOnline.current
    seenOnline.current = online
    if (online && !wasOnline) setOnlineEpoch((t) => t + 1)
  }, [online])

  // ── 多选批量改类目（M6 T11；D16 的「界面上多选批量改」那一半）──────────
  //
  // ⚠️ 一个 `Set<number>`、**跨分页保留**：用户会先挑几条、再往下翻着补几条
  // （存在 Feed 里而不是 ItemCard 里，就是为了这件事）。
  const [selected, setSelected] = useState<Set<number>>(new Set())
  // ⚠️ `bulkActor` 是**撤销的键**：`bulkSetKind` 回给我们的那个 actor。
  // 丢掉它，"改了 5 条、发现改错了"就只能一条条改回去。
  const [bulkActor, setBulkActor] = useState<string | null>(null)
  const [bulkCount, setBulkCount] = useState(0)
  const [bulkKind, setBulkKind] = useState('')
  const [bulkMsg, setBulkMsg] = useState<string | null>(null)
  const [bulkBusy, setBulkBusy] = useState(false)

  // ⚠️ 数组本身的引用每次渲染都变，直接拿它当依赖会让 effect 无限重跑；
  // 而直接对数组做比较又会让"选中顺序不同但集合相同"被当成换了筛选条件。
  // 排序后 join 成字符串：集合相同 ⇒ 串相同 ⇒ 不重跑。
  const kindsKey = [...kinds].sort().join(',')
  const personsKey = [...persons].sort().join(',')

  useEffect(() => {
    fetchCategories()
      .then((r) => setCats(r.categories))
      .catch(() => {
        // ⚠️ 失败(通常 = 离线)时**从快照取**类目表,而不是清成 `[]`(T6 的缺陷本身):
        // 清成 `[]` 的后果是类目条只剩「全部」——用户点不出任何筛选。
        //
        // ⚠️ 只在**离线**时用快照:在线 4xx 是"后端活着、请求有问题",
        // 那时回落到一张旧类目表 = 把一次失败谎报成一次成功(与下面取数回落、
        // 以及 F2/F3 的既有裁决同一条判据)。
        // ⚠️ `?? []` 是保底:快照也读不到时仍旧没有类目条——它是**真话**
        //("拿不到类目表"),不是"类目表是空的"(那是另一句话)。
        setCats(isOnline() ? [] : (readSnapshot()?.categories ?? []))
      })
  }, [])

  // 输入防抖：中文输入法下每敲一下都发请求，手机上会明显卡顿
  useEffect(() => {
    const t = setTimeout(() => setQ(input.trim()), 300)
    return () => clearTimeout(t)
  }, [input])

  useEffect(() => {
    const mine = ++seq.current
    setLoading(true)
    // 新一轮查询开始，上一次的失败结论即刻作废。
    // 留着它的话，重试成功后横幅会继续描述一个已经不存在的状态。
    setErr(null)
    fetchItems({
      kinds,
      persons,
      q: q || undefined,
      limit: PAGE,
    })
      .then((page) => {
        if (mine !== seq.current) return
        setShown({
          items: page.items,
          total: page.total,
          kindsKey,
          personsKey,
          q,
          snapshotAt: null,
        })
      })
      .catch((e: unknown) => {
        if (mine !== seq.current) return
        // ⚠️ 回落快照 = 「拿一份**匹配当前筛选**的清单来显示」。
        //
        // ⚠️⚠️ 为什么**无筛选**那一支仍旧带这个守卫（T6）：快照存的是
        // "最近 N 天的全部条目"——无条件把它显示在一个**带筛选**的查询下，
        // 就是拿一张不匹配的清单回答另一个问题（"只说自己有资格说的话"，M2）。
        //
        // ⚠️⚠️ T6 把"带筛选"这一支从"如实报错"改成"**本地筛快照**"，
        // 但**只在离线时**（`!isOnline()`）：
        //   · 离线 ⇒ 带筛选**可以**回落，并对 `snap.feed` 做同口径本地筛。
        //     用户要的效果就是"离线也能点类目筛"；不这么做，类目条即使从快照
        //     填出来了，点一下也只会看到"读取失败"。
        //   · **在线（4xx：后端活着、请求有问题）⇒ 行为一字不改**：仍如实报错。
        //     那是 M2 的既有裁决——在线时"筛不到"可能是后端真的出错了，
        //     拿本地快照冒充答案会把一次真实的失败谎报成一次成功的结果。
        const unfiltered = kinds.length === 0 && persons.length === 0 && q === ''
        const canFallback = isOnline() ? unfiltered : true
        const snap = canFallback ? readSnapshot() : null
        // ⚠️ 空筛选时 `filterFeed` 原样返回（`kinds/persons` 为空、`q` 为 ''），
        // 所以无筛选那一支走的仍是 `snap.feed` 本身，一字不差。
        const items = snap === null ? null : filterFeed(snap.feed, kinds, persons, q)
        // ⚠️⚠️ 「有没有东西可看」的判据分两种（刻意的不对称，T6）：
        //   · **无筛选** ⇒ 要 `items.length > 0`（= T5 的既有行为："空快照不外显"）。
        //   · **带筛选**（只有离线会走到）⇒ **空结果也算**：那是"这个筛选下快照里
        //     没有"——必须显示「共 0 条」+ 空结果文案 + 横幅（点明仅最近 5 天），
        //     而不是报"读取失败"（用户会以为离线根本没数据）。
        //     这正是 brief 里"筛不到会被读成没有这类 ⇒ 横幅要点明范围"这一条
        //     所预设的形态：**筛不到是要显示出来的**。
        const usable = snap !== null && items !== null && (items.length > 0 || !unfiltered)
        if (usable) {
          // ⚠️⚠️ F2:**在线**失败(4xx:后端活着、请求有问题)时**保留**错误——
          // 回落到快照 == "屏幕上有东西看",**不是**"什么都没发生"。少了这一行,
          // 在线 404 会既不说"离线"(横幅已被 `!online` 挡掉)又不报错 ⇒
          // 屏幕上只剩快照,用户以为那就是最新的实时数据。
          // 离线时清掉:那时**必然**失败是已知事实,横幅已说清"这是快照",
          // 再挂一条"读取失败"是把预期内的离线报成意外。
          setErr(isOnline() ? (e instanceof Error ? e.message : String(e)) : null)
          setShown({
            items,
            // ⚠️ `total` = **本地筛后**的条数（不是快照总条数）：计数行说的是
            // "这个筛选下有多少条"，用快照总条数会让用户以为筛出来了一整库。
            total: items.length,
            kindsKey,
            personsKey,
            q,
            snapshotAt: snap.synced_at,
          })
        } else {
          setErr(e instanceof Error ? e.message : String(e))
          // 清空而不是留着旧数据：旧数据标的是上一个筛选，留着会被读成本次结果
          setShown(null)
        }
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false)
      })
  }, [kindsKey, personsKey, q, reloadTick, reloadSignal, onlineEpoch])

  function loadMore() {
    // 没有可续的列表就不发请求：首屏失败后列表是空的，续页无从谈起
    if (shown === null) return
    const mine = seq.current
    setLoading(true)
    // 同上：重试一旦发出，横幅里那句失败就不再描述当前状态
    setErr(null)
    fetchItems({
      // 续的是屏幕上这批数据，用 shown 的条件，而不是用户刚点的新筛选。
      // ⚠️ 存的是 join 串，这里要还原成数组（空段滤掉：'' 不是有效 slug）。
      kinds: shown.kindsKey.split(',').filter(Boolean),
      persons: shown.personsKey.split(',').filter(Boolean).map(Number),
      q: shown.q || undefined,
      limit: PAGE,
      offset: shown.items.length,
    })
      .then((page) => {
        if (mine !== seq.current) return
        setShown((prev) =>
          prev ? { ...prev, items: [...prev.items, ...page.items] } : prev,
        )
      })
      .catch((e: unknown) => {
        if (mine !== seq.current) return
        setErr(e instanceof Error ? e.message : String(e))
      })
      .finally(() => {
        if (mine === seq.current) setLoading(false)
      })
  }

  // 错误横幅上的「重试」。列表里还有东西 = 失败的是续页，接着续；
  // 列表是空的 = 首屏就没读到，重跑第一页。这是首屏失败后唯一可见的恢复入口。
  function retry() {
    if (shown !== null && shown.items.length > 0) loadMore()
    else setReloadTick((t) => t + 1)
  }

  // 人工干预（改分类 / 删除）成功后的重取。
  //
  // ⚠️ 这里**刻意不复用 `retry()`**：列表里还有数据时 `retry()` 走的是
  // `loadMore()`——那是**往后追加**，追加**不会刷新屏幕上那条已经变了的条目**：
  // 删掉的条目会继续留在列表里、改过的分类也还是旧的（实测：Step 10 真跑一次，
  // 「删一条 → 从列表消失」与「撤销 → 条目回来」两条都因此不成立）。
  // 干预改变的是集合本身，唯一正确的动作是重跑第一页。
  function reload() {
    setReloadTick((t) => t + 1)
  }

  // ── 多选批量改类目的三个动作 ────────────────────────────────────────

  function toggleSelect(itemId: number) {
    // ⚠️ 必须造一个新 Set：直接 `setSelected` 里改原对象的话引用没变，
    // React 不会重渲染，而**屏幕上什么都不会发生**（勾了没反应）。
    setSelected((prev) => {
      const next = new Set(prev)
      if (next.has(itemId)) next.delete(itemId)
      else next.add(itemId)
      return next
    })
  }

  const kindLabel = (slug: string) =>
    cats.find((c) => c.slug === slug)?.label ?? slug

  async function applyBulk() {
    if (selected.size === 0 || bulkKind === '') return
    setBulkBusy(true)
    setBulkMsg(null)
    try {
      // ⚠️ 一次提交**一个** `actor`（后端为整批共用一个）：那几条从此能被
      // `POST /api/bulk-undo` 一次退回。逐条各发一次的话，"改了 5 条发现错了"
      // 要撤 5 次，而中途任何别的操作都会把那一串切成两段。
      const r = await bulkSetKind([...selected], bulkKind)
      setBulkActor(r.actor)
      setBulkCount(r.count)
      setBulkMsg(
        `已把 ${r.count} 条改成「${kindLabel(bulkKind)}」——` +
          '「撤销这次批量改」可以一次退回去。',
      )
      setSelected(new Set())
      // ⚠️ 重取**第一页**（`reload()`），**不是** `retry()`：后者在列表非空时走
      // `loadMore()`，那是往后**追加**——屏幕上刚改过的那几条不会被刷新，
      // 用户会以为改动没生效（M5 的 A-Minor-7 就是这个形状）。
      reload()
    } catch (e: unknown) {
      setBulkMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBulkBusy(false)
    }
  }

  async function undoBulk() {
    if (bulkActor === null) return
    setBulkBusy(true)
    setBulkMsg(null)
    try {
      const r = await bulkUndo(bulkActor)
      setBulkMsg(
        r.undone
          ? `已撤销这一批：${r.count} 条回到原来的类目。`
          : '这一批没有可撤销的改动（可能已经撤过了）。',
      )
      // 撤过一次就把键放掉：那个 actor 底下已经没有事件了，留着它只会
      // 让按钮再点一次、再看到同一句"没有可撤销的改动"。
      setBulkActor(null)
      reload()
    } catch (e: unknown) {
      setBulkMsg(e instanceof Error ? e.message : String(e))
    } finally {
      setBulkBusy(false)
    }
  }

  // 计数行里的标签与数字都取自 shown：它们说的是「屏幕上有什么」，
  // 而不是「用户刚选了什么」——后者在请求回来之前只是一句许愿
  const pickedKinds =
    shown !== null
      ? cats.filter((c) => shown.kindsKey.split(',').includes(c.slug))
      : []
  // 「当前筛选范围」的人话描述。空结果文案与计数行共用同一套词，
  // 免得同一个筛选在两处被说成两样。
  const scopeLabels = [
    ...pickedKinds.map((c) => c.label),
    ...(shown !== null && shown.personsKey !== ''
      ? [`${shown.personsKey.split(',').length} 个人物`]
      : []),
  ]
  // 数据还是旧的、请求已经在飞：计数行继续描述旧数据，但要说明正在刷新，
  // 否则用户会以为点击没生效
  const switching =
    loading &&
    shown !== null &&
    (shown.kindsKey !== kindsKey || shown.personsKey !== personsKey || shown.q !== q)
  const countLine =
    shown === null
      ? loading
        ? '读取中…'
        : '条目数未知' // 没有依据就不给数字：0 说的是「一条都没有」，不是「不知道」
      : `共 ${shown.total} 条` +
        (pickedKinds.length > 0
          ? ` · ${pickedKinds.map((c) => c.label).join('、')}`
          : '') +
        (shown.personsKey ? ` · ${shown.personsKey.split(',').length} 个人物` : '') +
        (shown.q ? ` · 关键词「${shown.q}」` : '') +
        (switching ? ' · 读取中…' : '')

  return (
    <div>
      <input
        value={input}
        onChange={(e) => setInput(e.target.value)}
        placeholder="搜标题或详情（两个字也能搜）"
        className="w-full rounded-xl bg-vigil-surface px-3 py-2.5 text-sm outline-none placeholder:text-vigil-fg-4 focus:bg-vigil-surface-2 shadow-vigil-card backdrop-blur-vigil-panel"
      />

      {/* 类目是**多选**：点一下切换，不是替换。选中的多个之间是并集。 */}
      <div className="mt-3 -mx-1 flex flex-wrap gap-1.5">
        <Chip active={kinds.length === 0} onClick={() => onKinds([])}>
          全部
        </Chip>
        {cats.map((c) => (
          <Chip
            key={c.slug}
            active={kinds.includes(c.slug)}
            onClick={() =>
              onKinds(
                kinds.includes(c.slug)
                  ? kinds.filter((k) => k !== c.slug)
                  : [...kinds, c.slug],
              )
            }
          >
            {c.icon} {c.label}
          </Chip>
        ))}
      </div>

      {/* ⚠️ `offline={!online}`（不是 `shown.snapshotAt !== null`）：置灰的理由是
          "**写**操作需要联网"，而这句话的真假只取决于网通不通（F4）。
          用 `snapshotAt` 的话，在线 4xx 回落快照时会把能用的写操作灰掉（F3 的同一条）。 */}
      <PersonPicker selected={persons} onChange={onPersons} offline={!online} />

      {/* ⚠️ 快照横幅：**必须**说清这是何时同步的，不许假装实时
          （G10 / 出口标准 2）。只认 `shown.snapshotAt`——离线但**读到了实时数据**
          （比如只是浏览器误报 offline）时不显示，那时屏幕上确实是实时的。
          ⚠️ 时刻用 `fmtDateTime`（本机时区），与它在别处的口径一致。
          ⚠️⚠️ 且 **`!online` 是承重的（F2）**：4xx 保持 `online===true`，回落
          快照 **!=** 离线；少了它，在线 4xx 会把"读不到"谎报成"离线"。 */}
      {shown !== null && shown.snapshotAt !== null && !online && (
        <p className="mt-3 rounded-lg bg-vigil-warn-soft p-2 text-xs text-vigil-warn">
          {/* ⚠️⚠️ T6:`仅最近 5 天` 这个**范围词**是承重的。快照只含最近 5 天,
              而离线 + 带筛选时"筛不到"会被用户读成"没有这类"(是**没有**,
              不是**这段时间内没有**)——"只说自己有资格说的话"(M2)。
              ⚠️ 变异反证:删掉「仅最近 5 天」⇒ Offline.dom.test 的范围词用例必红。 */}
          离线：显示的是本地快照（{fmtDateTime(shown.snapshotAt)} 同步，仅最近 5 天）。
        </p>
      )}

      {/* 说清「这句话覆盖的是什么」：带上筛选条件，别让一个数字看起来像全部 */}
      <p className="mt-3 text-xs text-vigil-fg-4">{countLine}</p>

      {/* ── 多选批量改（选中之后才出现）────────────────────────────────
          条数写在这儿而不是写成"几条被改了"：`selected` 可能包含**翻页前**
          选中的条目（这是刻意保留的），说清"已选几条"才对得上用户手里的事实。 */}
      {selected.size > 0 && (
        <div className="mt-3 flex flex-wrap items-center gap-2 rounded-lg bg-vigil-surface p-2.5 text-xs">
          <span className="text-vigil-fg-2">已选 {selected.size} 条</span>
          {/* ⚠️ 批量改是**写**操作：离线时按钮置灰之外，这里也明写原因
              （只置灰的话用户不知道是"坏了"还是"现在不行"）。 */}
          {!online && <span className="text-vigil-fg-4">批量改需要联网</span>}
          <select
            value={bulkKind}
            onChange={(e) => setBulkKind(e.target.value)}
            aria-label="批量改成的类目"
            className="rounded-lg bg-vigil-inset px-2 py-1 text-xs text-vigil-fg-2 outline-none"
          >
            <option value="">— 改成哪个类目 —</option>
            {cats.map((c) => (
              <option key={c.slug} value={c.slug}>
                {c.icon} {c.label}
              </option>
            ))}
          </select>
          <button
            onClick={applyBulk}
            disabled={bulkBusy || bulkKind === '' || !online}
            className="rounded-lg bg-vigil-accent-soft px-3 py-1 text-xs text-vigil-accent-text disabled:opacity-40"
          >
            改类目
          </button>
          <button
            onClick={() => setSelected(new Set())}
            className="rounded-lg bg-vigil-surface px-2 py-1 text-xs text-vigil-fg-3"
          >
            清空选择
          </button>
        </div>
      )}

      {bulkActor !== null && (
        <div className="mt-2 flex flex-wrap items-center gap-2 text-xs">
          <button
            onClick={undoBulk}
            // ⚠️⚠️ G9 / 出口标准 3 的**第五个消费者**（终审 re-review 探针 `RR-P6`
            // 抓到的漏网）：撤销批量改是**写**操作（`POST /api/bulk-undo`），
            // 离线时同样必须置灰。同一个条里的「改类目」早就带了 `|| !online`，
            // 撤销这一半漏了——而它恰恰是"改错了想退回去"时才点的那个按钮。
            disabled={bulkBusy || !online}
            className="rounded-lg bg-vigil-surface px-3 py-1.5 text-vigil-fg-2 disabled:opacity-40"
          >
            ↶ 撤销这次批量改（{bulkCount} 条）
          </button>
          {/* ⚠️ 置灰之外明写原因（只置灰的话用户不知道是"坏了"还是"现在不行"），
              与「改类目」那一句同一个形状。 */}
          {!online && <span className="text-vigil-fg-4">撤销需要联网</span>}
          {/* ⚠️ 说清这个按钮的**范围**：它只拿着最近一批的键，再改一批就换掉了。 */}
          <span className="text-vigil-fg-4">
            它只认最近这一批；再改一批，这一批就撤不回来了。
          </span>
        </div>
      )}
      {bulkMsg !== null && <p className="mt-2 text-xs text-vigil-fg-2">{bulkMsg}</p>}

      {err !== null && (
        <div className="mt-3 rounded-lg bg-vigil-danger-soft p-3 text-sm text-vigil-danger">
          <p>读取失败：{err}</p>
          {/* 显式的恢复入口：首屏失败时列表为空，分页按钮不渲染，
              没有这个按钮用户就只能靠「切走再切回」这类隐性绕路 */}
          <button
            onClick={retry}
            className="mt-2 rounded-lg bg-vigil-danger-btn px-3 py-1.5 text-xs text-vigil-danger-text active:bg-vigil-danger-btn-active"
          >
            重试
          </button>
        </div>
      )}

      {!loading &&
        err === null &&
        shown !== null &&
        shown.items.length === 0 && (
          <p className="mt-6 text-center text-sm text-vigil-fg-4">
            {/* 空结果必须说清「搜索范围」，否则会被读成「库里就没有这件事」。
                ⚠️ 多选之后不能只报一个类目名：说出来的范围必须与上面那句
                计数行描述的是同一件事（类目 ∧ 人物 ∧ 关键词）。
                ⚠️⚠️ **三态必须分开**（spec §3.3 / G10 / 出口标准 5）：
                这里的 `shown !== null` 已经保证了"读到了、确实是 0 条"，
                所以"还没有任何条目"在这一支里是**有依据**的——离线且读不到时
                走的是上面的错误横幅（`err !== null`，那一支把"离线，且还没有
                本地快照"明写出来），**不会**落到这里。 */}
            {shown.q !== ''
              ? `没有匹配「${shown.q}」的条目。`
              : scopeLabels.length > 0
                ? `当前筛选（${scopeLabels.join(' · ')}）下还没有条目。`
                : '还没有任何条目。'}
          </p>
        )}

      {/* ⚠️ 离线且**没有快照**可回落 ⇒ 这是错误横幅那一支：必须说清"离线"，
          不许落到上面的"还没有任何条目"（G10：空 ≠ 未知）。 */}
      {err !== null && !online && (
        <p className="mt-2 text-center text-xs text-vigil-fg-4">
          离线，且还没有本地快照。
        </p>
      )}

      <ul className="mt-3 space-y-2">
        {shown !== null &&
          shown.items.map((it) => (
            <ItemCard
              key={it.item_id}
              item={it}
              onMutated={reload}
              onSelect={toggleSelect}
              selected={selected.has(it.item_id)}
              // ⚠️⚠️ F3：判据是**连接性**（`!online`），**不是**"这份数据是不是快照"。
              // 置灰的理由是"**写**操作需要联网"——而网通不通只由 `connectivity.ts`
              // 说了算。写成 `shown.snapshotAt !== null` 会漏掉"在线 4xx"这一支：
              // 4xx 时快照照样回落（`snapshotAt` 非 null），但后端**是活的**，
              // 于是能用的写操作被灰掉、还挂着"需要联网"——三句假话连成一串。
              //
              // ⚠️ 反过来的那一支（离线但**读到了**实时数据：浏览器误报 offline 后
              // 又被 `noteFetchResult(true)` 纠正回在线）也确实存在——它的正确答案
              // 同样是 `!online`（此刻就是在线的），所以这一改对它是**顺带修好的**，
              // 不是回归（见本节末端那条"阴性对照"用例）。
              offline={!online}
            />
          ))}
      </ul>

      {shown !== null &&
        shown.items.length > 0 &&
        shown.items.length < shown.total && (
          <button
            onClick={loadMore}
            disabled={loading}
            className="mt-4 w-full rounded-xl bg-vigil-surface py-2.5 text-sm text-vigil-fg-2 active:bg-vigil-surface-2 disabled:opacity-40 shadow-vigil-card backdrop-blur-vigil-panel"
          >
            {loading
              ? '读取中…'
              : `加载更多（还有 ${shown.total - shown.items.length} 条）`}
          </button>
        )}
    </div>
  )
}

function Chip({
  active,
  onClick,
  children,
}: {
  active: boolean
  onClick: () => void
  children: React.ReactNode
}) {
  return (
    <button
      onClick={onClick}
      className={
        'rounded-full px-3 py-1 text-xs whitespace-nowrap transition-colors ' +
        (active ? 'bg-vigil-accent-soft text-vigil-accent-text' : 'bg-vigil-surface text-vigil-fg-3')
      }
    >
      {children}
    </button>
  )
}
