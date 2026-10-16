import { expect, test } from 'vitest'
import { THEMES } from '../theme.ts'

// ⚠️ Node 内置模块的写法与理由见 `noRawPalette.test.ts` 顶部那段（M7 勘误 E9 的两条墙）
const NODE_FS = 'node:' + 'fs'
const HERE = import.meta.url
const { readFileSync } = (await import(/* @vite-ignore */ NODE_FS)) as {
  readFileSync: (p: URL, enc: string) => string
}

/**
 * ⭐ 调色板的**可读性守卫**。它回答两个肉眼会漏的问题：
 *   ① 浅色主题**真的**是浅色吗（出口判据 4 的反面：深色残留）
 *   ② 弱化文字 / 软底按钮上的文字**读得清**吗（主题化最容易在"某一套"上翻车）
 *
 * ⚠️ 期望值一律**逐字写死**，绝不写成"读 CSS 再比 CSS"（空守卫的经典形状）。
 * ⚠️ 已知近似：玻璃主题的真实底是**渐变**，而这里只能拿 `--v-bg`（不透明基色）算
 *    ⇒ 故 `--v-bg` 取渐变中位色。这条近似**写在这里**，不许被说成"渐变也验过了"。
 * ⚠️ 剥注释再解析（理由同 `themeTokens.test.ts`：注释里含示例 CSS）。
 */
const CSS = readFileSync(new URL('../index.css', HERE), 'utf8').replace(
  /\/\*[\s\S]*?\*\//g,
  '',
)

type RGBA = [number, number, number, number]

function value(theme: string, token: string): string {
  const m = CSS.match(
    new RegExp(`\\[data-theme='${theme}'\\][^{]*\\{[^}]*${token}\\s*:\\s*([^;]+);`),
  )
  if (m === null) throw new Error(`找不到 ${theme} 的 ${token}`)
  return m[1].trim()
}

function parse(s: string): RGBA {
  const hex = s.match(/^#([0-9a-f]{6})$/i)
  if (hex !== null) {
    const n = parseInt(hex[1], 16)
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255, 1]
  }
  const rgba = s.match(/rgba?\(([^)]+)\)/)
  if (rgba !== null) {
    const p = rgba[1].split(',').map((x) => parseFloat(x.trim()))
    return [p[0], p[1], p[2], p.length > 3 ? p[3] : 1]
  }
  throw new Error(`不认识的色值：${s}`)
}

/** 半透明层叠在不透明底上（默认主题的面几乎都是 rgba，不算这步全错） */
function over(top: RGBA, bottom: RGBA): RGBA {
  const a = top[3]
  return [
    top[0] * a + bottom[0] * (1 - a),
    top[1] * a + bottom[1] * (1 - a),
    top[2] * a + bottom[2] * (1 - a),
    1,
  ]
}

function luminance([r, g, b]: RGBA): number {
  const f = (c: number) => {
    const x = c / 255
    return x <= 0.03928 ? x / 12.92 : ((x + 0.055) / 1.055) ** 2.4
  }
  return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b)
}

function contrast(a: RGBA, b: RGBA): number {
  const [hi, lo] = [luminance(a), luminance(b)].sort((x, y) => y - x)
  return (hi + 0.05) / (lo + 0.05)
}

test('四套非默认主题的两个"面"色都不等于默认主题（否则它没换过）', () => {
  for (const t of THEMES.filter((x) => x.id !== 'default')) {
    expect(value(t.id, '--v-surface'), `${t.id} 的 --v-surface`).not.toBe(
      value('default', '--v-surface'),
    )
    expect(value(t.id, '--v-bg'), `${t.id} 的 --v-bg`).not.toBe(value('default', '--v-bg'))
  }
})

test('浅色主题真的是浅色、深色主题真的是深色（出口判据 4 的静态形式）', () => {
  const light = ['material', 'brutalism']
  for (const t of THEMES) {
    const L = luminance(parse(value(t.id, '--v-bg')))
    if (light.includes(t.id)) {
      expect(L, `${t.id} 的 --v-bg 应该是浅色`).toBeGreaterThan(0.5)
    } else {
      expect(L, `${t.id} 的 --v-bg 应该是深色`).toBeLessThan(0.5)
    }
  }
})

test('正文与弱化文字在面板上都读得清（WCAG：正文 ≥4.5 / 弱化文字 ≥3）', () => {
  for (const t of THEMES) {
    const bg = parse(value(t.id, '--v-bg'))
    // ⚠️ 软底 / 危险按钮的底**也是半透明的**（默认主题与蒸汽波、玻璃都是 rgba）
    // ⇒ 必须先合成到**它所在的那个面**上再算对比度。拿**未合成**的色值去比，等于把
    // 「深底上的 20% 浅蓝」当成「浅蓝实底」——结论正好反过来：实测默认主题
    // `--v-accent-soft` 未合成时按 #38bdf8 算 ⇒ 1.61（对卡片；**假红**，而默认块是 T1
    // 冻结的基线、不许改值）；合成到面上后 ⇒ **7.65**（真值，两个面里更差的那个）。
    // ⚠️ 判据（≥4.5 / ≥3）一个字没动，动的是"底取哪些值"。这条修正的完整依据
    // （四套主题两种算法的逐项数字）见 task-3-report.md 偏差 1。
    //
    // ⭐ F2（fix 轮，reviewer 实测）：文字**不是只坐在卡片上**——选中行 / 输入框 focus /
    // 按钮 active 的底是 `--v-surface-2`，它通常比卡片更浅、更"贴近"文字 ⇒
    // 每条文字类断言**对两个面各算一次，取更差的那个**。实测差距不小：glass 的
    // `--v-accent-text` 在卡片上 6.17、在 `--v-surface-2` 上只有 **4.42**（差 1.75），
    // 而 4.42 才是屏幕上真会发生的那个数。
    const faces = [
      { name: '--v-surface', rgba: parse(value(t.id, '--v-surface')) },
      { name: '--v-surface-2', rgba: parse(value(t.id, '--v-surface-2')) },
    ]

    /**
     * 一条断言的**屏幕真值**：`text` 是文字 token，`chip` 是可选的"软底"token
     * （`null` ⇒ 文字直接坐在面上）。
     * 合成顺序照真实堆叠走：`--v-surface*` 先叠到 `--v-bg`（两者都半透明），
     * 软底再叠到那个面上；**文字那一侧也要叠**（F2b，见下）。
     */
    const worst = (text: string, chip: string | null) =>
      faces
        .map((f) => {
          const bottom = over(f.rgba, bg)
          const painted = chip === null ? bottom : over(parse(value(t.id, chip)), bottom)
          // ⭐ F2b（fix 轮，reviewer 实测）：**文字侧不合成 = 数字虚高**。
          // `--v-fg-3` 这类是 `rgba(255,255,255,.55)` 半透明色，只 `parse()` 不 `over()`
          // 等于拿"屏幕上不存在的颜色"去比：实测默认主题（对卡片）读 17.29、真值 **6.01**；
          // glass 读 10.96、真值 6.30。今天这两条都达标（**没有假绿**），但留着就是一类
          // **可构造的假绿**：把某个 `--v-*text` 写成与它软底同值的半透明色时，断言读到
          // 5.53（过）、屏幕真值 1.45（几乎看不见）。
          const ink = over(parse(value(t.id, text)), painted)
          return { ratio: contrast(ink, painted), where: `${chip ?? '(裸文字)'} @ ${f.name}` }
        })
        .reduce((a, b) => (b.ratio < a.ratio ? b : a))

    const body = worst('--v-fg', null)
    const muted = worst('--v-fg-3', null)
    const soft = worst('--v-accent-text', '--v-accent-soft')
    const danger = worst('--v-danger-text', '--v-danger-btn')

    // ⭐ R7（fix 轮，补一条**没人守的**文字 token）：`--v-accent` **也当文字用**
    // （`text-vigil-accent`：链接，以及刚修好的两个按钮），而**全仓此前没有任何判据
    // 守它的可读性**——上一轮是修订者**手算**的（最低 4.98，落在 material）。
    // ⚠️ 手算不是判据：它不进 CI、也不挡"下次顺手改个色值"。而这类 token 恰好最容易被
    // 改坏——material 的 `--v-accent`（#6750a4）离它的面（#e8def8）只差 0.48。
    // 判据按**正文级**（≥4.5）：accent 当文字用时与正文同重，它不是"弱化文字"。
    // ⚠️ 面取两个里**更差**的那个、文字侧同样 `over()` 合成——与上面四条同一写法。
    // ⚠️ 变异反证：把 material 的 `--v-accent` 改成 `#eaddff`（≈近底色）⇒ 本条必红。
    const accent = worst('--v-accent', null)

    // 断言消息带上"最差的那个面"：只写"glass 软底按钮文字 4.42"看不出是哪个面在拉低
    expect(body.ratio, `${t.id} 正文/面（最差面 ${body.where}）`).toBeGreaterThanOrEqual(4.5)
    expect(muted.ratio, `${t.id} 弱化文字/面（最差面 ${muted.where}）`).toBeGreaterThanOrEqual(3)
    expect(soft.ratio, `${t.id} 软底按钮文字（最差面 ${soft.where}）`).toBeGreaterThanOrEqual(3)
    expect(danger.ratio, `${t.id} 危险按钮文字（最差面 ${danger.where}）`).toBeGreaterThanOrEqual(3)
    expect(
      accent.ratio,
      `${t.id} 强调文字/面（--v-accent 当文字用；最差面 ${accent.where}）`,
    ).toBeGreaterThanOrEqual(4.5)
  }
})

/**
 * ⭐ R8（= 计划 **E14** 的落地）——**判据的"语境模型"漏了整个图层，就等于没判**。
 *
 * 上面那条把面合成到 `--v-bg`（不透明的基色）上，于是它对"面板压在**壁纸**上"这件事
 * 一无所知：有壁纸的主题里，卡片真正压着的是壁纸**最亮的那块像素**。实测（glass，
 * R8 之前）：压在最亮像素上正文 **4.32**、弱化文字 **2.93** —— 屏幕上已经读不出来，
 * 而断言 8/8 全绿。这不是值写错了，是**判据的语境少了一个图层**。
 * 这条把那个图层补进语境：面合成到最亮像素上，再算 4.5 / 3（阈值一个字没动）。
 */
// 各"有壁纸"主题的**最亮像素**（人肉从 `--v-wallpaper` 的渐变栈里找的最亮 stop）：
//   vaporwave 的 `#fff3a3` 是落日中心；glass 的 `#22ff88` 是右下绿圆最亮的 stop。
// ⚠️ **改壁纸必须回来改这张表**（并把渲染轮重跑一遍）——否则它就是一条会过期的判据：
//    壁纸变更亮而表没更新 ⇒ 判据仍然全绿，而屏幕上的字已经糊了。
// ⚠️ "最亮"是**逐个算过**的，不是看着挑的——P5 就栽在这一步：当时挑的是橙圆的 `#ffb01f`
//    （"壁纸最亮的一块"），而 WCAG 相对亮度 `#ffb01f` = **0.524** < `#22ff88` = **0.736**。
//    全部 stop 的 L（哪个最大一目了然）：
//      vaporwave 落日：`#fff3a3` **0.880** / `#ffb347` 0.540 / `#ff5e8a` 0.311 / `#ff2d95` 0.253
//      glass 橙红圆：`#ffb01f` 0.524 / `#ff6a1f` 0.317 / `#ff2d78` 0.245
//      glass 青绿圆：`#22d3ee` 0.531 / `#3b82f6` 0.236 / `#22ff88` **0.736** ← 全表最大
const WALLPAPER_BRIGHTEST: Record<string, string> = { vaporwave: '#fff3a3', glass: '#22ff88' }

test('面板压在壁纸的最亮像素上，正文与弱化文字仍读得清（R8 / E14）', () => {
  // ⚠️ 先堵住这张表**自己**的过期形态：表是手写的，而"整张表被悄悄挖空"与"全绿"
  // 长得一模一样（空守卫的经典形状）。有壁纸的主题一个都不能漏。
  for (const t of THEMES) {
    if (value(t.id, '--v-wallpaper') !== 'none') {
      expect(
        Object.keys(WALLPAPER_BRIGHTEST),
        `${t.id} 有壁纸，却不在 WALLPAPER_BRIGHTEST 里——表漏了它，这条判据就没守它`,
      ).toContain(t.id)
    }
  }

  for (const [id, brightest] of Object.entries(WALLPAPER_BRIGHTEST)) {
    const px = parse(brightest)
    // ⚠️ 这里**不**先把面叠到 `--v-bg` 上：卡片压住的就是那颗壁纸像素本身。
    // 文字侧照上面同一条写法 `over()` 合成（半透明字色不合成 = 数字虚高，见 F2b）。
    const ratio = (text: string, face: string) => {
      const bottom = over(parse(value(id, face)), px)
      return contrast(over(parse(value(id, text)), bottom), bottom)
    }
    // ⭐ F6（终审 fix 轮）：`--v-inset` **也进来了**——它是凹槽（输入框 / 小标签的底），
    // 与两个面是**同一件事**（屏幕上都压着同一颗壁纸像素），此前却只被"人记得"：
    // glass 的 inset 是 R8 那轮**顺手**一起改的（`rgba(10,14,30,.5)` → `rgba(8,10,24,.55)`），
    // 理由是"判据只点名 --v-surface / --v-surface-2" ⇒ **没人守它**，下次退化就是 0 条红。
    // ⚠️ 阈值**一个字没动**（正文 4.5 / 弱化 3），进判据的只是"底多算一个 token"。
    // 实测（合成到各自的最亮像素上）：glass fg 5.49 / fg-3 3.54、vaporwave fg 10.34 / fg-3 4.60。
    for (const face of ['--v-surface', '--v-surface-2', '--v-inset']) {
      expect(
        ratio('--v-fg', face),
        `${id} 正文 / ${face} 压在壁纸最亮像素 ${brightest} 上`,
      ).toBeGreaterThanOrEqual(4.5)
      expect(
        ratio('--v-fg-3', face),
        `${id} 弱化文字 / ${face} 压在壁纸最亮像素 ${brightest} 上`,
      ).toBeGreaterThanOrEqual(3)
    }
  }
})
