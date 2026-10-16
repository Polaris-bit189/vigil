import { cleanup } from '@testing-library/react'
import { afterEach } from 'vitest'

// ⚠️ 必须显式 cleanup：`@testing-library/react` 只在检测到**全局** `afterEach`
// 时才自动清理，而本仓的 vitest 没开 `globals: true`。
// 没有这一句，第二个用例会看到第一个用例留在 document 里的节点，
// `screen.getByText` 于是报「找到多个元素」——**那条失败与被测行为毫无关系**，
// 而它长得像产品缺陷。本项目在 `data/vigil.lock` 上吃过同一形状的亏
// （见 `tests/conftest.py::_isolate_lock_path` 的 docstring）。
afterEach(cleanup)
