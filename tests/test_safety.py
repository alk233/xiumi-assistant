"""Offline safety regressions; never imports or launches the live bridge."""
import ast
import copy
import sys
import threading
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from safety import cell_model_is_empty, content_equivalent, model_digest, verify_readback


def bridge_class():
    tree = ast.parse((ROOT / "bridge.py").read_text(encoding="utf-8-sig"))
    selected = [node for node in tree.body if
                isinstance(node, ast.ClassDef) and node.name == "Bridge" or
                isinstance(node, ast.FunctionDef) and node.name == "_compose_norm_blocks"]
    namespace = {"ARTICLE_EDIT_JS": "edit", "MODEL_SNAPSHOT_JS": "model", "verify_readback": verify_readback,
                 "content_equivalent": content_equivalent, "cell_model_is_empty": cell_model_is_empty,
                 "SAVE_WATCH_START_JS": "watch-start", "SAVE_WATCH_READ_JS": "watch-read", "SAVE_WATCH_STOP_JS": "watch-stop", "TITLE_SELECTOR": "title",
                 "_article_id_from_url": lambda url: url.rsplit("/", 1)[-1],
                 "_profile_now_iso": lambda: "test-time", "_log": lambda *args: None}
    import time
    import uuid
    import hashlib
    namespace["time"] = time
    namespace["uuid"] = uuid
    namespace["hashlib"] = hashlib
    exec("from __future__ import annotations\n" + "\n".join(ast.unparse(n) for n in selected), namespace)
    return namespace["Bridge"]


class ModelEvidenceTests(unittest.TestCase):
    def setUp(self):
        self.model = {"complete": True, "title": "draft", "cells": [
            {"model": {"type": "text", "text": "a" * 250, "style": {"color": "red"}}},
            {"model": {"type": "image", "src": "image-a", "style": {}}},
        ]}

    def test_full_text_styles_images_and_order_change_digest(self):
        original = model_digest(self.model)
        for mutate in (
            lambda m: m["cells"][0]["model"]["style"].update(color="blue"),
            lambda m: m["cells"][0]["model"].update(text="a" * 249 + "b"),
            lambda m: m["cells"][1]["model"].update(src="image-b"),
            lambda m: m["cells"].reverse(),
        ):
            changed = copy.deepcopy(self.model)
            mutate(changed)
            self.assertNotEqual(original, model_digest(changed))

    def test_incomplete_snapshot_is_not_an_empty_success(self):
        with self.assertRaises(RuntimeError):
            model_digest({"complete": False, "cells": []})

    def test_id_or_save_toast_cannot_replace_model_readback(self):
        before = {"title": "draft", "modelHash": "expected"}
        self.assertFalse(verify_readback(before, {"title": "draft", "modelHash": "wrong", "articleId": "100"}, "100")["saveVerified"])
        self.assertFalse(verify_readback(before, {**before, "articleId": "200"}, "100")["saveVerified"])
        self.assertTrue(verify_readback(before, {**before, "articleId": "100"}, "100")["saveVerified"])


class BridgeSafetyTests(unittest.TestCase):
    def setUp(self):
        self.bridge = bridge_class()({})
        self.calls = []
        self.page = types.SimpleNamespace(url="https://xiumi.us/studio/v5#/paper/for/100")
        self.page.evaluate = lambda script, payload=None: self.calls.append((script, payload)) or {
            "ok": True, "cellCount": 1, "cells": [{"index": 0, "path": "cell-a", "text": "draft", "html": "<p>draft</p>", "style": {}}]}
        self.bridge._require_page = lambda: self.page
        self.bridge._editor_present = lambda page: True
        self.bridge._handle_restore_dialog = lambda page: None
        self.revision = "revision-1"
        self.bridge._model_snapshot = lambda page: {"articleId": page.url.rsplit("/", 1)[-1], "url": page.url,
                                                  "modelHash": self.revision, "title": "draft"}
        self.bridge._opened_article = {"articleId": "100"}

    def test_mutation_requires_prior_inspection_and_confirmation(self):
        with self.assertRaisesRegex(RuntimeError, "inspect"):
            self.bridge.article_edit({"action": "clear_cell", "cellIndex": 0, "confirm": True})
        self.assertEqual(self.calls, [])
        self.bridge.article_edit({"action": "inspect"})
        with self.assertRaises(ValueError):
            self.bridge.article_edit({"action": "clear_cell", "cellIndex": 0})

    def test_revision_drift_with_identical_content_is_allowed(self):
        """渲染态/图片懒加载会让 modelHash 漂移；文本指纹一致时不该误拦。"""
        self.bridge.article_edit({"action": "inspect"})
        self.calls.clear()
        self.revision = "revision-2"
        self.bridge.article_edit({"action": "clear_cell", "cellIndex": 0, "confirm": True})
        self.assertTrue(self.calls)

    def test_changed_content_after_inspection_is_refused(self):
        self.bridge.article_edit({"action": "inspect"})
        self.calls.clear()
        self.revision = "revision-2"
        self.bridge._model_cells = lambda page: [
            {"index": 0, "path": "cell-a", "text": "someone else edited"}
        ]
        with self.assertRaisesRegex(RuntimeError, "变化"):
            self.bridge.article_edit({"action": "clear_cell", "cellIndex": 0, "confirm": True})
        self.assertEqual(self.calls, [])
        self.page.url = "https://xiumi.us/studio/v5#/paper/for/200"
        with self.assertRaisesRegex(RuntimeError, "目标"):
            self.bridge.article_edit({"action": "clear_cell", "cellIndex": 0, "confirm": True})

    def test_cancelled_operation_stops_before_a_write(self):
        self.bridge._cancel_event = threading.Event()
        self.bridge._cancel_event.set()
        with self.assertRaisesRegex(RuntimeError, "取消"):
            self.bridge.article_edit({"action": "inspect"})
        self.assertEqual(self.calls, [])

    def test_another_new_or_open_operation_invalidates_even_an_identical_model(self):
        self.bridge._expected_target = {"documentToken": "old-draft", "revision": self.revision}
        with self.assertRaisesRegex(RuntimeError, "替换"):
            self.bridge._assert_mutation_target(self.page)
        self.assertEqual(self.calls, [])

    def test_request_revision_is_consumed_before_internal_save_after_editing(self):
        self.bridge._expected_target = {"articleId": "100", "revision": self.revision}
        self.bridge._assert_mutation_target(self.page)
        self.revision = "after-write"
        self.assertEqual(self.bridge._assert_mutation_target(self.page)["modelHash"], "after-write")

    def test_article_list_never_navigates_the_working_editor(self):
        state = {"closed": False}
        tab = types.SimpleNamespace(close=lambda: state.update(closed=True))
        self.page.context = types.SimpleNamespace(new_page=lambda: tab)
        self.bridge._article_list_page = lambda page: self.assertIs(page, tab) or [{"title": "fixture"}]
        self.assertEqual(self.bridge.article_list(), [{"title": "fixture"}])
        self.assertTrue(state["closed"])
        self.assertEqual(self.page.url.rsplit("/", 1)[-1], "100")

    def test_same_length_pending_dom_edit_blocks_navigation_even_before_model_commit(self):
        self.bridge._saved_baseline = {"title": "draft", "charCount": 5, "modelHash": self.revision,
            "cellDigests": ["cell-a"], "cellEmpty": [False]}
        self.bridge._guard_snapshot = lambda page: {"title": "draft", "charCount": 5, "cellCount": 1,
            "modelHash": "another-hash", "cellDigests": ["cell-b"], "cellEmpty": [False]}
        self.assertTrue(self.bridge._unsaved_guard(self.page)["blocked"])

    def test_server_appended_empty_cell_is_tolerated_but_content_is_not(self):
        baseline = {"title": "draft", "charCount": 5, "modelHash": "h1",
            "cellDigests": ["a", "b"], "cellEmpty": [False, False]}
        appended_empty = {**baseline, "modelHash": "h2", "cellDigests": ["a", "b", "c"],
            "cellEmpty": [False, False, True]}
        self.assertTrue(content_equivalent(baseline, appended_empty))
        appended_text = {**baseline, "modelHash": "h3", "cellDigests": ["a", "b", "c"],
            "cellEmpty": [False, False, False]}
        self.assertFalse(content_equivalent(baseline, appended_text))
        edited = {**baseline, "modelHash": "h4", "cellDigests": ["a", "changed"],
            "cellEmpty": [False, False]}
        self.assertFalse(content_equivalent(baseline, edited))
        removed = {**baseline, "modelHash": "h5", "cellDigests": ["a"], "cellEmpty": [False]}
        self.assertFalse(content_equivalent(baseline, removed))

    def test_edit_typed_while_the_readback_runs_still_blocks_leaving(self):
        # The saved baseline describes the server version, not whatever the page
        # shows after save(); an edit typed during the readback must stay dirty.
        self.bridge._saved_baseline = {"title": "draft", "charCount": 5, "modelHash": "saved",
            "cellDigests": ["a"], "cellEmpty": [False]}
        self.bridge._guard_snapshot = lambda page: {"title": "draft", "charCount": 9,
            "modelHash": "typed-during-readback", "cellDigests": ["a", "b"], "cellEmpty": [False, False]}
        self.assertTrue(self.bridge._unsaved_guard(self.page)["blocked"])

    def test_save_commits_pending_edit_before_capturing_readback_expectation(self):
        seen = []
        self.bridge._current_page = lambda: self.page
        self.bridge._expected_target = {"articleId": "100", "revision": self.revision}
        self.page.locator = lambda selector: types.SimpleNamespace(first=types.SimpleNamespace(click=lambda: setattr(self, "revision", "committed")))
        self.page.wait_for_timeout = lambda ms: None
        self.page.on = lambda *a: None
        self.page.remove_listener = lambda *a: None
        self.page.evaluate = lambda script: {"success": "保存成功"} if script == "watch-read" else None
        self.bridge._editor_snapshot = lambda *a, **kw: {"title": "draft", "charCount": 5, "articleId": "100"}
        self.bridge._read_editor = lambda page: ("draft", "draft", True)
        self.bridge._click_save = lambda page: True
        self.bridge._save_verification = lambda page, before, *a, **kw: seen.append(before["modelHash"]) or {"saveVerified": True, "saveFailureReasons": [], "articleId": "100", "saveEvidence": {}}
        result = self.bridge.article_save({"confirm": True})
        self.assertEqual(seen, ["committed"])
        self.assertTrue(result["saved"] and result["saveVerified"])

    def test_navigation_never_implicitly_saves_a_dirty_preview(self):
        self.bridge._unsaved_guard = lambda page, discard=False: {"ok": False, "blocked": True, "unsavedChanges": True}
        self.bridge.article_save = lambda *a: self.fail("navigation must not auto-save")
        self.assertTrue(self.bridge._leave_editor_guarded(self.page)["blocked"])
        self.assertTrue(self.bridge._leave_editor_guarded(self.page, discard=True)["discardedDraft"])

    def test_inspection_hides_raw_cell_html_but_keeps_internal_preconditions(self):
        result = self.bridge.article_edit({"action": "inspect"})
        self.assertEqual(result["revision"], self.revision)
        self.assertNotIn("html", result["cells"][0])
        self.assertIn("html", self.bridge._edit_inspection["cells"][0])

    def test_image_first_compose_returns_structured_refusal(self):
        result = self.bridge.compose({"mode": "create", "dryRun": True, "blocks": [
            {"type": "image", "assetId": "fixture"}, {"type": "paragraph", "text": "fixture"}]})
        self.assertFalse(result["ok"])
        self.assertEqual(result["reason"], "unsupported")

    def test_verification_uses_a_separate_page_and_closes_it(self):
        state = {"closed": False, "navigated": ""}
        tab = types.SimpleNamespace(url=self.page.url,
            goto=lambda url, **kw: state.update(navigated=url), close=lambda: state.update(closed=True))
        self.page.context = types.SimpleNamespace(new_page=lambda: tab)
        self.bridge._wait_editor = lambda *a, **kw: True
        self.bridge._restore_dialog_info = lambda page: None
        self.bridge._editor_snapshot = lambda *a, **kw: {"charCount": 5}
        result = self.bridge._save_verification(self.page, {"title": "draft", "modelHash": self.revision}, "draft", 5, "saved", article_id="100")
        self.assertTrue(result["saveVerified"])
        self.assertTrue(result["saveEvidence"]["independentReadback"])
        self.assertTrue(state["closed"])
        self.assertIn("/100", state["navigated"])

    def test_same_title_and_charcount_does_not_replace_full_model_readback(self):
        # A same-length edit, a colour change or a reordered image keeps title and
        # charCount identical; acceptance must still fail on the model mismatch.
        tab = types.SimpleNamespace(url=self.page.url, close=lambda: None)
        self.page.context = types.SimpleNamespace(new_page=lambda: tab)
        self.bridge._wait_editor = lambda *a, **kw: True
        self.bridge._restore_dialog_info = lambda page: None
        self.bridge._editor_snapshot = lambda *a, **kw: {"charCount": 5}
        self.bridge._model_snapshot = lambda page: {"articleId": "100", "title": "draft",
            "url": page.url, "modelHash": "changed-during-save"}
        result = self.bridge._save_verification(self.page, {"title": "draft", "modelHash": self.revision}, "draft", 5, "saved", article_id="100")
        self.assertFalse(result["saveVerified"])
        self.assertIn("modelReadbackUnconfirmed", result["saveFailureReasons"])

    def test_unavailable_readback_keeps_saved_true_but_unverified(self):
        self.page.context = types.SimpleNamespace(new_page=lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        result = self.bridge._save_verification(self.page, {"title": "draft", "modelHash": self.revision}, "draft", 5, "saved", article_id="100")
        self.assertFalse(result["saveVerified"])
        self.assertIn("independentReadbackUnavailable", result["saveFailureReasons"])


if __name__ == "__main__":
    unittest.main()
