# xiumi-assistant

[![Version](https://img.shields.io/badge/version-0.2.0-4d6bfe?style=flat-square)](https://github.com/alk233/xiumi-assistant/releases)
[![License](https://img.shields.io/github/license/alk233/xiumi-assistant?style=flat-square)](https://github.com/alk233/xiumi-assistant/blob/main/LICENSE)
[![DSH](https://img.shields.io/badge/DSH-%3E%3D0.1.5--rc.1-4d6bfe?style=flat-square)](https://github.com/deepseek-ai/deepseek-harness)
[![platform](https://img.shields.io/badge/platform-Windows%20%2B%20Edge-4d6bfe?style=flat-square)](#)
[![Python](https://img.shields.io/badge/python-3.10%2B-3776AB?style=flat-square&logo=python&logoColor=white)](https://www.python.org/)
[![GitHub Stars](https://img.shields.io/github/stars/alk233/xiumi-assistant?style=flat-square)](https://github.com/alk233/xiumi-assistant/stargazers)

让 DSH 里的 AI 真正会用[秀米](https://xiumi.us)排版。

秀米没有公开 API，所有操作只能在浏览器里完成。这个插件开一个**你亲眼看得见的 Edge 窗口**，
让 AI 像人一样点进去——新建图文、挑模板、插图、设样式、保存——每一步都在你眼前发生，
你随时可以接手。

以 Cordis 插件交付，Host + Client 双半，17 个工具，零 `@deepseek-ai/*` 运行时依赖。

---

## ⚠️ 重要声明

> **请在使用前完整阅读本节。**

### 全程由 AI 编写

本项目的**全部代码、文档与测试均由 AI 生成**，未经专业软件工程师人工审查。
AI 生成内容可能包含逻辑缺陷、安全漏洞、平台兼容性问题或与事实不符的描述，
即使作者已尽力验证，**仍可能存在未被发现的错误**。

### 按「现状」提供，不承担任何责任

本项目按 **"AS IS"** 提供，不作任何明示或暗示的担保，包括但不限于对
**适销性、特定用途适用性、无侵权**的担保。

在适用法律允许的最大范围内，**作者及任何贡献者不对因使用或无法使用本项目
而产生的任何直接、间接、偶然、特殊、惩罚性或后果性损害承担责任**，包括但不限于：

- 图文内容丢失、排版错乱、账号被限制
- 服务中断、设备损坏、业务损失
- 因使用本项目而违反任何第三方（含秀米、腾讯）服务条款所导致的后果

**使用风险由你自行承担。** 账号密码只保存在你自己机器的
`~/.xiumi-assistant/config.json` 里，插件不上传、不代管、不记录。

### 仅供学习与研究

本项目**仅供个人学习、技术研究与交流**，不得用于：

- 任何商业用途
- 批量刷稿、营销推广、骚扰他人
- 任何违反秀米服务条款或相关法律法规的用途

底层依赖秀米的 Web 编辑器。**它的前端结构随时可能变更**，届时插件可能失效。
请遵守秀米的服务条款，因违规使用导致的一切后果由使用者自负。

### 与上游项目无关联

本项目为**非官方第三方插件**，与 DeepSeek Harness、秀米、腾讯及微信
**均无任何隶属或背书关系**。

---

## 为什么需要它

秀米没有开放平台，没有开发者文档，没有 API。想自动化只有一条路：控制浏览器。

而在这条路上，所有方案都会踩进同一个坑——**改 DOM，保存就丢**。

```js
// 看起来成功了，但一保存就回到原样
document.querySelector('[contenteditable]').innerHTML = '<p style="font-size:20px">标题</p>';
```

原因是秀米的编辑器是 Angular 应用，正文存在 **`scope.cell` 模型**里，不是 DOM 里。
DOM 只是渲染结果，你改它，Angular 不知道，保存时当然按模型写回旧内容。

第二个坑是**看不见**。无头浏览器跑完告诉你「成功」，但你不知道它到底排成了什么样。

这个插件同时解决这两件事：所有写入走 Angular 模型，所有操作在可见窗口里发生。

## 功能

- **可见浏览器** — 固定 `headless=False`，开在你有显示器的那台机器上，随时接管
- **Angular 模型写入** — 正文、样式、背景都写 `scope.cell` / `scope.comp`，保存必持久
- **模板库真实组件** — 标题、分割线、卡片从秀米模板库里的免费组件挑，不是手打符号
- **保存独立读回核验** — 保存后另开一个页面重新读，比对完整模型哈希，不一致就报 `saveVerified: false`
- **每段独立单元格** — 每段文本一个 cell，和人工在秀米里排版的结构一致
- **就地编辑** — 改旧文用 `xiumi_edit`，不新建；新建多少篇，「我的图文」里就多多少条垃圾
- **容量守卫** — 图库 100 张、图文 30 篇，快满前主动提醒，绝不自动删用户内容
- **持续学习** — 学参考推文的排版规则、记模板库目录结构，下次不用重新找

## 17 个工具

| 工具 | 干什么 |
|---|---|
| `xiumi_guide` | **创作前必调**。返回制作指南 + R1–R17 硬规则；不调它，所有产出型工具会被拒绝 |
| `xiumi_browser` | 开/关浏览器、查状态、站内导航（返回/首页/编辑器/列表）、截图 |
| `xiumi_login` | 登录：自动填（读本机 config）、等用户扫码、退出账号 |
| `xiumi_articles` | 图文列表、打开、新建、保存 |
| `xiumi_write` | 往当前稿写正文（纯文本 / 带样式的 HTML） |
| `xiumi_edit` | **就地**增删改：inspect / replace_body / set_cell / clear_cell / append_cell |
| `xiumi_compose` | 一次性组合：create（从零）/ layout-only（只排版，一字不改） |
| `xiumi_review` | 给当前稿打分（100 分制 + 五维评分 + 可执行的修改建议），只读不保存 |
| `xiumi_reference` | 按链接学别人的排版，存为可复用的风格档案 |
| `xiumi_template` | 搜秀米模板库、套用模板 |
| `xiumi_component` | 插模板库里的**单个组件**（标题/分割线/卡片），可批量、可替换占位文字 |
| `xiumi_catalog` | 模板库目录（分类树 + 缓存），避免每次重新翻 |
| `xiumi_asset` | 秀米素材库（形状/插画/图标），只处理免费素材 |
| `xiumi_upload` | 上传本地图片到「我的图库」 |
| `xiumi_image_insert` | 按锚点文字把图库图片插到对应段落后面 |
| `xiumi_image_manifest` | 批量插图：按 manifest 里的锚点一次插多张 |
| `xiumi_quota` | 查容量、清理指定条目（需要 `confirm=true`，且只删你点名的） |

## R1–R17 硬性规则

这些不是建议，是**写进代码的门禁**。违反任何一条，交付就算不合格。

| 编号 | 规则 |
|---|---|
| R1 | 标题、章节、分割线只能用秀米模板库组件，不许手打符号 |
| R2 | 先定本篇主题色，再挑协调的组件；换主题就换组件，不许沿用上一篇 |
| R3 | 每段文本一个独立 cell |
| R4 | 图片按段落落位，不许全堆到文末 |
| R5 | 背景必须铺满整篇，不许只铺到文字那一块 |
| R6 | 不要素：该有的装饰要有，但不能花到看不见内容 |
| R7 | 一次登录后持续操作，用站内导航返回，不许反复重启浏览器 |
| R8 | 改旧文用 `xiumi_edit` 就地改，不许新建再复制粘贴 |
| R9 | 创作前先调 `xiumi_guide`，读完规则再动手（60 分钟内有效） |
| R10 | 创作完必须保存，并确认保存成功 |
| R11 | 创作完必须打分，把分数和修改建议给用户 |
| R12 | 遇到无法解决的问题，先截图看页面提示，不许干等 |
| R13 | 账号密码由用户在设置页填，存本机 `config.json`，不写进代码/日志 |
| R14 | 发布前跑 `scripts/privacy-check.mjs`，不许有隐私泄漏 |
| R15 | 少重复请求，一次能完成的不要拆成多次 |
| R16 | 不要冗余功能，工具数能少不多 |
| R17 | 未经用户明确同意，不做任何 GitHub 写操作 |

完整版（含每条的用户原始要求与例外条件）：[`docs/HARD_RULES.md`](./docs/HARD_RULES.md)。

## 安装

### 第一步：装 Python 依赖

需要一个带 Playwright 的 Python。推荐 3.12：

```bash
python -m venv .venv
.venv\Scripts\activate
pip install playwright==1.64.1
```

不需要 `playwright install`——插件复用系统已有的 Microsoft Edge。

### 第二步：装插件

部署到 DSH profile：

```bash
# 从 GitHub 安装
npx @deepseek-ai/dsh plugin --profile <profile> add github:alk233/xiumi-assistant

# 或本地开发（link 方式）
npx @deepseek-ai/dsh plugin --profile <profile> add "file:/path/to/xiumi-assistant"

# 验证组合配置（应看到 "- id: xiumi-assistant" 行）
npx @deepseek-ai/dsh --profile <profile> --dump-config
```

**重启 DSH 后生效。**

> ⚠️ **别用** `npx dsh plugin`——npm 上 `dsh` 这个名字早在 2016 年就被一个不相关的
> JS shell 包占用（`dsh@1.0.1`），它没暴露 CLI bin，会报
> `could not determine executable to run`。DSH 的 CLI 在 scoped 包
> `@deepseek-ai/dsh` 下，必须用完整名。

### 第三步：登录并创作

打开 DSH 设置 → **秀米助手**：

1. 填秀米账号（手机号或邮箱）和密码
2. 点「保存账号与主题」
3. 点「打开浏览器」——会弹出一个 Edge 窗口
4. 点「登录秀米」——插件自动填账号密码并提交
5. 如果秀米要求验证码，在弹出的窗口里手动完成

浏览器会一直开着，登录态在窗口里，**不保存、不复用**。关掉窗口下次要重新登录。

## 设置页用法

设置页在 DSH 的「设置 → 秀米助手」，界面上有五块：

| 区域 | 作用 |
|---|---|
| 账号密码 | 填一次就存到本机 `config.json`，下次自动填；密码可以点「显示」查看 |
| 主题选择 | 13 种版式风格 × 12 个色系，或者选「跟随主题自动判断」 |
| 主题预览 | 选中的组合实时渲染出来，不用提交就能看效果 |
| 按钮区 | 保存账号与主题 / 打开浏览器 / 登录秀米 / 退出账号 / 恢复自动 |
| 状态区 | 显示浏览器开没开、登录没登录、当前页面地址 |

点「退出账号」会**同时清空**本机 `config.json` 里的账号密码和界面上的密码框，
并给出提示。

## 主题与智能模式

13 种风格分三组：

| 组 | 风格 |
|---|---|
| 经典 | 简约、文艺、商务、活力、手账 |
| 专业 | 科技、学术、财经、教育 |
| 特色 | 国潮、杂志、复古、暗黑 |

12 个色系：朱红、墨蓝、松绿、琥珀、石板、紫罗兰、黑白、玫瑰、青碧、金色、靛青、梅子。

**自动模式**（默认）：不选具体风格时，插件按标题和正文内容判断。优先级是
①风格规则库匹配 ②关键词映射 ③兜底简约 + 朱红。返回值里带
`resolvedStyle` / `resolvedTheme` / `resolveReason`，能看出为什么选了这个。

## 模板目录缓存

秀米模板库有 7 组、90 多个分类，每次创作都重新翻一遍很浪费时间。插件把目录结构缓存下来：

| 位置 | 内容 |
|---|---|
| `data/template-catalog.json` | 随包发布的基线目录（7 组 / 92 分类） |
| `~/.xiumi-assistant/template-catalog.json` | 运行时缓存，采集成功后写入 |

读取优先级：运行时缓存（30 天内）→ 包内基线 → 现场采集。

失效条件：文件缺失、`schemaVersion` 不匹配、超过 30 天、或显式 `refresh=true`。

变化阈值：分类增删或总数变动超过 20% 才整份覆盖并报告 `changed`/`diff`，否则只刷新
`lastVerifiedAt`。缓存只存分类名和模板 id/标题/是否免费，不存账号任何信息。

## 凭据与安全

- 账号密码只保存在 `~/.xiumi-assistant/config.json`，文件权限设为仅当前用户可读写
- 密码不进源码、不进日志、不进工具返回值
- HTTP 接口只接受 `127.0.0.1` 同源请求，响应带 `no-store`
- 插件不连任何第三方服务器，所有操作都在本机浏览器里

## 已知限制

- **只支持 Windows + Microsoft Edge**。Playwright 复用系统 Edge，不下载 Chromium
- **秀米没有公开 API**，所有功能都依赖浏览器自动化。秀米前端改版可能导致插件失效
- **模板库只处理免费素材**，VIP/付费素材跳过并计数
- **不发公众号**。这个插件只做秀米侧的图文排版，发布到公众号需要另走微信公众平台

## 架构

```
DSH Agent
    │  工具调用（17 个 xiumi_* 工具）
    ▼
┌─────────────────────────────────────┐
│  index.js（Host 半部）              │  1. 校验硬性规则门禁（未读指南 → 拒绝）
│                                     │  2. 把参数序列化成 JSON 行
└─────────────────────────────────────┘
    │  stdin / stdout（长驻子进程）
    ▼
┌─────────────────────────────────────┐
│  bridge.py（Python 常驻）           │  3. Playwright 驱动可见 Edge
│                                     │  4. 写 Angular cell 模型（不是 DOM）
│                                     │  5. 保存后开独立页面读回模型比对
└─────────────────────────────────────┘
    │
    ▼
秀米编辑器（xiumi.us）
```

**Host 半部**（`index.js`）

- `inject = ['tools', 'webServer']`，注册 17 个工具和 `/__xiumi/*` 路由
- 规则门禁：`xiumi_guide action=guide` 会写入 `gate.at`，产出型工具（write/edit/compose/component）
  在 `execute` 开头检查，没读过或超过 60 分钟就直接抛错
- 长驻 Python 子进程，通过 stdin/stdout 的 JSON 行协议通信，请求带自增 id 和超时
- Python 解释器按「配置里的 pythonPath → `XIUMI_PYTHON` → 系统 python/python3」逐级探测，
  并校验 `playwright` 版本 ≥ 1.40

**Client 半部**（`client.js`）

- 手写惰性 CJS bundle（`window.__ModuleLoader__.load({id, factory})`），仅 `require('react')`
- `inject = ['slots']`，注册 `settings.section`（`id: 'xiumi'`, `order: 105`）
- 通过同源 `fetch` 调 `/__xiumi/config`、`/__xiumi/login`、`/__xiumi/open` 等接口
- 主题表在 `theme.js` 里，Host 和 Client 共用同一份定义

## 设计说明

**为什么写 Angular 模型而不是 DOM**：秀米的保存逻辑从 `scope.cell` 序列化。DOM 改动不进模型，
保存时被覆盖。这是本项目花时间最久的一个坑，`bridge.py` 里所有写入路径最终都落到
`angular.element(el).scope().cell.text = ...` 加 `$apply()`。

**为什么保存要做独立读回**：秀米保存时会做一次规范化（补空 cell、重写组件签名），
所以「页面提示保存成功」不等于「内容真的进去了」。插件在保存后另开一个页面读回完整模型，
算归一化哈希和保存前比对，不一致就返回 `saveVerified: false` 和具体原因。

**为什么每段一个 cell**：秀米的 `cell` 是排版的最小单位。所有段落塞进一个 cell 里，
样式只能整块设，做不出章节标题和正文的区别。

**为什么强制可见**：无头浏览器跑完只给你一个 `ok: true`，你不知道它到底排成了什么样。
可见窗口让整个过程可观察、可中断、可接管。

## 已验证

以下都是在真实浏览器里跑过的，不是 mock：

| 项目 | 结果 |
|---|---|
| 创建图文 + 插模板组件 + 写正文 + 保存 | ✅ 重开后内容与样式完整 |
| 保存独立读回核验 | ✅ `saveVerified: true`，模型哈希一致 |
| 就地改旧文（`xiumi_edit replace_body`） | ✅ 同一篇内组件保留、正文整篇换掉 |
| 只排版模式（`layout-only`）改一个字 | ✅ 内容指纹不一致，拒绝写入 |
| 图片按段落落位 | ✅ 串行插图，每张落在对应段落之后 |
| 密码错误 | ✅ 8 秒内识别并返回 `loginFailed`，不再死等 |
| 验证码 | ✅ 返回 `captchaRequired`，提示用户手动完成 |
| 容量守卫 | ✅ 图库/图文快满时主动提醒，不自动删 |
| 编辑测试 | ✅ `node --test` 62 pass / `pytest` 27 passed |
| 隐私扫描 | ✅ `privacy-check` PASS (0 findings, 0 warnings) |

## 从源码安装（开发）

pnpm 的 `file:` 安装是**硬链接**。用编辑器原地修改源码会**断开链接**，导致
`profiles/<profile>/node_modules/` 里的副本停在旧修订——改了代码却测试旧版本。

改完源码必须三步走：

```bash
# 1. 重装
npx @deepseek-ai/dsh plugin --profile <profile> add "file:/path/to/xiumi-assistant"

# 2. 跑测试
node --test tests/*.mjs
python -m pytest tests -q
node scripts/privacy-check.mjs

# 3. 重启 DSH 验证
```

**注意**：`bridge.py` 是长驻 Python 进程，改完后必须重启 DSH 才能生效；
`index.js` 在 Node 启动时加载，同样需要重启；只有 `client.js`（设置页 UI）刷新页面即可。

## 许可

本项目基于 [MIT 许可证](https://github.com/alk233/xiumi-assistant/blob/main/LICENSE) 发布。

```
MIT License

Copyright (c) 2026 alk233

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```

简单说：你可以自由使用、修改、分发、商用，只要保留版权声明和许可证文本；
作者不对使用后果负责。
