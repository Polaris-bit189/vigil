/**
 * ARGUS 主题的颜色引擎：**基色 → 整套 CSS 变量**。
 *
 * 为什么不交给 CSS 的 `color-mix()`（见计划 §0.1）：本仓的对比度判据解析不了 `color-mix`，
 * 那等于把这一卷新增的整套配色弄成"没人能验"。派生放这里 ⇒ **纯函数、可单测**，
 * 写出去的一律是 `rgba(r, g, b, a)` 字面值。
 *
 * ⚠️ 写的是 `:root` 的**行内**自定义属性——行内赢过任何选择器（包括 `[data-theme='material']`）
 * ⇒ 切到别的主题时**必须** `clearArgusColor()`，否则材料主题的字色会被 ARGUS 的颜色残留顶掉。
 */
export const ARGUS_DEFAULT = '#00ff9c'
export const ARGUS_COLOR_KEY = 'vigil.argus.color'

/**
 * 派生表落盘的键（⭐ R10：**防闪**那一半）。
 *
 * ⚠️ 为什么盘上要**整份 JSON**、而不是只存基色让防闪脚本自己算：
 * `applyArgusColor` 是模块加载**之后**才跑的，而 `index.html` 的防闪脚本在**首帧之前**
 * ——那一段窗口里 `:root` 上没有任何行内变量 ⇒ 页面先画成 CSS 里的**默认绿**，再跳成
 * 用户的颜色（手机上就是一道闪）。防闪脚本要贴出**用户色**，就得能在不加载任何模块的
 * 前提下拿到那 12 个值。
 * ⛔ 让它"照着基色自己算一份" = 把 alpha 表复制进 `index.html`（两处真相，必然漂移）
 * ⇒ 存 `deriveArgusVars` 的**原样输出**：算的地方只有这一个函数。
 * ⚠️ 与 `ARGUS_COLOR_KEY` **同一条纪律**：`clearArgusColor` 只清"这次生效的覆盖"
 * （行内变量），盘上这份与基色一样是**用户的偏好**，不清。
 */
export const ARGUS_VARS_KEY = 'vigil.argus.vars'

/**
 * 具名色：不是 CSS 的 green（#008000 在纯黑底上偏暗），而是终端色。
 *
 * ⚠️⭐ **`red` 的值是 `#ff6b61`，不是它原来那个 `#ff5f56`**（用户 2026-09-29 裁决：
 * "换个更亮的红，保留'红'这个语义名字"）。理由写在下面，**别照着旧值改回去**。
 *
 * **为什么不能更暗**：闸门判的是**派生出来的整份配色**，不只是基色（C1，见
 * `argusDerivedFailure`）。`#ff5f56` 的基色在纯黑底上有 7.02:1（远超 `MIN_LUMINANCE`
 * 那关），**但**它派生出的 `--v-fg-3`（alpha 0.58）在 `--v-bg` 上只有 **2.85:1 < 3**
 * ⇒ 用户 `/color red` 会被自己的闸门拒（这就是用户裁定的那件事）。
 * ⇒ 新值从**同一个色相族**（保留 `#ff5f56` 的 g:b ≈ 1.10 那个"暖红"而不是纯红
 * `#ff0000`）整体提亮，**只提亮到刚过线**：`--v-fg-3` = **3.0053**（≥3，余量 0.18%）、
 * `--v-fg-4` = **1.8462**（≥1.75）。
 * ⚠️ 口径就是那条 `--v-fg-3 ≥ 3`：它沿用本仓对"弱化文字"（`themePalette.test.ts` 的
 * muted）的既有标准，是**闸门自己写着的**那个数（`DERIVED_TEXT_CHECKS`），不是这里另定的。
 * ⛔ **再暗一点都不行**：同一色相族里 `#ff6a5f`（再暗一档）的 fg-3 = 2.9905 < 3 ⇒ 被拒；
 * `#f85149`（GitHub 的"危险红"）更不行（fg-3 = 2.62）。**提亮就是这条闸门的代价**——
 * 红变浅是"能不能用"换来的，不是审美选择。
 * ⚠️ 跨文件连带（**有判据**）：`index.css` 的 argus 块里 `--v-danger` 及其派生
 * （`-soft`/`-btn`/`-btn-active`）是**照这个值抄的**，`argusColor.test.ts` 的 R12-a
 * 逐字对账两者 ⇒ 改这里必须同改那边。
 */
export const ARGUS_NAMED: Record<string, string> = {
  green: '#00ff9c',
  white: '#f2f2f2',
  amber: '#ffb000',
  cyan: '#22d3ee',
  red: '#ff6b61',
  magenta: '#ff5fd2',
}

export type ArgusColorResult = { ok: true; value: string } | { ok: false; reason: string }

type RGB = [number, number, number]

function toHex([r, g, b]: RGB): string {
  return `#${[r, g, b].map((c) => c.toString(16).padStart(2, '0')).join('')}`
}

function parseHex(s: string): RGB | null {
  const m = s.trim().toLowerCase()
  if (/^#[0-9a-f]{3}$/.test(m)) {
    return [
      parseInt(m[1] + m[1], 16),
      parseInt(m[2] + m[2], 16),
      parseInt(m[3] + m[3], 16),
    ]
  }
  if (/^#[0-9a-f]{6}$/.test(m)) {
    return [parseInt(m.slice(1, 3), 16), parseInt(m.slice(3, 5), 16), parseInt(m.slice(5, 7), 16)]
  }
  return null
}

/** WCAG 相对亮度（与 `themePalette.test.ts` 同一套算法） */
function luminance([r, g, b]: RGB): number {
  const f = (c: number) => {
    const x = c / 255
    return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

/**
 * ⛔ 太暗的颜色要拒：终端主题是**纯黑底**，一个接近黑的前景色等于把整个界面读没
 * （而它会静默生效 ⇒ 用户只会觉得"这个主题坏了"）。
 * 阈值取 4.5:1 对纯黑底反推出的亮度下界：L ≥ 0.175 ⇒ 对比度 = (L+0.05)/0.05 ≥ 4.5。
 *
 * ⚠️ 这一条只保证**基色自己**（`--v-fg` / `--v-accent`）读得出来 ⇒ 它是**必要**条件、
 * **不是**充分条件。（C1 的实证：`#757575` 基色 4.56:1 达标，而它派生出来的
 * `--v-fg-3` 只有 2.15:1——闸门与 alpha 表一度是**两个不知道彼此的所有者**。）
 * 派生值的安全由下面 `argusDerivedFailure()` 负责，两条一起才是"整套配色可用"。
 */
const MIN_LUMINANCE = 0.175

/**
 * ARGUS 的底。⚠️ **硬编码的纯黑**，因为下面那两条下界就是**对纯黑底**反推出来的。
 * 它不是"实现细节"：`[data-theme='argus']` 的 `--v-bg` 一旦被改成别的色，
 * 本文件里的每一个数（含 `MIN_LUMINANCE` 反推的 4.5:1）就都不作数了。
 * ⇒ `themeTokens.test.ts` 用 verbatim 断言把那个 `#000000` 钉死（C2）。
 */
const ARGUS_BG: RGB = [0, 0, 0]

/** 三层叠色：半透明层压在不透明底上（与 `themePalette.test.ts` 的 `over` 同一套算术）。 */
function over(top: RGB, alpha: number, bottom: RGB): RGB {
  return [
    top[0] * alpha + bottom[0] * (1 - alpha),
    top[1] * alpha + bottom[1] * (1 - alpha),
    top[2] * alpha + bottom[2] * (1 - alpha),
  ]
}

/** WCAG 对比度（与 `themePalette.test.ts` 的 `contrast` 同一套算术、同一个 `luminance`）。 */
function contrast(a: RGB, b: RGB): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

/**
 * ⭐ C1：派生检查里"文字坐在哪个面上"。终端真正会出现的面只有这两个：
 *   `--v-bg`（纯黑，alpha 0）与 `--v-surface-2`（选中行 / 输入框 focus / 按钮 active
 *   的底，= 基色叠 0.14）——**后者更亮 ⇒ 对比度更差 ⇒ 它就是最弱环**。
 * ⚠️ 与 `themePalette.test.ts` 的面清单同源（那里也是这"两个面取更差"）。
 */
const DERIVED_FACES: { name: string; alpha: number }[] = [
  { name: '--v-bg', alpha: 0 },
  { name: '--v-surface-2', alpha: 0.14 },
]

/**
 * 派生层的两个下界（alpha 照 `deriveArgusVars` 抄死；**不是**从那里读，见下面的注）。
 *
 * · `--v-fg-3` = **弱化文字**（未选中的 tab、群名、时间、品牌区小字…全仓最常用的弱化色）
 *   ⇒ 沿用本仓既有口径 **≥ 3**（`themePalette.test.ts` 的 `muted`）。
 * · `--v-fg-4` = **最弱一档**（提示符、占位符、"读取中…"）⇒ **≥ 1.75**。
 *   ⚠️ 为什么是 1.75 这个"看着低"的数：它不是可读性标准，是**下界**——0.38 这道 alpha
 *   天生只能到这么高（`#00ff9c` 才 2.77，`/color white` 3.14）。定高的会把**连默认色都拒掉**。
 *   ⚠️ 并且它今天是**被动的**：在 alpha 0.58/0.38 下，"fg-3 ≥ 3"已经蕴含 "fg-4 ≥ 1.83"
 *   （全色域实测的最小值，见 `argusColor.test.ts` 里那条断言）⇒ 第二条比第一条弱。
 *   ⇒ 它在这儿的意义是**把 fg-4 的 alpha 变成有主的**：谁把 0.38 调小，这条先红。
 *
 * ⚠️⭐ **收紧之后用户该按什么找色**（实测，照抄别猜）：
 * · **灰**：闸门落在 `#9a9a9a`（`--v-fg-3` = 2.9957，拒）/ `#9b9b9b`（3.0210，收）之间
 *   ⇒ "刚好够亮的灰"（4.5:1 那条线，`#757575` 一带）**今天会拒**，得往亮处再走 38 档。
 *   这就是为什么错误文案里点名的是"**派生值**"而不是"太暗"——基色明明达标。
 * · **彩色**：alpha 0.58/0.38 是**乘性**的，所以饱和色的窗口比灰宽得多
 *   （`#ff5fd2` 收、**旧** `red` `#ff5f56` 拒、`/color white` 富余）。
 *   ⚠️ 具名色 `red` 因此**换过一次值**（`#ff5f56` → `#ff6b61`，用户 2026-09-29 裁决，
 *   见 `ARGUS_NAMED` 上方那段）：旧值的 fg-3 = 2.85 **过不去这条闸门**；新值 3.0053 刚好过。
 * ⛔ 别把 1.75 或 3 当成"可读性标准"引用：它们唯一的作用是让**每一档 alpha 都有主**。
 */
const DERIVED_TEXT_CHECKS: { token: string; alpha: number; min: number }[] = [
  { token: '--v-fg-3', alpha: 0.58, min: 3 },
  { token: '--v-fg-4', alpha: 0.38, min: 1.75 },
]

/** 派生检查的失败详情（`null` = 整套派生配色达标）。 */
export type ArgusDerivedFailure = { token: string; face: string; ratio: number; min: number }

/**
 * 逐步复刻 `deriveArgusVars` 的**叠色链**，对每个派生文字 token 的**最弱环**做对比度检查。
 *
 * ⚠️ 为什么 alpha 在这里**抄死**、而不是 `deriveArgusVars(base)[token]` 读回来：
 * 读回来就得**解析** `rgba(r, g, b, a)` 字符串（正则 + `parseFloat`）——多一条解析路径就多一处
 * 能与派生表漂移的地方。抄死的代价是"改 alpha 要改两处"，而**这一点由判据兜住**：
 * `argusColor.test.ts` 有一条断言逐字比对 `DERIVED_TEXT_CHECKS` 的 alpha
 * 与 `deriveArgusVars()` 输出里的那几个数（改一处不改另一处 ⇒ 必红）。
 */
export function argusDerivedFailure(base: string): ArgusDerivedFailure | null {
  const rgb = parseHex(base)
  if (rgb === null) throw new Error(`argusDerivedFailure 需要 #rrggbb，收到 ${base}`)
  for (const { token, alpha, min } of DERIVED_TEXT_CHECKS) {
    for (const { name, alpha: faceAlpha } of DERIVED_FACES) {
      const face = over(rgb, faceAlpha, ARGUS_BG)
      const ratio = contrast(over(rgb, alpha, face), face)
      if (ratio < min) return { token, face: name, ratio, min }
    }
  }
  return null
}

export function parseArgusColor(input: string): ArgusColorResult {
  const key = input.trim().toLowerCase()
  if (key === '') return { ok: false, reason: '用法：/color green｜white｜#rgb｜#rrggbb' }
  const named = ARGUS_NAMED[key]
  const rgb = parseHex(named ?? key)
  if (rgb === null) {
    return { ok: false, reason: `认不出这个颜色：${input.trim()}（试试 /color green 或 /color #ff8800）` }
  }
  if (luminance(rgb) < MIN_LUMINANCE) {
    return { ok: false, reason: `这个颜色太暗了，在纯黑底上读不出来：${toHex(rgb)}` }
  }
  // ⭐ C1：基色够亮**不等于**派生出来的那套够亮——判**整份**（见 `argusDerivedFailure`）。
  const derived = argusDerivedFailure(toHex(rgb))
  if (derived !== null) {
    return {
      ok: false,
      reason:
        `这个颜色太暗了：它派生出的 ${derived.token} 在 ${derived.face} 上只有 ` +
        `${derived.ratio.toFixed(2)}:1（至少要 ${derived.min}:1）——换个更亮的颜色试试：${toHex(rgb)}`,
    }
  }
  return { ok: true, value: toHex(rgb) }
}

/** 基色 → 12 个变量的字面值（层级用降透明，与主题体系里"面/边框"的既有做法一致） */
export function deriveArgusVars(base: string): Record<string, string> {
  const rgb = parseHex(base)
  if (rgb === null) throw new Error(`deriveArgusVars 需要 #rrggbb，收到 ${base}`)
  const [r, g, b] = rgb
  const a = (alpha: number) => `rgba(${r}, ${g}, ${b}, ${alpha})`
  return {
    '--v-fg': base,
    '--v-fg-2': a(0.78),
    '--v-fg-3': a(0.58),
    '--v-fg-4': a(0.38),
    '--v-surface': a(0.06),
    '--v-surface-2': a(0.14),
    '--v-inset': a(0.03),
    '--v-border': a(0.45),
    '--v-border-soft': a(0.22),
    '--v-accent': base,
    '--v-accent-soft': a(0.18),
    '--v-accent-text': base,
  }
}

/**
 * 派生表 → JSON（⭐ R10 的**可单测的那一半**）。
 *
 * 存在的唯一理由：`index.html` 的防闪脚本要**原样读回**这套变量，而它不许复制派生数学
 * （R10③ 那条判据就是钉这个的）。⇒ 盘上那份必须是**同一个函数**的输出
 * ⇒ 前端回读时不需要知道任何 alpha，也就没有第二处真相。
 */
export function argusVarsJson(base: string): string {
  return JSON.stringify(deriveArgusVars(base))
}

/**
 * 基色 → 写 `:root` 行内变量 + 落盘。**它只负责"写变量"，不负责"判断该不该写"**。
 *
 * ⚠️ F7（T8-1 fix 轮）：这里**没有**"当前主题必须是 argus"的前置条件——按本仓纪律，
 * 引擎保持纯（可单测、无环境依赖），**门放在调用点**：`theme.ts` 的 `syncArgusColor()`
 * **只在 `id === 'argus'` 时**才调它，其余主题走 `clearArgusColor()`。
 * ⇒ 任何新调用点：要么经由 `applyTheme`（推荐），要么自己承担这个判断；直接调它
 * 而在非 argus 主题下就是 §0.1 那个"行内覆盖顶掉别的主题"的缺陷换个入口重演。
 */
export function applyArgusColor(input: string): ArgusColorResult {
  const parsed = parseArgusColor(input)
  if (!parsed.ok) return parsed
  const vars = deriveArgusVars(parsed.value)
  const root = document.documentElement
  for (const [k, v] of Object.entries(vars)) root.style.setProperty(k, v)
  try {
    localStorage.setItem(ARGUS_COLOR_KEY, parsed.value)
    // ⭐ R10：盘上再存一份**派生结果的 JSON**——`index.html` 的防闪脚本靠它把用户色
    // 赶在首帧之前贴上去（它能读盘，但加载不了任何模块、也不许自己算）。
    // ⚠️ 同一句 try 保证的是**失败不外溢**（存不下不该让这次切换失败），
    // ⛔ 而**不是**"两份都不动"——`setItem` 之间**没有原子性**：第一条成功、第二条因
    // 配额/被策略挡而抛，落盘的正是"**基色新、变量旧**"的错位（T8-2 复审 F-A3：原注释
    // 把 try 当成了事务，那是错的论证）。后果仅一帧旧色：`main.tsx` 启动时
    // `applyTheme(readTheme())` 会**整份重写**变量、把错位自愈。
    // ⇒ 要真正消除错位得把两条合成**一个** key（`{base, vars}`，代价是读侧解析变复杂）。
    localStorage.setItem(ARGUS_VARS_KEY, argusVarsJson(parsed.value))
  } catch {
    // 存不下只影响"下次还记得"（含防闪），不该让这次切换失败
  }
  return parsed
}

export function readArgusColor(): string {
  let raw: string | null = null
  try {
    raw = localStorage.getItem(ARGUS_COLOR_KEY)
  } catch {
    return ARGUS_DEFAULT
  }
  if (raw === null) return ARGUS_DEFAULT
  const parsed = parseArgusColor(raw)
  return parsed.ok ? parsed.value : ARGUS_DEFAULT
}

export function clearArgusColor(): void {
  if (typeof document === 'undefined') return
  const root = document.documentElement
  for (const k of Object.keys(deriveArgusVars(ARGUS_DEFAULT))) root.style.removeProperty(k)
}
