# 图文制作学习库

学习库把已观察的外部排版方法转成Agent可执行的制作步骤，保留适用条件、事实、推断和验证限制，不收录文章内容。当前源码有21个工具；用户入口为新增的 `xiumi_guide`，实际制作规范见 [PRODUCTION_GUIDE.md](PRODUCTION_GUIDE.md)。更新bundle后必须重新加载DSH插件。**当前尚未完成DSH真实保存端到端实测**，学习库测试不能证明宿主保存和发布端兼容性。

## 工具入口

- **硬规定**：每次创作或修改前先 `xiumi_guide(action="guide",intent="本次阅读目标、题材和限制")`；未读时写入类工具直接拒绝，60 分钟内有效。返回制作指南 `productionGuide`、可选规则、执行步骤和核验清单。按任务选择并列主题、过程叙事、图集或阶段推进，不整套套用一个来源。
- `xiumi_guide(action="learn",url=...)`：独立读取一个公开样例，积累通用方法并返回相关指引；不保存或修改源图文。
- `xiumi_guide(action="status")`：查看规则数、去重来源数、种子/运行时来源、自有作品和反馈计数。
- `xiumi_guide(action="feedback",ruleIds=[...],outcome="success")`：记录已实际验证的使用结果；失败用 `failure`。反馈不是新的外部证据。
- `xiumi_guide(action="mark_own",articleId=...)` 或提供 `url`：标记旧的自有作品，撤回此前误收录的外部证据，防止自我强化。

`xiumi_reference learn/list/show/remove` 继续管理参考参数档案；`profileId` 可传给写入或compose。删除档案需 `confirm=true`。这些档案与通用学习库是两个数据层，不能用档案数量代替独立外部来源数。

## 自动积累何时发生

以下已完成的读取会送入 `LearningLibrary.observe`：

1. `xiumi_guide learn` 与 `xiumi_reference action=learn`；前者内部复用参考读取，不把同一次读取重复计数。
2. `xiumi_reference learn` 学习单个来源；`xiumi_guide learn` 在学的同时沉淀可泛化方法。
3. 成功的 `xiumi_articles action=open`：绑定现稿时观察可提取结构。已识别为自有稿件的文章会被排除。
4. `xiumi_articles action=inspect` 提供另一个URL并在独立页面成功读取时；无URL的当前稿检查、`xiumi_edit inspect`、档案list/show及截图，不自动算新来源。

参考学习、study和独立inspect使用临时页面，完成后关闭，不替换当前工作稿。open是明确切换编辑目标，受未保存保护。读取成功但学习不足可返回 `learningWarning`；库也可能返回 `learned=false` 与 `duplicate-source/own-article/insufficient-structural-evidence` 等原因。界面已读到不等于已学到有效方法，不应把学习警告与编辑或保存结果混为一项。

相同文章的预览、编辑及重定向域名按身份去重；刷新同一来源不会增加独立证据。自己的新建/创作稿件在识别文章ID后记录为自有作品，旧作品可显式标记。自己的产出只记录实践成败，不能再learn成新的外部证明。

## 数据与内容边界

运行时库位于 `~/.xiumi-assistant/learning/library.json`。参考参数档案通常位于 `~/.xiumi-assistant/style-profiles/`，不可写时以工具报告的回退位置为准。源码中的种子只读，用户积累写入用户目录，更新插件不需要复制整套文章内容。

只接受公开http(s)的 `xiumi.us`、`xiumius.cn` 及子域，以及 `mp.weixin.qq.com`，不接受自定义端口。凭据和查询参数不落盘；秀米数字文章ID用于跨 `v/a/d/r` 预览域名去重与自有识别。微信query-only链接提取受限身份或不可逆标识，不存原查询和公众号标识。此去重不声称能识别跨平台转载。

观察严格逐字段处理，只保存有限字号/行高/间距、经验证颜色、对齐/线型枚举、图片装饰数量及受限 `sequence.kind/textLength/imageCount/level/style`。未知字段丢弃，包括原文、标题文字、作者、HTML、图片URL、原始samples和notes；不保存图片资产或整页截图。

DOM邻接不证明语义相关，字符数不证明段落表达完整，纯图片和长图内部层级无法从样式确认。不同风格条件与数值档位分别保留，不跨文章强制平均。`observed` 是观察，`inferred` 是用途推断，制作建议还要按当前稿判断；置信度不等于用户偏好或视觉验证。B批次第三来源有模板残留，只作条件性技法。

## 学习之后怎样安全制作

先guide，再打开目标并 `xiumi_edit inspect` 获得 `articleId/revision/cells`。所有编辑写动作需 `confirm=true`，多格 `replace_body` 明确 `cellIndexes`，未选组件不动；可带 `expectedRevision`，结构变化后重新inspect。不以最长正文替代全文，也不清除未选短段或装饰。

`save=false` 是未保存预览；导航、切稿、新建与关闭不会隐式保存脏稿。需要离开时显式保存确认或按用户明确决定 `discard=true`。保存必须分别检查 `saved` 与 `saveVerified`；独立新页面重新读回正文模型、样式、组件及图片顺序，不只核对标题/字数，不把toast或文章ID当完整证据。本地草稿恢复状态不能当服务器保存证据。

超时、取消或断连后结果可能未知（unknown），先inspect对账而非直接重试。制作后运行 `xiumi_review` 并查看截图；手机、微信清洗后显示、横向图库和动效要分别实测。只有已验证的效果才用feedback反馈成功或失败，不能靠自产文章数量提高外部证据强度。

## JavaScript模块接口

`learning.js` 是无第三方依赖的Node ESM模块，提供同步 `LearningLibrary`。构造及只读调用不创建用户目录。浏览器读取仍需要Python Playwright及系统Edge；运行时安装命令与解释器配置见 [README](../README.md)，库本身不自动安装依赖。

```js
import { LearningLibrary } from './learning.js'

const library = new LearningLibrary()
const learned = library.observe({ url, profile, observation, origin: 'external' })
const guidance = library.guide({ intent: '步骤与配图清楚对应', categories: ['imagery'], limit: 8 })
library.rememberOwn({ articleId, url: savedArticleUrl })
library.feedback({ ruleIds: learned.ruleIds, outcome: 'success' })
```

- `new LearningLibrary({directory?,seedDirectory?,clock?})`：可用临时目录测试；种子默认相邻 `data/`，传null禁用；clock可注入时间。
- `observe({url,observation,profile,techniques?,origin='external'})`：返回学习状态、原因、来源ID、规则ID、修订等。自己的作品、结构不足和重复证据不增加来源。任意自由文本和规则对象不会直接成为规则。
- `guide({intent='',categories=[],limit=12})`：返回 `steps/rules/sourceCount/matchedSourceCount/limitations/verificationChecklist/revision`。steps含 `ruleId/action/whenToUse/pitfalls`；工具层额外附带制作指南文字。
- `status()`：路径、版本、修订、更新时间、种子与动态来源/规则/自有作品/反馈计数及限制。
- `list()`：来源、规则、独立来源证据、适用差异与反馈；不通过Host暴露任意原始文章内容。
- `rememberOwn({articleId?,url?})` / `isOwn(...)`：登记或判断自有作品；登记至少提供一项，跨进程保留并撤回误收证据。
- `feedback({ruleIds,outcome,note?})`：只调整实践反馈；note不持久化，未知规则ID单独报告。

## 种子与持久化

读取 `data/learning-seeds.json`、`learning-seeds-a.json`、`learning-seeds-b.json`；缺失允许，损坏或错误顶层结构明确报错。接受规则数组或 `{sources:[],rules:[]}`。指导字段为 `id/category/technique/whenToUse/howToApply/pitfalls/sourceUrls/evidence/confidence`，只采纳白名单。HTML、URL、脚本协议混入指导文字会被丢弃；来源URL只用于受限身份关联。

通过合法来源关联独立证据；evidence只保留受限ID/类型及数值和颜色测量，不透传原观察文字、选择器或任意字段。无合法文章来源的抽象规则可检索，但来源数为零且降低置信度。中文英文类别归一；同类technique合并时差异留在variants。种子不复制到用户文件，对种子的反馈单独持久化。

修改库前获取 `library.lock`、重读磁盘，以唯一临时文件刷盘和原子重命名写入，不以旧内存覆盖新数据。损坏或不支持的存储保留原文件并报错；锁冲突明确提示。异常遗留锁只在确认进程结束后处理，不自动删除未知锁。

部分或截断采样保留 `incomplete/truncated` 标记，在规则证据中列为 `partialSourceIds` 并降低置信度；完整重新采样后可以更新。自动观察始终标为未经视觉核验，不把读取成功等同于设计质量已验证。

Agent指引只返回方法和精简证据，保留完整来源计数，不重复附带每篇的全部观察序列，避免学习库增长后挤占对话上下文。这是规则与实践反馈的持续积累，不是模型参数训练；新颖语义技巧、图片内部排版和视觉质量仍需Agent结合页面判断。

针对性测试命令为 `node --test tests/learning.test.mjs`，只使用临时目录；它不安装、启动或重载DSH，也不构成真实保存实测。运行宿主更新后仍须重新加载，并在指定稿件完成保存独立读回、截图和目标平台验收。

