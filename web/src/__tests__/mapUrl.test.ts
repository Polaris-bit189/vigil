import { describe, expect, it } from 'vitest'
import { mapSearchUrl } from '../mapUrl'

describe('mapSearchUrl', () => {
  it('高德：地点名进 keyword', () => {
    expect(mapSearchUrl('图书馆')).toBe(
      'https://uri.amap.com/search?keyword=' + encodeURIComponent('图书馆'),
    )
  })

  it('百度：地点名进 wd', () => {
    expect(mapSearchUrl('图书馆', 'baidu')).toBe(
      'https://map.baidu.com/search?querytype=s&wd=' + encodeURIComponent('图书馆'),
    )
  })

  // ⚠️ 这一条是**最重要**的一条：不编码的话 URL 会在空格处截断成
  // 「...keyword=实验楼」，搜到完全不同的地方，而用户不知道发生了什么。
  it('⚠️ 空格必须编码（否则被截断成另一个地点）', () => {
    const url = mapSearchUrl('实验楼 305')
    expect(url).not.toBeNull()
    expect(url).toContain('305')
    // 编码后不该有裸空格——`%20` 或 `+` 都可以，裸空格不行
    expect(url!.split('keyword=')[1]).not.toContain(' ')
    expect(url).toContain(encodeURIComponent('实验楼 305'))
  })

  it('中文也要编码', () => {
    const url = mapSearchUrl('食堂二楼')
    expect(url).toContain('%E9%A3%9F%E5%A0%82%E4%BA%8C%E6%A5%BC')
  })

  // ⚠️ 三个"空"的形态都要返回 null —— 调用方据此不渲染按钮。
  // 375 条里 255 条没有 place，渲染空按钮是噪声。
  it('空 / null / undefined / 全空白 ⇒ null（不渲染按钮）', () => {
    expect(mapSearchUrl('')).toBeNull()
    expect(mapSearchUrl(null)).toBeNull()
    expect(mapSearchUrl(undefined)).toBeNull()
    expect(mapSearchUrl('   ')).toBeNull()
  })

  // ⚠️ **刻意不判断"是不是地名"**：前端判断不了，硬做启发式必然误伤，
  // 而错的判据比没有判据更糟（把「图书馆」误判成非地名 ⇒ 按钮永远消失）。
  // 「老地方」照常给链接——用户点开搜不到，他自己知道那不是地名。
  it('非地名照常给链接（不自作聪明过滤）', () => {
    expect(mapSearchUrl('老地方')).not.toBeNull()
    expect(mapSearchUrl('回老家')).not.toBeNull()
  })

  it('两端空白被 trim（不留进 URL）', () => {
    expect(mapSearchUrl('  图书馆  ')).toBe(mapSearchUrl('图书馆'))
  })
})
