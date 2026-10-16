import { useEffect } from 'react'
import { cssTimeToMs } from '../cssTime.ts'

/**
 * 全局触点涟漪（**事件委托** + **挂在 `document.body` 上的独立图层**）。
 *
 * 为什么不"每个按钮包一层组件"：本仓有 60+ 个 button/chip/tab，逐个改会把「动效」这件事
 * 焊进每个调用点——而它其实是**主题资产**（每套主题自己给 `--v-ripple`、`--v-ripple-dur`
 * 与 `--v-motion-ease`）。⇒ 一个挂在 `document` 上的委托监听器；`--v-ripple` 是空/
 * `transparent` 时**整个机制自关**（默认主题与粗野主义就是这一档：零涟漪、零开销）。
 *
 * ⭐⭐ **载体为什么必须搬出"React 管的节点"**（本文件存在的全部理由；前两版都栽在这上面）：
 *   ① v1：`el.appendChild(span)` 把涟漪 `<span>` 插进按钮 ⇒ React 19.3.0 的
 *      `setTextContent`（`react-dom-client.development.js:2834`）在按钮**文案更新**时走
 *      `node.textContent = text`（:2846——因为 `firstChild`（文本节点）≠ `lastChild`（span），
 *      快路径 `firstChild === lastChild && nodeType === 3` 不成立）⇒ 活着的 span 被整个抹掉。
 *   ② v2：涟漪改成按钮上的**类**（`button.vigil-rippling::after`）⇒ React 写 `class` 走
 *      `setValueForKnownAttribute`（同文件 :1698 的 `setAttribute('class', …)`）⇒
 *      **类被一并抹掉**。这不是理论风险，有**活实例**：`views/Feed.tsx:416` 的 chip 与
 *      `views/Settings.tsx:33` 的 tab **点击时就会改 className**（选中态换底色）
 *      ⇒ 涟漪恰好在"点下去的那一瞬间"被清掉。复现（t7fix2 第 0 步，原始输出见
 *      `task-7-fix2-report.md` 第 0 步）：
 *        `class="bg-b text-y vigil-rippling"` → rerender 切 `active` →
 *        `class="bg-a text-x"`（写入栈：`setAttribute(class, "bg-a text-x")` @
 *        `setValueForKnownAttribute`），`classList.contains('vigil-rippling')` true → false。
 *   ③ **本版**：载体是 `document.body` 上的 `<div class="vigil-ripple-layer">`（+ 它里面的
 *      `.vigil-ripple-box` / `.vigil-ripple`）。React 只认 `#root` 这一棵树：body 的子节点
 *      它既不创建、也不 diff，更不会去写它们的 `class` ⇒ **上面两条写入路径结构上碰不到它**
 *      （⛔ 所以**不再**往按钮里插节点、也**不再**给按钮加/摘 class——那是两轮失败的全部原因）。
 *      ⚠️ 测试里那两条回归用例（切 className / 切文案之后涟漪仍在）守的正是这个**结构性**事实，
 *      而不是"某一次实现细节"。
 *
 * ⚠️ 三条边界（都有测试守）：
 * 1. **`prefers-reduced-motion` 必须尊重**——这是无障碍，不是可选装饰。
 * 2. **`disabled` 的按钮不插**（按不动的控件不该有反馈）。
 * 3. **右键不插**（`e.button !== 0`）——涟漪是"主键按下"的反馈。
 *
 * ⚠️ `main.tsx` 里本组件在 `StrictMode` 之下 ⇒ effect 会跑**两遍**（挂→卸→挂）。
 * 这是**正常**的，不是 bug：cleanup 把监听器**与图层**成对收掉（摘两条监听器 + 移除图层），
 * 重挂时再建一份 ⇒ **不会留下双监听器，也不会留下两个图层**。⛔ 别把双挂载当成需要修的缺陷。
 */

/** 涟漪的两个关键帧。⚠️ 与 `index.css` 的 `@keyframes vigil-ripple` **逐字同值**：
 *  动画由下面的 WAAPI 承担，那份 `@keyframes` 现在没有任何规则引用（保留它是当**规格说明**
 *  用——两个关键帧 + "只动 transform/opacity（合成器属性，不触发布局）"这条约束在那里一眼可见）。
 *  ⚠️ 改这里的数就要同步改那边，反之亦然。 */
const RIPPLE_KEYFRAMES: Keyframe[] = [
  { transform: 'scale(0)', opacity: 0.9 },
  { transform: 'scale(1)', opacity: 0 },
]

export default function RippleLayer() {
  useEffect(() => {
    // ⭐ 图层：**建一次**。它是本组件唯一的 DOM 产物，且挂在 React 完全不管的地方（见文件顶部 ③）。
    const layer = document.createElement('div')
    layer.className = 'vigil-ripple-layer'
    document.body.appendChild(layer)

    function onPointerDown(e: PointerEvent) {
      if (e.button !== 0) return
      // ⚠️ 泛型必须是 `HTMLButtonElement`（计划原稿写的是 `HTMLElement`）：`.disabled`
      // 只存在于 `HTMLButtonElement` 上 ⇒ 用 `HTMLElement` 会被 `tsc -b` 以 TS2339 拒绝
      // （实测：`Property 'disabled' does not exist on type 'HTMLElement'`，**构建直接失败**）。
      // 选择器本来就是 `'button'`，所以这个收窄与运行时行为**完全一致**。
      const el = e.target instanceof Element ? e.target.closest<HTMLButtonElement>('button') : null
      if (el === null || el.disabled) return
      if (window.matchMedia?.('(prefers-reduced-motion: reduce)').matches === true) return

      // ⚠️ 这几条读的是**计算值**（jsdom 不加载 CSS，所以测试里要在按钮上内联写这些 token）。
      // `--v-ripple` 同时是"这套主题要不要涟漪"的**唯一开关**——颜色本身由 CSS 的
      // `background: var(--v-ripple)` 去取（图层在 `body` 下，`data-theme` 挂在 `<html>` 上
      // ⇒ 那份 token 照样继承得到），这里不把它抄进内联 style（少一份会漂移的副本）。
      const cs = getComputedStyle(el)
      const color = cs.getPropertyValue('--v-ripple').trim()
      if (color === '' || color === 'transparent') return

      // ⚠️ 几何量都在**按钮**上量，坐标换算成**文档坐标**（`+ scrollX/scrollY`）——
      // 图层是 `position: absolute`（跟着文档走），普通内容的涟漪因此与内容一起滚。
      // 如实记一条副作用：**sticky 顶栏**（`App.tsx:73`）里的按钮在滚动中，涟漪会与按钮分离
      // ——顶栏停在视口顶端、涟漪按旧文档坐标留在原地；`--v-ripple-dur` 只有 300–640ms，
      // 且滚动中按压本就是少见手势，接受。
      const rect = el.getBoundingClientRect()
      const size = Math.max(rect.width, rect.height) * 2

      // ⭐ **两层**：外层盒子按按钮的几何裁切（代替原来按钮上的 `overflow: hidden`），
      // 内层才是会缩放的那颗涟漪。
      const box = document.createElement('span')
      box.className = 'vigil-ripple-box'
      box.style.left = `${rect.left + window.scrollX}px`
      box.style.top = `${rect.top + window.scrollY}px`
      box.style.width = `${rect.width}px`
      box.style.height = `${rect.height}px`
      // 圆角是**量**来的（按钮的圆角是本仓主题资产：`--v-radius-*`，方角 vs 药丸都有）
      // ⇒ 涟漪被裁成与按钮同一个轮廓，不会"撑破"圆角。
      const radius = cs.borderRadius.trim()
      if (radius !== '') box.style.borderRadius = radius

      const inner = document.createElement('span')
      inner.className = 'vigil-ripple'
      // 落点 = 触点相对按钮左上角（`clientX/Y` − `rect.left/top`），两个负 margin 把
      // `size × size` 的正方形回正到"以落点为中心"（与圆形涟漪的圆心一致）。
      inner.style.left = `${e.clientX - rect.left}px`
      inner.style.top = `${e.clientY - rect.top}px`
      inner.style.width = `${size}px`
      inner.style.height = `${size}px`
      inner.style.marginLeft = `${-size / 2}px`
      inner.style.marginTop = `${-size / 2}px`

      box.appendChild(inner)
      layer.appendChild(box)

      // ⚠️ 时长与曲线都取**主题 token**（`--v-ripple-dur` 比状态过渡慢一档；
      // `--v-motion-ease` 在 vaporwave 那套是带过冲的 `cubic-bezier(…, 1.2, …, 1)`
      // ——硬编码曲线会把那套主题的性格抹平）。取不到时给中性兜底（jsdom 不加载 CSS）。
      //
      // ⚠️⚠️ **时长必须走 `cssTimeToMs`，不能用 `parseFloat`**（M7 冒烟 F1，真缺陷）：
      // `parseFloat` 假设"值带 `ms` 单位 ⇒ `parseFloat('360ms') = 360`"，但**压缩后的产物**
      // 把它写成了 **`.36s`**（`vite build` / esbuild 的改写，`dist/assets/index-*.css` 实测）
      // ⇒ `parseFloat('.36s') = 0.36`（当成毫秒）= **0.36ms**，涟漪在线上等于不存在。
      // jsdom 不加载 CSS、dev 模式送的是 `360ms` ⇒ 这个单位陷阱**只在构建后出现**，
      // 解析器本身由 `cssTime.test.ts` 的单测钉住（含 `.36s ⇒ 360` 那条），
      // "涟漪真的看得见"归冒烟。详见 `../cssTime.ts` 的注释。
      const parsedDur = cssTimeToMs(cs.getPropertyValue('--v-ripple-dur'))
      const duration = parsedDur ?? 300
      const easing = cs.getPropertyValue('--v-motion-ease').trim() || 'ease'

      /** 动画结束 ⇒ 移除**外层**（内层随它一起去）。这是"连按两次各有一份涟漪"的成对收尾。 */
      const removeBox = () => box.remove()

      const animation =
        typeof inner.animate === 'function'
          ? inner.animate(RIPPLE_KEYFRAMES, { duration, easing, fill: 'forwards' })
          : null
      if (animation === null) {
        // ⚠️ 没有 Web Animations API（jsdom 就是这一档：实测 `typeof el.animate === 'undefined'`）
        // ⇒ 造完**立刻移除**，不留残渣。代价如实记：那种环境下用户看不到涟漪（纯装饰降级），
        // 而不是"留下一个不动的色块"。测试因此要装一个可手动结束的 `Element.animate` 替身。
        removeBox()
        return
      }
      // `.finished` 在取消时是 reject ⇒ 两个分支都收，避免 unhandled rejection 与残渣。
      animation.finished.then(removeBox, removeBox)
    }

    document.addEventListener('pointerdown', onPointerDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      // ⚠️ 必须移除**自己建的那个** `layer`（闭包引用），不要去 `querySelector`
      // ——StrictMode 的"挂→卸→挂"里，第二次挂载时若按选择器找会删错/找不到。
      layer.remove()
    }
  }, [])

  return null
}
