# scripts/ 辅助脚本说明

这里放的是**独立脚本**：把用户随稿件发来的 Word 文档里的图片和文字解析成「哪张图配哪段文字」，
并产出一份可执行的配图计划。它们不参与插件运行时（Host bundle），可以单独运行、单独测试；
插件本体（`bridge.py` / `index.js` / `client.js` / `theme.js` / `config.js`）由插件主文件维护。

| 脚本 | 一句话用途 | 依赖 |
| --- | --- | --- |
| `extract_doc_images.py` | 按文档真实顺序抽图，生成图文锚点 `manifest.json` | python-docx |
| `anchor_match.py` | 把锚点文本匹配到正文段号 | 标准库 |
| `demo_doc_workflow.py` | 一条命令串起「文档 → 配图计划」，产出 `insert-plan.json` | python-docx + 上面两个脚本 |
| `test_anchor_match.py` | `anchor_match.py` 的单元测试 | 标准库 |
| `_make_test_docx.py` | 生成带内嵌图片的测试 `.docx` | python-docx |
| `privacy-check.mjs` | 打包前的隐私 / 文件完整性闸门 | Node.js（由插件本体维护） |

约定：所有脚本的 stdout 用于机器可读结果（JSON / 表格），错误信息用中文写 stderr，
出错时退出码为 `1`，不会静默失败。脚本内已强制 UTF-8 输出，被其他进程捕获时中文不乱码。

---

## 1. extract_doc_images.py

**用途**：按 `.docx` 正文中图片**真实出现的顺序**导出内嵌图片，并生成 `manifest.json`
记录每张图的前后文与锚点文字。

```bash
python scripts/extract_doc_images.py 稿件.docx
python scripts/extract_doc_images.py 稿件.docx --out %TEMP%\xiumi-doc-images
```

**参数**

| 参数 | 说明 |
| --- | --- |
| `docx`（位置参数） | 稿件路径，只接受 `.docx` / `.docm` |
| `--out DIR` | 图片与 `manifest.json` 的输出目录；默认 `%TEMP%\xiumi-doc-images-<时间戳>` |

**输出**

- 图片文件：`01_原文件名.png`、`02_原文件名.png`……（保留原扩展名，编号即出现顺序）
- `manifest.json`：数组，每项字段如下

| 字段 | 含义 |
| --- | --- |
| `index` | 第几张图，从 1 开始，按正文出现顺序 |
| `file` | 导出的图片文件名 |
| `absolutePath` | 图片落盘的绝对路径（给上传步骤用） |
| `width` / `height` | 原始像素尺寸；格式无法解析时退回文档里的显示尺寸并在 stderr 警告 |
| `beforeText` | 图片之前最近 1~2 段正文，`\n` 分隔，单条截到 300 字 |
| `afterText` | 图片之后最近 1~2 段正文，同上 |
| `anchorText` | 锚点：图片所在段落自身文字；本段无文字时取最近上文，再退回最近下文 |

**常见错误**（均为中文提示 + 退出码 1）

- 文件不存在 / 传的是目录
- 不是 `.docx`（例如 `.doc`、`.txt`）
- 文件损坏、不是合法的 OOXML
- 文档内没有内嵌图片
- 图片没有二进制数据 / 解码失败 / 无法确定尺寸 / 写盘失败

---

## 2. anchor_match.py

**用途**：把 manifest 里的 `anchorText` 落到「正文第几段」，供插入定位使用。

```bash
python scripts/anchor_match.py %TEMP%\xiumi-doc-images\manifest.json --paragraphs paragraphs.json
```

`paragraphs.json` 是正文按顺序的纯文本数组：`["第一段正文", "第二段正文", ...]`。

**参数**

| 参数 | 说明 |
| --- | --- |
| `manifest`（位置参数） | 上一步的 `manifest.json` |
| `--paragraphs FILE` | 必填，正文段落数组 JSON |
| `--threshold N` | 模糊匹配最低相似度，0~1，默认 `0.6` |
| `--pretty` | 缩进输出 JSON |
| `--no-preview` | 不往 stderr 打人类可读摘要 |

**输出**：stdout 为 `{ "matches": [ ... ] }`，每项

| 字段 | 含义 |
| --- | --- |
| `index` / `file` / `anchorText` | 原样带回 |
| `matchType` | `exact` / `normalized` / `fuzzy` / `none` |
| `paragraphIndex` | 命中段号，**0-based**；`-1` 表示未匹配；同段多图时带 `.5` 偏移 |
| `confidence` | 1.0 / 0.9 / 模糊分数 / 0.0 |
| `snippet` | 命中段落压缩空白后的前 80 字（`none` 时为空串） |
| `sameAnchor` | 仅当多张图命中同一段时的第 2 张起出现，值为 `true` |

**策略优先级**

1. `exact`：去首尾空白后完全相等 → 1.0
2. `normalized`：去空白、全角转半角、去标点、小写后相等 → 0.9
3. `fuzzy`：最长公共子串长度 ÷ `max(len)`，取最高分且不低于阈值 → 分数即置信度
4. `none`：都不满足 → `paragraphIndex = -1`

**也可以当模块用**：

```python
from anchor_match import match_anchors

matches = match_anchors(manifest, paragraphs, threshold=0.6)
```

**常见错误**：文件不存在、JSON 非法（报行号列号）、顶层不是数组、`--threshold` 超出 0~1。

---

## 3. demo_doc_workflow.py

**用途**：一条命令跑完「抽图 → 锚点匹配 → 配图计划」，最适合先看效果或做端到端演示。

```bash
python scripts/demo_doc_workflow.py 稿件.docx --out %TEMP%\xiumi-plan
python scripts/demo_doc_workflow.py 稿件.docx --threshold 0.5
```

**参数**

| 参数 | 说明 |
| --- | --- |
| `docx`（位置参数） | 稿件路径 |
| `--out DIR` | 图片与 `insert-plan.json` 的输出目录；默认 `%TEMP%\xiumi-doc-images-<时间戳>` |
| `--threshold N` | 模糊匹配最低相似度，默认 `0.6` |

内部依次调用 `extract_doc_images.extract(...)` 与 `anchor_match.match_anchors(...)`，
不重复实现抽取和匹配逻辑。

**输出 1：`insert-plan.json`**

```json
{
  "source": "稿件路径",
  "paragraphCount": 4,
  "images": [
    {
      "index": 1,
      "file": "01_image1.png",
      "absolutePath": "…/01_image1.png",
      "anchorText": "段2",
      "paragraphIndex": 1,
      "insertAfterParagraph": 2,
      "confidence": 1.0,
      "matchType": "exact",
      "sameAnchor": false
    }
  ],
  "unmatched": [
    {
      "index": 2,
      "file": "02_image2.png",
      "absolutePath": "…/02_image2.png",
      "anchorText": "…",
      "matchType": "none",
      "confidence": 0.0,
      "reason": "未找到相似度不低于 0.60 的正文段落，请人工确认插图位置"
    }
  ]
}
```

| 字段 | 含义 |
| --- | --- |
| `paragraphCount` | 参与编号的正文段数：只数**有字段落**，纯图片段和空段不计，与人在文档里看到的段落一致 |
| `images[].paragraphIndex` | 0-based 段号，来自 `anchor_match` |
| `images[].insertAfterParagraph` | `paragraphIndex + 1`，**1-based**，直接表示「插到第 N 段之后」；同段多图会带 `.5`，按升序插入 |
| `images[].sameAnchor` | 是否为「同一段的第 2 张及以后」 |
| `unmatched` | 没能定位的图片，单独列出**绝不丢弃**，需人工定段 |

**输出 2：终端图文对照表**

列为「图号 / 文件名 / 锚点 / 命中段 / 置信度 / 状态」，状态为 `已定位`、`同段第 N 张` 或 `未匹配`，
末尾汇总图片目录、计划文件路径和未匹配数量。

**退出码**：全部匹配 → 0；有图片落进 `unmatched` 但流程正常 → 同样 0（并在输出里醒目提示）；
文档错误、依赖缺失、读写失败 → 1。

---

## 4. privacy-check.mjs

**用途**：发布 / 打包前的隐私与文件完整性闸门。它扫描所有会随包发布的文本文件，
抓盘符绝对路径、本机用户名与主机名、手机号、登录态 / token / 密码一类的字面量，
并检查必需文件是否齐全。

```bash
node scripts/privacy-check.mjs
```

- 退出码 `0` = 通过，`1` = 至少一条 findings
- 输出分 `checks` / `findings` / `warnings` / 项目树 四段
- 由插件本体维护，本目录只描述用法，不修改它

---

## 5. _make_test_docx.py

**用途**：生成用来验证抽取与匹配的样例 `.docx`（含内嵌纯色 PNG，不需要 Pillow）。

```bash
python scripts/_make_test_docx.py
python scripts/_make_test_docx.py %TEMP%\xiumi-docx-test\demo.docx
python scripts/_make_test_docx.py --variant same-paragraph
```

**参数**

| 参数 | 说明 |
| --- | --- |
| `out`（可选位置参数） | 输出 `.docx` 路径；默认 `%TEMP%\xiumi-docx-test\test_images.docx` |
| `--variant default` | 段落顺序：段1 / 段2 / 图A / 段3 / 段4 / 图B（期望 2 条 manifest，图A 锚点「段2」，图B 锚点「段4」） |
| `--variant same-paragraph` | 额外插入「图与文字同段」的段落，用于验证锚点取本段文字（期望 3 条 manifest） |

**验证用法**

```bash
python scripts/_make_test_docx.py
python scripts/extract_doc_images.py %TEMP%\xiumi-docx-test\test_images.docx --out %TEMP%\xiumi-demo
python scripts/demo_doc_workflow.py %TEMP%\xiumi-docx-test\test_images.docx
```

期望：图 1 → 第 2 段之后，图 2 → 第 4 段之后。

---

## 6. test_anchor_match.py

**用途**：`anchor_match.py` 的单元测试，只依赖标准库 `unittest`。

```bash
python scripts/test_anchor_match.py -v
```

覆盖：精确匹配（含首尾空白）、归一化匹配（全角数字 + 中文标点 + 空格）、
模糊匹配（分数与独立实现的最长公共子串公式逐位核对）、完全无关 → `none`、
多图同锚点（`sameAnchor` 与 `.5` 偏移）、空 manifest / 空段落 / 缺失锚点不崩溃、
阈值生效、参数非法报错，以及 4 个命令行子进程用例。

---

## 推荐工作流

1. **准备稿件**：用户在 Word 里写正文，并把图片放在希望出现的位置附近（图与配文不要隔太多段）。
2. **出计划**：跑 `python scripts/demo_doc_workflow.py 稿件.docx --out %TEMP%\xiumi-plan`，
   得到图片、`manifest.json` 与 `insert-plan.json`。
3. **人工 / Agent 确认图文对应**：看终端「图文对照表」和 `insert-plan.json`，
   重点核对 `anchorText` 与命中段是否确实是用户想要的对应关系；`unmatched` 必须人工定段。
4. **上传图片**：按 `absolutePath` 逐张上传。当前插件用 `xiumi_asset` 管理素材，
   规划中的专用上传工具名为 `xiumi_upload`（以插件实际导出的工具为准）。
5. **写正文**：用插件的 `xiumi_write` 写入正文段落。
6. **插图**：按 `insertAfterParagraph` 插入（1-based，插到第 N 段之后；同段多图按 `.5` 升序依次插入）。
   规划中的插图工具名为 `xiumi_image_insert`；插件尚未提供该工具时，按 `insert-plan.json` 人工插图。
7. **保存前预览确认**：先预览整篇效果，确认图文位置无误后再保存。

> 本目录的脚本只负责「解析文档、产出计划」，不直接操作浏览器、不代替确认步骤。

## 依赖与限制

- **依赖**：需要 `python-docx`（`python -m pip install python-docx`），其余全部标准库；
  Python 3.9+ 可用（实际验证环境为 3.13）。`privacy-check.mjs` 需要 Node.js。
- **只支持 `.docx` / `.docm`**：老的 `.doc`、PDF、网页要先另存为 `.docx`。
- **图片格式**：PNG / JPEG / GIF / BMP 能读出原始像素尺寸；其他格式（EMF、WMF、TIFF 等）
  依赖文档里给出的显示尺寸，取不到时报错。
- **锚点阈值**：默认 `0.6`。段落较短、锚点与段落措辞差异较大时可降到 `0.4`~`0.5` 提高召回，
  但误配概率上升，必须人工复核；反之段落很长、要求严格时可调高。
- **超长段落会被判 `none`**：`manifest` 里的 `anchorText` 会截到 300 字，
  当图片所在段落远超 300 字时，相似度按 `300 ÷ 段长` 计算，可能低于阈值而落进 `unmatched`。
  处理办法：人工确认该图的插入位置；或把长段拆成若干短段后重新生成；或适当调低 `--threshold`
  （建议不低于 `0.4`，并配合人工复核）。
- **段号含义**：只有「有字段落」参与编号（纯图片段、空段不计），所以 `insertAfterParagraph`
  与人眼数出来的段号一致；图片与文字同段时，锚点取该段文字本身。
- **表格与文本框**：表格内的段落会计入正文段落序列；文本框等不进入正文流的图片可能抽不到。
- **临时文件**：默认输出到 `%TEMP%`，可以随时删除；脚本跑完留下的 `__pycache__` 也可以直接删。

## 常见问题

| 现象 | 处理 |
| --- | --- |
| `ModuleNotFoundError: No module named 'docx'` | 安装 python-docx：`python -m pip install python-docx` |
| 中文在管道 / 其他进程里显示为乱码 | 脚本已强制 UTF-8；仍有问题时设置 `PYTHONIOENCODING=utf-8` |
| 明明有图却报「文档内没有找到内嵌图片」 | 图可能是链接图片（外链）或文本框内的图，先改为「嵌入型」图片 |
| 图片匹配到别的段落 | 检查锚点文字是否在正文里重复出现；必要时调高 `--threshold` 或人工指定段号 |
| 想复查脚本本身是否还能跑 | `python -m py_compile scripts/*.py` 加 `python scripts/test_anchor_match.py -v` |
