import { useEffect, useState } from 'react'
import { undoLast } from './api.ts'
import ArgusBrand from './components/ArgusBrand.tsx'
import ArgusCommand from './components/ArgusCommand.tsx'
import ThemePicker from './components/ThemePicker.tsx'
import { isOnline, subscribeConnectivity } from './connectivity.ts'
import { syncSnapshot } from './snapshot.ts'
import { applyTheme, readTheme, type ThemeId } from './theme.ts'
import { readView, writeView, TABS, type Tab } from './uiState.ts'
import Categories from './views/Categories.tsx'
import Calendar from './views/Calendar.tsx'
import Digests from './views/Digests.tsx'
import Feed from './views/Feed.tsx'
import Settings from './views/Settings.tsx'

// ⚠️ `type Tab` 与 `TABS` **搬去** `./uiState.ts`（T5）：它们与「当前在哪个
// tab」的持久化是同一件事——`readView` 要靠 `TABS` 校验取回的值合不合法，
// 两处各留一份的话，加一个 tab 就得记得改两个文件（漏一个的后果是
// 刷新后那个 tab 回落信息流，而代码里看不出为什么）。
export default function App() {
  // ⚠️ 三个坐标（tab / kinds / persons）收成**一份** `view` 并落盘（T5）。
  // 拆成三个 state 就要写三遍落盘；而 `readView` 是**逐字段**校验的，
  // 一份对象进出正好对上它的形状。
  // ⚠️ `readView` 传的是**函数引用** ⇒ React 当惰性初始化用，挂载时读一次盘
  // （与下面 `readTheme` 同一个形状）。坏字段的回落发生在 `readView` 里面。
  const [view, setView] = useState(readView)
  // 类目筛选状态**提到 App**：这样「类目」页点一个类目跳回信息流时，
  // 筛选条件能带过去，而 Feed 卸载重挂也不会丢。
  //
  // ⚠️ 多选：维度**内**是并集（选中多个类目 = 它们的合集），
  // 维度**间**是交集（类目 ∧ 人物）——spec §四 #4。
  // 撤销的可见反馈：点完要说一句，否则不知道到底生效没有
  const [undoMsg, setUndoMsg] = useState<string | null>(null)
  // 撤销改的是库的**有效状态**，而 Feed 手里那份列表是旧快照——不给它发个
  // 信号，条目不会回到屏幕上（实测：撤销成功了，界面却毫无变化，
  // 用户会以为撤销没生效，于是再点一次，把上一步也撤掉）。
  const [dataTick, setDataTick] = useState(0)

  // ── 在线/离线 + 快照同步（T5）────────────────────────────────────────
  //
  // ⚠️ 状态机的**单一真相源**是 `connectivity.ts`（T3）：它收两路信号
  //（浏览器事件 + 每个请求的真实结局），后者赢。这里只订阅、不自己探测——
  // 两个组件各自探测就会各说各话（spec R4）。
  const [online, setOnline] = useState(isOnline)
  // ⚠️ effect **返回**退订函数：既保证卸载时摘掉订阅，也让 `StrictMode`
  //（挂→卸→挂）下净剩**一个**订阅。
  useEffect(() => subscribeConnectivity(() => setOnline(isOnline())), [])

  useEffect(() => {
    // ⚠️ 离线就该 `return`：离线时这一组请求**必然失败**，发出去只会
    // ①白费一次超时等待 ②每个失败都会 `noteFetchResult(false)` 把状态机
    // 又按回离线（自己给自己制造事件）。
    if (!online) return
    // ⚠️ 失败不打扰用户：快照是"锦上添花"，同步失败只意味着离线时少一份数据
    //（真正的理由在 `syncSnapshot` 的 docstring 里）。
    void syncSnapshot().catch(() => {})
  }, [online])

  const { tab, kinds, persons } = view
  const setTab = (t: Tab) => setView((v) => ({ ...v, tab: t }))
  const setKinds = (k: string[]) => setView((v) => ({ ...v, kinds: k }))
  const setPersons = (p: number[]) => setView((v) => ({ ...v, persons: p }))

  // 任何一处筛选变化都写回一次（一个 effect 收口，好过在每个 setter 里各写一遍）。
  // 挂载时也跑一次：`readView` 已经把坏字段清成默认值了，这一写等于把
  // 盘上那份垃圾一并**改回合法**——否则坏 JSON 会一直躺在那里，每次刷新重解析。
  useEffect(() => {
    writeView(view)
  }, [view])

  // 主题：值放在 App（应用级状态），ThemePicker 只是它的一个**视图**
  // （两处各持一份就会出现「菜单说材质、页面是蒸汽波」）。
  // ⚠️ `readTheme` 传的是**函数引用** ⇒ React 当惰性初始化用，挂载时读一次盘。
  const [theme, setTheme] = useState<ThemeId>(readTheme)
  function pickTheme(id: ThemeId) {
    setTheme(id)
    applyTheme(id) // 落盘 + 挂属性 + 同步 theme-color，一处做完
  }

  async function undo() {
    setUndoMsg(null)
    try {
      const r = await undoLast()
      setUndoMsg(r.undone ? '已撤销上一步' : '没有可撤销的操作')
      // 没撤成（空栈）就不用重取：什么都没变
      if (r.undone) setDataTick((t) => t + 1)
    } catch (e: unknown) {
      setUndoMsg(e instanceof Error ? e.message : String(e))
    }
  }

  return (
    <div className="min-h-screen text-vigil-fg">
      {theme === 'argus' ? (
        /* ── ARGUS 头部（T8-2，计划 Task 2 Step 3）───────────────────────────
           用户原话：「看起来像终端一样的主题…一定要大变样，和现有的 UI 的按钮排布可以
           完全不同，**但功能不能少**」。⇒ 换掉的只有**排布与视觉**：
           品牌区（徽记 + ARGUS + 小字）整体取代「🏮 VIGIL」、撤销与主题切换变成终端风的
           文字动作、导航带 `[n]` 序号——四个 tab 与全部功能一个不少。
           ⚠️ 这里是**三行**（品牌 / 提示符行 / 导航），默认头部是两行；这是**设计**，
           不是回归（`[n]` 序号与提示符都要占地方）。375px 下不横向溢出归冒烟的渲染脚本核。 */
        <header className="sticky top-0 z-10 border-b border-vigil-border bg-vigil-header">
          <div className="mx-auto max-w-3xl px-4 pt-2">
            <div className="flex items-start gap-2">
              <ArgusBrand />
              <button
                onClick={undo}
                disabled={!online}
                className="ml-auto shrink-0 rounded-md px-2 py-1 text-xs text-vigil-fg-3 active:bg-vigil-surface disabled:opacity-40"
              >
                ↶undo
              </button>
              <ThemePicker value={theme} onChange={pickTheme} />
            </div>
            {/* ⚠️ `undoMsg` 在终端风里的落点：默认头部把它塞在撤销按钮**旁边**，
                这里没有那个位置（一行里已经放不下更多字）⇒ 单独一行小字。
                **功能不许少**：撤销的反馈在 ARGUS 下同样要看得见，否则用户点一下
                没有任何回话（那是"撤销到底生效没有"那个老问题的复现）。 */}
            {undoMsg !== null && (
              <div className="pb-0.5 text-[10px] text-vigil-fg-4">
                {/* ⚠️ 箭头**单占一个元素**（不是 `↶ {undoMsg}` 平铺）：平铺时这一格
                    的文本节点是 "↶ 没有可撤销的操作"，而判据（`argusBrand.dom.test.tsx`
                    的 `undoMsg` 那条）按**逐字**找那条消息 ⇒ 找不到。
                    判据要的是"那句话看得见"，箭头是装饰 ⇒ 各归各位。 */}
                <span aria-hidden="true">↶ </span>
                {undoMsg}
              </div>
            )}
            <div className="flex items-center gap-2 pb-1 pt-1.5 text-sm">
              <span className="shrink-0 text-vigil-fg-4">ARGUS:~$</span>
              <ArgusCommand />
            </div>
            <nav className="flex gap-1 overflow-x-auto pb-2 text-sm">
              {TABS.map((t, i) => (
                <button
                  key={t.key}
                  onClick={() => setTab(t.key)}
                  className={
                    'shrink-0 rounded-md px-1.5 py-1 transition-colors ' +
                    (tab === t.key
                      ? 'bg-vigil-surface-2 text-vigil-fg'
                      : 'text-vigil-fg-3 active:bg-vigil-surface')
                  }
                >
                  [{i + 1}]{t.label}
                </button>
              ))}
            </nav>
          </div>
        </header>
      ) : (
        /* ⚠️ 头部**两行**（品牌+动作 / tab）不是审美选择：375×667 下仪器实测
           （计划 §0 E8）——单行时顶栏 77px、tab 被 flex 挤到 48px 宽、标签折成**两行**，
           而**没有任何元素被裁**（右侧余 16px）⇒ 这不是「修溢出」，是「别把标签挤扁」；
           再加一个 🎨 只会更扁。第二行留 `overflow-x-auto`：宁可横向滚，也别再压扁。
           ⚠️ 这一段是**另外五套主题**的头部，T8-2 一字未动。 */
        <header className="sticky top-0 z-10 border-b border-vigil-border bg-vigil-header backdrop-blur-vigil-header">
          <div className="mx-auto flex max-w-3xl items-center gap-1.5 px-4 pt-2">
            <span className="text-base font-semibold tracking-wide">🏮 VIGIL</span>
            {/* ⚠️ `ml-auto` 原来挂在 `nav` 上；撤销按钮插到它前面之后必须**挪过来**
                （同一行里只有一个 `ml-auto` 起作用，两个都在会变成两段式布局）。 */}
            <button
              onClick={undo}
              disabled={!online}
              className="ml-auto rounded-lg px-2 py-1 text-xs text-vigil-fg-3 active:bg-vigil-surface disabled:opacity-40"
            >
              ↶ 撤销
            </button>
            {undoMsg !== null && (
              <span className="max-w-[9rem] truncate text-xs text-vigil-fg-4">{undoMsg}</span>
            )}
            <ThemePicker value={theme} onChange={pickTheme} />
          </div>
          <nav className="mx-auto flex max-w-3xl gap-1 overflow-x-auto px-4 pb-2 pt-1 text-sm">
            {TABS.map((t) => (
              <button
                key={t.key}
                onClick={() => setTab(t.key)}
                className={
                  'shrink-0 rounded-lg px-3 py-1.5 transition-colors ' +
                  (tab === t.key
                    ? 'bg-vigil-surface-2 text-vigil-fg'
                    : 'text-vigil-fg-3 active:bg-vigil-surface')
                }
              >
                {t.label}
              </button>
            ))}
          </nav>
        </header>
      )}

      {/* ⚠️ 离线横幅**一处**、在**头部下方**（brief Step 6 / G10 / 出口标准 2/3）。
          ⚠️ 全仓只此一处：AR GUS 头部与默认头部里各放一条的话，同一句话就有了
          两份真相（改一处漏一处），而且用户在同一个时刻**只看得到一条**
          （当前主题只有一套头部）——判据于是还得写两个分支去够它，是纯负荷。 */}
      {!online && (
        <div className="border-b border-vigil-border bg-vigil-warn-soft">
          <p className="mx-auto max-w-3xl px-4 py-1 text-xs text-vigil-warn">
            离线：显示的是本地快照，写操作不可用。
          </p>
        </div>
      )}

      <main className="mx-auto max-w-3xl px-4 py-4 pb-24">
        {tab === 'feed' && (
          <Feed
            kinds={kinds}
            onKinds={setKinds}
            persons={persons}
            onPersons={setPersons}
            reloadSignal={dataTick}
          />
        )}
        {tab === 'cats' && (
          <Categories
            onPick={(slug) => {
              // ⚠️ 用 `setKinds([slug])`（**替换**而不是追加）：从类目页点进来
              // 是"我要看这一类"，追加会得到「上一次的筛选 ∧ 这一次的」这种
              // 没人要的结果。
              setKinds([slug])
              setTab('feed')
            }}
          />
        )}
        {tab === 'digests' && <Digests />}
        {tab === 'calendar' && <Calendar />}
        {tab === 'settings' && <Settings />}
      </main>
    </div>
  )
}
