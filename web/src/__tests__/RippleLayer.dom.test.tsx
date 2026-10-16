import type { CSSProperties } from 'react'
import { StrictMode } from 'react'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, test } from 'vitest'
import RippleLayer from '../components/RippleLayer.tsx'

/**
 * ⭐ 这组用例守的是"动效的边界 + 它与 React 的写入不打架"，每一条都**必须能红**：
 *   ① 该有涟漪时**有**——形态是"图层里出现了 `.vigil-ripple-box`（按按钮几何摆好）+
 *      内层 `.vigil-ripple` 的落点/直径正确 + 动画按主题 token 提交给 WAAPI"；
 *   ② 该没事时**一个都没有**（主题不要 / 系统关了动效 / 右键 / disabled）；
 *   ③ ⭐⭐ **核心守卫（N-1 回归）**：按钮的 `className` 或**文案**变化之后，涟漪盒**仍在**；
 *   ④ ⭐ **连按两次**：每次都有自己的一份盒子，且各自在动画结束后消失；
 *   ⑤ 图层本身的生命周期（StrictMode 双挂载 ⇒ 只有一份；卸载 ⇒ 收干净）。
 *
 * ⚠️ **断言对象换了两次，每次都是被实测逼的**（三版机制对照见报告）：
 *   v1 断言"按钮里插了 `.vigil-ripple` span" ⇒ 被 React 的 `setTextContent` 抹掉；
 *   v2 断言"按钮上有 `vigil-rippling` 类"      ⇒ 被 React 的 `setAttribute('class', …)` 抹掉
 *      （**点击就改 className 的 chip / tab 上必现**）；
 *   v3（本版）断言"**图层里的盒子**"——图层挂在 `document.body`，React 既不建也不 diff body
 *      的子节点 ⇒ 前两条写入路径结构上够不着它。
 *
 * ⚠️⭐ **如实记一条方法论**（re-review 对上一轮的实测，就是本轮 F3 要改的那条不实注释）：
 * "把这些用例拿去跑旧实现"**证明不了机制**——旧实现放回后，红的是**前置行**
 * （`expect(rippleBoxes()).toHaveLength(1)`：旧实现压根不造盒子），**不是**守卫行
 * （"切了 className 之后盒子还在不在"）。那是一条**口径差**（断言形状与实现不匹配），
 * 不是"载体被 React 抹掉"的证据。⛔ 上一轮在这里写的"（它）不是口径差"是**错的**，本轮照实改。
 * ⭐ "载体被抹掉"这件事只能靠**探针**坐实：把 className / 文案一变，计数 1 → 0
 *（v1 的 `setTextContent` 复现与本轮 N-1 的 `setAttribute('class', …)` 复现都是这个形态，
 * 原始输出在 `task-7-fix-report.md` §1 与 `task-7-fix2-report.md` 第 0 步）。
 * ⇒ 所以下面 N-1 那条里"**还是同一个盒子**"（`toBe(box)`）格外重要：它问的是
 * "原来那个还在不在"，而不是"有没有别的东西顶着"。
 *
 * ⚠️ **为什么每条断言"没有涟漪"的用例也装了 `animate` 替身**（v2 删掉替身是对的，v3 又必须装回来，
 * 这两件事不矛盾）：v3 在**没有 WAAPI** 的环境里是"造完立刻移除"（jsdom 就是这一档：
 * 实测 `typeof document.createElement('div').animate === 'undefined'`）⇒ 不装替身时，
 * "该没有"（0 个盒子）与"该有但立刻被收走"（也是 0 个盒子）**长得一模一样**——
 * 那正是本仓最怕的**假绿**：把 `--v-ripple` 那条判断删掉，边界用例会**照样全绿**。
 * ⇒ 替身的作用是让"造出来的盒子**留在**那里"，边界用例才真的能红（报告 §3 变异 ①/② 的原始输出）。
 *
 * ⭐ **N-1 是本轮存在的理由**（scoped re-review 报的真缺陷，**有活实例**）：
 * `views/Feed.tsx:416` 的 chip 与 `views/Settings.tsx:33` 的 tab **点击时就会改 `className`**
 * （选中态换底色），而 React 写 `class` 走 `setValueForKnownAttribute`
 * （`react-dom-client.development.js:1698` 的 `setAttribute('class', …)`）
 * ⇒ v2 那个挂在这些按钮上的 `vigil-rippling` 类**在点下去的那一瞬间就被清掉**。
 * 第 0 步复现的原始输出（`class="bg-b text-y vigil-rippling"` → 切 `active` →
 * `class="bg-a text-x"`）见 `task-7-fix2-report.md`。⭐ 那条回归用例守的是
 * "**载体不在 React 管的节点上**"这个**结构性**事实——不是某一次实现细节。
 *
 * ⚠️ v2 的两条用例**随机制一起删掉了**（不是把断言改松）：
 *   · `animationend` 委托那条：v3 不再有"动画结束事件冒泡到 document"这回事——动画由
 *     WAAPI 的 `animation.finished` 逐节点收尾（成对地"谁造谁收"）；
 *   · `offsetWidth` 强制重排那条：v3 每次按下都**新建**盒子与动画，天生可重播
 *     ⇒"摘类→重排→挂类"这个手势不存在了。它守的"连按第二下要有反馈"由 ④ 接手。
 *
 * ⚠️ jsdom 不加载 CSS ⇒ `getComputedStyle(el).getPropertyValue('--v-ripple')` 是空串。
 * 所以"该有涟漪"那条要在按钮上**内联**写这些自定义属性（实测：jsdom 能读到内联自定义属性，
 * `--v-ripple-dur` / `--v-motion-ease` 同理，值**原样保留**——包括带空格的 cubic-bezier）。
 */

const BOX = 'vigil-ripple-box'
const LAYER = 'vigil-ripple-layer'

/** 故意用 vaporwave 那条**带过冲**的曲线：它证明曲线是从主题 token 读出来交给 WAAPI 的，
 *  而不是在 JS 里硬编码一个 `ease`（硬编码会把那套主题的性格抹平）。 */
const EASE = 'cubic-bezier(0.34, 1.2, 0.64, 1)'

/**
 * ⭐ I1（delta 终审 Important）：这份内联的 `--v-ripple-dur` **就是默认主题的值**。
 *
 * ⚠️ 它此前是 `360ms`——那是 **material 的**值（`STRUCTURAL.material`），而默认主题是 **300ms**
 * （`index.css` 的默认块 = 2 × `--v-motion-dur` 150ms），于是"默认主题按下时 WAAPI 拿到什么"
 * 这件事**取决于哪个文件先写**：两边各自"对"，却**没有一个所有者**。
 * ⇒ controller 裁决：**以 `index.css` 的默认主题值为唯一所有者**，这里逐字对齐它。
 * ⛔ 改这个数要先改 `index.css`（顺带 `themeTokens.test.ts` 那条 `v('default','--v-ripple-dur')`
 * 会一起红——那正是"所有者只有一个"的意思）。
 * ⚠️ 这里**不是**默认主题的完整语境：曲线用的是 vaporwave 那条（见上），这套夹具是
 * "把 token 原样读给 WAAPI"的最小组合，**不声称**它等于任何一套真主题。
 */
const DEFAULT_RIPPLE_DUR = '300ms'

/** jsdom 不排版 ⇒ 默认 `getBoundingClientRect()` 全是 0，几何量全塌成 `0px`。
 *  给**这个元素实例**钉一个固定几何（不改 `Element.prototype`，因此**不需要还原**：
 *  元素随 unmount 一起消失），几何量才能逐字断言。 */
function stubRect(el: Element) {
  el.getBoundingClientRect = () =>
    ({
      x: 100,
      y: 200,
      left: 100,
      top: 200,
      width: 80,
      height: 40,
      right: 180,
      bottom: 240,
      toJSON: () => ({}),
    }) as DOMRect
}

function rippleBtn(props: { ripple?: string; disabled?: boolean; radius?: string } = {}) {
  return (
    <button
      disabled={props.disabled}
      style={
        {
          ...(props.ripple === undefined ? {} : { '--v-ripple': props.ripple }),
          borderRadius: props.radius ?? '12px',
          '--v-ripple-dur': DEFAULT_RIPPLE_DUR,
          '--v-motion-ease': EASE,
        } as CSSProperties
      }
    >
      按我
    </button>
  )
}

const rippleBoxes = () => document.querySelectorAll(`.${BOX}`)
const layers = () => document.querySelectorAll(`.${LAYER}`)

/** 按一下（左键），落点固定为 (150, 220)——相对 stubRect 那个按钮就是 (50, 20)。 */
async function press(el: Element) {
  await userEvent.pointer({ keys: '[MouseLeft>]', target: el, coords: { clientX: 150, clientY: 220 } })
}

type AnimateCall = { keyframes: Keyframe[]; options: KeyframeAnimationOptions }

/**
 * ⭐ `Element.animate` 替身（jsdom 没有 WAAPI）。
 * 它**必须可手动"播完"**：v3 的"动画结束后移除盒子"是靠 `animation.finished` 落定来做的，
 * 而这个 promise 在 jsdom 里永远不会自己落定（没有合成器）⇒ 不装替身就**测不出收尾**，
 * 也就测不出本轮要守的"连按两次各有一份、各自收走"。
 */
function stubAnimate() {
  const calls: AnimateCall[] = []
  const pending: Array<() => void> = []
  const had = Object.prototype.hasOwnProperty.call(Element.prototype, 'animate')
  const original = Element.prototype.animate

  Element.prototype.animate = function (
    keyframes: Keyframe[] | PropertyIndexedKeyframes | null,
    options?: number | KeyframeAnimationOptions,
  ) {
    calls.push({ keyframes: keyframes as Keyframe[], options: (options ?? {}) as KeyframeAnimationOptions })
    return {
      finished: new Promise<void>((resolve) => pending.push(resolve)),
    } as unknown as Animation
  } as Element['animate']

  return {
    calls,
    /** 让**所有在飞的**动画播完（= 真浏览器里 `finished` 落定那一刻），并冲掉微任务。 */
    async finishAll() {
      for (const resolve of pending.splice(0)) resolve()
      await Promise.resolve()
    },
    restore() {
      if (had) Element.prototype.animate = original
      else delete (Element.prototype as { animate?: unknown }).animate
    },
  }
}

let stub: ReturnType<typeof stubAnimate> | null = null

afterEach(() => {
  stub?.restore()
  stub = null
  // ⚠️ 上面那条 `matchMedia` 用例是**显式存原值 + 显式还原**的（jsdom 没这个函数，
  // `vi.spyOn` 会抛 `can only spy on a function`）——它自己 try/finally 收，这里不管。
})

test('① 有涟漪色的主题：图层里出现涟漪盒，几何/圆角/动画参数都照按钮与主题来', async () => {
  stub = stubAnimate()
  render(
    <>
      <RippleLayer />
      {rippleBtn({ ripple: 'rgba(0,0,0,.2)' })}
    </>,
  )
  const btn = screen.getByRole('button', { name: '按我' })
  stubRect(btn)
  await press(btn)

  expect(rippleBoxes(), '按下后图层里没有涟漪盒（等于没做）').toHaveLength(1)
  const box = rippleBoxes()[0] as HTMLElement
  // ⚠️ 盒子用的是**文档坐标**（`rect.left + scrollX`）：jsdom 里 scrollX/Y = 0，
  // 所以这里与 rect 同值；真浏览器里滚动后两者才会分开（普通内容的涟漪因此跟着内容走）。
  expect(box.style.left, 'rect.left 100 + scrollX 0').toBe('100px')
  expect(box.style.top, 'rect.top 200 + scrollY 0').toBe('200px')
  expect(box.style.width).toBe('80px')
  expect(box.style.height).toBe('40px')
  // 圆角是**量**按钮的（本仓圆角是主题资产：方角 vs 药丸都有）⇒ 涟漪被裁成同一个轮廓。
  expect(box.style.borderRadius, '没照按钮的圆角裁 ⇒ 药丸按钮上涟漪会撑破圆角').toBe('12px')

  const inner = box.querySelector<HTMLElement>('.vigil-ripple')
  expect(inner, '盒子里没有内层 .vigil-ripple').not.toBeNull()
  // ⚠️ 落点 = 触点相对按钮左上角（clientX 150 − rect.left 100）；直径 = max(80,40)*2 = 160。
  // 两个负 margin 把方块回正到"以落点为中心"——写错这一处，涟漪会整体偏半个直径
  //（肉眼看得见，测试不写就看不见）。
  expect(inner!.style.left).toBe('50px')
  expect(inner!.style.top).toBe('20px')
  expect(inner!.style.width).toBe('160px')
  expect(inner!.style.height).toBe('160px')
  expect(inner!.style.marginLeft).toBe('-80px')
  expect(inner!.style.marginTop).toBe('-80px')

  // ⭐ 动画参数来自**主题 token**：时长取 `--v-ripple-dur`（比状态过渡慢一档），
  // 曲线取 `--v-motion-ease`（vaporwave 那条带过冲）。硬编码任一个都会丢掉主题性格。
  // ⚠️ 这个数（300ms）属于**默认主题**，所有者是 `index.css`（见 `DEFAULT_RIPPLE_DUR`）。
  expect(stub.calls, '没把动画交给 WAAPI').toHaveLength(1)
  expect(stub.calls[0].options.duration, `--v-ripple-dur: ${DEFAULT_RIPPLE_DUR}`).toBe(300)
  expect(stub.calls[0].options.easing).toBe(EASE)
  expect(stub.calls[0].keyframes, '关键帧必须与 index.css 里那份 @keyframes 同值').toEqual([
    { transform: 'scale(0)', opacity: 0.9 },
    { transform: 'scale(1)', opacity: 0 },
  ])
})

test('② 这套主题不要涟漪（--v-ripple 缺失 ⇒ 读到空串）⇒ 一个都不造', async () => {
  // ⚠️ 替身必须在场：不在场时"造了又立刻收走"也是 0 个盒子 ⇒ 这条会变成假绿（见文件顶部）。
  stub = stubAnimate()
  render(
    <>
      <RippleLayer />
      {rippleBtn()}
    </>,
  )
  await press(screen.getByRole('button', { name: '按我' }))
  expect(rippleBoxes()).toHaveLength(0)
})

test('② 显式 transparent ⇒ 也不造（"关掉"要有两种写法都行）', async () => {
  stub = stubAnimate()
  render(
    <>
      <RippleLayer />
      {rippleBtn({ ripple: 'transparent' })}
    </>,
  )
  await press(screen.getByRole('button', { name: '按我' }))
  expect(rippleBoxes()).toHaveLength(0)
})

test('② 系统里关了动效（prefers-reduced-motion: reduce）⇒ 不造', async () => {
  stub = stubAnimate()
  // ⚠️ 本机 jsdom（30.1.0）整个 lib 里没有一处 `matchMedia`（实测：
  // `typeof window.matchMedia === 'undefined'`，且 `grep -r matchMedia node_modules/jsdom/lib/` 为空）
  // ⇒ `vi.spyOn(window,'matchMedia')` 抛 `can only spy on a function`。
  // 处置沿用"显式存原值 + 显式还原"（没有原值时删回去）。
  const original = window.matchMedia
  window.matchMedia = ((query: string) => ({
    matches: true,
    media: query,
    onchange: null,
    addListener() {},
    removeListener() {},
    addEventListener() {},
    removeEventListener() {},
    dispatchEvent: () => false,
  })) as unknown as typeof window.matchMedia
  try {
    render(
      <>
        <RippleLayer />
        {rippleBtn({ ripple: 'rgba(0,0,0,.2)' })}
      </>,
    )
    await press(screen.getByRole('button', { name: '按我' }))
    expect(rippleBoxes(), 'prefers-reduced-motion 是无障碍，不是可选装饰').toHaveLength(0)
  } finally {
    if (original === undefined) delete (window as { matchMedia?: unknown }).matchMedia
    else window.matchMedia = original
  }
})

test('② disabled 的按钮 ⇒ 不造（按不动的控件不该有反馈）', async () => {
  stub = stubAnimate()
  render(
    <>
      <RippleLayer />
      {rippleBtn({ ripple: 'rgba(0,0,0,.2)', disabled: true })}
    </>,
  )
  await press(screen.getByRole('button', { name: '按我' }))
  expect(rippleBoxes()).toHaveLength(0)
})

test('② 右键 ⇒ 不造（涟漪是"主键按下"的反馈）', async () => {
  stub = stubAnimate()
  render(
    <>
      <RippleLayer />
      {rippleBtn({ ripple: 'rgba(0,0,0,.2)' })}
    </>,
  )
  const btn = screen.getByRole('button', { name: '按我' })
  stubRect(btn)
  await userEvent.pointer({ keys: '[MouseRight>]', target: btn })
  expect(rippleBoxes()).toHaveLength(0)
})

/**
 * ⭐⭐ **本轮存在的理由**（scoped re-review 的 N-1，有活实例——`Feed.tsx:416` 的 chip /
 * `Settings.tsx:33` 的 tab：**点击时就会改 className**）。
 *
 * 它守的是一条**结构性**事实：**涟漪的载体不在 React 管的节点上**。
 *   · 换 `className` ⇒ React 写 `setAttribute('class', …)`（`setValueForKnownAttribute`）；
 *   · 换文案     ⇒ React 写 `node.textContent = text`（`setTextContent` 的慢路径）。
 * v1（按钮里的 span）死在后者，v2（按钮上的类）死在前者——本用例把两条写入路径都走一遍，
 * 并且逐条钉住"**还是同一个盒子**"（不是"又造了一个"，那会掩盖"旧的被抹掉"）。
 */
test('⭐⭐ N-1 回归：按钮的 className / 文案变了之后，涟漪盒仍在（且是同一个）', async () => {
  stub = stubAnimate()
  function Subject({ active, text }: { active: boolean; text: string }) {
    return (
      <button
        className={active ? 'bg-vigil-accent-soft' : 'bg-vigil-surface'}
        style={{ '--v-ripple': 'rgba(0,0,0,.2)', borderRadius: '9999px' } as CSSProperties}
      >
        {text}
      </button>
    )
  }
  const { container, rerender } = render(
    <>
      <RippleLayer />
      <Subject active={false} text="按我" />
    </>,
  )
  const btn = screen.getByRole('button', { name: '按我' })
  stubRect(btn)
  await press(btn)
  expect(rippleBoxes(), '前置没上场：按下后没有涟漪盒').toHaveLength(1)
  const box = rippleBoxes()[0]
  // ⭐ 结构性断言：盒子**不在 React 渲染的容器里**（它是 `document.body` 那层的孩子）
  //——这条比"盒子还在"更接近要守的事实本身。
  expect(container.contains(box), '盒子跑进 React 管的树里了 ⇒ 迟早被 React 的写入抹掉').toBe(false)

  rerender(
    <>
      <RippleLayer />
      <Subject active text="按我" />
    </>,
  )
  expect(btn.className, 'className 没换成 ⇒ 这条没测到 setAttribute(class)').toBe('bg-vigil-accent-soft')
  expect(rippleBoxes(), 'React 写 className 时把涟漪一起清掉了（N-1）').toHaveLength(1)
  expect(rippleBoxes()[0], '旧盒子被抹掉、又补了一个新的（同样是坏了，只是看不出来）').toBe(box)

  rerender(
    <>
      <RippleLayer />
      <Subject active text="换个文案" />
    </>,
  )
  expect(btn.textContent, '文案没换成 ⇒ 这条没测到 textContent 覆写').toBe('换个文案')
  expect(rippleBoxes(), 'React 覆写 textContent 时把涟漪一起清掉了').toHaveLength(1)
  expect(rippleBoxes()[0]).toBe(box)
})

/**
 * ⭐ **核心守卫：连按两次**。
 *
 * ⚠️ **为什么"只按一次"的用例挡不住**（上一轮 re-review 实测）：把"动画结束后移除/摘类"
 * 那一步**整个删掉**，v2 的 9 条用例**全绿**——因为每条用例都只按一次、也都只看"按完之后在不在"，
 * 没有任何一条问过"它**后来**该不该消失"。⇒ 收尾那一步是"测不到的半条机制"。
 * 本用例问三件事：第二下**有自己的**一份（不是复用第一份）、第一份**到点自己消失**、
 * 第二下**照样能被收走**（说明收尾不是"只对第一条注册"）。
 *
 * ⚠️ 这条**必须**用手动能播完的替身：jsdom 里 `finished` 永远不会自己落定，
 * 不装替身就永远走不到"移除"那一步（报告 §3 变异 ③ 的原始输出）。
 */
test('⭐ 连按两次：每次都有自己的一份盒子，且各自在动画结束后消失', async () => {
  stub = stubAnimate()
  render(
    <>
      <RippleLayer />
      {rippleBtn({ ripple: 'rgba(0,0,0,.2)' })}
    </>,
  )
  const btn = screen.getByRole('button', { name: '按我' })
  stubRect(btn)

  await press(btn)
  const first = rippleBoxes()[0] as HTMLElement
  expect(rippleBoxes()).toHaveLength(1)

  // 第二下（第一下的动画**还没**结束）：两份并存，且第二份是新造的那个节点。
  await press(btn)
  expect(rippleBoxes(), '第二下没有造出自己那份（"第二下没反馈"）。').toHaveLength(2)
  const second = rippleBoxes()[1] as HTMLElement
  expect(second, '第二下复用了第一份盒子').not.toBe(first)

  // 动画播完 ⇒ 两份都收走：第一份证明"到点自己消失"，第二份证明"收尾不是只对第一条注册"。
  await stub.finishAll()
  expect(rippleBoxes(), '动画结束后没把盒子移除（旧用例挡不住的就是这一步）').toHaveLength(0)

  // 再按一下：仍然是"一次一份"，不是"攒着"也不是"再也不造"。
  await press(btn)
  expect(rippleBoxes()).toHaveLength(1)
  expect(rippleBoxes()[0]).not.toBe(first)
  await stub.finishAll()
  expect(rippleBoxes()).toHaveLength(0)
})

/**
 * ⚠️ 没有 WAAPI 的环境：**造完立刻移除**（不留残渣）。
 *
 * 这条与 ① 是一对：**装替身**时"有涟漪"看得见（1 个盒子），**不装替身**时是 0 个——
 * 两者一起才证明"0 个盒子"确实是"收走了"而不是"根本没造"（假绿的两种形态在这里恰相反）。
 * 代价如实记：那种环境下用户看不到涟漪（纯装饰降级），而不是"留下一个不动的色块"。
 */
test('⭐ 没有 Web Animations API（jsdom 原本就是这一档）⇒ 造完立刻移除，不留残渣', async () => {
  render(
    <>
      <RippleLayer />
      {rippleBtn({ ripple: 'rgba(0,0,0,.2)' })}
    </>,
  )
  const btn = screen.getByRole('button', { name: '按我' })
  stubRect(btn)
  await press(btn)
  expect(rippleBoxes(), '没有 WAAPI 还留下盒子 ⇒ 屏幕上会是一个不动的色块').toHaveLength(0)
  // 图层本身照旧在（机制没关，只是这一下没有动画可播）。
  expect(layers()).toHaveLength(1)
})

/**
 * ⑤ 图层的生命周期。
 *
 * ⚠️ 这两条**不是空守卫**（v2 那条"卸载后不再监听"本来是——`document.body` 里没有按钮，
 * `closest('button')` 恒为 null ⇒ 卸不卸监听器都绿）。图层是**可观察的节点**：
 *   · StrictMode 双挂载（`main.tsx` 就是这么挂的：挂→卸→挂）⇒ 留两份 = cleanup 漏了；
 *   · 卸载之后 ⇒ 一个都不该剩（否则每换一次页面就多一层，涟漪还会叠着画）。
 */
test('⑤ StrictMode 双挂载 ⇒ 只剩一份图层；卸载 ⇒ 收干净', () => {
  const { unmount } = render(
    <StrictMode>
      <RippleLayer />
    </StrictMode>,
  )
  expect(layers(), 'StrictMode 的"挂→卸→挂"后留下了两份图层（cleanup 没成对）').toHaveLength(1)
  unmount()
  expect(layers(), '卸载后图层没被移除').toHaveLength(0)
})
