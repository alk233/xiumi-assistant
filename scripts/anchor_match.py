#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把 t11 manifest 里的 anchorText 落到正文段号（可 import，也可命令行调用）。

命令行：
    python anchor_match.py manifest.json --paragraphs paragraphs.json [--threshold 0.6] [--pretty]

    manifest.json    extract_doc_images.py 的产物（数组，每项含 index / file / anchorText）
    paragraphs.json  正文按顺序的纯文本数组，例如 ["第一段正文", "第二段正文", ...]
    stdout           {"matches": [{"index", "file", "anchorText", "matchType",
                                   "paragraphIndex", "confidence", "snippet"[, "sameAnchor"]}]}

import：
    from anchor_match import match_anchors
    matches = match_anchors(manifest, paragraphs, threshold=0.6)

匹配优先级（每张图独立判定，按顺序第一个命中的策略生效）：
    1 exact       anchorText.strip() == 段落.strip()                → confidence 1.0
    2 normalized  去空白 / 全角转半角(NFKC) / 去标点符号 / 小写 后相等   → confidence 0.9
    3 fuzzy       最长公共子串长度 ÷ max(len(锚点), len(段落))        → confidence 为该分数
                  取所有段落中的最高分，且该分数必须 ≥ threshold（默认 0.6）
    4 none        都不满足 → paragraphIndex = -1, confidence 0.0, snippet ""

    fuzzy 的相似度在「去首尾空白后的原文」上计算，不做归一化，阈值判断也用同一分数。
    注意：t11 会把超过 300 字的 anchorText 截断，此时锚点只是段落前缀，
    按 max(len) 计分会偏低，长段落可能落到 none —— 需要更宽松召回时调低 threshold。

同一段落被多张图命中：
    按 manifest 原始顺序，第 k 张（k 从 0 起）命中同一段落时
    paragraphIndex = 该段索引 + 0.5 * k，并从第 2 张起追加 "sameAnchor": true。
    调用方按 paragraphIndex 升序排序即可还原「同段落配图的先后顺序」；
    只有 1 张图命中时 paragraphIndex 保持整数。

paragraphIndex 约定：
    0-based，指向 paragraphs 数组下标；-1 表示未匹配。同段落多图时会带 .5 偏移（浮点）。
    例：paragraphIndex = 1.5 表示「排在正文第 2 段（下标 1），并位于同段前一张图之后」。

退出码：0 成功；1 参数 / 文件 / JSON / 结构错误（中文错误信息写 stderr，不静默）。
依赖：仅标准库（difflib / json / unicodedata / argparse 等），不引用插件内任何模块。
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Iterable, Sequence

SNIPPET_LIMIT = 80          # snippet 展示的最大字符数


class AnchorMatchError(Exception):
    """带中文提示的匹配错误，main() 捕获后返回退出码 1。"""


# ---------------------------------------------------------------- 文本处理


def _normalize(text: str) -> str:
    """归一化：NFKC（全角转半角）→ 去空白 / 去标点符号 / 小写。"""
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    chars: list[str] = []
    for ch in text:
        category = unicodedata.category(ch)
        if category[0] in ("P", "S", "Z", "C") or ch.isspace():
            continue
        chars.append(ch.lower())
    return "".join(chars)


def _similarity(a: str, b: str) -> float:
    """最长公共子串长度 ÷ max(len(a), len(b))，取值范围 0.0 ~ 1.0。"""
    if not a or not b:
        return 0.0
    return _lcs_len(a, b) / float(max(len(a), len(b)))


def _lcs_len(a: str, b: str) -> int:
    """最长公共子串（连续）的长度。"""
    if not a or not b:
        return 0
    if len(a) > len(b):
        a, b = b, a
    matcher = difflib.SequenceMatcher(None, a, b, autojunk=False)
    block = matcher.find_longest_match(0, len(a), 0, len(b))
    return int(block.size)


def _snippet(text: str, limit: int = SNIPPET_LIMIT) -> str:
    flat = re.sub(r"\s+", " ", text or "").strip()
    if not flat:
        return ""
    return flat if len(flat) <= limit else flat[:limit] + "…"


# ---------------------------------------------------------------- 单张匹配


def _match_one(anchor: str, paragraphs: Sequence[str],
               normalized: Sequence[str], threshold: float) -> tuple[str, int, float]:
    """返回 (matchType, paragraphIndex, confidence)。"""
    anchor_text = anchor.strip()
    if not anchor_text:
        return "none", -1, 0.0

    # 1) exact：去首尾空白后完全相等
    for i, para in enumerate(paragraphs):
        if para and para == anchor_text:
            return "exact", i, 1.0

    # 2) normalized：去空白 / 全角转半角 / 去标点 / 小写 后相等
    anchor_norm = _normalize(anchor_text)
    if anchor_norm:
        for i, para_norm in enumerate(normalized):
            if para_norm and para_norm == anchor_norm:
                return "normalized", i, 0.9

    # 3) fuzzy：最长公共子串 ÷ max(len)，取最高分且 ≥ threshold
    best_index, best_score = -1, 0.0
    for i, para in enumerate(paragraphs):
        if not para:
            continue
        score = _similarity(anchor_text, para)
        if score > best_score:
            best_index, best_score = i, score
    if best_index >= 0 and best_score >= threshold:
        return "fuzzy", best_index, round(best_score, 4)

    return "none", -1, 0.0


# ---------------------------------------------------------------- 主函数


def match_anchors(manifest: list, paragraphs: list,
                  threshold: float = 0.6) -> list:
    """把 manifest 中每张图的 anchorText 匹配到 paragraphs 的段号。

    manifest     : [{"index": 1, "file": "01_x.png", "anchorText": "..."}]
                   anchorText 缺失 / 为空 / 非字符串时该图判为 none，不抛异常。
    paragraphs   : ["第一段正文", ...]；空数组时每张图都返回 none（不丢图、不崩溃）。
    threshold    : fuzzy 的最低相似度，0 ~ 1，默认 0.6。
    返回         : 与 manifest 等长、同顺序的匹配结果列表。
    """
    if not isinstance(manifest, list):
        raise AnchorMatchError("manifest 必须是数组，实际是 %s" % type(manifest).__name__)
    if not isinstance(paragraphs, list):
        raise AnchorMatchError("paragraphs 必须是数组，实际是 %s" % type(paragraphs).__name__)
    try:
        threshold = float(threshold)
    except (TypeError, ValueError) as exc:
        raise AnchorMatchError("threshold 必须是 0~1 之间的数字：%r" % (threshold,)) from exc
    if not 0.0 <= threshold <= 1.0:
        raise AnchorMatchError("threshold 必须在 0~1 之间：%r" % (threshold,))

    para_text = ["" if p is None else str(p) for p in paragraphs]
    para_stripped = [p.strip() for p in para_text]
    para_norm = [_normalize(p) for p in para_stripped]

    used: dict[int, int] = {}       # 段落下标 -> 已命中次数
    results: list[dict] = []

    for position, raw_item in enumerate(manifest):
        item = raw_item if isinstance(raw_item, dict) else {}

        index = item.get("index", position + 1)
        try:
            index = int(index)
        except (TypeError, ValueError):
            index = position + 1

        file_name = item.get("file")
        file_name = "" if file_name is None else str(file_name)

        anchor_raw = item.get("anchorText")
        anchor = "" if anchor_raw is None else str(anchor_raw)

        match_type, paragraph_index, confidence = _match_one(
            anchor, para_stripped, para_norm, threshold
        )

        if paragraph_index >= 0:
            hit_count = used.get(paragraph_index, 0)
            used[paragraph_index] = hit_count + 1
        else:
            hit_count = 0

        if paragraph_index >= 0 and hit_count > 0:
            # 同一段落被多张图命中：按 manifest 顺序给 0.5 的偏移标记
            output_index: float = paragraph_index + 0.5 * hit_count
            snippet = _snippet(para_text[paragraph_index])
        elif paragraph_index >= 0:
            output_index = paragraph_index
            snippet = _snippet(para_text[paragraph_index])
        else:
            output_index = -1
            snippet = ""

        match = {
            "index": index,
            "file": file_name,
            "anchorText": anchor,
            "matchType": match_type,
            "paragraphIndex": output_index,
            "confidence": confidence,
            "snippet": snippet,
        }
        if paragraph_index >= 0 and hit_count > 0:
            match["sameAnchor"] = True
        results.append(match)

    return results


# ---------------------------------------------------------------- 命令行


def _force_utf8_stdio() -> None:
    """统一按 UTF-8 输出，避免被其他进程（node / 管道）捕获时中文乱码。"""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            pass


def _read_json_array(path_text: str, label: str) -> list:
    path = Path(path_text).expanduser()
    if not path.exists():
        raise AnchorMatchError("%s 文件不存在：%s" % (label, path))
    if not path.is_file():
        raise AnchorMatchError("%s 不是文件（可能是目录）：%s" % (label, path))
    try:
        raw = path.read_text(encoding="utf-8-sig")
    except OSError as exc:
        raise AnchorMatchError("读取 %s 失败：%s（%s）" % (label, path, exc)) from exc
    except UnicodeDecodeError as exc:
        raise AnchorMatchError("%s 不是 UTF-8 文本：%s（%s）" % (label, path, exc)) from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise AnchorMatchError(
            "%s 不是合法 JSON：%s（第 %d 行第 %d 列：%s）"
            % (label, path, exc.lineno, exc.colno, exc.msg)
        ) from exc
    if not isinstance(data, list):
        raise AnchorMatchError(
            "%s 顶层必须是数组，实际是 %s：%s" % (label, type(data).__name__, path)
        )
    return data


def _print_preview(matches: list) -> None:
    """往 stderr 打一张人类可读的匹配摘要（不污染 stdout 的 JSON）。"""
    if not matches:
        print("没有图片需要匹配。", file=sys.stderr)
        return
    hits = sum(1 for m in matches if m["matchType"] != "none")
    print("匹配 %d / %d 张图片（paragraphIndex 为 0-based）：" % (hits, len(matches)),
          file=sys.stderr)
    for m in matches:
        flag = " [同段]" if m.get("sameAnchor") else ""
        print("  图 %s -> 段 %s  %s  conf=%.4f%s  %s"
              % (m["index"], m["paragraphIndex"], m["matchType"],
                 m["confidence"], flag, _snippet(m["snippet"], 40)),
              file=sys.stderr)


def main(argv: Iterable[str] | None = None) -> int:
    _force_utf8_stdio()
    parser = argparse.ArgumentParser(
        description="把 manifest 里的 anchorText 匹配到正文段号（paragraphIndex 为 0-based）",
        epilog="输出：stdout 为 {\"matches\": [...]} JSON，摘要写 stderr。",
    )
    parser.add_argument("manifest", help="t11 产出的 manifest.json 路径")
    parser.add_argument("--paragraphs", required=True,
                        help="正文纯文本数组的 JSON 路径，如 [\"第一段\", \"第二段\"]")
    parser.add_argument("--threshold", type=float, default=0.6,
                        help="fuzzy 匹配的最低相似度，0~1，默认 0.6")
    parser.add_argument("--pretty", action="store_true", help="缩进输出 JSON")
    parser.add_argument("--no-preview", action="store_true",
                        help="不往 stderr 打人类可读摘要")
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        manifest = _read_json_array(args.manifest, "manifest")
        paragraphs = _read_json_array(args.paragraphs, "paragraphs")
        matches = match_anchors(manifest, paragraphs, args.threshold)
    except AnchorMatchError as exc:
        print("错误：%s" % exc, file=sys.stderr)
        return 1

    payload = json.dumps(
        {"matches": matches},
        ensure_ascii=False,
        indent=2 if args.pretty else None,
    )
    print(payload)
    if not args.no_preview:
        _print_preview(matches)
    return 0


if __name__ == "__main__":
    sys.exit(main())
