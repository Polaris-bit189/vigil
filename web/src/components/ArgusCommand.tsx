import { useState } from 'react'
import { applyArgusColor } from '../argusColor.ts'

/**
 * 提示符后面的输入行：目前只认 `/color`。
 * ⚠️ 错误**必须说出来**（认不出 / 太暗 / 未知命令）——终端里静默吞掉输入是最糟的形态。
 */
export default function ArgusCommand() {
  const [line, setLine] = useState('')
  const [msg, setMsg] = useState<{ text: string; bad: boolean } | null>(null)

  function run(raw: string) {
    const text = raw.trim()
    if (text === '') return
    if (text === '/color' || text.startsWith('/color ')) {
      const r = applyArgusColor(text.slice('/color'.length))
      setMsg(r.ok ? { text: `颜色已设为 ${r.value}`, bad: false } : { text: `错误：${r.reason}`, bad: true })
      return
    }
    setMsg({ text: `未知命令：${text.split(' ')[0]}（试试 /color green）`, bad: true })
  }

  return (
    <div className="min-w-0 flex-1">
      <form
        onSubmit={(e) => {
          e.preventDefault()
          run(line)
          setLine('')
        }}
      >
        <input
          aria-label="ARGUS 命令"
          value={line}
          onChange={(e) => setLine(e.target.value)}
          placeholder="/color green"
          spellCheck={false}
          autoComplete="off"
          className="w-full bg-transparent text-vigil-fg outline-none placeholder:text-vigil-fg-4"
        />
      </form>
      {msg !== null && (
        <p className={'truncate text-xs ' + (msg.bad ? 'text-vigil-danger' : 'text-vigil-fg-3')}>
          {msg.text}
        </p>
      )}
    </div>
  )
}
