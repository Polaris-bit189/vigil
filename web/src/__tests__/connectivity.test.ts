import { afterEach, expect, test, vi } from 'vitest'
import {
  _resetForTests,
  isOnline,
  noteFetchResult,
  subscribeConnectivity,
} from '../connectivity.ts'

afterEach(() => {
  // ⚠️ 顺序要紧:先还原 stub,再复位模块状态。反过来的话
  // `_resetForTests()` 会拿**被 stub 的** navigator 当真相,把 `online`
  // 复位成一个 stub 一撤就失效的值,于是串味到下一个用例。
  vi.unstubAllGlobals()
  _resetForTests()
})

test('初值取自 navigator.onLine', () => {
  _resetForTests()
  // jsdom 里 navigator.onLine 默认为 true
  expect(isOnline()).toBe(true)
})

test('⚠️ initial() 真的读 navigator.onLine——先造一个 false 出来', () => {
  // 上一条在 jsdom 默认(navigator.onLine === true)下只断言 `isOnline() === true`,
  // 而"读了 navigator.onLine"与"硬编码 true"**在该断言下不可区分** ⇒ 把
  // `initial()` 改成 `return true` 照样绿,那条约束其实没被验证(review F1)。
  // 这里把 navigator 伪造成 onLine=false,再重置模块状态,断言初值随之为 false。
  vi.stubGlobal('navigator', { onLine: false })
  expect(navigator.onLine).toBe(false) // 先证实 stub 真的生效(jsdom 没有拦写)
  _resetForTests() // 让模块状态回到 initial()
  expect(isOnline()).toBe(false)
})

test('⚠️ 请求失败 ⇒ 判定为不可用（不能只信 navigator.onLine）', () => {
  // 这是 Review Focus #1 的落点：本机实测过"Tailscale 转发活着、后端没起 = 502"，
  // 而 navigator.onLine 那一刻**是 true**。只看事件的实现会在本条上红。
  // ⚠️ 变异反证：把 noteFetchResult 改成空函数 ⇒ 本条必红。
  noteFetchResult(false)
  expect(isOnline()).toBe(false)
})

test('请求成功 ⇒ 回到可用', () => {
  noteFetchResult(false)
  noteFetchResult(true)
  expect(isOnline()).toBe(true)
})

test('订阅：状态变化时被通知，且只通知一次', () => {
  const fn = vi.fn()
  const off = subscribeConnectivity(fn)
  noteFetchResult(false)
  expect(fn).toHaveBeenCalledTimes(1)
  noteFetchResult(false) // 同值不变 ⇒ 不再通知
  expect(fn).toHaveBeenCalledTimes(1)
  off()
  noteFetchResult(true)
  expect(fn).toHaveBeenCalledTimes(1) // 已退订
})

test('⚠️ 退订只摘掉一个监听器(N=2,不是 clear 全部)', () => {
  // 上一条只在 N=1 下测退订:全程只有一个 `fn`,`listeners.delete(fn)` 与
  // `listeners.clear()` **不可区分** ⇒ 退订闭包改成 `clear()` 照样绿(review F3)。
  // T2 会引入多个订阅者(离线横幅 + 角标),那时"退订一个却把别人也摘了"是真缺陷。
  const a = vi.fn()
  const b = vi.fn()
  const offA = subscribeConnectivity(a)
  subscribeConnectivity(b)
  noteFetchResult(false)
  expect(a).toHaveBeenCalledTimes(1)
  expect(b).toHaveBeenCalledTimes(1)
  offA() // 只退 a
  noteFetchResult(true)
  expect(a).toHaveBeenCalledTimes(1) // a 已退订,不再被通知
  expect(b).toHaveBeenCalledTimes(2) // b 还在
})

test('浏览器 offline 事件 ⇒ 不可用；online 事件 ⇒ 可用', () => {
  window.dispatchEvent(new Event('offline'))
  expect(isOnline()).toBe(false)
  window.dispatchEvent(new Event('online'))
  expect(isOnline()).toBe(true)
})
