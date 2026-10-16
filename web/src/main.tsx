import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import { registerSW } from 'virtual:pwa-register'
import App from './App.tsx'
import RippleLayer from './components/RippleLayer.tsx'
import SwUpdateToast from './components/SwUpdateToast.tsx'
import { markNeedRefresh } from './swUpdate.ts'
import { applyTheme, readTheme } from './theme.ts'
import './index.css'

applyTheme(readTheme())

// ⚠️ 全仓**唯一**的 virtual:pwa-register 引用点（E4：vitest 解析不了它，
// 所以它必须待在测试不 import 的文件里；能测的部分全在 swUpdate.ts）。
//
// 副作用（实测，插件 dist/index.js:254）：一旦这里出现 import，
// `injectRegister: 'auto'` 就不再往 index.html 注入 `registerSW.js`
// ⇒ **不会有第二次注册**。构建后 dist/index.html 里那个
// `<script id="vite-plugin-pwa:register-sw">` 应当消失——这是本任务的一条核对项。
const updateSW = registerSW({
  onNeedRefresh() {
    markNeedRefresh(() => {
      void updateSW(true) // true = 让新 SW 立刻接管并重新加载
    })
  },
})

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <App />
    <SwUpdateToast />
    {/* T7 动效层：一个挂在 document 上的委托监听器，自己渲染 null。
        ⚠️ `StrictMode` 下它的 effect 会跑两遍（挂→卸→挂）——那是**正常**的，
        cleanup 会成对摘掉/重装监听器，不会留下双监听器（见该文件顶部注释）。 */}
    <RippleLayer />
  </StrictMode>,
)
