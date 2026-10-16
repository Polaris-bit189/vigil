// `place` 文本 → 地图搜索链接。
//
// ⚠️ **只做「搜索链接」，不做导航**。校园内部建筑在公开地图上没有独立
// 点位（「实验楼 305」「食堂二楼」搜出来只会是学校大门），所以按钮文案
// 相应是「在地图中查看」——叫「导航」是过度承诺，用户点了发现到的是
// 大学门口，会觉得是 bug。完整论证见 `docs/design/地点导航.md` §二。
//
// ⚠️ 实测覆盖率只有 **32%**（120/375 条有 place），其中真正可导航的
// **29.6%**（111/375，扣掉「老地方」「回老家」这类不是地名的）。
// 这两个数都要如实写进材料：只说「32% 有地点」会被理解成「都能导航」。

export type MapProvider = 'amap' | 'baidu'

/**
 * `place` 文本 → 地图搜索 URL。**空/全空白返回 `null`**（调用方据此不渲染按钮）。
 *
 * ⚠️ 必须 `encodeURIComponent`：地点名里有空格（「实验楼 305」），
 * 直接拼进 URL 会被**截断成「实验楼」**——搜到完全不同的地方，
 * 而用户不知道发生了什么。这是本函数最容易漏、后果最隐蔽的一处。
 *
 * ⚠️ **不判断「是不是地名」**。前端判断不了，硬做启发式（长度、黑名单）
 * 必然误伤；而错的判据比没有判据更糟：把「图书馆」误判成非地名，
 * 用户就永远看不到那个按钮了。用户点「老地方」搜不到，他自己知道那不是地名。
 */
export function mapSearchUrl(
  place: string | null | undefined,
  provider: MapProvider = 'amap',
): string | null {
  const q = (place ?? '').trim()
  if (q === '') return null
  const enc = encodeURIComponent(q)
  return provider === 'baidu'
    ? `https://map.baidu.com/search?querytype=s&wd=${enc}`
    : `https://uri.amap.com/search?keyword=${enc}`
}

/** 按钮文案。**刻意不是「导航」**——见本文件顶部。 */
export const MAP_BUTTON_LABEL = '在地图中查看'
