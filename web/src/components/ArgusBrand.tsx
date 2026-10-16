import wordmark from '../assets/argus-wordmark.png'
import ArgusLogo from './ArgusLogo.tsx'

/**
 * 左上角的品牌区：徽记 + 大号字标 + 小字。它**整体取代**「🏮 VIGIL」（只在 ARGUS 主题下）。
 *
 * ⭐ 字标是**美术资产**（`assets/argus-wordmark.png`：用户给的「argus」连笔字，
 * 已预处理成「笔画不透明、其余全透明」的 RGBA 图，尺寸 **430×152**、宽高比 **2.8289**；
 * 预处理脚本与原始数字见
 * `.superpowers/sdd/2026-09-22-vigil-m7-appearance/task-8-art-report.md` §P2）。
 *
 * ⚠️ **它是蒙版、不是图片**——这是用户那条要求的机械落点（原话：
 * 「注意它要能随主题色切换而变色，所以**这个图片不能直接用**」）：
 *   - 形状来自资产：`mask-image: url(那份 PNG)`；
 *   - 颜色来自主题色：`background-color: currentColor`（Tailwind 的 **`bg-current`**）
 *     ⇒ `/color` 一改，字标跟着变，**不需要任何同步代码**。
 * ⇒ ⛔ **绝对不许**把它塞进 `<img>` / 写成 `background-image`：那样形状与颜色一起来自
 * 位图，颜色就被**烤死在像素里**了（用户的反对意见正是这一条）。
 * ⚠️ 因此 `bg-current` 这个类名是**功能件不是样式偏好**，删掉它字标就变成一块纯色方块
 * （无蒙版形状）或干脆看不见（无背景色）——`argusBrand.dom.test.tsx` 有判据逐条钉着。
 * ⛔ 颜色那一半**只能是 `bg-current`**，**不许换成 `text-current`**：后者只设 `color`，
 * 而蒙版读的是 `background-color`（这个 div 里没有文字，`color` 等于没设）⇒ 字标**整块消失**。
 * 这个错误**看不出错**：T8-2 复审实测那样改**全量 141 条全绿**（F-B1）⇒ 判据因此收紧成只认 `bg-current`。
 *
 * ⚠️ 尺寸：`h-6 w-[67.9px]` 是**按资产的宽高比算出来的**（430×152 ⇒ 2.8289，
 * 24 × 2.8289 = 67.9），高度取 24px 是为了让字标的 x-height（= 0.566 × 24 ≈ 13.6px）
 * 与它取代掉的 `text-lg` 粗体「ARGUS」的大写字高（≈13px）**大致齐平**——
 * 换字号/换资产时必须**一起重算这个宽度**，否则蒙版会被 `contain` 缩放成两边的空白。
 * `shrink-0`：这是图形，不许被 flex 挤扁。
 *
 * ⚠️ **无障碍语义**：字标现在是**图形**，屏幕阅读器读不出笔画 ⇒
 * `role="img"` + `aria-label="ARGUS"` 是它能被读出来的**唯一**途径。
 * （旧版是 `<div>ARGUS</div>` 文本，`getByText('ARGUS')` 查得中；改成语义后按 role/label 查。）
 */
export default function ArgusBrand() {
  return (
    <div className="flex items-center gap-2">
      <ArgusLogo className="h-9 w-9 shrink-0" />
      <div className="leading-none">
        <div
          role="img"
          aria-label="ARGUS"
          className="h-6 w-[67.9px] shrink-0 bg-current"
          style={{
            maskImage: `url(${wordmark})`,
            WebkitMaskImage: `url(${wordmark})`,
            maskSize: 'contain',
            WebkitMaskSize: 'contain',
            maskRepeat: 'no-repeat',
            WebkitMaskRepeat: 'no-repeat',
            maskPosition: 'center',
            WebkitMaskPosition: 'center',
          }}
        />
        <div className="mt-1 text-[10px] tracking-[0.18em] text-vigil-fg-3">睡亦警觉，眼观六路</div>
      </div>
    </div>
  )
}
