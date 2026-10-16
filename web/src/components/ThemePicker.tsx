import { useEffect, useRef, useState } from 'react'
import { THEMES, type ThemeId } from '../theme.ts'
import ArgusLabel from './ArgusLabel.tsx'

/**
 * 顶栏的紧凑切换器：一个 🎨 + 一个五项菜单。
 *
 * ⚠️ 它是**受控组件**（值在 App，不在自己手里）：主题是应用级状态，
 * 浮层只是它的一个视图——两处各持一份就会出现「菜单说材质、页面是蒸汽波」。
 */
export default function ThemePicker({
  value,
  onChange,
}: {
  value: ThemeId
  onChange: (id: ThemeId) => void
}) {
  const [open, setOpen] = useState(false)
  const box = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    function onPointer(e: PointerEvent) {
      if (box.current !== null && !box.current.contains(e.target as Node)) setOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('pointerdown', onPointer)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('pointerdown', onPointer)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <div className="relative" ref={box}>
      <button
        type="button"
        aria-label="切换主题"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((o) => !o)}
        className="rounded-lg px-1.5 py-1 text-base active:bg-vigil-surface"
      >
        🎨
      </button>
      {open && (
        <ul
          role="menu"
          aria-label="主题"
          /* ⚠️⭐ I3（delta 终审 Important）：这个钩子**今天没有消费者**——照实写。
             T8-2 加它时是"给 T8-3 用的"（原注释："那里要在这块浮层上挂乱码演出"），
             而 T8-3 最终**没有**用它：乱码标签直接渲染在菜单项**内部**（见下面
             `t.id === 'argus' ? <ArgusLabel /> : t.label` 那一行）⇒ 原注释**已过期**。
             ⚠️ 它是"布置时留下的接口"，**不是**在用的接口：全仓搜 `data-theme-picker`
             只有「这里加属性」与「`argusBrand.dom.test.tsx` 里那条判据」两处。
             ⇒ 留着它的**唯一**理由是"菜单容器没有一个稳定的机械标识"这个缺口本身；
             ⛔ 谁要清理它，连着那条判据一起删（那是**一次有意的接口退役**，不是回归）。 */
          data-theme-picker=""
          className="absolute right-0 z-30 mt-1 w-40 rounded-lg border border-vigil-border bg-vigil-surface-solid p-1 text-sm shadow-vigil-card"
        >
          {THEMES.map((t) => (
            <li key={t.id}>
              <button
                type="button"
                role="menuitemradio"
                aria-checked={t.id === value}
                onClick={() => {
                  onChange(t.id)
                  setOpen(false)
                }}
                className={
                  'w-full rounded-md px-2 py-1.5 text-left ' +
                  (t.id === value
                    ? 'bg-vigil-accent-soft text-vigil-accent-text'
                    : 'text-vigil-fg-2 active:bg-vigil-surface')
                }
              >
                {/* ⭐ T8-3：ARGUS 那一条**先乱码、后显名**（用户的要求「每次都演」）。
                    ⚠️ 乱码期间点它是"**揭晓**"而不是"选中"（`ArgusLabel` 内部 `stopPropagation`），
                    所以这个 `<button role="menuitemradio">` 里嵌的东西**只能是 span**——
                    HTML 不许 button 嵌 button（T4 的既有注释同款）。
                    无障碍的键盘路径不受影响：直接 Enter 就是选中该主题（乱码是装饰）。 */}
                {t.id === 'argus' ? <ArgusLabel /> : t.label}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}
