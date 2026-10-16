/**
 * 在线/离线状态机 —— 应用层的**单一真相源**。
 *
 * ⚠️ **为什么不能只信 `navigator.onLine`**（spec §3.4 + 本仓实测）：
 * 它只说"有没有网络链路"，不说"后端可达"。本机实测过的真形态：
 * Tailscale 转发活着、后端没起 ⇒ 浏览器认为在线（onLine === true），
 * 而每个 `/api/` 请求都 502。只看事件的实现会在那一刻显示"在线"，
 * 然后每个页面都报错——用户看到的是一堆莫名其妙的失败。
 *
 * ⇒ 本模块收**两路**信号：
 *   ① 浏览器的 `online` / `offline` 事件（**提前量**：立刻、但可能不准）；
 *   ② `noteFetchResult()`（**事实**：由 `api.getJSON` 按每个请求的真实结局上报）。
 * 后者赢：它描述的是"刚刚真的发生了什么"。
 *
 * ⚠️ 这不是"设置对了 ≠ 行为对了"（M4 二十五节）的复现，而是**它的解法**：
 * 设置（onLine）与行为（请求成败）**都要看**，且以行为为准。
 */

type Listener = () => void

function initial(): boolean {
  // SSR / 测试环境没有 navigator 时，按"在线"起步（宁可先试一次网络）
  return typeof navigator === 'undefined' ? true : navigator.onLine
}

let online = initial()
const listeners = new Set<Listener>()

function set(next: boolean): void {
  if (next === online) return
  online = next
  // ⚠️ 复制一份再遍历：监听器里退订会让 Set 在迭代中变化
  for (const l of [...listeners]) l()
}

export function isOnline(): boolean {
  return online
}

export function subscribeConnectivity(fn: Listener): () => void {
  listeners.add(fn)
  return () => {
    listeners.delete(fn)
  }
}

/** 由 `api.getJSON` 在每个请求结束后上报（`ok` = 没抛且 status < 500）。 */
export function noteFetchResult(ok: boolean): void {
  set(ok)
}

if (typeof window !== 'undefined') {
  window.addEventListener('online', () => set(true))
  window.addEventListener('offline', () => set(false))
}

/** 只有测试用:模块级状态会跨用例串。 */
export function _resetForTests(): void {
  online = initial()
  listeners.clear()
}
