import { expect, test } from 'vitest'

/**
 * ⚠️ Node 内置模块在本仓**必须**用「具名变量说明符」动态 import——两条实测出来的墙
 * （理由与替代方案的对照写在 `noRawPalette.test.ts` 顶部那段；M7 勘误 E9）：
 *   ① 本仓没有 `@types/node`，而 `tsc -b`（`npm run build` 会跑）**会编译测试文件**
 *      ⇒ `import { readFileSync } from 'node:fs'` 直接 TS2307 让构建失败；
 *   ② `new URL('…', import.meta.url)` 第二个参数写成**字面量**时，Vite 的
 *      asset-import-meta-url 变换会接管，基准被换成 jsdom 的 document base
 *      ⇒ 先取 `const HERE = import.meta.url` 再传就正常。
 */
const NODE_FS = 'node:' + 'fs'
const HERE = import.meta.url
const { readFileSync } = (await import(/* @vite-ignore */ NODE_FS)) as {
  readFileSync: (p: URL, enc: string) => string
}

/**
 * ⭐ F3（终审 fix 轮）——`registerType: 'prompt'` 的**唯一机械探测器**。
 *
 * ⛔ **这不是风格的钉子**，它守的是一个**真缺陷的回滚**：改回 `'autoUpdate'` 会把
 * `onNeedRefresh` 编成**死支** ⇒ `SwUpdateToast` 的提示条在生产环境**永不出现**、
 * "立即刷新"是空操作，而真正生效的是 auto 支里的 `window.location.reload()`
 * ——正是那个组件明文拒绝的"在用户打字/勾选的中途把页面抽走"。
 * 三层证据（插件源码 → 客户端模板 → 出厂产物）写在 `vite.config.ts` 自己那段注释里，
 * 不在本文件重复。
 *
 * ⛔ 为什么必须是**静态**钉子：终审实测，把配置改回 `'autoUpdate'` ⇒ **92 条全绿**
 * （E4 把这条接线挡在测试外：`swUpdate.test.tsx` 测的是 `SwUpdateToast` 这个组件本身，
 * 它**不经过** PWA 插件生成的注册代码）。真正的端到端证据（提示条真的出现、点一下
 * 真的换 SW）在**冒烟**里；这条只是那份冒烟之外的机械兜底——它证明"配置没被改回去"，
 * **证明不了**"提示条跑得起来"。
 *
 * ⚠️ 已知边界：这是**文本**匹配，不是"读配置对象"。它挡的是"有人把这一行改掉/删掉"
 * 这个具体动作；把 prompt 流程换成别的写法（例如显式 `workbox.skipWaiting`）不在这里的
 * 覆盖面内，那属于冒烟与 review 的事。
 */
const CONFIG = readFileSync(new URL('../../vite.config.ts', HERE), 'utf8')

test("vite.config.ts 的 registerType 必须是 'prompt'（'autoUpdate' 会把 onNeedRefresh 编成死支）", () => {
  // ⚠️ 正则要求 `registerType` 后面**直接**跟冒号：本文件里另有三处
  // `registerType === 'autoUpdate'`（都在证据注释里），中间隔着 `===`
  // ⇒ 不会被这条匹配到，命中的是**配置那一行**。
  const m = CONFIG.match(/registerType\s*:\s*(['"])([A-Za-z]+)\1/)
  expect(
    m?.[2],
    "vite.config.ts 里没有 registerType，或者它不是 'prompt'——'autoUpdate' 会让提示条永不出现",
  ).toBe('prompt')
})
