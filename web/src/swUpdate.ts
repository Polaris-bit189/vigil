/**
 * 「有新版本」的信号源——**故意不 import `virtual:pwa-register`**。
 *
 * ⚠️ 实测（M7 计划期空针）：vitest 解析不了那个虚拟模块
 * （`TypeError: … file:///@vite-plugin-pwa/virtual:pwa-register/react`）。
 * ⇒ 能被测试的部分必须与它解耦：`main.tsx` 是**唯一**的接线点，
 * 而这个文件是可测的纯状态源。
 */
let needRefresh = false
let apply: (() => void) | null = null
const listeners = new Set<() => void>()

export function markNeedRefresh(fn: () => void): void {
  needRefresh = true
  apply = fn
  for (const l of listeners) l()
}

export function applyUpdate(): void {
  apply?.()
}

export function getNeedRefresh(): boolean {
  return needRefresh
}

export function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** 只有测试用：模块级状态会跨用例串（前一例标记过，后一例就看到提示）。 */
export function _resetForTests(): void {
  needRefresh = false
  apply = null
  listeners.clear()
}
