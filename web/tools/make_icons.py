"""生成 PWA 图标（纯 stdlib，无第三方依赖）。

    python web/tools/make_icons.py

为什么不用 Pillow：本机 .venv 没有 Pillow，而为了几个静态 PNG 引一个
图像库不划算。PNG 的最小写入路径（zlib + struct + CRC32）只有几十行，
且**产物可复现**——想要新图标改这里的参数重跑即可，不用手工修图。

设计：深底 + 暖色提灯。这是「守夜人」的字面意象，也保证在小尺寸下
（192px）仍然认得出。maskable 版把主体缩到安全区（内切圆）以内，
免得被 Android 的圆形/方形遮罩切掉。
"""

from __future__ import annotations

import pathlib
import struct
import zlib

BG = (11, 15, 20)          # #0b0f14 与前端 --color-vigil-bg 一致
BODY_HI = (242, 161, 61)   # #f2a13d 灯体亮部
BODY_LO = (200, 68, 38)    # #c84426 灯体暗部
FLAME = (255, 226, 150)    # #ffe296 灯芯

SS = 4  # 超采样倍数：先按 4 倍画再平均，得到抗锯齿边缘


def _rounded_rect(x: float, y: float, cx: float, cy: float,
                  hw: float, hh: float, r: float) -> bool:
    """点 (x,y) 是否落在以 (cx,cy) 为中心、半径 r 的圆角矩形内。"""
    dx = abs(x - cx) - (hw - r)
    dy = abs(y - cy) - (hh - r)
    if dx <= 0 or dy <= 0:
        return abs(x - cx) <= hw and abs(y - cy) <= hh
    return dx * dx + dy * dy <= r * r


def _lantern(nx: float, ny: float) -> tuple[int, int, int]:
    """归一化坐标 (0..1) 处该画什么颜色。nx/ny 已按安全区缩放。"""
    cx, cy = 0.5, 0.52

    # 光晕：越靠近灯体越暖
    d = ((nx - cx) ** 2 + (ny - cy) ** 2) ** 0.5
    glow = max(0.0, 1.0 - d / 0.34) ** 2

    # 灯体：竖长圆角矩形
    body = _rounded_rect(nx, ny, cx, cy, 0.20, 0.26, 0.07)
    # 上下灯盖：稍宽的短横条
    cap_t = _rounded_rect(nx, ny, cx, cy - 0.30, 0.26, 0.035, 0.03)
    cap_b = _rounded_rect(nx, ny, cx, cy + 0.30, 0.26, 0.035, 0.03)
    # 灯芯：灯体中央的椭圆
    flame_d = (((nx - cx) / 0.10) ** 2 + ((ny - cy) / 0.16) ** 2) ** 0.5

    if cap_t or cap_b:
        return BODY_HI
    if flame_d <= 1.0:
        return FLAME
    if body:
        # 灯体做上下渐变，避免死板的纯色块
        t = min(1.0, max(0.0, (ny - (cy - 0.26)) / 0.52))
        return tuple(
            int(BODY_HI[i] + (BODY_LO[i] - BODY_HI[i]) * t) for i in range(3)
        )

    if glow > 0:
        return tuple(
            int(BG[i] + (BODY_HI[i] - BG[i]) * glow * 0.55) for i in range(3)
        )
    return BG


def _render(size: int, scale: float) -> bytes:
    """画一张 size×size 的图。scale 控制主体大小（maskable 用 0.62）。"""
    half = size / 2.0
    rows = bytearray()
    for py in range(size):
        rows.append(0)  # PNG 每行的 filter 字节
        for px in range(size):
            r = g = b = 0
            for sy in range(SS):
                for sx in range(SS):
                    fx = (px + (sx + 0.5) / SS - half) / half
                    fy = (py + (sy + 0.5) / SS - half) / half
                    c = _lantern(fx / scale + 0.5, fy / scale + 0.5)
                    r += c[0]
                    g += c[1]
                    b += c[2]
            n = SS * SS
            rows += bytes((r // n, g // n, b // n))
    return bytes(rows)


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def write_png(path: pathlib.Path, size: int, scale: float = 1.0) -> None:
    raw = _render(size, scale)
    png = (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(raw, 9))
        + _chunk(b"IEND", b"")
    )
    path.write_bytes(png)
    print(f"  {path.name}  {size}×{size}  {len(png):,} 字节")


def main() -> None:
    out = pathlib.Path(__file__).resolve().parent.parent / "public"
    out.mkdir(parents=True, exist_ok=True)
    print(f"生成 PWA 图标 → {out}")
    write_png(out / "icon-192.png", 192)
    write_png(out / "icon-512.png", 512)
    # maskable：主体缩到安全区以内，免得被系统遮罩切掉
    write_png(out / "icon-maskable-512.png", 512, scale=0.62)


if __name__ == "__main__":
    main()
