# -*- coding: utf-8 -*-
"""xiumi-assistant 浏览器桥接服务（常驻子进程）。

Host 插件 index.js 以子进程方式启动本文件，通过 stdin/stdout 逐行交换 JSON：

    请求  {"id": 1, "method": "open", "params": {}}
    成功  {"id": 1, "ok": true, "result": {...}}
    失败  {"id": 1, "ok": false, "error": "...", "trace": "..."}

每处理完一条必须 flush；stdout 只允许出现协议 JSON，诊断日志一律走 stderr，
否则 Node 侧按行解析会读到脏数据。

本机实测约束（勿随手改动）：
1. 启动参数从 sys.argv[1] 的 JSON 读取
   {pythonPath, browserChannel, headless, loginTimeoutMs}；
2. 系统 TEMP 在 DSH 子进程里创建临时目录会 EPERM（mkdtemp），必须在
   import playwright 之前把 TMP/TEMP/TMPDIR 指向插件目录下的 .tmp；
3. 本机没有 Playwright 自带 Chromium，也没有 Chrome，只有系统 Edge，
   必须 p.chromium.launch(headless=False, channel=config.browserChannel or "msedge")；
4. 登录判定照搬已验证的 login_check.py：秀米未登录也能进编辑器，不能拿能否
   进编辑器当登录依据；可靠信号是右上角「登录」入口是否可见；
5. 本桥接绝不加载外部导出的登录凭据文件或预采集登录态；登录态只能
   来自当前可见窗口里的实时操作；启动的浏览器配置是一次性隔离的。

异常处理：用户随时可能手动关窗，所有 page 操作都要捕获关闭类异常，
status 退化为 {running: false, ...}，进程本身不崩。
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import queue
import threading
import time
import traceback
import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from statistics import median
from typing import Any
from urllib.parse import urlparse
from safety import (
    MODEL_SNAPSHOT_JS,
    SAVE_WATCH_START_JS,
    SAVE_WATCH_READ_JS,
    SAVE_WATCH_STOP_JS,
    cell_model_is_empty,
    content_equivalent,
    model_digest,
    verify_readback,
)
from observations import observe_page

HERE = Path(__file__).resolve().parent
TMP_DIR = HERE / ".tmp"
SCREENS_DIR = TMP_DIR / "screens"
# 固定持久化浏览器配置目录：浏览器进程重启后仍复用同一份 cookie/登录态。
# 浏览器登录态要跨标签页关闭/桥接重启保留，放在稳定的用户本地目录；
# 不用插件 .tmp（会被清理），也不用系统 Edge 的默认配置目录。
# 允许用 XIUMI_PROFILE_DIR 覆盖：某些环境（沙箱/权限）下 %LOCALAPPDATA% 不可写时，
# 可以指到用户自己确认可写的位置，避免浏览器起不来。
BROWSER_PROFILE_DIR = Path(
    os.environ.get("XIUMI_PROFILE_DIR")
    or (Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "xiumi-assistant" / "edge-profile")
)

# 只读质检：读取当前编辑器的标题/正文 cell 与 computedStyle，不写模型、不保存。
_REVIEW_DECOR_RE = re.compile(r"^[\s✦★❖✧☆━─═◇◆·•—–\-_=~＊*]+$")

REVIEW_METRICS_JS = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const px = (value) => {
    const n = Number.parseFloat(String(value || ''));
    return Number.isFinite(n) ? n : null;
  };
  const titleInput = document.querySelector("input[placeholder='请输入图文的标题']");
  const title = titleInput ? clean(titleInput.value) : '';
  const cells = [];
  const paragraphs = [];
  for (const el of document.querySelectorAll("div[contenteditable='true']")) {
    const rect = el.getBoundingClientRect();
    if (rect.width < 80 || rect.height < 10) continue;
    if (rect.left < 350) continue;
    let style = {};
    try {
      const scope = angular.element(el).scope();
      style = (scope && scope.cell && scope.cell.style) || {};
    } catch (err) {
      style = {};
    }
    const cellText = clean(el.innerText);
    const cellIndex = cells.length;
    cells.push({
      text: cellText,
      textLength: cellText.length,
      style,
      rect: { x: rect.left, y: rect.top, w: rect.width, h: rect.height },
    });
    const nodes = Array.from(el.querySelectorAll('p'));
    if (!nodes.length) nodes.push(el);
    nodes.forEach((node, paragraphIndex) => {
      const computed = getComputedStyle(node);
      const text = clean(node.innerText);
      const nodeRect = node.getBoundingClientRect();
      paragraphs.push({
        cellIndex,
        paragraphIndex,
        cellPath: (() => {
          try { return String(angular.element(el).scope().cellPath || ''); } catch (err) { return ''; }
        })(),
        text,
        textLength: text.length,
        fontSize: px(computed.fontSize),
        color: computed.color || '',
        fontWeight: String(computed.fontWeight || ''),
        lineHeight: px(computed.lineHeight),
        marginBottom: px(computed.marginBottom),
        textAlign: String(computed.textAlign || ''),
        borderLeftWidth: px(computed.borderLeftWidth),
        borderLeftColor: computed.borderLeftColor || '',
        style,
        rect: {
          x: nodeRect.left || rect.left,
          y: nodeRect.top || rect.top,
          w: nodeRect.width || rect.width,
          h: nodeRect.height || rect.height,
        },
      });
    });
  }
  cells.sort((a, b) => a.rect.y - b.rect.y);
  paragraphs.sort((a, b) => a.rect.y - b.rect.y || a.paragraphIndex - b.paragraphIndex);
  return { url: location.href, title, cells, paragraphs };
}
"""

# ---- 模板目录两层缓存：包内只读基线 + 用户态可写缓存 ----
CATALOG_SCHEMA_VERSION = 1
CATALOG_TTL_DAYS = 30
CATALOG_BUNDLED_FILE = HERE / "data" / "template-catalog.json"
try:
    _CATALOG_HOME = Path.home()
except Exception:
    _CATALOG_HOME = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "")
CATALOG_RUNTIME_FILE = (
    _CATALOG_HOME / ".xiumi-assistant" / "template-catalog.json"
    if str(_CATALOG_HOME)
    else None
)
# ---- 参考排版档案：用户态优先，失败回退插件 data 目录 ----
PROFILE_DIR = (
    _CATALOG_HOME / ".xiumi-assistant" / "style-profiles"
    if str(_CATALOG_HOME)
    else None
)
PROFILE_FALLBACK_DIR = HERE / "data" / "style-profiles"
PROFILE_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{4,64}$")

DEFAULT_CONFIG: dict[str, Any] = {
    "pythonPath": "",
    "browserChannel": "msedge",
    "headless": False,
    "loginTimeoutMs": 900000,
}

XIUMI_URL = "https://xiumi.us"
LIST_URL = "https://xiumi.us/#/studio/papers"
EDITOR_URL = "https://xiumi.us/studio/v5/paper"
EDITOR_NEW_URL = "https://xiumi.us/studio/v5#/paper/for/new/cube/0"
TITLE_SELECTOR = "input[placeholder='请输入图文的标题']"
# 用户图片上传：秀米图库是公开结构，只保存 asset_id/CDN 地址，不落登录态。
IMAGE_UPLOAD_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
IMAGE_UPLOAD_MAX_BYTES = 10 * 1024 * 1024
GALLERY_SELECTOR = ".tn-tpl-item.tn-asset-image"
# 登录轮询间隔（任务要求 2-3 秒）
LOGIN_POLL_SECONDS = 2.5

# ---- 登录判定规则 ----
# 未登录时首页/编辑器右上角的登录入口文字
LOGIN_TEXTS = ["登录"]
# 登录后的工作台标志（仅作辅助，必须与「登录入口消失」同时成立）
WORKBENCH_MARKERS = ["我的图文", "图文素材", "我的秀米"]

# 风格学习用的只读 DOM 采样脚本：不点击、不输入、不保存，只读取可见元素的
# 计算样式。秀米编辑器正文是多个 contenteditable 单元格，普通网页则回退到
# article / #js_content / main / body。
STYLE_EXTRACT_JS = r"""
() => {
  const norm = (s) => (s || '').replace(/\s+/g, ' ').trim();
  const px = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : 0; };
  const visible = (el) => {
    try {
      const r = el.getBoundingClientRect();
      const s = getComputedStyle(el);
      return r.width > 1 && r.height > 1 && s.display !== 'none' &&
             s.visibility !== 'hidden' && parseFloat(s.opacity || '1') > 0.05;
    } catch (e) { return false; }
  };
  const isEditor = /\/studio\/v5|\/paper\/for\//.test(location.pathname + location.hash);
  const leafCells = Array.from(document.querySelectorAll("div[contenteditable='true']"))
    .filter(el => !el.querySelector("div[contenteditable='true']"));
  const cells = leafCells.filter(visible).map(el => {
    const r = el.getBoundingClientRect();
    const s = getComputedStyle(el);
    return {
      text: norm(el.innerText).slice(0, 600),
      fontSize: px(s.fontSize), fontWeight: s.fontWeight, lineHeight: px(s.lineHeight),
      color: s.color, backgroundColor: s.backgroundColor,
      borderTop: s.borderTopWidth + ' ' + s.borderTopStyle + ' ' + s.borderTopColor,
      borderBottom: s.borderBottomWidth + ' ' + s.borderBottomStyle + ' ' + s.borderBottomColor,
      borderRadius: px(s.borderRadius), textAlign: s.textAlign,
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height),
      cls: String(el.className || '').slice(0, 120),
      imgCount: el.querySelectorAll('img').length,
      svgCount: el.querySelectorAll('svg').length
    };
  }).filter(c => c.text || c.imgCount || c.svgCount)
    .sort((a, b) => a.y - b.y || a.x - b.x);

  const titleInput = document.querySelector("input[placeholder='请输入图文的标题']");
  const editorTitle = titleInput ? String(titleInput.value || '') : '';

  let root = null, rootKind = 'body';
  for (const sel of ['#js_content', '.rich_media_content', 'article', 'main', '[role="main"]']) {
    const el = document.querySelector(sel);
    if (el && visible(el)) { root = el; rootKind = sel; break; }
  }
  if (!root) root = document.body;

  const headings = Array.from(root.querySelectorAll('h1,h2,h3,h4,h5,h6'))
    .filter(visible).map(el => {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return {
        tag: el.tagName.toLowerCase(), text: norm(el.innerText).slice(0, 160),
        fontSize: px(s.fontSize), fontWeight: s.fontWeight, lineHeight: px(s.lineHeight),
        color: s.color, marginTop: px(s.marginTop), marginBottom: px(s.marginBottom),
        x: Math.round(r.x), y: Math.round(r.y)
      };
    }).filter(h => h.text).slice(0, 30);

  const paragraphs = Array.from(root.querySelectorAll('p, li, blockquote'))
    .filter(visible).map(el => {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return {
        text: norm(el.innerText).slice(0, 500), fontSize: px(s.fontSize),
        lineHeight: px(s.lineHeight), color: s.color,
        marginTop: px(s.marginTop), marginBottom: px(s.marginBottom),
        x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width)
      };
    }).filter(p => p.text.length >= 8).slice(0, 60);

  const scope = cells.length
    ? (leafCells[0].closest('.tn-editor-paper, .tn-editor, .tn-studio-root') || root)
    : root;
  const all = Array.from(scope.querySelectorAll('*')).slice(0, 2500);
  const colorItems = [];
  for (const el of all) {
    if (!visible(el)) continue;
    const s = getComputedStyle(el);
    const bg = s.backgroundColor;
    if (bg && bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent') {
      colorItems.push({ kind: 'bg', color: bg, weight: 1 });
    }
    if (px(s.borderTopWidth) > 0) colorItems.push({ kind: 'border', color: s.borderTopColor, weight: 1 });
    if (px(s.borderBottomWidth) > 0) colorItems.push({ kind: 'border', color: s.borderBottomColor, weight: 1 });
  }
  const radii = Array.from(new Set(all
    .filter(visible)
    .map(el => px(getComputedStyle(el).borderRadius))
    .filter(v => v > 0))).sort((a, b) => a - b).slice(0, 12);
  const bordered = all.filter(el => {
    if (!visible(el)) return false;
    const s = getComputedStyle(el);
    return px(s.borderTopWidth) > 0 || px(s.borderBottomWidth) > 0;
  }).length;
  const cards = all.filter(el => {
    if (!visible(el)) return false;
    const s = getComputedStyle(el);
    const bg = s.backgroundColor;
    return bg !== 'rgba(0, 0, 0, 0)' && bg !== 'transparent' && px(s.borderRadius) >= 4;
  }).length;
  const decor = {
    dividers: scope.querySelectorAll('hr').length,
    bordered,
    radii,
    images: scope.querySelectorAll('img').length,
    svgs: scope.querySelectorAll('svg').length,
    cards,
    imageTextBlocks: cells.filter(c => c.imgCount > 0 && c.text.length > 0).length
  };
  return {
    url: location.href, docTitle: document.title, isEditor, editorTitle,
    cells, headings, paragraphs, colorItems: colorItems.slice(0, 1500), decor, rootKind
  };
}
"""

# 模板库卡片采样：只读取可见卡片，不点击、不保存。付费模板由「去使用」等
# 购买/会员字样识别；不能用 ¥ 字样判断，因为免费模板的正文本身也可能含价格。
REFERENCE_STYLE_JS = r"""
() => {
  const norm = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const num = (v) => { const n = parseFloat(v); return Number.isFinite(n) ? n : 0; };
  const visible = (el) => {
    try {
      const s = getComputedStyle(el);
      const r = el.getBoundingClientRect();
      return s.display !== 'none' && s.visibility !== 'hidden' && r.width > 2 && r.height > 2;
    } catch (e) { return false; }
  };
  const isTextLeaf = (el) => {
    const t = norm(el.innerText);
    if (!t || t.length < 2) return false;
    if (el.children && el.children.length > 2) return false;
    return true;
  };
  const textColor = (el) => getComputedStyle(el).color || '';
  const bgColor = (el) => {
    const s = getComputedStyle(el);
    return s.backgroundColor && s.backgroundColor !== 'rgba(0, 0, 0, 0)' ? s.backgroundColor : '';
  };
  const rootCandidates = ['#js_content', '.rich_media_content', '.preview-content', '.article-content',
    '.article_content', '.content', 'article', 'main'];
  let root = null;
  for (const sel of rootCandidates) {
    const nodes = Array.from(document.querySelectorAll(sel)).filter(visible);
    if (nodes.length) {
      root = nodes.sort((a, b) => norm(b.innerText).length - norm(a.innerText).length)[0];
      if (norm(root.innerText).length > 120) break;
    }
  }
  if (!root) {
    const divs = Array.from(document.querySelectorAll('div,section,article'))
      .filter(visible).filter(el => norm(el.innerText).length > 200);
    root = divs.sort((a, b) => norm(b.innerText).length - norm(a.innerText).length)[0] || document.body;
  }
  const all = Array.from(root.querySelectorAll('h1,h2,h3,h4,h5,h6,p,li,blockquote,section,div,span,strong,b,em'))
    .filter(visible);
  const leaves = all.filter(isTextLeaf);
  const weighted = (items, getter) => {
    const pairs = items.map(el => [getter(el), Math.max(1, norm(el.innerText).length)])
      .filter(p => p[0] !== null && p[0] !== undefined && p[0] !== '');
    if (!pairs.length) return null;
    pairs.sort((a, b) => a[0] - b[0]);
    const total = pairs.reduce((s, p) => s + p[1], 0);
    let acc = 0;
    for (const [value, weight] of pairs) {
      acc += weight;
      if (acc >= total / 2) return value;
    }
    return pairs[pairs.length - 1][0];
  };
  const bodyEls = leaves.filter(el => {
    const t = norm(el.innerText);
    const s = getComputedStyle(el);
    return t.length >= 12 && num(s.fontSize) >= 10 && num(s.fontSize) <= 32;
  });
  const bodySize = weighted(bodyEls, el => num(getComputedStyle(el).fontSize)) || 0;
  const bodyLineRaw = weighted(bodyEls, el => num(getComputedStyle(el).lineHeight));
  const bodyColor = weighted(bodyEls, el => textColor(el)) || '';
  const bodyLetter = weighted(bodyEls, el => num(getComputedStyle(el).letterSpacing)) || 0;
  const bodyGap = weighted(bodyEls, el => num(getComputedStyle(el).marginBottom)) || 0;
  const bodyIndent = weighted(bodyEls, el => num(getComputedStyle(el).textIndent)) || 0;

  const titleGroups = new Map();
  for (const el of leaves) {
    const s = getComputedStyle(el);
    const size = num(s.fontSize);
    const tag = el.tagName.toLowerCase();
    const isHeadingTag = /^h[1-6]$/.test(tag);
    const weight = String(s.fontWeight || '400');
    const bold = parseInt(weight, 10) >= 600;
    if (!bodySize) continue;
    if (!isHeadingTag && !bold && size < bodySize * 1.18) continue;
    if (isHeadingTag && size < bodySize * 1.04) continue;
    if (size > 96) continue;
    const key = `${Math.round(size * 2) / 2}|${weight}`;
    const txt = norm(el.innerText).slice(0, 80);
    const old = titleGroups.get(key);
    const score = txt.length;
    if (!old || score > old.score) {
      titleGroups.set(key, {
        tag, fontSize: Math.round(size * 10) / 10, fontWeight: weight,
        lineHeight: Math.round(num(s.lineHeight) * 10) / 10 || null,
        color: s.color || '', marginTop: num(s.marginTop), marginBottom: num(s.marginBottom),
        hasDecoration: Boolean(bgColor(el) || num(s.borderTopWidth) || num(s.borderBottomWidth) ||
          num(s.borderLeftWidth) || num(s.paddingLeft) || num(s.borderRadius)),
        sample: txt, score
      });
    }
  }
  const titles = Array.from(titleGroups.values()).sort((a, b) => b.fontSize - a.fontSize).slice(0, 4)
    .map(({score, ...rest}) => rest);

  const colorCount = new Map();
  const bgCount = new Map();
  const addCount = (map, value, weight) => {
    if (!value) return;
    map.set(value, (map.get(value) || 0) + weight);
  };
  for (const el of leaves) {
    const t = norm(el.innerText);
    if (t.length < 2) continue;
    addCount(colorCount, textColor(el), Math.max(1, t.length));
  }
  for (const el of all) {
    const b = bgColor(el);
    if (!b) continue;
    const r = el.getBoundingClientRect();
    if (r.width * r.height > 2000) addCount(bgCount, b, Math.round(r.width * r.height / 1000));
  }
  const light = (c) => {
    const m = String(c).match(/rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
    if (!m) return false;
    const [r, g, b] = [+m[1], +m[2], +m[3]];
    return (r * 0.299 + g * 0.587 + b * 0.114) > 225;
  };
  const dark = (c) => {
    const m = String(c).match(/rgba?\((\d+)[,\s]+(\d+)[,\s]+(\d+)/i);
    if (!m) return false;
    const [r, g, b] = [+m[1], +m[2], +m[3]];
    return (r * 0.299 + g * 0.587 + b * 0.114) < 45;
  };
  const sortedColors = Array.from(colorCount.entries()).sort((a, b) => b[1] - a[1]);
  const sortedBg = Array.from(bgCount.entries()).sort((a, b) => b[1] - a[1]);
  const accents = sortedColors.map(x => x[0]).filter(c => !light(c) && !dark(c)).slice(0, 4);
  const text = (sortedColors.find(x => !light(x[0])) || ['rgb(51,51,51)'])[0];
  const background = (sortedBg.find(x => light(x[0])) || ['rgb(255,255,255)'])[0];

  const dividers = [];
  for (const el of all) {
    if (norm(el.innerText).length) continue;
    const s = getComputedStyle(el);
    const r = el.getBoundingClientRect();
    const isHr = el.tagName.toLowerCase() === 'hr';
    const h = Math.round(r.height * 10) / 10;
    const hasBorder = num(s.borderTopWidth) > 0 || num(s.borderBottomWidth) > 0;
    const bg = bgColor(el);
    if (!isHr && !(h > 0 && h <= 4 && (hasBorder || bg))) continue;
    dividers.push({
      style: isHr ? 'hr' : (hasBorder ? 'border' : 'background'),
      borderWidth: Math.max(num(s.borderTopWidth), num(s.borderBottomWidth), h),
      color: s.borderTopColor !== 'rgba(0, 0, 0, 0)' ? s.borderTopColor : (bg || s.color || ''),
      margin: `${s.marginTop} ${s.marginBottom}`, sample: el.outerHTML.slice(0, 160)
    });
  }
  const imgs = Array.from(root.querySelectorAll('img')).filter(visible).slice(0, 40).map(img => {
    const r = img.getBoundingClientRect();
    const s = getComputedStyle(img);
    const parent = img.parentElement;
    const ps = parent ? getComputedStyle(parent) : null;
    const cap = img.closest('figure')?.querySelector('figcaption') ||
      img.nextElementSibling;
    return {
      width: Math.round(r.width), borderRadius: num(s.borderRadius),
      align: ps ? ps.textAlign : '', captionStyle: cap ? {
        fontSize: num(getComputedStyle(cap).fontSize), color: textColor(cap),
        textAlign: getComputedStyle(cap).textAlign
      } : null
    };
  });
  const svgCount = root.querySelectorAll('svg').length;
  let badgeCount = 0;
  let numberingStyle = '';
  for (const el of leaves) {
    const s = getComputedStyle(el);
    const t = norm(el.innerText);
    if (num(s.borderRadius) >= 8 && num(s.paddingLeft) + num(s.paddingRight) > 4 && t.length <= 8) badgeCount++;
    if (!numberingStyle && /^[0-9]{1,2}[、.．]|^[一二三四五六七八九十]+[、.]/.test(t)) numberingStyle = t.slice(0, 8);
  }
  const bodySorted = bodyEls.map(el => ({ el, r: el.getBoundingClientRect() }))
    .filter(x => x.r.height > 0).sort((a, b) => a.r.y - b.r.y);
  const gaps = [];
  for (let i = 1; i < bodySorted.length; i++) {
    const gap = bodySorted[i].r.top - bodySorted[i - 1].r.bottom;
    if (gap >= 0 && gap < 80) gaps.push(gap);
  }
  const sectionBackgrounds = Array.from(new Set(sortedBg.map(x => x[0]).filter(c => !light(c)).slice(0, 4)));
  const docTitle = (() => {
    const og = document.querySelector('meta[property="og:title"]')?.content;
    const wx = norm(document.querySelector('#activity-name, .rich_media_title')?.innerText || '');
    return og || wx || document.title || '';
  })();
  return {
    docTitle,
    author: (document.querySelector('meta[name="author"]')?.content || '') ||
      norm(document.querySelector('.rich_media_meta_nickname, [class*="author"]')?.innerText || '').slice(0, 60),
    charCount: norm(root.innerText).length,
    imageCount: imgs.length,
    title: titles,
    body: {
      fontSize: Math.round(bodySize * 10) / 10, lineHeight: Math.round(bodyLineRaw * 10) / 10 || null,
      color: bodyColor, letterSpacing: Math.round(bodyLetter * 10) / 10,
      paragraphGap: Math.round(bodyGap * 10) / 10, firstLineIndent: Math.round(bodyIndent * 10) / 10
    },
    colors: { primary: accents[0] || '', accents, background, text },
    dividers: dividers.slice(0, 6),
    images: imgs.length ? {
      maxWidth: Math.max(...imgs.map(x => x.width || 0)),
      borderRadius: Math.max(...imgs.map(x => x.borderRadius || 0)),
      align: imgs[0].align || '', captionStyle: imgs[0].captionStyle
    } : null,
    decorations: { svgCount, badgeCount, numberingStyle, quoteStyle: '' },
    layout: { cellGap: gaps.length ? Math.round((gaps.reduce((a, b) => a + b, 0) / gaps.length) * 10) / 10 : null,
      sectionBackgrounds }
  };
}
"""

TEMPLATE_CARDS_JS = r"""
() => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const root = document.querySelector('.tn-dialog-template-comps-browser');
  if (!root) {
    return { url: location.href, title: document.title, cards: [], searchValue: '' };
  }
  const cards = Array.from(root.querySelectorAll('.tn-tpl-item.tn-lighting-box')).map((el, domIndex) => {
    const r = el.getBoundingClientRect();
    const rawTitle = el.getAttribute('title') || '';
    const matched = rawTitle.match(/^\[(\d+)\]\s*(.*)$/);
    const text = clean(el.innerText || '');
    const img = el.querySelector('img');
    const paid = /(去使用|立即购买|开通会员|VIP|付费模板|会员专享)/.test(text);
    const tags = matched ? matched[2].split(/\s+/).filter(Boolean) : [];
    return {
      domIndex, index: domIndex, id: matched ? matched[1] : '', title: matched ? (matched[2] || text.slice(0, 80)) : text.slice(0, 80),
      rawTitle, tags: tags.slice(0, 20), preview: text.slice(0, 300), paid, free: !paid,
      thumbnail: img ? String(img.currentSrc || img.src || '') : '',
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
    };
  }).filter(card => card.w > 80 && card.h > 30)
    .map((card, index) => ({ ...card, index }));
  const search = root.querySelector("input[placeholder*='搜索']");
  return {
    url: location.href, title: document.title, cards,
    searchValue: search ? String(search.value || '') : '',
  };
}
"""

# 正文 cell 指纹采样：只读所有叶子 contenteditable 的模型 text/style，用于插入组件前后对比。
CELLS_STATE_JS = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const cells = Array.from(document.querySelectorAll(".tn-working-pallet div[contenteditable='true']"))
    .filter(el => !el.querySelector("div[contenteditable='true']"))
    .map((el, order) => {
      const r = el.getBoundingClientRect();
      let scope = null;
      let cell = null;
      try {
        scope = window.angular ? angular.element(el).scope() : null;
        cell = scope && scope.cell ? scope.cell : null;
      } catch (e) {
        scope = null;
        cell = null;
      }
      const html = cell ? String(cell.text || '') : String(el.innerHTML || '');
      const plain = cell
        ? clean(String(html).replace(/<[^>]*>/g, ' ').replace(/&nbsp;/g, ' '))
        : clean(el.innerText || '');
      const style = cell && cell.style && typeof cell.style === 'object' ? cell.style : {};
      return {
        order,
        path: scope && scope.cellPath ? String(scope.cellPath) : '',
        type: cell ? String(cell.type || '') : '',
        text: html,
        plain,
        style: JSON.parse(JSON.stringify(style)),
        x: Math.round(r.x), y: Math.round(r.y),
        w: Math.round(r.width), h: Math.round(r.height),
        visible: r.width > 20 && r.height > 0
      };
    })
    .filter(item => item.visible);
  return { url: location.href, cells, count: cells.length };
}
"""

# 组件/分割线等不一定有 contenteditable：用编辑器内的全部 .tn-cell 做插入前后计数，
# 供 component_insert 识别“无文字组件”，同时保留叶子 contenteditable 的精确填充目标。
COMPONENT_CELLS_JS = r"""
() => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const root = document.querySelector('.tn-working-pallet') || document.body;
  const cells = [];
  const seen = new Set();
  for (const el of root.querySelectorAll('.tn-cell')) {
    if (!(el instanceof HTMLElement) || seen.has(el)) continue;
    seen.add(el);
    const r = el.getBoundingClientRect();
    if (r.width < 20 || r.height < 5 || r.x < 300) continue;
    let editable = el.matches("div[contenteditable='true']")
      ? el
      : el.querySelector("div[contenteditable='true']");
    let scope = null;
    let cell = null;
    try {
      scope = window.angular ? angular.element(editable || el).scope() : null;
      cell = scope && scope.cell ? scope.cell : null;
    } catch (e) { scope = null; cell = null; }
    cells.push({
      order: cells.length,
      cls: String(el.className || ''),
      type: cell ? String(cell.type || '') : '',
      path: scope && scope.cellPath ? String(scope.cellPath) : '',
      text: clean(el.innerText || '').slice(0, 120),
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height)
    });
  }
  return {url: location.href, cells, count: cells.length};
}
"""

# 只改 Angular 模型里的 cell.text，不重建 cell、不碰 style；每次调用重新按 path 定位。
COMPONENT_SET_TEXT_JS = r"""
([path, fallbackIndex, html, expected]) => {
  const clean = (value) => String(value || '').replace(/\s+/g, ' ').trim();
  const cells = Array.from(document.querySelectorAll(".tn-working-pallet div[contenteditable='true']"))
    .filter(el => !el.querySelector("div[contenteditable='true']"))
    .map((el) => {
      let scope = null;
      try { scope = window.angular ? angular.element(el).scope() : null; } catch (e) { scope = null; }
      return {el, scope, cell: scope && scope.cell ? scope.cell : null,
        path: scope && scope.cellPath ? String(scope.cellPath) : ''};
    })
    .filter(item => item.cell);
  let target = path ? cells.find(item => item.path === path) : null;
  if (!path && !target && Number.isInteger(fallbackIndex) && fallbackIndex >= 0) {
    target = cells[fallbackIndex] || null;
  }
  if (!target) return {ok: false, error: '未找到目标 cell（可能已被重组）'};
  const before = clean(String(target.cell.text || '').replace(/<[^>]*>/g, ' '));
  if (expected && before !== expected) {
    return {ok: false, error: '目标 cell 文本已变化', expected, actual: before};
  }
  try {
    target.cell.text = html;
    target.scope.$apply();
  } catch (e) {
    return {ok: false, error: String(e && e.message ? e.message : e)};
  }
  return {ok: true, before, actual: clean(String(target.cell.text || '').replace(/<[^>]*>/g, ' '))};
}
"""

# 素材库分类采样：只读左侧「图片素材」面板里的可见一级分类。
ASSET_CATEGORIES_JS = r"""
() => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const seen = new Set();
  const categories = [];
  for (const el of document.querySelectorAll('.tn-tpl-tag-lv1-child')) {
    const r = el.getBoundingClientRect();
    const text = clean(el.innerText || '');
    if (!text || r.width < 2 || r.height < 2 || seen.has(text)) continue;
    seen.add(text);
    categories.push({ name: text, active: /(^|\s)active(\s|$)/.test(String(el.className || '')) });
  }
  return { url: location.href, categories };
}
"""

# 素材库卡片采样：素材条目是 .tn-tpl-item.tn-lighting-box.tn-tpl-material。
ASSET_CARDS_JS = r"""
() => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const cards = Array.from(document.querySelectorAll('.tn-tpl-item.tn-lighting-box.tn-tpl-material')).map((el, domIndex) => {
    const r = el.getBoundingClientRect();
    const img = el.querySelector('img');
    const text = clean(el.innerText || '');
    const title = clean(el.getAttribute('title') || '');
    const alt = img ? clean(img.getAttribute('alt') || '') : '';
    const paid = /(去使用|立即购买|开通会员|VIP|付费素材|会员专享)/.test(text + ' ' + title);
    return {
      domIndex, index: domIndex, title: title || alt || ('素材 ' + (domIndex + 1)), alt, paid, free: !paid,
      thumbnail: img ? String(img.currentSrc || img.src || '') : '',
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
    };
  }).filter(card => card.w > 40 && card.h > 30)
    .map((card, index) => ({ ...card, index }));
  return { url: location.href, title: document.title, cards };
}
"""

# 我的图库：只读取文件公开信息（名称、大小、asset_id、CDN 地址），不接触登录态。
GALLERY_CARDS_JS = r"""
() => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const body = clean(document.body ? document.body.innerText : '');
  const capacityMatch = body.match(/容量[：:]\s*(\d+)\s*\/\s*(\d+)/);
  const cards = Array.from(document.querySelectorAll('.tn-tpl-item.tn-asset-image')).map((el, domIndex) => {
    const r = el.getBoundingClientRect();
    const img = el.querySelector('img.asset-image-inst') || el.querySelector('img');
    const ir = img ? img.getBoundingClientRect() : r;
    let model = null;
    try {
      const scope = angular.element(el).scope();
      model = scope && scope.img ? scope.img : null;
    } catch (e) { model = null; }
    const targetUri = model && model.target_uri ? String(model.target_uri) : '';
    const src = targetUri
      ? (targetUri.indexOf('//') === 0 ? 'https:' + targetUri : targetUri)
      : (img ? String(img.currentSrc || img.src || '') : '');
    return {
      domIndex,
      index: domIndex,
      id: model && model.asset_id ? String(model.asset_id) : '',
      name: model && model.display_name ? String(model.display_name) : clean(el.innerText || ''),
      size: model && typeof model.file_size === 'number' ? model.file_size : null,
      createdAt: model && (model.created_at || model.updated_at) ? String(model.created_at || model.updated_at) : '',
      url: src,
      thumbnail: src,
      x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)
    };
  }).filter(card => card.w > 40 && card.h > 30)
    .map((card, index) => ({ ...card, index }));
  return {
    url: location.href,
    title: document.title,
    capacity: capacityMatch ? { used: Number(capacityMatch[1]), limit: Number(capacityMatch[2]) } : null,
    cards,
    hasUpload: !!document.querySelector("input[type=file][accept*='image']")
  };
}
"""

# 秀米账号等级限制：只读用户公开的 levelLimit，不读取/不保存任何登录凭据。
QUOTA_INFO_JS = r"""
async () => {
  const resp = await fetch('/api/user/info?include=messageCount&include=userSetting', {
    credentials: 'same-origin',
    headers: { 'Accept': 'application/json' }
  });
  if (!resp.ok) throw new Error('user/info HTTP ' + resp.status);
  const data = await resp.json();
  const user = data && data.data && data.data.user ? data.data.user : null;
  const limits = user && user.levelLimit ? user.levelLimit : null;
  return {
    level: user ? user.level || null : null,
    limits: limits ? {
      articleLimit: Number(limits.articleLimit || 0),
      imageLimit: Number(limits.upImageNumLimit || 0),
      fragmentLimit: Number(limits.fragmentLimit || 0)
    } : null
  };
}
"""

# 「我的图文」当前使用量：只数当前列表卡片，不依赖“已到上限”提示是否出现。
QUOTA_ARTICLE_COUNT_JS = r"""
() => {
  const seen = new Set();
  let count = 0;
  for (const card of document.querySelectorAll('.show-item')) {
    const link = card.querySelector("a[href*='/paper/for/']");
    if (!link) continue;
    const url = link.href;
    if (!url || seen.has(url)) continue;
    seen.add(url);
    count += 1;
  }
  return count;
}
"""

# 编辑器插入/套用后的轻量状态，用于确认操作是否真的落到画布上。
EDITOR_STATS_JS = r"""
() => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const body = clean(document.body ? document.body.innerText : '');
  return {
    editableCount: document.querySelectorAll("div[contenteditable='true']").length,
    imageCells: document.querySelectorAll('.tn-cell-image').length,
    sectionImages: document.querySelectorAll('section img').length,
    hasComponentToolbar: /复制[\s\S]{0,30}收藏[\s\S]{0,30}变换组件/.test(body)
  };
}
"""

# 把编辑器中可见的第一个文本 cell 复制成多段：每段一个独立 tn-cell，
# 保留秀米自己的 cell 模型（不与单个 cell 内的 <p> 混写）。
COMPOSE_RESET_JS = r"""
(payload) => {
  try {
    const segments = payload && payload.segments;
    const options = payload || {};
    const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
    const all = Array.from(document.querySelectorAll("div[contenteditable='true']"))
      .filter(el => !el.querySelector("div[contenteditable='true']"))
      .map(el => ({ el, r: el.getBoundingClientRect() }))
      .filter(x => x.r.width > 120 && x.r.height > 0)
      .sort((a, b) => a.r.y - b.r.y || a.r.x - b.r.x);
    if (!all.length) return { ok: false, error: '未找到可见正文 cell' };
    const scope = angular.element(all[0].el).scope();
    const parentCell = scope && scope.parentCell;
    if (!parentCell || !Array.isArray(parentCell.items)) {
      return { ok: false, error: '正文 cell 没有 parentCell.items' };
    }
    let index = parentCell.items.findIndex(item => item && item.txt1);
    if (index < 0) return { ok: false, error: 'parentCell.items 中没有 txt1' };
    const base = parentCell.items[index];
    const layoutOnly = !!(options && options.layoutOnly);
    const preserveImages = layoutOnly || !!(options && options.preserveImages);
    const preservedImages = [];
    if (preserveImages) {
      let textBefore = 0;
      for (const item of parentCell.items) {
        if (!item) continue;
        if (item.txt1) {
          textBefore += 1;
        } else if (item.img1) {
          preservedImages.push({ afterText: textBefore, item: angular.copy(item) });
        }
      }
    }
    const segmentsToUse = Array.isArray(segments) && segments.length ? segments : ['<p><br></p>'];
    if (options.singleCell) {
      // 秀米的保存序列化只认原生编辑流程创建出来的 comp；直接复制 item 虽然能
      // 在 DOM 中显示多段，但保存后只保留第一段。为保证内容不丢，create 使用
      // 单个文本 cell 承载全部段落，图片以 <img> 内联在锚点段落之后。
      base.txt1.text = segmentsToUse.join('');
      if (scope.cell && typeof scope.cell === 'object') {
        scope.cell.text = base.txt1.text;
      }
      try { all[0].el.innerHTML = base.txt1.text; } catch (err) {}
      try { scope.$apply(); } catch (err) {
        if (!/digest/i.test(String(err && err.message || ''))) throw err;
      }
      try {
        all[0].el.dispatchEvent(new InputEvent('input', {bubbles: true, inputType: 'insertText', data: ''}));
        all[0].el.dispatchEvent(new Event('change', {bubbles: true}));
        all[0].el.blur();
      } catch (err) {}
      return { ok: true, count: segmentsToUse.length, itemCount: 1, singleCell: true };
    }
    parentCell.items.splice(0, parentCell.items.length);
    base.txt1.text = String(segmentsToUse[0] || '<p><br></p>');
    parentCell.items.push(base);
    for (let i = 1; i < segmentsToUse.length; i += 1) {
      const copy = angular.copy(base);
      // 复制会带上原 item 的 $$hashKey 和 _comp._$uuid；重复标识会在保存/图片
      // 插入的下一次 digest 被当成同一项，导致只剩第一段。必须重新分配。
      try { delete copy.$$hashKey; } catch (err) { copy.$$hashKey = undefined; }
      if (copy._comp) {
        try {
          copy._comp._$uuid = 't14-' + Date.now().toString(36) + '-' + i + '-' + Math.random().toString(36).slice(2, 8);
        } catch (err) {}
      }
      copy.txt1.text = String(segmentsToUse[i] || '<p><br></p>');
      parentCell.items.push(copy);
      // 秀米的保存序列化读取的是 comp 实例注册表，而不只是 ngRepeat 的 items。
      // 新复制的 item 必须注册到父 comp，否则 DOM 能显示但保存时会被丢掉。
      try {
        const parentComp = scope.parentComp || (scope.$parent && scope.$parent.parentComp);
        if (parentComp && parentComp.tnComp && typeof parentComp.tnComp.addComp === 'function' && copy._comp) {
          parentComp.tnComp.addComp(copy._comp);
        }
      } catch (err) {}
    }
    const imagePositions = Array.isArray(options && options.imagePositions)
      ? options.imagePositions : [];
    for (let i = 0; i < preservedImages.length; i += 1) {
      const image = preservedImages[i];
      const desired = i < imagePositions.length ? Number(imagePositions[i]) : image.afterText;
      const at = Math.max(0, Math.min(parentCell.items.length, desired));
      parentCell.items.splice(at, 0, image.item);
    }
    try { scope.$apply(); } catch (err) {
      if (!/digest/i.test(String(err && err.message || ''))) throw err;
    }
    return {
      ok: true,
      count: segmentsToUse.length,
      itemCount: parentCell.items.length,
      preservedImages: preservedImages.length
    };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
}
"""

# 用可见文本 cell 的序号定位；图片插入需要先把光标放进文本 cell。
COMPOSE_SELECT_CELL_JS = r"""
(index) => {
  try {
    const all = Array.from(document.querySelectorAll("div[contenteditable='true']"))
      .filter(el => !el.querySelector("div[contenteditable='true']"))
      .map(el => ({ el, r: el.getBoundingClientRect() }))
      .filter(x => x.r.width > 120 && x.r.height > 0)
      .sort((a, b) => a.r.y - b.r.y || a.r.x - b.r.x);
    const target = all[Math.max(0, Number(index) || 0)];
    if (!target) return { ok: false, count: all.length };
    target.el.scrollIntoView({ block: 'center', inline: 'nearest' });
    target.el.focus();
    try {
      const sel = window.getSelection();
      const range = document.createRange();
      range.selectNodeContents(target.el);
      range.collapse(false);
      sel.removeAllRanges();
      sel.addRange(range);
    } catch (err) {}
    const r = target.el.getBoundingClientRect();
    return {
      ok: true,
      count: all.length,
      x: Math.round(r.x), y: Math.round(r.y),
      w: Math.round(r.width), h: Math.round(r.height)
    };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
}
"""

# 原生创建完文本 cell 后，把每一格的内容/样式写入秀米 cell 模型。
# t25 已验证 cell.style 是唯一可靠的样式持久化通道；text 只保留纯 <p>。
# payload: [{ text?: string|null, style?: object }] 或 { items: [...] }
COMPOSE_CLEAN_EMPTY_JS = r"""
(payload) => {
  try {
    const includeAllEmpty = !!(payload && payload.includeAllEmpty);
    const onlyPaths = (payload && Array.isArray(payload.onlyPaths))
      ? new Set(payload.onlyPaths.map(value => String(value || '')).filter(Boolean))
      : null;
    const cells = Array.from(document.querySelectorAll("div[contenteditable='true']"))
      .filter(el => !el.querySelector("div[contenteditable='true']"))
      .map(el => ({ el, scope: angular.element(el).scope() }))
      .filter(x => x.scope && x.scope.cell && x.scope.cell.type === 'text');
    if (cells.length <= 1) return { removed: 0 };
    const empty = cells.filter(item => {
      const text = String(item.el.innerText || '').replace(/\u200b/g, '').trim();
      if (text) return false;
      if (onlyPaths && !onlyPaths.has(String(item.scope.cellPath || ''))) return false;
      if (includeAllEmpty) return true;
      return String(item.scope.cellPath || '').indexOf('._qiBlock:') >= 0;
    });
    // 统一清理函数也不能把正文删空；始终保留至少一个文本 cell。
    if (!empty.length || empty.length >= cells.length) return { removed: 0 };
    let removed = 0;
    for (const item of empty) {
      const parentScope = item.scope.$parent;
      const parentCell = parentScope && parentScope.parentCell;
      if (!parentCell || !Array.isArray(parentCell.items)) continue;
      const key = item.scope.cellKey;
      let index = -1;
      if (key) {
        index = parentCell.items.findIndex(entry => entry && entry[key] === item.scope.cell);
        if (index < 0) {
          const targetText = String(item.scope.cell.text || '');
          index = parentCell.items.findIndex(
            entry => entry && entry[key] && String(entry[key].text || '') === targetText
          );
        }
      }
      if (index < 0) index = parentCell.items.length - 1;
      if (index >= 0) {
        parentCell.items.splice(index, 1);
        removed += 1;
      }
    }
    if (removed) {
      try {
        if (cells[0] && cells[0].scope) cells[0].scope.$apply();
      } catch (err) {}
    }
    return { removed };
  } catch (err) {
    return { removed: 0, error: String(err && err.message || err) };
  }
}
"""

UPDATE_BODY_CELLS_JS = r"""
(payload) => {
  try {
    const items = (payload && Array.isArray(payload.items)) ? payload.items : [];
    const all = Array.from(document.querySelectorAll("div[contenteditable='true']"))
      .filter(el => !el.querySelector("div[contenteditable='true']"))
      .map(el => ({ el, scope: angular.element(el).scope(), r: el.getBoundingClientRect() }))
      .filter(x => x.scope && x.scope.cell && x.scope.cell.type === 'text'
        && x.r.width > 120 && x.r.height > 0)
      .sort((a, b) => a.r.y - b.r.y || a.r.x - b.r.x);
    if (!all.length) return { ok: false, error: '未找到正文文本 cell' };
    const applied = [];
    for (let i = 0; i < items.length && i < all.length; i++) {
      const item = items[i] || {};
      const scope = all[i].scope;
      scope.cell.text = item.text || '<p><br></p>';
      const next = Object.assign({}, item.style || {});
      if (!next.textAlign) {
        next.textAlign = (scope.cell.style && scope.cell.style.textAlign) || 'justify';
      }
      scope.cell.style = next;
      applied.push(i);
    }
    // 多余文本 cell 不在这里各自 splice，清空后交给统一的 COMPOSE_CLEAN_EMPTY_JS 删除。
    let cleared = 0;
    for (let i = items.length; i < all.length; i++) {
      const scope = all[i].scope;
      if (scope.cell.text !== '<p><br></p>' || Object.keys(scope.cell.style || {}).length) {
        scope.cell.text = '<p><br></p>';
        scope.cell.style = {};
        cleared += 1;
      }
    }
    // 所有模型修改后只触发一次 digest，避免循环中 DOM 重排导致后续 scope 错位。
    try {
      if (all[0] && all[0].scope) all[0].scope.$apply();
    } catch (err) {}
    return {
      ok: true,
      applied: applied.length,
      removed: 0,
      cleared,
      cellCount: all.length,
      missing: Math.max(0, items.length - all.length),
    };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
}
"""

COMPOSE_SET_CELLS_JS = r"""
(payload) => {
  try {
    const all = Array.from(document.querySelectorAll(".tn-working-pallet div[contenteditable='true']"))
      .filter(el => !el.querySelector("div[contenteditable='true']"))
      .map(el => ({ el, r: el.getBoundingClientRect() }))
      .filter(x => x.r.width > 120 && x.r.height > 0)
      .sort((a, b) => a.r.y - b.r.y || a.r.x - b.r.x);
    const raw = Array.isArray(payload) ? payload.map(text => ({ text }))
      : (payload && Array.isArray(payload.items) ? payload.items : []);
    const startIndex = payload && payload.startIndex !== undefined && payload.startIndex !== null
      ? Math.max(0, parseInt(payload.startIndex, 10) || 0) : 0;
    const paths = payload && Array.isArray(payload.paths) ? payload.paths : [];
    const findElement = (path) => {
      if (!path) return null;
      for (const item of all) {
        try {
          const sc = angular.element(item.el).scope();
          if (sc && String(sc.cellPath || '') === String(path)) return item.el;
        } catch (e) { /* ignore */ }
      }
      return null;
    };
    const allowed = new Set(['fontSize','color','fontWeight','fontStyle','lineHeight',
      'letterSpacing','textAlign','textIndent','backgroundColor','marginTop','marginRight',
      'marginBottom','marginLeft','paddingTop','paddingRight','paddingBottom','paddingLeft',
      'borderLeft','borderRight','borderTop','borderBottom']);
    if (payload && payload.requireExactCount && all.length !== raw.length) {
      return {ok: false, error: '文本格数量与分块不一致；请 inspect 后明确逐格排版。'};
    }
    // Resolve and validate every target before the first model change. An
    // explicit missing path must never fall back to an unrelated cell index.
    const plan = [];
    const used = new Set();
    for (let i = 0; i < raw.length; i++) {
      const el = paths[i] ? findElement(paths[i]) : (all[startIndex + i] || {}).el;
      if (!el || used.has(el)) return {ok: false, error: '文本格缺失或目标重复，未写入。'};
      used.add(el);
      let scope = angular.element(el).scope();
      if (!scope || !scope.cell) {
        const host = el.closest('.tn-cell') || el.parentElement;
        if (host) scope = angular.element(host).scope();
      }
      if (!scope || !scope.cell || (scope.cell.type && scope.cell.type !== 'text')) {
        return {ok: false, error: '目标不是可写文本格，未写入。'};
      }
      const item = raw[i] || {};
      const norm = value => String(value || '').normalize('NFC').replace(/\s+/g, '');
      if (item.expectedText !== undefined && norm(el.innerText) !== norm(item.expectedText)) {
        return {ok: false, error: '分块与目标格内容不对应，未写入；请 inspect 后逐格排版。'};
      }
      plan.push({scope, item, index: startIndex + i});
    }
    const applied = [];
    for (const {scope, item, index} of plan) {
      if (item.text !== undefined && item.text !== null) {
        scope.cell.text = String(item.text || '<p><br></p>');
      }
      if (item.style && typeof item.style === 'object') {
        scope.cell.style = scope.cell.style || {};
        for (const key of Object.keys(item.style)) {
          if (!allowed.has(key)) continue;
          const value = item.style[key];
          if (value === null || value === undefined || value === '') delete scope.cell.style[key];
          else scope.cell.style[key] = String(value);
        }
      }
      scope.$applyAsync ? scope.$applyAsync() : scope.$apply();
      applied.push(index);
    }
    return { ok: true, cellCount: all.length, applied, skipped: [] };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
}
"""

# 在「已打开的文章」里就地增删改正文。
#
# 这段是 t25/t7 同一条通道的延伸：内容与样式都必须写进 Angular 模型
# （scope.cell.text / scope.cell.style），不能改 DOM 的 innerHTML —— 改了只是
# 当下好看，保存后样式会整段丢失（已用对照实验确认）。
#
# 多格正文替换必须明确选中范围，不按文字长短猜正文或清空未选组件。
ARTICLE_EDIT_JS = r"""
(payload) => {
  try {
    const sel = "div[contenteditable='true']";
    const all = Array.from(document.querySelectorAll(".tn-working-pallet " + sel))
      .filter(el => !el.querySelector(sel))
      .map(el => ({ el, r: el.getBoundingClientRect() }))
      .filter(x => x.r.width > 120)
      .sort((a, b) => a.r.y - b.r.y || a.r.x - b.r.x);
    const getScope = (el) => {
      let sc = null;
      try { sc = angular.element(el).scope(); } catch (e) { sc = null; }
      if (!sc || !sc.cell) {
        const host = el.closest('.tn-cell');
        if (host) { try { sc = angular.element(host).scope(); } catch (e) { sc = null; } }
      }
      if (!sc || !sc.cell) return null;
      if (sc.cell.type && sc.cell.type !== 'text') return null;
      return sc;
    };
    const texts = [];
    for (const item of all) {
      const sc = getScope(item.el);
      if (sc) texts.push({ el: item.el, sc });
    }
    if (!texts.length) return { ok: false, reason: '当前文章里没有可编辑的正文文本 cell。' };
    const total = texts.length;
    const applyScope = (sc) => {
      try { sc.$applyAsync ? sc.$applyAsync() : sc.$apply(); } catch (e) { /* ignore */ }
    };
    const setContent = (entry, html, text) => {
      const sc = entry.sc;
      if (html !== undefined && html !== null) sc.cell.text = String(html);
      else if (text !== undefined && text !== null) {
        const safe = String(text)
          .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
        sc.cell.text = '<p>' + safe + '</p>';
      }
      if (payload.style && typeof payload.style === 'object') {
        sc.cell.style = sc.cell.style || {};
        for (const key of Object.keys(payload.style)) {
          const value = payload.style[key];
          if (value === null || value === undefined || value === '') delete sc.cell.style[key];
          else sc.cell.style[key] = String(value);
        }
      }
      applyScope(sc);
    };
    const readOut = () => texts.map((t, i) => {
      let plain = '';
      try { plain = String(t.el.innerText || '').replace(/\s+/g, ' ').trim(); } catch (e) { plain = ''; }
      return { index: i, path: String(t.sc.cellPath || ''), text: plain.slice(0, 160), html: String(t.sc.cell.text || ''), style: t.sc.cell.style || {} };
    });

    const action = String(payload.action || 'replace_body');
    if (action !== 'inspect' && Array.isArray(payload.expectedCells)) {
      const sorted = value => Array.isArray(value) ? value.map(sorted) :
        value && typeof value === 'object' ? Object.fromEntries(Object.keys(value).sort().map(k => [k, sorted(value[k])])) : value;
      const stable = value => JSON.stringify(sorted(value));
      if (payload.expectedCells.length !== texts.length || payload.expectedCells.some((item, i) =>
        String(item.path || '') !== String(texts[i].sc.cellPath || '') ||
        String(item.html || '') !== String(texts[i].sc.cell.text || '') ||
        stable(item.style) !== stable(texts[i].sc.cell.style))) {
        return {ok: false, reason: '单元格已变化，请重新 inspect；未执行修改。'};
      }
    }
    if (action === 'inspect') {
      return { ok: true, action, cellCount: total, cells: readOut() };
    }
    if (action === 'replace_body') {
      const html = payload.html;
      if (!html || !String(html).trim()) return { ok: false, reason: 'replace_body 需要非空 html。' };
      // A multi-cell document has no reliable implicit distinction between
      // body text and component titles. Only explicitly inspected cells change.
      const indexes = Array.isArray(payload.cellIndexes) ? payload.cellIndexes : (total === 1 ? [0] : []);
      if (!indexes.length || new Set(indexes).size !== indexes.length || indexes.some(i => !Number.isInteger(i) || i < 0 || i >= total)) {
        return {ok: false, reason: '多格正文必须先 inspect，再明确提供 cellIndexes；不会按字数猜测正文。'};
      }
      const host = indexes[0];
      setContent(texts[host], html, null);
      let cleared = 0;
      for (const i of indexes) {
        if (i === host) continue;
        setContent(texts[i], '<p><br></p>', null);
        cleared += 1;
      }
      return { ok: true, action, cellCount: total, host, cleared, cells: readOut() };
    }
    if (action === 'set_cell') {
      const idx = parseInt(payload.cellIndex, 10);
      if (!(idx >= 0 && idx < total)) {
        return { ok: false, reason: 'cellIndex 越界，当前正文 cell 数=' + total };
      }
      setContent(texts[idx], payload.html, payload.text);
      return { ok: true, action, index: idx, cellCount: total, cells: readOut() };
    }
    if (action === 'clear_cell') {
      const idx = parseInt(payload.cellIndex, 10);
      if (!(idx >= 0 && idx < total)) {
        return { ok: false, reason: 'cellIndex 越界，当前正文 cell 数=' + total };
      }
      setContent(texts[idx], '<p><br></p>', null);
      return { ok: true, action, index: idx, cellCount: total, cells: readOut() };
    }
    if (action === 'append_cell') {
      const idx = total - 1;
      const tail = String(texts[idx].el.innerText || '').trim();
      if (tail) {
        return {
          ok: false,
          reason: '文章末尾没有可复用的空正文 cell；要追加新格请用 xiumi_write（新建稿路径）。',
          cellCount: total,
        };
      }
      setContent(texts[idx], payload.html, payload.text);
      return { ok: true, action, index: idx, cellCount: total, cells: readOut() };
    }
    return { ok: false, reason: '不支持的 action：' + action };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
}
"""

# 把整篇带内联样式的 HTML 一次写入末尾的空白正文 cell。组件页里不能按
# “第 N 个文本格”逐段找，否则组件标题会被覆盖或产生重复 cell。
WRITE_HTML_SINGLE_CELL_JS = r"""
(payload) => {
  try {
    const html = String((payload && payload.html) || '');
    if (!html.trim()) return { ok: false, reason: '正文 HTML 为空。' };
    const sel = "div[contenteditable='true']";
    const all = Array.from(document.querySelectorAll(".tn-working-pallet " + sel))
      .filter(el => !el.querySelector(sel))
      .map(el => ({ el, r: el.getBoundingClientRect() }))
      .filter(x => x.r.width > 120 && x.r.height > 0)
      .sort((a, b) => a.r.y - b.r.y || a.r.x - b.r.x);
    const candidates = [];
    for (const item of all) {
      let sc = null;
      try { sc = angular.element(item.el).scope(); } catch (e) { sc = null; }
      if (!sc || !sc.cell) {
        const host = item.el.closest('.tn-cell');
        if (host) { try { sc = angular.element(host).scope(); } catch (e) { sc = null; } }
      }
      if (!sc || !sc.cell || (sc.cell.type && sc.cell.type !== 'text')) continue;
      candidates.push({ el: item.el, sc, plain: String(item.el.innerText || '').replace(/\s+/g, ' ').trim() });
    }
    if (!candidates.length) return { ok: false, reason: '当前文章里没有可写入的正文文本 cell。' };
    // 组件内部的空 contenteditable 也是「候选且为空」，但它属于组件槽位，
    // 写进去会毁掉组件。秀米把正文块放在 .tn-quick-input-block 里，组件没有
    // 这层祖先，所以用「是否位于快速输入块」判定正文，而不是靠 class 前缀。
    const isBodyBlock = el => {
      try { return !!(el.closest && el.closest('.tn-quick-input-block, .quick-input')); } catch (e) { return false; }
    };
    const pool = candidates.filter(item => isBodyBlock(item.el));
    if (!pool.length) return { ok: false, reason: '当前文章里没有可写入的正文文本 cell。' };
    let host = null;
    for (let i = pool.length - 1; i >= 0; i--) {
      if (pool[i].plain) break;          // 末尾还有正文 -> 没有可复用的末尾空白格
      if (!host) host = pool[i];
    }
    if (!host) {
      return { ok: false, reason: '当前文章末尾没有空白正文 cell；拒绝覆盖已有正文或组件标题。' };
    }
    host.sc.cell.text = html;
    try { host.sc.$applyAsync ? host.sc.$applyAsync() : host.sc.$apply(); } catch (e) {}
    return {
      ok: true,
      host: candidates.indexOf(host),
      cellCount: candidates.length,
      path: String(host.sc.cellPath || ''),
      htmlLength: html.length,
    };
  } catch (err) {
    return { ok: false, error: String(err && err.message || err) };
  }
}
"""

# 模板库目录采样：只读取分组/分类名与搜索提示，不接触模板卡片，不保存。
# 分类块在 DOM 中全部存在（打开「更多模板」即可读到），无需逐个点击分组。
TEMPLATE_CATALOG_JS = r"""
() => {
  const clean = (s) => String(s || '').replace(/\s+/g, ' ').trim();
  const root = document.querySelector('.tn-dialog-template-comps-browser');
  if (!root) {
    return { ok: false, url: location.href, title: document.title, groups: [] };
  }
  const groups = [];
  const seen = new Set();
  for (const block of root.querySelectorAll('.tn-tpl-tag-lv1')) {
    const nameEl = block.querySelector('.tn-tpl-tag-lv1-name');
    const name = clean(nameEl ? nameEl.innerText : '');
    if (!name || seen.has(name)) continue;
    const categories = Array.from(new Set(
      Array.from(block.querySelectorAll('.tn-tpl-tag-lv0-name'))
        .map(el => clean(el.innerText || ''))
        .filter(Boolean)
    ));
    if (!categories.length) continue;
    seen.add(name);
    groups.push({ name, categories });
  }
  const search = root.querySelector("input[placeholder*='搜索']");
  const sortMatch = (root.innerText || '').match(/倒序[^\n]{0,20}/);
  return {
    ok: groups.length > 0,
    url: location.href,
    title: document.title,
    searchHint: search ? String(search.getAttribute('placeholder') || '') : '',
    sortOrder: sortMatch ? clean(sortMatch[0]) : '',
    tags: ['全部'],
    groups
  };
}
"""


def _prepare_runtime_env() -> None:
    """在 import playwright 之前修好 TEMP/TMP/TMPDIR（本机 TEMP 不可写）。"""
    try:
        TMP_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:  # 目录不可建时仍继续，让真正的错误在启动浏览器时暴露
        _log(f"创建临时目录失败 {TMP_DIR}: {exc}")
    for key in ("TMP", "TEMP", "TMPDIR"):
        os.environ[key] = str(TMP_DIR)


def _reconfigure_stdio() -> None:
    """Node 侧按 UTF-8 读管道，这里固定编码，避免中文被 cp936 编成乱码。"""
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    try:
        sys.stdout.reconfigure(newline="\n")
    except Exception:
        pass


def _log(message: str) -> None:
    try:
        print(f"[bridge] {message}", file=sys.stderr, flush=True)
    except Exception:
        pass


# 模块导入即执行：任何 playwright 调用都在这之后发生
_prepare_runtime_env()


def login_button_visible(page) -> bool:
    """右上角「登录」入口是否仍可见（可见 = 未登录）。"""
    for text in LOGIN_TEXTS:
        try:
            loc = page.get_by_text(text, exact=True)
            count = loc.count()
        except Exception:
            continue
        for i in range(count):
            try:
                if loc.nth(i).is_visible():
                    return True
            except Exception:
                continue
    return False


def any_marker_visible(page):
    """返回第一个可见的工作台标志文字，没有则返回 None。"""
    for marker in WORKBENCH_MARKERS:
        try:
            loc = page.get_by_text(marker, exact=False)
            count = loc.count()
        except Exception:
            continue
        for i in range(count):
            try:
                if loc.nth(i).is_visible():
                    return marker
            except Exception:
                continue
    return None


def is_logged_in(page) -> bool:
    """登录入口消失 + 工作台标志可见，才判定为已登录。"""
    if login_button_visible(page):
        return False
    return any_marker_visible(page) is not None


def _norm_color(value: Any) -> str:
    """把计算样式里的颜色归一化为 rgb(...)；透明色返回空串。"""
    raw = str(value or "").strip().lower()
    if not raw or raw in {"transparent", "none", "currentcolor"}:
        return ""
    match = re.match(r"rgba?\(([^)]+)\)", raw)
    if not match:
        return raw
    parts = [part.strip() for part in match.group(1).split(",")]
    if len(parts) >= 4:
        try:
            if float(parts[3]) <= 0.05:
                return ""
        except ValueError:
            pass
    try:
        red, green, blue = (int(round(float(part))) for part in parts[:3])
    except (TypeError, ValueError):
        return raw
    return f"rgb({red}, {green}, {blue})"


def _color_saturation(color: str) -> float:
    match = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color)
    if not match:
        return 0.0
    red, green, blue = (int(part) for part in match.groups())
    high, low = max(red, green, blue), min(red, green, blue)
    return (high - low) / high if high else 0.0


def _weighted_top(counter: Counter, count: int = 8) -> list[dict[str, Any]]:
    return [
        {"color": color, "weight": weight}
        for color, weight in counter.most_common(count)
        if color
    ]


def _weighted_median(values: list[float], weights: list[float]) -> float:
    pairs = sorted(
        ((float(value), max(float(weight), 0.0)) for value, weight in zip(values, weights)),
        key=lambda item: item[0],
    )
    total = sum(weight for _value, weight in pairs)
    if not pairs or total <= 0:
        return 0.0
    halfway = total / 2.0
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= halfway:
            return value
    return pairs[-1][0]


def _profile_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _profile_id_from_url(url: str) -> str:
    return hashlib.sha1(url.strip().lower().encode("utf-8")).hexdigest()[:8]


def _profile_dirs() -> list[Path]:
    dirs = []
    if PROFILE_DIR is not None:
        dirs.append(PROFILE_DIR)
    dirs.append(PROFILE_FALLBACK_DIR)
    return dirs


def _profile_runtime_dir() -> Path | None:
    if PROFILE_DIR is None:
        return None
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        probe = PROFILE_DIR / ".write-probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return PROFILE_DIR
    except OSError:
        return None


def _profile_clean_value(value: Any, depth: int = 0) -> Any:
    if depth > 8:
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return value[:4000]
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            if not isinstance(key, str) or key.startswith("$"):
                continue
            item = _profile_clean_value(item, depth + 1)
            if item is not None and item != "" and item != [] and item != {}:
                cleaned[key] = item
        return cleaned
    if isinstance(value, (list, tuple)):
        items = [_profile_clean_value(item, depth + 1) for item in value]
        return [item for item in items if item is not None][:80]
    return str(value)[:400]


def _profile_public(profile: dict[str, Any]) -> dict[str, Any]:
    return _profile_clean_value(profile) or {}


def _profile_analysis_to_style(profile: dict[str, Any]) -> dict[str, Any]:
    """把参考页面的采样压缩成可复用的排版规则（不是整页 CSS）。"""
    raw_titles = profile.get("title") or []
    titles = []
    for entry in raw_titles:
        if not isinstance(entry, dict):
            continue
        size = entry.get("fontSize")
        if not isinstance(size, (int, float)) or size <= 0:
            continue
        titles.append({
            "level": len(titles) + 1,
            "tag": str(entry.get("tag") or "")[:16],
            "fontSize": round(float(size), 1),
            "fontWeight": str(entry.get("fontWeight") or "400")[:8],
            "lineHeight": round(float(entry["lineHeight"]), 1) if isinstance(entry.get("lineHeight"), (int, float)) else None,
            "color": _norm_color(entry.get("color")),
            "marginTop": round(float(entry.get("marginTop") or 0), 1),
            "marginBottom": round(float(entry.get("marginBottom") or 0), 1),
            "hasDecoration": bool(entry.get("hasDecoration")),
        })
    titles.sort(key=lambda item: item["fontSize"], reverse=True)
    titles = titles[:4]

    body = profile.get("body") if isinstance(profile.get("body"), dict) else {}
    body_out = {}
    for key in ("fontSize", "lineHeight", "letterSpacing", "paragraphGap", "firstLineIndent"):
        value = body.get(key)
        if isinstance(value, (int, float)):
            body_out[key] = round(float(value), 1)
    if isinstance(body.get("color"), str):
        color = _norm_color(body.get("color"))
        if color:
            body_out["color"] = color

    colors = profile.get("colors") if isinstance(profile.get("colors"), dict) else {}
    palette = {}
    for key in ("primary", "background", "text"):
        if isinstance(colors.get(key), str):
            color = _norm_color(colors.get(key))
            if color:
                palette[key] = color
    accents = []
    for color in (colors.get("accents") or []):
        color = _norm_color(color) if isinstance(color, str) else ""
        if color and color not in accents:
            accents.append(color)
    if accents:
        palette["accents"] = accents[:4]

    dividers = []
    for divider in (profile.get("dividers") or [])[:4]:
        if not isinstance(divider, dict):
            continue
        item = {"style": str(divider.get("style") or "")[:24]}
        for key in ("borderWidth", "color"):
            if key == "color":
                value = _norm_color(divider.get(key))
            else:
                value = divider.get(key)
                value = round(float(value), 1) if isinstance(value, (int, float)) else None
            if value not in (None, ""):
                item[key] = value
        if item:
            dividers.append(item)

    images = profile.get("images") if isinstance(profile.get("images"), dict) else {}
    image_rule = {}
    if isinstance(images.get("borderRadius"), (int, float)):
        image_rule["borderRadius"] = round(float(images["borderRadius"]), 1)
    if isinstance(images.get("align"), str):
        image_rule["align"] = images["align"][:20]

    decorations = profile.get("decorations") if isinstance(profile.get("decorations"), dict) else {}
    decor_out = {}
    for key in ("svgCount", "badgeCount", "numberingStyle"):
        value = decorations.get(key)
        if value not in (None, "", 0, "0"):
            decor_out[key] = value

    missing = []
    if len(titles) < 2:
        missing.append("标题层级少于 2 级")
    if not body_out.get("fontSize") or not body_out.get("lineHeight"):
        missing.append("正文参数不足")
    if not palette.get("primary"):
        missing.append("未识别主色")
    if not dividers:
        missing.append("未识别分隔线")
    if not profile.get("imageCount"):
        missing.append("未识别正文图片")

    return {
        "titles": titles,
        "body": body_out,
        "palette": palette,
        "dividers": dividers,
        "images": image_rule,
        "decorations": decor_out,
        "missing": missing,
    }


def _profile_from_analysis(analysis: dict[str, Any], url: str) -> dict[str, Any]:
    style = _profile_analysis_to_style(analysis)
    profile = {
        "id": _profile_id_from_url(url),
        "name": "参考排版 " + _profile_id_from_url(url),
        "sourceUrl": url,
        "fetchedAt": _profile_now_iso(),
        "charCount": int(analysis.get("charCount") or 0),
        "imageCount": int(analysis.get("imageCount") or 0),
        "style": style,
        "missing": style.get("missing", []),
    }
    return _profile_public(profile)


def _profile_find_path(profile_id: str) -> Path | None:
    if not PROFILE_ID_RE.match(profile_id):
        return None
    for directory in _profile_dirs():
        path = directory / f"{profile_id}.json"
        if path.exists():
            return path
    return None


def _profile_load(profile_id: str) -> dict[str, Any]:
    profile_id = str(profile_id or "").strip()
    path = _profile_find_path(profile_id)
    if path is None:
        raise ValueError(f"参考排版档案不存在：{profile_id}")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ValueError(f"参考排版档案损坏：{profile_id}（{exc.__class__.__name__}）") from exc
    if not isinstance(data, dict) or data.get("id") != profile_id:
        raise ValueError(f"参考排版档案格式不正确：{profile_id}")
    return data


def _profile_save(profile: dict[str, Any]) -> Path | None:
    """保存参考档案；运行时目录不可写时回退插件 data。

    某些 DSH 运行方式下 Python 子进程不能写用户主目录，此时返回 None，
    由宿主的 xiumi_reference 工具把同一份 profile 写入运行时目录。
    """
    payload = json.dumps(_profile_public(profile), ensure_ascii=False, indent=2)
    directories = [_profile_runtime_dir(), PROFILE_FALLBACK_DIR]
    for directory in directories:
        if directory is None:
            continue
        temp = directory / f".{profile['id']}.{os.getpid()}.tmp"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{profile['id']}.json"
            temp.write_text(payload, encoding="utf-8")
            os.replace(temp, target)
            return target
        except OSError:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass
            continue
    return None


def _profile_list() -> list[dict[str, Any]]:
    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for directory in _profile_dirs():
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.json")):
            profile_id = path.stem
            if profile_id in seen or not PROFILE_ID_RE.match(profile_id):
                continue
            seen.add(profile_id)
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if not isinstance(data, dict) or data.get("id") != profile_id:
                continue
            style = data.get("style") if isinstance(data.get("style"), dict) else {}
            titles = style.get("titles") if isinstance(style.get("titles"), list) else []
            dividers = style.get("dividers") if isinstance(style.get("dividers"), list) else []
            items.append({
                "id": profile_id,
                "name": data.get("name") or "",
                "sourceUrl": data.get("sourceUrl") or "",
                "title": data.get("title") or "",
                "fetchedAt": data.get("fetchedAt") or "",
                "titleLevels": len(titles),
                "hasDividers": bool(dividers),
            })
    items.sort(key=lambda item: item.get("fetchedAt") or "", reverse=True)
    return items


def _profile_delete(profile_id: str) -> bool:
    path = _profile_find_path(profile_id)
    if path is None:
        return False
    path.unlink()
    return True


def _profile_apply_theme(profile: dict[str, Any], theme: dict[str, Any]) -> dict[str, Any]:
    """把参考规则叠加到已解析主题上，不改主题对象本身。"""
    theme = json.loads(json.dumps(theme))
    style = profile.get("style") if isinstance(profile.get("style"), dict) else {}
    titles = style.get("titles") if isinstance(style.get("titles"), list) else []
    body = style.get("body") if isinstance(style.get("body"), dict) else {}
    palette = style.get("palette") if isinstance(style.get("palette"), dict) else {}

    style_tokens = theme.setdefault("style", {})
    if isinstance(theme.get("style"), dict):
        style_tokens = theme["style"]
    else:
        theme["style"] = style_tokens
    if titles:
        top = titles[0]
        style_tokens["title_size"] = top.get("fontSize", style_tokens.get("title_size"))
        style_tokens["title_weight"] = top.get("fontWeight", style_tokens.get("title_weight"))
        if top.get("lineHeight"):
            style_tokens["title_line_height"] = top["lineHeight"]
    if body:
        style_tokens["body_size"] = body.get("fontSize", style_tokens.get("body_size"))
        style_tokens["body_line_height"] = body.get("lineHeight", style_tokens.get("body_line_height"))
        style_tokens["body_paragraph_spacing"] = body.get(
            "paragraphGap", style_tokens.get("body_paragraph_spacing")
        )
        style_tokens["body_letter_spacing"] = body.get(
            "letterSpacing", style_tokens.get("body_letter_spacing")
        )
    palette_tokens = theme.setdefault("palette", {})
    if not isinstance(theme.get("palette"), dict):
        theme["palette"] = palette_tokens
    if palette.get("primary"):
        palette_tokens["primary"] = palette["primary"]
    for key in ("text", "background"):
        if palette.get(key):
            palette_tokens[key] = palette[key]
    accents = palette.get("accents") or []
    if accents:
        palette_tokens["accent_2"] = accents[0]
    if len(accents) > 1:
        palette_tokens["accent_3"] = accents[1]
    theme["profile"] = {
        "id": profile.get("id"),
        "name": profile.get("name"),
        "dividers": style.get("dividers") or [],
        "titles": titles[:4],
        "images": style.get("images") or {},
        "missing": profile.get("missing") or [],
    }
    return theme


def _is_light_color(color: str) -> bool:
    match = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color)
    if not match:
        return False
    red, green, blue = (int(part) for part in match.groups())
    return 0.299 * red + 0.587 * green + 0.114 * blue >= 235


def _color_luminance(color: str) -> float:
    match = re.match(r"rgb\((\d+),\s*(\d+),\s*(\d+)\)", color)
    if not match:
        return 0.0
    red, green, blue = (int(part) for part in match.groups())
    return 0.299 * red + 0.587 * green + 0.114 * blue


def _coerce_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _article_id_from_url(url: str) -> str:
    """从秀米图文 URL 中提取数字 articleId；新建稿返回空串。"""
    raw = str(url or "")
    match = re.search(r"/paper/(?:for/)?(\d+)(?:/|$|[?#])", raw)
    if not match:
        match = re.search(r"[?&#](?:paperId|paper_id|id)=(\d+)", raw)
    return match.group(1) if match else ""


def _catalog_now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _catalog_parse_time(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _catalog_age_days(catalog: dict[str, Any] | None) -> float | None:
    if not isinstance(catalog, dict):
        return None
    captured = _catalog_parse_time(catalog.get("capturedAt"))
    if captured is None:
        return None
    return max(0.0, (datetime.now(timezone.utc) - captured.astimezone(timezone.utc)).total_seconds() / 86400.0)


def _catalog_entry_count(groups: list[dict[str, Any]]) -> int:
    return sum(len(group.get("categories") or []) for group in groups if isinstance(group, dict))


def _catalog_normalize(data: Any) -> dict[str, Any] | None:
    """只保留公开结构字段，丢弃任何额外/私有内容。"""
    if not isinstance(data, dict):
        return None
    try:
        schema_version = int(data.get("schemaVersion") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    groups: list[dict[str, Any]] = []
    seen_groups: set[str] = set()
    for raw_group in data.get("groups") or []:
        if not isinstance(raw_group, dict):
            continue
        name = str(raw_group.get("name") or "").strip()[:60]
        if not name or name in seen_groups:
            continue
        categories: list[str] = []
        for raw_category in raw_group.get("categories") or []:
            if isinstance(raw_category, dict):
                raw_category = raw_category.get("name")
            category = str(raw_category or "").strip()[:80]
            if category and category not in categories:
                categories.append(category)
        if not categories:
            continue
        seen_groups.add(name)
        groups.append({"name": name, "categories": categories})
    if not groups:
        return None
    source_url = str(data.get("sourceUrl") or "").strip()[:500]
    try:
        parsed_url = urlparse(source_url)
        if parsed_url.scheme != "https" or not (
            parsed_url.netloc == "xiumi.us" or parsed_url.netloc.endswith(".xiumi.us")
        ):
            source_url = ""
    except Exception:
        source_url = ""
    captured_at = str(data.get("capturedAt") or "").strip()
    if _catalog_parse_time(captured_at) is None:
        return None
    last_verified = str(data.get("lastVerifiedAt") or "").strip() or captured_at
    tags = [str(item).strip()[:40] for item in (data.get("tags") or []) if str(item).strip()]
    return {
        "schemaVersion": schema_version,
        "capturedAt": captured_at,
        "lastVerifiedAt": last_verified,
        "sourceUrl": source_url,
        "entryCount": _catalog_entry_count(groups),
        "searchHint": str(data.get("searchHint") or "").strip()[:120],
        "sortOrder": str(data.get("sortOrder") or "").strip()[:40],
        "tags": tags[:20],
        "groups": groups,
    }


def _catalog_is_valid(catalog: dict[str, Any] | None, fresh: bool = True) -> bool:
    if not isinstance(catalog, dict):
        return False
    if catalog.get("schemaVersion") != CATALOG_SCHEMA_VERSION:
        return False
    age = _catalog_age_days(catalog)
    if age is None:
        return False
    if fresh and age > CATALOG_TTL_DAYS:
        return False
    return bool(catalog.get("groups"))


def _catalog_read(path: Path | None) -> dict[str, Any] | None:
    if path is None:
        return None
    try:
        if not path.exists():
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        _log(f"读取模板目录失败 {path.name}: {type(exc).__name__}")
        return None
    return _catalog_normalize(raw)


def _catalog_write(path: Path | None, catalog: dict[str, Any]) -> bool:
    """原子写：先写同目录 .tmp，再 os.replace 覆盖。"""
    if path is None:
        return False
    normalized = _catalog_normalize(catalog)
    if normalized is None:
        return False
    tmp = path.with_name(path.name + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(normalized, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        os.replace(tmp, path)
        return True
    except Exception as exc:
        _log(f"写入模板目录失败 {path.name}: {type(exc).__name__}")
        try:
            if tmp.exists():
                tmp.unlink()
        except Exception:
            pass
        return False


def _catalog_diff(previous: dict[str, Any] | None, current: dict[str, Any]) -> dict[str, Any]:
    def structure(catalog: dict[str, Any] | None) -> dict[str, set[str]]:
        result: dict[str, set[str]] = {}
        if not isinstance(catalog, dict):
            return result
        for group in catalog.get("groups") or []:
            if not isinstance(group, dict):
                continue
            name = str(group.get("name") or "").strip()
            if not name:
                continue
            result.setdefault(name, set()).update(
                str(category).strip() for category in (group.get("categories") or []) if str(category).strip()
            )
        return result

    before = structure(previous)
    after = structure(current)
    added_groups = sorted(set(after) - set(before))
    removed_groups = sorted(set(before) - set(after))
    added_categories: list[str] = []
    removed_categories: list[str] = []
    for name in sorted(set(before) | set(after)):
        old = before.get(name, set())
        new = after.get(name, set())
        added_categories.extend(f"{name}/{item}" for item in sorted(new - old))
        removed_categories.extend(f"{name}/{item}" for item in sorted(old - new))
    total_before = sum(len(items) for items in before.values())
    total_after = sum(len(items) for items in after.values())
    ratio = abs(total_after - total_before) / max(total_before, 1)
    changed = bool(added_groups or removed_groups or added_categories or removed_categories or ratio > 0.20)
    return {
        "changed": changed,
        "addedGroups": added_groups[:50],
        "removedGroups": removed_groups[:50],
        "addedCategories": added_categories[:200],
        "removedCategories": removed_categories[:200],
        "totalBefore": total_before,
        "totalAfter": total_after,
        "changeRatio": round(ratio, 4),
        "threshold": 0.20,
    }


def _load_config(argv: list[str]) -> dict[str, Any]:
    config = dict(DEFAULT_CONFIG)
    raw = argv[1] if len(argv) > 1 else ""
    if raw and str(raw).strip():
        try:
            parsed = json.loads(raw)
        except Exception as exc:
            _log(f"启动参数不是合法 JSON，使用默认配置: {exc}")
            parsed = None
        if isinstance(parsed, dict):
            config.update(parsed)
    channel = str(config.get("browserChannel") or "").strip()
    config["browserChannel"] = channel or "msedge"
    config["loginTimeoutMs"] = max(1000, _coerce_int(config.get("loginTimeoutMs"), 900000))
    return config


def _write_response(payload: dict[str, Any]) -> None:
    line = json.dumps(payload, ensure_ascii=False)
    try:
        sys.stdout.write(line + "\n")
        sys.stdout.flush()
    except UnicodeEncodeError:
        # 极端编码环境下退化为纯 ASCII JSON，内容仍然合法
        sys.stdout.write(json.dumps(payload, ensure_ascii=True) + "\n")
        sys.stdout.flush()


class _ComposeTextExtractor(HTMLParser):
    """从 compose 生成的 HTML 片段中取回纯文本，只用于保真比对。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []

    def handle_data(self, data: str) -> None:
        self._parts.append(data)

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "br":
            self._parts.append("\n")

    def text(self) -> str:
        return "".join(self._parts)


def _compose_text_from_html(value: str) -> str:
    parser = _ComposeTextExtractor()
    try:
        parser.feed(str(value or ""))
        parser.close()
    except Exception:
        return re.sub(r"<[^>]+>", "", str(value or ""))
    return parser.text()


_TOP_LEVEL_HTML_TAGS = {"p", "div", "section", "h1", "h2", "h3", "blockquote", "li"}
_HTML_VOID_TAGS = {
    "area",
    "base",
    "br",
    "col",
    "embed",
    "hr",
    "img",
    "input",
    "link",
    "meta",
    "param",
    "source",
    "track",
    "wbr",
}


class _TopLevelHtmlSegmenter(HTMLParser):
    """把 HTML 拆成顶层段落，保留每段 outerHTML（用于秀米原生 cell）。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.segments: list[str] = []
        self._capture: list[str] | None = None
        self._depth = 0

    def handle_starttag(self, tag: str, attrs) -> None:
        lowered = tag.lower()
        raw = self.get_starttag_text() or f"<{tag}>"
        if self._capture is None:
            if lowered in _TOP_LEVEL_HTML_TAGS:
                self._capture = [raw]
                self._depth = 0 if lowered in _HTML_VOID_TAGS else 1
            return
        self._capture.append(raw)
        if lowered not in _HTML_VOID_TAGS:
            self._depth += 1

    def handle_startendtag(self, tag: str, attrs) -> None:
        if self._capture is not None:
            self._capture.append(self.get_starttag_text() or f"<{tag}/>")

    def handle_endtag(self, tag: str) -> None:
        if self._capture is None:
            return
        self._capture.append(f"</{tag}>")
        if tag.lower() not in _HTML_VOID_TAGS:
            self._depth -= 1
        if self._depth <= 0:
            self.segments.append("".join(self._capture).strip())
            self._capture = None
            self._depth = 0

    def handle_data(self, data: str) -> None:
        if self._capture is not None:
            self._capture.append(data)

    def close(self) -> None:
        super().close()
        if self._capture is not None:
            self.segments.append("".join(self._capture).strip())
            self._capture = None
            self._depth = 0


_BLOCK_TAG_RE = re.compile(
    r"<(h[1-6]|p|blockquote|li)\b[^>]*>.*?</\1\s*>", re.I | re.S
)


def _extract_block_segments(source: str) -> list[str]:
    """按文档顺序抽取 h1-h6/p/blockquote/li，跳过容器 section/div 包裹。

    秀米 themed HTML 外层是 section/div，直接按顶层标签分段会把整篇塞进
    一个 cell；t25 要求每段独立 cell，所以只取真正的块级文本元素。
    """
    result: list[str] = []
    last_end = -1
    for match in _BLOCK_TAG_RE.finditer(source):
        if match.start() < last_end:
            continue
        segment = match.group(0).strip()
        if not segment:
            continue
        if _compose_norm_text(_compose_text_from_html(segment)):
            result.append(segment)
            last_end = match.end()
    return result


def _split_html_segments(value: str) -> tuple[list[str], list[str]]:
    """返回 (outerHTML 段落列表, 对应纯文本列表)，用于原生 cell 写入。"""
    source = str(value or "").strip()
    if not source:
        return [], []
    blocks = _extract_block_segments(source)
    if blocks:
        segments: list[str] = []
        texts: list[str] = []
        for segment in blocks:
            text = _compose_norm_text(_compose_text_from_html(segment))
            if not text:
                continue
            segments.append(segment)
            texts.append(text)
        if segments:
            return segments, texts
    parser = _TopLevelHtmlSegmenter()
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        parser.segments = []
    segments: list[str] = []
    texts: list[str] = []
    for segment in parser.segments:
        text = _compose_norm_text(_compose_text_from_html(segment))
        if not text:
            continue
        segments.append(segment)
        texts.append(text)
    if segments:
        return segments, texts
    text = _compose_norm_text(_compose_text_from_html(source))
    if not text:
        return [], []
    return [f"<p>{source}</p>"], [text]


def _compose_norm_text(value: str) -> str:
    """规范化只用于比对：trim、合并空白、NBSP→空格、统一换行。"""
    text = str(value or "").replace("\u00a0", " ")
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return re.sub(r"\s+", " ", text).strip()


def _compose_strip_ws(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "").replace("\u00a0", " "))


def _compose_first_text_diff(
    current: str, provided: str, text_blocks: list[dict[str, Any]]
) -> dict[str, Any] | None:
    """layout-only 写入前比对：当前正文与准备写入的文本必须逐字一致。"""
    current_chars = _compose_strip_ws(current)
    provided_chars = _compose_strip_ws(provided)
    if current_chars == provided_chars:
        return None
    position = 0
    limit = min(len(current_chars), len(provided_chars))
    while position < limit and current_chars[position] == provided_chars[position]:
        position += 1
    block_index = 0
    consumed = 0
    for block in text_blocks:
        length = len(_compose_strip_ws(block.get("text") or ""))
        if position < consumed + length:
            block_index = int(block.get("blockIndex") or 0)
            break
        consumed += length
        block_index = int(block.get("blockIndex") or 0)
    return {
        "blockIndex": block_index,
        "expected": current_chars[position : position + 120],
        "actual": provided_chars[position : position + 120],
        "position": position,
    }


def _compose_norm_blocks(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, list) or not raw:
        raise ValueError("compose 需要非空的 blocks 数组")
    result: list[dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise ValueError(f"blocks 第 {index + 1} 项必须是对象")
        kind = str(item.get("type") or "").strip().lower()
        if kind not in {"paragraph", "heading", "quote", "image"}:
            raise ValueError(
                f"blocks 第 {index + 1} 项的 type 必须是 paragraph/heading/quote/image"
            )
        if kind == "image":
            file_value = item.get("file")
            asset_value = item.get("assetId") or item.get("asset_id")
            if not file_value and not asset_value:
                raise ValueError(f"blocks 第 {index + 1} 项图片缺少 file 或 assetId")
            result.append(
                {
                    "type": "image",
                    "file": str(file_value).strip() if file_value else "",
                    "assetId": str(asset_value).strip() if asset_value else "",
                    "blockIndex": index,
                }
            )
            continue
        text = item.get("text")
        if text is None:
            text = ""
        text = str(text)
        if not text.strip():
            raise ValueError(f"blocks 第 {index + 1} 项的 text 不能为空")
        level = item.get("level", 1)
        try:
            level = int(level)
        except Exception:
            level = 0
        if kind == "heading" and level not in (1, 2):
            raise ValueError(f"blocks 第 {index + 1} 项 heading.level 只能是 1 或 2")
        result.append(
            {
                "type": kind,
                "text": text,
                "level": level if kind == "heading" else 0,
                "blockIndex": index,
            }
        )
    return result


def _compose_theme_tokens(theme: Any) -> dict[str, Any]:
    style = theme.get("style") if isinstance(theme, dict) else {}
    palette = theme.get("palette") if isinstance(theme, dict) else {}
    if not isinstance(style, dict):
        style = {}
    if not isinstance(palette, dict):
        palette = {}
    title = style.get("title") if isinstance(style.get("title"), dict) else {}
    body = style.get("body") if isinstance(style.get("body"), dict) else {}
    return {
        "title_size": int(style.get("title_size") or title.get("size") or 22),
        "title_weight": style.get("title_weight") or title.get("weight") or 600,
        "title_family": title.get("family") or "system-ui, sans-serif",
        "title_align": title.get("align") or "left",
        "title_line_height": style.get("title_line_height") or title.get("lineHeight") or 1.35,
        "title_spacing": title.get("letterSpacing") or 0,
        "body_size": int(style.get("body_size") or body.get("size") or 15),
        "body_family": body.get("family") or "system-ui, sans-serif",
        "body_line_height": style.get("body_line_height") or body.get("lineHeight") or 1.8,
        "body_letter_spacing": style.get("body_letter_spacing") or body.get("letterSpacing") or 0,
        "paragraph_spacing": int(
            style.get("body_paragraph_spacing") or body.get("paragraphSpacing") or 18
        ),
        "text": palette.get("text") or "#333333",
        "primary": palette.get("primary") or "#C95E57",
        "surface": palette.get("surface") or "rgba(0,0,0,0.035)",
        "background": palette.get("background") or "#FFFFFF",
        "title_color": (
            (palette.get("primary") or "#C95E57")
            if title.get("color") == "primary"
            else (palette.get("text") or "#333333")
        ),
        "body_color": body.get("color") or palette.get("text") or "#333333",
        "profile": theme.get("profile") if isinstance(theme, dict) else {},
    }


_CSS_HEX_RE = re.compile(r"^#([0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _cell_color(value: str) -> str:
    """把 CSS 颜色规范成秀米 cell.style 使用的 rgb()/rgba() 字符串。"""
    text = str(value or "").strip()
    match = _CSS_HEX_RE.match(text)
    if match:
        raw = match.group(1)
        if len(raw) == 3:
            raw = "".join(ch + ch for ch in raw)
        number = int(raw, 16)
        return f"rgb({(number >> 16) & 255},{(number >> 8) & 255},{number & 255})"
    match = re.match(r"rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)(?:\s*,\s*([\d.]+))?\s*\)", text, re.I)
    if match:
        if match.group(4) is not None:
            return f"rgba({match.group(1)},{match.group(2)},{match.group(3)},{match.group(4)})"
        return f"rgb({match.group(1)},{match.group(2)},{match.group(3)})"
    return text


def _cell_font_size(value: str) -> str:
    """把 CSS font-size 转成秀米可持久化的百分比字符串（基准 16px）。"""
    text = str(value or "").strip().lower()
    match = re.match(r"^(-?[\d.]+)\s*(px|pt|em|rem|%)?$", text)
    if not match:
        return text
    number = float(match.group(1))
    unit = match.group(2) or "px"
    if unit == "%":
        return f"{number:g}%"
    if unit in ("em", "rem"):
        number = number * 16.0
    elif unit == "pt":
        number = number * 4.0 / 3.0
    return f"{number / 16.0 * 100:g}%"


def _cell_length(value: str) -> str:
    """把 CSS 长度规范成 cell.style 可持久化的 px/%/原始值。"""
    text = str(value or "").strip()
    match = re.match(r"^(-?[\d.]+)\s*(px|pt|em|rem|%)?$", text, re.I)
    if not match:
        return text
    number = float(match.group(1))
    unit = (match.group(2) or "px").lower()
    if unit == "%":
        return f"{number:g}%"
    if unit in ("em", "rem"):
        number *= 16.0
    elif unit == "pt":
        number *= 4.0 / 3.0
    return f"{number:g}px"


def _split_css_box_values(value: str) -> list[str]:
    """把 margin/padding 的 1~4 值简写展开为 top,right,bottom,left。"""
    parts = [part for part in re.split(r"\s+", str(value or "").strip()) if part]
    if len(parts) == 1:
        return parts * 4
    if len(parts) == 2:
        return [parts[0], parts[1], parts[0], parts[1]]
    if len(parts) == 3:
        return [parts[0], parts[1], parts[2], parts[1]]
    if len(parts) >= 4:
        return parts[:4]
    return []


def _cell_style_from_inline(style_text: str) -> dict[str, str]:
    """把普通 CSS 声明映射为 cell.style 字段；只保留实测可持久化的字段。"""
    result: dict[str, str] = {}
    declarations = str(style_text or "").split(";")
    mapping = {
        "font-size": "fontSize",
        "color": "color",
        "font-weight": "fontWeight",
        "font-style": "fontStyle",
        "line-height": "lineHeight",
        "letter-spacing": "letterSpacing",
        "text-align": "textAlign",
        "text-indent": "textIndent",
        "background-color": "backgroundColor",
        "margin-top": "marginTop",
        "margin-right": "marginRight",
        "margin-bottom": "marginBottom",
        "margin-left": "marginLeft",
        "padding-top": "paddingTop",
        "padding-right": "paddingRight",
        "padding-bottom": "paddingBottom",
        "padding-left": "paddingLeft",
        "border-left": "borderLeft",
        "border-right": "borderRight",
        "border-top": "borderTop",
        "border-bottom": "borderBottom",
    }
    for declaration in declarations:
        if ":" not in declaration:
            continue
        name, value = declaration.split(":", 1)
        name = name.strip().lower()
        value = value.strip()
        if not value:
            continue
        if name in ("margin", "padding"):
            values = _split_css_box_values(value)
            if not values:
                continue
            prefix = "margin" if name == "margin" else "padding"
            for side, item in zip(("Top", "Right", "Bottom", "Left"), values):
                result[prefix + side] = _cell_length(item)
            continue
        if name not in mapping:
            continue
        key = mapping[name]
        if name == "font-size":
            value = _cell_font_size(value)
        elif name in ("color", "background-color"):
            value = _cell_color(value)
        elif name == "letter-spacing" and re.match(r"^-?[\d.]+$", value):
            value = f"{value}px"
        elif name.startswith("border-"):
            border = re.match(
                r"^(\S+)\s+(solid|dashed|dotted|double)\s+(.+)$", value, re.I
            )
            if border:
                value = "%s %s %s" % (
                    _cell_length(border.group(1)),
                    border.group(2).lower(),
                    _cell_color(border.group(3)),
                )
        elif name.startswith(("margin-", "padding-")):
            value = _cell_length(value)
        result[key] = value
    return result


def _cell_style_from_segment(segment: str) -> dict[str, str]:
    match = re.search(r"""style\s*=\s*(["'])(.*?)\1""", str(segment or ""), re.I | re.S)
    return _cell_style_from_inline(match.group(2)) if match else {}


def _compose_block_style(block: dict[str, Any], theme: Any) -> dict[str, str]:
    """把主题/参考 profile 的排版规则映射成 cell.style。"""
    tokens = _compose_theme_tokens(theme)
    kind = block["type"]
    level = int(block.get("level") or 1)
    if kind == "heading":
        size = tokens["title_size"] + (4 if level == 1 else 0)
        return {
            "fontSize": _cell_font_size(f"{size}px"),
            "fontWeight": str(tokens["title_weight"]),
            "lineHeight": str(tokens["title_line_height"]),
            "letterSpacing": f"{tokens['title_spacing']}px",
            "textAlign": str(tokens["title_align"]),
            "color": _cell_color(str(tokens["title_color"])),
            "marginBottom": "14px",
        }
    if kind == "quote":
        return {
            "fontSize": _cell_font_size(f"{tokens['body_size']}px"),
            "lineHeight": str(tokens["body_line_height"]),
            "letterSpacing": f"{tokens['body_letter_spacing']}px",
            "color": _cell_color(str(tokens["body_color"])),
            "backgroundColor": _cell_color(str(tokens["surface"])),
            "paddingTop": "12px",
            "paddingBottom": "12px",
            "paddingLeft": "14px",
            "paddingRight": "14px",
            "borderLeft": f"3px solid {_cell_color(str(tokens['primary']))}",
            "marginBottom": f"{tokens['paragraph_spacing']}px",
        }
    return {
        "fontSize": _cell_font_size(f"{tokens['body_size']}px"),
        "lineHeight": str(tokens["body_line_height"]),
        "letterSpacing": f"{tokens['body_letter_spacing']}px",
        "color": _cell_color(str(tokens["body_color"])),
        "marginBottom": f"{tokens['paragraph_spacing']}px",
    }


def _compose_segment_html(block: dict[str, Any], theme: Any) -> str:
    tokens = _compose_theme_tokens(theme)
    kind = block["type"]
    content = escape(str(block.get("text") or "")).replace("\n", "<br>")
    if kind == "heading":
        level = int(block.get("level") or 1)
        size = tokens["title_size"] + (4 if level == 1 else 0)
        divider = (tokens.get("profile") or {}).get("dividers") or []
        divider_css = ""
        if divider and isinstance(divider[0], dict):
            first = divider[0]
            width = float(first.get("borderWidth") or 1)
            color = first.get("color") or tokens["primary"]
            divider_css = (
                f'border-bottom:{max(width, 1):g}px solid {color};padding-bottom:8px;'
            )
        return (
            f'<p data-compose-type="heading" data-compose-level="{level}" '
            f'style="margin:0 0 14px;font-family:{tokens["title_family"]};'
            f'font-size:{size}px;font-weight:{tokens["title_weight"]};'
            f'line-height:{tokens["title_line_height"]};'
            f'letter-spacing:{tokens["title_spacing"]}px;{divider_css}'
            f'text-align:{tokens["title_align"]};color:{tokens["text"]};">{content}</p>'
        )
    if kind == "quote":
        return (
            f'<p data-compose-type="quote" '
            f'style="margin:0 0 {tokens["paragraph_spacing"]}px;'
            f'padding:12px 14px;border-left:3px solid {tokens["primary"]};'
            f'background:{tokens["surface"]};border-radius:6px;'
            f'font-family:{tokens["body_family"]};font-size:{tokens["body_size"]}px;'
            f'line-height:{tokens["body_line_height"]};letter-spacing:{tokens["body_letter_spacing"]}px;'
            f'color:{tokens["text"]};">{content}</p>'
        )
    return (
        f'<p data-compose-type="paragraph" '
        f'style="margin:0 0 {tokens["paragraph_spacing"]}px;'
        f'font-family:{tokens["body_family"]};font-size:{tokens["body_size"]}px;'
        f'line-height:{tokens["body_line_height"]};letter-spacing:{tokens["body_letter_spacing"]}px;'
        f'color:{tokens["text"]};">{content}</p>'
    )


class Bridge:
    """持有 Playwright 与浏览器会话，按请求分发方法。"""

    # 临时截图保留策略：只留最近 60 张、24 小时内，超出即删。
    SCREENSHOT_KEEP = 60
    SCREENSHOT_MAX_AGE_HOURS = 24.0

    def __init__(self, config: dict[str, Any]) -> None:
        self.config = config
        self._playwright = None
        self._browser = None
        self._context = None
        self._page = None
        self._new_article = False
        self._opened_article: dict[str, Any] | None = None
        self._last_error = ""
        self._last_url = ""
        self._last_editor_url = ""
        self._saved_baseline: dict[str, Any] | None = None
        self._page_recovered = False
        self._browser_relaunched = False
        self._quota_cache: dict[str, Any] | None = None
        self._quota_cache_at = 0.0
        self._restore_dialog_evidence: dict[str, Any] | None = None
        self._edit_inspection: dict[str, Any] | None = None
        self._cancel_event = None
        self._expected_target: dict[str, Any] | None = None
        self._document_token = uuid.uuid4().hex

    # ------------------------------------------------------------------ 内部
    def _ensure_playwright(self):
        if self._playwright is not None:
            return self._playwright
        # 延迟导入：确保 TMP/TEMP 已在模块导入阶段修好
        from playwright.sync_api import sync_playwright

        self._playwright = sync_playwright().start()
        _log("playwright driver started")
        return self._playwright

    def _browser_connected(self) -> bool:
        # persistent context 的 .browser 可能为 None；context 仍存活时也算浏览器可用。
        if self._browser is not None:
            try:
                if bool(self._browser.is_connected()):
                    return True
            except Exception:
                pass
        if self._context is not None:
            try:
                _ = self._context.pages
                return True
            except Exception:
                return False
        return False

    @staticmethod
    def _safe_close(obj) -> None:
        if obj is None:
            return
        try:
            obj.close()
        except Exception:
            pass

    def _teardown_browser(self) -> None:
        """关闭浏览器与 context，并把引用清空（可重复调用）。"""
        browser, context = self._browser, self._context
        self._browser = None
        self._context = None
        self._page = None
        self._new_article = False
        self._opened_article = None
        self._saved_baseline = None
        self._last_editor_url = ""
        self._page_recovered = False
        self._browser_relaunched = False
        self._quota_cache = None
        self._quota_cache_at = 0.0
        self._restore_dialog_evidence = None
        self._edit_inspection = None
        self._safe_close(context)
        self._safe_close(browser)

    def _current_page(self):
        """取当前页面。浏览器还活着时，标签页被关只新建标签页恢复，不销毁浏览器。"""
        if not self._browser_connected():
            # 只有浏览器进程真的断开才清理；之后 open 会用同一持久化 profile 重启。
            self._teardown_browser()
            return None

        page = self._page
        if page is not None:
            try:
                if not page.is_closed():
                    self._last_url = str(page.url or self._last_url)
                    if "/paper/for/" in self._last_url:
                        self._last_editor_url = self._last_url
                    return page
            except Exception:
                pass
            try:
                self._last_url = str(page.url or self._last_url)
            except Exception:
                pass

        # 接管 context 里仍存在的页面（例如用户自己新开的标签页）。
        pages = []
        try:
            if self._context is not None:
                pages = [p for p in self._context.pages if not p.is_closed()]
        except Exception:
            pages = []
        if pages:
            # 优先接管非空白页；只剩会话守护的空页时，导航回离开前的 URL。
            previous_url = self._last_url
            preferred = next((p for p in pages if str(p.url or "") not in ("", "about:blank")), pages[0])
            self._page = preferred
            current_url = str(getattr(self._page, "url", "") or "")
            if current_url not in ("", "about:blank"):
                self._last_url = current_url
                if "/paper/for/" in self._last_url:
                    self._last_editor_url = self._last_url
            self._page_recovered = True
            if current_url in ("", "about:blank"):
                try:
                    self._goto(self._page, previous_url or XIUMI_URL, settle=3.0)
                except Exception as exc:
                    _log(f"恢复标签页导航失败: {type(exc).__name__}: {exc}")
            return self._page

        # 最后一个标签页被关掉：新建标签页并回到离开前的 URL；不重启整个浏览器。
        if self._context is not None:
            try:
                recovered = self._context.new_page()
                self._page = recovered
                self._page_recovered = True
                target = self._last_url or XIUMI_URL
                try:
                    self._goto(recovered, target, settle=3.0)
                except Exception as exc:
                    _log(f"恢复标签页导航失败（保留新标签页）: {type(exc).__name__}: {exc}")
                return recovered
            except Exception as exc:
                _log(f"浏览器仍连接但无法新建标签页: {type(exc).__name__}: {exc}")

        # 连新标签页都建不出来，视为浏览器不可用；open 时按持久化 profile 重启。
        self._teardown_browser()
        return None

    def _status_dict(self) -> dict[str, Any]:
        page = self._current_page()
        if page is None:
            return {"running": False, "loggedIn": False, "url": "", "title": ""}
        try:
            url = str(page.url or "")
            title = str(page.title() or "")
            logged_in = bool(is_logged_in(page))
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            _log(f"status 读取失败，尝试恢复页面而不关闭浏览器: {self._last_error}")
            page = self._current_page()
            if page is None:
                return {"running": False, "loggedIn": False, "url": "", "title": ""}
            try:
                url = str(page.url or "")
                title = str(page.title() or "")
                logged_in = bool(is_logged_in(page))
            except Exception:
                url, title, logged_in = "", "", False
        result = {"running": True, "loggedIn": logged_in, "url": url, "title": title}
        if self._page_recovered:
            result["relaunchedPage"] = True
        if self._browser_relaunched:
            result["relaunchedBrowser"] = True
        if self._restore_dialog_evidence:
            result["recoveredFromDirtyDraft"] = self._restore_dialog_evidence
            result["discardedDraft"] = bool(
                self._restore_dialog_evidence.get("discardedDraft")
            )
        return result

    def _require_page(self):
        """取当前页面；浏览器没启动时自动拉起（失败如实抛错）。"""
        page = self._current_page()
        if page is None:
            self.open()
            page = self._current_page()
        if page is None:
            raise RuntimeError("浏览器未启动或窗口已被关闭，请重新调用 open。")
        return page

    def _ensure_page_alive(self, page=None):
        """在取 page 前确认标签页没被关；已关则新建标签页，必要时才重启浏览器。"""
        if page is not None:
            try:
                if not page.is_closed():
                    return page
            except Exception:
                pass
        recovered = self._current_page()
        if recovered is not None:
            return recovered
        # 浏览器进程已死：用同一持久化 profile 重启，cookie/登录态还在。
        self.open()
        self._browser_relaunched = True
        page = self._current_page()
        if page is None:
            raise RuntimeError("浏览器无法启动，请检查可见窗口是否被其他程序占用。")
        return page

    def _goto(self, page, url: str, settle: float = 6.0) -> None:
        self._check_cancelled()
        page.goto(url, wait_until="domcontentloaded", timeout=60000)
        try:
            page.wait_for_load_state("load", timeout=10000)
        except Exception:
            pass
        try:
            self._last_url = str(page.url or url)
        except Exception:
            self._last_url = url
        if "/paper/for/" in self._last_url:
            self._last_editor_url = self._last_url
        time.sleep(settle)

    @staticmethod
    def _is_auth_page(page) -> bool:
        try:
            if "/auth" in str(page.url or ""):
                return True
            loc = page.locator("input[name='password']")
            for index in range(min(loc.count(), 3)):
                try:
                    if loc.nth(index).is_visible():
                        return True
                except Exception:
                    continue
        except Exception:
            pass
        return False

    @staticmethod
    def _editor_present(page) -> bool:
        try:
            return (
                page.locator(TITLE_SELECTOR).count() > 0
                and page.locator("div[contenteditable='true']").count() > 0
            )
        except Exception:
            return False

    @staticmethod
    def _restore_dialog_info(page) -> dict[str, Any] | None:
        """检测秀米「恢复未保存草稿」确认框；只认可见节点，避免命中模板/隐藏脚本。"""
        try:
            return page.evaluate(
                """
                () => {
                  const markers = ["上次没有保存到服务器", "是否恢复"];
                  const visible = (el) => {
                    const r = el.getBoundingClientRect();
                    if (r.width <= 0 || r.height <= 0) return false;
                    const s = window.getComputedStyle(el);
                    return s.visibility !== "hidden" && s.display !== "none" && s.opacity !== "0";
                  };
                  const marked = Array.from(document.querySelectorAll("body *")).filter(el => {
                    if (!(el instanceof HTMLElement) || !visible(el)) return false;
                    const t = (el.innerText || "").trim();
                    return t && markers.some(m => t.includes(m));
                  });
                  if (!marked.length) return null;
                  marked.sort((a, b) => (a.innerText || "").length - (b.innerText || "").length);
                  const node = marked[0];
                  const findButton = (root, label) => {
                    const buttons = Array.from(root.querySelectorAll("button,a,span,div"));
                    return buttons.find(el => {
                      const t = (el.textContent || "").trim();
                      const r = el.getBoundingClientRect();
                      return t === label && r.width > 0 && r.height > 0;
                    });
                  };
                  let p = node;
                  for (let depth = 0; p && depth < 10; depth++, p = p.parentElement) {
                    const confirm = findButton(p, "确定");
                    const cancel = findButton(p, "取消");
                    if (confirm || cancel) {
                      const point = (el) => {
                        if (!el) return null;
                        const r = el.getBoundingClientRect();
                        return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
                      };
                      return {
                        text: (node.innerText || "").trim().slice(0, 500),
                        buttons: { confirm: point(confirm), cancel: point(cancel) }
                      };
                    }
                  }
                  return {text: (node.innerText || "").trim().slice(0, 500), buttons: {}};
                }
                """
            )
        except Exception:
            return None

    def _safe_screenshot_path(self, name: str) -> str:
        try:
            result = self.screenshot({"name": name, "fullPage": False})
            return str(result.get("path") or "")
        except Exception as exc:
            _log(f"恢复弹窗截图失败 name={name}: {type(exc).__name__}: {exc}")
            return ""

    def _handle_restore_dialog(self, page, policy: str = "block") -> dict[str, Any] | None:
        """Do not silently restore or discard a previously unsaved local draft."""
        policy = str(policy or "block").strip().lower()
        if policy not in ("block", "restore", "discard"):
            raise ValueError("恢复弹窗 policy 必须是 block/restore/discard。")
        info = self._restore_dialog_info(page)
        if not info:
            return None
        if policy == "block":
            raise RuntimeError("检测到上次未保存草稿；请先在可见窗口选择恢复或丢弃，再 inspect 对账。未自动处理。")
        self._check_cancelled()
        label = "确定" if policy == "restore" else "取消"
        evidence: dict[str, Any] = {
            "detected": True,
            "dialogText": info.get("text") or "",
            "action": label,
            "policy": policy,
            "discardedDraft": policy == "discard",
            "restoredDraft": policy == "restore",
            "beforeScreenshot": self._safe_screenshot_path("restore-dialog-before"),
            "afterScreenshot": "",
            "dismissed": False,
            "url": "",
        }
        actions: list[str] = []
        try:
            point = (info.get("buttons") or {}).get("confirm" if policy == "restore" else "cancel")
            if point:
                page.mouse.click(float(point["x"]), float(point["y"]))
                actions.append("click_confirm" if policy == "restore" else "click_cancel")
            else:
                clicked = False
                try:
                    loc = page.get_by_text(label, exact=True)
                    for index in range(min(loc.count(), 10)):
                        try:
                            item = loc.nth(index)
                            if item.is_visible():
                                item.click(timeout=3000)
                                clicked = True
                                break
                        except Exception:
                            continue
                except Exception:
                    clicked = False
                actions.append(
                    ("click_confirm" if policy == "restore" else "click_cancel")
                    if clicked
                    else ("no_confirm_button" if policy == "restore" else "no_cancel_button")
                )
            time.sleep(1.0 if policy == "restore" else 0.5)
            if self._restore_dialog_info(page):
                if policy == "discard":
                    try:
                        page.keyboard.press("Escape")
                        actions.append("escape")
                        time.sleep(0.5)
                    except Exception:
                        pass
                    try:
                        removed = page.evaluate(
                            """
                            () => {
                              const markers = ["上次没有保存到服务器", "是否恢复"];
                              const nodes = Array.from(document.querySelectorAll("div,section,aside"));
                              let removed = 0;
                              for (const el of nodes) {
                                const cls = String(el.className || "");
                                const txt = el.innerText || "";
                                const isMask = /modal-mask|modal-backdrop|tn-dialog-mask|ui-widget-overlay|tn-modal-mask/i.test(cls);
                                const isDialog = /dialog|modal|popup|confirm/i.test(cls) && markers.some(m => txt.includes(m));
                                if (isMask || isDialog) { el.remove(); removed += 1; }
                              }
                              return removed;
                            }
                            """
                        )
                        if removed:
                            actions.append(f"remove_overlay:{removed}")
                            time.sleep(0.5)
                    except Exception:
                        pass
            evidence["url"] = str(page.url or "")
            remaining = self._restore_dialog_info(page)
            evidence["dismissed"] = remaining is None
            evidence["afterScreenshot"] = self._safe_screenshot_path("restore-dialog-after")
        except Exception as exc:
            evidence["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            evidence["actionLabel"] = label
            evidence["actions"] = actions
            self._restore_dialog_evidence = evidence
        return evidence

    def clear_blocking_restore_dialog(self, policy: str = "restore") -> bool:
        """工具调用前的通用防御：默认点「确定」恢复；明确丢弃时才点「取消」。"""
        page = self._current_page()
        if page is None:
            return False
        if self._restore_dialog_info(page) is None:
            return False
        evidence = self._handle_restore_dialog(page, policy=policy)
        return bool(evidence and evidence.get("dismissed"))

    def _wait_editor(
        self,
        page,
        timeout_ms: int = 20000,
        dismiss_restore: bool = False,
        restore_policy: str = "block",
    ) -> bool:
        deadline = time.monotonic() + timeout_ms / 1000.0
        next_restore_check = 0.0
        restore_attempts = 0
        while time.monotonic() < deadline:
            self._check_cancelled()
            if restore_policy == "block" and self._restore_dialog_info(page):
                return False
            if self._editor_present(page):
                return True
            if self._is_auth_page(page):
                return False
            now = time.monotonic()
            if dismiss_restore and restore_attempts < 5 and now >= next_restore_check:
                restore_attempts += 1
                next_restore_check = now + 2.0
                try:
                    if self._restore_dialog_info(page):
                        self._handle_restore_dialog(page, policy=restore_policy)
                except Exception:
                    pass
            time.sleep(0.35)
        return False

    def _scroll_editor_to_top(self, page) -> int:
        """把秀米编辑器纸张滚动到顶部，避免自动滚动后首个 cell 被固定顶栏遮住。"""
        try:
            return int(
                page.evaluate(
                    """() => {
                      let changed = 0;
                      const pallet = document.querySelector('.tn-working-pallet');
                      if (pallet && pallet.scrollTop > 0) {
                        pallet.scrollTop = 0;
                        pallet.dispatchEvent(new Event('scroll', { bubbles: true }));
                        changed += 1;
                      }
                      if (window.scrollY > 0) {
                        window.scrollTo(0, 0);
                        changed += 1;
                      }
                      return changed;
                    }"""
                )
                or 0
            )
        except Exception as exc:
            _log(f"滚动编辑器到顶部失败: {type(exc).__name__}: {exc}")
            return 0

    def _raise_editor_load_error(self, page) -> None:
        """识别秀米编辑器加载失败弹窗，避免把空白页当成可编辑正文。"""
        try:
            body = page.locator("body").inner_text(timeout=2500) or ""
        except Exception:
            return
        for marker in ("网络错误", "请检查网络", "加载失败"):
            if marker in body:
                raise RuntimeError(
                    f"秀米编辑器加载已有图文失败（页面提示「{marker}」），未执行任何修改。"
                    "请稍后重试，或先在可见浏览器窗口中手动打开该图文确认可访问。"
                )

    def _read_editor(self, page) -> tuple[str, str, bool]:
        title = ""
        try:
            loc = page.locator(TITLE_SELECTOR)
            if loc.count() > 0:
                title = str(loc.first.input_value() or "")
        except Exception:
            title = ""
        try:
            text = page.evaluate(
                """
                () => {
                  const cells = Array.from(document.querySelectorAll("div[contenteditable='true']"))
                    .filter(el => !el.querySelector("div[contenteditable='true']"))
                    .map(el => {
                      const r = el.getBoundingClientRect();
                      return {text: (el.innerText || '').trim(), x: r.x, y: r.y, w: r.width, h: r.height};
                    })
                    .filter(c => c.text && c.w > 120 && c.h > 0 && c.x >= 100)
                    .sort((a, b) => a.y - b.y || a.x - b.x);
                  const parts = [];
                  for (const cell of cells) {
                    if (!parts.length || parts[parts.length - 1] !== cell.text) {
                      parts.push(cell.text);
                    }
                  }
                  return parts.join('\\n');
                }
                """
            )
        except Exception:
            text = ""
        cleaned = re.sub(r"\n{3,}", "\n\n", str(text or "")).strip()
        return title, cleaned, self._editor_present(page)

    def _editor_snapshot(
        self, page, paragraph_limit: int = 10, with_body_html: bool = False
    ) -> dict[str, Any]:
        """只读采集编辑器当前状态；bodyHtml 仅在需要还原时内部使用。"""
        title, text, present = self._read_editor(page)
        paragraphs = [part.strip() for part in re.split(r"\n+", text) if part.strip()]
        snapshot: dict[str, Any] = {
            "title": title,
            "textPreview": text[:800],
            "charCount": len(re.sub(r"\s+", "", text)),
            "paragraphCount": len(paragraphs),
            "paragraphs": paragraphs[: max(1, min(int(paragraph_limit or 10), 50))],
            "editable": bool(present),
            "articleId": _article_id_from_url(str(page.url or "")),
            "url": str(page.url or ""),
        }
        try:
            snapshot["imageCount"] = int(
                page.evaluate(
                    """
                    () => {
                      const seen = new Set();
                      for (const el of document.querySelectorAll('.tn-cell-image')) {
                        const r = el.getBoundingClientRect();
                        if (r.x > 500 && r.width > 100 && r.height > 40) seen.add(el);
                      }
                      for (const el of document.querySelectorAll("div[contenteditable='true'] img")) {
                        const r = el.getBoundingClientRect();
                        if (r.x > 500 && r.width > 40 && r.height > 20) seen.add(el);
                      }
                      return seen.size;
                    }
                    """
                )
                or 0
            )
        except Exception:
            snapshot["imageCount"] = 0
        if with_body_html:
            try:
                snapshot["bodyHtml"] = self._body_element(page).inner_html()
            except Exception:
                snapshot["bodyHtml"] = ""
        return snapshot

    def _check_cancelled(self) -> None:
        if self._cancel_event is not None and self._cancel_event.is_set():
            raise RuntimeError("操作已取消或超时；可能已有部分修改，请 inspect 对账后再继续，禁止直接重试。")

    def _model_snapshot(self, page) -> dict[str, Any]:
        raw = page.evaluate(MODEL_SNAPSHOT_JS)
        return {"title": str(raw.get("title") or ""), "modelHash": model_digest(raw),
                "articleId": _article_id_from_url(str(page.url or "")), "url": str(page.url or "")}

    def _model_cells(self, page) -> list[dict[str, Any]]:
        """Return the normalized per-cell models used for change detection."""
        try:
            raw = page.evaluate(MODEL_SNAPSHOT_JS)
        except Exception:
            return []
        cells = raw.get("cells") if isinstance(raw, dict) else None
        return cells if isinstance(cells, list) else []

    def _assert_mutation_target(self, page, params: dict[str, Any] | None = None, check_revision: bool = True) -> dict[str, Any]:
        self._check_cancelled()
        params = params or {}
        if not self._editor_present(page):
            raise RuntimeError("当前不是图文编辑器，请先 open/new 绑定编辑目标。")
        self._handle_restore_dialog(page)
        current = self._model_snapshot(page)
        opened = self._opened_article or {}
        expected = self._expected_target or {}
        if expected.get("documentToken") and expected["documentToken"] != self._document_token:
            raise RuntimeError("工作页已被另一次打开/新建替换，请重新 open/inspect 绑定目标。")
        requested_id = str(params.get("articleId") or expected.get("articleId") or opened.get("articleId") or "")
        if not (opened or self._new_article):
            raise RuntimeError("编辑目标未绑定，请先 article_open 或 article_new。")
        if requested_id and current["articleId"] != requested_id:
            raise RuntimeError("当前文章与已绑定目标不一致，已拒绝修改；请重新 open/inspect。")
        if not requested_id and not self._new_article:
            raise RuntimeError("无法识别当前文章 ID，已拒绝修改。")
        revision = str(params.get("expectedRevision") or expected.get("revision") or "")
        if check_revision and revision and revision != current["modelHash"]:
            raise RuntimeError("文章内容、样式或图片已变化，旧版本计划失效；请重新 inspect。")
        # The request precondition applies before its first write. Internal saves
        # must retain identity without comparing the pre-write revision again.
        self._expected_target = {"articleId": current["articleId"], "documentToken": self._document_token}
        return current

    def _operation_metadata(self) -> dict[str, Any] | None:
        page = self._current_page()
        if page is None or not self._editor_present(page):
            return None
        try:
            state = self._model_snapshot(page)
            return {"articleId": state["articleId"], "url": state["url"], "revision": state["modelHash"], "newArticle": self._new_article, "documentToken": self._document_token}
        except Exception:
            return None

    def _guard_snapshot(self, page) -> dict[str, Any]:
        """完整保存模型摘要覆盖正文、样式、图片和排列顺序。"""
        snapshot = self._editor_snapshot(page, paragraph_limit=3)
        try:
            cell_count = int(self._cells_state(page).get("count") or 0)
        except Exception:
            cell_count = -1
        component_count = -1
        component_signature = ""
        try:
            components = self._component_cells(page).get("cells") or []
            component_count = len(components)
            raw = "|".join(str(item.get("text") or "") for item in components)
            component_signature = hashlib.sha1(raw.encode("utf-8")).hexdigest()
        except Exception:
            pass
        model = self._model_snapshot(page)
        cell_digests: list[str] = []
        cell_empty: list[bool] = []
        for item in self._model_cells(page):
            value = item.get("model") if isinstance(item, dict) else None
            encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            cell_digests.append(hashlib.sha256(encoded.encode("utf-8")).hexdigest())
            cell_empty.append(cell_model_is_empty(value))
        dom_text_hash = hashlib.sha256(self._read_editor(page)[1].encode("utf-8")).hexdigest()
        return {
            "modelHash": model["modelHash"],
            "domTextHash": dom_text_hash,
            "title": str(snapshot.get("title") or "").strip(),
            "charCount": int(snapshot.get("charCount") or 0),
            "cellCount": cell_count,
            "componentCount": component_count,
            "componentSignature": component_signature,
            "cellDigests": cell_digests,
            "cellEmpty": cell_empty,
            "articleId": str(snapshot.get("articleId") or ""),
            "url": str(snapshot.get("url") or ""),
        }

    def _unsaved_guard(self, page, discard: bool = False) -> dict[str, Any] | None:
        """离开编辑器前对比保存基线；有未保存差异且未 discard 时返回拦截结果。"""
        if discard:
            return None
        try:
            if not self._editor_present(page):
                return None
        except Exception:
            return {"ok": False, "blocked": True, "unsavedChanges": True,
                    "reason": "editorStateUnreadable", "message": "无法确认编辑器状态，已阻止离开。"}
        baseline = self._saved_baseline
        if baseline is None:
            opened = self._opened_article
            if not opened:
                return {"ok": False, "blocked": True, "unsavedChanges": True,
                        "reason": "baselineUnavailable", "message": "缺少保存基线，请先显式保存或确认丢弃。"}
            baseline = {
                "title": str(opened.get("title") or "").strip(),
                "charCount": int(opened.get("charCount") or 0),
                "cellCount": -1,
            }
        try:
            current = self._guard_snapshot(page)
        except Exception:
            return {"ok": False, "blocked": True, "unsavedChanges": True,
                    "reason": "editorStateUnreadable", "message": "无法读取完整模型，已阻止离开。"}
        equivalent = content_equivalent(baseline, current)
        changed = (
            not equivalent
            or current["title"] != str(baseline.get("title") or "").strip()
            or current["charCount"] != int(baseline.get("charCount") or 0)
        )
        if not changed:
            return None
        return {
            "ok": False,
            "blocked": True,
            "unsavedChanges": True,
            "current": current,
            "baseline": baseline,
            "message": (
                "当前稿未保存，请先调用 article_save（或 xiumi_write / xiumi_compose 的保存）"
                "，确认后再离开；如果明确要丢弃改动，请传 discard:true。"
            ),
        }

    def _leave_editor_guarded(self, page, discard: bool = False) -> dict[str, Any]:
        """有未保存差异时阻止导航；只允许明确保存或明确丢弃。"""
        try:
            if not self._editor_present(page):
                return {
                    "ok": True,
                    "left": True,
                    "saved": False,
                    "unsavedChanges": False,
                    "discardedDraft": False,
                }
        except Exception:
            return {
                "ok": True,
                "left": True,
                "saved": False,
                "unsavedChanges": False,
                "discardedDraft": False,
            }
        blocked = self._unsaved_guard(page, discard=False)
        if blocked is None:
            return {
                "ok": True,
                "left": True,
                "saved": False,
                "unsavedChanges": False,
                "discardedDraft": False,
            }
        if discard:
            return {
                "ok": True,
                "left": True,
                "saved": False,
                "discardedDraft": True,
                "unsavedChanges": True,
                "current": blocked.get("current"),
                "baseline": blocked.get("baseline"),
                "message": "已按 discard 丢弃当前未保存改动并离开，未保存到服务器。",
            }
        return {**blocked, "left": False, "saved": False, "autoSaved": False,
                "message": "当前稿有未保存修改，已阻止离开；请明确保存并验收，或确认丢弃。"}


    def _update_body_segments(
        self,
        page,
        segments: list[str],
        plain_texts: list[str],
        styles: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """用 cell.style 更新已有正文；多余文本 cell 清空后由统一清理函数删除。"""
        items = []
        for index, plain in enumerate(plain_texts):
            style = styles[index] if index < len(styles) else {}
            items.append(
                {
                    "text": "<p>" + escape(str(plain or "")).replace("\n", "<br>") + "</p>",
                    "style": style if isinstance(style, dict) else {},
                }
            )
        result = page.evaluate(UPDATE_BODY_CELLS_JS, {"items": items})
        if not isinstance(result, dict) or not result.get("ok"):
            detail = result.get("error") if isinstance(result, dict) else "未知错误"
            raise RuntimeError(f"更新正文 cell 失败：{detail}")
        missing = int(result.get("missing") or 0)
        if missing > 0:
            # 目标段数多于现有 cell：先用原生“点击空白区输入”补齐，再一次写样式。
            leaves = page.locator("div[contenteditable='true']").filter(
                has_not=page.locator("div[contenteditable='true']")
            )
            base = len(plain_texts) - missing
            for offset in range(missing):
                current = leaves
                last = current.nth(current.count() - 1)
                last.scroll_into_view_if_needed()
                box = last.bounding_box()
                if not box:
                    raise RuntimeError("无法定位最后一个正文 cell，停止更新。")
                before = current.count()
                click_x = box["x"] + box["width"] / 2
                click_y = box["y"] + box["height"] + 38
                page.mouse.click(click_x, click_y)
                page.wait_for_timeout(900)
                if current.count() <= before:
                    page.mouse.click(click_x, click_y + 30)
                    page.wait_for_timeout(900)
                if current.count() <= before:
                    raise RuntimeError("秀米未能在当前文本 cell 之后创建新 cell，更新未完成。")
                created = current.nth(current.count() - 1)
                created.click()
                page.keyboard.insert_text(plain_texts[base + offset])
                page.wait_for_timeout(800)
            result = page.evaluate(UPDATE_BODY_CELLS_JS, {"items": items})
            if not isinstance(result, dict) or not result.get("ok"):
                detail = result.get("error") if isinstance(result, dict) else "未知错误"
                raise RuntimeError(f"更新正文 cell 失败：{detail}")
            if int(result.get("missing") or 0) > 0:
                raise RuntimeError("正文 cell 数量仍不足，更新未完成。")
        return result

    def _find_visible_text(self, page, texts: list[str], exact: bool = False):
        for text in texts:
            try:
                loc = page.get_by_text(text, exact=exact)
                for index in range(loc.count()):
                    try:
                        element = loc.nth(index)
                        if element.is_visible():
                            return element, text
                    except Exception:
                        continue
            except Exception:
                continue
        return None, None

    def _body_element(self, page):
        handle = page.evaluate_handle(
            """
            () => {
              const cells = Array.from(document.querySelectorAll("div[contenteditable='true']"))
                .filter(el => !el.querySelector("div[contenteditable='true']"));
              const scored = cells.map(el => {
                const r = el.getBoundingClientRect();
                return {el, area: r.width * r.height, x: r.x, w: r.width, h: r.height};
              }).filter(item => item.w > 150 && item.h > 0)
                .sort((a, b) => b.area - a.area);
              return scored.length ? scored[0].el : null;
            }
            """
        )
        element = handle.as_element()
        if element is None:
            raise RuntimeError("未找到正文可编辑区域，请确认当前页面是图文编辑器。")
        return element

    def _click_save(self, page) -> bool:
        self._check_cancelled()
        selectors = [
            "button.save",
            "button.op-btn.save",
            ".op-btn.save",
            "button:has-text('保存')",
        ]
        for selector in selectors:
            try:
                loc = page.locator(selector)
                for index in range(min(loc.count(), 10)):
                    try:
                        element = loc.nth(index)
                        if element.is_visible():
                            element.click()
                            return True
                    except Exception:
                        continue
            except Exception:
                continue
        element, _ = self._find_visible_text(page, ["保存"], exact=True)
        if element is not None:
            try:
                element.click()
                return True
            except Exception:
                pass
        return False

    def _click_visible_text(self, page, texts: list[str], exact: bool = True) -> bool:
        element, _ = self._find_visible_text(page, texts, exact=exact)
        if element is None:
            return False
        try:
            self._check_cancelled()
            element.click(timeout=5000)
            return True
        except Exception:
            try:
                box = element.bounding_box()
                if box:
                    self._check_cancelled()
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    return True
            except Exception:
                pass
        return False

    def _ensure_library_page(self, page) -> None:
        """确保在图文编辑器内；没有编辑器时打开空白稿，绝不导航离开已有稿件。"""
        if self._is_auth_page(page) or login_button_visible(page):
            raise RuntimeError("未登录，无法浏览模板/素材库。请先在可见窗口完成登录。")
        if not self._editor_present(page):
            self._handle_restore_dialog(page)
            self._goto(page, EDITOR_URL, settle=6.0)
            self._handle_restore_dialog(page)
            if not self._wait_editor(page, 20000, dismiss_restore=True):
                if self._is_auth_page(page) or login_button_visible(page):
                    raise RuntimeError("未登录，无法浏览模板/素材库。请先在可见窗口完成登录。")
                raise RuntimeError("未进入图文编辑器，无法浏览模板/素材库。请先打开或新建一篇图文。")
            self._new_article = True
        if self._is_auth_page(page) or login_button_visible(page):
            raise RuntimeError("未登录，无法浏览模板/素材库。请先在可见窗口完成登录。")

    def _open_template_browser(self, page) -> None:
        try:
            modal = page.locator(".tn-dialog-template-comps-browser")
            for index in range(min(modal.count(), 3)):
                if modal.nth(index).is_visible():
                    return
        except Exception:
            pass
        if not self._click_visible_text(page, ["更多模板"], exact=True):
            raise RuntimeError("未找到「更多模板」入口，模板库可能未加载完成。")
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            time.sleep(1)
            try:
                modal = page.locator(".tn-dialog-template-comps-browser")
                for index in range(min(modal.count(), 3)):
                    if modal.nth(index).is_visible() and page.evaluate(TEMPLATE_CARDS_JS).get("cards"):
                        return
            except Exception:
                continue
        raise RuntimeError("模板库已打开，但未找到可识别的模板卡片（可能是页面改版或加载失败）。")

    def _close_template_browser(self, page) -> None:
        for selector in (
            ".browser-close-button",
            ".tn-tpl-browser-close",
            "button.browser-close",
            ".modal-close",
        ):
            try:
                loc = page.locator(selector)
                for index in range(min(loc.count(), 5)):
                    element = loc.nth(index)
                    if element.is_visible():
                        element.click(timeout=3000)
                        time.sleep(0.5)
                        return
            except Exception:
                continue
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass
        time.sleep(0.5)

    def _click_card_by_selector(self, page, selector: str, index: int) -> bool:
        self._check_cancelled()
        handle = page.evaluate_handle(
            """
            ([selector, index]) => {
              const cards = Array.from(document.querySelectorAll(selector));
              const el = cards[index];
              if (!el) return null;
              el.scrollIntoView({block: 'center', inline: 'nearest'});
              return el;
            }
            """,
            [selector, index],
        )
        element = handle.as_element()
        if element is None:
            return False
        try:
            element.scroll_into_view_if_needed(timeout=3000)
        except Exception:
            pass
        try:
            point = page.evaluate(
                """
                ([selector, index]) => {
                  const cards = Array.from(document.querySelectorAll(selector));
                  const el = cards[index];
                  if (!el) return null;
                  el.scrollIntoView({block: 'center', inline: 'nearest'});
                  const r = el.getBoundingClientRect();
                  const xs = [0.5, 0.3, 0.7, 0.15, 0.85];
                  const ys = [0.5, 0.7, 0.85, 0.3, 0.15];
                  for (const y of ys) {
                    for (const x of xs) {
                      const cx = r.x + r.width * x;
                      const cy = r.y + r.height * y;
                      if (cx < 1 || cy < 1 || cx > innerWidth - 1 || cy > innerHeight - 1) continue;
                      const top = document.elementFromPoint(cx, cy);
                      if (top && (el === top || el.contains(top))) return {x: cx, y: cy};
                    }
                  }
                  return {x: r.x + r.width / 2, y: r.y + r.height * 0.75};
                }
                """,
                [selector, index],
            )
            if point:
                self._check_cancelled()
                page.mouse.click(point["x"], point["y"])
                return True
        except Exception:
            pass
        try:
            self._check_cancelled()
            element.click(timeout=5000)
            return True
        except Exception:
            return False

    def _template_cards(self, page) -> dict[str, Any]:
        return page.evaluate(TEMPLATE_CARDS_JS)

    def _select_template_category(self, page, category: str) -> bool:
        clicked = bool(
            page.evaluate(
                """
                (name) => {
                  const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                  const el = Array.from(document.querySelectorAll('.tn-tpl-tag-lv1-child'))
                    .find(node => clean(node.innerText || '') === name && node.getBoundingClientRect().width > 0);
                  if (!el) return false;
                  el.click();
                  return true;
                }
                """,
                category,
            )
        )
        if not clicked:
            return False
        time.sleep(1)
        # 一级分类（如「标题」）是下拉菜单；尽量选其中的「全部标题」，
        # 避免只展开菜单而没有真正切换筛选。
        page.evaluate(
            """
            (name) => {
              const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
              const all = Array.from(document.querySelectorAll('.tn-tpl-tag-lv0-name'))
                .filter(node => node.getBoundingClientRect().width > 0);
              const preferred = all.find(node => clean(node.innerText || '') === `全部${name}`)
                || all.find(node => clean(node.innerText || '').includes(name));
              if (preferred) preferred.click();
            }
            """,
            category,
        )
        time.sleep(1)
        return True

    def _set_template_search(self, page, query: str) -> bool:
        try:
            loc = page.locator("input[placeholder*='搜索']")
            for index in range(min(loc.count(), 10)):
                element = loc.nth(index)
                if element.is_visible():
                    element.fill(query)
                    element.press("Enter")
                    time.sleep(3)
                    return True
        except Exception:
            pass
        return False

    def _open_asset_library(self, page) -> dict[str, Any]:
        self._close_template_browser(page)
        if not self._click_visible_text(page, ["图片素材"], exact=True):
            raise RuntimeError("未找到「图片素材」入口，素材库可能未加载完成。")
        time.sleep(3)
        data = page.evaluate(ASSET_CATEGORIES_JS)
        if not data.get("categories"):
            raise RuntimeError("素材库已打开，但未找到可识别的素材分类。")
        return data

    def _select_asset_category(self, page, name: str) -> bool:
        return bool(
            page.evaluate(
                """
                (name) => {
                  const clean = (s) => String(s || '').replace(/\\s+/g, ' ').trim();
                  const el = Array.from(document.querySelectorAll('.tn-tpl-tag-lv1-child'))
                    .find(node => clean(node.innerText || '') === name && node.getBoundingClientRect().width > 0);
                  if (!el) return false;
                  el.click();
                  return true;
                }
                """,
                name,
            )
        )

    def _asset_cards(self, page) -> dict[str, Any]:
        return page.evaluate(ASSET_CARDS_JS)

    def _editor_stats(self, page) -> dict[str, Any]:
        return page.evaluate(EDITOR_STATS_JS)

    def template_catalog(self, params: dict[str, Any]) -> dict[str, Any]:
        """读取/刷新模板库目录；默认命中缓存时不打开模板库面板。"""
        action = str(params.get("action") or "get").strip().lower()
        if action not in ("get", "refresh"):
            raise ValueError("template_catalog 的 action 必须是 get 或 refresh。")
        force = action == "refresh" or params.get("refresh") is True

        runtime = _catalog_read(CATALOG_RUNTIME_FILE)
        bundled = _catalog_read(CATALOG_BUNDLED_FILE)
        # 运行时缓存缺失时，用包内基线初始化一份用户态副本。
        if runtime is None and bundled is not None:
            _catalog_write(CATALOG_RUNTIME_FILE, bundled)

        if not force:
            if _catalog_is_valid(runtime, fresh=True):
                return {
                    "catalog": runtime,
                    "fromCache": True,
                    "source": "runtime",
                    "changed": False,
                    "diff": {},
                    "cacheWritten": False,
                    "message": "已命中用户态模板目录缓存，未打开模板库面板。",
                }
            if _catalog_is_valid(bundled, fresh=True):
                _catalog_write(CATALOG_RUNTIME_FILE, bundled)
                return {
                    "catalog": bundled,
                    "fromCache": True,
                    "source": "bundled",
                    "changed": False,
                    "diff": {},
                    "cacheWritten": True,
                    "message": "运行时缓存不可用，已从包内基线恢复并写入用户态缓存。",
                }

        page = self._require_page()
        self._ensure_library_page(page)
        self._open_template_browser(page)
        try:
            raw = page.evaluate(TEMPLATE_CATALOG_JS)
        finally:
            self._close_template_browser(page)
        live = _catalog_normalize(
            {
                "schemaVersion": CATALOG_SCHEMA_VERSION,
                "capturedAt": _catalog_now_iso(),
                "lastVerifiedAt": _catalog_now_iso(),
                "sourceUrl": raw.get("url") or "",
                "searchHint": raw.get("searchHint") or "",
                "sortOrder": raw.get("sortOrder") or "",
                "tags": raw.get("tags") or ["全部"],
                "groups": raw.get("groups") or [],
            }
        )
        if live is None:
            raise RuntimeError(
                "模板库已打开，但没有读到分组/分类结构（可能是页面改版或未登录）。"
            )

        previous = runtime if _catalog_is_valid(runtime, fresh=False) else bundled
        diff = _catalog_diff(previous, live)
        now = _catalog_now_iso()
        if previous is not None and not diff["changed"]:
            catalog = dict(previous)
            catalog["lastVerifiedAt"] = now
            catalog["entryCount"] = _catalog_entry_count(catalog["groups"])
        else:
            catalog = live
            catalog["capturedAt"] = now
            catalog["lastVerifiedAt"] = now
        written = _catalog_write(CATALOG_RUNTIME_FILE, catalog)
        if diff["changed"]:
            message = "模板目录已重新采集并覆盖运行时缓存。"
        else:
            message = "模板目录未发生结构性变化，只更新 lastVerifiedAt。"
        if not written:
            message += " 警告：用户态缓存写入失败，本次结果未持久化。"
        return {
            "catalog": catalog,
            "fromCache": False,
            "source": "live",
            "changed": bool(diff["changed"]),
            "diff": diff,
            "cacheWritten": written,
            "message": message,
        }

    def template_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """只读列出模板库；默认只返回免费模板，付费项只计数不返回。"""
        page = self._require_page()
        self._ensure_library_page(page)
        self._open_template_browser(page)
        category = str(params.get("category") or "").strip()
        query = str(params.get("query") or params.get("search") or "").strip()
        include_paid = params.get("includePaid") is True
        if category and category not in ("全部", "all", "*"):
            if not self._select_template_category(page, category):
                categories = page.evaluate(
                    """
                    () => Array.from(document.querySelectorAll('.tn-tpl-tag-lv1-child'))
                      .map(el => String(el.innerText || '').replace(/\\s+/g, ' ').trim())
                      .filter(Boolean)
                    """
                )
                raise RuntimeError(
                    f"模板分类不存在：{category}；可用分类：{'、'.join(dict.fromkeys(categories))}"
                )
            time.sleep(3)
        if query:
            if not self._set_template_search(page, query):
                raise RuntimeError("未找到模板搜索框，无法按关键词搜索。")
        data = self._template_cards(page)
        items: list[dict[str, Any]] = []
        skipped = 0
        for card in data.get("cards", []):
            if not card.get("free") and not include_paid:
                skipped += 1
                continue
            haystack = f"{card.get('title', '')} {card.get('preview', '')} {' '.join(card.get('tags') or [])}".lower()
            if query and query.lower() not in haystack:
                continue
            item = dict(card)
            item["category"] = category or "全部"
            items.append(item)
        items = items[:50]
        return {
            "url": data.get("url", ""),
            "title": data.get("title", ""),
            "category": category or "全部",
            "query": query,
            "items": items,
            "total": len(items),
            "skippedPaid": skipped,
            "skippedReasons": ["VIP/付费模板"] if skipped else [],
            "message": (
                f"已读取模板库，返回 {len(items)} 个免费模板"
                + (f"，跳过 {skipped} 个 VIP/付费模板" if skipped else "")
                + "。"
            ),
        }

    def template_apply(self, params: dict[str, Any]) -> dict[str, Any]:
        """把指定免费模板套用到当前图文；不保存、不发布。"""
        page = self._require_page()
        self._ensure_library_page(page)
        self._open_template_browser(page)
        template_id = str(params.get("templateId") or params.get("id") or "").strip()
        title_query = str(params.get("title") or "").strip()
        index_value = params.get("index")
        if title_query:
            self._set_template_search(page, title_query)
        data = self._template_cards(page)
        selected = None
        for card in data.get("cards", []):
            if template_id and str(card.get("id") or "") == template_id:
                selected = card
                break
            if title_query and title_query.lower() in str(card.get("title") or "").lower():
                selected = card
                break
            if index_value is not None:
                try:
                    if int(card.get("index")) == int(index_value):
                        selected = card
                        break
                except Exception:
                    pass
        if selected is None:
            raise RuntimeError("未找到指定模板，请先用 xiumi_template list 查看可用模板。")
        if not selected.get("free"):
            raise RuntimeError(f"模板「{selected.get('title')}」是 VIP/付费模板，已跳过，不会套用。")
        if not self._click_card_by_selector(page, ".tn-dialog-template-comps-browser .tn-tpl-item.tn-lighting-box", int(selected.get("domIndex", selected["index"]))):
            raise RuntimeError("无法点击指定模板卡片。")
        time.sleep(1.5)
        use_element, _ = self._find_visible_text(page, ["直接使用"], exact=True)
        if use_element is None:
            raise RuntimeError("已选中模板，但未出现「直接使用」菜单；请查看可见窗口确认页面是否改版。")
        try:
            self._check_cancelled()
            use_element.click(timeout=5000)
        except Exception as exc:
            raise RuntimeError(f"点击「直接使用」失败：{exc}") from exc
        time.sleep(3)
        self._close_template_browser(page)
        title, preview, editable = self._read_editor(page)
        return {
            "applied": True,
            "template": {
                "id": selected.get("id", ""),
                "title": selected.get("title", ""),
                "tags": selected.get("tags", []),
                "paid": False,
            },
            "editor": {"title": title, "textPreview": preview[:800], "editable": editable},
            "message": f"已套用免费模板「{selected.get('title')}」，尚未保存；请在可见窗口预览确认。",
        }

    def _cells_state(self, page) -> dict[str, Any]:
        data = page.evaluate(CELLS_STATE_JS)
        if not isinstance(data, dict) or not isinstance(data.get("cells"), list):
            raise RuntimeError("读取正文 cell 失败：页面没有返回可识别的 cell 列表。")
        return data

    def _component_cells(self, page) -> dict[str, Any]:
        """统计编辑器内全部 .tn-cell（含分割线/图片等无文字组件）。"""
        try:
            data = page.evaluate(COMPONENT_CELLS_JS)
        except Exception as exc:
            raise RuntimeError(f"读取组件 cell 失败：{exc}") from exc
        if not isinstance(data, dict) or not isinstance(data.get("cells"), list):
            raise RuntimeError("读取组件 cell 失败：页面没有返回可识别的组件列表。")
        return data

    @staticmethod
    def _cell_fingerprint(cell: dict[str, Any]) -> str:
        style = cell.get("style") if isinstance(cell.get("style"), dict) else {}
        return json.dumps(
            {"path": cell.get("path") or "", "text": cell.get("text") or "",
             "style": style},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def _cells_added(self, before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
        before_cells = before.get("cells") or []
        after_cells = after.get("cells") or []
        before_paths = {str(c.get("path") or "") for c in before_cells if c.get("path")}
        if before_paths:
            added = [c for c in after_cells if not c.get("path") or str(c.get("path")) not in before_paths]
        else:
            counts = Counter(self._cell_fingerprint(c) for c in before_cells)
            added = []
            for cell in after_cells:
                key = self._cell_fingerprint(cell)
                if counts[key] > 0:
                    counts[key] -= 1
                else:
                    added.append(cell)
        return added

    def _resolve_template_id(self, page, template_id: str) -> dict[str, Any] | None:
        """按 id 从模板接口定位模板，供未出现在当前列表时的通用回退。"""
        data = page.evaluate(
            """
            async (id) => {
              const base = '/api/templates?tag_ids&tag_category=paper-cp&match_level=default'
                + '&template_category=paper-cp&sort=DESC&limit=32&page=';
              for (let page = 0; page < 80; page++) {
                const resp = await fetch(base + page, {credentials: 'include'});
                if (!resp.ok) return {error: 'HTTP ' + resp.status, page};
                const payload = await resp.json();
                const items = (payload.data && payload.data.templates) || [];
                const hit = items.find(item => String(item.template_id) === String(id));
                if (hit) {
                  return {
                    page,
                    templateId: String(hit.template_id),
                    tags: (hit.tags || []).map(tag => String(tag.tag_name || '')).filter(Boolean),
                    name: String(hit.display_name || '')
                  };
                }
                if (items.length < 32) break;
              }
              return null;
            }
            """,
            template_id,
        )
        return data if isinstance(data, dict) else None

    def _component_template_card(self, page, spec: dict[str, Any]) -> dict[str, Any]:
        template_id = str(spec.get("templateId") or spec.get("id") or "").strip()
        query = str(spec.get("query") or "").strip()
        index_value = spec.get("index")
        if not template_id and not query:
            raise ValueError("组件插入需要 templateId，或 query + index 定位模板。")
        self._open_template_browser(page)
        if query:
            if not self._set_template_search(page, query):
                raise RuntimeError("未找到模板搜索框，无法按关键词定位组件。")
        data = self._template_cards(page)
        cards = data.get("cards") or []
        selected = None
        if template_id:
            selected = next((c for c in cards if str(c.get("id") or "") == template_id), None)
        if selected is None and query and index_value is not None:
            try:
                selected = next((c for c in cards if int(c.get("index")) == int(index_value)), None)
            except Exception:
                selected = None
        if selected is None and template_id and not query:
            resolved = self._resolve_template_id(page, template_id)
            tags = [
                tag for tag in ((resolved or {}).get("tags") or [])
                if tag and not tag.startswith("$@") and tag not in ("paper-cp", "全部")
            ]
            for tag in sorted(set(tags), key=lambda value: (-len(value), value))[:6]:
                if not self._set_template_search(page, tag):
                    break
                time.sleep(2)
                cards = self._template_cards(page).get("cards") or []
                selected = next((c for c in cards if str(c.get("id") or "") == template_id), None)
                if selected is not None:
                    break
        if selected is None:
            available = "、".join(
                f"{c.get('id') or '?'} {c.get('title') or ''}".strip() for c in cards[:8]
            )
            raise RuntimeError(
                f"未找到组件模板（templateId={template_id or '-'}，query={query or '-'}，"
                f"index={index_value if index_value is not None else '-'}）；"
                f"当前可见模板：{available or '无'}。请先用 xiumi_template search 确认模板。"
            )
        if not selected.get("free"):
            raise RuntimeError(f"模板「{selected.get('title')}」是 VIP/付费模板，已跳过，不会套用。")
        if not self._click_card_by_selector(
            page,
            ".tn-dialog-template-comps-browser .tn-tpl-item.tn-lighting-box",
            int(selected.get("domIndex", selected.get("index", 0))),
        ):
            raise RuntimeError("无法点击指定组件模板卡片。")
        time.sleep(1.5)
        use_element, _ = self._find_visible_text(page, ["直接使用"], exact=True)
        if use_element is None:
            raise RuntimeError("已选中组件模板，但未出现「直接使用」菜单；请查看可见窗口确认页面是否改版。")
        self._check_cancelled()
        use_element.click(timeout=5000)
        time.sleep(3)
        self._close_template_browser(page)
        return selected

    def _fill_component_text(self, page, cells: list[dict[str, Any]],
                             mappings: list[dict[str, Any]]) -> list[dict[str, Any]]:
        cells = [cell for cell in cells if str(cell.get("type") or "text") == "text"]
        filled: list[dict[str, Any]] = []
        used: set[int] = set()
        for mapping in mappings:
            self._check_cancelled()
            if not isinstance(mapping, dict):
                continue
            text = str(mapping.get("text") or "")
            original = mapping.get("original")
            original = str(original) if original is not None else None
            cell_index = mapping.get("cellIndex")
            target: dict[str, Any] | None = None
            local_index = -1
            if cell_index is not None:
                try:
                    local_index = int(cell_index)
                    if 0 <= local_index < len(cells) and local_index not in used:
                        target = cells[local_index]
                except Exception:
                    target = None
            elif original is not None:
                for idx, cell in enumerate(cells):
                    if idx in used:
                        continue
                    plain = str(cell.get("plain") or "")
                    if original == plain or (original and (original in plain or (plain and plain in original))):
                        target = cell
                        local_index = idx
                        break
            else:
                for idx, cell in enumerate(cells):
                    if idx not in used:
                        target = cell
                        local_index = idx
                        break
            entry: dict[str, Any] = {
                "cellIndex": local_index,
                "original": original if original is not None else "",
                "text": text,
                "ok": False,
            }
            if target is None:
                entry["reason"] = (
                    f"新增 cell 中未找到文本「{original}」" if original is not None
                    else "没有可填充的新增 cell"
                )
                filled.append(entry)
                continue
            used.add(local_index)
            html = "<p>" + escape(text).replace("\n", "<br>") + "</p>"
            self._check_cancelled()
            # 组件刚插入时 Angular 还在异步渲染，dump 时的 plain 与此刻可能不同；
            # path 已经能唯一定位，这里不再用旧 plain 做前置校验（否则必然误报「文本已变化」）。
            result = page.evaluate(
                COMPONENT_SET_TEXT_JS,
                [str(target.get("path") or ""), int(target.get("order", local_index)),
                 html, ""],
            )
            if not isinstance(result, dict) or not result.get("ok"):
                entry["reason"] = str((result or {}).get("error") or "写入 cell.text 失败")
                entry["cellIndex"] = local_index
                filled.append(entry)
                continue
            entry.update({"ok": True, "actualText": str(result.get("actual") or ""),
                          "globalIndex": int(target.get("order", local_index))})
            filled.append(entry)
        return filled

    def component_insert(self, params: dict[str, Any]) -> dict[str, Any]:
        """按模板插入免费组件，并只改模型 text 填充占位文字；不保存、不发布。"""
        page = self._require_page()
        if self._is_auth_page(page) or login_button_visible(page):
            raise RuntimeError("未登录，无法插入组件。请先在可见窗口完成登录。")
        if not self._editor_present(page):
            raise RuntimeError("当前不是秀米图文编辑器；请先 article_new 或 article_open 再插入组件。")

        raw_components = params.get("components")
        specs: list[dict[str, Any]] = []
        if isinstance(raw_components, list) and raw_components:
            for item in raw_components:
                if not isinstance(item, dict):
                    raise ValueError("components 的每一项必须是对象。")
                spec = dict(item)
                if "setText" not in spec and params.get("setText"):
                    spec["setText"] = params.get("setText")
                specs.append(spec)
        else:
            specs = [dict(params)]
        for spec in specs:
            self._check_cancelled()
            if not str(spec.get("templateId") or spec.get("id") or "").strip() and not str(spec.get("query") or "").strip():
                raise ValueError("每个组件需要 templateId，或 query + index。")

        initial = self._cells_state(page)
        initial_components = self._component_cells(page)
        cell_count_before = int(initial.get("count") or 0)
        component_count_before = int(initial_components.get("count") or 0)
        all_new: list[dict[str, Any]] = []
        all_filled: list[dict[str, Any]] = []
        component_results: list[dict[str, Any]] = []
        inserted_components = 0
        for spec in specs:
            before = self._cells_state(page)
            self._check_cancelled()
            before_components = self._component_cells(page)
            before_component_count = int(before_components.get("count") or 0)
            selected = self._component_template_card(page, spec)
            time.sleep(2)
            after = self._cells_state(page)
            added = self._cells_added(before, after)
            after_component_count = int(self._component_cells(page).get("count") or 0)
            component_delta = max(0, after_component_count - before_component_count)
            if not added and component_delta <= 0:
                raise RuntimeError(
                    "组件已点击「直接使用」，但没有识别到新增 cell；"
                    "未写入任何占位文字，请查看可见窗口确认组件是否插入。"
                )
            mappings = spec.get("setText") or []
            if not isinstance(mappings, list):
                raise ValueError("setText 必须是数组。")
            filled = self._fill_component_text(page, added, mappings) if mappings else []
            all_new.extend(added)
            all_filled.extend(filled)
            inserted_components += 1
            component_results.append({
                "template": {
                    "id": selected.get("id", ""),
                    "title": selected.get("title", ""),
                    "tags": selected.get("tags", []),
                    "paid": False,
                },
                "inserted": 1,
                "cellIndexes": [int(c.get("order", 0)) for c in added],
                "componentCellCountBefore": before_component_count,
                "componentCellCountAfter": after_component_count,
                "filled": filled,
            })
            time.sleep(0.8)

        final = self._cells_state(page)
        final_components = self._component_cells(page)
        cell_count_after = int(final.get("count") or 0)
        component_count_after = int(final_components.get("count") or 0)
        screenshot_path = ""
        try:
            shot = self.screenshot({"name": "component-insert"})
            screenshot_path = str(shot.get("path") or "")
        except Exception as exc:
            _log(f"组件插入成功截图失败: {type(exc).__name__}: {exc}")
        ok = all(item.get("ok") for item in all_filled) if all_filled else True
        inserted_count = inserted_components
        return {
            "ok": ok,
            "inserted": inserted_count,
            "cellIndexes": [int(c.get("order", 0)) for c in all_new],
            "filled": all_filled,
            "cellCountBefore": cell_count_before,
            "cellCountAfter": cell_count_after,
            "componentCellCountBefore": component_count_before,
            "componentCellCountAfter": component_count_after,
            "components": component_results,
            "screenshotPath": screenshot_path,
            "message": (
                f"已插入 {inserted_components} 个免费组件（组件 cell 数 "
                f"{component_count_before}→{component_count_after}），"
                f"填充 {sum(1 for item in all_filled if item.get('ok'))}/{len(all_filled)} 处文字；尚未保存。"
            ),
        }

    def asset_categories(self) -> dict[str, Any]:
        page = self._require_page()
        self._ensure_library_page(page)
        data = self._open_asset_library(page)
        return {
            "url": data.get("url", ""),
            "categories": data.get("categories", []),
            "message": f"已读取 {len(data.get('categories', []))} 个素材分类。",
        }

    def asset_list(self, params: dict[str, Any]) -> dict[str, Any]:
        page = self._require_page()
        self._ensure_library_page(page)
        data = self._open_asset_library(page)
        categories = data.get("categories", [])
        category = str(params.get("category") or "").strip()
        if not category:
            category = categories[0]["name"] if categories else ""
        if not category:
            raise RuntimeError("素材库没有可用分类。")
        if not self._select_asset_category(page, category):
            names = "、".join(item.get("name", "") for item in categories)
            raise RuntimeError(f"素材分类不存在：{category}；可用分类：{names}")
        time.sleep(2)
        subcategory = str(params.get("subcategory") or "").strip()
        if subcategory:
            if not self._select_asset_category(page, subcategory):
                raise RuntimeError(f"素材子分类不存在：{subcategory}")
            time.sleep(3)
        cards_data = self._asset_cards(page)
        items: list[dict[str, Any]] = []
        skipped = 0
        for card in cards_data.get("cards", []):
            if not card.get("free"):
                skipped += 1
                continue
            item = dict(card)
            item["category"] = category
            item["subcategory"] = subcategory
            items.append(item)
        items = items[:50]
        return {
            "url": cards_data.get("url", ""),
            "category": category,
            "subcategory": subcategory,
            "items": items,
            "total": len(items),
            "skippedPaid": skipped,
            "skippedReasons": ["VIP/付费素材"] if skipped else [],
            "message": (
                f"已读取素材分类「{category}」下的 {len(items)} 个免费素材"
                + (f"，跳过 {skipped} 个 VIP/付费素材" if skipped else "")
                + "。"
            ),
        }

    def asset_insert(self, params: dict[str, Any]) -> dict[str, Any]:
        """把指定免费素材插入当前光标位置；不保存、不发布。"""
        page = self._require_page()
        self._ensure_library_page(page)
        data = self._open_asset_library(page)
        categories = data.get("categories", [])
        category = str(params.get("category") or "").strip()
        if not category:
            category = categories[0]["name"] if categories else ""
        if not category or not self._select_asset_category(page, category):
            raise RuntimeError(f"素材分类不存在：{category or '空'}")
        time.sleep(2)
        subcategory = str(params.get("subcategory") or "").strip()
        if subcategory:
            if not self._select_asset_category(page, subcategory):
                raise RuntimeError(f"素材子分类不存在：{subcategory}")
            time.sleep(3)
        cards = self._asset_cards(page).get("cards", [])
        if not cards:
            raise RuntimeError("当前素材分类下没有可用素材。")
        index_value = params.get("index", 0)
        try:
            index = int(index_value)
        except Exception:
            index = 0
        if index < 0 or index >= len(cards):
            raise RuntimeError(f"素材序号超出范围：{index}，当前共 {len(cards)} 个素材。")
        selected = cards[index]
        if not selected.get("free"):
            raise RuntimeError(f"素材「{selected.get('title')}」是 VIP/付费素材，已跳过，不会插入。")
        before = self._editor_stats(page)
        if not self._click_card_by_selector(
            page, ".tn-tpl-item.tn-lighting-box.tn-tpl-material", int(selected.get("domIndex", index))
        ):
            raise RuntimeError("无法点击指定素材卡片。")
        time.sleep(4)
        after = self._editor_stats(page)
        _log(f"asset insert stats before={before} after={after}")
        inserted = (
            after.get("imageCells", 0) > before.get("imageCells", 0)
            or after.get("sectionImages", 0) > before.get("sectionImages", 0)
            or (after.get("hasComponentToolbar") and not before.get("hasComponentToolbar"))
        )
        title, preview, editable = self._read_editor(page)
        return {
            "inserted": bool(inserted),
            "asset": {
                "index": selected.get("index"),
                "title": selected.get("title", ""),
                "thumbnail": selected.get("thumbnail", ""),
                "paid": False,
            },
            "editor": {"title": title, "textPreview": preview[:800], "editable": editable},
            "message": (
                "素材已插入当前光标位置，尚未保存；请在可见窗口预览确认。"
                if inserted
                else "已点击素材，但未检测到画布变化；请查看可见窗口确认是否插入。"
            ),
        }

    def _gallery_cards(self, page) -> dict[str, Any]:
        return page.evaluate(GALLERY_CARDS_JS)

    def _open_my_gallery(self, page, album: str = "") -> dict[str, Any]:
        """打开「我的图库」；只切到已存在的相册，不新建/删除相册。"""
        self._ensure_library_page(page)
        if not self._click_visible_text(page, ["我的图库"], exact=True):
            raise RuntimeError("未找到「我的图库」入口，请确认当前在图文编辑器内。")
        time.sleep(2.5)
        if album and album not in ("未标签图片", "全部"):
            if not self._click_visible_text(page, [album], exact=True):
                raise RuntimeError(f"我的图库中没有找到相册「{album}」。")
            time.sleep(2)
        deadline = time.monotonic() + 15
        data: dict[str, Any] = {}
        while time.monotonic() < deadline:
            data = self._gallery_cards(page)
            if data.get("hasUpload"):
                return data
            time.sleep(1)
        raise RuntimeError("我的图库已打开，但没有找到图片上传入口（可能是页面改版或加载失败）。")

    def _upload_paths(
        self, page, paths: list[Path], album: str = ""
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        """把本地图片送入图库；返回逐项结果与上传后的图库快照。"""
        self._open_my_gallery(page, album)
        before = self._gallery_cards(page)
        before_cards = before.get("cards") or []
        before_used = int((before.get("capacity") or {}).get("used") or len(before_cards))
        limit = int((before.get("capacity") or {}).get("limit") or 0)
        if limit and before_used + len(paths) > limit:
            raise RuntimeError(
                f"我的图库容量不足：当前 {before_used}/{limit}，本次需要 {len(paths)} 张。"
                " 请先在可见窗口清理图库或减少上传数量。"
            )

        loc = page.locator("input[type=file][accept*='image']")
        if loc.count() == 0:
            raise RuntimeError("没有找到图库的图片上传控件（可能未登录或页面改版）。")
        # 文件选择器在页面中通常是隐藏的，set_input_files 可直接驱动。
        self._check_cancelled()
        loc.first.set_input_files([str(path) for path in paths])
        _log(f"gallery upload started files={len(paths)}")

        expected = {path.name for path in paths}
        deadline = time.monotonic() + 60
        state = self._gallery_cards(page)
        seen: set[str] = set()
        while time.monotonic() < deadline:
            self._check_cancelled()
            state = self._gallery_cards(page)
            seen = {str(card.get("name") or "") for card in (state.get("cards") or [])}
            used = int((state.get("capacity") or {}).get("used") or 0)
            if expected.issubset(seen) and used >= before_used + len(paths):
                break
            time.sleep(1)

        cards = state.get("cards") or []
        cards_by_name: dict[str, dict[str, Any]] = {}
        for card in cards:
            name = str(card.get("name") or "")
            if name and name not in cards_by_name:
                cards_by_name[name] = card

        items: list[dict[str, Any]] = []
        for path in paths:
            card = cards_by_name.get(path.name)
            if card is None:
                items.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "ok": False,
                        "id": "",
                        "url": "",
                        "error": "上传后未在图库看到该文件（可能格式或大小不被秀米接受）。",
                    }
                )
            else:
                items.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "ok": True,
                        "id": card.get("id", ""),
                        "url": card.get("url", ""),
                        "size": card.get("size", path.stat().st_size),
                        "error": "",
                    }
                )
        return items, state

    def upload_images(self, params: dict[str, Any]) -> dict[str, Any]:
        """上传用户本地图片到「我的图库」；只写入图库，不保存/发布图文。"""
        if params.get("confirm") is not True:
            raise ValueError("上传图片会写入「我的图库」，需要 confirm=true 确认。")
        raw_paths = params.get("paths")
        if isinstance(raw_paths, str):
            raw_paths = [raw_paths]
        if not isinstance(raw_paths, list) or not raw_paths:
            raise ValueError("upload_images 需要非空的 paths 数组。")
        if len(raw_paths) > 20:
            raise ValueError("一次最多上传 20 张图片。")

        items: list[dict[str, Any]] = []
        valid: list[Path] = []
        for raw in raw_paths:
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                path = path.resolve()
            suffix = path.suffix.lower()
            if suffix not in IMAGE_UPLOAD_EXTS:
                items.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "ok": False,
                        "id": "",
                        "url": "",
                        "error": f"不支持的图片扩展名：{suffix or '无扩展名'}；允许 {', '.join(sorted(IMAGE_UPLOAD_EXTS))}。",
                    }
                )
                continue
            if not path.is_file():
                items.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "ok": False,
                        "id": "",
                        "url": "",
                        "error": "文件不存在或不是普通文件。",
                    }
                )
                continue
            size = path.stat().st_size
            if size > IMAGE_UPLOAD_MAX_BYTES:
                items.append(
                    {
                        "path": str(path),
                        "name": path.name,
                        "ok": False,
                        "id": "",
                        "url": "",
                        "error": f"文件 {size} 字节，超过单文件上限 {IMAGE_UPLOAD_MAX_BYTES} 字节（10MB）。",
                    }
                )
                continue
            valid.append(path)

        if not valid:
            return {
                "uploaded": 0,
                "total": 0,
                "failed": len(items),
                "items": items,
                "capacity": None,
                "message": "没有可上传的图片；请检查路径、扩展名与大小。",
            }

        page = self._require_page()
        blocked = self._quota_guard_images(
            len(valid),
            params.get("limitOverride") if isinstance(params.get("limitOverride"), dict) else None,
        )
        if blocked is not None:
            return blocked
        uploaded_items, state = self._upload_paths(page, valid, str(params.get("album") or ""))
        self._quota_cache = None
        self._quota_cache_at = 0.0
        items.extend(uploaded_items)
        ok_count = sum(1 for item in items if item.get("ok"))
        return {
            "uploaded": ok_count,
            "total": len(items),
            "failed": len(items) - ok_count,
            "items": items,
            "capacity": state.get("capacity"),
            "message": (
                f"已上传 {ok_count}/{len(items)} 张图片到「我的图库」"
                + ("；尚未保存或发布任何图文。" if ok_count else "；请查看失败原因。")
            ),
        }

    def _select_text_target(
        self, page, after_text: str = "", position: Any = None
    ) -> dict[str, Any] | None:
        cells = page.evaluate(
            """
            () => Array.from(document.querySelectorAll('.tn-cell-text'))
              .filter(el => {
                const r = el.getBoundingClientRect();
                const text = String(el.innerText || '').replace(/\\s+/g, ' ').trim();
                return r.width > 80 && r.height > 8 && r.x > 200 && text;
              })
              .map((el, index) => {
                const r = el.getBoundingClientRect();
                return {index, text: String(el.innerText || '').replace(/\\s+/g, ' ').trim().slice(0, 160),
                        x: Math.round(r.x), y: Math.round(r.y), w: Math.round(r.width), h: Math.round(r.height)};
              })
            """
        )
        if not cells:
            return None
        chosen = None
        requested_anchor = ""
        if after_text:
            needle = str(after_text).strip()
            requested_anchor = needle
            if needle:
                chosen = next((cell for cell in cells if needle in cell["text"]), None)
                if chosen is None:
                    return {
                        "error": "当前正文未找到 anchorText，未插入图片。",
                        "available": [cell["text"] for cell in cells][:10],
                    }
        elif position is not None and str(position).strip().lower() not in ("", "end", "bottom", "last"):
            value = str(position).strip().lower()
            if value in ("top", "start", "first"):
                chosen = cells[0]
            else:
                try:
                    idx = int(value)
                except Exception:
                    idx = -1
                if 0 <= idx < len(cells):
                    chosen = cells[idx]
        if chosen is None:
            chosen = cells[-1]
        chosen["requestedAnchor"] = requested_anchor

        # 关键：必须把光标定位到「锚点文字之后」，否则整篇正文是同一个 cell，
        # 每张图都会追加到 cell 末尾，看起来全堆在文章最后。
        placed = False
        if requested_anchor:
            placed = self._place_caret_after_text(page, requested_anchor)
        if not placed:
            page.mouse.click(chosen["x"] + min(24, chosen["w"] / 2), chosen["y"] + chosen["h"] / 2)
            self._collapse_caret_to_end(page)
        time.sleep(0.8)
        chosen["caretPlacedAfterAnchor"] = bool(placed)
        return chosen

    def _place_caret_after_text(self, page, needle: str) -> bool:
        """在正文里找到 needle，点中它，并把 caret 收到该文字之后。

        注意：必须用「所有 text node 拼接后的文本」来算 index，
        不能用 innerText —— 两者空白处理不同，offset 会错位，
        导致 caret 落在锚点文字中间，插图把文字切断。
        """
        probe_js = """
            ([needle]) => {
              const cells = Array.from(document.querySelectorAll('.tn-cell-text'))
                .filter(el => {
                  const r = el.getBoundingClientRect();
                  return r.width > 80 && r.height > 8 && r.x > 200;
                });
              for (const el of cells) {
                const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                const nodes = [];
                let all = '';
                while (true) {
                  const n = walker.nextNode();
                  if (!n) break;
                  nodes.push({node: n, start: all.length});
                  all += (n.nodeValue || '');
                }
                const idx = all.indexOf(needle);
                if (idx < 0) continue;
                const end = idx + needle.length;
                let target = null;
                for (const item of nodes) {
                  const len = (item.node.nodeValue || '').length;
                  if (end <= item.start + len && end > item.start) {
                    target = {node: item.node, offset: end - item.start};
                    break;
                  }
                }
                if (!target) continue;
                const range = document.createRange();
                range.setStart(target.node, target.offset);
                range.collapse(true);
                const rects = Array.from(range.getClientRects()).filter(r => r.width >= 0 && r.height > 0);
                let r = rects.length ? rects[rects.length - 1] : range.getBoundingClientRect();
                if (!r || r.height <= 0) {
                  const elRect = el.getBoundingClientRect();
                  r = {x: elRect.x + 8, y: elRect.y + elRect.height / 2, width: 2, height: 12};
                }
                return {x: r.x + (r.width ? r.width / 2 : 1), y: r.y + r.height / 2, found: true};
              }
              return {found: false};
            }
        """
        try:
            hit = page.evaluate(probe_js, [needle])
        except Exception:
            return False
        if not hit or not hit.get("found"):
            return False
        try:
            page.mouse.click(float(hit["x"]), float(hit["y"]))
            time.sleep(0.25)
            return bool(
                page.evaluate(
                    """
                    ([needle]) => {
                      const cells = Array.from(document.querySelectorAll('.tn-cell-text'));
                      for (const el of cells) {
                        const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
                        const nodes = [];
                        let all = '';
                        while (true) {
                          const n = walker.nextNode();
                          if (!n) break;
                          nodes.push({node: n, start: all.length});
                          all += (n.nodeValue || '');
                        }
                        const idx = all.indexOf(needle);
                        if (idx < 0) continue;
                        const end = idx + needle.length;
                        for (const item of nodes) {
                          const len = (item.node.nodeValue || '').length;
                          if (end <= item.start + len && end > item.start) {
                            const sel = window.getSelection();
                            if (!sel) return false;
                            const range = document.createRange();
                            range.setStart(item.node, end - item.start);
                            range.collapse(true);
                            sel.removeAllRanges();
                            sel.addRange(range);
                            return true;
                          }
                        }
                      }
                      return false;
                    }
                    """,
                    [needle],
                )
            )
        except Exception:
            return False

    def _collapse_caret_to_end(self, page) -> bool:
        """把光标收到当前 contenteditable 末尾，避免插图把段落从中间切开。"""
        try:
            return bool(
                page.evaluate(
                    """
                    () => {
                      const el = document.activeElement;
                      if (!el || !el.isContentEditable) return false;
                      const sel = window.getSelection();
                      if (!sel) return false;
                      const range = document.createRange();
                      range.selectNodeContents(el);
                      range.collapse(false);
                      sel.removeAllRanges();
                      sel.addRange(range);
                      return true;
                    }
                    """
                )
            )
        except Exception:
            return False

    def _canvas_image_count(self, page) -> int:
        """只数正文画布内的图片 cell，排除左侧模板库/图库缩略图。

        不要数 ``img`` 标签：秀米正文里的图片常是懒加载或背景图，``img``
        矩形的宽高经常读不到；而图片 cell 的容器 ``.tn-cell-image`` 尺寸
        稳定，是可靠的观测点。
        """
        try:
            return int(
                page.evaluate(
                    """
                    () => {
                      const root = document.querySelector('.tn-working-pallet');
                      if (!root) return -1;
                      return Array.from(root.querySelectorAll('.tn-cell-image'))
                        .filter(el => {
                          const r = el.getBoundingClientRect();
                          return r.width > 8 && r.height > 8;
                        }).length;
                    }
                    """
                )
            )
        except Exception:
            return -1

    def _insert_gallery_card(self, page, card: dict[str, Any]) -> dict[str, Any]:
        # 插图被判成功要看「正文画布里的 img 真的多了」，而不是全局计数
        # （全局 imageCells 会混入左侧模板库/图库缩略图，之前的实现因此假阳性）
        before_canvas = self._canvas_image_count(page)
        before = self._editor_stats(page)
        self._check_cancelled()
        index = int(card.get("domIndex", card.get("index", 0)))
        if not self._click_card_by_selector(page, GALLERY_SELECTOR, index):
            raise RuntimeError("无法点击图库图片。")
        canvas_delta = 0
        attempts = []

        def _wait_delta(seconds: float) -> int:
            waited = 0.0
            while waited < seconds:
                time.sleep(0.4)
                waited += 0.4
                now = self._canvas_image_count(page)
                if before_canvas >= 0 and now > before_canvas:
                    return now - before_canvas
            return 0

        canvas_delta = _wait_delta(9.0)
        attempts.append({"step": "single_click", "delta": canvas_delta})
        after = self._editor_stats(page)
        delta = int(after.get("imageCells", 0)) - int(before.get("imageCells", 0))
        inserted = canvas_delta > 0
        return {
            "inserted": bool(inserted),
            "canvasDelta": canvas_delta,
            "delta": delta,
            "before": before,
            "after": after,
        }

    def _gallery_card_for(self, page, params: dict[str, Any]) -> dict[str, Any]:
        cards = self._gallery_cards(page).get("cards") or []
        image_id = str(params.get("imageId") or "").strip()
        raw_path = str(params.get("path") or "").strip()
        selector = str(params.get("selector") or "").strip()
        basename = Path(raw_path).name if raw_path else ""
        target = None
        if image_id:
            target = next((card for card in cards if str(card.get("id") or "") == image_id), None)
        elif basename:
            target = next((card for card in cards if str(card.get("name") or "") == basename), None)
        elif selector:
            target = next((card for card in cards if str(card.get("id") or "") == selector), None)
            if target is None:
                target = next(
                    (card for card in cards if str(card.get("name") or "") == Path(selector).name),
                    None,
                )
            if target is None:
                try:
                    idx = int(selector)
                except Exception:
                    idx = -1
                target = next((card for card in cards if int(card.get("index", -1)) == idx), None)
        elif params.get("index") is not None:
            try:
                idx = int(params.get("index"))
            except Exception:
                idx = -1
            target = next((card for card in cards if int(card.get("index", -1)) == idx), None)
        if target is None:
            hint = image_id or basename or selector or str(params.get("index"))
            raise RuntimeError(
                f"我的图库中未找到图片「{hint}」；请先 xiumi_upload 上传，再用 imageId/path/selector/index 指定。"
            )
        return target

    def image_insert(self, params: dict[str, Any]) -> dict[str, Any]:
        """把已上传的图库图片插入当前图文；只插入，不保存/发布。"""
        if params.get("confirm") is not True:
            raise ValueError("插入图片会修改当前图文，需要 confirm=true 确认。")
        page = self._require_page()
        self._open_my_gallery(page, str(params.get("album") or ""))
        card = self._gallery_card_for(page, params)
        target = self._select_text_target(
            page, str(params.get("afterText") or ""), params.get("position")
        )
        if target is None:
            raise RuntimeError("当前正文没有可定位的文本单元格，无法插入图片。")
        if target.get("error"):
            return {
                "inserted": False,
                "reason": "anchor_not_found",
                "image": {"id": card.get("id", ""), "name": card.get("name", "")},
                "target": target,
                "message": target["error"],
            }
        result = self._insert_gallery_card(page, card)
        title, preview, editable = self._read_editor(page)
        return {
            "inserted": bool(result["inserted"]),
            "reason": "" if result["inserted"] else "editor_not_changed",
            "image": {
                "id": card.get("id", ""),
                "name": card.get("name", ""),
                "url": card.get("url", ""),
            },
            "target": {
                "anchorText": target.get("requestedAnchor") or target.get("text", ""),
                "matchedText": target.get("text", ""),
                "cellIndex": target.get("index"),
            },
            "editor": {
                "title": title,
                "textPreview": preview[:800],
                "editable": editable,
                "imageCells": result["after"].get("imageCells"),
            },
            "message": (
                "图片已插入正文，尚未保存；请在可见窗口确认位置与效果。"
                if result["inserted"]
                else "已点击图库图片，但未检测到画布新增图片；请查看可见窗口确认是否插入。"
            ),
        }

    def _normalize_manifest(
        self, raw: Any, base_dir: Path
    ) -> tuple[list[dict[str, Any]] | None, str]:
        if isinstance(raw, dict):
            if isinstance(raw.get("items"), list):
                entries = raw["items"]
            elif isinstance(raw.get("images"), list):
                images = raw["images"]
                anchors = raw.get("anchors")
                if anchors is None or not isinstance(anchors, list):
                    return None, "manifest.images 存在但缺少 anchors 数组。"
                if len(images) != len(anchors):
                    return None, f"manifest 图片数 {len(images)} 与 anchor 数 {len(anchors)} 不一致。"
                entries = []
                for image, anchor in zip(images, anchors):
                    if isinstance(image, dict):
                        item = dict(image)
                    else:
                        item = {"path": image}
                    item["anchorText"] = anchor
                    entries.append(item)
            else:
                return None, "manifest 需要 items 或 images 数组。"
        elif isinstance(raw, list):
            entries = raw
        else:
            return None, "manifest 必须是 JSON 数组或对象。"

        normalized: list[dict[str, Any]] = []
        for index_value, entry in enumerate(entries):
            if not isinstance(entry, dict):
                return None, f"manifest 第 {index_value + 1} 项不是对象。"
            raw_path = (
                entry.get("path")
                or entry.get("absolutePath")
                or entry.get("image")
                or entry.get("file")
            )
            anchor = entry.get("anchorText") or entry.get("anchor") or entry.get("text")
            if not raw_path:
                return None, f"manifest 第 {index_value + 1} 项缺少 path/image。"
            if not anchor or not str(anchor).strip():
                return None, f"manifest 第 {index_value + 1} 项缺少 anchorText。"
            path = Path(str(raw_path)).expanduser()
            if not path.is_absolute():
                path = (base_dir / path).resolve()
            normalized.append(
                {
                    "path": str(path),
                    "anchorText": str(anchor).strip(),
                    "index": index_value,
                }
            )
        return normalized, ""

    def image_manifest(self, params: dict[str, Any]) -> dict[str, Any]:
        """按 manifest 批量上传并插图；数量/锚点不一致时明确失败，不保存。"""
        if params.get("confirm") is not True:
            raise ValueError("按 manifest 批量插入会修改图文，需要 confirm=true 确认。")
        raw = params.get("manifest")
        base_dir = Path.cwd()
        if isinstance(raw, str):
            manifest_path = Path(raw).expanduser()
            if not manifest_path.is_absolute():
                manifest_path = manifest_path.resolve()
            if not manifest_path.is_file():
                raise RuntimeError(f"manifest 文件不存在：{manifest_path}")
            base_dir = manifest_path.parent
            try:
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception as exc:
                raise RuntimeError(f"manifest JSON 读取失败：{exc}") from exc
        entries, error = self._normalize_manifest(raw, base_dir)
        if entries is None:
            return {
                "inserted": False,
                "manifestConsistent": False,
                "insertedCount": 0,
                "failedCount": 0,
                "ledger": [],
                "message": f"manifest 不一致：{error}",
            }

        page = self._require_page()
        self._open_my_gallery(page, str(params.get("album") or ""))
        before_cards = {str(card.get("name") or ""): card for card in (self._gallery_cards(page).get("cards") or [])}
        missing = [entry["path"] for entry in entries if Path(entry["path"]).name not in before_cards]
        uploaded_items: list[dict[str, Any]] = []
        if missing:
            valid_paths: list[Path] = []
            for raw_path in missing:
                path = Path(raw_path)
                if path.suffix.lower() in IMAGE_UPLOAD_EXTS and path.is_file() and path.stat().st_size <= IMAGE_UPLOAD_MAX_BYTES:
                    valid_paths.append(path)
            if valid_paths:
                blocked = self._quota_guard_images(
                    len(valid_paths),
                    params.get("limitOverride")
                    if isinstance(params.get("limitOverride"), dict)
                    else None,
                )
                if blocked is not None:
                    return {
                        **blocked,
                        "inserted": False,
                        "manifestConsistent": True,
                        "insertedCount": 0,
                        "failedCount": len(entries),
                        "ledger": [],
                        "message": blocked.get("message", "图库容量不足，未上传任何图片。"),
                    }
                uploaded_items, _ = self._upload_paths(page, valid_paths, str(params.get("album") or ""))
        uploaded_by_name = {item["name"]: item for item in uploaded_items if item.get("ok")}
        cards = {str(card.get("name") or ""): card for card in (self._gallery_cards(page).get("cards") or [])}

        ledger: list[dict[str, Any]] = []
        inserted_count = 0
        for entry in entries:
            self._check_cancelled()
            name = Path(entry["path"]).name
            card = uploaded_by_name.get(name) or cards.get(name)
            record = {
                "index": entry["index"],
                "path": entry["path"],
                "file": entry["path"],
                "anchorText": entry["anchorText"],
                "cellText": "",
                "imageId": "",
                "uploadedId": "",
                "insertedAt": "",
                "inserted": False,
                "ok": False,
                "error": "",
            }
            if card is None:
                record["error"] = "图片未出现在我的图库（上传失败或格式不被支持）。"
                ledger.append(record)
                continue
            record["imageId"] = card.get("id", "")
            record["uploadedId"] = card.get("id", "")
            try:
                target = self._select_text_target(page, entry["anchorText"], None)
                if target is None or target.get("error"):
                    record["error"] = (target or {}).get("error", "未找到 anchorText 对应的正文。")
                    ledger.append(record)
                    continue
                result = self._insert_gallery_card(page, card)
                record["inserted"] = bool(result["inserted"])
                if not result["inserted"]:
                    record["error"] = "点击图片后画布未新增图片。"
                else:
                    inserted_count += 1
                    record["cellText"] = target.get("text", "")
                    record["insertedAt"] = (
                        f"cell[{target.get('index')}] after「{entry['anchorText']}」"
                    )
                    record["ok"] = True
            except Exception as exc:
                record["error"] = str(exc)
            ledger.append(record)

        title, preview, editable = self._read_editor(page)
        return {
            "inserted": inserted_count == len(entries) and inserted_count > 0,
            "manifestConsistent": True,
            "insertedCount": inserted_count,
            "failedCount": len(entries) - inserted_count,
            "ledger": ledger,
            "editor": {"title": title, "textPreview": preview[:800], "editable": editable},
            "message": (
                f"manifest 批量插入完成：成功 {inserted_count}/{len(entries)} 张，尚未保存；"
                "请在可见窗口按文案顺序核对图片位置。"
                if inserted_count == len(entries)
                else f"manifest 批量插入部分失败：成功 {inserted_count}/{len(entries)} 张，请查看 ledger。"
            ),
        }

    # ------------------------------------------------------------ 容量配额
    @staticmethod
    def _quota_dimension(used: int | None, limit: int | None, reason: str = "") -> dict[str, Any]:
        if used is None or limit is None or limit < 0:
            return {
                "used": used,
                "limit": limit,
                "remaining": None,
                "ratio": None,
                "unknown": True,
                "reason": reason or "未能读取该维度的容量信息。",
            }
        remaining = max(0, int(limit) - int(used))
        return {
            "used": int(used),
            "limit": int(limit),
            "remaining": remaining,
            "ratio": (
                1.0
                if limit <= 0
                else round(min(1.0, max(0.0, float(used) / float(limit))), 4)
            ),
            "unknown": False,
            "reason": "",
        }

    def _quota_read_limits(self, page) -> tuple[dict[str, Any] | None, str]:
        try:
            raw = page.evaluate(QUOTA_INFO_JS)
        except Exception as exc:
            return None, f"读取账号等级限制失败：{type(exc).__name__}: {exc}"
        if not isinstance(raw, dict) or not isinstance(raw.get("limits"), dict):
            return None, "账号接口没有返回 levelLimit。"
        limits = raw["limits"]
        return {
            "article": int(limits.get("articleLimit") or 0),
            "image": int(limits.get("imageLimit") or 0),
            "level": raw.get("level"),
        }, ""

    def _quota_read_images(self, page) -> tuple[int | None, int | None, str]:
        try:
            self._open_my_gallery(page)
            data = self._gallery_cards(page)
        except Exception as exc:
            return None, None, f"读取我的图库容量失败：{type(exc).__name__}: {exc}"
        capacity = data.get("capacity") if isinstance(data, dict) else None
        if not isinstance(capacity, dict):
            return None, None, "图库页面没有显示「容量：已用/上限」文案。"
        used = capacity.get("used")
        limit = capacity.get("limit")
        if used is None or limit is None:
            return None, None, "图库容量文案解析失败。"
        return int(used), int(limit), ""

    def _quota_read_articles(self, page) -> tuple[int | None, str]:
        try:
            self._goto(page, LIST_URL, settle=8.0)
        except Exception as exc:
            return None, f"打开我的图文列表失败：{type(exc).__name__}: {exc}"
        if self._is_auth_page(page) or login_button_visible(page):
            return None, "未登录或登录已失效，无法读取我的图文数量。"
        try:
            used = page.evaluate(QUOTA_ARTICLE_COUNT_JS)
        except Exception as exc:
            return None, f"统计我的图文数量失败：{type(exc).__name__}: {exc}"
        if not isinstance(used, (int, float)):
            return None, "我的图文数量不是数字。"
        return int(used), ""

    def quota(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """只读账号容量；进程内缓存 60 秒，refresh=true 强制重读。"""
        params = params or {}
        force = params.get("refresh") is True
        now = time.time()
        if not force and self._quota_cache is not None and now - self._quota_cache_at < 60:
            cached = dict(self._quota_cache)
            cached["source"] = "cache"
            return cached

        page = self._require_page()
        saved_new = self._new_article
        saved_opened = self._opened_article
        tab = None
        limits: dict[str, Any] | None = None
        limits_reason = ""
        image_used = image_limit = None
        image_reason = ""
        article_used = None
        article_reason = ""
        try:
            tab = page.context.new_page()
            tab.set_default_timeout(30000)
            tab.goto(XIUMI_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            limits, limits_reason = self._quota_read_limits(tab)
            image_used, image_limit, image_reason = self._quota_read_images(tab)
            article_used, article_reason = self._quota_read_articles(tab)
        finally:
            if tab is not None:
                self._safe_close(tab)
            self._new_article = saved_new
            self._opened_article = saved_opened

        override = params.get("limitOverride") if isinstance(params.get("limitOverride"), dict) else {}
        if limits and limits.get("image", 0) > 0:
            image_limit = int(limits["image"])
        if limits and limits.get("article", 0) > 0:
            article_limit = int(limits["article"])
        else:
            article_limit = None
        if override.get("images") is not None:
            try:
                image_limit = int(override["images"])
            except Exception:
                pass
        if override.get("articles") is not None:
            try:
                article_limit = int(override["articles"])
            except Exception:
                pass

        result = {
            "images": self._quota_dimension(
                image_used,
                image_limit,
                image_reason or (limits_reason if image_used is None or image_limit is None else ""),
            ),
            "articles": self._quota_dimension(
                article_used,
                article_limit,
                article_reason or (limits_reason if article_used is None or article_limit is None else ""),
            ),
            "fetchedAt": datetime.now(timezone.utc).isoformat(),
            "source": "live",
        }
        if limits:
            result["level"] = limits.get("level")
        if limits_reason and (image_used is None or image_limit is None):
            result["limitsWarning"] = limits_reason
        self._quota_cache = result
        self._quota_cache_at = now
        return dict(result)

    def _quota_image_candidates(self, limit: int = 20) -> list[dict[str, Any]]:
        page = self._require_page()
        saved_new = self._new_article
        saved_opened = self._opened_article
        tab = None
        try:
            tab = page.context.new_page()
            tab.set_default_timeout(30000)
            tab.goto(XIUMI_URL, wait_until="domcontentloaded", timeout=60000)
            time.sleep(2)
            self._open_my_gallery(tab)
            cards = list(self._gallery_cards(tab).get("cards") or [])
        except Exception:
            return []
        finally:
            if tab is not None:
                self._safe_close(tab)
            self._new_article = saved_new
            self._opened_article = saved_opened
        def sort_key(card: dict[str, Any]) -> tuple[int, str]:
            created = str(card.get("createdAt") or "")
            return (0 if created else 1, created)
        cards.sort(key=sort_key)
        return [
            {
                "assetId": str(card.get("id") or ""),
                "name": str(card.get("name") or ""),
                "sizeBytes": card.get("size"),
                "uploadedAt": str(card.get("createdAt") or ""),
                "usedByArticles": [],
                "referenceStatus": "unknown",
            }
            for card in cards[: max(1, min(int(limit or 20), 50))]
        ]

    def _quota_article_candidates(self, limit: int = 10) -> tuple[list[dict[str, Any]], str]:
        saved_new = self._new_article
        saved_opened = self._opened_article
        try:
            items = self.article_list()
        except Exception as exc:
            return [], f"读取图文候选失败：{type(exc).__name__}: {exc}"
        finally:
            self._new_article = saved_new
            self._opened_article = saved_opened
        result = []
        for item in items[: max(1, min(int(limit or 10), 20))]:
            url = str(item.get("url") or "")
            match = re.search(r"/paper/(?:for/)?(\d+)", url)
            result.append(
                {
                    "articleId": match.group(1) if match else "",
                    "title": str(item.get("title") or ""),
                    "url": url,
                    "updatedAt": "",
                    "charCount": None,
                }
            )
        return result, ""

    def _quota_block_images(self, need: int, snapshot: dict[str, Any]) -> dict[str, Any]:
        images = snapshot.get("images") or {}
        used = images.get("used")
        limit = images.get("limit")
        remaining = images.get("remaining")
        candidates = self._quota_image_candidates(20) if not images.get("unknown") else []
        options = [
            {
                "action": "delete_old_images",
                "label": "删除图库旧图腾空间",
                "dangerous": True,
                "requiresConfirm": True,
                "candidates": candidates,
                "note": "候选按上传时间/图库顺序取最久未使用；秀米未提供图片引用关系，删除前必须由用户确认。",
            },
            {
                "action": "reduce_count",
                "label": "本次少用几张图",
                "detail": f"把本次需求从 {need} 张降到 {max(0, int(remaining or 0))} 张",
            },
            {"action": "cancel", "label": "先不传"},
        ]
        return {
            "ok": False,
            "quotaBlocked": True,
            "dimension": "images",
            "used": used,
            "limit": limit,
            "remaining": remaining,
            "need": need,
            "options": options,
            "message": (
                f"我的图库剩余容量不足：当前 {used}/{limit}，本次需要 {need} 张。"
                " 请把 options 展示给用户并等待选择；不得自行删除图片或静默上传。"
            ),
        }

    def _quota_guard_images(self, need: int, limit_override: dict[str, Any] | None = None) -> dict[str, Any] | None:
        if need <= 0:
            return None
        snapshot = self.quota({"refresh": True, "limitOverride": limit_override or {}})
        images = snapshot.get("images") or {}
        if images.get("unknown"):
            # 容量未知时宁可让上层按原有流程尝试，也不能伪造一个“已满”结论。
            return None
        remaining = int(images.get("remaining") or 0)
        if remaining < need or remaining <= 5:
            return self._quota_block_images(need, snapshot)
        return None

    def _quota_guard_articles(self, limit_override: dict[str, Any] | None = None) -> dict[str, Any] | None:
        snapshot = self.quota({"refresh": True, "limitOverride": limit_override or {}})
        articles = snapshot.get("articles") or {}
        if articles.get("unknown"):
            return None
        remaining = int(articles.get("remaining") or 0)
        if remaining > 2:
            return None
        candidates, reason = self._quota_article_candidates(10)
        options = [
            {
                "action": "edit_existing",
                "label": "在原有一篇上修改（推荐）",
                "candidates": candidates,
            },
            {
                "action": "replace_one",
                "label": "删除一篇旧文后新建",
                "dangerous": True,
                "requiresConfirm": True,
                "candidates": candidates,
            },
            {"action": "cancel", "label": "先不新建"},
        ]
        return {
            "ok": False,
            "quotaBlocked": True,
            "dimension": "articles",
            "used": articles.get("used"),
            "limit": articles.get("limit"),
            "remaining": articles.get("remaining"),
            "need": 1,
            "options": options,
            "message": (
                f"我的图文剩余容量不足：当前 {articles.get('used')}/{articles.get('limit')}，"
                f"剩余 {articles.get('remaining')} 篇。请把 options 展示给用户并等待选择；"
                "不得自行删除旧文或直接新建。"
                + (f" 候选读取提示：{reason}" if reason else "")
            ),
        }

    def quota_cleanup(self, params: dict[str, Any]) -> dict[str, Any]:
        """删除指定图库图片或图文；必须 confirm=true，逐项如实返回。"""
        if params.get("confirm") is not True:
            raise ValueError("quota_cleanup 是破坏性操作，需要 confirm=true 确认。")
        dimension = str(params.get("dimension") or "").strip()
        if dimension not in ("images", "articles"):
            raise ValueError("quota_cleanup 的 dimension 必须是 images 或 articles。")
        raw_items = params.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("quota_cleanup 的 items 不能为空。")
        items: list[dict[str, Any]] = []
        for raw in raw_items:
            if isinstance(raw, dict):
                entry = dict(raw)
            else:
                entry = {"id": str(raw)}
            ident = str(entry.get("id") or entry.get("assetId") or entry.get("articleId") or "").strip()
            if not ident:
                raise ValueError("quota_cleanup 的 items 中存在空 id。")
            used = entry.get("usedByArticles")
            if dimension == "images" and isinstance(used, list) and used:
                raise ValueError(f"图片 {ident} 被现有图文引用，拒绝删除。")
            entry["id"] = ident
            items.append(entry)

        page = self._require_page()
        results: list[dict[str, Any]] = []
        if dimension == "images":
            try:
                self._open_my_gallery(page)
            except Exception as exc:
                raise RuntimeError(f"打开我的图库失败：{type(exc).__name__}: {exc}") from exc
            for entry in items:
                ident = str(entry["id"])
                try:
                    cards = self._gallery_cards(page).get("cards") or []
                    target = None
                    for card in cards:
                        if ident in (str(card.get("id") or ""), str(card.get("name") or "")):
                            target = card
                            break
                    if target is None:
                        results.append({"id": ident, "deleted": False, "reason": "图库中没有找到该图片。"})
                        continue
                    loc = page.locator(".tn-tpl-item.tn-asset-image").nth(int(target.get("index") or 0))
                    remove = loc.locator(".bar-item.remove").first
                    remove.click(timeout=8000)
                    time.sleep(0.8)
                    confirmed = False
                    for text in ("确定", "删除", "确认"):
                        btns = page.get_by_text(text, exact=True)
                        for idx in range(btns.count()):
                            try:
                                if btns.nth(idx).is_visible():
                                    btns.nth(idx).click(timeout=3000)
                                    confirmed = True
                                    break
                            except Exception:
                                pass
                        if confirmed:
                            break
                    time.sleep(1.2)
                    exists = any(
                        ident in (str(card.get("id") or ""), str(card.get("name") or ""))
                        for card in (self._gallery_cards(page).get("cards") or [])
                    )
                    results.append(
                        {
                            "id": ident,
                            "deleted": not exists,
                            "reason": "" if not exists else "点击删除后图片仍在图库中。",
                        }
                    )
                except Exception as exc:
                    results.append({"id": ident, "deleted": False, "reason": str(exc)})
        else:
            self._goto(page, LIST_URL, settle=8.0)
            if self._is_auth_page(page) or login_button_visible(page):
                raise RuntimeError("未登录或登录已失效，无法删除图文。")
            for entry in items:
                ident = str(entry["id"])
                try:
                    cards = page.evaluate(
                        """
                        () => Array.from(document.querySelectorAll('.show-item')).map((el, i) => {
                          const a = el.querySelector("a[href*='/paper/for/']");
                          const t = el.querySelector('.title');
                          return {i, href: a ? a.href : '', title: t ? t.innerText.trim() : ''};
                        })
                        """
                    )
                    target = next(
                        (card for card in cards if ident in str(card.get("href") or "")),
                        None,
                    )
                    if target is None:
                        results.append({"id": ident, "deleted": False, "reason": "我的图中没有找到该图文。"})
                        continue
                    row = page.locator(".show-item").nth(int(target["i"]))
                    row.hover()
                    time.sleep(0.4)
                    row.locator(".action-btn-small.delete").first.click(timeout=8000)
                    time.sleep(0.8)
                    confirmed = False
                    for text in ("确定", "删除", "确认删除"):
                        btns = page.get_by_text(text, exact=True)
                        for idx in range(btns.count()):
                            try:
                                if btns.nth(idx).is_visible():
                                    btns.nth(idx).click(timeout=3000)
                                    confirmed = True
                                    break
                            except Exception:
                                pass
                        if confirmed:
                            break
                    time.sleep(1.2)
                    exists = page.evaluate(
                        """(ident) => Array.from(document.querySelectorAll('.show-item a[href*="/paper/for/"]')).some(a => a.href.includes(ident))""",
                        ident,
                    )
                    results.append(
                        {
                            "id": ident,
                            "deleted": not bool(exists),
                            "reason": "" if not exists else "点击删除后图文仍在列表中。",
                        }
                    )
                except Exception as exc:
                    results.append({"id": ident, "deleted": False, "reason": str(exc)})
        self._quota_cache = None
        self._quota_cache_at = 0.0
        return {
            "dimension": dimension,
            "results": results,
            "deletedCount": sum(1 for item in results if item.get("deleted")),
            "message": (
                f"已删除 {sum(1 for item in results if item.get('deleted'))}/{len(results)} 项。"
                " 删除是破坏性操作，请让用户刷新图库/列表确认最终状态。"
            ),
        }

    def _launch_browser(self) -> None:
        self._teardown_browser()
        self._ensure_playwright()
        channel = str(self.config.get("browserChannel") or "msedge").strip() or "msedge"
        # 硬性要求：全程可见、可中断。即使外部误传 headless=true 也不静默降级。
        if self.config.get("headless"):
            _log("忽略 headless=true：本插件要求可见浏览器")
        headless = False
        _log(f"launching chromium channel={channel} headless={headless}")
        # 刻意不注入外部导出的登录凭据或预采集登录态：登录状态一律来自
        # 当前可见窗口的实时操作。固定 user-data-dir 让浏览器进程重启后仍复用
        # 同一份 cookie/登录态，避免用户重复登录。
        BROWSER_PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        context = self._playwright.chromium.launch_persistent_context(
            user_data_dir=str(BROWSER_PROFILE_DIR),
            headless=headless,
            channel=channel,
            args=["--no-first-run", "--no-default-browser-check"],
            # 关闭固定 viewport，让页面跟随用户手动调整的窗口大小自适应。
            # 注意 Python API 的参数名是 no_viewport；写 viewport=None 会被当成
            # 未提供，退回默认 1280x720，用户放大窗口时页面就不自适配了。
            no_viewport=True,
        )
        try:
            pages = [p for p in context.pages if not p.is_closed()]
            page = pages[0] if pages else context.new_page()
            # 保留一个后台空白页作为会话守护：用户关掉最后一个业务标签页时，
            # 浏览器进程不会跟着退出；恢复时再把守护页导航回原 URL。
            if len(pages) <= 1:
                keeper = context.new_page()
                try:
                    keeper.goto("about:blank", wait_until="domcontentloaded", timeout=15000)
                except Exception:
                    pass
                try:
                    page.bring_to_front()
                except Exception:
                    pass
        except Exception:
            self._safe_close(context)
            raise
        self._browser = context.browser
        self._context = context
        self._page = page
        self._browser_relaunched = False

    # ------------------------------------------------------------------ 方法
    def open(self) -> dict[str, Any]:
        """首次调用启动可见浏览器并打开秀米首页；已启动则直接返回状态。"""
        if self._current_page() is not None:
            return self._status_dict()
        self._launch_browser()
        try:
            self._goto(self._page, XIUMI_URL, settle=3.0)
            try:
                self._page.bring_to_front()
            except Exception:
                pass
        except Exception as exc:
            self._last_error = f"{type(exc).__name__}: {exc}"
            _log(f"打开秀米首页失败（保留浏览器，不自动关闭）: {self._last_error}")
            raise RuntimeError(f"打开秀米首页失败：{exc}") from exc
        return self._status_dict()

    def press_key(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """调试用：给当前页面按一次键。"""
        page = self._require_page()
        key = str((params or {}).get("key") or "").strip()
        if not key:
            raise ValueError("params.key 不能为空")
        page.keyboard.press(key)
        return {"ok": True, "key": key}

    def eval_js(self, params: dict[str, Any] | None = None) -> Any:
        """调试用：在当前工作页执行一段 JS 并返回结果。

        仅供开发排查，不注册成工具、不写进 README。
        """
        page = self._require_page()
        params = params or {}
        script = params.get("script")
        if not isinstance(script, str) or not script.strip():
            raise ValueError("params.script 必须是非空字符串")
        return page.evaluate(script, params.get("arg"))

    DELETE_SCROLL_JS = r"""
    () => {
      const root = document.querySelector('.tn-working-pallet');
      if (!root) return { ok: false, reason: 'noPallet' };
      const imgs = Array.from(root.querySelectorAll('.tn-cell-image'))
        .filter(el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; });
      if (!imgs.length) return { ok: false, reason: 'none' };
      imgs[0].scrollIntoView({ block: 'center' });
      return { ok: true, left: imgs.length };
    }
    """

    DELETE_IMAGE_CELLS_JS = r"""
    (payload) => {
      try {
        const limit = (payload && Number(payload.limit)) || 500;
        const root = document.querySelector('.tn-working-pallet');
        if (!root) return { ok: false, reason: 'noPallet', removed: 0 };
        const wraps = Array.from(root.querySelectorAll('.tn-cell-image'));
        const before = wraps.length;
        if (!before) return { ok: true, removed: 0, before: 0, remaining: 0 };
        let removed = 0;
        let applyScope = null;
        const skipped = [];
        for (const el of wraps) {
          if (removed >= limit) break;
          const cellEl = el.closest('.tn-cell') || el;
          const scope = window.angular ? angular.element(cellEl).scope() : null;
          if (!scope || !scope.cell || scope.cell.type !== 'image') { skipped.push('noScope'); continue; }
          const parent = scope.$parent;
          const parentCell = parent && parent.parentCell;
          if (!parentCell || !Array.isArray(parentCell.items)) { skipped.push('noParentCell'); continue; }
          const key = scope.cellKey || '';
          let idx = -1;
          if (key) {
            idx = parentCell.items.findIndex(entry => entry && entry[key] === scope.cell);
          }
          if (idx < 0) {
            const targetText = String(scope.cell.text || '');
            const targetSrc = String(scope.cell.src || '');
            idx = parentCell.items.findIndex(entry => {
              if (!entry || typeof entry !== 'object') return false;
              for (const k of Object.keys(entry)) {
                const v = entry[k];
                if (!v || typeof v !== 'object') continue;
                if (targetText && String(v.text || '') === targetText) return true;
                if (targetSrc && String(v.src || '') === targetSrc) return true;
              }
              return false;
            });
          }
          if (idx < 0) { skipped.push('notInItems'); continue; }
          parentCell.items.splice(idx, 1);
          removed += 1;
          applyScope = applyScope || scope;
        }
        if (removed && applyScope) {
          try { applyScope.$apply(); } catch (err) {}
        }
        const remaining = Array.from(root.querySelectorAll('.tn-cell-image')).length;
        return { ok: true, removed, before, remaining, skipped: skipped.slice(0, 5) };
      } catch (err) {
        return { ok: false, removed: 0, error: String(err && err.message || err) };
      }
    }
    """

    DELETE_FIND_BUTTON_JS = r"""
    () => {
      const btn = document.querySelector('.btn-danger.op-bar-btn');
      if (!btn) return { ok: false, reason: 'noButton' };
      const r = btn.getBoundingClientRect();
      if (r.width < 4 || r.height < 4) return { ok: false, reason: 'invisible' };
      return { ok: true, x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2) };
    }
    """

    DELETE_COUNT_JS = r"""
    () => {
      const root = document.querySelector('.tn-working-pallet');
      if (!root) return { left: -1 };
      return { left: Array.from(root.querySelectorAll('.tn-cell-image'))
        .filter(el => { const r = el.getBoundingClientRect(); return r.width > 8 && r.height > 8; }).length };
    }
    """

    def delete_image_cells(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """删除正文里的图片单元格。

        走 Angular 模型：每个图片 cell 的 scope 上有 ``cell``（type=image）与
        ``$parent.parentCell.items``，从父级 items 里 splice 掉该 cell 即删除。
        （试过模拟「点击图片 → 点浮动工具栏垃圾桶」的路线，秀米不认合成鼠标
        事件，垃圾桶始终不可见，故改为模型级删除。）

        参数：
            limit: 最多删几张，默认 200
            dryRun: true 时只统计不删除
        """
        page = self._require_page()
        params = params or {}
        limit = int(params.get("limit") or 200)
        dry_run = bool(params.get("dryRun"))

        before = page.evaluate(self.DELETE_COUNT_JS).get("left", -1)
        if dry_run:
            return {"ok": True, "dryRun": True, "before": before, "removed": 0, "remaining": before}

        removed_total = 0
        rounds = 0
        last: dict[str, Any] = {}
        while removed_total < limit and rounds < 12:
            rounds += 1
            last = page.evaluate(self.DELETE_IMAGE_CELLS_JS, {"limit": max(1, limit - removed_total)})
            removed = int(last.get("removed") or 0) if isinstance(last, dict) else 0
            removed_total += removed
            if removed <= 0:
                break
            page.wait_for_timeout(450)

        after = page.evaluate(self.DELETE_COUNT_JS).get("left", -1)
        result: dict[str, Any] = {
            "ok": True,
            "removed": removed_total,
            "before": before,
            "remaining": after,
            "rounds": rounds,
        }
        if isinstance(last, dict):
            if last.get("skipped"):
                result["skipped"] = last["skipped"]
            if last.get("error"):
                result["error"] = last["error"]
        return result

    def focus_working_page(self) -> None:
        """把工作页顶到前台。

        浏览器里常驻一个 about:blank 守护页；它一旦成为活动标签，用户看到
        的窗口就是一片白屏。每次工具调用前把工作页切回前面，避免用户以为
        页面坏了。
        """
        page = self._page
        if page is None:
            return
        try:
            if page.is_closed():
                return
            page.bring_to_front()
        except Exception:
            pass

    def status(self) -> dict[str, Any]:
        return self._status_dict()

    def logout(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """清除本机浏览器中的秀米登录态，并回到未登录的秀米首页。

        只动浏览器里的登录态（cookies + localStorage/sessionStorage），
        不删除 profile 目录、不动用户填写在设置页的账号密码。
        """
        params = params or {}
        keep_profile = bool(params.get("keepProfile", True))
        before = self._status_dict()
        if not before.get("running"):
            return {
                "ok": True,
                "loggedIn": False,
                "running": False,
                "url": "",
                "title": "",
                "clearedCookies": False,
                "clearedStorage": False,
                "profileKept": True,
                "message": "浏览器未打开，没有可清除的登录态。",
            }
        cleared_cookies = False
        cleared_storage = False
        try:
            page = self._require_page()
        except Exception:
            return {
                "ok": False,
                "loggedIn": bool(before.get("loggedIn")),
                "running": bool(before.get("running")),
                "url": str(before.get("url") or ""),
                "title": str(before.get("title") or ""),
                "clearedCookies": False,
                "clearedStorage": False,
                "profileKept": True,
                "message": "无法获取当前页面，未执行退出。",
            }
        context = getattr(self, "_context", None)
        if context is not None:
            try:
                context.clear_cookies()
                cleared_cookies = True
            except Exception:
                cleared_cookies = False
        try:
            page.evaluate(
                "() => { try { localStorage.clear(); sessionStorage.clear(); }"
                " catch (e) {} }"
            )
            cleared_storage = True
        except Exception:
            cleared_storage = False
        try:
            self._goto(page, "https://xiumi.us/", settle=3.0)
        except Exception:
            pass
        after = self._status_dict()
        logged_in = bool(after.get("loggedIn"))
        if not keep_profile:
            try:
                self.close({"discard": True})
            except Exception:
                pass
        if logged_in:
            message = (
                "已清除 cookies 与本地存储，但页面仍显示已登录；"
                "请点「打开浏览器」刷新后再确认，或稍后重试。"
            )
        else:
            message = "已退出秀米账号（清除了本机浏览器中的登录态）。"
        return {
            "ok": not logged_in,
            "loggedIn": logged_in,
            "running": bool(after.get("running")),
            "url": str(after.get("url") or ""),
            "title": str(after.get("title") or ""),
            "clearedCookies": cleared_cookies,
            "clearedStorage": cleared_storage,
            "profileKept": keep_profile,
            "message": message,
        }

    def prune_screens(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """清理临时截图目录，避免 .tmp/screens 无限膨胀。

        默认只保留最近 60 张、且不超过 24 小时；可用 keep / maxAgeHours 覆盖。
        """
        params = params or {}
        keep = params.get("keep")
        age = params.get("maxAgeHours")
        try:
            keep_n = int(keep) if keep is not None else self.SCREENSHOT_KEEP
        except (TypeError, ValueError):
            keep_n = self.SCREENSHOT_KEEP
        try:
            age_h = float(age) if age is not None else self.SCREENSHOT_MAX_AGE_HOURS
        except (TypeError, ValueError):
            age_h = self.SCREENSHOT_MAX_AGE_HOURS
        return self._prune_screens(keep=keep_n, max_age_hours=age_h)

    def _prune_screens(self, keep: int = 60, max_age_hours: float = 24.0) -> dict[str, Any]:
        """删除超出保留策略的临时截图；任何异常都不影响主流程。"""
        result: dict[str, Any] = {
            "removed": 0,
            "kept": 0,
            "freedBytes": 0,
            "keep": keep,
            "maxAgeHours": max_age_hours,
        }
        try:
            if not SCREENS_DIR.exists():
                return result
            now = time.time()
            entries: list[tuple[float, Path, int]] = []
            for entry in SCREENS_DIR.glob("*.png"):
                try:
                    stat = entry.stat()
                except OSError:
                    continue
                entries.append((stat.st_mtime, entry, stat.st_size))
            entries.sort(key=lambda item: item[0], reverse=True)
            removed = 0
            freed = 0
            for index, (mtime, entry, size) in enumerate(entries):
                too_old = (now - mtime) > max_age_hours * 3600.0
                too_many = index >= keep
                if not (too_old or too_many):
                    continue
                try:
                    entry.unlink()
                except OSError:
                    continue
                removed += 1
                freed += size
            result["removed"] = removed
            result["kept"] = len(entries) - removed
            result["freedBytes"] = freed
            if removed:
                _log(f"pruned screens: removed={removed} kept={result['kept']} freed={freed}B")
        except Exception as exc:  # 清理是尽力而为，绝不影响截图本身
            _log(f"prune screens failed: {exc}")
            result["error"] = str(exc)
        return result

    def screenshot(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """对当前可见页面截图，并带回 url/title/可见正文，便于失败时诊断。"""
        params = params or {}
        page = self._current_page()
        if page is None:
            raise RuntimeError("浏览器未启动或窗口已被关闭，无法截图；请先调用 open。")
        SCREENS_DIR.mkdir(parents=True, exist_ok=True)
        raw_name = str(params.get("name") or "").strip()
        safe_name = re.sub(r"[^0-9A-Za-z_\-\u4e00-\u9fff]+", "-", raw_name).strip("-")
        safe_name = (safe_name or "screenshot")[:48]
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        target = SCREENS_DIR / f"{stamp}-01-{safe_name}.png"
        sequence = 1
        while target.exists() and sequence < 1000:
            sequence += 1
            target = SCREENS_DIR / f"{stamp}-{sequence:02d}-{safe_name}.png"
        page.screenshot(
            path=str(target),
            full_page=bool(params.get("fullPage")),
            timeout=20000,
        )
        try:
            url = str(page.url or "")
        except Exception:
            url = ""
        try:
            title = str(page.title() or "")
        except Exception:
            title = ""
        try:
            body_text = str(page.locator("body").inner_text(timeout=3000) or "")
        except Exception:
            body_text = ""
        body_text = re.sub(r"\s+", " ", body_text).strip()
        # 每次截图后顺手清理一次旧图，避免 .tmp/screens 无限增长。
        try:
            self._prune_screens()
        except Exception:
            pass
        return {
            "path": str(target.resolve()),
            "url": url,
            "title": title,
            "textPreview": body_text[:500],
        }

    def close(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """显式关闭浏览器；离开前自动保存，只有 discard=true 才丢弃。"""
        params = params or {}
        page = self._current_page()
        leave: dict[str, Any] = {}
        if page is not None:
            leave = self._leave_editor_guarded(page, discard=params.get("discard") is True)
            if not leave.get("ok"):
                status = self._status_dict()
                status.update(leave)
                status["running"] = True
                return status
        self._teardown_browser()
        _log("browser closed")
        return {
            "running": False,
            "loggedIn": False,
            "url": "",
            "title": "",
            "autoSaved": bool(leave.get("saved")),
            "discardedDraft": bool(leave.get("discardedDraft")),
        }

    def nav(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """在同一浏览器会话内做秀米站内导航，不重启、不重新登录。"""
        params = params or {}
        action = str(params.get("action") or "").strip().lower()
        if action not in ("back", "home", "editor", "list"):
            raise ValueError("nav 的 action 必须是 back、home、editor 或 list。")
        page = self._require_page()
        leave: dict[str, Any] = {}
        if action in ("back", "home", "list"):
            leave = self._leave_editor_guarded(page, discard=params.get("discard") is True)
            if not leave.get("ok"):
                status = self._status_dict()
                status.update(leave)
                status.update({"running": True, "action": action})
                return status
        if action == "back":
            try:
                page.go_back(wait_until="domcontentloaded", timeout=30000)
            except Exception as exc:
                raise RuntimeError(f"浏览器后退失败：{exc}") from exc
            time.sleep(2.0)
        elif action == "home":
            self._goto(page, XIUMI_URL, settle=3.0)
        elif action == "list":
            self._goto(page, LIST_URL, settle=8.0)
        else:  # editor
            if not self._editor_present(page):
                opened = self._opened_article or {}
                article_id = str(opened.get("articleId") or "").strip()
                if article_id:
                    target = f"https://xiumi.us/studio/v5#/paper/for/{article_id}"
                elif "/paper/for/" in str(self._last_editor_url or ""):
                    target = self._last_editor_url
                else:
                    target = EDITOR_NEW_URL
                self._handle_restore_dialog(page)
                self._goto(page, target, settle=6.0)
                self._handle_restore_dialog(page)
                self._wait_editor(page, timeout_ms=20000, dismiss_restore=True)
        self._handle_restore_dialog(page)
        try:
            current_url = str(page.url or "")
        except Exception:
            current_url = ""
        status = self._status_dict()
        status.update({"ok": True, "action": action, "url": current_url})
        if leave:
            status.update(
                {
                    "autoSaved": bool(leave.get("saved")),
                    "discardedDraft": bool(leave.get("discardedDraft")),
                }
            )
            if leave.get("saved"):
                status["savedResult"] = leave.get("saveResult")
        if self._editor_present(page):
            self._last_editor_url = current_url
        return status

    def login_wait(self, params: dict[str, Any]) -> dict[str, Any]:
        """轮询等待用户手动登录；超时只返回结果，绝不关闭浏览器。"""
        timeout_ms = _coerce_int(
            params.get("timeoutMs"), int(self.config.get("loginTimeoutMs") or 900000)
        )
        timeout_ms = max(1000, min(timeout_ms, 24 * 3600 * 1000))
        started = time.monotonic()
        deadline = started + timeout_ms / 1000.0

        status = self._status_dict()
        if not status["running"]:
            # 等待登录必须有可见窗口，未启动时自动拉起（失败则如实报错）
            status = self.open()

        if status["loggedIn"]:
            return {
                "loggedIn": True,
                "url": status["url"],
                "waitedMs": 0,
                "message": "已登录。",
            }

        latest = status
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(LOGIN_POLL_SECONDS, remaining))
            latest = self._status_dict()
            if latest["loggedIn"]:
                waited_ms = int((time.monotonic() - started) * 1000)
                return {
                    "loggedIn": True,
                    "url": latest["url"],
                    "waitedMs": waited_ms,
                    "message": "已检测到登录状态。",
                }
            if not latest["running"]:
                # 用户等待期间把窗口关了：立即结束等待，不再空转到超时
                waited_ms = int((time.monotonic() - started) * 1000)
                return {
                    "loggedIn": False,
                    "url": "",
                    "waitedMs": waited_ms,
                    "message": "浏览器窗口已关闭，等待登录中止；请重新打开浏览器后再试。",
                }

        waited_ms = int((time.monotonic() - started) * 1000)
        seconds = max(1, round(timeout_ms / 1000))
        return {
            "loggedIn": False,
            "url": latest.get("url", ""),
            "waitedMs": waited_ms,
            "message": (
                f"等待登录超时（{seconds} 秒内未检测到登录）。浏览器保持打开，"
                "请点右上角「登录」完成扫码或账号密码登录后重试。"
            ),
        }

    # ------------------------------------------------------------ 登录辅助
    def login_fill(self, params: dict[str, Any]) -> dict[str, Any]:
        """把调用方传入的账号密码填入登录页；绝不记录、回显或落盘凭据。"""
        account = params.get("account")
        password = params.get("password")
        submit = bool(params.get("submit", True))
        url = ""
        if not isinstance(account, str) or not account.strip():
            return {
                "filled": False,
                "submitted": False,
                "url": "",
                "message": "缺少 account 参数（账号只能由工具调用方传入，本工具不保存账号）。",
            }
        if not isinstance(password, str) or not password:
            return {
                "filled": False,
                "submitted": False,
                "url": "",
                "message": "缺少 password 参数（密码只能由工具调用方传入，本工具不保存密码）。",
            }
        try:
            page = self._require_page()
            url = str(page.url or "")
            login_failed = False
            login_error = ""
            captcha_required = False
            if not self._is_auth_page(page):
                entry, _ = self._find_visible_text(page, ["登录"], exact=True)
                if entry is None:
                    return {
                        "filled": False,
                        "submitted": False,
                        "url": url,
                        "message": "未找到可见的「登录」入口，无法进入登录页；请确认浏览器停在秀米页面。",
                    }
                entry.click()
                deadline = time.monotonic() + 12.0
                while time.monotonic() < deadline and not self._is_auth_page(page):
                    time.sleep(0.5)
                url = str(page.url or "")
                if not self._is_auth_page(page):
                    return {
                        "filled": False,
                        "submitted": False,
                        "url": url,
                        "message": "已点击「登录」，但未进入登录页（未出现账号密码输入框）。",
                    }

            account_input = None
            account_selector = ""
            password_input = None
            password_selector = ""
            tab_clicked = False
            deadline = time.monotonic() + 18.0
            while time.monotonic() < deadline:
                if account_input is None:
                    for selector in (
                        "input[name='email']",
                        "input[placeholder*='邮箱或手机']",
                        "input[placeholder*='手机号']",
                        "input[type='text'][placeholder*='邮箱']",
                    ):
                        try:
                            loc = page.locator(selector)
                            for index in range(min(loc.count(), 5)):
                                element = loc.nth(index)
                                if element.is_visible():
                                    account_input = element
                                    account_selector = selector
                                    break
                        except Exception:
                            pass
                        if account_input is not None:
                            break
                if password_input is None:
                    for selector in ("input[name='password']", "input[type='password']"):
                        try:
                            loc = page.locator(selector)
                            for index in range(min(loc.count(), 5)):
                                element = loc.nth(index)
                                if element.is_visible():
                                    password_input = element
                                    password_selector = selector
                                    break
                        except Exception:
                            pass
                        if password_input is not None:
                            break
                if account_input is not None and password_input is not None:
                    break
                # 登录页可能先渲染扫码页，需要切到「使用邮箱或手机登录」
                if not tab_clicked:
                    tab, _ = self._find_visible_text(page, ["使用邮箱或手机登录"], exact=False)
                    if tab is not None:
                        try:
                            tab.click()
                            tab_clicked = True
                        except Exception:
                            pass
                time.sleep(0.6)

            if account_input is None:
                return {
                    "filled": False,
                    "submitted": False,
                    "url": str(page.url or ""),
                    "message": "进入登录页后未找到可见的账号输入框（手机号/邮箱）。",
                }
            if password_input is None:
                return {
                    "filled": False,
                    "submitted": False,
                    "url": str(page.url or ""),
                    "message": "进入登录页后未找到可见的密码输入框。",
                }

            try:
                account_input.click()
                account_input.fill(account)
                password_input.click()
                password_input.fill(password)
            except Exception:
                return {
                    "filled": False,
                    "submitted": False,
                    "url": str(page.url or ""),
                    "message": "定位到了账号/密码输入框，但填入失败；未回显任何凭据。",
                }

            submitted = False
            message = (
                f"已定位账号输入框（{account_selector}）与密码输入框（{password_selector}），"
                "账号与密码已填入（未提交）。请在可见浏览器窗口中确认后手动点击「登录」。"
            )
            if submit:
                consent_clicked = False
                try:
                    checkboxes = page.locator("input[type='checkbox']")
                    for index in range(min(checkboxes.count(), 5)):
                        checkbox = checkboxes.nth(index)
                        if checkbox.is_visible() and not checkbox.is_checked():
                            checkbox.click()
                            consent_clicked = True
                            time.sleep(0.4)
                            break
                except Exception:
                    pass

                def find_login_button():
                    for selector in ("button.login-button", "button:has-text('登录')",
                                     "input[type='submit'][value*='登录']"):
                        try:
                            loc = page.locator(selector)
                            for index in range(min(loc.count(), 10)):
                                element = loc.nth(index)
                                if element.is_visible():
                                    return element
                        except Exception:
                            pass
                    return None

                button = find_login_button()
                if button is None:
                    message = (
                        f"已定位账号输入框（{account_selector}）与密码输入框（{password_selector}），"
                        "账号密码已填入，但未找到可见的「登录」按钮；请在窗口中手动提交。"
                    )
                else:
                    try:
                        if not button.is_enabled():
                            time.sleep(1.0)
                            button = find_login_button()
                        if button is not None and button.is_enabled():
                            button.click(timeout=5000)
                            submitted = True
                            time.sleep(2.0)
                        else:
                            message = (
                                f"已定位账号输入框（{account_selector}）与密码输入框（{password_selector}），"
                                "账号密码已填入，但「登录」按钮当前不可点击（可能需要先勾选协议或完成页面校验）；"
                                "请在窗口中手动提交。"
                            )
                    except Exception:
                        message = "账号密码已填入，但点击「登录」失败；请在窗口中手动提交。"
                    else:
                        if submitted:
                            # 提交后轮询最多 10 秒：秀米有可能跳转到 /auth/... 错误页，
                            # 也可能在页面上直接弹出错误提示，固定 sleep 2 秒抓不到。
                            failed_words = (
                                "密码错误",
                                "账号或密码",
                                "用户名或密码",
                                "密码不正确",
                                "账号不存在",
                                "账号异常",
                                "登录失败",
                                "请输入正确的密码",
                                "请检查账号",
                                "incorrect password",
                                "invalid credential",
                                "wrong password",
                            )
                            body = ""
                            login_failed = False
                            scroll_deadline = time.monotonic() + 10.0
                            while time.monotonic() < scroll_deadline:
                                try:
                                    body = page.locator("body").inner_text(timeout=3000) or ""
                                except Exception:
                                    body = ""
                                if any(word in body for word in failed_words):
                                    login_failed = True
                                    break
                                try:
                                    current_url = str(page.url or "")
                                except Exception:
                                    current_url = ""
                                if current_url and "/auth" not in current_url and "login" not in current_url:
                                    # 已经离开登录/鉴权页，说明提交成功，交给 login_wait 判定。
                                    break
                                time.sleep(0.5)
                            consent_note = "已勾选登录协议，" if consent_clicked else ""
                            captcha_required = any(
                                word in body
                                for word in ("验证码", "短信验证", "图形验证", "安全验证", "人机验证", "短信校验")
                            )
                            if not login_failed:
                                try:
                                    final_url = str(page.url or "")
                                except Exception:
                                    final_url = ""
                                # 10 秒后仍停在 /auth 页面 = 没有登录成功（密码错误会停留在此）。
                                if "/auth" in final_url:
                                    login_failed = True
                                    if not body:
                                        try:
                                            body = page.locator("body").inner_text(timeout=3000) or ""
                                        except Exception:
                                            body = ""
                            if captcha_required and not login_failed:
                                message = (
                                    "登录被要求完成验证码：" + consent_note + "账号密码已填入并已提交，"
                                    "但秀米要求人机/短信验证。请在弹出的浏览器窗口里手动完成验证码，"
                                    "完成后调用 xiumi_login 的 wait（或回到设置页点「登录秀米」）确认登录状态。"
                                )
                            elif login_failed:
                                # 秀米明确拒绝了这次登录，不必再等 login_wait。
                                login_error = (
                                    "登录失败：秀米提示账号或密码不正确"
                                    "（页面原文包含「密码错误」类提示）。请检查设置页里的账号与密码后重试。"
                                )
                                message = login_error
                                # 秀米会跳到 /auth/... 401 错误页，回首页让用户能再次点「登录」。
                                try:
                                    self._goto(page, "https://xiumi.us/#/", settle=3.0)
                                except Exception:
                                    pass
                            elif any(word in body for word in ("验证码", "短信验证", "图形验证", "安全验证")):
                                message = (
                                    "账号密码已填入，" + consent_note + "并已点击登录；页面出现验证码，"
                                    "请在弹出的浏览器窗口中手动完成验证码并登录，然后调用 xiumi_login 的 wait 等待登录完成。"
                                )
                            else:
                                message = (
                                    "账号密码已填入，" + consent_note + "并已点击登录；请在可见浏览器窗口中确认结果"
                                    "（如出现验证码请手动完成），然后调用 xiumi_login 的 wait 等待登录完成。"
                                )
            return {
                "filled": True,
                "submitted": submitted,
                "loginFailed": bool(login_failed),
                "loginError": login_error,
                "captchaRequired": bool(captcha_required),
                "url": str(page.url or ""),
                "message": message,
            }
        except Exception as exc:
            return {
                "filled": False,
                "submitted": False,
                "url": url,
                "message": f"登录填入过程失败（{type(exc).__name__}），未回显任何凭据；请在浏览器窗口中手动登录。",
            }

    # ------------------------------------------------------------ 图文操作
    def article_list(self) -> list[dict[str, str]]:
        """Read the library on a separate page without leaving a dirty editor."""
        tab = self._require_page().context.new_page()
        try:
            return self._article_list_page(tab)
        finally:
            self._safe_close(tab)

    def _article_list_page(self, page) -> list[dict[str, str]]:
        """只读列出「我的图文」，最多 50 条；未登录明确报错。"""
        self._goto(page, LIST_URL, settle=8.0)
        if self._is_auth_page(page) or login_button_visible(page):
            raise RuntimeError(
                "未登录：秀米把「我的图文」重定向到了登录页。请先在可见浏览器窗口完成登录，"
                "再调用 article_list；本工具不会返回伪造的空列表。"
            )
        try:
            for _ in range(3):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                time.sleep(1.0)
            page.evaluate("window.scrollTo(0, 0)")
        except Exception:
            pass
        try:
            items = page.evaluate(
                """
                () => {
                  const seen = new Set();
                  const result = [];
                  for (const card of document.querySelectorAll('.show-item')) {
                    const link = card.querySelector("a[href*='/paper/for/']");
                    if (!link) continue;
                    const url = link.href;
                    if (!url || seen.has(url)) continue;
                    const titleEl = card.querySelector('.title');
                    let title = titleEl ? (titleEl.innerText || '').trim() : '';
                    if (!title) title = (link.getAttribute('title') || link.innerText || '').trim();
                    if (!title) continue;
                    seen.add(url);
                    result.push({title, url});
                  }
                  return result;
                }
                """
            )
        except Exception as exc:
            raise RuntimeError(f"读取「我的图文」列表失败：{exc}") from exc
        if not isinstance(items, list):
            return []
        cleaned = []
        for item in items:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            url = str(item.get("url") or "").strip()
            if title and url:
                cleaned.append({"title": title, "url": url})
        return cleaned[:50]

    def article_open(self, params: dict[str, Any]) -> dict[str, Any]:
        """只读打开已有图文，记录 articleId 供 article_update 使用；不修改内容。"""
        article_id = str(params.get("articleId") or "").strip()
        url = str(params.get("url") or "").strip()
        if not url:
            if not article_id:
                raise ValueError("article_open 需要 params.articleId 或 params.url")
            if not re.fullmatch(r"\d{1,20}", article_id):
                raise ValueError("params.articleId 必须是数字")
            url = f"https://xiumi.us/studio/v5#/paper/for/{article_id}"
        if not re.match(r"^https?://", url):
            raise ValueError("article_open 只接受 http(s) 链接")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if host != "xiumi.us" and not host.endswith(".xiumi.us"):
            raise ValueError("article_open 只支持秀米（xiumi.us）图文链接")
        page = self._require_page()
        leave: dict[str, Any] = {}
        if self._editor_present(page):
            leave = self._leave_editor_guarded(page, discard=params.get("discard") is True)
            if not leave.get("ok"):
                leave["action"] = "open"
                return leave
        self._handle_restore_dialog(page)
        self._goto(page, url, settle=6.0)
        self._handle_restore_dialog(page)
        if self._is_auth_page(page) or login_button_visible(page):
            self._opened_article = None
            raise RuntimeError(
                "未登录或登录已失效：打开图文时页面仍显示「登录」入口，无法读取。"
                "请先在可见浏览器窗口完成登录。"
            )
        if not self._wait_editor(page, timeout_ms=20000, dismiss_restore=True):
            self._opened_article = None
            raise RuntimeError("未检测到秀米图文编辑器，可能链接不是图文页或页面尚未加载完成。")
        self._raise_editor_load_error(page)
        snapshot = self._editor_snapshot(page, paragraph_limit=10, with_body_html=True)
        if not snapshot.get("articleId"):
            self._opened_article = None
            raise RuntimeError(
                "未从编辑器 URL 识别出 articleId，无法建立修改记录；请确认传入的是已有图文链接。"
            )
        if article_id and snapshot["articleId"] != article_id:
            self._opened_article = None
            raise RuntimeError(
                f"打开的 articleId（{snapshot['articleId']}）与请求的 {article_id} 不一致，已拒绝记录。"
            )
        self._new_article = False
        self._scroll_editor_to_top(page)
        self._opened_article = dict(snapshot)
        self._document_token = uuid.uuid4().hex
        self._edit_inspection = None
        self._saved_baseline = self._guard_snapshot(page)
        result: dict[str, Any] = {
            "title": snapshot["title"],
            "textPreview": snapshot["textPreview"],
            "charCount": snapshot["charCount"],
            "articleId": snapshot["articleId"],
            "url": snapshot["url"],
            "editable": snapshot["editable"],
            "paragraphCount": snapshot["paragraphCount"],
            "paragraphs": snapshot["paragraphs"],
            "imageCount": snapshot.get("imageCount", 0),
            "autoSavedPrevious": bool(leave.get("saved")),
            "discardedDraft": bool(
                self._restore_dialog_evidence
                and self._restore_dialog_evidence.get("discardedDraft")
            ),
        }
        if leave.get("saved"):
            result["previousSave"] = leave.get("saveResult")
        if self._restore_dialog_evidence:
            result["recoveredFromDirtyDraft"] = self._restore_dialog_evidence
        try:
            result["learningObservations"] = [observe_page(page)]
        except Exception as exc:
            result["learningWarning"] = f"已打开文章，但结构未学习：{type(exc).__name__}: {exc}"
        return result

    def article_inspect(self, params: dict[str, Any]) -> dict[str, Any]:
        """Read another article on a temporary page; never replace the working draft."""
        url = str(params.get("url") or "").strip()
        limit = _coerce_int(params.get("paragraphLimit"), 10)
        working = self._require_page()
        page = working
        tab = None
        try:
            if url:
                parsed = urlparse(url)
                host = (parsed.hostname or "").lower()
                if parsed.scheme not in ("http", "https") or parsed.port not in (None, 80, 443) or not (
                    host in ("xiumi.us", "xiumius.cn", "mp.weixin.qq.com") or
                    host.endswith(".xiumi.us") or host.endswith(".xiumius.cn")
                ):
                    raise ValueError("inspect 只支持秀米或微信图文链接。")
                current_id = _article_id_from_url(str(working.url or ""))
                if url != str(working.url or ""):
                    tab = working.context.new_page()
                    page = tab
                    self._goto(page, url, settle=3.0)
                    if self._is_auth_page(page):
                        raise RuntimeError("查看链接需要登录，未读取也未修改工作稿。")
                    if self._editor_present(page):
                        self._handle_restore_dialog(page)
            elif not self._editor_present(page):
                raise RuntimeError("请先 open 绑定文章，或为 inspect 提供 URL。")
            self._check_cancelled()
            snapshot = self._editor_snapshot(page, paragraph_limit=limit)
            result = {key: snapshot[key] for key in (
                "title", "textPreview", "charCount", "articleId", "url",
                "editable", "paragraphCount", "paragraphs")}
            if url and tab is not None:
                try:
                    result["learningObservations"] = [observe_page(page)]
                except Exception as exc:
                    result["learningWarning"] = f"页面已读取，但结构学习未完成：{type(exc).__name__}: {exc}"
            return result
        finally:
            if tab is not None:
                self._safe_close(tab)

    def review_metrics(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """只读审核当前编辑器：读取样式与文本，按固定规则评分；不保存、不修改。"""
        params = params or {}
        include_text = params.get("includeText") is not False
        page = self._require_page()
        if not self._editor_present(page):
            self._wait_editor(page, timeout_ms=8000, dismiss_restore=True)
        if not self._editor_present(page):
            raise RuntimeError(
                "当前页面不是秀米图文编辑器，无法审核；请先调用 article_new 或 article_open。"
            )
        try:
            raw = page.evaluate(REVIEW_METRICS_JS)
        except Exception as exc:
            raise RuntimeError(f"读取编辑器审核数据失败：{exc}") from exc
        if not isinstance(raw, dict):
            raise RuntimeError("编辑器审核数据格式异常。")

        def norm_color(value: Any) -> str:
            text = str(value or "").strip().lower().replace(" ", "")
            match = re.fullmatch(r"rgba?\((\d{1,3}),(\d{1,3}),(\d{1,3})(?:,[0-9.]+)?\)", text)
            if match:
                return f"rgb({int(match.group(1))},{int(match.group(2))},{int(match.group(3))})"
            return text

        def is_decoration(text: str) -> bool:
            return bool(text) and bool(_REVIEW_DECOR_RE.fullmatch(text))

        cells = [item for item in (raw.get("cells") or []) if isinstance(item, dict)]
        paragraph_items = [item for item in (raw.get("paragraphs") or []) if isinstance(item, dict)]
        if paragraph_items:
            # 秀米正文常把多段写在同一个 cell 的多个 <p> 里；审核必须以段落
            # 为单位，而不是按 contenteditable 的数量统计。组件里的单段短标题
            # 不是正文段落，正文存在多段时不计入 paragraphCount。
            counts = Counter(int(item.get("cellIndex") or 0) for item in paragraph_items)
            max_per_cell = max(counts.values()) if counts else 0
            selected = []
            for item in paragraph_items:
                text = str(item.get("text") or "").strip()
                cell_index = int(item.get("cellIndex") or 0)
                if (
                    max_per_cell >= 2
                    and counts.get(cell_index) == 1
                    and len(text) < 20
                ):
                    continue
                selected.append(item)
            content = [item for item in selected if str(item.get("text") or "").strip()]
            empty_segments = sum(
                1 for item in paragraph_items if not str(item.get("text") or "").strip()
            )
            cell_count = len(cells)
        else:
            content = [item for item in cells if str(item.get("text") or "").strip()]
            empty_segments = sum(1 for item in cells if not str(item.get("text") or "").strip())
            cell_count = len(content)
        if not content:
            raise RuntimeError("当前编辑器没有可审核的正文段落。")
        paragraphs = [str(item.get("text") or "") for item in content]
        char_count = sum(len(item) for item in paragraphs)
        font_sizes = sorted(
            {
                round(float(item["fontSize"]), 1)
                for item in content
                if isinstance(item.get("fontSize"), (int, float))
            }
        )
        colors = sorted(
            {
                norm_color(item.get("color"))
                for item in content
                if norm_color(item.get("color"))
            }
        )
        font_weights = sorted(
            {
                str(item.get("fontWeight") or "").strip()
                for item in content
                if str(item.get("fontWeight") or "").strip()
            }
        )
        line_heights = sorted(
            {
                round(float(item["lineHeight"]), 1)
                for item in content
                if isinstance(item.get("lineHeight"), (int, float))
            }
        )
        if font_sizes:
            size_counter = Counter(
                round(float(item["fontSize"]), 1)
                for item in content
                if isinstance(item.get("fontSize"), (int, float))
            )
            body_size = float(size_counter.most_common(1)[0][0])
            max_size = float(max(font_sizes))
        else:
            body_size = 0.0
            max_size = 0.0
        hierarchy_ratio = round(max_size / body_size, 2) if body_size else 0.0
        decorations = [item for item in content if is_decoration(str(item.get("text") or ""))]
        decoration_segments = len(decorations)
        centered_segments = sum(
            1 for item in content if str(item.get("textAlign") or "").lower() == "center"
        )
        body_cells = [
            item
            for item in content
            if not is_decoration(str(item.get("text") or ""))
            and isinstance(item.get("fontSize"), (int, float))
            and abs(float(item["fontSize"]) - body_size) <= 1.0
            and len(str(item.get("text") or "")) >= 10
        ]
        if not body_cells:
            body_cells = [
                item
                for item in content
                if not is_decoration(str(item.get("text") or ""))
                and len(str(item.get("text") or "")) >= 10
            ]
        body_lengths = [len(str(item.get("text") or "")) for item in body_cells]
        average_body_chars = round(sum(body_lengths) / len(body_lengths), 1) if body_lengths else 0.0
        margins = [
            round(float(item["marginBottom"]), 1)
            for item in body_cells
            if isinstance(item.get("marginBottom"), (int, float))
        ]
        margin_spread = round(max(margins) - min(margins), 1) if len(margins) >= 2 else 0.0

        # 固定规则评分：层次 25 + 一致性 25 + 间距 20 + 装饰 15 + 可读 15
        dimensions: list[dict[str, Any]] = []
        style_variety = 0.0
        if len(font_weights) >= 2:
            style_variety += 5.0
        if len(colors) >= 2:
            style_variety += 4.0
        if centered_segments >= 1:
            style_variety += 4.0
        hierarchy_base = 25.0 if hierarchy_ratio >= 1.6 else (15.0 if hierarchy_ratio >= 1.3 else 7.5)
        hierarchy_score = min(25.0, hierarchy_base + style_variety)
        dimensions.append(
            {
                "key": "hierarchy",
                "name": "视觉层次",
                "score": hierarchy_score,
                "max": 25,
                "detail": (
                    f"最大字号 {max_size:g}px / 正文字号 {body_size:g}px，比值 {hierarchy_ratio:g}；"
                    f"字重/颜色/居中差异 +{style_variety:g}；建议标题明显大于正文。"
                ),
            }
        )

        consistency_score = 25.0
        if len(font_sizes) > 4:
            consistency_score -= (len(font_sizes) - 4) * 5
        if len(colors) > 4:
            consistency_score -= (len(colors) - 4) * 5
        consistency_score = max(0.0, consistency_score)
        dimensions.append(
            {
                "key": "consistency",
                "name": "字号与配色一致性",
                "score": consistency_score,
                "max": 25,
                "detail": (
                    f"字号 {len(font_sizes)} 种（{', '.join(f'{v:g}px' for v in font_sizes) or '无'}）；"
                    f"颜色 {len(colors)} 种。建议字号不超过 4 种、颜色不超过 4 种。"
                ),
            }
        )

        if len(margins) < 2:
            spacing_score = 20.0
            spacing_detail = "正文段数不足两段，暂不扣间距分。"
        elif margin_spread <= 2:
            spacing_score = 20.0
            spacing_detail = f"正文段 marginBottom 差值 {margin_spread:g}px，段距一致。"
        else:
            spacing_score = round(20.0 * max(0.0, 1.0 - (margin_spread - 2.0) / 10.0), 1)
            spacing_detail = f"正文段 marginBottom 差值 {margin_spread:g}px，段距不一致。"
        dimensions.append(
            {
                "key": "spacing",
                "name": "段落间距",
                "score": spacing_score,
                "max": 20,
                "detail": spacing_detail,
            }
        )

        if 1 <= decoration_segments <= 3:
            decoration_score = 15.0
        elif decoration_segments == 4:
            decoration_score = 12.0
        else:
            decoration_score = 7.0
        dimensions.append(
            {
                "key": "decoration",
                "name": "装饰节奏",
                "score": decoration_score,
                "max": 15,
                "detail": f"识别到 {decoration_segments} 个装饰段，建议 1~3 个。",
            }
        )

        if 20 <= average_body_chars <= 120:
            readability_score = 15.0
        elif average_body_chars <= 0:
            readability_score = 7.5
        elif average_body_chars < 20:
            readability_score = round(15.0 * average_body_chars / 20.0, 1)
        else:
            readability_score = round(15.0 * 120.0 / average_body_chars, 1)
        readability_score = max(0.0, min(15.0, readability_score))
        dimensions.append(
            {
                "key": "readability",
                "name": "正文可读性",
                "score": readability_score,
                "max": 15,
                "detail": f"正文段平均 {average_body_chars:g} 字，建议每段 20~120 字。",
            }
        )

        total = round(sum(item["score"] for item in dimensions), 1)
        grade = "A" if total >= 90 else ("B" if total >= 75 else ("C" if total >= 60 else "D"))
        findings: list[dict[str, str]] = []
        title = str(raw.get("title") or "").strip()
        if not title:
            findings.append(
                {
                    "severity": "low",
                    "problem": "图文标题为空。",
                    "suggestion": "补一个能概括全文的标题，并让标题字号明显大于正文。",
                }
            )
        if hierarchy_ratio < 1.6:
            findings.append(
                {
                    "severity": "medium" if hierarchy_ratio < 1.3 else "low",
                    "problem": f"标题与正文字号接近（比值 {hierarchy_ratio:g}）。",
                    "suggestion": "把标题字号提高到正文的 1.6 倍以上，例如正文 15~16px、标题 26~32px。",
                }
            )
        if len(font_sizes) > 4:
            findings.append(
                {
                    "severity": "medium",
                    "problem": f"字号共 {len(font_sizes)} 种，视觉层级略散。",
                    "suggestion": "收敛为标题、小标题、正文三档，最多保留 4 种字号。",
                }
            )
        if len(colors) > 4:
            findings.append(
                {
                    "severity": "medium",
                    "problem": f"正文颜色共 {len(colors)} 种。",
                    "suggestion": "正文统一为主文字色，只保留 1 个强调色和 1 个辅助色。",
                }
            )
        if len(margins) >= 2 and margin_spread > 2:
            findings.append(
                {
                    "severity": "medium",
                    "problem": f"正文段距不一致（最大差值 {margin_spread:g}px）。",
                    "suggestion": "统一正文段落的 marginBottom，建议 16~18px。",
                }
            )
        if decoration_segments == 0:
            findings.append(
                {
                    "severity": "medium",
                    "problem": "没有装饰段，长文容易显得平。",
                    "suggestion": "在 1~3 个转场处加简短分隔线或符号，不要每段都加。",
                }
            )
        elif decoration_segments > 4:
            findings.append(
                {
                    "severity": "medium",
                    "problem": f"装饰段 {decoration_segments} 个，节奏偏碎。",
                    "suggestion": "删减到 1~3 个，把装饰留给真正的转场。",
                }
            )
        if average_body_chars < 20:
            findings.append(
                {
                    "severity": "low",
                    "problem": f"正文段平均只有 {average_body_chars:g} 字，信息密度偏低。",
                    "suggestion": "合并过短的段落，每段补到 20~120 字。",
                }
            )
        elif average_body_chars > 120:
            findings.append(
                {
                    "severity": "medium",
                    "problem": f"正文段平均 {average_body_chars:g} 字，长段不易读。",
                    "suggestion": "按语义拆成 2~3 段，每段控制在 20~120 字。",
                }
            )
        if empty_segments > 1:
            findings.append(
                {
                    "severity": "low",
                    "problem": f"存在 {empty_segments} 个空白段。",
                    "suggestion": "删除多余空段，避免正文出现无法解释的留白。",
                }
            )
        if centered_segments > max(1, len(content) // 2):
            findings.append(
                {
                    "severity": "low",
                    "problem": f"居中段落 {centered_segments} 个，超过正文一半。",
                    "suggestion": "正文优先左对齐或两端对齐，只在标题/引语处居中。",
                }
            )

        metrics: dict[str, Any] = {
            "title": title,
            "url": str(raw.get("url") or page.url or ""),
            "articleId": _article_id_from_url(str(raw.get("url") or page.url or "")),
            "cellCount": cell_count,
            "emptySegments": empty_segments,
            "charCount": char_count,
            "paragraphCount": len(paragraphs),
            "fontSizes": font_sizes,
            "colors": colors,
            "fontWeights": font_weights,
            "lineHeights": line_heights,
            "maxFontSize": max_size,
            "bodyFontSize": body_size,
            "hierarchyRatio": hierarchy_ratio,
            "averageBodyChars": average_body_chars,
            "marginSpread": margin_spread,
            "decorationSegments": decoration_segments,
            "centeredSegments": centered_segments,
            "paragraphs": [item[:60] for item in paragraphs] if include_text else [],
        }
        return {
            "score": total,
            "grade": grade,
            "dimensions": dimensions,
            "findings": findings,
            "metrics": metrics,
        }

    def set_cell_styles(self, params: dict[str, Any]) -> dict[str, Any]:
        """只改样式、不动文本：按 cellPath 或起始下标批量套用排版样式。

        与 article_edit 的 set_cell 不同，这里不写 cell.text，因此不会清空
        已有正文；用于「每段独立 cell 写完后逐格补样式」的场景。
        """
        page = self._require_page()
        if not self._editor_present(page):
            raise RuntimeError("当前不是图文编辑器。")
        items = params.get("items")
        if not isinstance(items, list) or not items:
            raise ValueError("set_cell_styles 需要非空 items 数组")
        paths = params.get("paths") if isinstance(params.get("paths"), list) else []
        payload: dict[str, Any] = {"items": items}
        if paths:
            payload["paths"] = paths
        start = params.get("startIndex")
        if isinstance(start, int) and start > 0:
            payload["startIndex"] = start
        self._check_cancelled()
        result = page.evaluate(COMPOSE_SET_CELLS_JS, payload)
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(
                f"批量设样式失败：{(result or {}).get('error') or (result or {}).get('reason') or '未知原因'}"
            )
        return {"ok": True, "applied": result.get("applied") or [],
                "cellCount": len(self._cells_state(page).get("cells") or [])}

    def article_edit(self, params: dict[str, Any]) -> dict[str, Any]:
        """Inspect a bound document; edit only a current, explicit cell selection."""
        page = self._require_page()
        action = str(params.get("action") or "inspect").strip()
        allowed = ("inspect", "replace_body", "set_cell", "clear_cell", "append_cell")
        if action not in allowed:
            raise ValueError(f"action 必须是 {'/'.join(allowed)} 之一")
        if action == "inspect":
            self._check_cancelled()
            if not self._editor_present(page):
                raise RuntimeError("当前不是图文编辑器。")
            current = self._model_snapshot(page)
            if current["articleId"]:
                self._opened_article = {"articleId": current["articleId"], "url": current["url"]}
        else:
            current = self._assert_mutation_target(page, params)
        if action != "inspect":
            if params.get("confirm") is not True:
                raise ValueError("编辑需要 confirm=true，请先 inspect 核对目标和修改方案。")
            inspection = self._edit_inspection
            if not inspection or not inspection.get("cells"):
                raise RuntimeError("尚未 inspect，或检查后未取到单元格列表；拒绝使用旧的单元格编号。")
            if inspection.get("revision") != current["modelHash"]:
                # 模型摘要漂移时用「单元格数量与文本指纹」再判一次：只要结构与
                # 文字都还对得上就放行，避免 Angular 渲染态/图片懒加载导致误拦。
                prev = inspection.get("cells") or []
                prev_texts = [str(item.get("text") or "") for item in prev if isinstance(item, dict)]
                curr_texts = [
                    str(item.get("text") or "")
                    for item in (self._model_cells(page) or [])
                    if isinstance(item, dict)
                ]
                if prev_texts != curr_texts:
                    raise RuntimeError("尚未 inspect，或检查后文章已变化；拒绝使用旧的单元格编号。")
        html = params.get("html")
        if html is not None and not isinstance(html, str):
            raise ValueError("params.html 必须是字符串")
        if action == "replace_body" and not (isinstance(html, str) and html.strip()):
            raise ValueError("replace_body 需要非空 params.html")
        if action in ("set_cell", "clear_cell") and not isinstance(params.get("cellIndex"), int):
            raise ValueError(f"{action} 需要整数 cellIndex")
        if action == "set_cell" and not (
            html is not None or isinstance(params.get("text"), str) or isinstance(params.get("style"), dict)
        ):
            raise ValueError("set_cell 需要 html、text 或 style")
        payload = {
            "action": action, "html": html, "text": params.get("text"),
            "cellIndex": params.get("cellIndex"), "cellIndexes": params.get("cellIndexes"),
            "style": params.get("style") if isinstance(params.get("style"), dict) else None,
            "expectedCells": (self._edit_inspection or {}).get("cells") if action != "inspect" else None,
        }
        self._check_cancelled()
        result = page.evaluate(ARTICLE_EDIT_JS, payload)
        if not isinstance(result, dict) or not result.get("ok"):
            raise RuntimeError(f"就地编辑失败：{(result or {}).get('reason') or (result or {}).get('error') or '未知原因'}")
        if action in ("replace_body", "clear_cell"):
            # ARTICLE_EDIT_JS 只把被清空的格写成空段落；这里按被清理格的
            # cellPath 定向删除模型节点，避免留下空壳影响 review 统计。
            before_cells = result.get("cells") or []
            if action == "replace_body":
                host = result.get("host")
                cleared_indexes = [
                    i for i in (params.get("cellIndexes") or [])
                    if isinstance(i, int) and i != host
                ]
            else:
                cleared_indexes = [result.get("index")]
            only_paths = [
                str(before_cells[i].get("path") or "")
                for i in cleared_indexes
                if isinstance(i, int) and 0 <= i < len(before_cells)
            ]
            removed = self._clean_empty_placeholder_cells(
                page, wait_ms=500, include_all_empty=True, only_paths=only_paths
            )
            refreshed = page.evaluate(ARTICLE_EDIT_JS, {"action": "inspect"})
            if isinstance(refreshed, dict) and refreshed.get("ok"):
                result["cells"] = refreshed.get("cells") or []
                result["cellCount"] = refreshed.get("cellCount", result.get("cellCount"))
            result["removedEmptyCells"] = removed
        after = self._model_snapshot(page)
        raw_cells = result.get("cells") or []
        self._edit_inspection = {"revision": after["modelHash"], "articleId": after["articleId"], "cells": raw_cells}
        result["cells"] = [
            {key: item.get(key) for key in ("index", "path", "text")}
            for item in raw_cells if isinstance(item, dict)
        ]
        result.update({"action": action, "url": str(page.url or ""), "articleId": after["articleId"],
                       "revision": after["modelHash"]})
        if params.get("save") is True and action != "inspect":
            result["save"] = self.article_save({"confirm": True})
            result["ok"] = bool(result["save"].get("saved") and result["save"].get("saveVerified"))
        return result

    def article_update(self, params: dict[str, Any]) -> dict[str, Any]:
        """在已 article_open 的图文上写入新正文；confirm 必须严格为 true。"""
        if params.get("confirm") is not True:
            raise RuntimeError(
                "拒绝修改：article_update 需要 confirm=true。"
                "请先 article_open + article_inspect 把对象和当前内容展示给用户，确认后再修改。"
            )
        opened = self._opened_article
        if not opened or not opened.get("articleId"):
            raise RuntimeError("请先 article_open 并确认要修改的对象，再调用 article_update。")
        page = self._require_page()
        if not self._editor_present(page):
            raise RuntimeError("当前页面不是图文编辑器，无法修改；请重新 article_open。")
        current_id = _article_id_from_url(str(page.url or ""))
        if current_id != opened.get("articleId"):
            raise RuntimeError(
                "当前编辑器已不在 article_open 记录的图文上，请重新 article_open 并确认对象。"
            )
        html = params.get("html")
        blocks = params.get("blocks")
        if html is not None and not isinstance(html, str):
            raise ValueError("params.html 必须是字符串")
        if blocks is not None:
            if not isinstance(blocks, list):
                raise ValueError("params.blocks 必须是数组")
            parts = []
            for item in blocks:
                if isinstance(item, dict):
                    item = item.get("text") or item.get("content") or ""
                if not isinstance(item, str):
                    raise ValueError("params.blocks 的每一项必须是字符串或 {text} 对象")
                text = item.strip()
                if text:
                    parts.append(f"<p>{escape(text)}</p>")
            if not parts:
                raise ValueError("params.blocks 不能为空")
            html = "\n".join(parts)
        if not isinstance(html, str) or not html.strip():
            raise ValueError("article_update 至少需要非空的 params.html 或 params.blocks")
        # Reuse the inspected-cell path; the old updater touched every text cell,
        # including unrelated component titles.
        return self.article_edit({**params, "action": "replace_body", "html": html})

    def article_new(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """打开空白图文编辑器，返回空标题状态；新建前先做图文容量守卫。"""
        params = params or {}
        restore_policy = "discard" if params.get("discard") is True else "block"
        current = self._current_page()
        leave: dict[str, Any] = {}
        if current is not None:
            self._handle_restore_dialog(current, policy=restore_policy if params.get("discard") is True else "block")
            leave = self._leave_editor_guarded(current, discard=params.get("discard") is True)
            if not leave.get("ok"):
                leave["action"] = "new"
                return leave
        blocked = self._quota_guard_articles(
            params.get("limitOverride") if isinstance(params.get("limitOverride"), dict) else None
        )
        if blocked is not None:
            return blocked
        self._new_article = False
        self._opened_article = None
        page = self._require_page()
        self._handle_restore_dialog(page, policy=restore_policy)
        self._goto(page, EDITOR_URL, settle=5.0)
        self._handle_restore_dialog(page, policy=restore_policy)
        if self._is_auth_page(page):
            raise RuntimeError("打开编辑器时被重定向到登录页，请先完成登录。")

        # SPA 内部导航可能仍停留在上一篇（甚至未保存的）图文上；强制刷新/改 hash 路由。
        if not self._editor_present(page):
            try:
                page.reload(wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                _log(f"article_new 刷新编辑器失败: {type(exc).__name__}: {exc}")
            time.sleep(2.0)
            self._handle_restore_dialog(page, policy=restore_policy)
        if not self._editor_present(page):
            self._goto(page, EDITOR_NEW_URL, settle=4.0)
            self._handle_restore_dialog(page, policy=restore_policy)

        if not self._wait_editor(page, timeout_ms=20000, dismiss_restore=True, restore_policy=restore_policy):
            raise RuntimeError(
                "未能打开秀米空白图文编辑器（未找到标题输入框或正文区域）。"
                "页面可能仍被「恢复未保存草稿」弹窗遮挡，或未完成登录。"
            )

        title, text, _present = self._read_editor(page)
        url = str(page.url or "")
        stale = bool(title.strip()) or ("/paper/for/new" not in url)
        if stale:
            # 从列表页点「新建一个图文」是最可靠的重置方式；导航前先清恢复弹窗。
            self._goto(page, LIST_URL, settle=3.0)
            self._handle_restore_dialog(page, policy=restore_policy)
            clicked = self._click_visible_text(page, ["新建一个图文"], exact=True)
            if not clicked:
                self._goto(page, EDITOR_NEW_URL, settle=4.0)
                self._handle_restore_dialog(page, policy=restore_policy)
            if not self._wait_editor(page, timeout_ms=20000, dismiss_restore=True, restore_policy=restore_policy):
                raise RuntimeError(
                    "未能通过「新建一个图文」打开空白稿；请先在可见浏览器窗口手动新建并确认。"
                )
            title, text, _present = self._read_editor(page)
            url = str(page.url or "")
            if title.strip():
                raise RuntimeError(
                    f"article_new 打开后仍显示已有图文标题「{title}」，为避免误写已有图文已中止。"
                    "请先在可见浏览器窗口手动新建空白图文。"
                )

        self._new_article = True
        self._opened_article = None
        self._document_token = uuid.uuid4().hex
        self._edit_inspection = None
        self._scroll_editor_to_top(page)
        self._saved_baseline = self._guard_snapshot(page)
        result: dict[str, Any] = {
            "title": title,
            "textPreview": text[:800],
            "editable": True,
            "url": url,
            "discardedDraft": bool(
                self._restore_dialog_evidence
                and self._restore_dialog_evidence.get("discardedDraft")
            ),
            "autoSavedPrevious": bool(leave.get("saved")),
        }
        if leave.get("saved"):
            result["previousSave"] = leave.get("saveResult")
        if self._restore_dialog_evidence:
            result["recoveredFromDirtyDraft"] = self._restore_dialog_evidence
        return result

    def article_write(self, params: dict[str, Any]) -> dict[str, Any]:
        """把标题与正文写入当前图文。

        默认只允许写入新建稿（防止误改已有文章）；传 ``append=true`` 时允许
        在已打开的稿件末尾追加段落——追加只增不改，不会碰已有内容。
        """
        append_mode = bool(params.get("append"))
        if not self._new_article and not append_mode:
            raise RuntimeError(
                "当前不是新建图文，为避免误改已有文章已拒绝写入。"
                "请先调用 article_new 新建空白图文，向用户确认后再写入；"
                "若确实只是往已打开的文章末尾追加段落，传 append=true。"
            )
        page = self._require_page()
        if not self._editor_present(page):
            raise RuntimeError("当前页面不是可写的图文编辑器，请先调用 article_new。")
        title = params.get("title")
        title = "" if title is None else str(title)
        html = params.get("html")
        paragraphs = params.get("paragraphs")
        if html is not None and not isinstance(html, str):
            raise ValueError("params.html 必须是字符串")
        if paragraphs is not None:
            if not isinstance(paragraphs, list):
                raise ValueError("params.paragraphs 必须是字符串数组")
            for item in paragraphs:
                if not isinstance(item, (str, dict)):
                    raise ValueError("params.paragraphs 的每一项必须是字符串或 {text} 对象")

        has_html = isinstance(html, str) and bool(html.strip())
        body_items: list[str] = []
        if isinstance(paragraphs, list):
            for item in paragraphs:
                if isinstance(item, dict):
                    item = item.get("text") or item.get("content") or ""
                if isinstance(item, str) and item.strip():
                    body_items.append(item.strip())
        if not title.strip() and not has_html and not body_items:
            raise ValueError("至少需要 title、html 或 paragraphs 之一")

        if title.strip():
            try:
                title_loc = page.locator(TITLE_SELECTOR)
                if title_loc.count() == 0:
                    raise RuntimeError("未找到标题输入框")
                title_loc.first.click()
                self._check_cancelled()
                title_loc.first.fill(title)
            except Exception as exc:
                raise RuntimeError(f"写入标题失败：{exc}") from exc

        model_updated = False
        model_message = ""
        if has_html or body_items:
            if has_html:
                try:
                    segments, plain_texts = _split_html_segments(html)
                    if not segments:
                        raise RuntimeError("HTML 中没有可写入的正文段落")
                    styles = [_cell_style_from_segment(segment) for segment in segments]
                    # includeTitle 默认会把标题也生成 h1；正文不应重复标题。
                    if (
                        title.strip()
                        and _compose_norm_text(plain_texts[0]) == _compose_norm_text(title)
                        and re.match(r"<h[1-6]\b", segments[0].strip(), re.I)
                    ):
                        segments = segments[1:]
                        plain_texts = plain_texts[1:]
                        styles = styles[1:]
                    if not segments:
                        raise RuntimeError("HTML 中只有标题段，没有可写入的正文段落")
                    html_to_write = "\n".join(segments)
                    self._write_html_body_once(page, html_to_write)
                    model_updated = True
                except Exception as exc:
                    raise RuntimeError(f"写入 HTML 正文失败：{exc}") from exc
            else:
                segments = [f"<p>{escape(item)}</p>" for item in body_items]
                try:
                    self._compose_native_text_cells(
                        page, segments, body_items, [{} for _ in body_items]
                    )
                    model_updated = True
                except Exception as exc:
                    raise RuntimeError(f"逐段写入纯文本失败：{exc}") from exc
            time.sleep(1.2)
            title_now, text, _present = self._read_editor(page)
            if (body_items or has_html) and not text:
                raise RuntimeError("写入后未检测到正文文本，请查看浏览器窗口确认编辑器状态。")
        else:
            time.sleep(0.4)
            title_now, text, _present = self._read_editor(page)

        if not model_updated:
            model_message = "未写入正文内容（仅更新标题），正文未进入秀米保存模型。"
        written_chars = len(re.sub(r"\s+", "", text))
        snapshot = self._editor_snapshot(page, paragraph_limit=1)
        result = {
            "title": title_now,
            "textPreview": text[:800],
            "writtenChars": written_chars,
            "charCount": int(snapshot.get("charCount") or 0),
            "articleId": str(snapshot.get("articleId") or ""),
            "url": str(snapshot.get("url") or ""),
            "modelUpdated": model_updated,
        }
        if model_message:
            result["message"] = model_message
        self._scroll_editor_to_top(page)
        return result

    def _compose_fidelity(
        self, blocks: list[dict[str, Any]], segments: list[str]
    ) -> dict[str, Any]:
        text_blocks = [block for block in blocks if block["type"] != "image"]
        if len(segments) != len(text_blocks):
            return {
                "fidelity": "failed",
                "verifiedBlocks": len(blocks),
                "firstDiff": {
                    "blockIndex": 0,
                    "expected": "同数量的正文段落",
                    "actual": f"准备了 {len(segments)} 段，但有 {len(text_blocks)} 个文本块",
                },
            }
        expected = [_compose_norm_text(block["text"]) for block in text_blocks]
        actual = [_compose_norm_text(_compose_text_from_html(segment)) for segment in segments]
        for index, (want, got) in enumerate(zip(expected, actual)):
            if want != got:
                return {
                    "fidelity": "failed",
                    "verifiedBlocks": len(blocks),
                    "firstDiff": {
                        "blockIndex": text_blocks[index]["blockIndex"],
                        "expected": want[:300],
                        "actual": got[:300],
                    },
                }
        return {
            "fidelity": "passed",
            "verifiedBlocks": len(blocks),
            "firstDiff": None,
            "textBlocks": len(text_blocks),
            "imageBlocks": len(blocks) - len(text_blocks),
        }

    def _compose_prepare_images(
        self, image_blocks: list[dict[str, Any]]
    ) -> list[dict[str, Any]]:
        prepared: list[dict[str, Any]] = []
        for block in image_blocks:
            raw_file = str(block.get("file") or "").strip()
            asset_id = str(block.get("assetId") or "").strip()
            if raw_file:
                path = Path(raw_file).expanduser()
                if not path.is_absolute():
                    path = path.resolve()
                if path.suffix.lower() not in IMAGE_UPLOAD_EXTS:
                    raise ValueError(
                        f"图片 {path.name} 的扩展名 {path.suffix or '(无)'} 不受支持；"
                        f"允许 {', '.join(sorted(IMAGE_UPLOAD_EXTS))}。"
                    )
                if not path.is_file():
                    raise ValueError(f"图片文件不存在：{path}")
                if path.stat().st_size > IMAGE_UPLOAD_MAX_BYTES:
                    raise ValueError(f"图片 {path.name} 超过 10MB 上限。")
                prepared.append(
                    {
                        "blockIndex": block["blockIndex"],
                        "file": str(path),
                        "name": path.name,
                        "assetId": "",
                    }
                )
            else:
                prepared.append(
                    {
                        "blockIndex": block["blockIndex"],
                        "file": "",
                        "name": "",
                        "assetId": asset_id,
                    }
                )
        return prepared

    def _write_html_body_once(self, page, html: str) -> dict[str, Any]:
        """把整篇 HTML 一次写入末尾空白正文 cell；不逐段定位，避免组件页重复格。"""
        result = page.evaluate(WRITE_HTML_SINGLE_CELL_JS, {"html": html})
        if not isinstance(result, dict) or not result.get("ok"):
            reason = (result or {}).get("reason") or (result or {}).get("error") or "未知原因"
            raise RuntimeError(f"写入整篇 HTML 失败：{reason}")
        page.wait_for_timeout(700)
        return result

    def _compose_native_text_cells(
        self,
        page,
        segments: list[str],
        plain_texts: list[str],
        styles: list[dict[str, Any]] | None = None,
    ) -> int:
        """按秀米原生“空白区点击输入”流程逐格创建文本 cell。

        已验证：在最后一个文本 cell 下方点击空白区会生成新的原生空 cell；
        直接键盘输入后保存可持久化。创建完成后再把每格内容替换为纯 <p> 文本，
        样式写入 scope.cell.style（t25 已验证这是唯一可靠的样式持久化通道）。
        """
        self._check_cancelled()
        leaf_selector = ".tn-working-pallet div[contenteditable='true']"
        nested_selector = "div[contenteditable='true']"

        def leaves():
            return page.locator(leaf_selector).filter(has_not=page.locator(nested_selector))

        def last_nonempty_index() -> int:
            current = leaves()
            for idx in range(current.count() - 1, -1, -1):
                try:
                    plain = current.nth(idx).inner_text().strip()
                except Exception:
                    plain = ""
                if plain:
                    return idx
            return current.count() - 1

        def create_cell_below(anchor_index: int) -> None:
            """在指定 cell 之后创建并聚焦一个新的原生正文 cell。

            首选秀米自带的「后插空行」：选中任意栏目后，浮动工具栏里就有这个
            按钮，点它等于「在选中栏目后面另起一行」。它比点空白坐标稳定得多，
            组件（分割线/卡片）夹在中间时也不会点不动。
            坐标点击只在按钮不可用时兜底。
            """
            try:
                current = leaves()
                if current.count() > 0 and anchor_index >= 0:
                    anchor = current.nth(min(anchor_index, current.count() - 1))
                    self._check_cancelled()
                    anchor.click()
                    page.wait_for_timeout(350)
                    btn = page.get_by_text("后插空行", exact=True)
                    if btn.count() > 0 and btn.first.is_visible():
                        n_before = current.count()
                        self._check_cancelled()
                        btn.first.click()
                        page.wait_for_timeout(750)
                        if leaves().count() > n_before:
                            created = leaves().nth(leaves().count() - 1)
                            self._check_cancelled()
                            created.click()
                            return
            except Exception:
                pass
            current = leaves()
            if current.count() == 0 or anchor_index < 0:
                raise RuntimeError("当前编辑器没有可定位的正文单元格。")
            # 秀米编辑区是内部滚动容器；新 cell 在视口外时坐标点击无效，
            # 先把滚动容器推到底部，再重新取 anchor 的 bounding box。
            try:
                self._check_cancelled()
                page.evaluate(
                    "() => { const el = document.querySelector('.tn-working-pallet') || "
                    "document.querySelector('.tn-editing-desk'); "
                    "if (el) el.scrollTop = el.scrollHeight; }"
                )
                page.wait_for_timeout(300)
            except Exception:
                pass
            anchor = leaves().nth(anchor_index)
            anchor.scroll_into_view_if_needed()
            box = anchor.bounding_box()
            if not box:
                raise RuntimeError("无法定位正文单元格，停止创建。")
            click_x = box["x"] + box["width"] / 2
            before = current.count()
            # 组件页底部留白很窄；以最后一个非空 cell 为锚点，从近到远逐档尝试。
            for offset in (12, 22, 34, 48, 70):
                self._check_cancelled()
                page.mouse.click(click_x, box["y"] + box["height"] + offset)
                page.wait_for_timeout(650)
                if leaves().count() > before:
                    created = leaves().nth(leaves().count() - 1)
                    self._check_cancelled()
                    created.click()
                    return
            # 兜底：anchor 紧邻下方可能压着组件（分割线、卡片），点不到空白。
            # 改点编辑器内容区最底部的空白，秀米会在正文末尾插入新的正文 cell。
            try:
                pallet = page.locator(".tn-working-pallet").first
                if pallet.count() > 0:
                    pbox = pallet.bounding_box()
                    if pbox:
                        try:
                            self._check_cancelled()
                            vh = float(page.evaluate("() => window.innerHeight"))
                        except Exception:
                            vh = 900.0
                        bottom_y = min(pbox["y"] + pbox["height"] - 24, vh - 40)
                        for dx in (0, 40, -40, 80, -80, 120):
                            if leaves().count() > before:
                                break
                            self._check_cancelled()
                            page.mouse.click(pbox["x"] + pbox["width"] / 2 + dx, bottom_y)
                            page.wait_for_timeout(650)
                        if leaves().count() > before:
                            created = leaves().nth(leaves().count() - 1)
                            self._check_cancelled()
                            created.click()
                            return
            except Exception:
                pass
            raise RuntimeError(
                "秀米未能在当前文本 cell 之后创建新的原生文本块；"
                "请先在浏览器中手工点一下正文下方空白处确认编辑器可插入。"
            )

        # 追加模式：组件 cell（标题、桂冠、横条等）已存在时必须排在它们之后。
        base = leaves().count()
        last_plain = ""
        if base > 0:
            try:
                last_plain = leaves().nth(base - 1).inner_text().strip()
            except Exception:
                last_plain = ""

        plain_mode = not any(isinstance(s, dict) and s for s in (styles or []))
        if plain_mode:
            # 每段独立一个新 cell（用户要求：一段文本一个单元格）。
            # 这样插图时可以用「选中该段落 cell → 后插空行 → 在新空 cell 里插图」，
            # 不会像「一个 cell 塞多段」那样被插图切开、后续锚点全部失效。
            for index, text in enumerate(plain_texts):
                self._check_cancelled()
                if index == 0:
                    if last_plain:
                        create_cell_below(last_nonempty_index())
                    else:
                        try:
                            placeholder = page.get_by_text("点击输入文字", exact=True).last
                            if placeholder.is_visible():
                                placeholder.click()
                            else:
                                raise RuntimeError("placeholder not visible")
                        except Exception:
                            blank_state = self._cells_state(page).get("cells") or []
                            if blank_state:
                                blank_cell = blank_state[-1]
                                page.mouse.click(
                                    blank_cell["x"] + blank_cell["w"] / 2,
                                    blank_cell["y"] + blank_cell["h"] / 2,
                                )
                            else:
                                leaves().nth(base - 1).click()
                    page.wait_for_timeout(500)
                else:
                    create_cell_below(last_nonempty_index())
                    page.wait_for_timeout(450)
                self._check_cancelled()
                page.keyboard.insert_text(text)
                page.wait_for_timeout(450)
            # 失焦后秀米才会把 contenteditable 的全部段落同步回 cell 模型。
            try:
                self._check_cancelled()
                page.locator("input[placeholder='请输入图文的标题']").first.click()
            except Exception:
                self._check_cancelled()
                page.keyboard.press("Escape")
            page.wait_for_timeout(1200)
            return len(self._cells_state(page).get("cells") or [])

        def focus_blank_cell() -> bool:
            """聚焦末尾的空 contenteditable；组件后它是新正文的入口。"""
            current = leaves()
            for idx in range(current.count() - 1, -1, -1):
                cell = current.nth(idx)
                try:
                    if cell.inner_text().strip():
                        continue
                    box = cell.bounding_box()
                    if not box:
                        continue
                    self._check_cancelled()
                    page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
                    page.wait_for_timeout(300)
                    return True
                except Exception:
                    continue
            return False

        if not last_plain:
            if not focus_blank_cell():
                create_cell_below(last_nonempty_index())
                page.wait_for_timeout(400)
            start_index = max(0, base - 1)
        else:
            start_index = base
            # 组件插入后秀米通常会在末尾留一个「点击输入文字」空占位 cell。
            # 直接复用它，否则「后插空行」会再建一个，导致第一段重复两次。
            reused = False
            current = leaves()
            if current.count() > 0:
                last = current.nth(current.count() - 1)
                try:
                    if not last.inner_text().strip():
                        self._check_cancelled()
                        last.click()
                        page.wait_for_timeout(300)
                        reused = True
                except Exception:
                    reused = False
            if reused:
                start_index = max(0, base - 1)
            else:
                create_cell_below(last_nonempty_index())
                page.wait_for_timeout(400)

        self._check_cancelled()
        page.keyboard.insert_text(plain_texts[0])
        page.wait_for_timeout(800)
        self._check_cancelled()
        page.keyboard.press("Escape")
        page.wait_for_timeout(800)
        for index in range(1, len(plain_texts)):
            if not focus_blank_cell():
                create_cell_below(last_nonempty_index())
                page.wait_for_timeout(400)
            self._check_cancelled()
            page.keyboard.insert_text(plain_texts[index])
            page.wait_for_timeout(700)
            self._check_cancelled()
            page.keyboard.press("Escape")
            page.wait_for_timeout(800)

        style_list = styles if isinstance(styles, list) else []
        cells_after = self._cells_state(page).get("cells") or []
        body_cells = cells_after[start_index:]
        used_paths: set[str] = set()
        items = []
        paths: list[str] = []
        for index, plain in enumerate(plain_texts):
            style = style_list[index] if index < len(style_list) else None
            items.append(
                {
                    "text": "<p>" + escape(str(plain or "")).replace("\n", "<br>") + "</p>",
                    "style": style if isinstance(style, dict) else {},
                }
            )
            match = None
            for cell in reversed(body_cells):
                path = str(cell.get("path") or "")
                if path and path not in used_paths and str(cell.get("plain") or "").strip() == str(plain or "").strip():
                    match = cell
                    break
            if match is not None:
                used_paths.add(str(match.get("path") or ""))
                paths.append(str(match.get("path") or ""))
            else:
                paths.append("")
        self._check_cancelled()
        applied = page.evaluate(
            COMPOSE_SET_CELLS_JS, {"items": items, "paths": paths, "startIndex": start_index}
        )
        if not isinstance(applied, dict) or not applied.get("ok"):
            detail = applied.get("error") if isinstance(applied, dict) else "未知错误"
            raise RuntimeError(f"写入原生文本 cell 样式失败：{detail}")
        applied_indexes = [int(i) for i in (applied.get("applied") or [])]
        if len(applied_indexes) < len(segments) or any(i < start_index for i in applied_indexes):
            raise RuntimeError(
                f"原生文本 cell 数量不足或越界：期望从 {start_index} 起写入 "
                f"{len(segments)} 段，实际应用 {applied_indexes}。"
            )
        page.wait_for_timeout(1200)
        self._clean_empty_placeholder_cells(page)
        return int(applied.get("cellCount") or (start_index + len(segments)))

    def _clean_empty_placeholder_cells(
        self, page, wait_ms: int = 0, include_all_empty: bool = False,
        only_paths: list[str] | None = None,
    ) -> int:
        """统一清理空文本 cell：默认只删 `_qiBlock` 占位；update 时允许删所有空 cell。"""
        if wait_ms > 0:
            try:
                page.wait_for_timeout(int(wait_ms))
            except Exception:
                pass
        removed = 0
        for attempt in range(3):
            try:
                value = page.evaluate(
                    COMPOSE_CLEAN_EMPTY_JS,
                    {
                        "includeAllEmpty": bool(include_all_empty),
                        "onlyPaths": list(only_paths or []),
                    },
                )
                count = int(value.get("removed") or 0) if isinstance(value, dict) else 0
                removed += count
                if count:
                    page.wait_for_timeout(700)
                    continue
                if attempt < 2:
                    page.wait_for_timeout(500)
            except Exception:
                break
        return removed

    def compose(self, params: dict[str, Any]) -> dict[str, Any]:
        """双模式图文生成：create 从零创作，layout-only 只排版并做零改动保真校验。"""
        mode = str(params.get("mode") or "").strip().lower()
        if mode not in ("create", "layout-only"):
            raise ValueError("compose.mode 必须是 create 或 layout-only")
        blocks = _compose_norm_blocks(params.get("blocks"))
        text_blocks = [block for block in blocks if block["type"] != "image"]
        image_blocks = [block for block in blocks if block["type"] == "image"]
        profile_meta: dict[str, Any] = {}
        if not text_blocks:
            raise ValueError("compose.blocks 至少需要一个 paragraph/heading/quote")
        if image_blocks and image_blocks[0]["blockIndex"] < text_blocks[0]["blockIndex"]:
            return {
                **profile_meta,
                "ok": False,
                "mode": mode,
                "reason": "unsupported",
                "fidelity": "passed",
                "verifiedBlocks": len(blocks),
                "cellCount": 0,
                "message": "图片不能作为第一个块：秀米只能把图片插在文本 cell 之后，已拒绝写入。",
            }

        theme = params.get("theme")
        if not isinstance(theme, dict):
            theme = {}
        profile_meta: dict[str, Any] = {}
        profile_id = str(params.get("profileId") or "").strip()
        if profile_id:
            profile = _profile_load(profile_id)
            theme = _profile_apply_theme(profile, theme)
            profile_meta = {
                "appliedProfile": {"id": profile_id, "name": profile.get("name") or ""},
                "appliedStyle": (
                    f"profile:{profile_id}"
                    if not theme.get("resolvedStyle")
                    else f"profile:{profile_id}/{theme.get('resolvedStyle')}"
                ),
            }
        elif theme:
            profile_meta = {
                "appliedStyle": str(
                    theme.get("resolvedStyle") or theme.get("stylePreset") or "auto"
                )
            }
        segments = [_compose_segment_html(block, theme) for block in text_blocks]
        fidelity = self._compose_fidelity(blocks, segments)
        image_specs = (
            []
            if mode == "layout-only"
            else self._compose_prepare_images(image_blocks)
        )
        generated = {
            "text": len(text_blocks),
            "heading": sum(1 for block in text_blocks if block["type"] == "heading"),
            "paragraph": sum(1 for block in text_blocks if block["type"] == "paragraph"),
            "quote": sum(1 for block in text_blocks if block["type"] == "quote"),
            "image": len(image_blocks),
        }
        cell_count = len(text_blocks) + len(image_blocks)

        if mode == "layout-only" and fidelity["fidelity"] != "passed":
            return {
                **profile_meta,
                "ok": False,
                "mode": mode,
                "fidelity": "failed",
                "firstDiff": fidelity.get("firstDiff"),
                "verifiedBlocks": len(blocks),
                "cellCount": cell_count,
                "generatedBlocks": generated,
                "message": "布局稿保真校验未通过：准备写入的 HTML 与原文不一致，已拒绝写入。",
            }

        layout_page = None
        if mode == "layout-only":
            if image_blocks:
                # layout-only 不新增/重排图片；保留现有图片，只给文本 cell 换样式。
                pass
            layout_page = self._require_page()
            self._assert_mutation_target(layout_page, params)
            opened = self._opened_article
            if not opened:
                raise RuntimeError("layout-only 必须先 article_open 打开要排版的图文。")
            if not self._editor_present(layout_page):
                raise RuntimeError("当前页面不是可排版的图文编辑器。")
            current_id = _article_id_from_url(layout_page.url) or ""
            if current_id != str(opened.get("articleId") or ""):
                raise RuntimeError(
                    "当前编辑器已不是 article_open 打开的图文，已拒绝 layout-only 写入。"
                )
            current_text = self._read_editor(layout_page)[1]
            provided_text = "\n".join(block["text"] for block in text_blocks)
            content_diff = _compose_first_text_diff(current_text, provided_text, text_blocks)
            if content_diff is not None:
                return {
                    **profile_meta,
                    "ok": False,
                    "mode": mode,
                    "fidelity": "failed",
                    "firstDiff": content_diff,
                    "verifiedBlocks": len(blocks),
                    "cellCount": cell_count,
                    "generatedBlocks": generated,
                    "message": "layout-only 内容保真校验未通过：blocks 文本与当前图文正文不一致（疑似改了字），已拒绝写入。",
                }

        if params.get("dryRun") is True:
            return {
                **profile_meta,
                "ok": True,
                "mode": mode,
                "dryRun": True,
                "fidelity": fidelity["fidelity"],
                "firstDiff": fidelity.get("firstDiff"),
                "verifiedBlocks": len(blocks),
                "cellCount": cell_count,
                "generatedBlocks": generated,
                "htmlSummary": "\n".join(segments),
                "images": [dict(spec) for spec in image_specs],
                "saved": False,
                "message": "预检完成：未写入正文、未上传图片、未保存。",
            }

        if params.get("confirm") is not True:
            raise ValueError("compose 会写入当前图文，需要 confirm=true 确认。")

        page = self._require_page()
        if mode == "layout-only":
            opened = self._opened_article
            if not opened:
                raise RuntimeError("layout-only 必须先 article_open 打开要排版的图文。")
            current_id = _article_id_from_url(page.url) or ""
            if current_id != str(opened.get("articleId") or ""):
                raise RuntimeError(
                    "当前编辑器已不是 article_open 打开的图文，已拒绝 layout-only 写入。"
                )
        else:
            if not (self._new_article and self._editor_present(page)):
                self.article_new()
            page = self._require_page()
            title = str(params.get("title") or "")
            if title.strip():
                loc = page.locator(TITLE_SELECTOR)
                if loc.count() == 0:
                    raise RuntimeError("未找到标题输入框。")
                loc.first.click()
                self._check_cancelled()
                loc.first.fill(title)

        album = str(params.get("album") or "")
        unique_paths: list[Path] = []
        for spec in image_specs:
            if not spec["file"]:
                continue
            path = Path(spec["file"])
            if path not in unique_paths:
                unique_paths.append(path)
        uploaded_by_file: dict[str, dict[str, Any]] = {}
        if unique_paths:
            upload_items, _state = self._upload_paths(page, unique_paths, album)
            failed = [item for item in upload_items if not item.get("ok")]
            if failed:
                detail = "；".join(
                    f"{item.get('name') or item.get('path')}: {item.get('error')}"
                    for item in failed[:3]
                )
                raise RuntimeError("部分图片上传失败，未写入正文：" + detail)
            for item in upload_items:
                uploaded_by_file[str(item.get("path") or "")] = item

        # create 走秀米原生插入流程：逐格点击正文下方空白区，键盘输入生成原生
        # 文本 cell（已验证保存可持久化），最后再把每格内容替换成主题 HTML。
        # 图片仍用 t10 已验证的“选中文本 cell 后单击图库图片”方式追加。
        image_ledger: list[dict[str, Any]] = []
        cell_count_actual = len(text_blocks)
        if mode == "create":
            cell_count_actual = self._compose_native_text_cells(
                page,
                segments,
                [block["text"] for block in text_blocks],
                [_compose_block_style(block, theme) for block in text_blocks],
            )
            if image_specs:
                for spec in image_specs:
                    self._check_cancelled()
                    uploaded = uploaded_by_file.get(str(spec.get("file") or ""), {})
                    anchor_text = ""
                    for block in blocks[: int(spec["blockIndex"])]:
                        if block["type"] in ("paragraph", "heading", "quote"):
                            anchor_text = block["text"]
                    result = self.image_insert(
                        {
                            "imageId": str(uploaded.get("id") or ""),
                            "afterText": anchor_text,
                            "confirm": True,
                        }
                    )
                    if result.get("inserted"):
                        image_ledger.append(
                            {
                                **spec,
                                "imageId": str(uploaded.get("id") or ""),
                                "url": uploaded.get("url") or "",
                                "inserted": True,
                                "error": "",
                            }
                        )
                    else:
                        image_ledger.append(
                            {
                                **spec,
                                "imageId": str(uploaded.get("id") or ""),
                                "url": uploaded.get("url") or "",
                                "inserted": False,
                                "error": result.get("message") or result.get("reason") or "图片未插入",
                            }
                        )
        else:
            # layout-only 只给现有文本 cell 换样式，不重建正文、不触碰图片
            # cell；这样原图与图片位置保持不变。
            # 只写样式、不替换 text，避免 layout-only 破坏原文中的行内格式。
            layout_items = [
                {"text": None, "expectedText": block["text"], "style": _compose_block_style(block, theme)}
                for block in text_blocks
            ]
            self._check_cancelled()
            applied = page.evaluate(COMPOSE_SET_CELLS_JS, {"items": layout_items, "requireExactCount": True})
            if not isinstance(applied, dict) or not applied.get("ok"):
                detail = applied.get("error") if isinstance(applied, dict) else "未知错误"
                raise RuntimeError(f"layout-only 写入文本 cell 失败：{detail}")
            if len(applied.get("applied") or []) < len(segments):
                raise RuntimeError(
                    f"layout-only 文本 cell 数不足：期望 {len(segments)}，"
                    f"实际应用 {len(applied.get('applied') or [])}。"
                )
        time.sleep(1.2)
        self._scroll_editor_to_top(page)

        after = self._editor_snapshot(page, paragraph_limit=20)
        failed_images = [item for item in image_ledger if not item.get("inserted")]
        image_count = int(after.get("imageCount") or 0)
        if image_specs and image_count < len(image_specs):
            failed_images = list(failed_images) + [
                {
                    "blockIndex": spec["blockIndex"],
                    "name": spec.get("name") or "",
                    "inserted": False,
                    "error": f"重建正文后图片数 {image_count} 少于期望 {len(image_specs)}",
                }
                for spec in image_specs
            ]
        result: dict[str, Any] = {
            **profile_meta,
            "ok": not failed_images,
            "mode": mode,
            "fidelity": fidelity["fidelity"],
            "firstDiff": fidelity.get("firstDiff"),
            "verifiedBlocks": len(blocks),
            "cellCount": cell_count_actual,
            "layoutLimit": "cell-level" if image_specs else "",
            "generatedBlocks": generated,
            "htmlSummary": "\n".join(segments),
            "images": image_ledger,
            "imageCount": image_count,
            "articleId": after.get("articleId") or "",
            "url": after.get("url") or "",
            "title": after.get("title") or "",
            "textPreview": after.get("textPreview") or "",
            "charCount": after.get("charCount") or 0,
            "saved": False,
        }
        # t33：create 默认保存；layout-only 仍需显式 save=true，避免自动保存别人的稿件。
        want_save = params.get("save") is True or (
            mode == "create" and params.get("save") is not False
        )
        if not want_save:
            result["message"] = (
                "正文已按块写入，尚未保存；请让用户预览并确认后再保存。"
                + (" 图片未能全部插入，请查看 images 对账。" if failed_images else "")
            )
            return result

        save_result = self.article_save(
            {
                "confirm": True,
                "verify": True,
                "expectedTitle": str(after.get("title") or ""),
                "expectedCharCount": int(after.get("charCount") or 0),
            }
        )
        saved = bool(save_result.get("saved"))
        verified = bool(save_result.get("saveVerified"))
        result.update(
            {
                "saved": saved,
                "saveVerified": verified,
                "saveEvidence": save_result.get("saveEvidence")
                or {"message": save_result.get("message") or "", "matched": verified},
                "articleId": str(save_result.get("articleId") or result["articleId"] or ""),
                "url": str(after.get("url") or ""),
            }
        )
        if not saved or not verified:
            try:
                shot = self.screenshot({"name": "compose-save-failed"})
                result["screenshotPath"] = str(shot.get("path") or "")
            except Exception as exc:
                _log(f"compose save failure screenshot failed: {exc}")
            result["ok"] = False
            failure_reasons = list(save_result.get("saveFailureReasons") or [])
            result["saveFailureReasons"] = failure_reasons
            result["reason"] = (
                failure_reasons[0]
                if failure_reasons
                else ("saveFailed" if not saved else "saveVerifiedFailed")
            )
            result["message"] = (
                f"正文已写入，但保存未通过验证：{save_result.get('message') or '未知原因'}。"
                " 已保留当前编辑器内容；请在可见窗口检查，不要假定已保存。"
            )
            return result
        result["ok"] = not failed_images
        result["message"] = (
            "正文已保存并通过校验：标题与字数与保存前一致。"
            + (" 图片未能全部插入，请查看 images 对账。" if failed_images else "")
        )
        return result

    def _lookup_article_id_by_title(self, title: str) -> str:
        """保存后若当前 URL 仍是 /new，用临时标签页从列表解析新文章 id。"""
        title = str(title or "").strip()
        if not title or self._context is None:
            return ""
        temp = None
        try:
            temp = self._context.new_page()
            temp.goto(LIST_URL, wait_until="domcontentloaded", timeout=30000)
            time.sleep(2.0)
            items = temp.evaluate(
                """
                () => {
                  const seen = new Set();
                  const result = [];
                  for (const card of document.querySelectorAll('.show-item')) {
                    const link = card.querySelector("a[href*='/paper/for/']");
                    if (!link) continue;
                    const url = link.href;
                    if (!url || seen.has(url)) continue;
                    const titleEl = card.querySelector('.title');
                    let title = titleEl ? (titleEl.innerText || '').trim() : '';
                    if (!title) title = (link.getAttribute('title') || link.innerText || '').trim();
                    if (!title) continue;
                    seen.add(url);
                    result.push({title, url});
                  }
                  return result;
                }
                """
            )
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    if str(item.get("title") or "").strip() != title:
                        continue
                    article_id = _article_id_from_url(str(item.get("url") or ""))
                    if article_id:
                        return article_id
        except Exception as exc:
            _log(f"lookup article id failed: {exc}")
        finally:
            if temp is not None:
                self._safe_close(temp)
        return ""

    def _save_verification(
        self, page, before: dict[str, Any], expected_title: str,
        expected_chars: int | None, saved_message: str, save_hint_seen: bool = True,
        article_id: str = "", before_guard: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Verify the saved article with the three independent save signals.

        Acceptance is strict: the fresh save acknowledgement, the exact server
        article identity and the normalized full editor model must all agree.
        Title and charCount stay supporting evidence only, because a same-length
        edit, a colour change, a missing image or a reordered image can keep both
        unchanged. When any part cannot be verified the result is
        saved=true / saveVerified=false with the concrete reasons kept.
        """
        article_id = article_id or _article_id_from_url(str(page.url or ""))
        evidence: dict[str, Any] = {
            "checkedAt": _profile_now_iso(), "message": saved_message,
            "saveHintSeen": bool(save_hint_seen), "articleId": article_id,
            "independentReadback": False, "rereadMatched": False, "matched": False,
            "expectedModelHash": before.get("modelHash") or "",
            "expectedTitle": expected_title,
            "expectedCharCount": expected_chars,
        }
        reasons: list[str] = []
        if not save_hint_seen:
            reasons.append("saveAcknowledgementMissing")
        if not article_id:
            reasons.append("articleIdUnavailable")
        if not before.get("modelHash"):
            reasons.append("expectedModelUnavailable")
        tab = None
        reread_matched = False
        actual_guard = None
        try:
            self._check_cancelled()
            tab = page.context.new_page()
            url = f"https://xiumi.us/studio/v5#/paper/for/{article_id}"
            tab.goto(url, wait_until="domcontentloaded", timeout=45000)
            if not self._wait_editor(tab, timeout_ms=20000, dismiss_restore=False):
                raise RuntimeError("独立页面未能加载编辑器。")
            if self._restore_dialog_info(tab):
                raise RuntimeError("独立页面出现本地草稿恢复提示，不能当作服务器读回证据。")
            actual = self._model_snapshot(tab)
            # The independent page carries the server-side identity; without it
            # verify_readback would always report articleIdentityMismatch.
            actual["articleId"] = _article_id_from_url(str(tab.url or ""))
            try:
                actual_guard = self._guard_snapshot(tab)
            except Exception:
                actual_guard = None
            read = self._editor_snapshot(tab, paragraph_limit=1)
            outcome = verify_readback(before, actual, article_id)
            title_ok = (
                not expected_title
                or str(actual.get("title") or "").strip() == expected_title.strip()
            )
            chars_ok = (
                expected_chars is None
                or int(read.get("charCount") or 0) == int(expected_chars)
            )
            reread_matched = bool(title_ok and chars_ok)
            if not reread_matched:
                reasons.extend(outcome["saveFailureReasons"])
                if not title_ok:
                    reasons.append("expectedTitleMismatch")
                if not chars_ok:
                    reasons.append("expectedCharCountMismatch")
            evidence.update({
                "independentReadback": True, "url": url,
                "expectedModelHash": str(before.get("modelHash") or ""),
                "actualModelHash": actual.get("modelHash") or "",
                "actualArticleId": actual.get("articleId") or "",
                "expectedTitle": expected_title,
                "actualTitle": actual.get("title") or "",
                "expectedCharCount": expected_chars,
                "actualCharCount": int(read.get("charCount") or 0),
                "rereadMatched": reread_matched,
            })
            self._check_cancelled()
        except Exception as exc:
            reasons.append("independentReadbackUnavailable")
            evidence["error"] = str(exc)
        finally:
            self._safe_close(tab)
        identity_ok = bool(
            article_id
            and str(evidence.get("actualArticleId") or "") == str(article_id)
        )
        model_ok = False
        if isinstance(before_guard, dict) and isinstance(actual_guard, dict):
            model_ok = content_equivalent(before_guard, actual_guard)
        else:
            model_ok = bool(
                evidence.get("expectedModelHash")
                and evidence.get("actualModelHash")
                and evidence["expectedModelHash"] == evidence["actualModelHash"]
            )
        verified = bool(save_hint_seen and identity_ok and model_ok)
        if not identity_ok:
            reasons.append("articleIdentityUnconfirmed")
        if not model_ok:
            reasons.append("modelReadbackUnconfirmed")
        evidence["identityOk"] = identity_ok
        evidence["modelOk"] = model_ok
        evidence["matched"] = verified
        return {"saveVerified": verified, "saveFailureReasons": list(dict.fromkeys(reasons)),
                "articleId": article_id, "saveEvidence": evidence}

    def article_save(self, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Acknowledge a fresh save, then compare the full independently read model."""
        params = params or {}
        if params.get("confirm") is not True:
            return {"saved": False, "saveVerified": False, "reason": "confirmationRequired",
                    "message": "保存需要 confirm=true；未执行保存。"}
        page = self._current_page()
        if page is None:
            return {"saved": False, "saveVerified": False, "message": "浏览器未启动，没有可保存的图文。"}
        self._assert_mutation_target(page, params)
        # Losing focus commits contenteditable text into Angular's save model.
        # Establish evidence after that commit, rather than hashing an old cell.
        self._check_cancelled()
        page.locator(TITLE_SELECTOR).first.click()
        page.wait_for_timeout(350)
        before_model = self._model_snapshot(page)
        for _ in range(4):
            self._check_cancelled()
            page.wait_for_timeout(150)
            stable = self._model_snapshot(page)
            if stable["modelHash"] == before_model["modelHash"]:
                break
            before_model = stable
        else:
            raise RuntimeError("正文保存模型仍在变化，未点击保存；请先 inspect 对账。")
        # Baseline for the "unsaved changes" guard must describe the server-side
        # version we are about to write, not whatever the working page shows
        # after the save returns: edits typed while the independent readback runs
        # would otherwise be folded into the baseline and silently accepted.
        try:
            before_guard = self._guard_snapshot(page)
        except Exception:
            before_guard = None
        before_read = self._editor_snapshot(page, paragraph_limit=1)
        before_dom_hash = hashlib.sha256(self._read_editor(page)[1].encode("utf-8")).hexdigest()
        response_evidence: list[dict[str, Any]] = []

        def capture(response):
            try:
                parsed = urlparse(response.url)
                if parsed.hostname != "xiumi.us" and not (parsed.hostname or "").endswith(".xiumi.us"):
                    return
                if response.request.method not in ("POST", "PUT", "PATCH"):
                    return
                if not re.search(r"/papers?(?:/|$)", parsed.path):
                    return
                if not 200 <= response.status < 300:
                    return
                data = response.json()
                if not isinstance(data, dict) or data.get("success") is False or data.get("error"):
                    return
                for item in (data, data.get("data"), data.get("paper"), data.get("result")):
                    if not isinstance(item, dict):
                        continue
                    value = str(item.get("paperId") or item.get("paper_id") or item.get("id") or "")
                    if re.fullmatch(r"\d{1,20}", value):
                        response_evidence.append({"articleId": value, "status": response.status})
                        break
            except Exception:
                return

        page.on("response", capture)
        acknowledged = False
        message = ""
        failure = ""
        try:
            page.evaluate(SAVE_WATCH_START_JS)
            self._check_cancelled()
            if not self._click_save(page):
                return {"saved": False, "saveVerified": False, "reason": "saveButtonMissing",
                        "saveFailureReasons": ["saveButtonMissing"], "message": "未找到保存按钮，未执行保存。"}
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline:
                self._check_cancelled()
                feedback = page.evaluate(SAVE_WATCH_READ_JS) or {}
                if feedback.get("failure"):
                    failure = str(feedback["failure"])
                    break
                if feedback.get("success"):
                    acknowledged = True
                    message = str(feedback["success"])
                    break
                if response_evidence:
                    acknowledged = True
                    message = "保存接口返回了图文编号。"
                    break
                page.wait_for_timeout(300)
        finally:
            page.remove_listener("response", capture)
            try:
                page.evaluate(SAVE_WATCH_STOP_JS)
            except Exception:
                pass
        if not acknowledged:
            return {"saved": False, "saveVerified": False,
                    "reason": "saveRejected" if failure else "saveAcknowledgementMissing",
                    "saveFailureReasons": ["saveRejected" if failure else "saveAcknowledgementMissing"],
                    "message": f"保存未确认：{failure or '未观察到本次保存的新反馈，结果未知，请先对账，勿重复提交。'}"}
        article_id = _article_id_from_url(str(page.url or ""))
        if not article_id and response_evidence:
            article_id = response_evidence[-1]["articleId"]
        verification = self._save_verification(
            page, before_model, str(params.get("expectedTitle") or before_read.get("title") or ""),
            int(params.get("expectedCharCount", before_read.get("charCount") or 0)),
            message, save_hint_seen=True, article_id=article_id,
            before_guard=before_guard,
        )
        result = {"saved": True, **verification}
        if not verification["saveVerified"]:
            result["reason"] = (verification.get("saveFailureReasons") or ["verificationFailed"])[0]
            result["message"] = "收到保存反馈，但独立读回验收未通过；保留当前稿，不把保存提示当作验收成功。"
            result["screenshotPath"] = self._safe_screenshot_path("save-verification-failed")
            return result
        self._new_article = False
        # The verified server version is the pre-save snapshot. Xiumi may still
        # normalize the draft after saving (append an empty cell / rewrite a
        # component signature); _content_equivalent tolerates exactly that and
        # nothing else, so a user's own edits are never absorbed into the baseline.
        self._saved_baseline = before_guard or {
            **before_model, **before_read,
            "modelHash": before_model["modelHash"], "domTextHash": before_dom_hash,
        }
        self._opened_article = {**before_read, "articleId": article_id, "revision": before_model["modelHash"]}
        self._edit_inspection = None
        # A new draft can keep /new in its URL after the first server save.
        # Bind the visible editor to the verified server id only if it is unchanged.
        if not _article_id_from_url(str(page.url or "")):
            try:
                if self._model_snapshot(page)["modelHash"] == before_model["modelHash"]:
                    page.goto(f"https://xiumi.us/studio/v5#/paper/for/{article_id}", wait_until="domcontentloaded", timeout=30000)
                    self._wait_editor(page, timeout_ms=15000, dismiss_restore=False)
            except Exception:
                result["reopenRequired"] = True
        save_evidence = verification.get("saveEvidence") or {}
        full_model_match = bool(verification.get("saveVerified"))
        result["message"] = (
            "已保存，并在独立页面核对文章身份、完整正文模型、样式、图片及顺序一致。"
            if full_model_match
            else "已保存，但独立读回未确认完整模型一致；已保留失败原因，请先 inspect 对账。"
        )
        return result

    # ------------------------------------------------------------ 风格学习
    @staticmethod
    def _looks_like_template_page(raw: dict[str, Any]) -> bool:
        url = str(raw.get("url") or "").lower()
        title = str(raw.get("docTitle") or "")
        return bool(raw.get("isEditor")) or "template" in url or "模板" in title

    @staticmethod
    def _template_snapshot(page) -> dict[str, Any]:
        try:
            value = page.evaluate(
                """
                () => {
                  const all = Array.from(document.querySelectorAll(
                    '.tn-cell-image img, .tn-tpl-library-box img'
                  ));
                  const imgs = all.length ? all : Array.from(document.querySelectorAll('img'));
                  const signature = imgs.slice(0, 12)
                    .map(img => img.currentSrc || img.src || '')
                    .join('|');
                  return {count: imgs.length, signature};
                }
                """
            )
            if isinstance(value, dict):
                return {
                    "count": int(value.get("count") or 0),
                    "signature": str(value.get("signature") or ""),
                }
        except Exception:
            pass
        return {"count": 0, "signature": ""}

    def _refresh_template_library(self, page) -> list[dict[str, Any]]:
        """只在模板库/编辑器侧栏点「换一换」类按钮，1-3 次，绝不保存。"""
        stats: list[dict[str, Any]] = []
        for _attempt in range(3):
            element, label = self._find_visible_text(
                page, ["换一换", "换一批", "刷新模板"], exact=False
            )
            if element is None:
                if not stats:
                    stats.append(
                        {
                            "action": None,
                            "before": 0,
                            "after": 0,
                            "delta": 0,
                            "message": "未找到可见的「换一换/换一批」按钮，未刷新模板。",
                        }
                    )
                break
            before = self._template_snapshot(page)
            try:
                element.click()
            except Exception as exc:
                stats.append(
                    {
                        "action": label,
                        "before": before["count"],
                        "after": before["count"],
                        "delta": 0,
                        "changed": False,
                        "message": f"点击「{label}」失败：{type(exc).__name__}",
                    }
                )
                break
            time.sleep(2.5)
            after = self._template_snapshot(page)
            changed = bool(after["signature"] and after["signature"] != before["signature"])
            stats.append(
                {
                    "action": label,
                    "before": before["count"],
                    "after": after["count"],
                    "delta": after["count"] - before["count"],
                    "changed": changed,
                    "message": (
                        f"已点击「{label}」：模板图 {before['count']} → {after['count']}"
                        f"（{'内容已变化' if changed else '内容签名未变化'}）。"
                    ),
                }
            )
            if not changed and after["count"] <= before["count"]:
                break
        return stats

    def _style_body(self, raw: dict[str, Any]) -> dict[str, Any]:
        cells = [
            cell for cell in (raw.get("cells") or [])
            if str(cell.get("text") or "").strip() and float(cell.get("fontSize") or 0) > 0
        ]
        body_cells = [
            cell for cell in cells
            if len(str(cell.get("text") or "")) >= 10 and float(cell.get("w") or 0) >= 150
        ] or cells
        paragraphs = [
            item for item in (raw.get("paragraphs") or [])
            if str(item.get("text") or "").strip()
        ]
        if not body_cells and not paragraphs:
            return {
                "fontSize": 0, "lineHeight": 0, "lineHeightRatio": 0,
                "paragraphSpacing": 0, "blockGap": 0, "color": "",
                "sample": "", "blocks": 0,
            }
        sample_items = body_cells or paragraphs
        sample = str(sample_items[0].get("text") or "")[:120]
        if body_cells:
            weights = [max(len(str(cell.get("text") or "")), 1) for cell in body_cells]
            font_size = _weighted_median(
                [float(cell.get("fontSize") or 0) for cell in body_cells], weights
            )
            line_heights = [
                (float(cell.get("lineHeight") or 0), weight)
                for cell, weight in zip(body_cells, weights)
                if float(cell.get("lineHeight") or 0) > 0
            ]
            line_height = (
                _weighted_median([v for v, _w in line_heights], [w for _v, w in line_heights])
                if line_heights else 0.0
            )
            color_counter = Counter(
                _norm_color(cell.get("color")) for cell in body_cells if _norm_color(cell.get("color"))
            )
            body_color = color_counter.most_common(1)[0][0] if color_counter else ""
            sorted_cells = sorted(
                body_cells, key=lambda cell: (float(cell.get("y") or 0), float(cell.get("x") or 0))
            )
            gaps = []
            for previous, current in zip(sorted_cells, sorted_cells[1:]):
                if abs(float(previous.get("x") or 0) - float(current.get("x") or 0)) > 40:
                    continue
                gap = float(current.get("y") or 0) - (
                    float(previous.get("y") or 0) + float(previous.get("h") or 0)
                )
                if 0 <= gap <= 96:
                    gaps.append(gap)
            block_gap = median(gaps) if gaps else 0.0
        else:
            weights = [max(len(str(item.get("text") or "")), 1) for item in paragraphs]
            font_size = _weighted_median(
                [float(item.get("fontSize") or 0) for item in paragraphs], weights
            )
            line_heights = [
                (float(item.get("lineHeight") or 0), weight)
                for item, weight in zip(paragraphs, weights)
                if float(item.get("lineHeight") or 0) > 0
            ]
            line_height = (
                _weighted_median([v for v, _w in line_heights], [w for _v, w in line_heights])
                if line_heights else 0.0
            )
            color_counter = Counter(
                _norm_color(item.get("color")) for item in paragraphs if _norm_color(item.get("color"))
            )
            body_color = color_counter.most_common(1)[0][0] if color_counter else ""
            block_gap = 0.0
        margins = [
            float(item.get("marginTop") or 0) + float(item.get("marginBottom") or 0)
            for item in paragraphs
            if float(item.get("marginTop") or 0) + float(item.get("marginBottom") or 0) > 0
        ]
        paragraph_spacing = median(margins) if margins else 0.0
        return {
            "fontSize": round(font_size, 1),
            "lineHeight": round(line_height, 1),
            "lineHeightRatio": round(line_height / font_size, 2) if font_size else 0,
            "paragraphSpacing": round(paragraph_spacing, 1),
            "blockGap": round(block_gap, 1),
            "color": body_color,
            "sample": sample,
            "blocks": len(body_cells or paragraphs),
        }

    def _style_title(self, raw: dict[str, Any], body_size: float) -> dict[str, Any]:
        text = str(raw.get("editorTitle") or raw.get("docTitle") or "").strip()
        headings = [
            item for item in (raw.get("headings") or [])
            if str(item.get("text") or "").strip()
        ]
        levels: list[dict[str, Any]] = []
        if headings:
            for item in headings[:8]:
                size = float(item.get("fontSize") or 0)
                levels.append(
                    {
                        "tag": item.get("tag") or "",
                        "text": str(item.get("text") or "")[:80],
                        "fontSize": round(size, 1),
                        "fontWeight": str(item.get("fontWeight") or ""),
                        "lineHeight": round(float(item.get("lineHeight") or 0), 1),
                        "color": _norm_color(item.get("color")),
                        "level": (
                            "主标题" if body_size and size >= body_size + 6
                            else "小标题" if body_size and size >= body_size + 2
                            else "正文强调"
                        ),
                    }
                )
        else:
            first_by_size: dict[float, dict[str, Any]] = {}
            for cell in raw.get("cells") or []:
                content = str(cell.get("text") or "").strip()
                size = float(cell.get("fontSize") or 0)
                if not content or size <= 0:
                    continue
                first_by_size.setdefault(round(size, 1), cell)
            for size in sorted(first_by_size, reverse=True)[:5]:
                cell = first_by_size[size]
                levels.append(
                    {
                        "tag": "div",
                        "text": str(cell.get("text") or "")[:80],
                        "fontSize": size,
                        "fontWeight": str(cell.get("fontWeight") or ""),
                        "lineHeight": round(float(cell.get("lineHeight") or 0), 1),
                        "color": _norm_color(cell.get("color")),
                        "level": (
                            "主标题" if body_size and size >= body_size + 6
                            else "小标题" if body_size and size >= body_size + 2
                            else "正文强调"
                        ),
                    }
                )
        return {
            "text": text,
            "levels": levels,
            "sizes": sorted({level["fontSize"] for level in levels}, reverse=True),
        }

    def _style_palette(self, raw: dict[str, Any]) -> dict[str, Any]:
        text_counter: Counter = Counter()
        for cell in raw.get("cells") or []:
            color = _norm_color(cell.get("color"))
            if color and str(cell.get("text") or "").strip():
                text_counter[color] += max(len(str(cell.get("text") or "")), 1)
        for item in (raw.get("headings") or []) + (raw.get("paragraphs") or []):
            color = _norm_color(item.get("color"))
            if color and str(item.get("text") or "").strip():
                text_counter[color] += 1
        background_counter: Counter = Counter()
        border_counter: Counter = Counter()
        all_counter: Counter = Counter()
        for item in raw.get("colorItems") or []:
            color = _norm_color(item.get("color"))
            if not color:
                continue
            kind = item.get("kind")
            if kind == "bg":
                background_counter[color] += 1
            elif kind == "border":
                border_counter[color] += 1
            all_counter[color] += 1
        for color, weight in text_counter.items():
            all_counter[color] += weight

        def first_saturated(exclude: set[str]) -> str:
            for color, _weight in all_counter.most_common(60):
                if color in exclude or _is_light_color(color):
                    continue
                if _color_luminance(color) < 70:
                    continue
                if _color_saturation(color) >= 0.18:
                    return color
            return ""

        primary = first_saturated(set())
        secondary = first_saturated({primary} if primary else set())
        background = ""
        for color, _weight in background_counter.most_common(20):
            if _is_light_color(color):
                background = color
                break
        if not background and background_counter:
            background = background_counter.most_common(1)[0][0]
        text_color = text_counter.most_common(1)[0][0] if text_counter else ""
        accents = []
        for color, weight in all_counter.most_common(40):
            if color in {primary, secondary} or _is_light_color(color):
                continue
            if _color_luminance(color) < 70:
                continue
            if _color_saturation(color) >= 0.18:
                accents.append({"color": color, "weight": weight})
            if len(accents) >= 5:
                break
        return {
            "primary": primary,
            "secondary": secondary,
            "background": background,
            "text": text_color,
            "accents": accents,
            "topColors": _weighted_top(all_counter, 8),
        }

    def _style_decor(self, raw: dict[str, Any]) -> dict[str, Any]:
        decor = raw.get("decor") or {}
        border_counter: Counter = Counter()
        for item in raw.get("colorItems") or []:
            if item.get("kind") == "border":
                color = _norm_color(item.get("color"))
                if color:
                    border_counter[color] += 1
        images = int(decor.get("images") or 0)
        image_text_blocks = int(decor.get("imageTextBlocks") or 0)
        if image_text_blocks:
            relation = "图文同块"
        elif images:
            relation = "图文分块"
        else:
            relation = "无图片"
        return {
            "dividers": int(decor.get("dividers") or 0),
            "borderedElements": int(decor.get("bordered") or 0),
            "borderColors": _weighted_top(border_counter, 6),
            "borderRadii": list(decor.get("radii") or []),
            "images": images,
            "svgs": int(decor.get("svgs") or 0),
            "cards": int(decor.get("cards") or 0),
            "imageTextBlocks": image_text_blocks,
            "imageTextRelation": relation,
            "hasDivider": bool(int(decor.get("dividers") or 0) or int(decor.get("bordered") or 0)),
            "templateRefresh": [],
        }

    def _summarize_style(self, raw: dict[str, Any],
                         refresh_stats: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        body = self._style_body(raw)
        title = self._style_title(raw, float(body.get("fontSize") or 0))
        palette = self._style_palette(raw)
        decor = self._style_decor(raw)
        stats = refresh_stats or []
        decor["templateRefresh"] = stats
        notes = []
        if title.get("levels"):
            sizes = "、".join(f"{level['level']} {level['fontSize']}px" for level in title["levels"][:4])
            notes.append(f"标题层级：{sizes}。")
        notes.append(
            f"正文约 {body['fontSize']}px，行高 {body['lineHeight']}px"
            f"（{body['lineHeightRatio']} 倍），段距 {body['paragraphSpacing']}px，"
            f"正文块间距 {body['blockGap']}px。"
        )
        if palette.get("primary"):
            notes.append(
                f"主色 {palette['primary']}，辅助色 {palette.get('secondary') or '未识别'}，"
                f"背景 {palette.get('background') or '未识别'}。"
            )
        notes.append(
            f"装饰：分隔线/边框元素 {decor['dividers']}/{decor['borderedElements']}，"
            f"图片 {decor['images']} 张，SVG {decor['svgs']} 个，卡片 {decor['cards']} 个，"
            f"图文关系 {decor['imageTextRelation']}。"
        )
        for stat in stats:
            if stat.get("action"):
                notes.append(
                    f"模板刷新：{stat['action']} {stat['before']}→{stat['after']}"
                    f"（{stat['delta']:+d}，{'内容已变化' if stat.get('changed') else '内容签名未变化'}）。"
                )
            elif stat.get("message"):
                notes.append(str(stat["message"]))
        return {"title": title, "body": body, "palette": palette, "decor": decor, "notes": notes}

    def _merge_style_summaries(self, summaries: list[dict[str, Any]]) -> dict[str, Any]:
        first = summaries[0]
        if len(summaries) == 1:
            return {
                "title": first["title"],
                "body": first["body"],
                "palette": first["palette"],
                "decor": first["decor"],
                "notes": list(first["notes"]),
            }
        title = {
            "text": first["title"].get("text") or "",
            "levels": [],
            "sizes": sorted(
                {size for item in summaries for size in item["title"].get("sizes", [])},
                reverse=True,
            ),
        }
        seen_levels = set()
        for item in summaries:
            for level in item["title"].get("levels", []):
                key = (level.get("level"), level.get("fontSize"), level.get("text"))
                if key in seen_levels:
                    continue
                seen_levels.add(key)
                title["levels"].append(level)
        body_weights = [max(int(item["body"].get("blocks") or 0), 1) for item in summaries]
        body = {
            "fontSize": round(_weighted_median([float(item["body"].get("fontSize") or 0) for item in summaries], body_weights), 1),
            "lineHeight": round(_weighted_median([float(item["body"].get("lineHeight") or 0) for item in summaries], body_weights), 1),
            "lineHeightRatio": 0,
            "paragraphSpacing": round(_weighted_median([float(item["body"].get("paragraphSpacing") or 0) for item in summaries], body_weights), 1),
            "blockGap": round(_weighted_median([float(item["body"].get("blockGap") or 0) for item in summaries], body_weights), 1),
            "color": first["body"].get("color") or "",
            "sample": first["body"].get("sample") or "",
            "blocks": sum(int(item["body"].get("blocks") or 0) for item in summaries),
        }
        if body["fontSize"]:
            body["lineHeightRatio"] = round(body["lineHeight"] / body["fontSize"], 2)
        palette = {
            "primary": first["palette"].get("primary") or "",
            "secondary": first["palette"].get("secondary") or "",
            "background": first["palette"].get("background") or "",
            "text": first["palette"].get("text") or "",
            "accents": [],
            "topColors": [],
        }
        for key in ("primary", "secondary", "background", "text"):
            if not palette[key]:
                for item in summaries:
                    if item["palette"].get(key):
                        palette[key] = item["palette"][key]
                        break
        seen_accents = {palette["primary"], palette["secondary"]}
        for item in summaries:
            for accent in item["palette"].get("accents", []):
                if accent.get("color") in seen_accents:
                    continue
                seen_accents.add(accent.get("color"))
                palette["accents"].append(accent)
        palette["accents"] = palette["accents"][:8]
        accent_counter: Counter = Counter()
        for item in summaries:
            for entry in item["palette"].get("topColors", []):
                accent_counter[entry.get("color")] += entry.get("weight") or 0
        palette["topColors"] = _weighted_top(accent_counter, 8)
        decor = {"dividers": 0, "borderedElements": 0, "borderColors": [], "borderRadii": [],
                 "images": 0, "svgs": 0, "cards": 0, "imageTextBlocks": 0,
                 "imageTextRelation": first["decor"].get("imageTextRelation") or "",
                 "hasDivider": False, "templateRefresh": []}
        for key in ("dividers", "borderedElements", "images", "svgs", "cards", "imageTextBlocks"):
            decor[key] = sum(int(item["decor"].get(key) or 0) for item in summaries)
        radii = set()
        border_counter: Counter = Counter()
        for item in summaries:
            radii.update(item["decor"].get("borderRadii") or [])
            for entry in item["decor"].get("borderColors", []):
                border_counter[entry.get("color")] += entry.get("weight") or 0
            decor["templateRefresh"].extend(item["decor"].get("templateRefresh") or [])
        decor["borderRadii"] = sorted(radii)
        decor["borderColors"] = _weighted_top(border_counter, 6)
        decor["hasDivider"] = bool(decor["dividers"] or decor["borderedElements"])
        notes: list[str] = []
        for item in summaries:
            notes.extend(item["notes"])
        notes.append(f"已聚合 {len(summaries)} 个成功来源（标题/调色板取首个来源，数值取加权中位数）。")
        return {"title": title, "body": body, "palette": palette, "decor": decor, "notes": notes}

    def reference(self, params: dict[str, Any]) -> dict[str, Any]:
        action = str(params.get("action") or "list").strip().lower()
        if action == "list":
            return {"profiles": _profile_list()}
        if action == "show":
            profile_id = str(params.get("id") or "").strip()
            if not profile_id:
                raise ValueError("reference show 需要 id。")
            return {"profile": _profile_load(profile_id)}
        if action == "remove":
            if params.get("confirm") is not True:
                raise ValueError("删除参考排版档案需要 confirm=true。")
            profile_id = str(params.get("id") or "").strip()
            if not profile_id:
                raise ValueError("reference remove 需要 id。")
            return {"removed": _profile_delete(profile_id), "id": profile_id}
        if action != "learn":
            raise ValueError(f"不支持的 reference action：{action}")

        url = str(params.get("url") or "").strip()
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.scheme not in ("http", "https") or parsed.port not in (None, 80, 443) or not (
            host in ("xiumi.us", "xiumius.cn", "mp.weixin.qq.com") or
            host.endswith(".xiumi.us") or host.endswith(".xiumius.cn")
        ):
            raise ValueError("学习只支持秀米或微信的公开图文链接。")
        page = self._require_page().context.new_page()
        try:
            self._check_cancelled()
            response = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            if response is not None and int(response.status) >= 400:
                raise ValueError(f"参考链接返回 HTTP {response.status}，无法学习。")
            try:
                page.wait_for_load_state("load", timeout=12000)
            except Exception:
                pass
            self._check_cancelled()
            if self._is_auth_page(page):
                raise ValueError("参考链接跳到了登录页，未学习。")
            page.wait_for_timeout(2000)
            sample = observe_page(page)
            analysis = page.evaluate(REFERENCE_STYLE_JS) or {}
            if not isinstance(analysis, dict):
                raise ValueError("参考页面样式采样结果为空。")
            profile = _profile_from_analysis(analysis, sample["url"])
            if not profile.get("style", {}).get("titles") and not profile.get("style", {}).get("body"):
                raise ValueError("页面没有可识别的标题/正文样式，未生成参考档案。")
            self._check_cancelled()
            saved = _profile_save(profile)
            profile["storedAt"] = str(saved) if saved is not None else "host"
            notes = []
            missing = profile.get("missing") or []
            if missing:
                notes.append("采样不完整：" + "；".join(str(item) for item in missing))
            if sample["observation"].get("incomplete"):
                notes.append("结构采样不完整，需要查看截图补充判断。")
            profile["notes"] = notes
            return {"profile": profile, "observation": sample["observation"],
                    "fromCache": False, "source": "live"}
        except Exception as exc:
            raise ValueError(f"参考学习未完成：{type(exc).__name__}: {exc}") from exc
        finally:
            self._safe_close(page)

    def style_study(self, params: dict[str, Any]) -> dict[str, Any]:
        """只读学习来源页面的排版特征；可刷新模板库，但绝不保存或修改图文。"""
        raw_sources = params.get("sources")
        if isinstance(raw_sources, str):
            raw_sources = [raw_sources]
        if not isinstance(raw_sources, list) or not raw_sources:
            raise ValueError("style_study 需要 params.sources（URL 字符串数组）")
        urls: list[str] = []
        for item in raw_sources:
            url = str(item or "").strip()
            if not url:
                continue
            if not re.match(r"^https?://", url):
                raise ValueError(f"来源不是 http(s) 链接：{url[:120]}")
            if url not in urls:
                urls.append(url)
        urls = urls[:5]
        if not urls:
            raise ValueError("style_study 的 sources 中没有有效 URL")
        refresh = bool(params.get("refreshTemplates"))
        base = self._require_page()
        context = base.context
        sources: list[dict[str, Any]] = []
        summaries: list[dict[str, Any]] = []
        errors: list[str] = []
        refresh_all: list[dict[str, Any]] = []
        observations: list[dict[str, Any]] = []
        for url in urls:
            tab = None
            try:
                self._check_cancelled()
                tab = context.new_page()
                tab.set_default_timeout(30000)
                tab.goto(url, wait_until="domcontentloaded", timeout=60000)
                try:
                    tab.wait_for_load_state("load", timeout=10000)
                except Exception:
                    pass
                time.sleep(5.0)
                if self._is_auth_page(tab):
                    raise RuntimeError("来源被重定向到登录页，需要先在可见窗口登录后再学习")
                raw = tab.evaluate(STYLE_EXTRACT_JS)
                if not isinstance(raw, dict):
                    raise RuntimeError("来源页面没有返回可分析的结构")
                refresh_stats: list[dict[str, Any]] = []
                if refresh and self._looks_like_template_page(raw):
                    refresh_stats = self._refresh_template_library(tab)
                    if any(stat.get("delta") for stat in refresh_stats):
                        time.sleep(1.5)
                        raw_after = tab.evaluate(STYLE_EXTRACT_JS)
                        if isinstance(raw_after, dict):
                            raw = raw_after
                summary = self._summarize_style(raw, refresh_stats)
                page_title = str(raw.get("editorTitle") or raw.get("docTitle") or "").strip()
                sources.append({"url": str(tab.url), "title": page_title, "ok": True})
                summaries.append(summary)
                try:
                    observations.append(observe_page(tab))
                except Exception as exc:
                    errors.append(f"结构未学习：{type(exc).__name__}: {exc}")
                for stat in refresh_stats:
                    entry = dict(stat)
                    entry["url"] = str(tab.url)
                    refresh_all.append(entry)
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                sources.append({"url": url, "title": "", "ok": False, "error": message})
                errors.append(f"{url}（{message}）")
            finally:
                if tab is not None:
                    self._safe_close(tab)
        if not summaries:
            raise RuntimeError("所有来源都读取失败：" + "；".join(errors[:3]))
        merged = self._merge_style_summaries(summaries)
        merged["decor"]["templateRefresh"] = refresh_all
        notes = list(merged.get("notes") or [])
        if errors:
            notes.append("部分来源失败：" + "；".join(errors[:3]))
        notes.append("只读采样：本次未点击保存，未修改任何图文。")
        merged["notes"] = notes
        result: dict[str, Any] = {"sources": sources, "learningObservations": observations}
        result.update(merged)
        return result

    def shutdown(self) -> dict[str, Any]:
        self._teardown_browser()
        playwright, self._playwright = self._playwright, None
        if playwright is not None:
            try:
                playwright.stop()
            except Exception as exc:
                _log(f"停止 playwright 失败: {exc}")
        _log("shutdown complete")
        return {"running": False}

    def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        self._check_cancelled()
        target_mutations = {"article_write", "article_update", "article_save", "template_apply",
                            "component_insert", "asset_insert", "image_insert", "image_manifest"}
        if method in target_mutations:
            self._assert_mutation_target(self._require_page(), params)
        handlers = {
            "ping": lambda _p: {"pong": True},
            "open": lambda _p: self.open(),
            "status": lambda _p: self.status(),
            "eval_js": lambda p: self.eval_js(p),
            "press_key": lambda p: self.press_key(p),
            "delete_image_cells": lambda p: self.delete_image_cells(p),
            "screenshot": lambda p: self.screenshot(p),
            "prune_screens": lambda p: self.prune_screens(p),
            "close": lambda p: self.close(p),
            "nav": lambda p: self.nav(p),
            "login_check": lambda _p: self.status(),
            "login_wait": lambda p: self.login_wait(p),
            "logout": lambda p: self.logout(p),
            "login_fill": lambda p: self.login_fill(p),
            "article_list": lambda _p: self.article_list(),
            "article_open": lambda p: self.article_open(p),
            "article_inspect": lambda p: self.article_inspect(p),
            "review_metrics": lambda p: self.review_metrics(p),
            "article_edit": lambda p: self.article_edit(p),
            "set_cell_styles": lambda p: self.set_cell_styles(p),
            "article_update": lambda p: self.article_update(p),
            "article_new": lambda p: self.article_new(p),
            "article_write": lambda p: self.article_write(p),
            "article_save": lambda p: self.article_save(p),
            "style_study": lambda p: self.style_study(p),
            "template_catalog": lambda p: self.template_catalog(p),
            "template_list": lambda p: self.template_list(p),
            "template_apply": lambda p: self.template_apply(p),
            "component_insert": lambda p: self.component_insert(p),
            "asset_categories": lambda p: self.asset_categories(),
            "asset_list": lambda p: self.asset_list(p),
            "asset_insert": lambda p: self.asset_insert(p),
            "upload_images": lambda p: self.upload_images(p),
            "image_insert": lambda p: self.image_insert(p),
            "image_manifest": lambda p: self.image_manifest(p),
            "compose": lambda p: self.compose(p),
            "reference": lambda p: self.reference(p),
            "quota": lambda p: self.quota(p),
            "quota_cleanup": lambda p: self.quota_cleanup(p),
            "shutdown": lambda _p: self.shutdown(),
        }
        handler = handlers.get(method)
        if handler is None:
            raise ValueError(f"未知方法：{method}")
        return handler(params)


def main(argv: list[str]) -> int:
    _reconfigure_stdio()
    config = _load_config(argv)
    bridge = Bridge(config)
    requests = queue.Queue()
    cancel_events: dict[Any, threading.Event] = {}

    def read_requests():
        try:
            for raw in sys.stdin:
                try:
                    incoming = json.loads(raw)
                except Exception:
                    incoming = None
                if isinstance(incoming, dict) and incoming.get("method") == "cancel":
                    event = cancel_events.get((incoming.get("params") or {}).get("requestId"))
                    if event is not None:
                        event.set()
                    continue
                if isinstance(incoming, dict):
                    cancel_events.setdefault(incoming.get("id"), threading.Event())
                requests.put(raw)
        finally:
            requests.put(None)

    threading.Thread(target=read_requests, daemon=True).start()
    _log(
        f"started pid={os.getpid()} channel={config['browserChannel']} "
        f"loginTimeoutMs={config['loginTimeoutMs']} tmp={TMP_DIR}"
    )

    try:
        while True:
            raw_line = requests.get()
            if raw_line is None:
                break
            line = raw_line.strip()
            if not line:
                continue
            try:
                request = json.loads(line)
            except Exception:
                _write_response(
                    {
                        "id": None,
                        "ok": False,
                        "error": "请求不是合法 JSON",
                        "trace": traceback.format_exc(),
                    }
                )
                continue
            if not isinstance(request, dict):
                _write_response(
                    {
                        "id": None,
                        "ok": False,
                        "error": "请求必须是 JSON 对象",
                        "trace": "",
                    }
                )
                continue

            request_id = request.get("id")
            bridge._cancel_event = cancel_events.get(request_id)
            bridge._expected_target = request.get("target") if isinstance(request.get("target"), dict) else None
            method = request.get("method")
            params = request.get("params")
            if not isinstance(params, dict):
                params = {}

            if not isinstance(method, str) or not method:
                _write_response(
                    {
                        "id": request_id,
                        "ok": False,
                        "error": "缺少 method 字段",
                        "trace": "",
                    }
                )
                continue

            bridge._restore_dialog_evidence = None
            passive = {"ping", "status", "login_check", "screenshot", "reference", "style_study", "article_inspect", "prune_screens"}
            if method not in passive and not (bridge._cancel_event and bridge._cancel_event.is_set()):
                bridge.focus_working_page()

            try:
                result = bridge.dispatch(method, params)
                bridge._check_cancelled()
            except Exception as exc:
                _log(f"method={method} failed: {type(exc).__name__}: {exc}")
                error_payload = {
                    "id": request_id,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {exc}",
                    "trace": traceback.format_exc(),
                }
                # 失败时尽力截一张图，便于直接看页面提示；截图失败不得覆盖原错误。
                try:
                    if bridge._browser_connected():
                        shot = bridge.screenshot(
                            {"name": f"error-{method}", "fullPage": False}
                        )
                        error_payload["screenshotPath"] = shot.get("path", "")
                except Exception as shot_exc:
                    _log(f"失败自动截图未成功: {type(shot_exc).__name__}: {shot_exc}")
                _write_response(error_payload)
                cancel_events.pop(request_id, None)
                if method == "shutdown":
                    break
                continue

            metadata_methods = {"article_open", "article_new", "article_edit", "article_write", "article_update", "article_save", "compose", "component_insert", "template_apply", "asset_insert", "image_insert", "image_manifest"}
            metadata = bridge._operation_metadata() if method in metadata_methods else None
            _write_response({"id": request_id, "ok": True, "result": result, "context": metadata})
            cancel_events.pop(request_id, None)
            if method == "shutdown":
                break
    except KeyboardInterrupt:
        _log("interrupted")
    finally:
        try:
            bridge.shutdown()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
