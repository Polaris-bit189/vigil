import { useEffect, useRef, useState } from 'react'
import Markdown from 'react-markdown'
import { fetchDigest, fetchDigests } from '../api.ts'
import ItemCard from '../components/ItemCard.tsx'
import { isOnline, subscribeConnectivity } from '../connectivity.ts'
import { fmtDateTime } from '../format.ts'
import { readSnapshot } from '../snapshot.ts'
import type { DigestDetail, DigestSummary } from '../types.ts'

// 正在看的那一篇：id 与正文合成一个对象。拆成两个 state 的话，会出现
// 「标题已经是 A、正文还是 B」的中间帧——而屏幕上所有字都来自这个对象。
// detail 为 null = 这篇还没读到（请求在飞 / 刚失败），它**不是**空正文：
// 把「不知道」渲染成一篇没有内容的日报，就是拿失败冒充内容。
type Opened = { id: number; detail: DigestDetail | null }

/**
 * 屏幕上这份**日报列表**，连同**它是实时还是快照**一起存。
 *
 * ⚠️ 为什么合成**一个** state（不是 `list` + `snapshotAt` 两个，F2 的根因）：
 * 两个 state 靠"每一次 setList 都记得同时 setSnapshotAt"这条**纪律**维持一致
 * ——纪律不是结构。T5-concern 记下的"恢复在线后横幅消失而数据仍是快照"，
 * 就是这条纪律被 `online` 单独翻转打断的形态（屏幕上是旧数据，却不再声明它是旧数据）。
 * 出口标准 5 的两半（有快照数据 ⇔ 有横幅）由这个形状**结构性**保证。
 * `Feed.tsx` 的 `Shown` 是这个形状的原始出处。
 *
 * ⚠️ 也不要试图让横幅直接读 `!online` 就完事：那是另一种半新半旧——"离线读到了
 * 实时数据"（浏览器误报 offline）时，屏幕上是实时数据却被说成快照。
 */
type ListShown = {
  list: DigestSummary[]
  /** 列表数据来自快照时的同步时刻；实时数据时为 null。 */
  snapshotAt: number | null
}

export default function Digests() {
  // null = 还没读到过（请求在飞 / 列表读取失败）。
  // 它必须与「读到了、结果是 0 篇」分开：前者只能说「不确定」，
  // 后者才有资格说「还没有日报」。
  const [listShown, setListShown] = useState<ListShown | null>(null)
  const [opened, setOpened] = useState<Opened | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // 重跑列表请求的手动扳机：没有它，列表读取失败后就没有可见的恢复入口
  const [reloadTick, setReloadTick] = useState(0)

  // ── 在线/离线（F2）：横幅里"离线"两字的**唯一**依据 ─────────────────────
  //
  // ⚠️ 与 `Feed.tsx` 同一形状：`connectivity.ts`（T3）是状态的单一真相源，
  // 这里只订阅、不自己探测（两处各探就会各说各话，spec R4）。
  // ⚠️ effect **返回**退订函数 ⇒ 卸载时摘掉订阅，且 `StrictMode`（挂→卸→挂）
  // 下净剩**一个**订阅（「卸」摘掉第一个、「挂」再装一个）。
  //
  // ⚠️⚠️ 为什么这条状态**必需**（F2，不是装饰）：`api.ts` 对 **4xx** 保持
  // `online===true`（`noteFetchResult(r.status < 500)`），回落到快照
  // **不等于**离线。少了它，"后端活着但回 4xx"时下面的横幅会把"读不到"
  // 谎报成"离线"——一句用户无从分辨的假话。
  const [online, setOnline] = useState(isOnline)
  useEffect(() => subscribeConnectivity(() => setOnline(isOnline())), [])
  /**
   * ⚠️ 由 `online` 派生的"重取一次"扳机，**专供下面那个 effect 的依赖数组**（F2）。
   *
   * 出口标准 4 要求"网络恢复后**自动**回到实时数据"——列表 effect 原本只依赖
   * `[reloadTick]`，**不听 online 边沿** ⇒ 离线打开日报页、网络恢复后**一直卡在
   * 快照上**（审查实测 `afterStale=true`）。
   *
   * 为什么不直接依赖 `online`：true→false 也会让 effect 重跑，那是离线时白发
   * 一次必然失败的请求（还会 `noteFetchResult(false)` 把状态机再按一次离线）。
   * 为什么不写成「订阅回调里直接 `setReloadTick`」：那样**首帧**的 `isOnline()`
   * 初值就不生效了——初值是"挂载那一刻的真相"，依赖变化是"之后每一次变化"。
   *
   * ⚠️⚠️ 判据里 **`online` 是承重的**：`seenOnline.current` 记的是"上一次算这个值
   * 时在不在线"，所以只有**真的从离线变成在线**才会 `+1`。少了它，任何一次无关
   * 的重渲染都会把 `online` 重新算成 `true` 并 `+1` ⇒ 每次渲染白拉一次列表
   * （`Feed.tsx` 的同一条注释，变异 E 的教训）。
   */
  const [onlineEpoch, setOnlineEpoch] = useState(0)
  const seenOnline = useRef(online)
  useEffect(() => {
    const wasOnline = seenOnline.current
    seenOnline.current = online
    if (online && !wasOnline) setOnlineEpoch((t) => t + 1)
  }, [online])
  // 请求序号：连点两篇日报时，慢的先发请求不许晚到覆盖后点的
  const seq = useRef(0)
  /**
   * 快照里的日报（含正文），按 `digest_id` 索引。
   *
   * ⚠️ 它**不是** `list` 的副本：`list` 是喂给列表的 `DigestSummary[]`，
   * 这一份留着 `body_md`/`items`，好在离线时"打开某篇"不必再发一个
   * 必然失败的请求。离线读快照 = **一次性读一次**（读的时候判定），
   * 而不是每次打开都去读盘。
   */
  const snapDigests = useRef<Map<number, DigestDetail> | null>(null)

  useEffect(() => {
    const mine = ++seq.current
    // 新一轮读取开始，上一次的失败结论即刻作废：留着它，重试成功后
    // 横幅会继续描述一个已经不存在的状态。
    setErr(null)
    fetchDigests()
      .then((r) => {
        if (mine !== seq.current) return
        setListShown({ list: r.digests, snapshotAt: null })
        snapDigests.current = null
      })
      .catch((e: unknown) => {
        if (mine !== seq.current) return
        // ⚠️ 失败了**才**回落快照（spec §3.3 的回落顺序：先网络 → 失败 → 快照）。
        const snap = readSnapshot()
        if (snap !== null && snap.digests.length > 0) {
          // `DigestDetail` 是 `DigestSummary` 的超集 ⇒ 显式挑字段而不是硬转类型
          //（多出来的是 `body_md`/`items`，留着给下面"打开"用）。
          setListShown({
            list: snap.digests.map((d) => ({
              digest_id: d.digest_id,
              day: d.day,
              window_from: d.window_from,
              window_to: d.window_to,
              created_at: d.created_at,
              item_count: d.item_count,
            })),
            snapshotAt: snap.synced_at,
          })
          snapDigests.current = new Map(snap.digests.map((d) => [d.digest_id, d]))
          // ⚠️⚠️ F2：**在线**失败（4xx）时保留错误（与 Feed 同一形状）。回落到
          // 快照 == "有东西看"，不是"什么都没发生"；吞掉它，在线 404 的用户会
          // 以为眼前这份快照就是实时的日报列表。离线时清掉（必然失败是已知前提）。
          setErr(isOnline() ? (e instanceof Error ? e.message : String(e)) : null)
        } else {
          setListShown(null)
          snapDigests.current = null
          setErr(e instanceof Error ? e.message : String(e))
        }
        // ⚠️ 判为"没有快照"时这里**不写 setListShown({list: [], ...})**：请求失败
        // 说的是「不知道有几篇」，写下 [] 就是把它记成「读到了 0 篇」，下一帧会
        // 渲染出「还没有日报。生成：`vigil digest`」——一句没有依据的话。
      })
    // ⚠️ `onlineEpoch` **承重**（F2 / 出口标准 4）：它只在"离线→在线"那个边沿
    // +1 ⇒ 网络恢复时重跑列表请求，回到实时数据，而不是**卡在快照上**
    //（且不会把 `online` 的 true→false 也接进来白发一次请求）。
  }, [reloadTick, onlineEpoch])

  function open(id: number) {
    const mine = ++seq.current
    // ⚠️ 快照里就有这一篇 ⇒ **直接用**，不发那个必然失败的请求：
    // 发了它，失败回调会把用户刚点开的那一篇报成"读取失败"——而我们手里
    // 明明有正文（自找的失败，且会盖掉列表）。
    const cached = snapDigests.current?.get(id)
    if (cached !== undefined) {
      setErr(null)
      setOpened({ id, detail: cached })
      return
    }
    setErr(null) // 请求一发，上一次的失败结论就不再描述当前状态
    // 只记「正在打开哪一篇」，不动 list：详情请求的抖动不该把用户
    // 正看着的列表从屏幕上抹掉（下面失败时仍渲染列表 + 一条横幅）。
    setOpened({ id, detail: null })
    fetchDigest(id)
      .then((d) => {
        if (mine !== seq.current) return
        setOpened({ id, detail: d })
      })
      .catch((e: unknown) => {
        if (mine !== seq.current) return
        setErr(e instanceof Error ? e.message : String(e))
      })
  }

  // 错误横幅上的「重试」。失败的是一次「打开详情」→ 重发那一篇；
  // 否则失败的是列表本身 → 重跑列表。
  // 两条路都必须**真的重发请求**：只清 err 会把「不知道」直接渲染成
  // 「还没有日报」，那是拿一次失败冒充一次空库。
  function retry() {
    if (opened !== null && opened.detail === null) open(opened.id)
    else setReloadTick((t) => t + 1)
  }

  // 已读到的正文。它是从 opened 派生的，不是独立 state——独立 state
  // 就有两处真相，迟早对不上。
  const cur = opened !== null ? opened.detail : null
  // 正在打开（或刚打开失败）的那一篇，用来在列表项上标出进度
  const pendingId = opened !== null ? opened.id : null

  if (cur !== null) {
    return (
      <div>
        <button
          onClick={() => setOpened(null)}
          className="mb-3 text-sm text-vigil-accent active:opacity-70"
        >
          ‹ 返回日报列表
        </button>

        <article className="rounded-xl bg-vigil-surface p-4 shadow-vigil-card backdrop-blur-vigil-panel">
          {/* 这一天由数据层给出，是页面唯一的标题 */}
          <h1 className="text-lg font-semibold">{cur.day} 日报</h1>
          <div className="mt-3">
            {/* react-markdown 默认**不渲染原始 HTML**，日报正文里的尖括号
                不会变成可执行标记——不额外引 sanitizer 也能安全 */}
            <Markdown
              components={{
                // 正文的 h1 恒定是「# 守夜人日报 · {day}」这一行（digest.py 的
                // 固定开头），说的就是上面那行标题的事，正文其余标题一律是 h2/h3。
                // 不管它的话：Tailwind v4 的 preflight 把 h1 重置成 font-size/
                // font-weight: inherit，它会以正文的样子紧贴在标题下面——同一个
                // 标题读两遍，且 React 树里出现两个 h1，屏幕阅读器要念两遍。
                // 所以这里按「不重复」处理：这行头部不渲染，标题以数据层的 cur.day 为准。
                h1: () => null,
                h2: ({ children }) => (
                  <h2 className="mt-4 mb-2 text-base font-semibold text-vigil-fg">
                    {children}
                  </h2>
                ),
                h3: ({ children }) => (
                  <h3 className="mt-3 mb-1 text-sm font-semibold text-vigil-fg">
                    {children}
                  </h3>
                ),
                // preflight 把 ul 的 list-style 重置成 none：不恢复的话日报
                // 每一条（- **{label}** {text} · {群名}）的圆点全没了，
                // 只剩加粗与行距，读起来像一串散句而不是一张清单
                ul: ({ children }) => (
                  <ul className="my-2 list-disc space-y-1.5 pl-5">
                    {children}
                  </ul>
                ),
                li: ({ children }) => (
                  <li className="text-sm leading-relaxed text-vigil-fg-2">
                    {children}
                  </li>
                ),
                p: ({ children }) => (
                  <p className="my-1.5 text-sm leading-relaxed text-vigil-fg-2">
                    {children}
                  </p>
                ),
                strong: ({ children }) => (
                  <strong className="font-semibold text-vigil-fg">
                    {children}
                  </strong>
                ),
                a: ({ href, children }) => (
                  <a
                    href={href}
                    target="_blank"
                    rel="noreferrer"
                    className="break-all text-vigil-accent underline"
                  >
                    {children}
                  </a>
                ),
              }}
            >
              {cur.body_md}
            </Markdown>
          </div>
        </article>

        {cur.items.length > 0 && (
          <>
            <p className="mt-5 mb-2 text-xs text-vigil-fg-4">
              这篇日报引用的 {cur.items.length} 条条目（点开可看源消息）
            </p>
            <ul className="space-y-2">
              {cur.items.map((it) => (
                <ItemCard key={it.item_id} item={it} />
              ))}
            </ul>
          </>
        )}
      </div>
    )
  }

  return (
    <div>
      {/* ⚠️ 快照横幅：**必须**说清这是何时同步的，不许假装实时（G10 / 出口标准 2）。
          只认 `snapshotAt`（跟着列表数据走），不认"上次请求失败过"。
          ⚠️⚠️ `!online` 是承重的（F2）：4xx 保持 `online===true`（`api.ts`），
          回落到快照 **!=** 离线。少了它，"后端活着但回 4xx"时会把"读不到"
          谎报成"离线"。 */}
      {listShown !== null && listShown.snapshotAt !== null && !online && (
        <p className="mb-3 rounded-lg bg-vigil-warn-soft p-3 text-sm text-vigil-warn">
          {/* ⚠️⚠️ T6：`仅最近 5 篇` 是**范围词**（Feed/Calendar 那两条同族）。
              快照只留最近 5 篇日报 ⇒ 列表短不是"日报只有这几篇"，而是"快照只有这几篇"。
              ⚠️ 这里是**篇**不是**天**：日报是一天一篇，但快照存的是"最近 5 篇"
              （`SNAPSHOT_DIGESTS`），措辞必须与它一致，别写成"最近 5 天"。 */}
          离线：显示的是本地快照（{fmtDateTime(listShown.snapshotAt)} 同步，仅最近 5 篇）。
        </p>
      )}

      {/* 失败横幅与列表同时在场：一次请求的抖动只该占一条横幅，不该把
          用户已经读到的列表从屏幕上抹掉（抹掉了就再也渲染不出列表按钮，
          err 也就永远清不掉了）。 */}
      {err !== null && (
        <div className="mb-3 rounded-lg bg-vigil-danger-soft p-3 text-sm text-vigil-danger">
          <p>日报读取失败：{err}</p>
          {/* ⚠️ 三态分开（G10 / 出口标准 5）：读到**没有快照**时，
              必须明说"离线"，而不是让用户把"读不到"读成"没有日报"。
              ⚠️⚠️ `!online` 是承重的（F2）：失败横幅对"在线 4xx"同样会出现
              （那**是**错误，该出现），但"离线"这一句只有 `!online` 才配说。 */}
          {!online && <p className="mt-1 text-xs">离线，且还没有本地快照。</p>}
          <button
            onClick={retry}
            className="mt-2 rounded-lg bg-vigil-danger-btn px-3 py-1.5 text-xs text-vigil-danger-text active:bg-vigil-danger-btn-active"
          >
            重试
          </button>
        </div>
      )}

      {listShown === null ? (
        // 失败时横幅已经说了原因，这里不再说「读取中」——那会是一句谎话
        err === null && <p className="text-sm text-vigil-fg-4">读取日报列表…</p>
      ) : listShown.list.length === 0 ? (
        <p className="text-center text-sm text-vigil-fg-4">
          还没有日报。生成：<code className="text-vigil-fg-2">vigil digest</code>
        </p>
      ) : (
        <ul className="space-y-2">
          {listShown.list.map((d) => (
            <li key={d.digest_id}>
              <button
                onClick={() => open(d.digest_id)}
                className="w-full rounded-xl bg-vigil-surface p-4 text-left transition-colors active:bg-vigil-surface-2 shadow-vigil-card backdrop-blur-vigil-panel"
              >
                <div className="font-medium">{d.day}</div>
                <div className="mt-0.5 text-xs text-vigil-fg-3">
                  {d.item_count} 条条目
                  {/* 列表留在原地，就得有「点击生效了」的痕迹，否则用户会
                      以为没点动（与 Feed 计数行的「读取中…」同一个理由）。
                      失败时同理明写，不能让这一项继续显示「打开中」。 */}
                  {d.digest_id === pendingId &&
                    (err === null ? ' · 打开中…' : ' · 打开失败')}
                </div>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
