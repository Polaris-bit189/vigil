// ⚠️ `defineConfig` 必须从 `vitest/config` 导入，**不是** `vite`：
// `vite` 的那一份不认 `test` 段——TS 上不报错、运行时被忽略，于是测试
// 悄悄跑在 node 环境里，第一句 `document is not defined` 才现形。
import { defineConfig } from 'vitest/config'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import { VitePWA } from 'vite-plugin-pwa'

export default defineConfig({
  plugins: [
    react(),
    tailwindcss(),
    VitePWA({
      // ⚠️ 必须是 `'prompt'`，**不能**是 `'autoUpdate'`——后者会把 `onNeedRefresh`
      // 编成**死支**（提示条在生产环境永不出现，且"立即刷新"按钮是空操作）。
      // 三层证据（读插件源码得出，并已用出厂产物复核）：
      //   ① 本处配置：插件 `dist/index.js:169` 把客户端模板里的占位符
      //      `__SW_AUTO_UPDATE__` 替换成 `${registerType === 'autoUpdate'}`，
      //      于是它变成字符串 `"true"`；
      //   ② 模板 `dist/client/build/register.js`：`var auto = autoUpdateMode === "true"`，
      //      而 `onNeedRefresh?.()` 的**唯一**调用点只出现在
      //      `if (auto) {…} else {…}` 的 **else 支**里；
      //   ③ 出厂产物 `dist/assets/index-*.js`：那段编译成
      //      ``ne=`true`,S=`false`,C=ne===`true` `` ⇒ `C` 恒真 ⇒ 走 auto 支，else 支是死代码。
      // 后果不止"不提示"：返回值 `updateServiceWorker` 是
      // `async(e=!0)=>{await l,C||u?.()}` ⇒ `C` 为真时它**连 `messageSkipWaiting`
      // 都不发**，点了白点；而真正生效的是 auto 支 `activated` 监听器里的
      // `window.location.reload()`——正是 `SwUpdateToast.tsx` 明文拒绝的
      // "在用户打字/勾选的中途把页面抽走"。
      //
      // ⚠️ 代价（如实记下）：`'prompt'` 下插件不再烘入**无条件**的
      // `skipWaiting`/`clientsClaim`（`dist/index.js:874-876` 那条 `if` 要求
      // `registerType === 'autoUpdate'`；实测产物 `dist/sw.js` 相应地从
      // `self.skipWaiting(),e.clientsClaim()` 变成只剩一条
      // `"SKIP_WAITING"===e.data.type&&self.skipWaiting()` 的消息监听）
      // ⇒ 新 SW 会停在 waiting，版本更新**必须用户点一次**「立即刷新」才接管。
      // 这正是 spec §4.3④ 想要的"不静默跑旧 bundle"，但它与"部署后自动生效"
      // 是**互斥**的两件事：要提示条，就得接受这一下点击。
      // ⛔ 因此**不要**顺手补 `workbox.skipWaiting`/`clientsClaim`：那等于把 prompt
      // 流程又变回静默自动更新，提示条会重新变成摆设。
      registerType: 'prompt',
      includeAssets: ['icon-192.png', 'icon-512.png'],
      manifest: {
        name: 'VIGIL 守夜人',
        short_name: 'VIGIL',
        description: 'QQ 群信息聚合：通知、学业、活动、生活一页看完',
        lang: 'zh-CN',
        start_url: '/',
        scope: '/',
        display: 'standalone',
        background_color: '#0b0f14',
        theme_color: '#0b0f14',
        icons: [
          { src: 'icon-192.png', sizes: '192x192', type: 'image/png' },
          { src: 'icon-512.png', sizes: '512x512', type: 'image/png' },
          {
            src: 'icon-maskable-512.png',
            sizes: '512x512',
            type: 'image/png',
            purpose: 'maskable',
          },
        ],
      },
      workbox: {
        navigateFallback: '/index.html',
        // ⚠️ /api/ 的导航请求不许被兜底成 index.html——那会把一个接口
        // 404 变成一份看起来正常的 HTML，调用方解析失败却看不到原因。
        navigateFallbackDenylist: [/^\/api\//],
      },
    }),
  ],
  server: {
    // 开发期前端跑 5173、后端跑 8787，靠代理合到一个源上（免 CORS）
    proxy: { '/api': 'http://127.0.0.1:8787' },
  },

  // ⚠️ `environment: 'jsdom'` 是**全局**的（不是只给 DOM 用例）。
  // Vitest 3+ 已把 `environmentMatchGlobs` 标记为弃用、改用 projects/workspace，
  // 而本仓的 vitest 是 5.0.1 —— 与其赌一个具体 API，不如全局开 jsdom：
  // 既有的 `api.test.ts` / `format.test.ts` 是纯逻辑，在 jsdom 下照样跑，
  // 代价是每次多初始化一个 DOM，对本仓 22 条用例完全可忽略。
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test-setup.ts'],
  },
})
