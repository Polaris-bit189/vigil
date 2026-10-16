import { useEffect, useState } from 'react'

const GLYPHS = '▚▞▓▒░█▄▀◣◢◤◥╳╱╲#$%&@0123456789'

function scramble(n: number): string {
  let out = ''
  for (let i = 0; i < n; i += 1) out += GLYPHS[Math.floor(Math.random() * GLYPHS.length)]
  return out
}

/**
 * 主题切换里的「先乱码、后显名」。
 *
 * ⚠️ 三条约束：
 * 1. **只能是 `<span>`**：菜单项本身已经是 `<button role="menuitemradio">`，HTML 不许 button 嵌 button。
 *    ⇒ 跳过用的点击挂在 span 上 + `stopPropagation()`。
 *    ⚠️⭐ 那条 `stopPropagation()` **只在乱码期生效**：乱码期点它是"**揭晓**"（不选中），
 *    **揭晓之后**点它是"**选中**"（正常冒泡到菜单项）。此前它是无条件的，于是揭晓后
 *    这一行仍点不中（冒烟 F2）——注释说的本来是"乱码期间"，代码做的是"永远"，
 *    又一处「结论对、依据错」。
 *    无障碍的键盘路径 = 直接 Enter 选中该主题（乱码是**装饰**，不是功能）。
 * 2. 每次挂载都演一遍（用户的要求："每次都演"）⇒ 不需要 localStorage 开关。
 * 3. 显名后**必须清掉定时器**（判据在 `argusLabel.dom.test.tsx` 第三条：**观察
 *    `clearInterval` 被调用**——不是数 `vi.getTimerCount()`，那需要假定时器，
 *    而 `vi.useFakeTimers()` 与 `userEvent` 是已知会互相咬的组合）。
 */
export default function ArgusLabel({ text = 'Project ARGUS', ms = 2500 }: { text?: string; ms?: number }) {
  const [revealed, setRevealed] = useState(false)
  const [garbled, setGarbled] = useState(() => scramble(text.length))

  useEffect(() => {
    if (revealed) return
    const spin = window.setInterval(() => setGarbled(scramble(text.length)), 90)
    const done = window.setTimeout(() => setRevealed(true), ms)
    return () => {
      window.clearInterval(spin)
      window.clearTimeout(done)
    }
  }, [revealed, ms, text.length])

  return (
    <span
      data-testid="argus-label"
      title={revealed ? undefined : '点一下直接揭晓'}
      onClick={(e) => {
        // ⭐⭐ M7 冒烟 F2（真缺陷）：**乱码期是"揭晓"，揭晓后是"选中"**。
        // ⚠️ `stopPropagation` 的意图从来就只是"乱码期间点一下 = 揭晓，别顺手把主题选了"
        // （见文件顶部约束 1），但它此前是**无条件**的 ⇒ 揭晓之后这个 span 继续吞掉点击，
        // 而菜单项（`button role="menuitemradio"`）靠冒泡上来的 click 触发 `onChange`
        // ⇒ 点标签正中间选不中主题（可点区只剩按钮右侧那条窄边）。
        // ⇒ 只有**还没揭晓**时才拦。揭晓之后让点击正常冒泡 = 像别的菜单项一样选中。
        if (revealed) return
        e.stopPropagation()
        setRevealed(true)
      }}
    >
      {revealed ? text : garbled}
    </span>
  )
}
