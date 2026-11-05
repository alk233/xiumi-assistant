import copy
import importlib.util
import json
from pathlib import Path
import unittest

_MODULE_PATH = Path(__file__).resolve().parents[1] / "observations.py"
_SPEC = importlib.util.spec_from_file_location("layout_observations", _MODULE_PATH)
observations = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(observations)


def sample():
    return {
        "url": "https://v.xiumius.cn/board/v5/example/123",
        "origin": "external",
        "observation": {
            "version": 1, "rootKind": "xiumi-public", "rootConfidence": "explicit-content",
            "charCount": 60, "imageCount": 1, "paragraphCount": 1, "headingCount": 0,
            "sequence": [
                {"kind": "paragraph", "textLength": 60, "style": {"fontSize": 15, "lineHeight": 1.8}},
                {"kind": "image", "textLength": 0, "imageCount": 1, "aspectRatio": 1.33,
                 "style": {"width": 600, "height": 450}, "internalLayoutRecognized": False},
            ],
            "profile": {"charCount": 60, "imageCount": 1, "style": {"titles": [], "body": {"fontSize": 15, "lineHeight": 1.8, "paragraphGap": 18}, "palette": {"text": "#222222"}, "decor": {"cards": 1}}},
            "metrics": {"positionOnly": True, "hasSvgForeignObjectText": False, "editorEditabilityVerified": False},
            "sampling": {"elementLimit": 6000, "sequenceLimit": 600, "sampledElementCount": 5, "sampledEventCount": 2, "observedRenderedContent": True},
            "incomplete": False, "truncated": False, "visuallyVerified": False,
            "limitations": ["dom-structure-only", "image-internal-layout-unavailable"],
        },
    }


class FakePage:
    def __init__(self, value=None, error=None, url="https://v.xiumius.cn/board/v5/example/123?tracking=private"):
        self.value, self.error, self.url = value, error, url
        self.calls = []

    def evaluate(self, script):
        self.calls.append(script)
        if self.error:
            raise self.error
        return copy.deepcopy(self.value)


class ObservationTests(unittest.TestCase):
    def test_evaluates_only_current_page_and_outputs_structural_payload(self):
        page = FakePage(sample())
        result = observations.observe_page(page)
        self.assertEqual(page.calls, [observations.LAYOUT_OBSERVATION_JS])
        self.assertEqual(result["origin"], "external")
        self.assertEqual(result["url"], "https://v.xiumius.cn/board/v5/example/123")
        self.assertEqual(result["observation"]["articleId"], "123")
        self.assertEqual(result["observation"]["profile"]["style"]["body"]["paragraphGap"], 18)
        self.assertNotIn("tracking", json.dumps(result))

    def test_query_only_wechat_keeps_article_identity_without_publisher_or_query(self):
        page = FakePage(sample(), url="https://user:pass@mp.weixin.qq.com/s?__biz=private-publisher&mid=1&idx=1")
        result = observations.observe_page(page)
        self.assertEqual(result["url"], "https://mp.weixin.qq.com/s")
        self.assertRegex(result["observation"]["articleId"], r"^wx-[a-f0-9]{20}$")
        self.assertNotIn("private-publisher", json.dumps(result))
        self.assertNotIn("user:pass", json.dumps(result))

    def test_style_sampling_errors_raise_without_zero_success(self):
        with self.assertRaisesRegex(observations.LayoutObservationError, "未生成默认样本"):
            observations.observe_page(FakePage(error=RuntimeError("page closed")))
        for value in (None, {}, {"observation": {}}, {"observation": {"sequence": []}}):
            with self.subTest(value=value):
                with self.assertRaises(observations.LayoutObservationError):
                    observations.observe_page(FakePage(value))

    def test_original_text_image_urls_unknown_fields_and_nan_are_rejected(self):
        for field, value in (("rawText", "保密正文"), ("src", "https://example.com/image.png"), ("notes", "原文")):
            payload = sample()
            payload["observation"]["sequence"][0][field] = value
            with self.subTest(field=field):
                with self.assertRaisesRegex(observations.LayoutObservationError, "未授权字段"):
                    observations.observe_page(FakePage(payload))
        payload = sample()
        payload["observation"]["sequence"][0]["kind"] = "保密正文"
        with self.assertRaises(observations.LayoutObservationError):
            observations.observe_page(FakePage(payload))
        payload = sample()
        payload["observation"]["sequence"][0]["textLength"] = float("nan")
        with self.assertRaisesRegex(observations.LayoutObservationError, "无效数值"):
            observations.observe_page(FakePage(payload))

    def test_partial_real_samples_retain_explicit_flags(self):
        payload = sample()
        payload["observation"].update(incomplete=True, truncated=True)
        payload["observation"]["limitations"].append("sequence-truncated")
        result = observations.observe_page(FakePage(payload))
        self.assertTrue(result["observation"]["incomplete"])
        self.assertTrue(result["observation"]["truncated"])
        self.assertFalse(result["observation"]["visuallyVerified"])

    def test_rejects_claimed_visual_verification_and_unsupported_origins(self):
        payload = sample()
        payload["observation"]["visuallyVerified"] = True
        with self.assertRaisesRegex(observations.LayoutObservationError, "验证状态"):
            observations.observe_page(FakePage(payload))
        with self.assertRaises(observations.LayoutObservationError):
            observations.observe_page(FakePage(sample(), url="https://xiumi.us.evil.example/article"))

    def test_array_bound_is_enforced(self):
        payload = sample()
        payload["observation"]["sequence"] *= 301
        with self.assertRaisesRegex(observations.LayoutObservationError, "采样上限"):
            observations.observe_page(FakePage(payload))


if __name__ == "__main__":
    unittest.main()
