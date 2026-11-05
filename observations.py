"""Read-only, content-free DOM layout sampling for the browser bridge.

No navigation, interaction, screenshots, HTML, text, image URLs, or browser setup.
All strings in the returned observation are fixed vocabulary, IDs, or colors.
"""
from __future__ import annotations

import hashlib
import math
import re
from urllib.parse import parse_qs, urlsplit, urlunsplit


LAYOUT_OBSERVATION_JS = r"""
() => {
  const ELEMENT_LIMIT = 6000, SEQUENCE_LIMIT = 600, IMAGE_LIMIT = 240;
  const excluded = 'nav,aside,button,input,textarea,select,script,style,noscript,template,' +
    '[role="toolbar"],[role="navigation"],[role="menu"],.tn-editor-toolbar,' +
    '.tn-studio-toolbar,.tn-toolbar,.rich_media_tool,.rich_media_area_extra,' +
    '#js_pc_qr_code,.qr_code_pc,.weui-desktop-nav';
  const round = (n) => Math.round(n * 100) / 100;
  const num = (v) => { const n = Number.parseFloat(v); return Number.isFinite(n) ? round(n) : undefined; };
  const col = (v) => typeof v === 'string' && /^(?:#[\da-f]{3,8}|rgba?\([\d.,\s]+\)|transparent)$/i.test(v.trim()) ? v.trim().toLowerCase() : undefined;
  const opaque = (v) => Boolean(v && v !== 'transparent' && !/^rgba\([^)]*,\s*0(?:\.0+)?\s*\)$/.test(v));
  let measurementErrors = 0;
  const rawStyles = new Map(), rects = new Map();
  const measure = (el) => {
    if (rawStyles.has(el)) return rawStyles.get(el);
    try {
      const s = getComputedStyle(el), r = el.getBoundingClientRect();
      if (!Number.isFinite(r.width) || !Number.isFinite(r.height)) throw new Error('invalid-geometry');
      rawStyles.set(el, s); rects.set(el, r); return s;
    } catch { measurementErrors += 1; rawStyles.set(el, null); return null; }
  };
  const visible = (el) => {
    if (!el || el.nodeType !== 1 || el.closest(excluded)) return false;
    const s = measure(el), r = rects.get(el);
    return Boolean(s && r && r.width > 0.5 && r.height > 0.5 && s.display !== 'none' &&
      s.visibility !== 'hidden' && s.visibility !== 'collapse' && Number(s.opacity || 1) > 0.01);
  };
  const rootOptions = [
    ['.tn-article-body', 'xiumi-public'], ['#js_content', 'wechat-article'],
    ['.rich_media_content', 'wechat-article'], ['.tn-editor-paper', 'xiumi-editor'],
    ['.tn-editor-paper-content', 'xiumi-editor'], ['.tn-paper-content', 'xiumi-editor'],
    ['.preview-content', 'preview-content'], ['.article-content', 'article-content'],
    ['article', 'article'], ['main', 'main'], ['[role="main"]', 'role-main'],
  ];
  let root = null, rootKind = '';
  for (const [selector, kind] of rootOptions) {
    const candidates = Array.from(document.querySelectorAll(selector)).filter(visible);
    if (candidates.length) {
      candidates.sort((a, b) => rects.get(b).width * rects.get(b).height - rects.get(a).width * rects.get(a).height);
      root = candidates[0]; rootKind = kind; break;
    }
  }
  if (!root) {
    const cells = Array.from(document.querySelectorAll('[contenteditable="true"]')).filter(visible);
    if (cells.length) {
      let common = cells[0];
      while (common && !cells.every((cell) => common.contains(cell))) common = common.parentElement;
      if (common && common !== document.body && common !== document.documentElement && visible(common)) {
        root = common; rootKind = 'editable-cell-root';
      }
    }
  }
  if (!root) throw new Error('layout-observation:no-content-root');
  const rootRect = rects.get(root);
  const candidates = [root, ...Array.from(root.querySelectorAll('*'))];
  const truncatedElements = candidates.length > ELEMENT_LIMIT;
  const scanned = candidates.slice(0, ELEMENT_LIMIT);
  const elements = scanned.filter(visible), allowed = new Set(elements);
  const lengths = new Map(), directLengths = new Map(), representatives = new Map();
  const descendantsWithImages = new Map(), descendantBlocks = new Map();
  const tag = (el) => String(el.localName || el.tagName || '').toLowerCase();
  const paragraphTags = new Set(['p', 'li', 'blockquote', 'figcaption', 'dt', 'dd', 'summary']);
  const headingTag = (el) => /^h[1-6]$/.test(tag(el)) || el.getAttribute('role') === 'heading';
  const isMedia = (el) => ['img', 'image', 'video', 'canvas'].includes(tag(el)) ||
    (tag(el) === 'svg' && !el.parentElement?.closest('svg'));
  const transformed = (el) => {
    for (let node = el; node && allowed.has(node); node = node.parentElement) {
      const s = measure(node);
      if (s?.transform && s.transform !== 'none') return true;
    }
    return false;
  };
  const inForeignObject = (el) => Boolean(el.closest('foreignObject,foreignobject'));
  for (const el of [...elements].reverse()) {
    let own = 0;
    // Read only lengths; never place node text in the result, even temporarily as samples.
    for (const node of el.childNodes || []) {
      if (node.nodeType === 3) own += String(node.nodeValue || '').replace(/\s+/g, ' ').trim().length;
    }
    directLengths.set(el, own);
    let total = own, images = isMedia(el) ? 1 : 0, blocks = 0;
    let representative = own ? { el, length: own } : null;
    for (const child of el.children || []) {
      if (!allowed.has(child)) continue;
      total += lengths.get(child) || 0;
      images += descendantsWithImages.get(child) || 0;
      blocks += (paragraphTags.has(tag(child)) || headingTag(child) ? 1 : 0) + (descendantBlocks.get(child) || 0);
      const next = representatives.get(child);
      if (next && (!representative || next.length > representative.length)) representative = next;
    }
    lengths.set(el, total); descendantsWithImages.set(el, images); descendantBlocks.set(el, blocks);
    if (representative) representatives.set(el, representative);
  }
  const styleFor = (el, forText = false) => {
    const s = measure(el), r = rects.get(el), out = {};
    if (!s || !r) return out;
    const textNode = forText ? representatives.get(el)?.el || el : el;
    const ts = measure(textNode) || s;
    const specialCoordinates = forText && (inForeignObject(textNode) || transformed(textNode));
    const mappings = { marginTop: 'marginTop', marginBottom: 'marginBottom', paddingTop: 'paddingTop',
      paddingBottom: 'paddingBottom', paddingLeft: 'paddingLeft', paddingRight: 'paddingRight',
      borderRadius: 'borderRadius', borderLeftWidth: 'borderLeftWidth' };
    for (const [key, property] of Object.entries(mappings)) {
      const value = num(s[property]); if (value !== undefined && value >= 0) out[key] = value;
    }
    const border = Math.max(...['Top', 'Right', 'Bottom', 'Left'].map((side) => num(s['border' + side + 'Width']) || 0));
    if (border > 0) out.borderWidth = border;
    const borderStyle = ['Top', 'Right', 'Bottom', 'Left'].map((side) => s['border' + side + 'Style']).find((v) => ['solid', 'dashed', 'dotted', 'double'].includes(v));
    if (borderStyle) out.borderStyle = borderStyle;
    const borderColor = ['Top', 'Right', 'Bottom', 'Left'].map((side) => col(s['border' + side + 'Color'])).find(Boolean);
    if (border && borderColor) out.borderColor = borderColor;
    if (forText && !specialCoordinates) {
      const fontSize = num(ts.fontSize), linePx = num(ts.lineHeight);
      if (fontSize > 0 && fontSize <= 200) out.fontSize = fontSize;
      const weight = num(ts.fontWeight); if (weight >= 100 && weight <= 1000) out.fontWeight = weight;
      if (linePx > 0 && fontSize > 0 && linePx / fontSize <= 10) out.lineHeight = round(linePx / fontSize);
      const spacing = num(ts.letterSpacing); if (spacing !== undefined) out.letterSpacing = spacing;
    }
    const textColor = col(ts.color), background = col(s.backgroundColor);
    if (textColor && forText) out.color = textColor;
    if (opaque(background)) out.backgroundColor = background;
    if (['left', 'right', 'center', 'justify'].includes(s.textAlign)) out.textAlign = s.textAlign;
    out.width = round(r.width); out.height = round(r.height);
    return out;
  };
  const rawEvents = [], imageEvents = [], textEvents = [], cardStyles = [], dividerStyles = [];
  const counts = { svg: 0, foreignObject: 0, foreignText: 0, backgroundImages: 0, unmeasuredImages: 0, transformedText: 0 };
  let extraMedia = false;
  const foregroundColors = new Map();
  let ordinal = 0;
  const add = (el, entry) => {
    const result = { el, rect: rects.get(el), ordinal: ordinal++, entry };
    rawEvents.push(result); return result;
  };
  const bodySizes = new Map();
  for (const el of elements) {
    if (!paragraphTags.has(tag(el)) || descendantBlocks.get(el) || !lengths.get(el)) continue;
    const fs = styleFor(el, true).fontSize;
    if (fs >= 8 && fs <= 32) bodySizes.set(fs, (bodySizes.get(fs) || 0) + lengths.get(el));
  }
  const bodySize = [...bodySizes].sort((a, b) => b[1] - a[1])[0]?.[0];
  for (const el of elements) {
    const t = tag(el), s = measure(el), r = rects.get(el), length = lengths.get(el) || 0;
    if (t === 'svg') counts.svg += 1;
    if (t === 'foreignobject') { counts.foreignObject += 1; if (length > 0) counts.foreignText += 1; }
    if (s.backgroundImage && s.backgroundImage !== 'none') counts.backgroundImages += 1;
    if (isMedia(el)) {
      if (el.parentElement?.closest('svg')) continue;
      if (imageEvents.length >= IMAGE_LIMIT) { extraMedia = true; continue; }
      const entry = { kind: 'image', textLength: 0, imageCount: 1, style: styleFor(el),
        mediaKind: t === 'svg' ? 'svg' : t === 'video' ? 'video' : t === 'canvas' ? 'canvas' : 'raster',
        aspectRatio: round(r.width / r.height), internalLayoutRecognized: false };
      if (Number(el.naturalWidth) > 0 && Number(el.naturalHeight) > 0) entry.naturalAspectRatio = round(el.naturalWidth / el.naturalHeight);
      if (t === 'img' && (!el.complete || !(Number(el.naturalWidth) > 0))) counts.unmeasuredImages += 1;
      imageEvents.push(add(el, entry));
      continue;
    }
    const isBlock = headingTag(el) || paragraphTags.has(t) ||
      (['div', 'section'].includes(t) && !descendantBlocks.get(el) && length > 0) ||
      (el.getAttribute('contenteditable') === 'true' && !descendantBlocks.get(el) && length > 0);
    // Generic wrappers and nested blockquotes must not duplicate their child text events.
    const nestedTextBlock = descendantBlocks.get(el) > 0 && !headingTag(el);
    if (isBlock && length > 0 && !nestedTextBlock) {
      const st = styleFor(el, true), foreign = inForeignObject(el), scaled = transformed(representatives.get(el)?.el || el);
      const genericDescendant = ['div', 'section'].includes(t) && Array.from(el.children || []).some((child) =>
        allowed.has(child) && ['div', 'section'].includes(tag(child)) && (lengths.get(child) || 0) > 0);
      if (!genericDescendant) {
        const heuristicHeading = !headingTag(el) && !foreign && !scaled && length <= 160 &&
          st.fontWeight >= 600 && st.fontSize > (bodySize || 16) * 1.12;
        const isHeading = headingTag(el) && !foreign && !scaled || heuristicHeading;
        const entry = { kind: isHeading ? 'heading' : t === 'figcaption' ? 'caption' : 'paragraph',
          textLength: length, style: st, hasDomText: true,
          classification: isHeading && !headingTag(el) ? 'numeric-heuristic' : 'dom-structure',
          insideSvgForeignObject: foreign, fontMetricsExcluded: foreign || scaled };
        if (isHeading) {
          const level = /^h[1-6]$/.test(t) ? Number(t.slice(1)) : num(el.getAttribute('aria-level'));
          if (level >= 1 && level <= 6) entry.level = level;
        }
        if (scaled) counts.transformedText += 1;
        if (st.color) foregroundColors.set(st.color, (foregroundColors.get(st.color) || 0) + length);
        textEvents.push(add(el, entry));
      }
    }
    if (el === root) continue;
    const st = styleFor(el), hasImage = (descendantsWithImages.get(el) || 0) > 0;
    const line = t === 'hr' || (!length && !hasImage && r.width >= 12 && r.height <= 12 &&
      (st.borderWidth > 0 || (opaque(st.backgroundColor) && r.height <= 6)));
    if (line) { dividerStyles.push(st); add(el, { kind: 'divider', textLength: 0, style: st }); continue; }
    const padding = Math.max(st.paddingTop || 0, st.paddingBottom || 0, st.paddingLeft || 0, st.paddingRight || 0);
    const card = ['div', 'section', 'article', 'blockquote'].includes(t) && (length >= 10 || hasImage) &&
      r.width >= rootRect.width * 0.25 && r.height >= 20 &&
      (st.borderWidth > 0 || (opaque(st.backgroundColor) && padding >= 4));
    if (card) { cardStyles.push(st); add(el, { kind: 'card', textLength: 0, imageCount: descendantsWithImages.get(el) || 0, style: st }); }
    if (!length && !hasImage && !el.children.length && r.height >= 4 && r.height <= 200 && r.width > rootRect.width * 0.1) {
      add(el, { kind: 'spacer', textLength: 0, style: st });
    }
  }
  if (!textEvents.length && !imageEvents.length) throw new Error('layout-observation:no-measurable-content');
  const groups = [], grouped = new Set();
  for (let i = 0; i < imageEvents.length; i++) {
    if (grouped.has(i)) continue;
    const group = [i];
    for (let j = i + 1; j < Math.min(imageEvents.length, i + 12); j++) {
      if (grouped.has(j)) continue;
      const a = imageEvents[group[group.length - 1]].rect, b = imageEvents[j].rect;
      const overlap = Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top);
      const gap = b.left - a.right;
      if (overlap >= Math.min(a.height, b.height) * 0.5 && gap >= -1 && gap <= rootRect.width * 0.15) group.push(j);
    }
    if (group.length < 2) continue;
    group.forEach((index) => grouped.add(index));
    const members = group.map((index) => imageEvents[index]);
    const gaps = members.slice(1).map((member, index) => Math.max(0, round(member.rect.left - members[index].rect.right)));
    const gap = gaps.length ? gaps[0] : 0;
    const alignment = Math.max(...members.map((v) => v.rect.top)) - Math.min(...members.map((v) => v.rect.top)) <= 8 ? 'top' :
      Math.max(...members.map((v) => v.rect.bottom)) - Math.min(...members.map((v) => v.rect.bottom)) <= 8 ? 'bottom' : 'overlap';
    const groupId = groups.length;
    for (const member of members) member.entry.groupIndex = groupId;
    groups.push({ members, gap, alignment });
    rawEvents.push({ el: members[0].el, rect: members[0].rect, ordinal: members[0].ordinal - 0.5,
      entry: { kind: 'image-group', textLength: 0, imageCount: members.length, groupIndex: groupId,
        style: { columns: members.length, gap }, groupingBasis: 'geometry-only' } });
  }
  rawEvents.sort((a, b) => a.ordinal - b.ordinal);
  const truncatedSequence = rawEvents.length > SEQUENCE_LIMIT || extraMedia;
  const sampledEvents = rawEvents.slice(0, SEQUENCE_LIMIT);
  const eventIndex = new Map(sampledEvents.map((event, index) => [event, index]));
  const sampledImages = imageEvents.filter((event) => eventIndex.has(event));
  const imageGroups = groups.map((group) => ({
    imageIndices: group.members.filter((event) => eventIndex.has(event)).map((event) => eventIndex.get(event)),
    count: group.members.length, gapPx: group.gap, align: group.alignment, groupingBasis: 'geometry-only',
  })).filter((group) => group.imageIndices.length >= 2);
  const imageTextAdjacency = sampledImages.map((image) => {
    const index = eventIndex.get(image), out = { imageIndex: index, relationship: 'positional-only' };
    for (let k = index - 1; k >= Math.max(0, index - 4); k--) {
      const candidate = sampledEvents[k];
      if (['paragraph', 'heading', 'caption'].includes(candidate.entry.kind) && !candidate.entry.insideSvgForeignObject) {
        out.beforeTextIndex = k; out.precedingGapPx = round(image.rect.top - candidate.rect.bottom); break;
      }
    }
    for (let k = index + 1; k < Math.min(sampledEvents.length, index + 5); k++) {
      const candidate = sampledEvents[k];
      if (['paragraph', 'heading', 'caption'].includes(candidate.entry.kind) && !candidate.entry.insideSvgForeignObject) {
        out.afterTextIndex = k; out.followingGapPx = round(candidate.rect.top - image.rect.bottom); break;
      }
    }
    return out;
  });
  const paragraphs = textEvents.filter((e) => e.entry.kind === 'paragraph' && !e.entry.fontMetricsExcluded);
  const headings = textEvents.filter((e) => e.entry.kind === 'heading');
  const bodyEvent = [...paragraphs].sort((a, b) => b.entry.textLength - a.entry.textLength)[0];
  const body = bodyEvent ? { ...bodyEvent.entry.style } : {};
  if (bodyEvent) {
    const gaps = paragraphs.map((event) => event.entry.style.marginBottom).filter((gap) => gap > 0 && gap <= 500);
    // Some previews encode paragraph spacing as empty blocks or wrapper margins.
    // Use measured gaps only between consecutive paragraphs, without intervening
    // media/section events, and keep CSS margin evidence preferable when present.
    if (!gaps.length) {
      for (let index = 0; index < sampledEvents.length - 1; index++) {
        const first = sampledEvents[index];
        if (first.entry.kind !== 'paragraph' || first.entry.fontMetricsExcluded) continue;
        let nextIndex = index + 1;
        while (sampledEvents[nextIndex]?.entry.kind === 'spacer') nextIndex += 1;
        const next = sampledEvents[nextIndex];
        if (next?.entry.kind !== 'paragraph' || next.entry.fontMetricsExcluded) continue;
        const gap = round(next.rect.top - first.rect.bottom);
        if (gap > 0 && gap <= 100 && Math.abs(first.rect.left - next.rect.left) <= rootRect.width * 0.1) gaps.push(gap);
      }
    }
    if (gaps.length) {
      const frequencies = new Map(); for (const gap of gaps) frequencies.set(gap, (frequencies.get(gap) || 0) + 1);
      body.paragraphGap = [...frequencies].sort((a, b) => b[1] - a[1])[0][0];
    }
  }
  const titles = [];
  for (const event of headings) {
    const item = { ...event.entry.style };
    if (event.entry.level) item.level = event.entry.level;
    if (!titles.some((s) => s.fontSize === item.fontSize && s.fontWeight === item.fontWeight)) titles.push(item);
  }
  titles.sort((a, b) => (b.fontSize || 0) - (a.fontSize || 0));
  const palette = {};
  if (body.color) palette.text = body.color;
  const rootBackground = col(measure(root).backgroundColor);
  if (opaque(rootBackground)) palette.background = rootBackground;
  const accentColors = [...foregroundColors].sort((a, b) => b[1] - a[1]).map(([value]) => value).filter((value) => value !== body.color);
  if (accentColors.length) { palette.primary = accentColors[0]; palette.accents = accentColors.slice(0, 4); }
  const limitations = ['dom-structure-only', 'not-semantically-verified', 'not-visually-verified',
    'image-internal-layout-unavailable', 'editor-editability-not-verified', 'text-lengths-are-approximate'];
  if (counts.foreignObject) limitations.push('svg-internal-coordinates-not-rendered-font-size');
  if (counts.transformedText) limitations.push('font-metrics-with-transform-excluded');
  if (counts.backgroundImages) limitations.push('css-background-images-have-no-internal-layout');
  if (counts.unmeasuredImages) limitations.push('lazy-or-unmeasured-media');
  if (truncatedElements) limitations.push('element-scan-truncated');
  if (truncatedSequence) limitations.push('sequence-truncated');
  if (measurementErrors) limitations.push('some-style-measurements-failed');
  const generic = ['article', 'main', 'role-main', 'editable-cell-root'].includes(rootKind);
  if (generic) limitations.push('generic-content-root');
  const sparse = (lengths.get(root) || 0) < 20 && !imageEvents.length;
  if (sparse) limitations.push('sparse-content');
  const source = new URL(location.href); source.username = ''; source.password = ''; source.search = '';
  const paper = source.hash.replace(/^#/, '').match(/^\/?paper\/(?:for\/)?(\d+)/);
  source.hash = paper ? `/paper/for/${paper[1]}` : '';
  const observation = {
    version: 1, rootKind, rootConfidence: generic ? 'generic-content' : 'explicit-content',
    charCount: lengths.get(root) || 0, imageCount: imageEvents.length,
    paragraphCount: textEvents.filter((e) => e.entry.kind === 'paragraph').length, headingCount: headings.length,
    sequence: sampledEvents.map((event) => event.entry),
    profile: {
      charCount: lengths.get(root) || 0, imageCount: imageEvents.length,
      paragraphCount: textEvents.filter((e) => e.entry.kind === 'paragraph').length, headingCount: headings.length,
      style: { titles: titles.slice(0, 8), body, palette,
        images: sampledImages[0] ? { ...sampledImages[0].entry.style } : {},
        decor: { cards: cardStyles.length, dividers: dividerStyles.length,
          borderedElements: cardStyles.filter((s) => s.borderWidth > 0).length, svgCount: counts.svg,
          imageTextBlocks: imageTextAdjacency.filter((v) => v.beforeTextIndex !== undefined || v.afterTextIndex !== undefined).length },
        dividers: dividerStyles.slice(0, 16) },
    },
    metrics: {
      rootWidthPx: round(rootRect.width), rootHeightPx: round(rootRect.height), svgCount: counts.svg,
      foreignObjectCount: counts.foreignObject, foreignObjectTextCount: counts.foreignText,
      hasSvgForeignObjectText: counts.foreignText > 0, editorEditabilityVerified: false,
      backgroundImageCount: counts.backgroundImages, unmeasuredImageCount: counts.unmeasuredImages,
      computedStylesTransformed: counts.transformedText, imageGroups, imageTextAdjacency,
      positionOnly: true, lineHeightUnit: 'multiple', textLengthUnit: 'utf16-code-units-normalized-per-run',
    },
    sampling: { elementLimit: ELEMENT_LIMIT, sequenceLimit: SEQUENCE_LIMIT, imageLimit: IMAGE_LIMIT,
      candidateCount: candidates.length, sampledElementCount: elements.length, sampledEventCount: sampledEvents.length,
      measurementErrors, observedRenderedContent: true },
    incomplete: truncatedElements || truncatedSequence || measurementErrors > 0 || counts.unmeasuredImages > 0 || sparse,
    truncated: truncatedElements || truncatedSequence, visuallyVerified: false, limitations,
  };
  return { url: source.toString(), observation, origin: 'external' };
}
"""


class LayoutObservationError(RuntimeError):
    """The page could not produce a reliable, content-free observation."""


_ALLOWED_KEYS = set("""
version rootKind rootConfidence charCount imageCount paragraphCount headingCount
sequence profile style titles body palette images decor dividers metrics sampling
incomplete truncated visuallyVerified limitations articleId
kind textLength level hasDomText classification insideSvgForeignObject fontMetricsExcluded
mediaKind aspectRatio naturalAspectRatio internalLayoutRecognized groupIndex groupingBasis
fontSize fontWeight lineHeight letterSpacing paragraphGap firstLineIndent marginTop marginBottom
padding paddingTop paddingBottom paddingLeft paddingRight borderWidth borderLeftWidth borderRadius
width height gap columns color background backgroundColor borderColor borderLeftColor borderStyle
textAlign align tag primary secondary text surface accents cards borderedElements svgCount imageTextBlocks
rootWidthPx rootHeightPx foreignObjectCount foreignObjectTextCount hasSvgForeignObjectText
editorEditabilityVerified backgroundImageCount unmeasuredImageCount computedStylesTransformed
imageGroups imageTextAdjacency positionOnly lineHeightUnit textLengthUnit
imageIndices count gapPx relationship imageIndex beforeTextIndex afterTextIndex precedingGapPx followingGapPx
elementLimit sequenceLimit imageLimit candidateCount sampledElementCount sampledEventCount
measurementErrors observedRenderedContent
""".split())
_ALLOWED_VALUES = set("""
xiumi-public wechat-article xiumi-editor preview-content article-content article main role-main
editable-cell-root generic-content explicit-content heading paragraph image image-group caption card
divider spacer raster svg video canvas numeric-heuristic dom-structure geometry-only positional-only
left right center justify top bottom overlap none solid dashed dotted double transparent
h1 h2 h3 h4 h5 h6 p section div multiple utf16-code-units-normalized-per-run
dom-structure-only not-semantically-verified not-visually-verified image-internal-layout-unavailable
editor-editability-not-verified text-lengths-are-approximate svg-internal-coordinates-not-rendered-font-size
font-metrics-with-transform-excluded css-background-images-have-no-internal-layout lazy-or-unmeasured-media
element-scan-truncated sequence-truncated some-style-measurements-failed generic-content-root sparse-content
""".split())
_COLOR = re.compile(r"^(?:#[\da-f]{3,8}|rgba?\([\d.,\s]+\))$", re.I)


def _source_identity(raw: str) -> tuple[str, str]:
    try:
        parsed = urlsplit(raw)
        host = (parsed.hostname or "").lower()
        xiumi = any(host == domain or host.endswith("." + domain) for domain in ("xiumi.us", "xiumius.cn"))
        default_port = 443 if parsed.scheme == "https" else 80
        if parsed.scheme not in {"http", "https"} or not (xiumi or host == "mp.weixin.qq.com") or parsed.port not in (None, default_port):
            raise ValueError("unsupported-source")
        query = parse_qs(parsed.query)
        match = re.search(r"/paper/(?:for/)?(\d+)(?:/|$|[?#])", parsed.path + "#" + parsed.fragment)
        if not match and xiumi:
            match = re.search(r"/board/v5/(?:[^/]+/)+(\d+)(?:/|$)", parsed.path)
        article_id = match.group(1) if match else ""
        if not article_id and xiumi:
            for key in ("paperId", "paper_id", "articleId", "itemId", "id"):
                candidate = query.get(key, [""])[0]
                if re.fullmatch(r"\d{1,40}", candidate):
                    article_id = candidate
                    break
        if not article_id and host == "mp.weixin.qq.com":
            sn = query.get("sn", [""])[0]
            if re.fullmatch(r"[a-zA-Z\d_-]{8,80}", sn):
                article_id = "wx-" + sn
            else:
                mid, idx = query.get("mid", [""])[0], query.get("idx", ["1"])[0]
                if re.fullmatch(r"\d{1,40}", mid) and re.fullmatch(r"\d{1,6}", idx):
                    payload = f"{query.get('__biz', [''])[0]}:{mid}:{idx}"
                    article_id = "wx-" + hashlib.sha256(payload.encode()).hexdigest()[:20]
        fragment_match = re.match(r"/?paper/(?:for/)?(\d+)", parsed.fragment)
        fragment = "/paper/for/" + fragment_match.group(1) if fragment_match else ""
        normalized = urlunsplit((parsed.scheme, host, parsed.path, "", fragment))
        return normalized, article_id
    except (ValueError, TypeError, AttributeError) as exc:
        raise LayoutObservationError("观察页来源必须为公开 http(s) 秀米或微信文章链接。") from exc


def _validate_tree(value, key="", depth=0):
    if depth > 12:
        raise LayoutObservationError("DOM 观察结果嵌套过深。")
    if isinstance(value, dict):
        for name, child in value.items():
            if name not in _ALLOWED_KEYS:
                raise LayoutObservationError("DOM 观察结果包含未授权字段，已拒绝输出。")
            _validate_tree(child, name, depth + 1)
    elif isinstance(value, list):
        if len(value) > 600:
            raise LayoutObservationError("DOM 观察结果超过采样上限。")
        for child in value:
            _validate_tree(child, key, depth + 1)
    elif isinstance(value, str):
        if key == "articleId" and re.fullmatch(r"[a-zA-Z\d_-]{1,100}", value):
            return
        if value not in _ALLOWED_VALUES and not _COLOR.fullmatch(value):
            raise LayoutObservationError("DOM 观察结果包含非结构文字，已拒绝输出。")
    elif isinstance(value, bool):
        return
    elif isinstance(value, (int, float)):
        if not math.isfinite(value) or abs(value) > 100_000_000:
            raise LayoutObservationError("DOM 观察结果包含无效数值。")
    else:
        raise LayoutObservationError("DOM 观察结果包含无效数据类型。")


def observe_page(page) -> dict:
    """Evaluate the current synchronous Playwright-like page without interacting.

    Returns {url, observation, origin: 'external'}. For LearningLibrary.observe,
    also pass profile=result['observation']['profile']. SVG text presence proves
    DOM text only, never editor editability or image-internal typography.
    Raises LayoutObservationError for missing roots, empty/invalid sampling, or
    evaluation errors. Partial real samples retain incomplete/truncated flags.
    """
    try:
        payload = page.evaluate(LAYOUT_OBSERVATION_JS)
    except Exception as exc:
        raise LayoutObservationError(f"只读 DOM 结构观察失败（{type(exc).__name__}）；未生成默认样本。") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("observation"), dict):
        raise LayoutObservationError("DOM 观察未返回有效结构；未生成默认样本。")
    observation = payload["observation"]
    required = {"version", "sequence", "profile", "metrics", "sampling", "incomplete", "truncated", "visuallyVerified", "charCount", "imageCount"}
    if not required.issubset(observation) or observation.get("version") != 1 or not isinstance(observation["sequence"], list) or not observation["sequence"]:
        raise LayoutObservationError("DOM 观察缺少必要采样；未生成默认样本。")
    if observation["visuallyVerified"] is not False or not isinstance(observation["incomplete"], bool) or not isinstance(observation["truncated"], bool):
        raise LayoutObservationError("DOM 观察验证状态无效。")
    if any(not isinstance(observation[key], dict) for key in ("profile", "metrics", "sampling")):
        raise LayoutObservationError("DOM 观察缺少有效样式与采样元数据。")
    if any(type(observation[key]) is not int or observation[key] < 0 for key in ("charCount", "imageCount")):
        raise LayoutObservationError("DOM 观察内容计数无效。")
    kinds = {"heading", "paragraph", "caption", "image", "image-group", "card", "divider", "spacer"}
    if any(not isinstance(entry, dict) or not isinstance(entry.get("kind"), str) or entry.get("kind") not in kinds for entry in observation["sequence"]):
        raise LayoutObservationError("DOM 观察序列元素无效。")
    if not any((entry.get("kind") in {"heading", "paragraph", "caption"} and isinstance(entry.get("textLength"), (int, float)) and entry["textLength"] > 0) or
               (entry.get("kind") in {"image", "image-group"} and isinstance(entry.get("imageCount"), (int, float)) and entry["imageCount"] > 0)
               for entry in observation["sequence"]):
        raise LayoutObservationError("DOM 观察没有有效图文采样；未生成默认样本。")
    if not isinstance(observation["sampling"], dict) or not observation["sampling"].get("observedRenderedContent") or observation["sampling"].get("sampledElementCount", 0) <= 0:
        raise LayoutObservationError("DOM 观察没有实际渲染内容证据。")
    _validate_tree(observation)
    source_url = getattr(page, "url", None) or payload.get("url", "")
    url, article_id = _source_identity(source_url)
    # Browser query parameters and publisher identifiers never leave this function.
    if article_id:
        observation["articleId"] = article_id
    return {"url": url, "observation": observation, "origin": "external"}
