import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test } from 'vitest'
import ArgusCommand from '../components/ArgusCommand.tsx'
import { clearArgusColor } from '../argusColor.ts'

afterEach(() => {
  localStorage.clear()
  clearArgusColor()
})

async function run(text: string) {
  const input = screen.getByLabelText('ARGUS 命令')
  await userEvent.clear(input)
  await userEvent.type(input, text)
  await userEvent.keyboard('{Enter}')
}

test('/color green ⇒ 写进 :root 并落盘，回一行确认', async () => {
  render(<ArgusCommand />)
  await run('/color green')
  expect(document.documentElement.style.getPropertyValue('--v-fg')).toBe('#00ff9c')
  expect(localStorage.getItem('vigil.argus.color')).toBe('#00ff9c')
  expect(await screen.findByText(/颜色已设为/)).toBeTruthy()
})

test('/color 不合法 ⇒ 回一行**错误**，且什么都不改', async () => {
  render(<ArgusCommand />)
  await run('/color zzz')
  expect(await screen.findByText(/认不出这个颜色/)).toBeTruthy()
  expect(document.documentElement.style.getPropertyValue('--v-fg')).toBe('')
})

test('/color 太暗 ⇒ 错误信息要说出"为什么"（纯黑底上读不出来）', async () => {
  render(<ArgusCommand />)
  await run('/color #101010')
  expect(await screen.findByText(/太暗/)).toBeTruthy()
})

test('未知命令 ⇒ 提示怎么用，而不是静默吞掉', async () => {
  render(<ArgusCommand />)
  await run('/nope')
  expect(await screen.findByText(/未知命令/)).toBeTruthy()
})

test('空行 Enter ⇒ 什么都不做（不报错、不写盘）', async () => {
  render(<ArgusCommand />)
  await userEvent.click(screen.getByLabelText('ARGUS 命令'))
  await userEvent.keyboard('{Enter}')
  expect(screen.queryByText(/错误|未知/)).toBeNull()
})

/**
 * ⭐ 阴性对照（本仓的老账：**全称否定 / 只验"该发生时发生了"**）：上面四条都在验
 * "有输入 ⇒ 有回话"，而"**没有输入时不许凭空说话**"缺一条——一个 `run()` 里
 * 无条件 `setMsg({text:'错误：…'})` 的实现，上面四条**照样全绿**。
 * ⚠️ 再补一条"回话是**替换**不是堆叠"：终端是一行一行刷的，四行错误提示同时挂在
 * 输入框下面，屏幕会变成一张报错清单（而那是本组件最容易写出的形态）。
 */
test('阴性对照：什么都没输入时不许有回话；且新回话**替换**旧回话（不堆叠）', async () => {
  render(<ArgusCommand />)
  expect(screen.queryByText(/颜色已设为|错误|未知/), '还没输入就有回话').toBeNull()

  await run('/color zzz')
  expect(await screen.findByText(/认不出这个颜色/)).toBeTruthy()
  await run('/color green')
  expect(await screen.findByText(/颜色已设为/)).toBeTruthy()
  expect(screen.queryByText(/认不出这个颜色/), '新回话该替换旧回话').toBeNull()
})
