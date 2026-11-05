# 秀米图文助手

## 功能描述

一个跑在 DSH 里的秀米排版插件。它打开一个可见的 Edge 窗口，在真实页面上完成新建、编辑、插图、套模板、保存这一整套操作。秀米没有公开 API，所以插件走的是浏览器自动化，每一步都在你眼前的窗口里发生。

浏览器可见是刻意的设计。无头模式跑完会告诉你成功，但你没法确认它排成了什么样；窗口开着，你随时能接手，也能在验证码弹出来的时候自己处理。

写入走秀米自己的 Angular cell 模型，不碰 DOM。因为改 DOM 的结果是一保存就还原，而写模型才会真正存下来。保存之后插件会另开一个页面重新加载文章，把正文、样式、组件、图片顺序逐项比对，不只看那条"保存成功"的提示。

目前有 18 个工具，覆盖浏览器控制、登录、文章管理、就地编辑、组件与模板、图片上传与插入、评分、容量查询。

## 工作原理

```
DSH Agent
   │  工具调用（18 个 xiumi_* 工具）
   ▼
┌──────────────────────────────┐
│  index.js（Host 侧）          │  1. 校验硬性规则门禁（未读指南 → 拒绝）
│                              │  2. 把参数序列化成 JSON 行
└──────────┬───────────────────┘
           │ stdin / stdout
           ▼
┌──────────────────────────────┐
│  bridge.py（Python 常驻）      │  3. Playwright 驱动可见 Edge
│                              │  4. 写 Angular cell 模型（不是 DOM）
│                              │  5. 保存后开独立页面读回模型比对
└──────────┬───────────────────┘
           │
           ▼
    秀米编辑器（xiumi.us）
```

### 三个必须知道的事实

秀米正文存在 Angular cell 模型里，不在 DOM：

- 改 DOM 能立刻看到效果，一保存就全丢
- 写 `scope.cell.text`（带内联样式）才会真正落库
- `div` / `span` 画的块级占位会被丢弃，占位只能用图库里的真实图片

## 18 个工具

| 工具 | 能力 |
|---|---|
| `xiumi_guide` | 入口。返回制作指导 + 17 条硬性规则；未读它时所有创作工具直接拒绝 |
| `xiumi_browser` | `open/status/close/back/home/editor/list/screenshot` 管理可见浏览器与站内导航 |
| `xiumi_login` | `check/wait/fill` 登录；识别密码错误与验证码并立即返回 |
| `xiumi_articles` | `list/inspect/new/open/update/save` 管理文章 |
| `xiumi_edit` | `inspect/replace_body/set_cell/clear_cell/append_cell` 就地编辑现稿 |
| `xiumi_delete_cells` | 批量删除正文里的图片单元格 |
| `xiumi_write` | 写标题与正文，每段独立 cell |
| `xiumi_compose` | `create` 从零创作；`layout-only` 只排版不改字 |
| `xiumi_review` | 按层次/一致性/间距/装饰/可读性打分（100 分制） |
| `xiumi_template` | `list/search/apply` 模板库（仅免费） |
| `xiumi_component` | 插入模板库组件并替换占位文字 |
| `xiumi_asset` | `categories/list/insert` 素材库 |
| `xiumi_upload` | 上传本地图片到「我的图库」 |
| `xiumi_image_insert` | 按锚点文字插图 |
| `xiumi_image_manifest` | 批量插图（图 + 锚点数组） |
| `xiumi_catalog` | 模板目录缓存（避免重复采集） |
| `xiumi_quota` | `status/cleanup` 账号容量（图文 30 篇、图库 100 张） |
| `xiumi_reference` | `learn/list/show/remove` 学习别人推文的排版规则 |

## 硬性规则（R1–R17）

这些规则写进了代码，不只是写在文档里。违反任何一条都算交付不合格。

| # | 规则 |
|---|---|
| R1 | 标题、章节标题、分割线只用秀米模板库的真实组件，不许手打文字样式顶替 |
| R2 | 组件配色必须与推文主题一致；换主题就换组件，不沿用上一篇 |
| R3 | 每一段文本一个独立 cell |
| R4 | 图片按段落位置落位，不许全堆文末 |
| R5 | 背景必须正确，不能留白一片 |
| R6 | 不要"很素"的排版 |
| R7 | 不频繁重启浏览器，用站内导航 |
| R8 | 改旧文就地改，不新建复制粘贴 |
| R9 | 创作前先读指南（`xiumi_guide`） |
| R10 | 创作完必须保存并确认 |
| R11 | 创作完必须打分 |
| R12 | 遇问题截图看提示，不要瞎猜 |
| R13 | 账号密码只在本机用户目录，不进源码、不进日志 |
| R14 | 发布前隐私脱敏 |
| R15 | 少重复请求，省成本 |
| R16 | 不加冗余功能 |
| R17 | 未经同意不碰 GitHub |

完整版带出处见 [`docs/HARD_RULES.md`](docs/HARD_RULES.md)。

## 快速上手

### 第一步：装依赖

需要一个装了 Playwright 的 Python：

```powershell
python -m pip install -U playwright==1.64.1
python -c "from importlib.metadata import version; print(version('playwright'))"
```

默认用系统 Edge（`browserChannel=msedge`），不需要额外下载 Chromium。

### 第二步：装插件

通过 DSH 插件管理入口安装，或在 profile 里指向本目录。装好后要重新加载插件。改磁盘文件不会热更新，`index.js` 在 Node 启动时就进内存了，`bridge.py` 是长驻进程。设置页里应该出现「秀米助手」。

### 第三步：登录并创作

1. 设置页填账号密码，点「打开浏览器」，扫码或点「登录秀米」
2. 让 Agent 先调 `xiumi_guide action=guide`，不调会被拒绝
3. 开始创作

## 设置页用法

DSH 设置里进入「秀米助手」：

| 项 | 说明 |
|---|---|
| 账号 / 密码 | 填秀米账号。密码输入框可点「显示」查看 |
| 保存账号与主题 | 写入本机配置，同时保存当前主题选择 |
| 打开浏览器 | 启动可见 Edge；已启动则聚焦 |
| 登录秀米 | 用已保存的账号密码自动登录；验证码需手动完成 |
| 退出账号 | 清登录态，同时清空本机保存的账号密码与输入框 |
| 恢复自动 | 主题选择退回 `auto` |

## 主题与智能模式

内置 13 种版式风格与 12 套配色，可在设置页选，也可以让 Agent 按内容自动匹配（`auto`）：

- 风格：简约 / 文艺 / 商务 / 活力 / 手账 / 科技 / 学术 / 财经 / 教育 / 国潮 / 杂志 / 复古 / 暗黑
- 配色：朱红 / 墨蓝 / 森绿 / 琥珀 / 石板 / 紫罗兰 / 黑白 / 玫瑰 / 青碧 / 鎏金 / 靛蓝 / 梅子

`auto` 的解析顺序是风格规则库匹配、关键词映射、最后兜底到简约 + 朱红。返回值里带 `resolveReason`，说明这次为什么选它。

主题决定的是正文与配色基调。标题、章节、分割线用哪个模板库组件，按 R1/R2 从库里找，不拿上一套凑。

## 凭据与安全

- 账号密码只保存在本机用户目录下的配置文件里，路径由 `config.js` 决定，不写死在文档里
- 密码不进源码、不进日志、不进工具返回值；`/__xiumi/config` 默认只返回 `hasPassword` 与长度
- 浏览器用独立的本地配置目录，登录态只存在那个目录里，不加载外部导出的 cookie
- 插件不与任何第三方服务器通信，所有请求都发往秀米官网
- 发布前可运行 `node scripts/privacy-check.mjs` 自检

## 已知限制

- 仅支持 Windows + Microsoft Edge
- 卡片圆角无法实现，秀米的样式白名单里没有 `borderRadius`
- 页面纸张背景是 base64 图片，只能改 section 层背景
- 秀米页面结构变更时，选择器需要同步维护

## 目录结构

```
xiumi-assistant/
├── index.js          # DSH Host 侧：工具定义 + 门禁 + HTTP 路由
├── client.js         # 设置页 UI（React，跑在 DSH 页面里）
├── bridge.py         # Playwright 桥接（Angular 模型读写）
├── safety.py         # 保存验收（模型快照 / 摘要 / 读回比对）
├── observations.py   # 样式观察与规则提取
├── learning.js       # 学习库（规则沉淀）
├── theme.js          # 13 风格 + 12 配色
├── config.js         # 配置读写
├── docs/             # HARD_RULES / PRODUCTION_GUIDE / USAGE / LEARNING / TYPESETTING
├── data/             # 模板目录缓存、学习种子
└── scripts/          # 抽图、锚点匹配、隐私检查等独立脚本
```

## 从源码安装（开发）

```bash
cd xiumi-assistant
npm pack                    # 生成 tarball
# 或直接在 DSH profile 里 link 本目录
```

改代码后必须重新加载插件。`index.js` 被 Node 启动时加载进内存，`bridge.py` 是长驻进程，都不会自动重载。

## 许可证

MIT
