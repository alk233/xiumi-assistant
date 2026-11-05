# 秀米图文助手 · 使用手册

它在DSH中用可见的Edge窗口操作秀米，支持写新稿、改现稿、编排图片、免费模板与素材，以及学习外部文章的制作方法。当前源码有21个工具，包含新增的 `xiumi_guide`。修改文件或更新bundle后须重新加载插件；**当前尚未完成DSH内真实保存的端到端实测**，不能把文档更新理解为宿主已验证可用。

安装细节见 [README](../README.md)，制作方法见 [PRODUCTION_GUIDE](PRODUCTION_GUIDE.md)，积累机制见 [LEARNING](LEARNING.md)。

## 第一次使用

1. 准备系统Microsoft Edge、Python 3.10+（建议3.12）和Python Playwright。启动探测要求Playwright不低于1.40；当前固定安装命令为 `python -m pip install -U playwright==1.64.1`，必须装在插件实际使用的解释器中。默认使用系统Edge，不要求额外下载Chromium。插件不自动安装依赖。
2. 由DSH安装并启用bundle；找不到“秀米助手”设置分区时先检查插件是否加载。设置 `pythonPath` 后也要重新加载插件。
3. 点“打开浏览器”，在可见窗口扫码或输入账号密码；验证码由你自己完成。也可使用设置页“登录秀米”填写已配置凭据。
4. 设置页账号密码保存在本机用户目录 `~/.xiumi-assistant/config.json`，密码默认圆点显示；浏览器配置目录保留cookie。它们不会随源码打包，不能把配置文件发到日志或聊天。**点“退出账号”会同时清除浏览器登录态、清空该文件里的账号与密码（主题保留），设置页输入框随之变空**；只想退登录态、保留凭据时用 `xiumi_login action=logout clearCredentials=false`。
5. 首次更新后的验收先选一篇你明确指定的稿件，检查预览、显式保存与独立读回结果。当前更新未自动重载DSH，也未完成这项真实保存验证。

## 开始制作时怎么说

可以直接说明阅读目标、改动范围和保存要求，例如：

> 把现有稿排成“活动经过＋摄影图集”，文字和图片都不改。先预览，暂不保存。

> 给我写一篇新推文，按三个真实阶段展开；关键合照完整展示。排好并保存。

> 学习这个外部链接的标题和图组方法，应用到当前稿的第二节，其余组件保留。

**硬规定**：每次创作或修改前都必须先调用 `xiumi_guide action=guide` 读制作指导，未读时写入类工具会被直接拒绝，读过之后 60 分钟内有效。助手应先调用 `xiumi_guide action=guide` 获取方法，按目标选择结构，而非强制使用相同章数、字数或配色。你已明确授权的具体编辑与保存范围可以连续执行；`confirm=true` 是接口执行门禁，不意味着同一授权每一步都要重新询问。范围不明确时先核对对象和计划。

## 修改现稿：先看清范围

1. 先列出或只读查看文章，使用 `xiumi_articles action=open` 绑定要修改的稿件，再用 `xiumi_edit action=inspect` 查看文章ID、版本与单元格（cell）列表。
2. 把要改的段落、图片和组件对应到明确的cell。多格 `replace_body` 必须提供 `cellIndexes`；单格使用本次检查的 `cellIndex`。未选择的组件、页头和图片不动。
3. 写动作必须 `confirm=true`，可以带 `expectedRevision` 防止旧计划覆盖新内容。结构发生变化后重新inspect，不能沿用旧编号。不要挑“最长正文”替代整篇，也不要把其他短段当作残留删除。
4. 先预览时使用 `save=false`。运行 `xiumi_review` 并看截图，核对标题层级、文字、图片顺序、主体裁切和保留区域。
5. 需要保存时显式保存，并完成下面的独立读回验收。你说“改这篇”时应就地修改，不能新建同名文章代替。

如果你要求“只排版、一个字和一张图都不改”，可用 `xiumi_compose mode=layout-only`，先 `dryRun=true` 检查 `fidelity` 和 `firstDiff`，再按明确范围确认写入。保真检查发现差异时应停止并查原因，不能改成逐字重打来绕过检查；预检通过也不代表已保存。

## 写新稿：创建与保存分开理解

只有你明确要新文章时才新建。先把主题、结构和素材顺序形成可检查的方案；`xiumi_compose mode=create` 支持 `dryRun=true` 预检。新稿写入、模板和配图动作仍受确认与目标绑定约束。

`xiumi_write` 和 `xiumi_compose create` 保留默认保存行为；若你要“先预览”，助手必须显式 `save=false`。只允许写稿却未要求立即保存时，应先说明保存策略。后续再次调整模板或图片后，要重新检查并按授权保存。

## 保存是否真的完成

`xiumi_articles(action="save",confirm=true)` 显式保存当前稿。结果至少分两部分看：

- `saved`：保存操作是否得到页面回执。
- `saveVerified`：是否在独立新页面重新加载该文章，并对比正文模型、样式、组件及图片顺序。还应查看 `saveEvidence` 和 `saveFailureReasons`。

两者都成功且没有未解释差异，才算保存完成。出现toast、生成文章ID、标题相同或字数相同，都不足以单独证明内容保存完整。独立页面若出现本地草稿恢复提示，不能把它当服务器保存证据。

如果超时、取消或断连，结果可能未知（unknown）：助手先 `xiumi_edit inspect` 对账实际文章、版本和cell，再决定补做；不能直接重发同一写入或插图命令。`saved=true` 但 `saveVerified=false` 时，应报告“保存回执已收到，读回尚未验证”，并说明失败原因。

## 未保存稿与独立查看

导航、切稿、新建、返回列表或关闭浏览器不会隐式保存旧稿。存在未保存修改时，会返回 `unsavedChanges` / `blocked` 并保留工作稿。继续前只能按你已有授权显式保存验收，或由你明确决定丢弃后传 `discard=true`。恢复草稿提示也不会由助手擅自作恢复或丢弃决定。

想查看另一篇文章又保留当前稿时，使用带另一个URL的 `xiumi_articles action=inspect`；参考学习使用 `xiumi_guide learn` 或 `xiumi_reference learn`，它们在独立临时页面读取，完成后关闭临时页，不替换当前工作稿。`open` 则是主动切换编辑目标，仍受脏稿保护。

## 学习外部排版与自动积累

给助手公开的秀米或微信图文链接，首选 `xiumi_guide(action="learn",url=...)`。它学习可泛化的结构、字阶、间距、配色角色、图文关系、卡片和分隔方法，不保存原文、具体标题、整页HTML或图片素材。

这些操作也会自动送入学习库：`xiumi_reference action=learn`、成功的 `xiumi_articles open`，以及带另一个URL并成功读取的 `xiumi_articles inspect`。学习失败或结构不足会报告 `learningWarning` 或未学习原因，不能凭计数声称已学到。

相同文章的重复链接不重复增加证据；助手制作并识别的自有作品会排除。旧作品可用 `xiumi_guide action=mark_own` 标记，误收录的外部证据会撤回。用 `status` 看积累，`guide` 看适用条件，用 `feedback` 记录实测成功或失败；自己的产出只算实践反馈，不是新外部样例。

样式档案仍可通过 `xiumi_reference list/show/remove` 管理，`profileId` 可用于写入或compose；删除需确认。参考参数只是候选，不能保证标题可编辑、手机可读或微信兼容。纯图片长图和复杂装饰需要截图查看；横向图库要实测手势和目标平台，不支持时改成纵向图组。

## 图片、模板与容量

免费模板与素材通过对应工具选择；VIP和付费项会跳过。不能因组件看起来有字就假定可替换，要检查可填文字格及结果；不能手打装饰字符假扮图形。

本地图片先 `xiumi_upload`，再 `xiumi_image_insert`；多个图文锚点用 `xiumi_image_manifest` 返回配图对账表。上传和插入都需确认，图片作为独立图片格插入后还要检查实际顺序。`.docx` 抽图可选用 `scripts/demo_doc_workflow.py`，另需 `python-docx`。

容量不够先用 `xiumi_quota action=status` 查看，收到 `quotaBlocked` 时根据返回选项减少图片、改现稿或停止。只有你明确点名要删除的条目，才用 `xiumi_quota action=cleanup`（需 `confirm=true` 且 `items` 非空）；不能自动清理旧文章或图片腾空间。

## 常见问题

- **浏览器没有打开**：检查Edge和所配置Python中的Playwright；修改 `pythonPath` 后重新加载插件，查看启动错误。不要在另一个Python环境反复安装。
- **查看链接失败**：确认是支持的公开图文链接；登录页、网络错误或无法识别正文都应报告，保留当前稿，不用新建稿代替读取。
- **预览正确但保存未验证**：先查两个保存状态及独立对账证据，不直接改DOM、刷新覆盖当前脏稿或重复写入。
- **保真检查失败**：查看 `fidelity/firstDiff`，恢复到明确的原文和范围后重做计划；不通过删掉差异段或重打全文绕过。
- **学习后为什么没有整篇照搬**：学习库存适用方法和条件；不同题材不会强行求平均，也不搬用来源品牌和素材。

## 工具索引与限制

17个工具分为：浏览与检查 `xiumi_browser`（open/status/close/站内导航/截图）+ `xiumi_login`；文章 `xiumi_articles/xiumi_edit/xiumi_write/xiumi_compose/xiumi_review`；学习 `xiumi_guide/xiumi_reference`；模板素材 `xiumi_catalog/xiumi_template/xiumi_component/xiumi_asset`；图片与容量 `xiumi_upload/xiumi_image_insert/xiumi_image_manifest/xiumi_quota`（status/cleanup）。具体动作见 [README工具清单](../README.md)。

主要支持Windows与系统Edge；需要本机运行依赖，不自动发布到微信公众号。页面改版可能使选择器失效，届时应如实停止。交互、手机与微信端须分别实测；保存验收已做过无图与有图两轮真实端到端实测，更新代码后需重载插件。

