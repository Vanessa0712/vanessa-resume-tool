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

## 三种筛选模式

| 模式 | 说明 | 成本 |
|------|------|------|
| `keyword` | 引擎A：本地关键词加权评分，完全离线 | 零 |
| `ai` | 引擎B：逐份调用大模型语义评估 | 按次计费 |
| `funnel`（推荐） | 关键词全量初筛 → AI 只复核「待核实」名单 | 成本可控 |

引擎A评分模型：硬性门槛（必备技能/年限）一票否决 + 核心技能加权评分（证据次数 0-5 分映射，核心占 90 分池 + 加分项 10 分池），与 `resume-screener` 同一套已验证逻辑。

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

## 筛选模板

`config/template.json`：`hard_gates`（必备技能 + 最低年限）+ `core_skills`（权重 + 别名）+ `nice_to_have`。换岗位 = 复制一份改技能表，`--template` 指定即可。多模板可并存。

## 已验证（本机实测）

- 7 份样本覆盖全部格式路径（txt / 真docx / docx伪装.doc / HTML伪装.doc / RTF伪装.doc / 手工PDF）全部正确解析
- 关键词引擎：91.7 分资深后端通过、0 分缺必备技能未通过，门槛与评分逻辑正确
- AI 未配置时给出可操作指引（不崩溃）；funnel 在 AI 缺失时自动降级为"人工复核提示"
- 重复导入按内容哈希 100% 去重；CSV 导出 utf-8-sig（Excel 直开不乱码）

## Web UI（Beta ver1.0.1）

```bash
python app.py        # → http://127.0.0.1:5000
```

| 页面 | 能力 |
|------|------|
| 简历库 `/` | 目录导入 / 拖拽上传、统计卡片、**主列表分页（每页 15 条，底部翻页 + 页码跳转）**、**正文全文搜索**（`?q=数据 工程师`，FTS5 + 中文按字切分，带命中片段）、**按模板查看**（`?template=<slug>`，统计与状态只算该模板）、多选批量删除（确认弹窗 + 今日不再提示）、导出 CSV |
| 候选人 `/persons` | 同人多版本自动归并（邮箱 → 手机 → 姓名），显示最新版本与版本数；`/person/<id>` 查看该人全部版本并可对比 |
| 回收站 `/trash` | 删除即入回收站，支持单条/批量恢复、彻底删除、清空；重新导入同内容文件会自动恢复原记录 |
| 筛选中心 `/screen` | **模板可搜索多选**（输入即过滤、chip 展示、★收藏置顶、最近使用、AI 调用量预估、键盘操作）；引擎三选一；简历范围 = 全部 / 仅未筛选（按所选模板）/ 指定简历（可搜索文件名或 ID、按状态过滤、全选）；**筛选在后台线程执行**，页面轮询进度条（`/screen/job/<id>`），完成卡片保留结果摘要；结果按模板分组 |
| 图表视图 `/charts` | 状态分布环形图、分数分布、各模板通过率、技能命中热度、导入趋势（服务端计算 + CSS/SVG 自绘，无外部图表库） |
| 模板管理 `/templates` | 新建、可视化编辑（含所属部门 / 子职业，技能行增删 / 权重 / 别名 / 必备勾选，实时权重合计）、★收藏、JSON 源码模式、批量导入（逐个校验、同名自动改名）、单个导出 JSON / 全部导出 ZIP |
| 简历详情 `/resume/<id>` | 解析文本预览 → 筛选历史（每页 10 条，底部翻页 + 页码跳转）→ 原文件预览（PDF/文本内嵌，doc/docx 引导本机打开）、打开方式可选：系统默认 / WPS / Word / 资源管理器定位 / 云文档（复制路径 + 打开网盘） |
| 导出 | `GET /export-xlsx`：候选人报告（14 列，表头冻结 + 自动筛选 + 汇总页，支持 q/template/ids 过滤）；`/export` 导出 CSV |
| 更新日志 `/changelog` | 侧边栏入口（未读显示 NEW），历史公告按版本卡片展示，可展开/收起 |
| AI 设置 `/settings` | 多厂商 Key/BaseURL/模型配置（留空不覆盖已存 Key）、默认打开方式与云文档地址 |

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
- 后台任务状态存于进程内存，重启服务后历史任务进度不可查（数据本身已落库）

## 下一步路线（对应可行性报告里程碑）

1. exe 打包：PyInstaller onedir + waitress + 数据目录外移 + 必改 5 项（见《exe-feasibility-report.md》）
2. 简历标签 / 同人多版本聚合、搜索历史与高亮
3. 批量导出候选人报告（xlsx，含评分明细与判定理由）
