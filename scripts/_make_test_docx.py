#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成用于验证 extract_doc_images.py 的测试 .docx（含内嵌纯色 PNG）。

用法：
    python _make_test_docx.py                       # 默认写到 %TEMP%\\xiumi-docx-test\\
    python _make_test_docx.py %USERPROFILE%\\tmp\\demo.docx    # 指定输出文件
    python _make_test_docx.py --variant same-paragraph

variant=default（验证主流程）文档顺序：
    段1 / 段2 / 图A / 段3 / 段4 / 图B
    期望：图A.anchorText == "段2"，图B.anchorText == "段4"，共 2 条 manifest。

variant=same-paragraph（验证「图与文字同段」时 anchorText 取本段）文档顺序：
    段1 / 段2 / 图A / 【同段图文：图C 与这段文字位于同一段落】 / 段3 / 段4 / 图B
    期望：第 2 条 anchorText 为该段文字本身，共 3 条 manifest。

依赖：python-docx + 标准库（PNG 用 zlib 手写，不依赖 Pillow）。
"""

from __future__ import annotations

import argparse
import struct
import sys
import tempfile
import zlib
from pathlib import Path

try:
    from docx import Document
    from docx.shared import Pt
except ImportError:  # pragma: no cover
    print(
        "错误：缺少依赖 python-docx，请先运行：python -m pip install python-docx",
        file=sys.stderr,
    )
    raise SystemExit(1)

SAME_PARAGRAPH_TEXT = "同段图文：图C 与这段文字位于同一段落"


def solid_png(width: int, height: int, rgb: tuple[int, int, int]) -> bytes:
    """生成 width x height 的纯色 PNG（8bit truecolor，无隔行）。"""
    row = b"\x00" + bytes(rgb) * width
    raw = row * height

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def build_docx(target: Path, variant: str = "default") -> Path:
    work = target.parent / "_assets"
    work.mkdir(parents=True, exist_ok=True)
    png_a = work / "figure_a.png"
    png_b = work / "figure_b.png"
    png_c = work / "figure_c.png"
    png_a.write_bytes(solid_png(100, 80, (220, 60, 60)))     # 图A 红 100x80
    png_b.write_bytes(solid_png(120, 60, (60, 90, 220)))     # 图B 蓝 120x60
    png_c.write_bytes(solid_png(90, 90, (60, 180, 90)))      # 图C 绿 90x90

    doc = Document()

    # 段1 前后带空格，用于验证 beforeText/anchorText 的去首尾空白
    doc.add_paragraph("  段1  ")
    doc.add_paragraph("段2")
    doc.add_picture(str(png_a))          # 单独一段，本段无文字
    if variant == "same-paragraph":
        holder = doc.add_paragraph(SAME_PARAGRAPH_TEXT)
        holder.add_run().add_picture(str(png_c), width=Pt(60))
    doc.add_paragraph("段3")
    doc.add_paragraph("段4 ")
    doc.add_picture(str(png_b))          # 单独一段，本段无文字

    target.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(target))
    return target


def _force_utf8_stdio() -> None:
    """统一按 UTF-8 输出，避免被其他进程捕获时中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(description="生成 extract_doc_images.py 的测试 docx")
    parser.add_argument(
        "out",
        nargs="?",
        default=None,
        help="输出 .docx 路径；默认 %%TEMP%%\\xiumi-docx-test\\test_images.docx",
    )
    parser.add_argument(
        "--variant",
        choices=("default", "same-paragraph"),
        default="default",
        help="default=4 段文字 + 2 图；same-paragraph=额外验证图文同段",
    )
    args = parser.parse_args(argv)

    if args.out:
        target = Path(args.out).expanduser()
    else:
        name = (
            "test_images_same_paragraph.docx"
            if args.variant == "same-paragraph"
            else "test_images.docx"
        )
        target = Path(tempfile.gettempdir()) / "xiumi-docx-test" / name

    try:
        path = build_docx(target, args.variant)
    except OSError as exc:
        print("错误：无法写入测试文档 %s（%s）" % (target, exc), file=sys.stderr)
        return 1

    print("已生成测试文档：%s" % path)
    print("变体：%s" % args.variant)
    if args.variant == "same-paragraph":
        print("顺序：段1 / 段2 / 图A / 同段图文(图C) / 段3 / 段4 / 图B（共 3 张图）")
    else:
        print("顺序：段1 / 段2 / 图A / 段3 / 段4 / 图B（共 2 张图）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
