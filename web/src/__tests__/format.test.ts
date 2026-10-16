import { describe, expect, it } from 'vitest'
import { fmtDate, fmtDateTime, relDeadline, relTime } from '../format'

describe('fmtDate / fmtDateTime', () => {
  it('按本地时区渲染，不是 UTC', () => {
    // 本地 2026-09-13 12:00 —— 用 UTC 解释会偏 8 小时到 9月13日 04:00 或 9月12日
    const ts = Math.floor(new Date(2026, 8, 13, 12, 0).getTime() / 1000)
    expect(fmtDate(ts)).toBe('9月13日')
    expect(fmtDateTime(ts)).toBe('9月13日 12:00')
  })
})

describe('relTime', () => {
  const now = Math.floor(Date.now() / 1000)

  it('一小时内按分钟', () => {
    expect(relTime(now - 120)).toBe('2 分钟前')
  })
  it('一天内按小时', () => {
    expect(relTime(now - 7200)).toBe('2 小时前')
  })
  it('一周内按天', () => {
    expect(relTime(now - 86400 * 3)).toBe('3 天前')
  })
  it('超过一周回落到日期', () => {
    expect(relTime(now - 86400 * 30)).toMatch(/月.*日/)
  })
  it('未来时间不编「-3 分钟前」', () => {
    // 库里存在 1970 脏数据与未来戳；负数分钟会被读成「-1 分钟前」
    expect(relTime(now + 600)).toMatch(/月.*日/)
  })
  it('刚发出不说「0 分钟前」', () => {
    expect(relTime(now - 10)).toBe('1 分钟前')
  })
})

// ⚠️ **这组用例与后端 `tests/test_deadline.py` 的「死线倒数分级」逐条对应。**
// 两边各写一份是知情取舍（日报落盘要后端算、日历要跟当前时刻要前端算），
// 代价是阈值可能漂移 —— 而漂移的症状是「日历上标红了、日报里却没提」，
// 单看任何一边都正常。所以**边界值必须两边都钉**（0 / 1 / 2 / 4 / 7 / 8 天）。
describe('relDeadline', () => {
  // 固定的「现在」：2026-09-16 10:00 本地。用例因此**不依赖当前时刻**
  // （否则今天绿明天红）。
  const now = Math.floor(new Date(2026, 8, 16, 10, 0).getTime() / 1000)
  const at = (y: number, mo: number, d: number, h = 12) =>
    Math.floor(new Date(y, mo, d, h, 0).getTime() / 1000)

  it('已过期：负数天数照实报', () => {
    expect(relDeadline(at(2026, 8, 15), now)).toEqual({
      text: '已过期 1 天',
      level: 'expired',
    })
  })

  it('⭐ 边界 0 天与 1 天都是 urgent', () => {
    expect(relDeadline(at(2026, 8, 16), now)).toEqual({
      text: '今天截止',
      level: 'urgent',
    })
    expect(relDeadline(at(2026, 8, 17), now)).toEqual({
      text: '明天截止',
      level: 'urgent',
    })
  })

  it('⚠️ 按本地日对齐，不是按秒差', () => {
    // 现在 09-16 10:00，死线 09-17 09:00（差 23 小时）。
    // 按秒差取整会得到 0 天 ⇒「今天截止」——差了一天。
    expect(relDeadline(at(2026, 8, 17, 9), now).text).toBe('明天截止')
  })

  it('2 天起离开 urgent', () => {
    expect(relDeadline(at(2026, 8, 18), now).level).toBe('soon')
  })

  it('4 天起进入 normal', () => {
    expect(relDeadline(at(2026, 8, 20), now).level).toBe('normal')
  })

  it('⭐ 边界 7 天仍是相对文案，8 天起回落绝对日期', () => {
    expect(relDeadline(at(2026, 8, 23), now).text).toBe('还有 7 天')
    expect(relDeadline(at(2026, 8, 24), now).text).toBe('9月24日')
    // 分级不变——8 天不该因为文案变了就换档
    expect(relDeadline(at(2026, 8, 23), now).level).toBe('normal')
    expect(relDeadline(at(2026, 8, 24), now).level).toBe('normal')
  })
})
