import { afterEach, expect, test } from 'vitest'
import { VIEW_STORAGE_KEY, readView, writeView } from '../uiState.ts'

afterEach(() => localStorage.clear())

/**
 * ⭐ T5 review 的 Low-1（fix 轮）：本节两条用例里的**三处**期望原先写的是
 * `toEqual(DEFAULT_VIEW)` ——**自指**，等于「readView 没读到东西时返回 DEFAULT_VIEW」
 * 这一句同义反复（对象是同一个引用，比它自己永远成立）。
 * reviewer 实测：把 `uiState.ts` 的 `DEFAULT_VIEW` 改成
 * `{ tab: 'feed', kinds: ['zzz'], persons: [9] }` 之后 **5/5 照样全绿**
 * ——因为期望值跟着被测常量一起动了。空守卫的经典形状。
 * ⇒ 期望值一律**逐字写死**（下面三处 `toEqual({ tab: 'feed', kinds: [], persons: [] })`），
 * 坏掉 `DEFAULT_VIEW` 就必须红。
 * ⚠️ 变异反证：把 `uiState.ts` 的 `DEFAULT_VIEW.kinds` 改成 `['zzz']` ⇒ 本条必红。
 */
test('没存过 ⇒ 默认', () => {
  expect(readView()).toEqual({ tab: 'feed', kinds: [], persons: [] })
})

test('存取一个完整视图', () => {
  writeView({ tab: 'digests', kinds: ['notice', 'life'], persons: [12345] })
  expect(readView()).toEqual({ tab: 'digests', kinds: ['notice', 'life'], persons: [12345] })
})

test('⚠️ 逐字段校验：坏的字段回落默认，好的字段保留', () => {
  // 一个坏字段（旧版本留下的 tab 名之类）不该毁掉整份记忆——
  // 那会让"记得我选的类目"这种小事变成"每次刷新都从头来"。
  localStorage.setItem(
    VIEW_STORAGE_KEY,
    JSON.stringify({ tab: 'gallery', kinds: ['notice', 42], persons: [1, -3, 'x'] }),
  )
  expect(readView()).toEqual({ tab: 'feed', kinds: ['notice'], persons: [1] })
})

test('垃圾 JSON / 非对象 ⇒ 默认，且不抛', () => {
  // ⚠️ 期望值同样**逐字写死**（理由见文件头那条：`toEqual(DEFAULT_VIEW)` 是自指的）
  localStorage.setItem(VIEW_STORAGE_KEY, '{not json')
  expect(readView()).toEqual({ tab: 'feed', kinds: [], persons: [] })
  localStorage.setItem(VIEW_STORAGE_KEY, '42')
  expect(readView()).toEqual({ tab: 'feed', kinds: [], persons: [] })
})

test('kinds 去重、persons 去重', () => {
  localStorage.setItem(
    VIEW_STORAGE_KEY,
    JSON.stringify({ tab: 'feed', kinds: ['a', 'a'], persons: [7, 7, 8] }),
  )
  expect(readView().kinds).toEqual(['a'])
  expect(readView().persons).toEqual([7, 8])
})
