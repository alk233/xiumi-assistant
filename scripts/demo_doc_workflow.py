#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""一条命令演示「稿件 .docx -> 配图计划」的完整链路。

用法：
    python demo_doc_workflow.py 稿件.docx [--out DIR] [--threshold 0.6]

流程（复用现有模块，不重复实现抽取与匹配逻辑）：
    1) extract_doc_images.extract(...)   按正文真实顺序抽图，生成 manifest.json
    2) anchor_match.match_anchors(...)   把每张图的 anchorText 落到正文段号
    3) 在同一输出目录写出 insert-plan.json

insert-plan.json：
{
  "source": "稿件.docx 的绝对路径",
  "paragraphCount": 4,
  "images": [
    {"index": 1, "file": "01_image1.png", "absolutePath": "...",
     "anchorText": "段2", "paragraphIndex": 1, "insertAfterParagraph": 2,
     "confidence": 1.0, "matchType": "exact", "sameAnchor": false}
  ],
  "unmatched": [
    {"index": 2, "file": "02_image2.png", "absolutePath": "...",
     "anchorText": "无关文本", "matchType": "none", "confidence": 0.0,
     "reason": "未找到相似度不低于阈值的正文段落"}
  ]
}

字段说明：
    paragraphCount      参与编号的正文段数：只有去掉纯图片段 / 空段后的「有字段落」才计数，
                        这样「第 N 段」与人在 Word 里看到的有字段落一致。
    paragraphIndex      anchor_match 给出的 0-based 段号，-1 表示未匹配；
                        同一段被多张图命中时，第 2 张起会带 .5 偏移（1.5 / 2.0 ...）以保留先后顺序。
    insertAfterParagraph paragraphIndex + 1，1-based，直接对应「插入到第 N 段之后」；
                        整数表示该段之后，带 .5 表示「同一段内、排在前一张图之后」，按升序依次插入即可。
    images              已定位的图片（按文档出现顺序）。
    unmatched           没能定位的图片，单独列出，绝不丢弃，由人工 / Agent 复核。

退出码：
    0  流程正常（即使有图片落进 unmatched，也算正常结束，并打印醒目提示）
    1  文档错误 / 依赖缺失 / 读写失败（错误信息为中文，写 stderr）

依赖：python-docx + 标准库；同目录需要 extract_doc_images.py 与 anchor_match.py。
"""

from __future__ import annotations

import argparse
import json
import sys
import unicodedata
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

_IMPORT_ERROR: BaseException | None = None
try:
    from docx import Document

    # 复用 extract_doc_images 的段落遍历与取文函数，保证「段号」与 manifest
    # 的 anchorText 出自同一套遍历逻辑（含表格内段落），不会因两套实现而错位。
    from extract_doc_images import (
        ExtractError,
        _iter_container_paragraphs,
        _paragraph_text,
        extract,
    )
    from anchor_match import AnchorMatchError, match_anchors
except ImportError as exc:  # 缺 python-docx，或两个脚本不在同目录
    _IMPORT_ERROR = exc


PLAN_FILENAME = "insert-plan.json"


# ---------------------------------------------------------------- 小工具


def _force_utf8_stdio() -> None:
    """统一按 UTF-8 输出，避免被 node / 管道捕获时中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _display_width(text: str) -> int:
    width = 0
    for ch in text:
        width += 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
    return width


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _pad(text: str, width: int, align: str = "left") -> str:
    """按显示宽度补空格（中日韩字符算 2 格）。"""
    pad = max(0, width - _display_width(text))
    return (" " * pad + text) if align == "right" else (text + " " * pad)


def _paragraph_after_label(value: float) -> str:
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    return "第 %s 段后" % (int(value) if isinstance(value, float) else value)


# ---------------------------------------------------------------- 正文段落


def _load_body_paragraphs(docx_path: str) -> tuple[list[str], int]:
    """读取 docx 正文段落文本，返回 (有字段落列表, 原始段落总数)。

    - 遍历方式与 extract_doc_images 一致（body 内 w:p / 表格内段落 / sdt），
      因此匹配用的段号与该 manifest 的锚点语义一致。
    - 纯图片段落在 Word 里没有文字，抽取后是空串；这里过滤掉，
      使 paragraphCount / paragraphIndex 只数「有字段落」，与人眼看到的段号对齐。
    """
    try:
        document = Document(str(docx_path))
        texts = [_paragraph_text(p) for p in _iter_container_paragraphs(document.element.body)]
    except Exception as exc:  # 损坏的 docx 等
        raise ExtractError(
            "无法读取正文段落：%s（%s: %s）" % (docx_path, type(exc).__name__, exc)
        ) from exc
    paragraphs = [text for text in texts if text]
    return paragraphs, len(texts)


# ---------------------------------------------------------------- 配图计划


def build_plan(docx_path: str, out_dir: str | None = None,
               threshold: float = 0.6) -> dict:
    """跑完整链路并写出 insert-plan.json，返回生成结果（含 plan 与路径）。"""
    result = extract(docx_path, out_dir)
    paragraphs, raw_paragraph_count = _load_body_paragraphs(result["source"])
    matches = match_anchors(result["items"], paragraphs, threshold)

    images: list[dict] = []
    unmatched: list[dict] = []
    seen_same_paragraph: dict[float, int] = {}

    for item, match in zip(result["items"], matches):
        anchor = item.get("anchorText") or ""
        if isinstance(anchor, str):
            anchor = anchor.strip()
        else:
            anchor = str(anchor)

        match_type = match.get("matchType", "none")
        paragraph_index = match.get("paragraphIndex", -1)
        confidence = match.get("confidence", 0.0)

        if match_type == "none" or not isinstance(paragraph_index, (int, float)) \
                or paragraph_index < 0:
            if not anchor:
                reason = "锚点文本为空：图片所在段落及邻近段落都没有文字，无法定位"
            else:
                reason = "未找到相似度不低于 %.2f 的正文段落，请人工确认插图位置" % threshold
            unmatched.append({
                "index": item.get("index"),
                "file": item.get("file"),
                "absolutePath": item.get("absolutePath"),
                "anchorText": anchor,
                "matchType": match_type,
                "confidence": confidence,
                "reason": reason,
            })
            continue

        base = int(paragraph_index)
        seen_same_paragraph[base] = seen_same_paragraph.get(base, 0) + 1
        images.append({
            "index": item.get("index"),
            "file": item.get("file"),
            "absolutePath": item.get("absolutePath"),
            "anchorText": anchor,
            "paragraphIndex": paragraph_index,
            "insertAfterParagraph": paragraph_index + 1,
            "confidence": confidence,
            "matchType": match_type,
            "sameAnchor": bool(match.get("sameAnchor")),
        })

    plan = {
        "source": result["source"],
        "paragraphCount": len(paragraphs),
        "images": images,
        "unmatched": unmatched,
    }

    plan_path = Path(result["dest"]) / PLAN_FILENAME
    try:
        plan_path.write_text(
            json.dumps(plan, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    except OSError as exc:
        raise ExtractError("写入 %s 失败：%s（%s）" % (plan_path, plan_path, exc)) from exc

    return {
        "plan": plan,
        "planPath": str(plan_path),
        "dest": result["dest"],
        "warnings": result.get("warnings", []),
        "rawParagraphCount": raw_paragraph_count,
        "threshold": threshold,
    }


# ---------------------------------------------------------------- 输出


def _print_table(plan: dict) -> None:
    images = plan["images"]
    unmatched = plan["unmatched"]
    if not images and not unmatched:
        print("文档里没有图片，无需插图。")
        return

    columns = (
        ("图号", 6, "right"),
        ("文件名", 22, "left"),
        ("锚点", 30, "left"),
        ("命中段", 12, "left"),
        ("置信度", 8, "right"),
        ("状态", 12, "left"),
    )
    separator_width = sum(width for _, width, _ in columns) + 2 * (len(columns) - 1)
    print("=== 图文对照表 ===")
    print("  ".join(_pad(title, width, align) for title, width, align in columns).rstrip())
    print("-" * separator_width)

    # 同段第几张：按 paragraphIndex 的整数部分统计，便于人理解 .5 偏移的来源
    order_in_paragraph: dict[int, int] = {}
    rows = images + unmatched
    rows.sort(key=lambda item: (item.get("index") is None, item.get("index") or 0))
    for item in rows:
        anchor_text = _clip(item.get("anchorText") or "", 28) or "（空）"
        if "insertAfterParagraph" in item:
            paragraph_index = item["paragraphIndex"]
            base = int(paragraph_index)
            order_in_paragraph[base] = order_in_paragraph.get(base, 0) + 1
            position = _paragraph_after_label(item["insertAfterParagraph"])
            if item.get("sameAnchor"):
                status = "同段第 %d 张" % order_in_paragraph[base]
            else:
                status = "已定位"
            confidence = "%.2f" % item.get("confidence", 0.0)
        else:
            position = "—"
            status = "未匹配"
            confidence = "%.2f" % item.get("confidence", 0.0)
        cells = (
            _pad(str(item.get("index")), 6, "right"),
            _pad(_clip(str(item.get("file") or ""), 20), 22, "left"),
            _pad(anchor_text, 30, "left"),
            _pad(position, 12, "left"),
            _pad(confidence, 8, "right"),
            _pad(status, 12, "left"),
        )
        print("  ".join(cells).rstrip())


def _print_summary(outcome: dict) -> None:
    plan = outcome["plan"]
    matched = len(plan["images"])
    missed = len(plan["unmatched"])
    total = matched + missed

    print()
    print("正文段落：%d 段（有字段落计入编号；原始段落共 %d 个，含纯图片段）"
          % (plan["paragraphCount"], outcome["rawParagraphCount"]))
    print("图片：共 %d 张，已定位 %d 张，未匹配 %d 张" % (total, matched, missed))
    print("图片目录：%s" % outcome["dest"])
    print("计划文件：%s" % outcome["planPath"])

    if missed:
        print()
        print("⚠ 有 %d 张图片没能自动定位（已写入 insert-plan.json 的 unmatched，未丢弃）。"
              % missed)
        print("  请人工确认它们该插到哪一段；必要时调低 --threshold（建议 0.4~0.5）后重跑。")
    else:
        print()
        print("下一步：按 absolutePath 上传图片 -> xiumi_write 写正文 -> 按 insertAfterParagraph")
        print("        插图（1-based，插到第 N 段之后；插图工具尚未提供时按本计划人工插入），保存前先预览确认。")

    for warning in outcome["warnings"]:
        print("注意：%s" % warning, file=sys.stderr)


# ---------------------------------------------------------------- 入口


def main(argv: list[str] | None = None) -> int:
    _force_utf8_stdio()

    parser = argparse.ArgumentParser(
        description="演示「稿件 .docx -> 配图计划」：抽图 + 锚点匹配 + insert-plan.json",
        epilog="示例：python demo_doc_workflow.py 稿件.docx --out %TEMP%\\xiumi-demo --threshold 0.6",
    )
    parser.add_argument("docx", help="稿件 .docx 路径")
    parser.add_argument(
        "--out",
        default=None,
        help="图片与 insert-plan.json 的输出目录；默认 %%TEMP%%\\xiumi-doc-images-<时间戳>",
    )
    parser.add_argument(
        "--threshold", type=float, default=0.6,
        help="锚点模糊匹配的最低相似度，0~1，默认 0.6；长段落可试 0.4~0.5",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)

    if _IMPORT_ERROR is not None:
        print("错误：依赖缺失或脚本不同目录——%s" % _IMPORT_ERROR, file=sys.stderr)
        print("      请先安装 python-docx（python -m pip install python-docx），"
              "并确保 extract_doc_images.py、anchor_match.py 与本脚本在同一目录。",
              file=sys.stderr)
        return 1

    try:
        outcome = build_plan(args.docx, args.out, args.threshold)
    except ExtractError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except AnchorMatchError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1
    except OSError as exc:
        print("错误：读写文件失败：%s" % exc, file=sys.stderr)
        return 1

    print("稿件：%s" % outcome["plan"]["source"])
    print()
    _print_table(outcome["plan"])
    _print_summary(outcome)
    return 0


if __name__ == "__main__":
    sys.exit(main())
