import { fmtDateTime } from '../format.ts'
import type { Source } from '../types.ts'

export default function SourceList({ sources }: { sources: Source[] }) {
  if (sources.length === 0) {
    return (
      <p className="text-xs text-vigil-fg-4">
        这条没有可回溯的源消息（抽取时没记下来源）。
      </p>
    )
  }
  return (
    <ul className="space-y-2">
      {sources.map((s) => (
        <li key={s.msg_id} className="rounded-lg bg-vigil-inset p-2">
          <div className="flex flex-wrap gap-x-2 text-xs text-vigil-fg-3">
            <span>{s.sender || '（未解析出发信人）'}</span>
            <span>{fmtDateTime(s.ts)}</span>
            <span className="ml-auto">{s.group_name}</span>
          </div>
          {/* 原文照登、不截断：「不信 LLM」的落地就是让人能自己看原文 */}
          <p className="mt-1 text-sm whitespace-pre-wrap text-vigil-fg">
            {s.content}
          </p>
        </li>
      ))}
    </ul>
  )
}
