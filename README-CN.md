# vanessa-resume-tool

一款智能简历筛选工具，仍在开发中。

> 中文说明：命令、路径、文件名、JSON 键名与示例数据值均保留原文；界面标签已译为中文，当前软件实际界面语言为中文。

# 随附技能包

> `resume-tool-agent`
>
> 提示：若要通过 WorkBuddy 处理简历，请先安装 `resume-tool-agent` 技能包。加载后即可用自然语言指令驱动整套流程。

# Resume Tool —— 本地简历筛选工具（双引擎原型）

按已确认的需求自主开发：**单人、纯本地使用；简历批量导入/导出与管理；模板驱动筛选；可选接入多个 AI API，或完全离线的关键词引擎**。exe 打包方案遵循 `exe-feasibility-report.md` 中的结论。

## 快速上手

```bash
pip install pypdf          # 可选：PDF 支持（缺失时优雅降级）
python make_samples.py     # 生成 8 个测试样例（含 docx / 伪装的 .doc / pdf）到 samples/
python resume_tool.py import --dir samples                 # 批量导入
python resume_tool.py screen --engine keyword --template config/template.json   # 离线筛选
python resume_tool.py screen --engine funnel   --template config/template.json  # 漏斗模式
python resume_tool.py search --q "数据 工程师"              # 正文全文检索（AND 逻辑）
python resume_tool.py export --out report.csv              # 导出报告
```

> 搜索词等示例值保留原文，因为随附的测试简历为中文。

## 数据目录

可写数据（数据库、上传原件、备份、AI 配置、筛选模板）与程序代码分离存放：

| 运行方式 | 默认数据目录 |
|---|---|
| 源码运行：`python app.py` | 项目目录（兼容当前开发用法） |
| 打包后的 exe | `%LOCALAPPDATA%\ResumeTool` |
| 自定义 | 设置环境变量 `RESUME_TOOL_DATA_DIR=D:\ResumeToolData` |

exe 首次启动时，会把 `resume_tool.db`、`uploads/`、`backups/` 和 `config/` 从 exe 自身所在目录（或源码目录）复制到新数据目录；**已有文件保留，不会删除**。设置页的"数据备份与恢复"区域会显示当前实际使用的数据目录。

目录导入和上传导入都会把原件复制到 `uploads/`，数据库存的是相对路径 `uploads/...`。因此即使在另一台机器上恢复备份，详情页预览和打开原件依然可用；目录导入也不再依赖源文件夹。

命令行工具（`resume_tool.py`）按与 Web 应用相同的规则解析数据目录，因此 `agent-import` 写回的是与界面读取的同一个 `resume_tool.db`。需要时可用同一环境变量 `RESUME_TOOL_DATA_DIR` 覆盖。

## 三种筛选模式

| 模式 | 说明 | 成本 |
|------|------|------|
| `keyword` | 引擎 A：本地加权关键词评分，完全离线 | 零 |
| `ai` | 引擎 B：逐份简历通过 LLM 调用做语义评估 | 按调用付费 |
| `funnel`（推荐） | 关键词对全部简历预筛 → AI 只复核"需复核"名单 | 成本可预测 |
| `agent`（无需 API Key） | 通过 WorkBuddy Agent 协作评估（见下文） | 消耗操作者自己的 WorkBuddy 账号额度 |

引擎 A 评分模型：硬性门槛（必备技能 / 年限）一票否决，另有加权核心技能评分（证据出现次数映射为 0–5 分；核心技能取自 90 分池，加分项取自 10 分池）。这与 `resume-screener` 采用的已验证逻辑一致。

## Agent 协作模式（无需 API Key，2026-11-14 新增）

面向没有自己 API Key 的办公室同事分发使用：**AI 评估在操作者自己的 WorkBuddy 会话中完成，额度计入其本人账号**。工具本身不接触任何凭证。

```bash
# 1) 关键词预筛（或用 --all 导出全部）
python resume_tool.py screen --engine keyword --template config/template.json

# 2) 导出"需复核"批次（文本已脱敏：姓名 / 电话 / 邮箱 / 身份证号 / 银行卡号打码）
python resume_tool.py agent-export --template config/template.json --out agent-batch.json

# 3) 在 WorkBuddy 中告诉 Agent：读取 agent-batch.json，按其中说明
#    逐份评估简历，并将结果 JSON 数组写入 agent-results.json

# 4) 写回结果（引擎记为 ai；detail 带 provider=workbuddy-agent 以便追溯）
python resume_tool.py agent-import --in agent-results.json --template config/template.json
```

- 批次文件自带评估说明、筛选标准和输出 JSON Schema（与引擎 B 输出同构），Agent 照做即可
- 写回之后，Web 界面报告页 / CSV / xlsx 导出**无需改任何代码**即可展示结果
- `agent-export --all` 导出全部；`--ids 1,3,5` 导出指定记录；默认只导出关键词预筛标记为"需复核"的简历（相当于人力替代漏斗第二阶段）
- 此模式与云服务无关：无需部署、无需 Origin 校验；天然适配既定的 exe 分发路径
- 隐私说明：脱敏覆盖姓名 / 电话 / 邮箱 / 身份证号 / 银行卡号，能收窄而非完全匿名化暴露面——姓名只按提取器识别出的形式打码，拼音、英文名或昵称可能残留；工作经历及其余内容仍会进入会话上下文
- **配套 Skill**：`dist/resume-tool-agent.zip` —— 安装到办公室同事的 WorkBuddy（用户级技能），一句"帮我筛选这些简历"即可触发全流程；源码位于 `~/.workbuddy/skills/resume-tool-agent/SKILL.md`

## 格式支持（doc/pdf 优先，按已确认需求）

| 格式 | 解析方式 | 依赖 |
|------|---------|------|
| .docx | 标准库 zipfile + XML 解析 | 无 |
| .doc（伪装的） | 智能识别：市面上许多 .doc 实为 docx / RTF / HTML / 纯文本；逐一探测 | 无 |
| .doc（真二进制） | 用 Word COM 转 docx 再解析 | pywin32 + 本机安装的 Word（仅 Windows） |
| .pdf | pypdf 提取文本 | `pip install pypdf`（缺失时跳过并告警） |
| .txt / .md | 多编码尝试（utf-8 / gb18030） | 无 |

导入按文件内容的 SHA-256 去重，重复自动跳过。

## 多厂商 AI 接入（OpenAI 兼容协议）

编辑 `config/providers.json`：

```json
{
  "active": "deepseek",
  "providers": {
    "deepseek": {"label": "DeepSeek", "base_url": "https://api.deepseek.com/v1", "api_key": "YOUR_API_KEY", "model": "deepseek-chat"},
    "custom":   {"label": "Custom",   "base_url": "https://your-gateway.example.com/v1", "api_key": "...", "model": "..."}
  }
}
```

- 三个预设：DeepSeek / Moonshot Kimi / 自定义；**任何兼容 OpenAI chat/completions 协议的服务均可**（包括自建网关和企业代理）
- 切换厂商 = 修改 `active` 字段；运行 `python resume_tool.py providers` 查看就绪状态
- Key 只保存在本地 JSON 文件中，**绝不提交进仓库、绝不打进 exe**（按可行性报告的安全要求）
- base_url / model 取值请参见各厂商官方文档

## 版本更新与分发（GitHub Release 驱动）

客户端始终查询 `Vanessa0712/vanessa-resume-tool` 的[最新稳定 Release](https://github.com/Vanessa0712/vanessa-resume-tool/releases/latest)。默认每次运行 `python app.py` 时做一次后台检查，不阻塞 Web 服务；设置 → 版本更新显示检查结果，点"检查更新"可再查一次。重开设置页只读本地状态，不会反复请求 GitHub。

发现新版本时，会显示 Release 标题、说明和发布页链接。更新目前为半自动——**检查 + 手动下载**：不会静默下载、覆盖或执行任何东西。

维护者发布更新的步骤：

1. 在 `app.py` 的 `CHANGELOG` 顶部新增一条版本记录（第一条为客户端当前版本），并更新 `update-manifest.json` 作为发布说明模板
2. 提交代码并创建与版本对应的 Git tag，例如 `ver1.3.0`
3. 在 GitHub 仓库新建 Release，选中该 tag，填写标题与发布说明；如有 exe / zip 安装包，上传为 Release 附件
4. 以**稳定 Release** 发布——不要勾选 Draft 或 Pre-release

客户端调用 `https://api.github.com/repos/Vanessa0712/vanessa-resume-tool/releases/latest`：该端点只返回最新稳定 Release，排除草稿和预发布；若尚无稳定 Release，客户端会明确说明 404，而不是错误地报告"已是最新"。启动检查可在通用设置中关闭；手动检查不受影响。

升级前请备份 `resume_tool.db` 和 `config/`；exe 数据目录移出安装包后，更新安装包不会覆盖简历数据。

## 数据备份与恢复

设置 → 数据备份与恢复为日常办公提供兜底：

- 手动备份：SQLite 一致性快照 + AI 配置 + 筛选模板打包为 ZIP；可选"包含简历原件"
- 每日自动备份：默认开启，每天首次启动时生成一份数据库 + 配置备份；保留 7 份自动备份、3 份恢复前备份、20 份手动备份
- 下载与恢复：设置页列出备份文件可直接下载；上传 `.zip` 或 `.db` 即可恢复，且总是先创建一份 `pre-restore` 备份
- 恢复校验：检查 SQLite 完整性、必备表、备份大小与上传路径；筛选任务进行中时拒绝恢复

备份文件**不加密**，且包含候选人个人信息和 AI Key。请存放在公司受控目录或加密磁盘上，勿通过公共聊天工具传输。`backups/` 已列入 `.gitignore`。

## 隐私清理、日志与诊断

- 路径信任边界：原件只能定位在数据目录的 `uploads/` 内；拒绝绝对路径和 `..` 穿越，越界路径在恢复后会做清理。数据目录内的遗留文件（如 `samples/`）启动时会被复制到 `uploads/legacy/` 并改写为相对路径
- 回收站隔离：回收站中的简历在被恢复之前，无法被导出、下载或用本地程序打开
- 永久删除：选择"永久删除"或"清空回收站"会删除简历及其筛选记录，再把原件移入数据目录内的 `.trash/` 暂存区，保留 30 天后自动清除；从外部目录导入的文件未经确认绝不被移动——通知里只显示数量
- 孤儿清理：设置 → 运行诊断与存储清理 会统计 `uploads/` 中不再被任何简历引用的文件（重复导入留下的副本、失败上传残留），一键移入 `.trash/`；10 分钟内的新文件和 `.partial` 上传会被跳过，进行中的导入绝不会被误扫
- 原子写入：上传和目录导入以 `.partial` 落盘后重命名，备份恢复先落到临时数据库，再用单次 `os.replace` 切换——恢复失败时当前数据库原样保留
- 单任务筛选：同一时刻只允许一个筛选任务排队或运行，防止重复 AI 计费和对库的并发写入
- 运行日志：写入数据目录的 `logs/resume-tool.log`；单文件 2 MB，滚动 5 份；打成无控制台 exe 后事故场景仍可还原
- 错误 ID：发生 500 时页面展示 8 字符错误 ID，堆栈按同一 ID 记入日志
- 诊断包："下载诊断包"导出 ZIP，含环境信息、数据量统计、脱敏后的 AI 配置（仅显示是否配置了 Key）以及最近 3 份日志；打包前日志已清洗（查询值、电话、邮箱、身份证与银行卡号打码），诊断包**不含简历文本、候选人个人信息、API Key 或数据库文件**

## 筛选模板

`config/template.json`：`hard_gates`（必备技能 + 最低年限）+ `core_skills`（权重 + 别名）+ `nice_to_have`。切换岗位时，复制该文件修改技能表，再把 `--template` 指过去即可。可并存多个模板。

## 已验证（本地测试）

- 关键回归：`python -m unittest discover -s tests -v`（53 项测试），覆盖导出 ID 去重、CSV 约定、损坏配置降级、模板路径穿越、跨站写请求、后台任务异常、DB 连接释放、备份/恢复、恢复到全新目录、数据目录迁移、相对路径、导入批次、永久删除时源文件清理、孤儿清理、诊断包脱敏（含日志清洗与身份证/银行卡打码）、模板选择记忆、模板删除/恢复、路径信任边界、事务化恢复（恢复失败保留当前库）、回收站保留期、孤儿扫描宽限期、单任务闸门、CLI/Web 数据目录一致性、删除失败上报、删除后搜索上下文保留，以及不再依赖原生 `confirm()`
- 测试运行在隔离的临时数据目录（`RESUME_TOOL_DATA_DIR` + `RESUME_TOOL_SKIP_MIGRATION=1`），绝不读写真实简历库
- 7 个样例覆盖全部格式路径（txt / 真 docx / 伪装 .doc 的 docx / 伪装 .doc 的 HTML / 伪装 .doc 的 RTF / 手工构造的 PDF），全部解析正确
- 关键词引擎：高级后端候选人 91.7 分通过；缺少必备技能的候选人 0 分不通过；门槛与评分逻辑正确
- 未配置 AI 时展示可操作指引（不崩溃）；AI 不可用时漏斗自动降级为"人工复核"提示
- 重复导入按内容哈希 100% 去重；CSV 导出使用 utf-8-sig（Excel 打开不乱码）
- **完整 Agent 协作链路（2026-11-14）**：8 份简历关键词预筛 → agent-export（PII 脱敏生效）→ Agent 会话内评估 → agent-import 写回 8 条记录（engine=ai + provider 可追溯）→ Web 界面 /screen 与 CSV 报告均正确展示

## 桌面应用（Beta ver1.1.0）

```bash
python app.py            # 打开独立应用窗口（默认）
python app.py --browser  # 改用浏览器打开（排障或开发调试时用）
```

- 窗口关闭即退出程序，后台服务随窗口一起结束
- 筛选任务运行中关窗会先弹确认框，避免误中断任务
- 同一时间只运行一个实例：重复双击会直接切换到已打开的窗口
- 系统缺少 WebView2 运行时时自动回退到浏览器打开，功能不受影响

| 页面 | 能力 |
|------|------|
| 简历库 `/` | 目录导入 / 拖拽上传（原件复制进数据目录），**导入批次记录（逐文件失败原因）**，统计卡，**主列表分页（每页 15 条，底部分页器 + 跳页）**，**简历正文全文搜索**（`?q=数据 工程师`，FTS5 + 中文按字切分，带命中片段），**按模板查看**（`?template=<slug>`，统计与状态只按该模板计算），多选批量删除（确认弹窗 + "今天不再询问"），CSV 导出 |
| 导入日志 `/imports` | 批次来源、总数、成功 / 重复 / 失败统计；批次详情逐文件列出新增、已存在、重复、无法解析或出错及原因 |
| 候选人 `/persons` | 同一人多个版本自动归并（邮箱 → 电话 → 姓名），展示最新版本与版本数；`/person/<id>` 列出全部版本供对比 |
| 回收站 `/trash` | 删除进回收站；单个/批量恢复、永久删除、清空回收站；重新导入相同文件时自动恢复原记录 |
| 筛选中心 `/screen` | **可搜索的多选模板**（边输边过滤、chip 展示、★ 收藏置顶、最近使用、AI 调用量预估、键盘操作）；**选择会被记住并在下次启动时还原**（存于 `config/app_settings.json` 的 `screen_templates`，模板被删除时自动失效）；三种引擎可选；简历范围 = 全部 / 仅未筛（按所选模板）/ 指定简历（可按文件名或 ID 搜索、按状态筛选、全选）；**筛选在后台线程执行**，页面轮询进度条（`/screen/job/<id>`），完成卡片保留结果摘要；结果按模板分组 |
| 图表 `/charts` | 状态分布环图、分数分布、各模板通过率、技能命中热力图、导入趋势（服务端计算 + 手写 CSS/SVG，零外部图表库） |
| 模板管理 `/templates` | 创建、可视化编辑（部门 / 子岗位、增删技能行、权重、别名、必备标记、权重实时合计）、★ 收藏、JSON 源码模式、批量导入（逐文件校验、重名自动改名）、单个 JSON 导出 / 全部导出 ZIP、**删除带"最近删除"恢复（移入 `.trash`，保留 30 天，绝不覆盖已有模板）** |
| 简历详情 `/resume/<id>` | 解析文本预览 → 筛选历史（每页 10 条，底部分页器 + 跳页）→ 原件预览（PDF/文本内嵌，doc/docx 提示本地打开）；用以下方式打开：系统默认 / WPS / Word / 资源管理器中显示 / 云文档（复制路径 + 打开网盘） |
| 导出 | `GET /export-xlsx`：候选人报告（14 列，冻结表头 + 自动筛选 + 汇总 sheet，支持 q/template/ids 过滤，ID 自动去重）；`/export` 产出同搜索/模板约定的 CSV，空结果时给出中文提示 |
| 更新日志 `/changelog` | 侧栏入口（未读时带 NEW 徽标），历史公告以版本卡片呈现，可展开/收起 |
| 设置 `/settings` | 启动自检 + 对照最新稳定 GitHub Release + 发布说明与发布页链接，通用设置（打开方式 / 云文档 / 启动检查 / 每日自动备份），备份列表与恢复，孤儿清理与诊断包导出，多厂商 Key / BaseURL / 模型配置（留空不覆盖已存 Key） |
| 筛选中心 `/screen` | 提交 AI/漏斗任务前确认预估调用量；后台任务离开页面后继续运行，失败或失效时给出明确提示 |

其他：

- 进入时弹出更新公告（`app.py` 中的 `CHANGELOG`），每个会话一次；"今天不再显示"存于 localStorage，按日期自动重置
- 模板修改即时生效（`TEMPLATES_AUTO_RELOAD`）；界面迭代无需重启
- 全部前端资源本地自制，零 CDN，可离线运行

## 视觉主题（Phase A）

暖纸风格：`--bg #FAF9F5`，主色珊瑚 `#D97757`，白色卡片配 16px 圆角。样式分为两层：

| 文件 | 职责 |
|------|------|
| `static/style.css` | 基础结构与组件样式（保持不动） |
| `static/theme.css` | 主题覆盖层，在 style.css 之后加载；只在此处改颜色 / 圆角 / 字体 |
| `static/fonts/*.woff2` | 内嵌 Noto Serif SC 子集（完整 GB2312 字集 + 界面文案字形）；无需本机安装字体 |

字体重建：`pip install fonttools brotli && python tools/build_fonts.py`（下载 → 子集化 → 校验汉字覆盖）。
字体许可证为 SIL OFL 1.1，见 `static/fonts/LICENSE.txt`。

## 已知限制

- 真二进制 .doc 需要使用者本机安装 Word（否则跳过并告警；其他简历不受影响）
- 扫描件（纯图片）PDF 无文本可提取，需要 OCR（未实现；如需要另行评估）
- 关键词引擎可能漏掉改写措辞的简历——正因如此，漏斗由 AI 复核灰色地带
- 引擎 B 使用防御性 JSON 解析；极端情况下单份简历失败不会中断整批
- "用以下方式打开"调用本地程序，仅对 127.0.0.1 本地访问有效；云文档只复制路径，不上传任何内容
- 候选人归并依赖可识别的姓名 / 电话 / 邮箱：三者都缺失时退回按文件名（绝不与他人合并）；同名者可能被误并
- xlsx 导出依赖 `openpyxl`（缺失时页面展示安装命令；其他功能不受影响）
- 全文搜索使用 SQLite FTS5：中文按字切分 + 短语匹配（`"数 据"`），英文按词；不支持同义词或拼音
- 无 FTS5 的环境（罕见）退回 LIKE 全表扫描：仍可用，但大数据量时较慢
- "全部模板"视图取每份简历**最近一次**筛选结果（跨模板）；按模板看统计请用下拉选择
- 回收站是软删除：筛选记录保留，磁盘上的源文件始终保留；永久删除不可逆
- 后台任务状态存于进程内存，服务重启后无法查看历史任务进度（数据本身已持久化）
