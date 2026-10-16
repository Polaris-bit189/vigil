#!/usr/bin/env python3
"""M7 一次性搬迁：裸 Tailwind 调色板类名 → 语义 token 类名。

⚠️ **这是一次性脚本，不是常驻工具**（跑完就该没人再用它）。保留它的唯一理由是：
守卫测试只能证明「没有残留」，**证明不了「映射对不对」**——而这张映射表
是可审查的机械形式（把"为什么 sky-300 与 sky-200 合成同一个 token"这类判断
留在代码里，而不是留在某个人的记忆里）。

用法（在 `web/` 下）：
    python tools/retheme_m7.py --check     # 只报命中数（逐文件）
    python tools/retheme_m7.py --apply     # 落地

⚠️ 按**字节**读写（不经过文本换行转换）——见 M7 Global Constraints G5：
本仓 `web/src` 里 `w/lf` 与 `w/crlf` 混着，而 blob 全是 `i/lf`。
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src"

# 与 src/__tests__/noRawPalette.test.ts 的 PALETTE 同源（两份副本，测试那份说了算）
PALETTE = re.compile(
    r"\b(?:bg|text|border|ring|divide|from|to|via|placeholder|fill|stroke|shadow"
    r"|outline|decoration|accent|caret)-"
    r"(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald"
    r"|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|white|black)"
    r"(?:-\d{2,3})?(?:/\d{1,3})?\b"
)

# ⚠️ 按**键长降序**替换：`text-white/40` 必须先于 `text-white`
#（否则前者被后者的前缀吃掉，产出 `text-vigil-fg/40`——一个不存在的类名，
# 而 Tailwind 对不存在的类名**不报错**，只是不生成 ⇒ 元素没颜色、控制台安静）。
MAPPING: dict[str, str] = {
    # 面
    "bg-white/5": "bg-vigil-surface",
    "bg-white/10": "bg-vigil-surface-2",
    "bg-black/30": "bg-vigil-inset",
    "bg-vigil-bg/90": "bg-vigil-header",
    # 边框
    "border-white/10": "border-vigil-border",
    "border-white/5": "border-vigil-border-soft",
    # 文字四级（白系 13 档 → 4 档；档位合并是**设计决定**，不是误差）
    "text-white/95": "text-vigil-fg",
    "text-white/90": "text-vigil-fg",
    "text-white/85": "text-vigil-fg",
    "text-white/80": "text-vigil-fg",
    # ⚠️ R5（F2）:`/75`（≈76% 白）落 `fg`（#e6edf3，≈93%）= 全轮最大漂移（+40/通道），
    # 且落在**日报正文**上 ⇒ 单独降一档到 `fg-2`（72%）。`/80`、`/85` 仍归 `fg`。
    "text-white/75": "text-vigil-fg-2",
    "text-white/70": "text-vigil-fg-2",
    "text-white/60": "text-vigil-fg-2",
    "text-white/55": "text-vigil-fg-3",
    "text-white/50": "text-vigil-fg-3",
    "text-white/45": "text-vigil-fg-3",
    "text-white/40": "text-vigil-fg-4",
    "text-white/35": "text-vigil-fg-4",
    "text-white/30": "text-vigil-fg-4",
    "text-white": "text-vigil-fg",
    # 强调
    "bg-sky-500/20": "bg-vigil-accent-soft",
    "text-sky-400": "text-vigil-accent",
    "text-sky-300": "text-vigil-accent-text",
    "text-sky-200": "text-vigil-accent-text",
    # 危险
    "bg-red-500/25": "bg-vigil-danger-btn-active",
    "bg-red-500/20": "bg-vigil-danger-btn",
    "bg-red-500/15": "bg-vigil-danger-btn",
    "bg-red-500/10": "bg-vigil-danger-soft",
    "text-red-400": "text-vigil-danger",
    "text-red-300": "text-vigil-danger",
    "text-red-200": "text-vigil-danger-text",
    # 提醒
    "bg-amber-500/20": "bg-vigil-warn-btn",
    "bg-amber-500/10": "bg-vigil-warn-soft",
    "text-amber-400": "text-vigil-warn",
    "text-amber-200": "text-vigil-warn-text",
    "text-amber-100": "text-vigil-warn-text",
}

# 裸 `rounded` 是**字面量 .25rem**（E5）⇒ 换成可主题化的 `rounded-md`。
# 用词边界，别把 `rounded-lg` / `rounded-r-full` 的前缀也吃掉。
BARE_ROUNDED = re.compile(r"(?<![\w-])rounded(?![\w-])")


def files() -> list[Path]:
    return sorted(p for p in SRC.rglob("*.ts*") if p.is_file())


def rewrite(text: str) -> str:
    for old in sorted(MAPPING, key=len, reverse=True):
        text = text.replace(old, MAPPING[old])
    return BARE_ROUNDED.sub("rounded-md", text)


def main() -> int:
    apply = "--apply" in sys.argv
    total = 0
    for p in files():
        raw = p.read_bytes()
        text = raw.decode("utf-8")
        n = len(PALETTE.findall(text))
        total += n
        if n:
            print(f"{n:>4}  {p.relative_to(SRC.parent)}")
        if apply and n:
            out = rewrite(text)
            left = len(PALETTE.findall(out))
            if left:
                print(f"  ⛔ {p} 迁完还剩 {left} 处——映射表缺键，**没有写盘**")
                return 1
            p.write_bytes(out.encode("utf-8"))  # 按字节写：行尾原样保留（G5）
    print(f"总命中 {total}（应用前）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
