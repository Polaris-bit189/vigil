import { useEffect, useRef, useState } from 'react'
import { fetchCategories } from '../api.ts'
import type { Category } from '../types.ts'

export default function Categories({
  onPick,
}: {
  onPick: (slug: string) => void
}) {
  // null = 还没读到过（请求在飞 / 读取失败）。
  // 它必须与「读到了、结果是 0 个」分开：写成 cats: Category[] 的话，
  // 接口真返回空列表时页面会永远挂着「读取类目…」——把「读到了 0 个」
  // 说成「还在读」，两句话都没资格说。
  const [cats, setCats] = useState<Category[] | null>(null)
  const [err, setErr] = useState<string | null>(null)
  // 重跑请求的手动扳机：没有它，失败后就没有任何可见的恢复入口
  // （见下方「重试」按钮）。
  const [reloadTick, setReloadTick] = useState(0)
  // 请求序号：慢的旧请求晚于新请求返回时，不许覆盖新结果
  const seq = useRef(0)

  useEffect(() => {
    const mine = ++seq.current
    // 新一轮读取开始，上一次的失败结论即刻作废：留着它，重试成功后
    // 横幅会继续描述一个已经不存在的状态。
    setErr(null)
    fetchCategories()
      .then((r) => {
        if (mine !== seq.current) return
        setCats(r.categories)
      })
      .catch((e: unknown) => {
        if (mine !== seq.current) return
        setErr(e instanceof Error ? e.message : String(e))
        // 刻意**不清空 cats**：类目接口的查询条件不随用户变化（没有筛选、
        // 没有分页），上一次读到的卡片描述的还是同一个东西；清掉只会让
        // 屏幕上凭空少一块，却换不来「更新」——按重试就有新数据。
      })
  }, [reloadTick])

  return (
    <div>
      {err !== null && (
        <div className="mb-3 rounded-lg bg-vigil-danger-soft p-3 text-sm text-vigil-danger">
          <p>类目读取失败：{err}</p>
          {/* 显式的恢复入口：没有这个按钮，用户就只能靠「切走再切回」这类
              隐性绕路（App.tsx 是条件渲染，切 tab 会卸载重挂本组件）。
              它**重发请求**而不是只清 err——只清 err 会立刻渲染出
              「接口没有返回任何类目」这类没有依据的话。 */}
          <button
            onClick={() => setReloadTick((t) => t + 1)}
            className="mt-2 rounded-lg bg-vigil-danger-btn px-3 py-1.5 text-xs text-vigil-danger-text active:bg-vigil-danger-btn-active"
          >
            重试
          </button>
        </div>
      )}

      {cats === null ? (
        // 失败时横幅已经解释了原因，这里不再说「读取中」——那会是一句谎话
        err === null && <p className="text-sm text-vigil-fg-4">读取类目…</p>
      ) : cats.length === 0 ? (
        // 说的是「接口这次给了什么」，不是「世界上没有类目」——后者我们无从知道
        <p className="text-sm text-vigil-fg-4">接口没有返回任何类目。</p>
      ) : (
        <>
          {/* 数字**由实际数据推导**，不写死：配置文件主动教用户加类目，
              加完第八个，上一版的「七个类目」当场变成假话——而正确数字
              就在手边的 cats 里。走到这里 cats.length >= 1 恒成立
              （空列表与 null 两个分支在上面） */}
          <p className="mb-3 text-xs text-vigil-fg-4">
            {cats.length} 个类目 · 点一个看该类全部条目
          </p>
          <div className="grid grid-cols-2 gap-3">
            {cats.map((c) => (
              <button
                key={c.slug}
                onClick={() => onPick(c.slug)}
                className="rounded-xl bg-vigil-surface p-4 text-left transition-colors active:bg-vigil-surface-2 shadow-vigil-card backdrop-blur-vigil-panel"
              >
                <div className="text-2xl">{c.icon}</div>
                <div className="mt-1 font-medium">{c.label}</div>
                <div className="text-xs text-vigil-fg-3">{c.count} 条</div>
              </button>
            ))}
          </div>
        </>
      )}
    </div>
  )
}
