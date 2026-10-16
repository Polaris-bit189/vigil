import { describe, expect, test } from 'vitest'
import { THEMES } from '../theme.ts'

// ⚠️ `node:fs` 的具名导入会让 `tsc -b` 以 TS2307 失败（本仓没有 `@types/node`，
// 而 package.json 不在 T1 的 Interfaces 里）——理由与替代方案的实测对照写在
// `theme.test.ts` 顶部同名注释里（同一处坑，两处都要绕）。
const NODE_FS = 'node:' + 'fs'
const { readFileSync } = (await import(/* @vite-ignore */ NODE_FS)) as {
  readFileSync: (p: URL, enc: string) => string
}

/**
 * ⭐ 主题层的**结构性守卫**。它守两条只有机械检查才抓得住的东西：
 *   ① **parity**：每套主题必须给**全部** token 一份值。缺一个 ⇒ CSS 继承
 *      `:root` 那一份（默认主题的值）⇒ **浅色主题里冒出深色残留**
 *      （出口判据 4 的真实形态），而肉眼要翻到那一页才看得见。
 *   ② **引用完整性**：`@theme inline` / `body{}` 里引用的 `--v-*` 必须存在。
 *      拼错的 token 名 **Tailwind 不会报错**，它只是不生成那个类 ⇒ 元素没样式、
 *      控制台一片安静。
 *
 * ⚠️ 解析用「选择器 + 花括号体」的粗切（本文件的 CSS 值都**不含嵌套花括号**）。
 * ⛔ 这一段是**已知限制**，**不是守卫**（F4，终审 fix 轮按实测改写）——原文写的是
 * 「哪天有人写 `@media` 嵌进来，这条解析会先红——那是好事，它逼你换解析器」，
 * 而终审的变异 **M7**（把主题块整个包进 `@media`）实测：**0 条红**，build 也干净。
 * 机理：这条正则**看不见嵌套层级**，内层规则被当成一个**顶层**块 ⇒ 选择器与花括号体
 * 都还在，`v()` 照样找得到、值照样读得到、断言照样过——**它不会"先红"，它会静默错解析**。
 * ⇒ 别把这句话当安全网；真需要嵌套时得换解析器（本文件下方 `rulesWithAncestors`
 * 就是那个"按花括号层级切"的替代品，它连"这条规则在不在某个 `@layer` 里"都问得出来）。
 * ⚠️ **必须先剥注释再解析**：本文件顶部那段说明里就含**示例 CSS**
 * （`{…}` 与 `var(--v-probe-radius)`），不剥的话它会被当成一个选择器块，
 * 而那个 `var(--v-probe-radius)` 会进"被引用但没定义"的名单 ⇒ **假红**。
 */

// ⚠️ 必须先把 `import.meta.url` 收进一个变量再用（实测，2026-09-22）：
// `new URL('…', import.meta.url)` 这种**第二个参数是字面量**的写法会被 Vite 的
// asset-import-meta-url 变换接管，解析基准变成 jsdom 的 document base
// （`http://localhost:3000/index.css`），而 `readFileSync` 只认 `file:`
// ⇒ 报 `ERR_INVALID_URL_SCHEME: The URL must be of scheme file`。
// 理由与实测对照写在 `theme.test.ts` 同名注释里（同一处坑）。
const HERE = import.meta.url

export const CSS = readFileSync(new URL('../index.css', HERE), 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
)

export type Block = { selector: string; body: string }

export function blocks(css: string): Block[] {
  return [...css.matchAll(/([^{}]*)\{([^{}]*)\}/g)].map((m) => ({
    selector: m[1].trim(),
    body: m[2],
  }))
}

function tokenNames(body: string): string[] {
  return [...body.matchAll(/(--v-[a-z0-9-]+)\s*:/g)].map((m) => m[1]).sort()
}

const ALL = blocks(CSS)
const root = ALL.find((b) => b.selector.includes("[data-theme='default']"))

/**
 * 取某套主题的某个 `--v-*` 的**值**（不是名字）。
 * ⚠️ 修复轮 F1 的 brief 假定这个助手**已经存在**，实际文件里没有（只有 `blocks()` /
 * `tokenNames()`，两个都只处理"名字"）⇒ 这里新建一个，解析沿用同一套「选择器 + 花括号体」
 * 粗切，所以它继承同一处已知缺口（嵌套块，见文件尾的 F4 守卫与报告 concerns）。
 * `'default'` 落到 `:root, [data-theme='default']` 那一块（默认主题与 `:root` 共用一块）。
 */
function v(themeId: string, name: string): string {
  const block = ALL.find((b) =>
    themeId === 'default'
      ? b.selector.includes("[data-theme='default']")
      : b.selector === `[data-theme='${themeId}']`,
  )
  expect(block, `index.css 里找不到 ${themeId} 的 token 块`).toBeTruthy()
  const m = block!.body.match(new RegExp(`${name}\\s*:\\s*([^;]+)`))
  expect(m, `${themeId} 块里找不到 ${name}`).toBeTruthy()
  return m![1].trim()
}

describe('token 层', () => {
  test(':root 与默认主题共用一块（否则"默认值"有两份）', () => {
    expect(root).toBeTruthy()
    expect(root!.selector).toContain(':root')
    expect(tokenNames(root!.body).length).toBeGreaterThanOrEqual(30)
  })

  // ⚠️ 只比**另外四套**（偏差 2，见 task-1-report.md）：默认那一份与 `:root`
  // **共用同一块**（选择器是 `:root, [data-theme='default']`）——Interfaces 写的是
  // 「`:root, [data-theme='default']` 与 **4 个** `[data-theme='x']` 块」，
  // 本文件第一条用例也断言了「共用一块」。若照原稿写 `for (const t of THEMES)`，
  // `t.id === 'default'` 那一轮要求存在一个选择器**恰好等于** `[data-theme='default']`
  // 的块 ⇒ 与"共用一块"直接冲突，**在本设计下永远不可能通过**；
  // 而且即便能通过，那也只是拿默认块和自己比，恒真、没有信息量。
  for (const t of THEMES.filter((x) => x.id !== 'default')) {
    test(`[data-theme='${t.id}'] 的 token 集合与默认**逐字相等**`, () => {
      const b = ALL.find((x) => x.selector === `[data-theme='${t.id}']`)
      expect(b, `index.css 缺 [data-theme='${t.id}'] 块`).toBeTruthy()
      expect(tokenNames(b!.body)).toEqual(tokenNames(root!.body))
    })
  }

  test('@theme inline 与 body 引用的每个 --v-* 都有定义', () => {
    const defined = new Set(tokenNames(root!.body))
    const referenced = [...CSS.matchAll(/var\(\s*(--v-[a-z0-9-]+)/g)].map((m) => m[1])
    expect(referenced.length).toBeGreaterThan(20)
    const missing = [...new Set(referenced)].filter((n) => !defined.has(n))
    expect(missing, `这些 token 被引用但没有定义：${missing.join(', ')}`).toEqual([])
  })

  /**
   * ⭐ F2（终审 fix 轮）——**`--radius-full` 的映射一行都删不得**。
   *
   * ⚠️ 它存在的理由 = 终审实测：把 `@theme inline` 里的
   * `--radius-full: var(--v-radius-full);` **删掉** ⇒ **92 条全绿 + build 干净**，
   * 而**产物**退回 `.rounded-full{border-radius:2147483647px}`（粗野主义下 5 处圆药丸
   * 的方角修复**静默回滚**）。
   * ⛔ 为什么别的东西拦不住：上面那条"引用完整性"是**反向**的——它查"被引用的
   * `--v-*` 有没有定义"，删掉的是**映射**（`@theme` 一侧），不是引用（`var()` 一侧）；
   * 而 parity / `STRUCTURAL` 表守的都是**五块主题里的值**（`--v-radius-full: 9999px/0px`），
   * 五块照旧一个不少 ⇒ **只有"@theme 里那行还在不在"这件事没有任何东西在问**。
   * ⇒ 判据必须落在**映射自身**上，且必须问**值**（`var(--v-radius-full)`）：写成
   * `includes('--radius-full')` 会被注释 / 别处的 `--v-radius-full` 满足，正是要挡的假绿。
   */
  test('@theme inline 必须把 --radius-full 接上（删这行 = 静默回滚 R6，其余全绿）', () => {
    const theme = ALL.find((b) => b.selector === '@theme inline')
    expect(theme, "index.css 里找不到 '@theme inline' 块").toBeTruthy()
    const m = theme!.body.match(/--radius-full\s*:\s*([^;]+)/)
    expect(
      m,
      '@theme inline 里没有 --radius-full 映射：rounded-full 家族会退回 2147483647px 字面量',
    ).toBeTruthy()
    expect(m![1].trim(), '--radius-full 必须接 var(--v-radius-full)，否则五块主题的值等于没接上').toBe(
      'var(--v-radius-full)',
    )
  })

  // ⚠️ F1 的守卫**只守默认块**，而且**不守值**（parity 只比名字、不比值的同族缺口）：
  // 改默认块的字体栈不会有任何测试变红。它守的是"T1 的目标：默认主题 = 今天的基线"
  // 这件事的**唯一机械抓手**——一旦有人把 `system-ui` 挪到前面，基线就破了。
  test("默认主题的字体栈必须 'Segoe UI' 打头（M7 之前渲染拉丁文的就是它；改了=破基线）", () => {
    // 实测指纹：'AaBbGg123 !@#' 宽 —— "Segoe UI" 146.221 / Tailwind 旧栈 146.221（相同）
    // vs system-ui（本机→雅黑）158.857。数字见 T1 报告 §4 发现 B。
    expect(v('default', '--v-font-sans').startsWith("'Segoe UI'")).toBe(true)
  })
})

/**
 * 按**花括号层级**切规则：每条规则带上它**由外到内的祖先块**序列。
 * ⚠️ 为什么不能用 `blocks()` 那条 `/([^{}]*)\{([^{}]*)\}/`：它匹配不了嵌套块，
 * 遇到 `@layer base { … { … } }` 时会把**内层**规则当成一个**顶层**块（T1 已实测），
 * ⇒「这条规则在不在某个 `@layer base` 里」这个问题**根本问不出来**（不是答错，是问不出）。
 */
function rulesWithAncestors(css: string): { selector: string; ancestors: string[] }[] {
  const out: { selector: string; ancestors: string[] }[] = []
  const stack: string[] = []
  let buf = ''
  let quote: string | null = null
  for (let i = 0; i < css.length; i++) {
    const c = css[i]
    if (quote !== null) {
      buf += c
      if (c === quote && css[i - 1] !== '\\') quote = null
      continue
    }
    if (c === '"' || c === "'") {
      quote = c
      buf += c
      continue
    }
    if (c === '{') {
      stack.push(buf.trim().replace(/\s+/g, ' '))
      buf = ''
      continue
    }
    if (c === '}') {
      out.push({ selector: stack.pop()!, ancestors: [...stack] })
      buf = ''
      continue
    }
    buf += c
  }
  return out
}

/**
 * ⭐ F4 守卫。**它存在的原因 = reviewer 实测**：把 `button, input, textarea, select`
 * 外面那层 `@layer base` 去掉、规则一字不动 ⇒ **68/68 全绿 + build 成功**（0 条红）。
 * 而它的失败形态是本项目的"假绿家族"：那条规则一旦不属任何层，**未分层样式胜过一切
 * 分层样式** ⇒ 它压掉 utilities 层的 `text-*`，**所有按钮/输入框的字号偷偷跟着 UA 默认走**
 * （实测 8 个元素 12px → 16px、整页高度 1305 → 1287），而页面看着"只是有点怪"。
 * ⛔ 判据必须走**层级**：`CSS.includes('@layer base')` 之类的字符串包含会被注释或别的
 * `@layer` 满足，正是这里要挡的假绿。
 */
describe('表单控件规则的分层（F4 守卫）', () => {
  test('button,input,textarea,select 那条规则必须在 @layer base 之内（去掉层 = 0 条红）', () => {
    const WANT = ['button', 'input', 'select', 'textarea']
    const hits = rulesWithAncestors(CSS).filter((r) => {
      const parts = r.selector
        .split(',')
        .map((s) => s.trim())
        .sort()
      return parts.length === WANT.length && parts.every((p, i) => p === WANT[i])
    })
    expect(hits, 'index.css 里找不到 button,input,textarea,select 那条规则').toHaveLength(1)

    const rule = hits[0]
    const parent = rule.ancestors[rule.ancestors.length - 1] ?? '(未分层：没有任何外层块)'
    expect(
      parent,
      `这条规则直接待在 "${parent}" 里；未分层会压掉 utilities 层的 text-*`,
    ).toMatch(/^@layer\s+base\b/)
  })
})

/**
 * 结构维度的守卫：形状 / 字体 / 阴影 / 模糊 / 页面底图**也随主题走**。
 * 只换颜色的四套主题在这里会红——那正是这条断言存在的理由。
 *
 * ⚠️ brief（T3 Step 1）这段自带一个**内层** `const v = …`，它要求存在一个选择器
 * **恰好等于** `[data-theme='default']` 的块。**实测它解析不了 `'default'`**
 * （brief 原文照抄跑出来的红：「缺 [data-theme='default'] 块」，2026-09-22）：
 * 本设计里默认主题与 `:root` **共用一块**（选择器是 `:root,\n[data-theme='default']`，
 * 见文件首条用例），所以 `selector === "[data-theme='default']"` 恒为假。
 * ⇒ 这里**复用文件上方的模块级 `v()`**（它按 `includes` 找默认块，是内层版的严格超集），
 * 不留两份会漂移的取值助手。差异已写进 task-3-report.md 的偏差清单。
 *
 * ⭐ F3（fix 轮，reviewer 的变异 (e) 实测）：**这里此前是一组散点断言**，只钉了 9 个值
 * ⇒ 把 material 的 `--v-radius-md/lg` 与 `--v-surface-2` 三处**退回默认主题的值**，
 * 全量 **76/76 仍全绿**。据此改成**表驱动**：四套非默认主题 × 9 个结构 token 的
 * **字面期望值**逐字锁死（G8）。值一律照 `index.css` 那四块**抄死**，不写成
 * "读 CSS 再比 CSS"（空守卫的经典形状）——改主题就必须**同时**改这张表，那正是"锁"的
 * 代价，也是它的目的。
 * ⚠️ 这张表锁的**本来只有**结构 token；变异 (e) 的第三条腿（`--v-surface-2` 是**颜色**，
 * 退回默认值仍全绿，我复跑过）当时**不在**覆盖面内，缺口记在 task-3-fix-report.md §6.4。
 * ⭐ F5（终审 fix 轮）：那条缺口**已按 reviewer 的裁决补上**——`--v-surface-2` 现在也在
 * 下面这张表里（每套末尾那一行）。理由不是"它属于结构"，而是**它的值此前没有任何东西在问**：
 * `themePalette.test.ts` 那条"面不等于默认"的用例只点名 `--v-surface` / `--v-bg`，
 * 而两条对比度判据**只是拿它当底算数**——值退化了它们会算出一个**不同的数**，照样可能全绿
 * （终审实测：四套一起退回默认值 = 全量 76/76 全绿）。⛔ 别把这条再挪出表外。
 *
 * ⭐ M7-P4 换成三行：`--v-wallpaper` / `--v-wallpaper-size` / `--v-wallpaper-pos`
 * （**固定壁纸层**的图 / 尺寸 / 定位）。为什么它们**也是结构值**：`-size` / `-pos` 决定
 * "这套主题的壁纸占哪块地方、有几层"，与圆角/模糊是同一类东西；而 `--v-wallpaper`
 * 本身是"这套主题**有没有**壁纸资产"的开关（`none` 与一个 data URI 是两种主题身份）。
 * ⚠️ 四套里两套（material / brutalism）的 `--v-wallpaper` 是 `'none'`，**照样写进表**：
 * "某套忘了给这份值"（继承 `:root`）由 parity 挡，而这里挡的是另一件事——**将来给某套
 * 加壁纸时顺手把值写进 `body::before{}`**（那就等于把主题资产又拉回全局，而
 * `body::before{}` 没有任何测试在比"值"）。
 * ⚠️ `--v-wallpaper` 的**值**只在"短到能逐字比"时才进表（`'none'`）；蒸汽波是 1.4KB 的
 * SVG data URI、玻璃是两个大 radial —— 与本文件对 `--v-page-image` 的既有处理一致
 *（`toContain` 只问形状；理由见文件末尾那段），另外**层数**在末尾单独钉住。
 * ⚠️ 默认块那三份值（`none` / `auto` / `center top`）在文件末尾与 `--v-shadow-card`
 * 同一簇断言里（那里的判据是"默认主题**没有**被主题化"）。
 */
const STRUCTURAL: Record<string, Record<string, string>> = {
  material: {
    '--v-radius-md': '0.5rem',
    '--v-radius-lg': '0.75rem',
    '--v-radius-xl': '0.75rem',
    '--v-radius-full': '9999px', // R6：Material 3 的 chip 是全圆
    '--v-shadow-card': '0 1px 2px rgba(0, 0, 0, 0.3), 0 1px 3px 1px rgba(0, 0, 0, 0.15)', // 抬升
    '--v-header-blur': '0px',
    '--v-panel-blur': '0px',
    '--v-font-sans': "'Roboto', system-ui, 'Segoe UI', 'Microsoft YaHei', sans-serif",
    '--v-tracking': 'normal',
    '--v-wallpaper': 'none', // P4：M3 的底是纯色 + tonal surface，没有壁纸资产
    '--v-wallpaper-size': 'auto',
    '--v-wallpaper-pos': 'center top',
    '--v-surface-2': '#e8def8', // F5：M3 secondary-container（选中态的底，= 它的 --v-accent-soft）
    // ⭐ T7：动效 token 也进这张表——它们是**主题身份**（180ms vs 0ms vs 320ms 是三种性格），
    // 不只是"能跑就行"的实现细节。四套 × 5 = 20 条，表驱动写法不变。
    '--v-motion-dur': '180ms',
    '--v-motion-ease': 'cubic-bezier(0.2, 0, 0, 1)',
    '--v-ripple': 'rgba(103, 80, 164, 0.22)',
    // T7 fix2：涟漪的时长（= 2 × 180ms）。⚠️ 它**不只是给 CSS 用的**——v3 机制下
    // `RippleLayer.tsx` 把它读出来当 WAAPI 的 `duration`（载体已搬到 `document.body` 的图层上，
    // 不再是 v2 那个 `button.vigil-rippling::after` 伪元素，注释里的"伪元素"二字随之作废）。
    '--v-ripple-dur': '360ms',
    '--v-press-scale': '1',
  },
  brutalism: {
    '--v-radius-md': '0px', // 方角是粗野主义的判据
    '--v-radius-lg': '0px',
    '--v-radius-xl': '0px',
    '--v-radius-full': '0px', // R6：方角**到底**（此前 rounded-full 家族是字面量，5 处圆药丸漏网）
    '--v-shadow-card': '4px 4px 0 0 #0a0a0a', // 硬阴影承担"厚边框"（E5）
    '--v-header-blur': '0px',
    '--v-panel-blur': '0px',
    '--v-font-sans': "ui-monospace, 'Cascadia Mono', Consolas, 'Microsoft YaHei', monospace",
    '--v-tracking': '0.02em',
    '--v-wallpaper': 'none', // P4：网格纸是**纹理**（该跟着内容滚）⇒ 留在 --v-page-image
    '--v-wallpaper-size': 'auto',
    '--v-wallpaper-pos': 'center top',
    '--v-surface-2': '#ffe600', // F5：选中态**纯黄**（这套主题唯一的高亮色，退回默认=选中没了）
    // ⭐ T7：**粗野主义的 0ms 是这套主题的判据**——退回默认的 150ms 会让"别动"变成"慢慢动"，
    // 而屏幕上只是"有点不对"（这正是这张表存在的理由，见文件上方 F3 那段）。
    '--v-motion-dur': '0ms',
    '--v-motion-ease': 'linear',
    '--v-ripple': 'transparent',
    // ⭐ T7 fix：0ms（= 2 × 0ms）。这套主题的涟漪本来就关着，但**值必须给**
    //——parity 只比名字；而"给了 0ms 还是继承默认的 300ms"这件事只有这张表看得见。
    '--v-ripple-dur': '0ms',
    '--v-press-scale': '1',
  },
  vaporwave: {
    '--v-radius-md': '0.5rem',
    '--v-radius-lg': '0.75rem',
    '--v-radius-xl': '1rem',
    '--v-radius-full': '9999px', // R6
    '--v-shadow-card':
      '0 0 0 1px rgba(0, 229, 255, 0.28), 0 0 20px rgba(255, 52, 182, 0.35)', // 辉光收边
    '--v-header-blur': '12px',
    '--v-panel-blur': '6px',
    '--v-font-sans': "system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif",
    '--v-tracking': '0.02em',
    // ⭐ P4：壁纸层两层（落日 + 天空）**逐位对应**，都是"顶部同一条"。
    // ⚠️ `100% 62%` 是**百分比**，不是 `520px` / `62vh`——手机上地址栏收起会让 vh 抖。
    // ⚠️ 位序/层数错了不会报错，只会"某层尺寸被套到另一层上"——所以这里逐字锁死
    //（层数在文件末尾与 `--v-wallpaper` 的形状断言并排钉住）。
    '--v-wallpaper-size': '100% 62%, 100% 62%',
    '--v-wallpaper-pos': 'center top, center top',
    '--v-surface-2': 'rgba(64, 16, 82, 0.84)', // F5：R7 的暗玻璃**选中态**（alpha 是照判据定的）
    // ⭐ T7：慢一档 + 回弹（`cubic-bezier(0.34, 1.2, …)`，y>1 就是过冲）。
    '--v-motion-dur': '320ms',
    '--v-motion-ease': 'cubic-bezier(0.34, 1.2, 0.64, 1)',
    '--v-ripple': 'rgba(0, 229, 255, 0.28)',
    '--v-ripple-dur': '640ms', // T7 fix：= 2 × 320ms
    '--v-press-scale': '0.98',
  },
  glass: {
    '--v-radius-md': '0.5rem',
    '--v-radius-lg': '0.75rem',
    '--v-radius-xl': '1rem',
    '--v-radius-full': '9999px', // R6
    '--v-shadow-card': '0 8px 32px rgba(12, 8, 32, 0.45)',
    '--v-header-blur': '18px', // 大模糊 + 半透明白面板是这套主题的身份
    '--v-panel-blur': '14px',
    '--v-font-sans': "system-ui, -apple-system, 'Segoe UI', 'Microsoft YaHei', sans-serif",
    '--v-tracking': 'normal',
    // ⭐ P5：壁纸是两个**大几何块**（左上橙红 / 右下青绿），各占整个视口 ⇒ `100% 100%`。
    // 值不写进表（两行长 radial，理由同文件末尾那段），只锁尺寸/定位 + 末尾的层数。
    '--v-wallpaper-size': '100% 100%, 100% 100%',
    '--v-wallpaper-pos': '0 0, 0 0',
    '--v-surface-2': 'rgba(34, 42, 80, 0.68)', // F5：R8 的暗玻璃**选中态**（压在 #22ff88 上定的）
    // ⭐ T7：柔和那一档。
    '--v-motion-dur': '240ms',
    '--v-motion-ease': 'cubic-bezier(0.2, 0, 0, 1)',
    '--v-ripple': 'rgba(255, 255, 255, 0.25)',
    '--v-ripple-dur': '480ms', // T7 fix：= 2 × 240ms
    '--v-press-scale': '0.98',
  },
  // ⭐ T8-1（Project ARGUS，第六套）：值是照 `index.css` 那张块**抄死**的，不写成"读 CSS 再比 CSS"。
  // ⚠️ 这一套有两处**故意偏离**另外几套的形态，别当成抄错：
  //   ① `--v-radius-full: 2px` —— 不是 9999px：终端没有药丸，四档圆角一律 2px；
  //   ② `--v-ripple-dur: 0ms` —— **不守**"= 2 × --v-motion-dur"那条规律（那会算出 180ms），
  //      因为这套主题的涟漪整个关掉了（`--v-ripple: transparent`），时长自然归零。
  //      （粗野主义也是 0ms，但那里的 0ms 是"动效本来就 0"的副产品，两种理由不同。）
  argus: {
    '--v-radius-md': '2px',
    '--v-radius-lg': '2px',
    '--v-radius-xl': '2px',
    '--v-radius-full': '2px',
    '--v-shadow-card': '0 0 0 1px rgba(0, 255, 156, 0.18)', // 终端不投影，改用一圈细描边
    '--v-header-blur': '0px',
    '--v-panel-blur': '0px',
    '--v-font-sans': "ui-monospace, 'Cascadia Mono', Consolas, 'Microsoft YaHei', monospace",
    '--v-tracking': '0.02em',
    '--v-wallpaper': 'none', // P4：终端不要美术资产（三件套都是"无"）
    '--v-wallpaper-size': 'auto',
    '--v-wallpaper-pos': 'center top',
    // ⚠️ 这份值与 `argusColor.ts` 的 `deriveArgusVars()` 对同一个基色算出来的**逐位相同**
    //（alpha 0.14）——不同的话，"没跑过 /color"与"/color 成同一个色"会长得不一样，
    // 而这种差异**只有真浏览器里并排看**才看得出来（没有别的东西会红）。
    '--v-surface-2': 'rgba(0, 255, 156, 0.14)',
    '--v-motion-dur': '90ms', // 终端那一档：短 + 分步跳变
    '--v-motion-ease': 'steps(2, end)',
    '--v-ripple': 'transparent',
    '--v-ripple-dur': '0ms', // 见上：这套不守 2× 规则
    '--v-press-scale': '1',
  },
}

/**
 * ⚠️ 为什么这 9 个 token 要**锁死字面值**（而不是只断言"与默认不同"）：主题身份一旦被
 * 顺手改回默认，外观主题就**名存实亡**，而肉眼看不出是哪一处退化了——圆角从 `0.5rem`
 * 退回 `0.375rem`、面板模糊从 `12px` 退回 `0px`，都只让人"觉得有点不对"。
 * 反过来，"与默认不同"是**太弱**的判据：把 `0.5rem` 改成 `0.6rem` 它也过，
 * 而那是没人批准过的外观改动。
 */
test('结构维度每套主题各给一份（F3 + P4：结构 token 的字面值表驱动锁死，不写死个数）', () => {
  for (const [theme, want] of Object.entries(STRUCTURAL)) {
    for (const [token, expected] of Object.entries(want)) {
      expect(v(theme, token), `${theme} 的 ${token} 退化了（主题身份靠它）`).toBe(expected)
    }
  }
  // 表没覆盖的几件事，单独留在这里（不是散点断言，是几条问不进表里的问题）：
  // ① `--v-page-image` / `--v-wallpaper` 是**多行**值（蒸汽波那张壁纸还是 1.4KB 的
  //    SVG data URI），写进表里就成了"按空白逐字比"，脆且难读 ⇒ 只问**形状与层数**。
  //    ⚠️ 层数必须问：`--v-wallpaper-size` / `-pos` 是**按层序**的逗号列表，层数与
  //    列表长度对不上时 CSS **不报错**，只是静默错位（P2 的教训）。
  const countOf = (s: string, w: string) => s.split(w).length - 1
  expect(v('brutalism', '--v-page-image')).toContain('repeating-linear-gradient') // 网格纸
  expect(v('vaporwave', '--v-page-image')).toContain('repeating-linear-gradient') // 霓虹网格
  expect(countOf(v('vaporwave', '--v-page-image'), 'repeating-linear-gradient')).toBe(2) // 横 + 纵
  // 落日：**必须是 SVG**（CSS 渐变做不出"被横条切开的圆"）；天空那层是唯一一个
  // `linear-gradient`（SVG 里的 `linearGradient` 大小写不同，数不进来）。
  expect(v('vaporwave', '--v-wallpaper')).toContain('data:image/svg+xml')
  expect(countOf(v('vaporwave', '--v-wallpaper'), 'linear-gradient')).toBe(1)
  // 玻璃：两个**大几何块**（左上橙红 / 右下青绿），与参考图同构。
  expect(countOf(v('glass', '--v-wallpaper'), 'radial-gradient')).toBe(2)
  // ② 默认块是 T1 冻结的基线（"默认主题 = 今天"）：这几条是"它**没有**被主题化"的抓手。
  //    `--v-radius-full: 9999px` 同理——它是 R6 新增的、唯一进了冻结块的新 token，
  //    值必须保持"与内置字面量同像素"（见 index.css 里 @theme inline 那条注释）。
  //    ⭐ P4 补 `--v-wallpaper`：默认主题一旦长了壁纸，就不是"今天"了。
  expect(v('default', '--v-shadow-card')).toBe('none')
  expect(v('default', '--v-page-image')).toBe('none')
  expect(v('default', '--v-wallpaper')).toBe('none')
  expect(v('default', '--v-radius-full')).toBe('9999px')
  // ③ ⭐ T7 fix 补：**默认块**的 `--v-ripple-dur`。
  //    为什么非要单独锁一份：上面那张 `STRUCTURAL` 表**只覆盖四套非默认主题**
  //    （默认块与 `:root` 共用一块，见文件首条用例），而 parity **只比名字、不比值**
  //    ⇒ 默认这份的值此前**没有任何东西在问**：把它写成 `500ms`、或误抄成
  //    `--v-motion-dur` 的 `150ms`，全量照样全绿。而默认主题是全站份额最大的一套，
  //    "克制那一档"的手感就靠这个数。
  //    值 = 2 × `--v-motion-dur`(150ms)，与另外四套同一条规则（各套值见上表）。
  expect(v('default', '--v-ripple-dur')).toBe('300ms')
  // ④ ⭐⭐ C2（delta 终审 Critical）：**ARGUS 的底必须是纯黑**，值逐字写死。
  //
  //    为什么这条非有不可：`src/argusColor.ts` 的整个闸门是**对纯黑底反推**出来的——
  //    `MIN_LUMINANCE = 0.175` 就是"L ≥ 0.175 ⇒ (L+0.05)/0.05 ≥ 4.5:1 **对纯黑**"解出来的，
  //    而派生检查（C1）的每一个对比度也都以 `ARGUS_BG = [0,0,0]` 为底。`index.css` 里
  //    那段注释把这件事写清楚了，**但注释不是判据**：把 `--v-bg: #000000` 改成 `#101010`
  //    （"很深的灰"，看着一样黑）⇒ 边界见证 `#757575` 仍被收、`#747474` 仍被拒、
  //    `#101010` 仍被拒，**全量 146 条一条不红**（delta 终审变异 M-O 实测），
  //    而那个常数与它声称的 4.5:1 已经**不再对应同一个底**——
  //    判据还在、还是绿的，但它验的东西已经不存在了（本项目最贵的那一类）。
  //    ⚠️ 为什么不是"让它跟着 `--v-bg` 走"：那要把 `#000000` 从 `argusColor.ts`
  //    一路喂到闸门里（多一条跨文件耦合），而这里要守的恰恰是**这条前提本身**：
  //    终端那一套的观感与全部对比度数字**都建立在"底是纯黑"之上** ⇒ 换底不是配色微调，
  //    是**整套闸门的前提**。真要换，先来改这条断言，并同改 `argusColor.ts` 的 `ARGUS_BG`。
  expect(v('argus', '--v-bg')).toBe('#000000')
})
