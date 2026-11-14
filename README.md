# vanessa-resume-tool
An intelligent resume screening tool,still under development.


#skill package include
>resume-tool-agent
  Tips:The Harness requirements for dealing with the resume,please use the harness and commanding in
       informantion rows :please get the 'resume-tool-agent' skill package,then user can use the natrual
       words to manage harness.

# Resume Tool — 单机简历筛选工具（双引擎原型）

按已确认的需求自研：**单用户本地使用，批量导入导出简历并管理，筛选模板化，可选接入多家 AI API 或纯本地关键词筛选**。exe 打包路线沿用《exe-feasibility-report.md》评估结论。

## 快速开始

```bash
pip install pypdf          # 可选：PDF 支持（缺失时优雅降级）
python make_samples.py     # 生成 8 份测试样本（含 docx/伪装doc/pdf）到 samples/
python resume_tool.py import --dir samples                 # 批量导入
python resume_tool.py screen --engine keyword --template config/template.json   # 离线筛选
python resume_tool.py screen --engine funnel   --template config/template.json  # 漏斗模式
python resume_tool.py search --q "数据 工程师"              # 正文全文搜索（AND）
python resume_tool.py export --out report.csv              # 导出报告
```

## 数据目录

可写数据（数据库、上传原文件、备份、AI 配置、筛选模板）与程序代码分离：

| 运行方式 | 默认数据目录 |
|---|---|
| 源码 `python app.py` | 项目目录（兼容现有开发用法） |
| 打包后的 exe | `%LOCALAPPDATA%\ResumeTool` |
| 自定义 | 设置环境变量 `RESUME_TOOL_DATA_DIR=D:\ResumeToolData` |

首次在 exe 模式启动时，会把 exe 同目录或源码目录中的 `resume_tool.db`、`uploads/`、`backups/`、`config/` 复制到新数据目录；**旧文件保留不删除**。设置页「数据备份与恢复」会显示当前实际数据目录。

目录导入和上传导入都会把原文件复制进 `uploads/`，数据库保存 `uploads/...` 相对路径。这样备份恢复到另一台机器后，详情页的原文件预览与打开仍然有效；外部目录导入不再依赖原文件夹。

## 三种筛选模式

| 模式 | 说明 | 成本 |
|------|------|------|
| `keyword` | 引擎A：本地关键词加权评分，完全离线 | 零 |
| `ai` | 引擎B：逐份调用大模型语义评估 | 按次计费 |
| `funnel`（推荐） | 关键词全量初筛 → AI 只复核「待核实」名单 | 成本可控 |
| `agent`（免Key） | WorkBuddy Agent 协作评估（见下节） | 消耗使用者自己 WorkBuddy 账号额度 |

引擎A评分模型：硬性门槛（必备技能/年限）一票否决 + 核心技能加权评分（证据次数 0-5 分映射，核心占 90 分池 + 加分项 10 分池），与 `resume-screener` 同一套已验证逻辑。

## Agent 协作模式（免 API Key，2026-11-14 新增）

适合分发给办公人员 + 无自备 Key 的场景：**AI 评估发生在使用者自己的 WorkBuddy 会话里，额度消耗记在本人账号**，本工具全程不接触任何密钥。

```bash
# 1) keyword 初筛（或直接 --all 全量导出）
python resume_tool.py screen --engine keyword --template config/template.json

# 2) 导出「待核实」批次（文本已脱敏：姓名/手机/邮箱打码）
python resume_tool.py agent-export --template config/template.json --out agent-batch.json

# 3) 在 WorkBuddy 里对 Agent 说：读 agent-batch.json，按其中 instructions
#    逐份评估，把结果 JSON 数组写入 agent-results.json

# 4) 写回简历库（engine 记为 ai，detail 含 provider=workbuddy-agent 溯源）
python resume_tool.py agent-import --in agent-results.json --template config/template.json
```

- 批次文件自带评估说明、筛选标准与输出 JSON 格式（与引擎B输出同构），Agent 照做即可
- 结果写回后 Web UI 报告页 / CSV / xlsx 导出**零改动**即可显示
- `agent-export --all` 全量导出、`--ids 1,3,5` 指定导出；默认只导 keyword 初筛为「待核实」的名单（即 funnel 第 2 阶段的人工替代）
- 该模式与云服务无关：无需发布、无需 Origin 校验，天然适配未来 exe 打包路线
- 隐私提示：脱敏只覆盖姓名/手机/邮箱三类，工作经历等其余内容仍会进入会话上下文
- **配套 Skill**：`dist/resume-tool-agent.zip` —— 安装到办公人员的 WorkBuddy（用户级技能）后，一句「帮我筛选简历」即可触发全流程；源码见 `~/.workbuddy/skills/resume-tool-agent/SKILL.md`

## 格式支持（doc/pdf 为主，按你的确认项实现）

| 格式 | 解析方式 | 依赖 |
|------|---------|------|
| .docx | 标准库 zipfile + XML 解析 | 无 |
| .doc（伪） | 智能检测：市面 .doc 常见实为 docx/RTF/HTML/纯文本，逐一探测 | 无 |
| .doc（真二进制） | Word COM 转换 docx 后解析 | pywin32 + 本机安装 Word（仅 Windows） |
| .pdf | pypdf 文本抽取 | `pip install pypdf`（缺失跳过并告警） |
| .txt / .md | 多编码尝试（utf-8/gb18030） | 无 |

导入按文件内容 sha256 去重，重复导入自动跳过。

## AI 多厂商接入（OpenAI 兼容协议）

编辑 `config/providers.json`：

```json
{
  "active": "deepseek",
  "providers": {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "api_key": "你的Key", "model": "deepseek-chat"},
    "custom":   {"label": "自定义",   "base_url": "https://your-gateway.example.com/v1", "api_key": "...", "model": "..."}
  }
}
```

- 预置 DeepSeek / Moonshot Kimi / 自定义三档；**任何兼容 OpenAI chat/completions 协议的服务均可接入**（含自建网关、公司代理）
- 切换厂商 = 改 `active` 字段；`python resume_tool.py providers` 查看就绪状态
- Key 只存本地 JSON，**不进代码库、不打包进 exe**（沿用可行性报告的安全要求）
- base_url/model 以各厂商官方文档为准

## 版本更新与分发（GitHub Release 驱动）

客户端固定查询 `Vanessa0712/vanessa-resume-tool` 的[最新正式 Release](https://github.com/Vanessa0712/vanessa-resume-tool/releases/latest)。默认每次运行 `python app.py` 时在后台自检一次，不阻塞 Web 服务；「设置 → 版本更新」展示这次检查的结果，点击「检查更新」可重新检查。反复打开设置页只读取本地状态，不会重复请求 GitHub。

发现新版本后展示 Release 标题、说明和发布页链接。当前为**检查 + 手动下载**的半自动更新，不会静默下载、覆盖安装或执行安装包。

维护者推送一次更新的步骤：

1. 在 `app.py` 的 `CHANGELOG` 顶部添加新版本记录（第一条就是客户端当前版本），同步更新 `update-manifest.json` 作为版本说明模板
2. 提交代码，并创建与版本号一致的 Git 标签，例如 `ver1.3.0`
3. 在 GitHub 仓库新建 Release，选择该标签，填写标题和更新说明；如有 exe / zip 安装包，上传到 Release Assets
4. 以**正式 Release** 发布，不要勾选 Draft 或 Pre-release

客户端调用 `https://api.github.com/repos/Vanessa0712/vanessa-resume-tool/releases/latest`：该接口只返回最新正式 Release，不包含草稿和预发布版本；尚无正式 Release 时会明确提示 404 原因，不会误报“已是最新”。可在通用设置中关闭启动自检，手动检查不受影响。

升级前备份 `resume_tool.db` 与 `config/`；后续 exe 数据目录外移后，安装包更新不覆盖简历数据。

## 数据备份与恢复

「设置 → 数据备份与恢复」提供企业日常使用的安全网：

- 手动备份：SQLite 一致性快照 + AI 配置 + 筛选模板打包为 ZIP；可勾选“同时打包原始简历文件”
- 每日自动备份：默认开启，每天首次启动生成一份数据库+配置备份；自动备份保留 7 份，恢复前备份保留 3 份，手动备份保留 20 份
- 下载与恢复：设置页列出备份文件，可直接下载；上传 `.zip` 或 `.db` 恢复，恢复前必定先生成 `pre-restore` 备份
- 恢复校验：检查 SQLite 完整性、必需表、备份大小与上传路径；有筛选任务运行时拒绝恢复

备份文件**未加密**，内含候选人个人信息和 AI Key，应存放在公司受控目录或加密磁盘，不要用公共聊天工具传输。`backups/` 已加入 `.gitignore`。

## 隐私清理、日志与诊断

- 彻底删除：回收站里执行「彻底删除」或「清空回收站」时，数据目录 `uploads/` 内的原始文件同步删除；外部目录导入的历史文件不会被擅自删除，只在提示中说明数量
- 孤立文件清理：「设置 → 运行诊断与存储清理」统计 `uploads/` 中未被任何简历引用的文件（重复导入留下的副本、失败上传残留），可一键清理并释放空间
- 运行日志：写入数据目录 `logs/resume-tool.log`，单文件 2 MB 滚动保留 5 份；打包成无控制台 exe 后仍可回溯现场
- 错误编号：页面出现 500 时显示 8 位错误编号，并在日志中以同一编号记录堆栈
- 诊断包：「下载诊断包」导出 ZIP，含环境信息、数据规模统计、脱敏后的 AI 配置（只保留是否已配置 Key）和最近 3 份日志；**不含简历正文、候选人个人信息、API Key 或数据库文件**

## 筛选模板

`config/template.json`：`hard_gates`（必备技能 + 最低年限）+ `core_skills`（权重 + 别名）+ `nice_to_have`。换岗位 = 复制一份改技能表，`--template` 指定即可。多模板可并存。

## 已验证（本机实测）

- 关键回归：`python -m unittest discover -s tests -v`（26 项）覆盖导出 ID 去重、CSV 口径、配置损坏降级、模板路径穿越、跨站写请求、后台任务异常、数据库连接释放、备份/恢复、全新目录恢复、数据目录迁移、相对路径、导入批次、彻底删除清理原文件、孤立文件清理、诊断包脱敏与筛选模板记忆
- 7 份样本覆盖全部格式路径（txt / 真docx / docx伪装.doc / HTML伪装.doc / RTF伪装.doc / 手工PDF）全部正确解析
- 关键词引擎：91.7 分资深后端通过、0 分缺必备技能未通过，门槛与评分逻辑正确
- AI 未配置时给出可操作指引（不崩溃）；funnel 在 AI 缺失时自动降级为"人工复核提示"
- 重复导入按内容哈希 100% 去重；CSV 导出 utf-8-sig（Excel 直开不乱码）
- **Agent 协作模式全链路（2026-11-14）**：8 份简历 keyword 初筛 → agent-export（PII 打码生效）→ Agent 会话内评估 → agent-import 写回 8 条（engine=ai + provider 溯源）→ Web UI /screen 与 CSV 报告均正确显示

## Web UI（Beta ver1.2.3）

```bash
python app.py        # → http://127.0.0.1:5000
```

| 页面 | 能力 |
|------|------|
| 简历库 `/` | 目录导入 / 拖拽上传（原文件复制进数据目录）、**导入批次记录与逐文件失败原因**、统计卡片、**主列表分页（每页 15 条，底部翻页 + 页码跳转）**、**正文全文搜索**（`?q=数据 工程师`，FTS5 + 中文按字切分，带命中片段）、**按模板查看**（`?template=<slug>`，统计与状态只算该模板）、多选批量删除（确认弹窗 + 今日不再提示）、导出 CSV |
| 导入记录 `/imports` | 批次来源、总数、成功/重复/失败统计；批次详情逐文件展示新增、恢复、重复、无法解析和异常原因 |
| 候选人 `/persons` | 同人多版本自动归并（邮箱 → 手机 → 姓名），显示最新版本与版本数；`/person/<id>` 查看该人全部版本并可对比 |
| 回收站 `/trash` | 删除即入回收站，支持单条/批量恢复、彻底删除、清空；重新导入同内容文件会自动恢复原记录 |
| 筛选中心 `/screen` | **模板可搜索多选**（输入即过滤、chip 展示、★收藏置顶、最近使用、AI 调用量预估、键盘操作）；**选择自动记住并在下次启动恢复**（存 `config/app_settings.json` 的 `screen_templates`，模板删除后自动失效）；引擎三选一；简历范围 = 全部 / 仅未筛选（按所选模板）/ 指定简历（可搜索文件名或 ID、按状态过滤、全选）；**筛选在后台线程执行**，页面轮询进度条（`/screen/job/<id>`），完成卡片保留结果摘要；结果按模板分组 |
| 图表视图 `/charts` | 状态分布环形图、分数分布、各模板通过率、技能命中热度、导入趋势（服务端计算 + CSS/SVG 自绘，无外部图表库） |
| 模板管理 `/templates` | 新建、可视化编辑（含所属部门 / 子职业，技能行增删 / 权重 / 别名 / 必备勾选，实时权重合计）、★收藏、JSON 源码模式、批量导入（逐个校验、同名自动改名）、单个导出 JSON / 全部导出 ZIP |
| 简历详情 `/resume/<id>` | 解析文本预览 → 筛选历史（每页 10 条，底部翻页 + 页码跳转）→ 原文件预览（PDF/文本内嵌，doc/docx 引导本机打开）、打开方式可选：系统默认 / WPS / Word / 资源管理器定位 / 云文档（复制路径 + 打开网盘） |
| 导出 | `GET /export-xlsx`：候选人报告（14 列，表头冻结 + 自动筛选 + 汇总页，支持 q/template/ids 过滤，ID 自动去重）；`/export` 导出 CSV 并沿用相同的搜索/模板口径，空结果给中文提示 |
| 更新日志 `/changelog` | 侧边栏入口（未读显示 NEW），历史公告按版本卡片展示，可展开/收起 |
| 设置 `/settings` | 启动自检 + GitHub 最新正式 Release 比对 + 更新说明与发布页入口、通用设置（打开方式/云文档/启动自动检查/每日自动备份）、备份列表与恢复、孤立文件清理与诊断包导出、多厂商 Key/BaseURL/模型配置（留空不覆盖已存 Key） |
| 筛选中心 `/screen` | 提交 AI/漏斗任务前弹出调用量确认；后台任务可离开页面继续运行，失败或失效有明确提示 |

其他：

- 进入页面弹更新公告（`app.py` 的 `CHANGELOG`），同会话只弹一次；勾选「今日不再显示」记入 localStorage，按日期自动恢复
- 模板改动即时生效（`TEMPLATES_AUTO_RELOAD`），UI 迭代无需重启
- 全部前端资源本地自研，零 CDN，离线可用

## 视觉主题（A 阶段）

暖纸风格：`--bg #FAF9F5`、主色珊瑚 `#D97757`、卡片白底 16px 圆角。样式分两层：

| 文件 | 作用 |
|------|------|
| `static/style.css` | 结构与组件基础样式（不改动） |
| `static/theme.css` | 主题覆盖层，加载在 style.css 之后；改配色/圆角/字体只动这里 |
| `static/fonts/*.woff2` | 内嵌 Noto Serif SC 子集（GB2312 全字集 + 界面文案用字），无需本机安装字体 |

字体重建：`pip install fonttools brotli && python tools/build_fonts.py`（自动下载 → 子集化 → 校验汉字覆盖）。
字体为 SIL OFL 1.1，见 `static/fonts/LICENSE.txt`。

## 已知边界

- 真二进制 .doc 依赖最终用户机器装有 Word（未装则跳过并告警，不影响其他简历）
- 扫描版 PDF（图片型）无法抽取文本，需 OCR（未实现，如需要单独评估）
- 关键词引擎对「换表述」简历有漏判可能——这正是 funnel 模式让 AI 复核灰色地带的原因
- AI 引擎的 JSON 输出做了防御式解析，极端情况下单份失败不中断批次
- 「打开方式」走本机程序调用，仅对 127.0.0.1 本地访问生效；云文档只复制路径，不自动上传
- 候选人归并依赖简历中可识别的姓名/手机/邮箱：都没有时按文件名兜底（不会与其他人合并），同名同姓可能被误并
- 导出 xlsx 依赖 `openpyxl`（缺失时页面会提示安装命令，其余功能不受影响）
- 全文搜索走 SQLite FTS5：中文按字切分 + 短语匹配（`"数 据"`），英文按词；不支持同义词与拼音
- FTS5 不可用的环境（极少）会自动降级为 LIKE 全表匹配，功能可用但大库较慢
- 「全部模板」视图取每份简历「最新一次」筛选结果（跨模板），要按模板口径统计请用下拉切换
- 回收站只做逻辑删除：筛选记录保留，磁盘原文件始终保留；彻底删除后不可恢复
- 后台任务状态存于进程内存，重启服务后历史任务进度不可查（数据本身已落库)
