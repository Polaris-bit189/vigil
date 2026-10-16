import { expect, test } from 'vitest'
import { cssTimeToMs } from '../cssTime.ts'

/**
 * ⭐⭐ M7 冒烟 F1 的**判据**（真缺陷：生产构建下涟漪几乎不存在）。
 *
 * ## 为什么这条判据是**单测**、而不是端到端
 * 实测的因果链（见 `_smoke/m7-smoke-render.md` §4）：
 *   `src/index.css` 里 `--v-ripple-dur: 360ms` --(`vite build` / esbuild 压缩)-->
 *   产物 `dist/assets/index-*.css` 里 **`.36s`** --(`getComputedStyle` 原样读回)-->
 *   `Number.parseFloat('.36s')` = **0.36**（被当成毫秒）--> WAAPI `duration: 0.36`
 *   = 0.36ms --> **看不见**。
 *
 * 而这条链**在测试里走不到**：
 *   · `vitest`(jsdom) **不加载 CSS** ⇒ `getComputedStyle` 读到的是空串，`.36s` 这个
 *     形态根本不会出现在单测的运行环境里；
 *   · `npm run dev` 送的是**源码** `src/index.css`（`360ms`）⇒ dev 下解析出来是对的。
 * ⇒ 单测能钉、也必须钉的是**解析器本身**；"涟漪真的看得见"归**冒烟**那一层
 * （冒烟已实测过一次：把 token 改写成浏览器会算成 300 的形式后，"有涟漪的帧数 2 → 89"）。
 *
 * ⚠️ 下面那条 `.36s` 用例里**逐字写出**了来自生产的那个形态与它的期望值——
 * 它就是本组用例存在的理由，不是凑数的边界值。
 */

test('⭐ 生产构建里真实的形态：`.36s` ⇒ 360ms（不是 0.36）', () => {
  // ⚠️ 这个输入**不是**手编的边界值：它是 `dist/assets/index-*.css` 里
  // `--v-ripple-dur` 的实际字面量（esbuild 把 `360ms` 压成了 `.36s`）。
  // 期望值 360 = `src/index.css` 里那个 token 的原值（material 那套）。
  expect(cssTimeToMs('.36s'), '压缩器把 360ms 写成了 .36s ⇒ 必须还原成 360 毫秒').toBe(360)
})

test('ms 单位：原样是毫秒', () => {
  expect(cssTimeToMs('360ms')).toBe(360)
  expect(cssTimeToMs('300ms')).toBe(300)
  expect(cssTimeToMs('640ms')).toBe(640)
  expect(cssTimeToMs('0ms')).toBe(0)
})

test('s 单位：乘 1000（带与不带小数点前后导零都一样）', () => {
  expect(cssTimeToMs('0.36s')).toBe(360)
  expect(cssTimeToMs('.36s')).toBe(360)
  expect(cssTimeToMs('2.5s')).toBe(2500)
  expect(cssTimeToMs('1s')).toBe(1000)
  expect(cssTimeToMs('0s')).toBe(0)
  // ⚠️ 二进制浮点残渣挡在函数里（`0.07 * 1000` 在 IEEE754 下是 70.00000000000001）
  expect(cssTimeToMs('0.07s')).toBe(70)
})

test('大小写与空白：`MS` / `S` 与前后/中间空白都认', () => {
  expect(cssTimeToMs('360MS')).toBe(360)
  expect(cssTimeToMs('.36S')).toBe(360)
  expect(cssTimeToMs('  480ms  ')).toBe(480)
  expect(cssTimeToMs('480 ms')).toBe(480)
})

test('读不到就 null（**不猜**——调用点回落到自己的安全默认值）', () => {
  expect(cssTimeToMs('')).toBeNull()
  expect(cssTimeToMs('abc')).toBeNull()
  // 裸数字：没有单位 ⇒ 无法判断是毫秒还是秒 ⇒ 拒绝，而不是"当成毫秒"
  expect(cssTimeToMs('300')).toBeNull()
  expect(cssTimeToMs('.36')).toBeNull()
  // 只有单位没有数字
  expect(cssTimeToMs('ms')).toBeNull()
  expect(cssTimeToMs('s')).toBeNull()
  // CSS 时间不支持多值；`calc()` 之类也一律不认（不是白名单里那两种单位）
  expect(cssTimeToMs('1s 2s')).toBeNull()
  expect(cssTimeToMs('calc(0.36s)')).toBeNull()
  expect(cssTimeToMs('0.36em')).toBeNull()
  expect(cssTimeToMs('NaN')).toBeNull()
  expect(cssTimeToMs('Infinityms')).toBeNull()
})

test('负值合法（CSS 允许；数学上就是那个数）', () => {
  expect(cssTimeToMs('-5ms')).toBe(-5)
  expect(cssTimeToMs('-0.5s')).toBe(-500)
})

/**
 * ⭐ 变异反证（实测，原始输出见 `smoke-fix-report.md`）：
 * 把 `cssTimeToMs` 的函数体换成 `Number.parseFloat(raw)`（= 修复前的行为）⇒
 * 上面**每一条** `s` 单位的用例都红，且 `.36s` 那条报出的是 **0.36**（不是 360）。
 * ⚠️ 这正是"生产构建下涟漪是 0.36 毫秒"那个缺陷的最小复现。
 */
