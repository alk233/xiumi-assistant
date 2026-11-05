"""Editor evidence shared by the bridge and offline regression tests."""
from __future__ import annotations

import hashlib
import json
import re
from typing import Any


MODEL_SNAPSHOT_JS = r"""
() => {
  const root = document.querySelector('.tn-working-pallet');
  if (!root || !window.angular) return {complete: false, reason: 'editorModelUnavailable'};
  const ignored = new Set(['$$hashKey', 'selected', 'isSelected', 'hover', 'editing', 'dragging']);
  // Runtime-only fields: per-session uuids, viewport/geometry and reverse
  // lookups change between a write and a reload without any content change.
  const ignoredPattern = /uuid|viewport|backview|frontview|geometry|_\$|transient/i;
  const clone = (value, seen = new WeakSet()) => {
    if (value === null || ['string', 'number', 'boolean'].includes(typeof value)) return value;
    if (typeof value !== 'object' || seen.has(value)) return undefined;
    seen.add(value);
    if (Array.isArray(value)) {
      const out = value.map(v => clone(v, seen));
      seen.delete(value);
      return out;
    }
    const out = {};
    for (const key of Object.keys(value).sort()) {
      if (key.startsWith('$') || ignored.has(key) || ignoredPattern.test(key)) continue;
      const child = clone(value[key], seen);
      if (child !== undefined) out[key] = child;
    }
    seen.delete(value);
    return out;
  };
  const seen = new Set();
  const cells = [];
  let missing = 0;
  for (const el of root.querySelectorAll('.tn-cell, div[contenteditable="true"]')) {
    let scope = null;
    try { scope = angular.element(el).scope(); } catch { scope = null; }
    const cell = scope && scope.cell;
    if (!cell) {
      // Layout containers and slots (e.g. .tn-slice-slot) legitimately carry no
      // editor model; only a rendered editable node missing its model is a gap.
      // Unknown element shapes (offline fixtures) stay conservative.
      const editable = typeof el.getAttribute === 'function'
        ? el.getAttribute('contenteditable') === 'true'
        : true;
      if (editable) missing++;
      continue;
    }
    if (seen.has(cell)) continue;
    seen.add(cell);
    cells.push({model: clone(cell)});
  }
  const title = document.querySelector("input[placeholder='请输入图文的标题']");
  // A blank new article legitimately has zero cells; completeness only means
  // every rendered editable cell contributed its Angular model.
  return {complete: missing === 0, title: title ? title.value : '', cells};
}
"""


SAVE_WATCH_START_JS = r"""
() => {
  if (window.__xiumiSaveWatch) window.__xiumiSaveWatch.observer.disconnect();
  const state = {success: '', failure: ''};
  const success = ['保存成功', '保存完成', '保存到我的图文', '已保存'];
  const failure = ['保存失败', '文件保存数量', '文件数量已满', '已到上限', '已达上限', '登录已过期'];
  const existing = new Map();
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (r.width > 0 && r.height > 0 && s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0') {
      const text = String(el.innerText || el.textContent || '').trim();
      if (text.length <= 180) existing.set(el, text);
    }
  }
  const inspect = node => {
    const el = node.nodeType === 1 ? node : node.parentElement;
    if (!el || el.closest('.tn-working-pallet, [contenteditable="true"]')) return;
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    if (r.width <= 0 || r.height <= 0 || s.display === 'none' || s.visibility === 'hidden' || s.opacity === '0') return;
    const text = String(el.innerText || el.textContent || '').trim();
    if (!text || text.length > 180 || existing.get(el) === text) return;
    for (const word of failure) if (text.includes(word)) state.failure = word;
    for (const word of success) if (text.includes(word)) state.success = word;
  };
  const observer = new MutationObserver(mutations => {
    for (const m of mutations) {
      if (m.type === 'characterData' || m.type === 'attributes') inspect(m.target);
      for (const node of m.addedNodes || []) {
        inspect(node);
        if (node.querySelectorAll) for (const el of node.querySelectorAll('[role="alert"], .toast, .message')) inspect(el);
      }
    }
  });
  observer.observe(document.body, {childList: true, subtree: true, characterData: true, attributes: true, attributeFilter: ['style', 'class', 'hidden', 'aria-hidden']});
  window.__xiumiSaveWatch = {state, observer};
}
"""

SAVE_WATCH_READ_JS = "() => window.__xiumiSaveWatch ? {...window.__xiumiSaveWatch.state} : {}"
SAVE_WATCH_STOP_JS = "() => { if (window.__xiumiSaveWatch) { window.__xiumiSaveWatch.observer.disconnect(); delete window.__xiumiSaveWatch; } }"


def model_digest(raw: dict[str, Any]) -> str:
    """Hash full model content, styles, assets and order; never geometry or previews."""
    if not isinstance(raw, dict) or raw.get("complete") is not True:
        raise RuntimeError("无法完整读取编辑器模型，拒绝假定内容未变化。")
    cells = raw.get("cells")
    if not isinstance(cells, list):
        raise RuntimeError("编辑器模型没有可验证的单元格。")
    value = {"title": str(raw.get("title") or ""), "cells": cells}
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def cell_model_is_empty(model: Any) -> bool:
    """True only when a cell's normalized model carries no visible text."""
    if not isinstance(model, dict):
        return False
    raw = str(model.get("text") or "")
    return not re.sub(r"<[^>]+>", "", raw).replace("&nbsp;", " ").strip()


def content_equivalent(baseline: dict[str, Any], current: dict[str, Any]) -> bool:
    """Allow only server-appended empty placeholder cells; nothing else.

    Xiumi normalizes a saved draft by appending an empty cell (and rewriting
    component signatures). Ignoring every empty cell would hide a user clearing a
    paragraph, so only cells beyond the baseline that are provably empty are
    tolerated; every baseline cell must still match exactly. Any change to a
    baseline cell, a removed cell, or a non-empty appended cell counts as an edit.
    """
    base_hash = baseline.get("modelHash")
    current_hash = current.get("modelHash")
    if base_hash and current_hash and base_hash == current_hash:
        return True
    base_digests = baseline.get("cellDigests")
    current_digests = current.get("cellDigests")
    if not isinstance(base_digests, list) or not isinstance(current_digests, list):
        return False
    if not base_digests or not current_digests:
        return False
    if len(current_digests) < len(base_digests):
        return False
    current_empty = current.get("cellEmpty") or []
    if len(current_empty) < len(current_digests):
        return False
    if any(not bool(flag) for flag in current_empty[len(base_digests):]):
        return False
    return current_digests[: len(base_digests)] == base_digests


def verify_readback(expected: dict[str, Any], actual: dict[str, Any], article_id: str) -> dict[str, Any]:
    """Require independent readback of the exact saved article and full model."""
    reasons = []
    if not article_id or str(actual.get("articleId") or "") != str(article_id):
        reasons.append("articleIdentityMismatch")
    if not expected.get("modelHash") or expected.get("modelHash") != actual.get("modelHash"):
        reasons.append("modelReadbackMismatch")
    if str(expected.get("title") or "").strip() != str(actual.get("title") or "").strip():
        reasons.append("titleReadbackMismatch")
    return {"saveVerified": not reasons, "saveFailureReasons": reasons}
