/**
 * CSS 时间值的**单位安全**解析：`'360ms'` / `'.36s'` ⇒ 毫秒数;读不到 ⇒ `null`。
 *
 * ⭐⭐ 这个函数存在的**唯一理由**(M7 冒烟 F1,真缺陷):
 * 浏览器把 `getComputedStyle` 读到的 CSS 自定义属性**原样**给出,但**构建产物里的字面量
 * 已经被压缩器改写过了**——`vite build`(esbuild) 会把 `--v-ripple-dur: 360ms` 压成
 * `--v-ripple-dur:.36s`(实测,见 `dist/assets/index-*.css`)。
 * 于是 `Number.parseFloat('.36s')` = **0.36**,而调用点把它当**毫秒**用
 * ⇒ WAAPI 收到 `duration: 0.36`(= 0.36ms,肉眼看不见)⇒ material / vaporwave / glass
 * 三套主题的涟漪在**生产构建下等于不存在**。
 *
 * ⚠️ **为什么这个坑只能靠单测钉住**(不是"偷懒不写端到端"):
 *   · `vitest`(jsdom) **不加载 CSS** ⇒ `getComputedStyle` 上读到的是空串,这条路径在
 *     单测里**根本不会被执行到**;
 *   · `npm run dev` 送的是**源码** `src/index.css`(里面是 `360ms`)⇒ dev 下解析出来是 360,
 *     **看起来是对的**。
 *   ⇒ 只有"`vite build` 之后的压缩 CSS"才带着 `.36s` 这个形态,而指标(涟漪看得见)
 *     归**冒烟**那一层核(已实测过一次:改 `300s` 后"有涟漪的帧数 2 → 89")。
 *     这里能做的、也必须做的,是**把解析器本身钉死**。
 *
 * ⚠️ 支持的单位**只有** `ms` / `s`(CSS 时间类型的全部单位);裸数字(`'300'`)、
 * 多值(`'1s 2s'`)、空串、`'abc'`、`'ms'`、`'s'` 一律 **`null`**——**不猜**。
 * 调用点取到 `null` 时回落到自己的**安全默认值**(而不是把 `NaN` 或半个数字喂给 WAAPI)。
 * ⚠️ 负值合法(CSS 允许 `-5ms`,语义是"该过渡不生效/立即"),在数学上是同一个值,照收。
 */
export function cssTimeToMs(raw: string): number | null {
  // 允许前后空白与数字/单位之间的空白(`' 480 ms '`)——`getComputedStyle` 在某些
  // 引擎上会插入空格,而 `trim()` 只能处理首尾。
  const m = /^\s*([+-]?(?:\d+\.?\d*|\.\d+))\s*(ms|s)\s*$/i.exec(raw)
  if (m === null) return null
  const n = Number.parseFloat(m[1])
  if (!Number.isFinite(n)) return null
  // ⚠️ 四舍五入到 1e-6 毫秒:挡掉 `0.07s * 1000 = 70.00000000000001` 这类
  // 二进制浮点残渣,免得它出现在断言与调试输出里(值本身没变,只是好看/可比)。
  const ms = m[2].toLowerCase() === 's' ? n * 1000 : n
  return Math.round(ms * 1e6) / 1e6
}
