#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按文档真实顺序抽取 .docx 内嵌图片，并生成「图文锚点」manifest.json。

用法：
    python extract_doc_images.py 稿件.docx
    python extract_doc_images.py 稿件.docx --out %USERPROFILE%\\tmp\\doc-images

产出：
    <out>/01_image1.png ...   按正文出现顺序导出的内嵌图片（保留原扩展名）
    <out>/manifest.json       数组，每项含：
        index        图片序号，从 1 开始，等于正文出现顺序
        file         导出文件名，形如 "01_image1.png"
        absolutePath 导出图片的绝对路径
        width/height 图片原始像素尺寸；图片格式无法解析时退回文档显示尺寸
                     （EMU ÷ 9525 → px，96dpi），并在 stderr 给出警告
        beforeText   图片所在段落之前最近 1~2 段正文文本（文档顺序，"\n" 连接；
                     每段去除首尾空白、压缩连续空白、截到 300 字，空段落跳过）
        afterText    图片所在段落之后最近 1~2 段正文文本，规则同上
        anchorText   图片所在段落自身的文字；同段无文字时取最近上文段落，
                     再退回最近下文段落；都没有则为 ""

遍历方式：直接按 document.element.body 的 XML 顺序取段落（含表格、内容控件
内的段落），再在段落内部按 XML 顺序找 w:drawing / w:pict 里的图片引用，
因此图片顺序与正文顺序一致（document.inline_shapes 的顺序会乱，不使用）。

退出码：0 成功；1 参数错误 / 文件问题 / 文档解析失败 / 图片解码失败，
错误信息一律为中文并输出到 stderr，不静默。

依赖：python-docx + 标准库；不引用本插件内任何模块。
"""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
import tempfile
import time
from pathlib import Path
from typing import Iterable, Iterator

try:
    from docx import Document
except ImportError:  # pragma: no cover - 依赖缺失时给出明确中文提示
    print(
        "错误：缺少依赖 python-docx，请先运行：python -m pip install python-docx",
        file=sys.stderr,
    )
    raise SystemExit(1)

R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
R_EMBED = "{%s}embed" % R_NS
R_LINK = "{%s}link" % R_NS
R_ID = "{%s}id" % R_NS

EMU_PER_PX = 9525           # 914400 EMU/英寸 ÷ 96 px/英寸
TEXT_LIMIT = 300            # beforeText / afterText 单条截断长度
CONTEXT_PARAGRAPHS = 2      # 图片前后各取几段正文
ANCHOR_SCAN_LIMIT = 50      # 找锚点段时最多回溯/前探多少段


class ExtractError(Exception):
    """带中文提示的抽取错误，main() 捕获后返回退出码 1。"""


# ---------------------------------------------------------------- 基础工具


def _local(tag: object) -> str:
    """'{ns}name' -> 'name'；非字符串标签原样返回。"""
    if isinstance(tag, str) and "}" in tag:
        return tag.rsplit("}", 1)[1]
    return str(tag)


def _int_or_none(value: object) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def _normalize(text: str) -> str:
    """去除首尾空白并压缩连续空白（含全角空格、换行）。"""
    return re.sub(r"\s+", " ", text or "").strip()


def _clip(text: str, limit: int = TEXT_LIMIT) -> str:
    return text if len(text) <= limit else text[:limit]


def _safe_name(name: str) -> str:
    """清洗成 Windows 合法文件名。"""
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    return name or "image"


# ---------------------------------------------------------------- 遍历段落


def _iter_container_paragraphs(container) -> Iterator[object]:
    """按 XML 顺序产出容器内的 w:p 元素（表格 -> 单元格 -> 段落）。"""
    for child in container.iterchildren():
        name = _local(child.tag)
        if name == "p":
            yield child
        elif name == "tbl":
            for el in child.iter():
                if _local(el.tag) == "p":
                    yield el
        elif name == "sdt":
            content = None
            for sub in child.iterchildren():
                if _local(sub.tag) == "sdtContent":
                    content = sub
                    break
            if content is not None:
                yield from _iter_container_paragraphs(content)


def _paragraph_text(p) -> str:
    """段落纯文本（含 w:t / 制表符 / 换行），已压缩空白。"""
    pieces: list[str] = []
    for el in p.iter():
        name = _local(el.tag)
        if name == "t":
            pieces.append(el.text or "")
        elif name in ("tab", "br", "cr"):
            pieces.append(" ")
    return _normalize("".join(pieces))


# ---------------------------------------------------------------- 图片定位


def _find_extent(el) -> tuple[int, int] | None:
    """找图片的显示尺寸（EMU）。先看 wp:inline / wp:anchor 的 wp:extent，
    再退回 pic:pic/a:xfrm/a:ext。找不到返回 None。"""
    node = el
    for _ in range(40):
        node = node.getparent()
        if node is None:
            return None
        name = _local(node.tag)
        if name in ("inline", "anchor"):
            for child in node.iterchildren():
                if _local(child.tag) == "extent":
                    cx = _int_or_none(child.get("cx"))
                    cy = _int_or_none(child.get("cy"))
                    if cx and cy:
                        return cx, cy
            break
        if name in ("drawing", "pict"):
            break

    pic = None
    for ancestor in el.iterancestors():
        if _local(ancestor.tag) == "pic":
            pic = ancestor
            break
    if pic is not None:
        for node in pic.iter():
            if _local(node.tag) != "xfrm":
                continue
            for child in node.iterchildren():
                if _local(child.tag) == "ext":
                    cx = _int_or_none(child.get("cx"))
                    cy = _int_or_none(child.get("cy"))
                    if cx and cy:
                        return cx, cy
    return None


def _find_paragraph_images(p) -> list[tuple[str, tuple[int, int] | None]]:
    """按 XML 顺序返回段落内的 (关系 id, 显示尺寸 EMU) 列表。"""
    hits: list[tuple[str, tuple[int, int] | None]] = []
    for el in p.iter():
        name = _local(el.tag)
        rid = None
        if name == "blip":                      # DrawingML
            rid = el.get(R_EMBED) or el.get(R_LINK)
        elif name == "imagedata":               # VML (w:pict)
            rid = el.get(R_ID)
        if not rid:
            continue
        hits.append((rid, _find_extent(el)))
    return hits


# ---------------------------------------------------------------- 图片解码


def _jpeg_size(blob: bytes) -> tuple[int, int]:
    i, n = 2, len(blob)
    while i + 9 <= n:
        if blob[i] != 0xFF:
            i += 1
            continue
        marker = blob[i + 1]
        if marker in (0xD8, 0xD9, 0x01) or 0xD0 <= marker <= 0xD7:
            i += 2
            continue
        seg_len = struct.unpack(">H", blob[i + 2:i + 4])[0]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height, width = struct.unpack(">HH", blob[i + 5:i + 9])
            if width and height:
                return int(width), int(height)
            raise ExtractError("JPEG 的 SOF 段尺寸为 0")
        if seg_len < 2:
            break
        i += 2 + seg_len
    raise ExtractError("JPEG 结构异常，未找到 SOF 尺寸段")


def _probe_image_size(blob: bytes) -> tuple[int, int] | None:
    """从图片头部读原始像素尺寸。

    认识的格式（PNG/JPEG/GIF/BMP）解析失败时抛 ExtractError（视为解码失败）；
    不认识的格式返回 None，由调用方退回文档显示尺寸并给出警告。
    """
    if not blob:
        raise ExtractError("图片二进制数据为空")
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        if len(blob) < 24:
            raise ExtractError("PNG 头部长度不足，无法解码")
        width, height = struct.unpack(">II", blob[16:24])
        if not width or not height:
            raise ExtractError("PNG 的 IHDR 尺寸为 0")
        return int(width), int(height)
    if blob[:2] == b"\xff\xd8":
        return _jpeg_size(blob)
    if blob[:6] in (b"GIF87a", b"GIF89a"):
        if len(blob) < 10:
            raise ExtractError("GIF 头部长度不足，无法解码")
        width, height = struct.unpack("<HH", blob[6:10])
        if not width or not height:
            raise ExtractError("GIF 逻辑屏幕尺寸为 0")
        return int(width), int(height)
    if blob[:2] == b"BM":
        if len(blob) < 26:
            raise ExtractError("BMP 头部长度不足，无法解码")
        width, height = struct.unpack("<ii", blob[18:26])
        return abs(int(width)), abs(int(height))
    return None


# ---------------------------------------------------------------- 上下文文本


def _collect_neighbors(texts: list[str], start: int, step: int,
                       limit: int = CONTEXT_PARAGRAPHS) -> list[str]:
    """从 start 开始沿 step 方向收集最多 limit 条非空段落文本，返回文档顺序。"""
    picked: list[str] = []
    i = start
    while 0 <= i < len(texts) and len(picked) < limit:
        if texts[i]:
            picked.append(_clip(texts[i]))
        i += step
    if step < 0:
        picked.reverse()
    return picked


def _nearest_text(texts: list[str], index: int, step: int) -> str:
    i = index
    for _ in range(ANCHOR_SCAN_LIMIT):
        i += step
        if not 0 <= i < len(texts):
            return ""
        if texts[i]:
            return _clip(texts[i])
    return ""


def _resolve_anchor(texts: list[str], index: int) -> str:
    """图片所在段落自身文字；同段无文字取最近上文，再退回最近下文。"""
    own = texts[index]
    if own:
        return _clip(own)
    return _nearest_text(texts, index, -1) or _nearest_text(texts, index, +1)


# ---------------------------------------------------------------- 主流程


def extract(docx_path: str, out_dir: str | None) -> dict:
    src = Path(docx_path).expanduser()
    if not src.exists():
        raise ExtractError("文件不存在：%s" % src)
    if not src.is_file():
        raise ExtractError("不是文件（可能是目录）：%s" % src)
    if src.suffix.lower() not in (".docx", ".docm"):
        shown = src.suffix if src.suffix else "（无扩展名）"
        raise ExtractError(
            "不是 .docx 文件：%s（当前扩展名 %s，只接受 .docx / .docm）" % (src, shown)
        )

    try:
        document = Document(str(src))
    except Exception as exc:  # zip 损坏、不是 OOXML 等
        raise ExtractError(
            "无法作为 .docx 打开：%s（%s: %s）" % (src, type(exc).__name__, exc)
        ) from exc

    body = document.element.body
    paragraphs = list(_iter_container_paragraphs(body))
    texts = [_paragraph_text(p) for p in paragraphs]
    hits_per_para = [_find_paragraph_images(p) for p in paragraphs]
    total_images = sum(len(hits) for hits in hits_per_para)
    if total_images == 0:
        raise ExtractError("文档内没有找到内嵌图片：%s" % src)

    if out_dir:
        dest = Path(out_dir).expanduser()
    else:
        stamp = time.strftime("%Y%m%d-%H%M%S")
        dest = Path(tempfile.gettempdir()) / ("xiumi-doc-images-%s" % stamp)
    try:
        dest.mkdir(parents=True, exist_ok=True)
        dest = dest.resolve()
    except OSError as exc:
        raise ExtractError("无法创建输出目录：%s（%s）" % (dest, exc)) from exc

    items: list[dict] = []
    warnings: list[str] = []
    display_sizes: dict[int, tuple[int, int]] = {}
    index = 0

    for para_index, hits in enumerate(hits_per_para):
        for rid, extent in hits:
            index += 1
            try:
                part = document.part.related_parts[rid]
            except KeyError as exc:
                raise ExtractError(
                    "第 %d 张图片的关系 %s 在文档中找不到，无法读取" % (index, rid)
                ) from exc

            blob = getattr(part, "blob", None) or b""
            if not blob:
                raise ExtractError("第 %d 张图片没有二进制数据（rId=%s）" % (index, rid))

            try:
                native = _probe_image_size(blob)
            except ExtractError as exc:
                raise ExtractError("第 %d 张图片解码失败：%s" % (index, exc)) from exc

            display = None
            if extent:
                display = (
                    max(1, round(extent[0] / EMU_PER_PX)),
                    max(1, round(extent[1] / EMU_PER_PX)),
                )
                display_sizes[index] = display

            if native is not None:
                width, height = native
            elif display is not None:
                width, height = display
                warnings.append(
                    "第 %d 张图片（%s）格式无法解析原始像素尺寸，已改用文档显示尺寸"
                    % (index, getattr(part, "content_type", "未知"))
                )
            else:
                raise ExtractError(
                    "第 %d 张图片无法解析尺寸（格式 %s，且文档未给出显示尺寸）"
                    % (index, getattr(part, "content_type", "未知"))
                )

            partname = Path(str(getattr(part, "partname", "") or "image.bin")).name
            partname = _safe_name(partname)
            stem = Path(partname).stem or ("image%d" % index)
            suffix = Path(partname).suffix
            filename = "%02d_%s%s" % (index, stem, suffix)
            target = dest / filename
            try:
                target.write_bytes(blob)
            except OSError as exc:
                raise ExtractError("写入图片失败：%s（%s）" % (target, exc)) from exc

            items.append({
                "index": index,
                "file": filename,
                "absolutePath": str(target),
                "width": int(width),
                "height": int(height),
                "beforeText": "\n".join(
                    _collect_neighbors(texts, para_index - 1, -1)
                ),
                "afterText": "\n".join(
                    _collect_neighbors(texts, para_index + 1, +1)
                ),
                "anchorText": _resolve_anchor(texts, para_index),
            })

    manifest_path = dest / "manifest.json"
    try:
        manifest_path.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError as exc:
        raise ExtractError("写入 manifest.json 失败：%s（%s）" % (manifest_path, exc)) from exc

    return {
        "source": str(src.resolve()),
        "dest": str(dest),
        "manifest": str(manifest_path),
        "items": items,
        "warnings": warnings,
        "paragraph_count": len(paragraphs),
        "display_sizes": display_sizes,
    }


def _one_line(text: str, limit: int = 80) -> str:
    flat = " / ".join(part for part in text.split("\n") if part) if text else ""
    if len(flat) > limit:
        flat = flat[:limit] + "…"
    return flat or "（空）"


def _print_summary(result: dict) -> None:
    items = result["items"]
    display_sizes = result.get("display_sizes", {})
    print("源文档：%s" % result["source"])
    print("输出目录：%s" % result["dest"])
    print("manifest：%s" % result["manifest"])
    print("正文段落 %d 段，内嵌图片 %d 张（按正文出现顺序）"
          % (result["paragraph_count"], len(items)))
    print("")
    for item in items:
        line = "图 %d | %s | %d x %d px" % (
            item["index"], item["file"], item["width"], item["height"]
        )
        display = display_sizes.get(item["index"])
        if display and display != (item["width"], item["height"]):
            line += "（文档显示 %d x %d px）" % display
        print(line)
        print("  上文：%s" % _one_line(item["beforeText"]))
        print("  本段：%s" % _one_line(item["anchorText"]))
        print("  下文：%s" % _one_line(item["afterText"]))
    for warning in result["warnings"]:
        print("警告：%s" % warning, file=sys.stderr)
    print("")
    print("已导出 %d 张图片 -> %s" % (len(items), result["dest"]))


def _force_utf8_stdio() -> None:
    """统一按 UTF-8 输出，避免被其他进程（node / 管道）捕获时中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # 某些包装流不支持重新配置
            pass


def main(argv: Iterable[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="按文档真实顺序抽取 .docx 内嵌图片，并生成图文锚点 manifest.json"
    )
    parser.add_argument("docx", help="要处理的 .docx 文件路径")
    parser.add_argument(
        "--out",
        default=None,
        help="图片与 manifest.json 的输出目录；默认 %%TEMP%%\\xiumi-doc-images-<时间戳>",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        result = extract(args.docx, args.out)
    except ExtractError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1

    _print_summary(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
