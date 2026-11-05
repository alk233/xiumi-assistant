#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""anchor_match.py 的单元测试（仅标准库 unittest，无第三方依赖）。

运行：
    python scripts/test_anchor_match.py -v

覆盖：
    a. 精确匹配（含首尾空白）
    b. 只差空格 / 标点 / 全角的归一化匹配
    c. 半句相同的模糊匹配（分数与「最长公共子串 ÷ max(len)」逐位核对）
    d. 完全无关 → none
    e. 多张图命中同一段 → 第二张起 sameAnchor=true 且 paragraphIndex 带 .5 偏移
    f. 空 manifest / 空段落数组 / 缺 anchorText / 空 anchorText / None 段落 → 不崩
    另加：threshold 生效、参数非法报错、CLI 往返与异常退出码
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from anchor_match import AnchorMatchError, match_anchors  # noqa: E402

MATCH_KEYS = ["index", "file", "anchorText", "matchType",
              "paragraphIndex", "confidence", "snippet"]


def brute_lcs(a: str, b: str) -> int:
    """独立实现的最长公共子串长度（用于核对模块给出的分数）。"""
    if not a or not b:
        return 0
    prev = [0] * (len(b) + 1)
    best = 0
    for i in range(1, len(a) + 1):
        cur = [0] * (len(b) + 1)
        ai = a[i - 1]
        for j in range(1, len(b) + 1):
            if ai == b[j - 1]:
                cur[j] = prev[j - 1] + 1
                if cur[j] > best:
                    best = cur[j]
        prev = cur
    return best


def brute_score(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    return brute_lcs(a, b) / float(max(len(a), len(b)))


class MatchStrategyTest(unittest.TestCase):
    def test_a_exact(self):
        manifest = [{"index": 1, "file": "01_a.png", "anchorText": "  第二段正文  "}]
        paragraphs = ["第一段正文", "第二段正文", "第三段正文"]
        matches = match_anchors(manifest, paragraphs)
        self.assertEqual(len(matches), 1)
        match = matches[0]
        self.assertEqual(match["matchType"], "exact")
        self.assertEqual(match["paragraphIndex"], 1)
        self.assertEqual(match["confidence"], 1.0)
        self.assertEqual(match["index"], 1)
        self.assertEqual(match["file"], "01_a.png")
        self.assertEqual(match["anchorText"], "  第二段正文  ")
        self.assertEqual(match["snippet"], "第二段正文")
        self.assertEqual(list(match.keys()), MATCH_KEYS)

    def test_b_normalized(self):
        # 全角数字 + 中文标点 + 空格：原文不同，归一化后相同
        manifest = [{"index": 2, "file": "02_b.png", "anchorText": "  「第２段，正文！」 "}]
        paragraphs = ["第一段正文", "第2段正文"]
        matches = match_anchors(manifest, paragraphs)
        self.assertEqual(matches[0]["matchType"], "normalized")
        self.assertEqual(matches[0]["paragraphIndex"], 1)
        self.assertEqual(matches[0]["confidence"], 0.9)
        self.assertEqual(matches[0]["snippet"], "第2段正文")

    def test_c_fuzzy_score_matches_formula(self):
        anchor = "今天天气很好我们去公园散步"
        paragraph = "今天天气很好我们去公园散步吧顺便看看花"
        manifest = [{"index": 3, "file": "03_c.png", "anchorText": anchor}]
        paragraphs = ["第一段完全无关", paragraph, "第三段也不相关"]

        expected = brute_score(anchor, paragraph)
        self.assertGreaterEqual(expected, 0.6)
        self.assertLess(expected, 1.0)

        matches = match_anchors(manifest, paragraphs)
        match = matches[0]
        self.assertEqual(match["matchType"], "fuzzy")
        self.assertEqual(match["paragraphIndex"], 1)
        self.assertAlmostEqual(match["confidence"], expected, places=4)
        self.assertEqual(match["confidence"], round(expected, 4))
        # 分数确实是「最长公共子串 ÷ max(len)」，此处锚点是段落前缀 → 13 / 19
        self.assertEqual(brute_lcs(anchor, paragraph), len(anchor))
        self.assertAlmostEqual(expected, len(anchor) / float(len(paragraph)), places=6)

    def test_c2_threshold_gates_fuzzy(self):
        anchor = "今天天气很好我们去公园散步"
        paragraph = "今天天气很好我们去公园散步吧顺便看看花"
        manifest = [{"index": 1, "file": "01.png", "anchorText": anchor}]
        loose = match_anchors(manifest, [paragraph], threshold=0.6)
        strict = match_anchors(manifest, [paragraph], threshold=0.9)
        self.assertEqual(loose[0]["matchType"], "fuzzy")
        self.assertEqual(strict[0]["matchType"], "none")
        self.assertEqual(strict[0]["paragraphIndex"], -1)

    def test_d_none(self):
        manifest = [{"index": 4, "file": "04_d.png", "anchorText": "完全无关的一句话XYZ"}]
        paragraphs = ["第一段正文", "第二段正文"]
        matches = match_anchors(manifest, paragraphs)
        match = matches[0]
        self.assertEqual(match["matchType"], "none")
        self.assertEqual(match["paragraphIndex"], -1)
        self.assertEqual(match["confidence"], 0.0)
        self.assertEqual(match["snippet"], "")
        self.assertNotIn("sameAnchor", match)

    def test_e_same_anchor(self):
        manifest = [
            {"index": 1, "file": "01_a.png", "anchorText": "第二段正文"},
            {"index": 2, "file": "02_b.png", "anchorText": "第二段正文"},
            {"index": 3, "file": "03_c.png", "anchorText": "第二段正文"},
        ]
        paragraphs = ["第一段正文", "第二段正文", "第三段正文"]
        matches = match_anchors(manifest, paragraphs)

        self.assertEqual([m["index"] for m in matches], [1, 2, 3])
        self.assertEqual(matches[0]["paragraphIndex"], 1)
        self.assertNotIn("sameAnchor", matches[0])
        self.assertEqual(matches[1]["paragraphIndex"], 1.5)
        self.assertTrue(matches[1]["sameAnchor"])
        self.assertEqual(matches[2]["paragraphIndex"], 2.0)
        self.assertTrue(matches[2]["sameAnchor"])
        # 只有多图命中的条目才追加 sameAnchor，且排在最后
        self.assertEqual(list(matches[1].keys()), MATCH_KEYS + ["sameAnchor"])
        # 按 paragraphIndex 升序排列即可还原「同段落配图先后」
        order = [m["index"] for m in sorted(matches, key=lambda m: m["paragraphIndex"])]
        self.assertEqual(order, [1, 2, 3])

    def test_f_empty_inputs(self):
        # 空 manifest → 空结果
        self.assertEqual(match_anchors([], ["第一段正文"]), [])
        self.assertEqual(match_anchors([], []), [])
        # 空段落数组 → 每张图都判 none（不丢图、不崩溃）
        matches = match_anchors(
            [{"index": 1, "file": "01.png", "anchorText": "第二段正文"}], []
        )
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["matchType"], "none")
        self.assertEqual(matches[0]["paragraphIndex"], -1)

    def test_f2_blank_anchor_and_bad_paragraphs(self):
        # 缺 anchorText
        missing = match_anchors([{"index": 1, "file": "01.png"}], ["第一段正文"])
        self.assertEqual(missing[0]["matchType"], "none")
        self.assertEqual(missing[0]["anchorText"], "")
        # 空 anchorText / 纯空白
        for blank in ("", "   ", "\n\t"):
            result = match_anchors(
                [{"index": 1, "file": "01.png", "anchorText": blank}], ["第一段正文"]
            )
            self.assertEqual(result[0]["matchType"], "none")
            self.assertEqual(result[0]["paragraphIndex"], -1)
        # 段落里有 None / 空串：跳过，不影响后面的正确匹配
        result = match_anchors(
            [{"index": 1, "file": "01.png", "anchorText": "第一段"}],
            ["", None, "第一段"],
        )
        self.assertEqual(result[0]["matchType"], "exact")
        self.assertEqual(result[0]["paragraphIndex"], 2)
        # manifest 条目不是 dict / index 非法：不崩，按位置兜底
        result = match_anchors(["bad-item", {"index": "x", "anchorText": "第一段"}],
                               ["第一段"])
        self.assertEqual([m["index"] for m in result], [1, 2])

    def test_invalid_arguments(self):
        with self.assertRaises(AnchorMatchError):
            match_anchors("not-a-list", [])
        with self.assertRaises(AnchorMatchError):
            match_anchors([], "not-a-list")
        with self.assertRaises(AnchorMatchError):
            match_anchors([], [], threshold=1.5)
        with self.assertRaises(AnchorMatchError):
            match_anchors([], [], threshold="not-a-number")


class CliTest(unittest.TestCase):
    def _write(self, path: Path, data) -> None:
        path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")

    def _run(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPT_DIR / "anchor_match.py"), *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace",
        )

    def test_cli_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = tmp_path / "manifest.json"
            paragraphs = tmp_path / "paragraphs.json"
            self._write(manifest, [
                {"index": 1, "file": "01_a.png", "anchorText": "第二段正文"},
                {"index": 2, "file": "02_b.png", "anchorText": "第二段正文"},
            ])
            self._write(paragraphs, ["第一段正文", "第二段正文"])

            proc = self._run(str(manifest), "--paragraphs", str(paragraphs), "--pretty")
            self.assertEqual(proc.returncode, 0, proc.stderr)
            payload = json.loads(proc.stdout)
            self.assertIn("matches", payload)
            self.assertEqual(len(payload["matches"]), 2)
            self.assertEqual(payload["matches"][0]["matchType"], "exact")
            self.assertEqual(payload["matches"][1]["paragraphIndex"], 1.5)
            self.assertTrue(payload["matches"][1]["sameAnchor"])
            # 人类可读摘要走 stderr，不污染 stdout 的 JSON
            self.assertIn("匹配 2 / 2 张图片", proc.stderr)

    def test_cli_missing_file(self):
        proc = self._run("no-such-manifest.json", "--paragraphs", "also-missing.json")
        self.assertEqual(proc.returncode, 1)
        self.assertIn("错误", proc.stderr)
        self.assertIn("文件不存在", proc.stderr)

    def test_cli_bad_json_and_wrong_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            bad = tmp_path / "bad.json"
            bad.write_text("{not json", encoding="utf-8")
            proc = self._run(str(bad), "--paragraphs", str(bad))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("不是合法 JSON", proc.stderr)

            obj = tmp_path / "obj.json"
            self._write(obj, {"matches": []})
            proc = self._run(str(obj), "--paragraphs", str(obj))
            self.assertEqual(proc.returncode, 1)
            self.assertIn("顶层必须是数组", proc.stderr)

    def test_cli_empty_manifest(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            manifest = tmp_path / "manifest.json"
            paragraphs = tmp_path / "paragraphs.json"
            self._write(manifest, [])
            self._write(paragraphs, ["第一段正文"])
            proc = self._run(str(manifest), "--paragraphs", str(paragraphs))
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertEqual(json.loads(proc.stdout), {"matches": []})


if __name__ == "__main__":
    unittest.main(verbosity=2)
