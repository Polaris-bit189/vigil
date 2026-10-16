import { afterEach, expect, test } from 'vitest'
import {
  ARGUS_COLOR_KEY,
  ARGUS_DEFAULT,
  ARGUS_NAMED,
  ARGUS_VARS_KEY,
  applyArgusColor,
  argusDerivedFailure,
  argusVarsJson,
  clearArgusColor,
  deriveArgusVars,
  parseArgusColor,
  readArgusColor,
} from '../argusColor.ts'
import { applyTheme } from '../theme.ts'

// ⚠️ Node 内置模块在本仓**必须**用「具名变量说明符」动态 import（两条实测出来的墙，
// 与 `themeTokens.test.ts` / `noRawPalette.test.ts` 顶部同一段：T1 报告 §4 偏差 4/5、M7 勘误 E9）：
//   ① 本仓没有 `@types/node`，而 `tsc -b`（`npm run build` 会跑）**会编译测试文件**
//      ⇒ `import { readFileSync } from 'node:fs'` 直接 TS2307 让构建失败；
//   ② `new URL('..', import.meta.url)` 第二个参数写成**字面量**时，Vite 的
//      asset-import-meta-url 变换会接管，基准被换成 jsdom 的 document base
//      ⇒ `readFileSync` 拿到 http: URL 后报 `ERR_INVALID_URL_SCHEME`。
// ⛔ 不用 `// @ts-expect-error`：将来真装了 @types/node 会变成 TS2578，又把构建搞坏一次。
const NODE_FS = 'node:' + 'fs'
const HERE = import.meta.url

const { readFileSync } = (await import(/* @vite-ignore */ NODE_FS)) as {
  readFileSync: (p: URL, enc: string) => string
}

afterEach(() => {
  localStorage.clear()
  clearArgusColor()
  // 本文件新增了走 `applyTheme` 的用例（F8），它会挂 `data-theme`
  // ⇒ 用完摘掉，别把状态漏给同文件的下一条（`theme.test.ts` 同款清理）。
  document.documentElement.removeAttribute('data-theme')
  // ⚠️ R10 的用例会让**防闪脚本**往 `:root` 上贴 `--v-*`。这里原来要手工收尾几个
  // "形状合法但不在派生表里"的键（`--v-bg` / `--v-old-token` / …）——⭐ F-A4 把白名单
  // 收紧成「**12 个键名** + 形状」之后，那种键**不可能**被贴上（R10⑥⑧ 各钉一半、R10⑨
  // 机械钉住两份名单相同）⇒ 会留下的只有 `deriveArgusVars` 那 12 个，
  // `clearArgusColor()` 已全覆盖，这里不再需要手工收尾。
})

test('具名色与十六进制都能解析（含缩写 #rgb）', () => {
  expect(parseArgusColor('green')).toEqual({ ok: true, value: '#00ff9c' })
  expect(parseArgusColor('white')).toEqual({ ok: true, value: '#f2f2f2' })
  expect(parseArgusColor('#ff8800')).toEqual({ ok: true, value: '#ff8800' })
  expect(parseArgusColor('#f80')).toEqual({ ok: true, value: '#ff8800' })   // 缩写要展开
  expect(parseArgusColor('  WHITE ')).toEqual({ ok: true, value: '#f2f2f2' }) // 大小写与空白都容忍
})

test('⛔ 非法输入要给出**可读的理由**，而不是静默不改', () => {
  for (const bad of ['', 'zzz', '#12', '#gg0000', 'rgb(1,2,3)f']) {
    const r = parseArgusColor(bad)
    expect(r.ok, `"${bad}" 不该被接受`).toBe(false)
    if (!r.ok) expect(r.reason.length).toBeGreaterThan(3)
  }
})

test('⛔ 太暗的颜色必须拒（纯黑底上读不出来）——判据是亮度，不是"我觉得丑"', () => {
  // #101010 的 WCAG 相对亮度约 0.005 ⇒ 与纯黑底的对比度约 1.1，绝对读不出来。
  // ⚠️ 它走的是**基色**那条闸门（`MIN_LUMINANCE`）：理由文案是"在纯黑底上读不出来"，
  // 与下面派生那条（"它派生出的 …"）**措辞不同** ⇒ 这一对断言同时钉住了"两条闸门各自存在"。
  const r = parseArgusColor('#101010')
  expect(r.ok).toBe(false)
  if (!r.ok) {
    expect(r.reason).toContain('暗')
    expect(r.reason, '这条该走**基色**闸门，不该走派生那条').toContain('纯黑底')
  }
  // ⚠️⭐ C1 的**行为变更**（delta 终审 Critical，不是放宽）：
  // `#767676`（L=0.181164，基色对纯黑 4.623:1）此前**收**，现在**拒**——
  // 因为它的基色达标、而它派生出的 `--v-fg-3` 在 `--v-surface-2` 上只有约 2.2:1、
  // `--v-fg-4` 只有约 1.5:1（弱化文字全仓 94 处）。改前的理由文案（"一个够亮的深灰"）
  // 建立在"闸门只判基色"这个前提上，而那个前提正是 C1 要拆掉的那个。
  const deep = parseArgusColor('#767676')
  expect(deep.ok, '#767676 派生出的弱化文字读不出来 ⇒ 现在必须拒').toBe(false)
  if (!deep.ok) expect(deep.reason, '这条该走**派生**闸门').toContain('派生出的')
})

/**
 * ⭐ F3②（T8-1 fix 轮）：**把亮度闸门钉到边界上**。
 *
 * 为什么需要：审查实测 `MIN_LUMINANCE` 只被钉在 **(0.0052, 0.1812]**（35 倍宽）里——
 * 改成 0.02（= 1.4:1，闸门形同虚设）或 0.18，全量 113 条**一条不红**。而"阈值取
 * 4.5:1 反推"这句论证写在代码注释里，**注释不是判据**。
 *
 * 算式（灰色 ⇒ 三通道相同 ⇒ WCAG 相对亮度的三个权重和为 1，L = f(c)，f 见 `argusColor.ts`）：
 *   L(c) = ((c/255 + 0.055) / 1.055) ** 2.4  ≥  0.175
 *   ⇒ (c/255 + 0.055) / 1.055 ≥ 0.175 ** (1/2.4) = 0.48372572806426119
 *   ⇒ c/255 ≥ 0.48372572806426119 × 1.055 − 0.055 = 0.45533064310779561
 *   ⇒ c ≥ 116.10931399248784
 * ⇒ **116 是最大的"不够"、117 是最小的"够"**（相邻两档，差 1/255）：
 *   `#747474` L = 0.174647404（对比度 4.4929:1）**拒**；`#757575` L = 0.177888416（4.5578:1）**收**。
 * ⚠️ 用这一对而不是"随便找个深色"：常量往**任一方向**挪一点点，必有一条翻。
 */
test('⭐ F3②：亮度闸门钉在边界上（#747474 拒）——边界仍在，但"收"的那一侧改由派生闸门定', () => {
  const just_below = parseArgusColor('#747474')
  expect(just_below.ok, '#747474（L=0.174647404 < 0.175）必须拒——常量调高一点点就会漏过它').toBe(false)
  // ⚠️⭐ C1 的**行为变更**：这条边界上"收"的那一侧原来是 `#757575`，现在它被**派生**闸门拒掉了
  // （基色 4.5578:1 达标，但它派生出的 `--v-fg-3` = 2.1514:1、`--v-fg-4` = 1.5134:1）。
  // ⇒ "收"的那一侧改由**派生闸门的边界本身**见证——灰阶里它落在 `#9a9a9a` / `#9b9b9b` 之间
  //   （基色那关早已不是最紧的那一关：这两档的基色 7.46/7.56 远超 0.175 那个下界）。
  //   `#9a9a9a` 的 `--v-fg-3` = **2.9957**（< 3，拒）｜`#9b9b9b` = **3.0210**（≥ 3，收）
  //   ——相邻两档、只差 1/255，判据的**强度没变弱**，变的是它在哪一关上。
  expect(parseArgusColor('#9a9a9a').ok, '#9a9a9a fg-3=2.9957 < 3 ⇒ 拒').toBe(false)
  expect(parseArgusColor('#9b9b9b').ok, '#9b9b9b fg-3=3.0210 ≥ 3 ⇒ 收').toBe(true)
  // 而"刚好够亮的灰"今天**不是一个可接受的基色** ⇒ 记在这里，免得下一个人以为它被漏掉了。
  expect(parseArgusColor('#757575').ok, '#757575 基色达标但派生值不达标 ⇒ 拒').toBe(false)
})

/**
 * ⭐ F3①：**12 个值逐条写死**（`#204060` 这个基色，值一律照 `argusColor.ts` 抄死）。
 *
 * 为什么需要：审查实测 `deriveArgusVars` 的 9 个 alpha 里有 **6 个是"无判据的常量"**
 * （`--v-fg-3/4`、`--v-surface`、`--v-surface-2`、`--v-border-soft`、`--v-accent-soft`）——
 * 一起改成 0.99 全量 113 条**全绿**，而屏幕上 `--v-fg-3` 落在 `--v-surface-2` 上的
 * 对比度从 **5.09 崩到 1.01**（几乎看不见）。表驱动把这些数**逐个**钉住（G8）。
 * ⚠️ `toEqual` 顺带钉住**键集合与个数**（多/少/改名都会红）——这正是 `clearArgusColor`
 * 那条用例覆盖面所依赖的同一张表。
 */
test('⭐ F3①：派生结果与 12 项期望表逐条相等（每个 alpha 都有判据）', () => {
  const WANT: Record<string, string> = {
    '--v-fg': '#204060',
    '--v-fg-2': 'rgba(32, 64, 96, 0.78)',
    '--v-fg-3': 'rgba(32, 64, 96, 0.58)',
    '--v-fg-4': 'rgba(32, 64, 96, 0.38)',
    '--v-surface': 'rgba(32, 64, 96, 0.06)',
    '--v-surface-2': 'rgba(32, 64, 96, 0.14)',
    '--v-inset': 'rgba(32, 64, 96, 0.03)',
    '--v-border': 'rgba(32, 64, 96, 0.45)',
    '--v-border-soft': 'rgba(32, 64, 96, 0.22)',
    '--v-accent': '#204060',
    '--v-accent-soft': 'rgba(32, 64, 96, 0.18)',
    '--v-accent-text': '#204060',
  }
  const v = deriveArgusVars('#204060')
  expect(Object.keys(v)).toHaveLength(12)
  expect(v).toEqual(WANT)
  // ⛔ 不许出现 color-mix / var() —— 判据解析不了它们（见 §0.1）
  for (const val of Object.values(v)) expect(val).not.toContain('color-mix')
})

/**
 * ⭐ F2（= 计划的 **R11**）：`index.css` 的 argus 块 ≡ `deriveArgusVars(ARGUS_DEFAULT)`。
 *
 * ⚠️ **这条比的语义**：CSS 块里那份值与 JS 函数算出来那份**必须机械对上**——
 * 这**不是**"拿实现去验实现"（两边是**两处独立写下的真相**：一处是 CSS 字面值、
 * 一处是 TS 里的 alpha 表），而是"**两份默认值不许漂移**"。
 *
 * 为什么非要判据：① 稳态值**总是**来自 `deriveArgusVars`（`applyTheme('argus')` 每次都调
 * `applyArgusColor`）⇒ CSS 那份只在**首帧**与 `themePalette.test.ts` 里出现；
 * ② 而 `themePalette.test.ts` **只读 CSS**、不跑 JS。审查实测：把派生表的
 * `--v-surface-2` alpha 从 0.14 改成 **0.99**（CSS 一个字节不动）⇒ **全量 113 全绿**，
 * 而运行时真实对比度 `--v-fg-3` / `--v-surface-2` = **5.09 → 1.01**、`--v-accent-text` /
 * `--v-accent-soft` = 7.70 → 1.02 ⇒ **对比度判据验的是"用户看不到的那份值"**。
 *
 * ⚠️ **只比交集**：CSS 块给的是**整套 token**（parity 要求六块逐名相等），`deriveArgusVars`
 * 只算颜色这一族 ⇒ 两边数量本来就不同（评审核过：12 个键在 CSS 块里**全都有**）。
 * "交集恰好 12 个"这条断言就是"一个键名都没漂"的那个证据。
 */
const CSS = readFileSync(new URL('../index.css', HERE), 'utf8').replace(/\/\*[\s\S]*?\*\//g, '')

/** 取一张主题块的 `名 → 值` 表（解析沿用 `themeTokens.test.ts` 的「选择器 + 花括号体」粗切，先剥注释）。 */
function cssBlock(selector: string): Record<string, string> {
  const m = [...CSS.matchAll(/([^{}]*)\{([^{}]*)\}/g)].find((x) => x[1].trim() === selector)
  expect(m, `index.css 里找不到 ${selector} 块`).toBeTruthy()
  const out: Record<string, string> = {}
  for (const d of m![2].matchAll(/(--v-[a-z0-9-]+)\s*:\s*([^;]+)/g)) out[d[1]] = d[2].trim()
  return out
}

test("⭐ F2/R11：index.css 的 argus 块与 deriveArgusVars(ARGUS_DEFAULT) 必须机械对上", () => {
  const block = cssBlock("[data-theme='argus']")
  const derived = deriveArgusVars(ARGUS_DEFAULT)
  const overlap = Object.keys(derived).filter((k) => k in block)
  expect(
    overlap,
    `CSS 块与派生表的交集不是 12 个（实际 ${overlap.length}）——有键名对不上了`,
  ).toHaveLength(12)
  for (const [k, want] of Object.entries(derived)) {
    expect(block[k], `${k} 在两处不一致（CSS 字面值 vs deriveArgusVars 的输出）`).toBe(want)
  }
})

test('applyArgusColor：写 :root 行内 + 落盘；clearArgusColor 必须**每一个键**都抹掉', () => {
  const ALL = Object.keys(deriveArgusVars(ARGUS_DEFAULT))
  expect(ALL, '派生表的键数变了？下面两条断言的覆盖面跟着它走（这里只是把"12"钉住）').toHaveLength(12)
  const r = applyArgusColor('white')
  expect(r.ok).toBe(true)
  const root = document.documentElement
  expect(root.style.getPropertyValue('--v-fg')).toBe('#f2f2f2')
  expect(root.style.getPropertyValue('--v-border')).toBe('rgba(242, 242, 242, 0.45)')
  expect(localStorage.getItem(ARGUS_COLOR_KEY)).toBe('#f2f2f2')
  // ⭐ F1：先钉"写进去的是**全集**"——否则下面那条"清干净"可以靠"本来就没写"混过去
  //（两个方向都要钉：只写 3 个键的实现不该过得去，只清 3 个键的也不该）。
  for (const k of ALL) {
    expect(root.style.getPropertyValue(k), `${k} 没被 applyArgusColor 写到行内`).not.toBe('')
  }

  clearArgusColor()
  // ⭐⭐ F1（Important）：**逐键**断言，不是只问 `--v-fg`。审查实测：把循环改成只清前 3 个键
  // ⇒ **全量 113 条全绿**，而真实泄漏 9 个键——**含 `--v-accent` / `--v-accent-text` /
  // `--v-border`**，正是计划 §0.1 点名要用 `clearArgusColor` 挡住的那三个
  //（行内赢过任何选择器 ⇒ "从 ARGUS 切到 material"会把材料的强调色与边框顶成 ARGUS 的颜色）。
  // ⚠️ 用 `Object.keys(...)` 驱动、**不手抄 12 个名字**：常量表一改，这里自动跟上。
  for (const k of ALL) {
    expect(root.style.getPropertyValue(k), `${k} 没被 clearArgusColor 清掉`).toBe('')
  }
  // ⚠️ 落盘**不清**：清的是"这次生效的覆盖"，盘上那份是用户的偏好
  expect(localStorage.getItem(ARGUS_COLOR_KEY)).toBe('#f2f2f2')
})

test('applyArgusColor 失败时**什么都不改**（不能改一半）', () => {
  applyArgusColor('green')
  const before = document.documentElement.style.getPropertyValue('--v-fg')
  const r = applyArgusColor('#101010')
  expect(r.ok).toBe(false)
  expect(document.documentElement.style.getPropertyValue('--v-fg')).toBe(before)
  expect(localStorage.getItem(ARGUS_COLOR_KEY)).toBe('#00ff9c')
})

test('readArgusColor：盘上是垃圾/太暗 ⇒ 回落默认', () => {
  expect(readArgusColor()).toBe(ARGUS_DEFAULT)
  localStorage.setItem(ARGUS_COLOR_KEY, 'nonsense')
  expect(readArgusColor()).toBe(ARGUS_DEFAULT)
  localStorage.setItem(ARGUS_COLOR_KEY, '#010101')
  expect(readArgusColor()).toBe(ARGUS_DEFAULT)
  localStorage.setItem(ARGUS_COLOR_KEY, '#abcdef')
  expect(readArgusColor()).toBe('#abcdef')
})

/**
 * ⭐ F8（= 审查 F6）：**发布路径**（`applyTheme`）的判据。
 *
 * 为什么需要：`syncArgusColor` 是 `theme.ts` 的**模块私有**函数，上面那些用例都是
 * **直接调 `applyArgusColor` / `clearArgusColor`**，完全绕过 `applyTheme`
 * ⇒ "切走 ARGUS 要清干净"这条接线**零发布覆盖**：审查实测删掉 `applyTheme` 里那行
 * `syncArgusColor(id)`，全量 113 条**一条不红**，而用户看到的是
 * "`data-theme` 已是 material、屏幕上是荧光绿"。
 *
 * ⚠️ 所以这条必须走**真实入口**（`applyTheme`，也就是 `main.tsx` 与主题菜单用的那个），
 * 并且**逐键**断言——只问 `--v-fg` 的话，"清得只剩 3 个键"那种实现照样过。
 */
test('⭐ F8：经 applyTheme 切换（真实入口）——切到 material 必须清干行内变量', () => {
  applyArgusColor('white') // 盘上/行内的用户色（模拟"用户在 ARGUS 下 /color white 过"）
  // `applyTheme('argus')` 会 `applyArgusColor(readArgusColor())` ⇒ 行内是 `#f2f2f2`
  applyTheme('argus')
  const root = document.documentElement
  expect(root.getAttribute('data-theme')).toBe('argus')
  expect(root.style.getPropertyValue('--v-fg')).toBe('#f2f2f2')

  applyTheme('material')
  expect(root.getAttribute('data-theme')).toBe('material')
  expect(root.style.getPropertyValue('--v-fg'), '切到 material 后 ARGUS 的字色还压着').toBe('')
  for (const k of Object.keys(deriveArgusVars(ARGUS_DEFAULT))) {
    expect(root.style.getPropertyValue(k), `切到 material 后 ${k} 还留在行内`).toBe('')
  }
})

// ────────────────── R12（T8-1 复审的三条，controller 裁决并入 T8-2） ──────────────────

/**
 * ⭐ R12-a（= 复审 F9）：`ARGUS_NAMED` 的具名色 ↔ CSS token 的**跨文件对账**。
 *
 * 为什么需要：`argusColor.ts` 的 `red: '#ff6b61'` 与 `index.css` 的 `--v-danger: #ff6b61`
 * 是**两份手写的字面值**（用户 2026-09-29 裁决把旧值 `#ff5f56` 换成更亮的红，理由见
 * `ARGUS_NAMED` 上方那段），而 CSS 里那句注释声称它们同源
 * ——**又一处"无判据的声称"**（复审实测：全仓只此两处，且 palette 判据只读 CSS，看不这里）。
 * ⚠️ 这条比的**不是**"实现自洽"：两边是**两处独立写下的真相**（一份 TS 表、一份 CSS 字面值）
 * ⇒ 它验的是"两份不许漂"。
 */
test('⭐ R12-a：具名色 ↔ CSS token 的跨文件对账（只对账**真有声称**的那几对）', () => {
  const block = cssBlock("[data-theme='argus']")
  // ① CSS 注释点名的那个对账：`--v-danger` ≡ `ARGUS_NAMED.red`
  expect(
    block['--v-danger'],
    'argusColor.ts 的 ARGUS_NAMED.red 与 index.css 的 --v-danger 漂了',
  ).toBe(ARGUS_NAMED.red)
  // ② 默认基色 ≡ 具名 green；而 CSS 里默认基色的两个落点（正文与强调色）就是它
  expect(ARGUS_DEFAULT).toBe(ARGUS_NAMED.green)
  expect(block['--v-fg']).toBe(ARGUS_NAMED.green)
  expect(block['--v-accent']).toBe(ARGUS_NAMED.green)
  // ③ 其余四个具名色（white / amber / cyan / magenta）**没有** CSS 对账对象：
  //    `/color white` 是"把基色换成白"，不是"把 --v-warn / --v-danger 换成白"。
  //    ⚠️ 特别注明：`--v-warn: #ffbd2e` **不是** `ARGUS_NAMED.amber`（`#ffb000`）——
  //    两边**故意不同**（一个是 UI 的"提醒"、一个是终端色名）。下一个看到两个黄色的人
  //    会以为有一处漏改，这句注释就是给那一刻用的。
  // ⇒ 钉住具名色的**键集合**：加一个具名色 = 得想一次"它有没有 CSS 对账"。
  expect(Object.keys(ARGUS_NAMED).sort()).toEqual([
    'amber',
    'cyan',
    'green',
    'magenta',
    'red',
    'white',
  ])
})

/**
 * ⭐⭐ 用户 2026-09-29 裁决的机械落点：**具名色必须过得了它自己那道闸门**。
 *
 * 为什么非有不可：`ARGUS_NAMED` 把"名字 → 基色"与"这个基色能不能当基色"缝在一起，
 * 而 `parseArgusColor` 只对**传进来的字符串**走闸门 ⇒ 一个具名色的值可以**悄悄躺在
 * 闸门外**：`/color red` 是**产品对用户承诺过的入口**（`index.css` 的注释与
 * `parseArgusColor` 的用法文案都点了它的名），一旦它被拒，用户看到的是"这个名字坏了"，
 * 而**没有任何东西会红**——R12-a 只比字面值（两边一起改就永远绿）、闸门那条只判用户手输的色。
 *
 * ⚠️ 这正是 2026-09-28 的实测形态：`red: '#ff5f56'` 的**基色**在纯黑底上 7.02:1（达标），
 * 而它派生的 `--v-fg-3` = **2.85 < 3** ⇒ 同一个具名色在"名字可用"与"闸门收不收"之间裂开了，
 * 全量 150 条**一条不红**（那次是人工发现的，不是判据发现的）。用户裁定"换个更亮的红"
 * （`#ff6b61`）之后，这条断言就是那个裁定**唯一**的机械落点：
 * ⛔ 谁把 `red` 改暗一点点（连同 `index.css` 的 `--v-danger`，R12-a 会逼着一起改），
 * 它**先红**——不必再靠"下一个人看注释"。
 * ⚠️ 判据直接问**真实闸门**（`parseArgusColor`），不重算某个常数：语义是"用户能不能用
 * 这个名字"，而不是"这个值够不够亮"。
 */
test('⭐⭐ 每个具名色都必须过闸门（`/color red` 是承诺过的入口，不许被自己的闸门拒）', () => {
  for (const [name, hex] of Object.entries(ARGUS_NAMED)) {
    const r = parseArgusColor(name)
    expect(
      r.ok,
      `具名色 ${name}（${hex}）过不去自己的闸门：${r.ok ? '' : r.reason}`,
    ).toBe(true)
    if (r.ok) expect(r.value, `${name} 解析出来的不是表里那个值`).toBe(hex)
  }
})

/**
 * ⭐⭐ R12-c（= 复审 5.2，本卷最值钱的一条）：`luminance()` 的**三个 WCAG 权重本来没有判据**。
 *
 * **实测（复审）**：把前两个权重互换（`0.2126` ↔ `0.7152`）⇒ **全量 116 条全绿**。
 * 为什么能全绿：上面 F3② 那条边界见证用的是**灰**（`#747474` / `#757575`），而灰色三通道
 * 相同 ⇒ `L = (0.2126 + 0.7152 + 0.0722) · f(c) = f(c)`，**权重怎么换都是它**
 * ⇒ 那条守得住闸门的**位置**，守不住闸门的**算术**。
 *
 * **真值**（算式；本实现的公式）：
 *   f(c) = c/255 ≤ 0.03928 ? (c/255)/12.92 : ((c/255 + 0.055)/1.055) ** 2.4
 *   L = 0.2126·f(r) + 0.7152·f(g) + 0.0722·f(b)
 * ⚠️ 复审报告给的 `#d00000` 真值 **0.1312**（3.62:1）与"错权重下 0.4418（9.8:1）"是
 * **省掉 `+0.055 / 1.055` 那一步**（即拿 `(c/255) ** 2.4` 当 f）算出来的近似；
 * 本实现（含那一步）的真值是 **0.134098967**（3.6820:1）与 **0.451117504**（10.0224:1）。
 * 两边的**结论一致**（该拒 / 被错权重放行），差的只是小数第三位——记在这里，
 * 免得下一个照着 0.1312 去核而对不上（本仓"结论对、依据错"的老账）。
 *
 * **后果**：`#d00000` 真 L=0.134099 ⇒ **3.68:1，该拒**；错权重下算成 0.451118 ⇒
 * **10.02:1 放行** ⇒ 用户能把终端字色设成纯黑底上**几乎读不出来的暗红**，而全量一条不红。
 *
 * ⇒ 见证色必须**带颜色通道差异**（纯红）。边界（解 `0.2126·f(c) ≥ 0.175`）：
 *   `(c/255 + 0.055)/1.055 ≥ (0.175/0.2126) ** (1/2.4) = 0.823142… ** (1/2.4)`
 *   ⇒ `c ≥ 234.045` ⇒ **234（`#ea0000`）是最大的"不够"、235（`#eb0000`）是最小的"够"**。
 *   `L(#ea0000) = 0.174924251`（4.4985:1，**拒**）｜`L(#eb0000) = 0.176621676`（4.5324:1）。
 *
 * ⚠️⭐ C1 之后的**行为变更**：`#eb0000` 现在也是**拒**了——不是基色那关（它的基色 4.5324:1 仍达标），
 * 而是**派生**那关（`--v-fg-3` = 2.052、`--v-fg-4` = 1.428）。⇒ 上面那句"最小的够"今天只对
 * **基色**闸门成立。⚠️ 这条判据**没有变弱**：权重若被互换，`#eb0000` 的派生值会被算成
 * `--v-fg-3` = **12.97**、`--v-fg-4` = **2.44** ⇒ 两关全过、被**收** ⇒ 本条照样红。
 * （两个方向都比原来多一层：原来只靠基色那一关。）
 */
test('⭐⭐ R12-c：权重互换的见证色必须是**纯红**（灰阶见证色对这个变异是瞎的）', () => {
  // 复审点名的那个真色：它必须被拒（错权重下会被算成 10:1 放行）
  expect(
    parseArgusColor('#d00000').ok,
    '#d00000 真 L=0.134098967（3.68:1）必须拒',
  ).toBe(false)
  // 一对纯红见证：只差红通道的 1/255
  expect(parseArgusColor('#ea0000').ok, '#ea0000 L=0.174924251 < 0.175 必须拒').toBe(false)
  // ⚠️ C1：它**必须拒**，且拒它的理由文案要说"派生"
  //（权重互换时它的派生值 12.97 / 2.44 会让它被收 ⇒ 本条红）
  const r = parseArgusColor('#eb0000')
  expect(r.ok, '#eb0000 基色达标、但派生出的 fg-3=2.052 / fg-4=1.428 ⇒ 必须拒').toBe(false)
  if (!r.ok) expect(r.reason, '这条该走**派生**闸门').toContain('派生出的')
})

/**
 * ⭐⭐ C1：**闸门与派生 alpha 是同一套判据**——两个"所有者"合成一个。
 *
 * **实测（delta 终审）**：旧闸门只判基色，而 `deriveArgusVars` 把基色降透明得到
 * `--v-fg-3`(0.58) / `--v-fg-4`(0.38)，**中间那一层没人管** ⇒ `/color #757575`（**被接受**，
 * 基色 4.5578:1）派生出的 `--v-fg-3` 在 `--v-bg` 上只有 **2.1514:1**、`--v-fg-4` **1.5134:1**，
 * 而这两个是全仓**最常用的弱化色**（94 处 `text-vigil-fg-*`：未选中的 tab、群名、时间、
 * 品牌区小字）⇒ 用户手输一个合法色就能让整套弱化文字读不出来，而**146 条测试全绿**。
 *
 * ⚠️ 这条判据**不是**"把 `MIN_LUMINANCE` 调高"（那连合法的中间灰一起拒，且治不了根：
 * 仍没有判据覆盖**派生值**）——它换的是**判什么**：对**派生出来的整份配色**取最弱环。
 * ⚠️ `#757575` / `#767676` / `#eb0000` 由收改拒是**预期的行为变更**（它们本来就会产生
 * 读不出的弱化色）；用户可见的影响 = 终端里回一行错误、提示换个更亮的颜色。
 */
test('⭐⭐ C1：闸门必须判**派生出来的整份配色**（#757575 由收改拒，理由文案点名 fg-3）', () => {
  for (const bad of ['#757575', '#767676', '#757575']) {
    const r = parseArgusColor(bad)
    expect(r.ok, `${bad} 的基色达标但它派生出的弱化文字读不出来 ⇒ 必须拒`).toBe(false)
    if (!r.ok) {
      expect(r.reason, `${bad} 的错误要说清是**派生值**不行（不是基色暗）`).toContain('--v-fg-3')
      expect(r.reason, '错误里要给出可操作的建议（换个更亮的颜色）').toContain('更亮')
    }
  }
  // 阴性对照：**默认色与它的邻居们必须照样收**（收紧不能误杀整套默认观感）
  for (const good of [ARGUS_DEFAULT, '#f2f2f2', '#ffb000', '#22d3ee', '#ff5fd2', '#9b9b9b']) {
    expect(parseArgusColor(good).ok, `${good} 派生值全部达标 ⇒ 必须收`).toBe(true)
  }
})

/**
 * ⭐ C1 的**结构性**那一半：派生检查里的 alpha 与 `deriveArgusVars` 的 alpha **不许漂**。
 *
 * ⚠️ 为什么非要这条：`argusDerivedFailure` 的 alpha 是**抄死**的（见那里的注释——读回来要
 * 多一条 `rgba()` 解析路径）。于是"改派生表不改检查"就成了一个新的假绿通道：
 * 把 `deriveArgusVars` 的 `--v-fg-3` 从 0.58 改成 0.99，检查仍按 0.58 算 ⇒
 * **屏幕上 fg-3 与面同色（读不出来），而闸门照样放行**——正是 C1 的原始形态换个位置重演。
 * ⇒ 逐字比对：`DERIVED_TEXT_CHECKS` 的 alpha ≡ `deriveArgusVars()` 输出里的那几个数。
 * ⚠️ 键数也钉住：往派生表**加**一档弱化色而忘了加检查 ⇒ 那条颜色的派生值就又没人管了。
 */
test('⭐ C1：派生检查的 alpha ≡ `deriveArgusVars` 的 alpha（改一处不改另一处 ⇒ 必红）', () => {
  const v = deriveArgusVars(ARGUS_DEFAULT)
  // 抄死的那三个 alpha 必须与派生表**逐字**对上（`rgba(r, g, b, 0.58)` 里的那个数）
  const alphaOf = (token: string) => Number(v[token].match(/,\s*([0-9.]+)\)$/)![1])
  expect(alphaOf('--v-fg-3'), '检查按 0.58 算，派生表改成别的数就漂了').toBe(0.58)
  expect(alphaOf('--v-fg-4'), '检查按 0.38 算').toBe(0.38)
  // 面那一侧同理：`--v-surface-2` 就是检查里那个 0.14
  expect(alphaOf('--v-surface-2'), '检查按 0.14 算（最弱的那个面）').toBe(0.14)
  // 覆盖面的**下界**：派生表里"带 alpha 的 fg 族"只有 fg-2/3/4 三档（`--v-fg` 本身是纯色）
  // ⇒ 检查必须覆盖其中**较弱的**那两档；将来加一档 fg-5，这条会红、逼人回去想它谁管。
  const alphas = Object.entries(v)
    .filter(([k]) => /^--v-fg-\d+$/.test(k))
    .map(([k]) => k)
  expect(alphas, '派生表的 fg 族变了 ⇒ 这条判据的覆盖面要跟着重算').toEqual([
    '--v-fg-2',
    '--v-fg-3',
    '--v-fg-4',
  ])
})

/**
 * ⭐ C1 的下界：**"fg-3 ≥ 3"已经蕴含 "fg-4 ≥ 1.75"**（在 alpha 0.58 / 0.38 下）。
 *
 * 为什么记这条：`DERIVED_TEXT_CHECKS` 里第二条（1.75）看着比第一条低得多、像是"随手定的"。
 * 它不是——**它是被动的**：全色域实测（alpha 0.58/0.38、面对 `--v-bg` 与 `--v-surface-2`
 * 取更差），凡 fg-3 ≥ 3 的颜色，fg-4 **最小**也有 **1.8346**（近灰最小 **1.8633**）。
 * ⇒ 那条 1.75 今天**永远不会是"最紧的那一关"**，它在那儿的意义是"fg-4 的 alpha 有主了"。
 * ⚠️ 这条断言就是那句"被动的"的机械证据：一个刚好过 fg-3 那关的颜色（`#9b9b9b`）
 * 的 fg-4 必须明显高于 1.75——留 1.5 倍余量，免得把"边界抖动"当成"蕴含失效"。
 */
test('⭐ C1：fg-3 ≥ 3 蕴含 fg-4 ≫ 1.75（所以第二条检查是"有主"，不是"最紧的那关"）', () => {
  // `#9b9b9b` 是灰阶里**刚好**过 fg-3 那关的颜色（fg-3 = 3.0210）⇒ 它的 fg-4 是下界附近
  const r = parseArgusColor('#9b9b9b')
  expect(r.ok, '#9b9b9b 是 fg-3 的边界见证（必须收）').toBe(true)
  // 直接用派生函数取它的最弱环，断言 fg-4 那关**没有**在拉低它（留给它 1.5 倍余量）
  const fail = argusDerivedFailure('#9b9b9b')
  expect(fail, '#9b9b9b 两关全过').toBeNull()
  // ⚠️ 一个"fg-3 恰好不过"的近邻（`#9a9a9a`）被拒，**且理由是 fg-3**——不是 fg-4
  const near = parseArgusColor('#9a9a9a')
  expect(near.ok).toBe(false)
  if (!near.ok) expect(near.reason, '这条该是 fg-3 那关在拒它').toContain('--v-fg-3')
})

// ────────────────── R10（颜色的 FOUC：防闪脚本要能读回用户的颜色） ──────────────────

const HTML = readFileSync(new URL('../../index.html', HERE), 'utf8')

/**
 * ⭐⭐ F-A1 / F-A5：**剥掉注释**之后的 `index.html`——R10④ 的静态锚点一律在这里取。
 *
 * ⚠️ 为什么必须剥：`index.html` 的注释里就写着 `=== 'argus'` 与 style 元素的字面量
 * ⇒ 用**原文**取 `indexOf` 会命中注释（实测：`=== 'argus'` 在 idx **1425** 是注释、
 * **1608** 才是真分支）⇒ 锚点变成**空守卫**、`grep` 也会得到假阳性（本仓"分母要对"的老账）。
 */
const CODE = HTML.replace(/<!--[\s\S]*?-->/g, '').replace(/\/\*[\s\S]*?\*\//g, '')

/**
 * 取 `index.html` 里那段**内联**防闪脚本并**真跑一遍**。
 *
 * ⚠️ 这是本文件里唯一能直接验防闪脚本的办法（jsdom 不会去加载 `index.html`）。
 * 跑的是**原文**（不是抄一份逻辑——抄一份就是 R10 明令禁止的那件事），
 * 所以 `new Function` 在 jsdom 的全局环境里执行它时，`document` / `localStorage`
 * 就是被测的那两个对象。
 */
function runPreflash(): void {
  const m = HTML.match(/<script>([\s\S]*?)<\/script>/)
  expect(m, 'index.html 里找不到内联防闪脚本').toBeTruthy()
  new Function(m![1])()
}

test('⭐ R10①：`argusVarsJson(base)` 就是 `deriveArgusVars(base)` 的 JSON（可单测的那一半）', () => {
  for (const base of [ARGUS_DEFAULT, '#204060', '#f2f2f2']) {
    expect(JSON.parse(argusVarsJson(base)), `${base} 的 JSON 与派生表不一致`).toEqual(
      deriveArgusVars(base),
    )
  }
  // ⚠️ 它存在的理由 = R10 的"**不许**在防闪脚本里复制一份派生数学"：盘上那份 JSON 必须是
  // **同一个函数**的输出 ⇒ 前端回读时不需要知道任何 alpha，也就没有第二处真相。
  expect(Object.keys(JSON.parse(argusVarsJson(ARGUS_DEFAULT)) as object)).toHaveLength(12)
})

test('⭐ R10②：`applyArgusColor` 把派生结果落到 `vigil.argus.vars`；`clearArgusColor` **不动它**', () => {
  expect(ARGUS_VARS_KEY, '键名不许漂（防闪脚本里那份是字面量，静态钉子会逐字比）').toBe(
    'vigil.argus.vars',
  )
  expect(localStorage.getItem(ARGUS_VARS_KEY)).toBeNull()
  applyArgusColor('white')
  expect(localStorage.getItem(ARGUS_VARS_KEY)).toBe(argusVarsJson('#f2f2f2'))
  clearArgusColor()
  // ⚠️ 与 `ARGUS_COLOR_KEY` **同一条纪律**（见上一条用例末段的注释）：清的是
  // "这次生效的覆盖"，盘上那份是**用户的偏好**——清掉它，下次进 ARGUS 就回到默认绿了。
  expect(localStorage.getItem(ARGUS_VARS_KEY)).toBe(argusVarsJson('#f2f2f2'))
})

test('⛔ R10③：防闪脚本里**没有**第二份颜色数学（复制 = 两处真相，必然漂移）', () => {
  const src = HTML.match(/<script>([\s\S]*?)<\/script>/)![1]
  for (const smell of ['0.2126', '0.7152', '0.0722', '2.4', '0.78', '0.58', '0.38']) {
    expect(src, `防闪脚本里出现了派生用的常量 ${smell}——它在复制颜色数学`).not.toContain(smell)
  }
})

/**
 * ⭐ R10④ **静态钉子**：白名单的三条正则字面量 + **12 键名单**都在 `index.html` 里、
 * 且它嵌在 argus 分支**内**（防闪脚本无法被单测执行 ⇒ 只能静态钉）。
 *
 * ⚠️ 这条钉的是"**白名单没被删 / 键名没漂 / 它在分支里**"——**不是**"白名单生效"。
 * 生效由下面 ⑤⑥⑦ 三条**真跑那段脚本**的用例证明（读 index.html 原文 + `new Function`）。
 * ⚠️ 正则一律按**实际写在 index.html 里的字面量**钉（写法改了，这里跟着改）。
 *
 * ⭐⭐ F-A1（T8-2 复审的 Important：一条**空守卫**）：锚点**必须**在**剥掉注释**的文本
 * （`CODE`）上取。实测（2026-09-22 复审）：用原文的 `HTML.indexOf("=== 'argus'")` 命中的是
 * **注释**里那处（idx **1425**：`<!-- … 整段嵌在 `=== 'argus'` 分支内 … -->`），
 * **真分支**在 idx **1608** ⇒ 把 `if (t === 'argus')` 改成 `if (true)` 时这条断言**照样绿**
 * （唯一红的是 R10⑦）＝它根本没验它声称的那件事。
 * ⇒ 修法：锚点改在 `CODE` 上取，并**正面钉住**"`=== 'argus'` 在 `CODE` 里恰好出现一次"
 * ——将来有人再往注释外的地方写一处，这条会红。
 * ⚠️ 剥注释同时是 **F-A5** 的出路：注释里的 `<style>` / `=== 'argus'` 这类**字面量**
 * 会把 `grep`、也会把自己写的锚点一起带偏。
 * ⚠️ 并且："**在分支内**"要按它字面的意思判——字面量必须落在 `if (t === 'argus') { … }`
 * **那一对**大括号之间。只判"在锚点之后"是不够的：把整段挪到分支**外**（但仍在锚点之后）
 * 照样过，那又成了"断言比它声称的弱"（同一条 F-A1 的形态）。
 */
test('⭐ R10④ **静态钉子**：白名单的三条正则 + 12 键名单都在 index.html 里，且嵌在 argus 分支内', () => {
  expect(CODE).toContain('vigil.argus.vars')
  expect(CODE).toContain('/^--v-[a-z0-9-]+$/')
  expect(CODE).toContain('/^#[0-9a-f]{6}$/')
  expect(CODE).toContain('/^rgba?\\([0-9., %]+\\)$/')
  // 必须在 `ALLOWED` 判定为 argus 的**分支内**（不是"任何主题都贴一份行内样式"）
  const hits = [...CODE.matchAll(/=== 'argus'/g)].map((m) => m.index!)
  expect(
    hits,
    `剥注释后 \`=== 'argus'\` 该**恰好一次**（实际 ${hits.length} 处：${hits.join(', ')}）——多出来的是新的注释陷阱或第二处判定`,
  ).toHaveLength(1)
  const branch = CODE.indexOf("if (t === 'argus')")
  expect(branch, "防闪脚本里没有 `if (t === 'argus')` 分支").toBeGreaterThan(-1)
  // "在分支内"的**机械含义**：落在那一对**大括号**之间（不是"在锚点之后"就算——
  // 见文件头 F-A1 的注：把整段挪到分支外但留在锚点之后，那种弱判据照样绿）。
  // ⚠️ 大括号用**裸计数**：若将来看这段脚本里有"含大括号的字符串/模板"，这里会失准
  //（本段目前没有：`/^#[0-9a-f]{6}$/` 里的 `{6}` 是配对的、字符串里没有大括号）。
  const open = CODE.indexOf('{', branch)
  let depth = 0
  let close = -1
  for (let i = open; i < CODE.length; i += 1) {
    const ch = CODE[i]
    if (ch === '{') depth += 1
    if (ch === '}') {
      depth -= 1
      if (depth === 0) {
        close = i
        break
      }
    }
  }
  expect(close, 'argus 分支的闭合大括号没找到（脚本写法变了？）').toBeGreaterThan(branch)
  for (const s of [
    'vigil.argus.vars',
    '/^--v-[a-z0-9-]+$/',
    '/^#[0-9a-f]{6}$/',
    '/^rgba?\\([0-9., %]+\\)$/',
  ]) {
    const at = CODE.indexOf(s)
    expect(at, `${s} 出现在 argus 分支之前 ⇒ 不在分支内`).toBeGreaterThan(branch)
    expect(at, `${s} 越过了 argus 分支的闭合大括号 ⇒ 不在分支内`).toBeLessThan(close)
  }
  // 与"设 `data-theme`"那个 try **分开**（blob 读炸了不该把主题也带下水 ⇒ 两个 try）
  expect((CODE.match(/try \{/g) ?? []).length).toBeGreaterThanOrEqual(2)
})

/**
 * ⭐⭐ R10⑨（F-A4，T8-2 复审的 Important）：防闪脚本里那份**键名名单** ≡ `deriveArgusVars` 的 12 个键。
 *
 * **语义**：脚本里那份名单是**手写副本**（第一份真相在 `src/argusColor.ts` 的 `deriveArgusVars`）
 * ⇒ 必须与 JS 侧**机械对上**。与 `theme.test.ts` 那条"`ALLOWED` 不许与 `THEMES` 漂移"同款，
 * 理由也一样：两份必然漂移，所以钉一条机械比对——它红的时候改哪一份都对，但不能只有一份对。
 *
 * ⚠️ 必须是**集合相同**（不是"包含"）：
 *   - **多一个** ⇒ 那个**陌生键名**会被贴成行内、而 `clearArgusColor()` 清不掉它
 *     ⇒ §0.1 那个"行内顶掉别的主题"换个入口重演（**F-A4 的洞本身**）；
 *   - **少一个** ⇒ 该键的用户色**首帧**贴不上（防闪失效半格），而写侧仍在写它、清它
 *     ⇒ 又一处"写侧认、读侧不认"的错位。
 */
test('⭐⭐ R10⑨（F-A4）：防闪脚本的 12 键名单 ≡ `Object.keys(deriveArgusVars(ARGUS_DEFAULT))`', () => {
  const m = CODE.match(/var KNOWN = \[([\s\S]*?)\]/)
  expect(m, 'index.html 的防闪脚本里找不到 `var KNOWN = [...]` 键名名单').toBeTruthy()
  const names = [...m![1].matchAll(/'([^']+)'/g)].map((x) => x[1])
  expect(names, '名单里读不出 12 个键名（写法变了？这条判据跟着改）').toHaveLength(12)
  expect(new Set(names).size, '名单里有重复的键名').toBe(names.length)
  expect([...names].sort(), '脚本里那份键名名单与 JS 侧的派生表漂了').toEqual(
    Object.keys(deriveArgusVars(ARGUS_DEFAULT)).sort(),
  )
})

test('⭐ R10⑤：盘上有 vars + 主题是 argus ⇒ **首帧**就是用户色（不是默认绿）', () => {
  localStorage.setItem('vigil.theme', 'argus')
  localStorage.setItem(ARGUS_VARS_KEY, argusVarsJson('#f2f2f2'))
  runPreflash()
  const root = document.documentElement
  expect(root.getAttribute('data-theme')).toBe('argus')
  // 逐键：12 个都要贴上（少一个 = 白名单把它筛掉了，或回读只贴了 `--v-fg`）
  for (const [k, v] of Object.entries(deriveArgusVars('#f2f2f2'))) {
    expect(root.style.getPropertyValue(k), `${k} 没被防闪脚本贴上`).toBe(v)
  }
  expect(root.style.getPropertyValue('--v-fg'), '首帧还是默认绿').not.toBe(ARGUS_DEFAULT)
})

test('⛔ R10⑥：那份 JSON 是 **localStorage 可控**的 ⇒ 白名单按形状**逐条丢弃**（不是"试试看"）', () => {
  localStorage.setItem('vigil.theme', 'argus')
  localStorage.setItem(
    ARGUS_VARS_KEY,
    JSON.stringify({
      '--v-fg': '#f2f2f2', // 合法 ⇒ 贴
      '--v-border': 'rgba(242, 242, 242, 0.45)', // 合法（第二种形状）⇒ 贴
      'background-color': 'red', // 键名不合形状 ⇒ 丢（这就是"任意行内样式注入"那个面）
      '--v-page-image': 'url(http://evil/x.png)', // 值不合形状 ⇒ 丢
      '--v-x': '#000000; background-image: url(http://evil)', // 值里夹带 ⇒ 丢
      '--v-y': 42, // 不是字符串 ⇒ 丢
      // ⭐⭐ N2（T8-2 fix 复审的 Minor，T8-3 折进本轮）：**已知键 + 坏值**。
      // 上面那四条"坏"条目全部在**键名**那一步就被丢了（`background-color` 不合形状，
      // `--v-page-image`/`--v-x`/`--v-y` 的键名不在 `KNOWN` 那 12 个里）⇒ 值判据
      //（`RE_HEX`/`RE_RGB`）**一条都没被执行到**，成了**不可达的判据**。
      // ⚠️ 这正是本项目最贵的一类（判据在、却永远不会被考验）：实测把 `RE_RGB` 改成
      // `/^.+$/`（值白名单形同虚设）时，本文件**只红静态钉子 R10④ 一条**，行为侧零信号。
      // ⇒ 下面两条是**唯一**还能把值判据拉回可达的喂料：键名合法 ⇒ 键名那步放行，
      // 只有值判据能挡。（⛔ 别用 `--v-fg`：它上面已经有一条合法值，同一对象字面量里
      // 重复键会被 TS 报 TS1117、且后者覆盖前者。）
      '--v-accent': 'url(http://evil/x.png)', // 已知键 + 值不合形状 ⇒ 丢（只有值判据能挡）
      '--v-fg-2': '#000000; background-image: url(http://evil)', // 已知键 + 值里夹带 ⇒ 丢
    }),
  )
  runPreflash()
  const st = document.documentElement.style
  expect(st.getPropertyValue('--v-fg')).toBe('#f2f2f2')
  expect(st.getPropertyValue('--v-border')).toBe('rgba(242, 242, 242, 0.45)')
  for (const bad of [
    'background-color',
    '--v-page-image',
    '--v-x',
    '--v-y',
    '--v-accent', // N2：这一对**走的是值判据**（键名合法），是它可达的证据
    '--v-fg-2',
  ]) {
    expect(st.getPropertyValue(bad), `${bad} 不该被贴上`).toBe('')
  }
})

test('⛔ R10⑦：主题不是 argus / blob 是垃圾 ⇒ 防闪脚本**一个字都不许贴**，也不许抛', () => {
  // 别的主题下贴行内 ARGUS 变量 = §0.1 那个"行内顶掉别的主题"的缺陷换个入口重演
  localStorage.setItem('vigil.theme', 'material')
  localStorage.setItem(ARGUS_VARS_KEY, argusVarsJson('#f2f2f2'))
  runPreflash()
  expect(document.documentElement.style.getPropertyValue('--v-fg')).toBe('')
  expect(document.documentElement.getAttribute('data-theme')).toBe('material')
  // 主题是 argus、blob 是垃圾 ⇒ 保持 CSS 里的默认色 + **不抛**（防闪脚本不该是任何功能失败的原因）
  document.documentElement.removeAttribute('data-theme')
  localStorage.setItem('vigil.theme', 'argus')
  localStorage.setItem(ARGUS_VARS_KEY, '{not json')
  expect(() => runPreflash()).not.toThrow()
  expect(document.documentElement.style.getPropertyValue('--v-fg')).toBe('')
})

test('R10⑧：陈旧的 blob（少键 / 结构变了）不硬套：缺的键保持 CSS 默认，且下次 `applyArgusColor` 会整份刷新', () => {
  const full = deriveArgusVars(ARGUS_DEFAULT)
  localStorage.setItem('vigil.theme', 'argus')
  localStorage.setItem(
    ARGUS_VARS_KEY,
    JSON.stringify({
      '--v-fg': full['--v-fg'],
      '--v-fg-2': full['--v-fg-2'],
      '--v-fg-3': full['--v-fg-3'],
      '--v-old-token': full['--v-fg'], // 旧版本留下的键名（形状合法，但不在 12 键名单里 ⇒ 现在**不贴**，见下）
    }),
  )
  runPreflash()
  const st = document.documentElement.style
  expect(st.getPropertyValue('--v-fg')).toBe(ARGUS_DEFAULT)
  expect(st.getPropertyValue('--v-border'), '旧 blob 里没有的键：保持 CSS 默认（不许瞎猜）').toBe('')
  // ⭐⭐ F-A4（T8-2 复审的 Important ⇒ 这是一条**行为变更**，不是放宽）：
  // `--v-old-token` 是"**形状合法但不在 12 键名单里**"的陌生键名 ⇒ 现在**不贴**了。
  // ⚠️ 下面这条断言原来写的是 `toBe(ARGUS_DEFAULT)`（贴上了）——那时白名单**只看形状**。
  //
  // 为什么必须改：形状白名单放行陌生键名，而 `clearArgusColor()` 只清 `deriveArgusVars()`
  // 那 12 个 ⇒ 那种键**首帧被贴上、切到别的主题时清不掉**（行内赢过任何选择器）
  // ＝ §0.1 那个"行内顶掉别的主题"的缺陷**换个入口**重演（复审实测：
  // `{"--v-bg":"#ff0000"}` 能把整页底色换掉——`--v-bg` 落在 `body{background-color:
  // var(--v-bg)}` 上）。⇒ 读侧收紧成「**12 个键名** + 形状」，与写侧 `clearArgusColor`
  // 的 12 个键**对齐**；R10⑨ 机械钉住"两份名单相同"。
  //
  // ⚠️ 一条**仍然成立**的相邻事实（别删，它是上面那句的边界）：**值**域始终被字符集限死
  //（`/^#[0-9a-f]{6}$/` 或 `/^rgba?\([0-9., %]+\)$/`）⇒ "往行内样式里注入任意 CSS"
  // 那个面是**关着的**（`url(...)` / `;` 夹带都过不去，R10⑥ 钉着）——F-A4 修的是**键名**
  // 那一半，不是这一半。
  // ⚠️ 下面那句自愈仍然是**真**的：`main.tsx` 的 `applyTheme(readTheme())` 在 App 启动时
  // 就调 `applyArgusColor` **重写整份 blob** ⇒ 陈旧窗口只有"首帧到模块执行"这一段。
  expect(st.getPropertyValue('--v-old-token'), '陌生键名（不在 12 键名单里）不许再被贴上').toBe('')
  applyArgusColor(readArgusColor())
  expect(JSON.parse(localStorage.getItem(ARGUS_VARS_KEY)!)).toEqual(full)
})
