import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test, vi } from 'vitest'
import SwUpdateToast from '../components/SwUpdateToast.tsx'
import { _resetForTests, markNeedRefresh } from '../swUpdate.ts'

afterEach(() => _resetForTests()) // ⚠️ 模块级状态必须逐用例清（M6 的 #27 就是漏了这个）

test('没有新版本 ⇒ 不出提示', () => {
  render(<SwUpdateToast />)
  expect(screen.queryByText(/有新版本/)).toBeNull()
})

test('先挂载后收到通知 ⇒ 提示出现（真实时序：页面开着，SW 才更新）', async () => {
  const apply = vi.fn()
  render(<SwUpdateToast />)
  markNeedRefresh(apply)
  expect(await screen.findByText(/有新版本/)).toBeTruthy()
  await userEvent.click(screen.getByRole('button', { name: '立即刷新' }))
  expect(apply).toHaveBeenCalledTimes(1)
})

test('先收到通知后挂载 ⇒ 提示也在（顺序不该决定看不看得见）', () => {
  markNeedRefresh(() => {})
  render(<SwUpdateToast />)
  expect(screen.getByText(/有新版本/)).toBeTruthy()
})
