import { useEffect, useState } from 'react'
import { addPerson, fetchPersons, removePerson } from '../api.ts'
import { isOnline } from '../connectivity.ts'
import { readSnapshot } from '../snapshot.ts'
import type { Person } from '../types.ts'

/**
 * 「按人物筛选」——一个**与类目体系并列的第二维度**。
 *
 * ⚠️ 与类目的关系是：**维度内并集、维度间交集**（spec §四 #4）。
 * 这个组件只管"选了哪些人"，拼 WHERE 是后端的事——前端不要试图
 * 把两个维度揉成一个 `q` 或一个 `kind`，那会让交集变成并集。
 *
 * ⚠️ 三态必须分开（与 Feed 同一条规矩）：还没读到 / 读到了 0 个人 / 读取失败。
 * 把「读失败」画成「一个人都没有」会让用户以为名单被清空了。
 */
export default function PersonPicker({
  selected,
  onChange,
  offline,
}: {
  selected: number[]
  onChange: (uins: number[]) => void
  /**
   * 离线（G9 / spec §3.3 / 出口标准 3）：**新增/删除人物是写操作**，
   * 离线时必须置灰并说明原因。
   *
   * ⚠️ **可选**，且判据一律写成 `=== true`（与 `ItemCard` 逐字同一条规矩）：
   * 缺省是 `undefined` ⇒ 每一处判断都为假 ⇒ 与加这个 prop 之前**一字不差**。
   *
   * ⚠️ 只影响**写**的两处（`新增` 与每个人物后面的 `×`）：左半边那个
   * 「选中/取消选中」按钮改的是**本地的筛选条件**（`onChange`），不发任何请求
   * ⇒ 离线时它照常可用（把筛选也灰掉是另一回事：那会让离线用户连
   * "看某个人发的东西"都做不到——而这条路径完全不需要网络）。
   */
  offline?: boolean
}) {
  const [people, setPeople] = useState<Person[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  const [open, setOpen] = useState(false)
  const [uin, setUin] = useState('')
  const [label, setLabel] = useState('')
  const [busy, setBusy] = useState(false)

  function reload() {
    setErr(null)
    fetchPersons()
      .then((r) => setPeople(r.persons))
      .catch((e: unknown) => {
        // ⚠️ 离线时**从快照取**名单,而不是清成 `null`(T6 的第二个消费者):
        // `null` 的后果是整块人物筛选器消失——与"类目条只剩「全部」"同一个洞,
        // 只是换了个组件。列表为空(`people.length === 0`)与"读不到"是两件事,
        // 这里只解决后者。
        //
        // ⚠️ 只在**离线**时用快照(在线 4xx 见 Feed 的同一条判据:回落到旧名单
        // = 把一次失败谎报成一次成功)。
        // ⚠️ 快照里那份是**上次在线时的名单**:离线期间新加/删的人物不会在这里
        // 反映出来——那是离线本身的边界,横幅会说明这是快照。
        const snap = isOnline() ? null : readSnapshot()
        if (snap !== null) {
          // 快照取到了 ⇒ 有名单可用,错误横幅就不再出现(否则屏幕上会同时是
          // "一份名单"与"读取失败",两句互相矛盾的话)。
          setErr(null)
          setPeople(snap.persons)
          return
        }
        setErr(e instanceof Error ? e.message : String(e))
        setPeople(null)
      })
  }

  useEffect(reload, [])

  async function add() {
    const n = Number(uin.trim())
    if (!Number.isInteger(n) || n <= 0) {
      setErr('QQ 号必须是正整数（0 不是有效账号）')
      return
    }
    if (!label.trim()) {
      setErr('给它起个名字——群昵称会变，这个名字不会')
      return
    }
    setBusy(true)
    setErr(null)
    try {
      await addPerson(n, label.trim())
      setUin('')
      setLabel('')
      reload()
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  async function drop(n: number) {
    setBusy(true)
    setErr(null)
    try {
      await removePerson(n)
      // 从当前筛选里也摘掉——否则筛选项还留着一个已经不存在的人，
      // 列表会一直空着，而用户不知道是为什么
      onChange(selected.filter((u) => u !== n))
      reload()
    } catch (e: unknown) {
      setErr(e instanceof Error ? e.message : String(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="mt-2">
      <button
        onClick={() => setOpen((v) => !v)}
        className="rounded-full bg-vigil-surface px-3 py-1 text-xs text-vigil-fg-3"
      >
        👤 按人物筛选
        {selected.length > 0 ? `（已选 ${selected.length}）` : ''}
      </button>

      {open && (
        <div className="mt-2 rounded-xl bg-vigil-surface p-3 shadow-vigil-card backdrop-blur-vigil-panel">
          {err !== null && (
            <p className="mb-2 text-xs text-vigil-danger">{err}</p>
          )}
          {people === null && err === null && (
            <p className="text-xs text-vigil-fg-4">读取中…</p>
          )}
          {people !== null && people.length === 0 && (
            <p className="text-xs text-vigil-fg-4">
              还没有监视任何人。在下面加一个 QQ 号。
            </p>
          )}
          {people !== null && people.length > 0 && (
            <div className="flex flex-wrap gap-1.5">
              {people.map((p) => {
                const on = selected.includes(p.uin)
                return (
                  <span key={p.uin} className="inline-flex items-center">
                    <button
                      onClick={() =>
                        onChange(
                          on
                            ? selected.filter((u) => u !== p.uin)
                            : [...selected, p.uin],
                        )
                      }
                      className={
                        'rounded-l-full px-3 py-1 text-xs ' +
                        (on
                          ? 'bg-vigil-accent-soft text-vigil-accent-text'
                          : 'bg-vigil-surface text-vigil-fg-3')
                      }
                    >
                      {p.label}（{p.count}）
                    </button>
                    <button
                      onClick={() => drop(p.uin)}
                      // ⚠️ 删人是**写**操作：离线时置灰（G9 / 出口标准 3）。
                      // ⚠️ `disabled:opacity-40` 是「置灰」的一半——只加 `disabled`
                      // 属性的话这个 × 在视觉上与可点的一模一样（它的类名里本没有
                      // 禁用态样式），用户看到的是"点不动但看不出为什么"。
                      disabled={busy || offline === true}
                      title={`不再监视 ${p.uin}`}
                      className="rounded-r-full bg-vigil-surface px-2 py-1 text-xs text-vigil-fg-4 active:bg-vigil-surface-2 disabled:opacity-40"
                    >
                      ×
                    </button>
                  </span>
                )
              })}
            </div>
          )}

          <div className="mt-3 flex flex-wrap gap-1.5">
            <input
              value={uin}
              onChange={(e) => setUin(e.target.value)}
              inputMode="numeric"
              placeholder="QQ 号"
              className="w-28 rounded-lg bg-vigil-inset px-2 py-1 text-xs outline-none placeholder:text-vigil-fg-4"
            />
            <input
              value={label}
              onChange={(e) => setLabel(e.target.value)}
              placeholder="名字（辅导员张老师）"
              className="flex-1 rounded-lg bg-vigil-inset px-2 py-1 text-xs outline-none placeholder:text-vigil-fg-4"
            />
            <button
              onClick={add}
              // ⚠️ 加人是**写**操作：离线时置灰（G9 / 出口标准 3）。
              disabled={busy || offline === true}
              className="rounded-lg bg-vigil-accent-soft px-3 py-1 text-xs text-vigil-accent-text disabled:opacity-40"
            >
              新增
            </button>
            {/* ⚠️ 置灰之外**必须**说一句「需要联网」：只置灰的话，用户看到的是
                一个点不动的按钮，而不知道是"这里坏了"还是"我现在没法改"
                （与 `ItemCard` 的操作条同一条规矩，逐字复用那句话）。 */}
            {offline === true && (
              <span className="text-vigil-fg-4">需要联网</span>
            )}
          </div>
        </div>
      )}
    </div>
  )
}
