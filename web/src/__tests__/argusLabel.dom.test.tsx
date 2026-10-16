// ⚠️ brief 原本还 import 了 `act`——**没用到**，而本仓 `tsconfig.json` 开着 `noUnusedLocals`
// ⇒ `tsc -b`（`npm run build` 会跑）直接让构建失败（实测：
// `error TS6133: 'act' is declared but its value is never read.`，EXIT=1）。
// ⇒ 去掉这一个没用到的名字（**三条用例的正文一字未动**）。
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, test, vi } from 'vitest'
import ArgusLabel from '../components/ArgusLabel.tsx'

/**
 * ⚠️ **不用假定时器**（执行前必读，T7 的教训）：`vi.useFakeTimers()` 与 `userEvent` 是已知会互相咬的组合
 * （userEvent 内部要推进计时器，默认配置下点击会卡住）。这里的做法是**把 `ms` 调小 + 用真实计时器**：
 * 一样确定性、零配置陷阱。
 * ⚠️ 另：jsdom 里**没有 `matchMedia`**（T7 grep 实证：jsdom@30.1.0 整个 lib 里 0 处）⇒ 别 `vi.spyOn(window, 'matchMedia')`
 * （会报 `can only spy on a function`）；本组件也不需要它。
 */

test('挂载时**不是**真名（乱码），到时后显名', async () => {
  render(<ArgusLabel ms={60} />)
  expect(screen.queryByText('Project ARGUS')).toBeNull()
  const garbled = screen.getByTestId('argus-label').textContent ?? ''
  expect(garbled.length).toBe('Project ARGUS'.length) // 长度先对齐，避免布局跳
  expect(await screen.findByText('Project ARGUS', undefined, { timeout: 2000 })).toBeTruthy()
})

test('⭐ 乱码期间点一下 ⇒ 立即显名，而且**不冒泡**（不能顺手把主题选了）', async () => {
  const onParent = vi.fn()
  render(
    <div onClick={onParent}>
      <ArgusLabel ms={10000} />   {/* 长到"不会自己揭晓"，于是这次揭晓只能来自点击 */}
    </div>,
  )
  await userEvent.click(screen.getByTestId('argus-label'))
  expect(screen.getByText('Project ARGUS')).toBeTruthy()
  expect(onParent).not.toHaveBeenCalled()
})

test('揭晓之后**不再有定时器在跑**（不许留着）', async () => {
  vi.spyOn(globalThis, 'clearInterval')
  render(<ArgusLabel ms={60} />)
  await screen.findByText('Project ARGUS', undefined, { timeout: 2000 })
  expect(vi.mocked(globalThis.clearInterval).mock.calls.length).toBeGreaterThan(0)
  vi.restoreAllMocks()
})

/**
 * ⭐⭐ **承重的那一条**（T8-3 实测出来的补充，不是抄计划）。
 *
 * ⚠️ 为什么上面第三条**不算数**（这是实测，不是推理）：把 `ArgusLabel` 的 cleanup 整段
 * （`clearInterval` + `clearTimeout`）删掉，第三条**照样绿**——因为 `findByText` 内部的
 * `waitFor` 自己会 `setInterval` 轮询、再 `clearInterval` 收尾（实测那次 `CALLS=1`，
 * 调用栈落在 `@testing-library/dom/dist/wait-for.js:103` 的 `onDone`）⇒ 那句
 * `calls.length > 0` 量的是**测试工具自己**，与"揭晓后有没有留下定时器"无关。
 * 本仓同族老账：「探针被写进被测物内部」／「判据够不到它声称的那件事」。
 *
 * ⚠️ 这条怎么才算承重，两处都关键：
 *   ① 窗口里**不能出现 `waitFor`**：改用**裸 `setTimeout`** 等揭晓（不经过 RTL 的轮询）
 *      ⇒ 那个窗口里"起的 interval"只可能来自组件。实测：**恰好 1 起 1 清**。
 *   ② **不数次数、按 id 认领**：`setInterval` 返回的那个 id 必须出现在 `clearInterval`
 *      的实参里。⛔ "至少有一个 interval 被清了"仍是不够的（同一个假绿形态）——
 *      这里要求**这个窗口里起的每一个** interval 都被清掉。
 * ⚠️ 与第三条同一条纪律：**真实计时器**，不用 `vi.useFakeTimers()`。
 */
test('⭐⭐ 揭晓后**组件自己起的那个 interval** 被按 id 清掉（第三条实测不承重，这条才承重）', async () => {
  const si = vi.spyOn(globalThis, 'setInterval')
  const ci = vi.spyOn(globalThis, 'clearInterval')
  render(<ArgusLabel ms={60} />)
  // ⛔ 不用 `findByText`：它的 `waitFor` 会自己 setInterval + clearInterval，混进统计里
  await new Promise((r) => {
    setTimeout(r, 300)
  })
  expect(screen.getByText('Project ARGUS'), '先确认"确实揭晓了"，否则这条是空的').toBeTruthy()
  const ids = si.mock.results.map((r) => r.value)
  const cleared = ci.mock.calls.map((c) => c[0])
  expect(ids.length, '这个窗口里没起过 interval ⇒ 判据无从谈起（spy 没生效？）').toBeGreaterThan(0)
  for (const id of ids) {
    expect(cleared, `起的 interval（id=${String(id)}）没被 clearInterval 清掉`).toContain(id)
  }
  vi.restoreAllMocks()
})
