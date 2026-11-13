# 《简历筛选与匹配系统 → Windows exe 桌面软件》可行性评估报告

> 评估对象：CSDN 文章《基于大模型的简历筛选与匹配系统》（Flask 2.3.2 + SQLite + Bootstrap/ECharts + DeepSeek API + XGBoost + Selenium 爬虫）
> 评估团队：系统架构师 梁承宇 ｜ 部署运维工程师 陆上线 ｜ 汇总裁决：首席交付架构师 高屋建
> 报告日期：2026-11-12

---

## 一、结论（裁决）

**有条件可行——技术侧无阻断性障碍，"行"。**

是否成立取决于三个前置条件，**任何一条不满足，结论需回炉重议**：

| # | 前置条件 | 说明 | 不满足时的后果 |
|---|---------|------|--------------|
| 1 | **拿到完整可用源码**（最关键） | 本次评估基于文章描述。若只有文章没有代码，项目性质变为"照文章重写一个单机版"，工作量完全不同 | 结论重评 |
| 2 | **剥离或可选化 Selenium 爬虫** | 三重硬伤：用户机器 Chrome/ChromeDriver 版本强耦合、ChromeDriver 是杀软误报重灾区、显著增大体积。CSV 导入保留为数据入口 | 打包可行但分发故障率与误报率大增 |
| 3 | **接受 DeepSeek 云 API 形态** | 联网 + 用户自备 API Key + 按 token 计费。这是系统核心卖点，无法打包进 exe，也不建议删除 | 若要纯离线，产品价值坍缩为"带图表的简历管理器" |

**一句话**：源码在手、砍掉爬虫、接受云 LLM——满足三条，这就是一个 1-2 周量级可以落地的单机桌面项目。

---

## 二、推荐技术路线

### 2.1 形态改造：B/S 多用户 Web → 单机单用户桌面

```
┌────────────────── Windows 进程（exe，onedir）──────────────────┐
│  Flask 应用（waitress 承载，绑定 127.0.0.1:随机端口）            │
│   ├─ 路由/模板/静态资源（ECharts，_MEIPASS 路径适配）            │
│   ├─ SSE 流式接口（本机回环，无代理缓冲干扰）                    │
│   └─ XGBoost + pickle 模型（data 资源随包分发）                 │
│  SQLite 数据库（%APPDATA% 或 exe 同级可写目录）                  │
│  渲染壳：自动拉起默认浏览器（首版）→ pywebview 原生窗口（二阶段）  │
└────────────────────────┬───────────────────────────────────────┘
                         │ HTTPS 出网（唯一外部依赖）
                  DeepSeek 大模型 API
```

### 2.2 选型结论与被否决方案

| 决策点 | 推荐 | 被否决方案及理由 |
|-------|------|----------------|
| 打包工具 | **PyInstaller 6.x，onedir 模式**（hook 生态覆盖 sklearn/xgboost/pandas/bcrypt 最全，2-5 分钟/次利于迭代） | ❌ Nuitka：收益（启动快、误报低）在本场景价值有限，全量编译 30 分钟+ 拖垮"打包→验证→补 hidden imports"的 3-5 轮迭代节奏；❌ cx_Freeze：hook 生态小 |
| 服务承载 | **waitress（生产级 WSGI）+ 自动拉起默认浏览器** | ❌ 保留 werkzeug 开发服务器：单机 127.0.0.1 勉强可用但无必要冒险（见裁决 #1）；❌ Electron 壳：+100~300 MB、引入 Node 双运行时、Python 变子进程管理；❌ Tauri：需 Rust 工具链，远超本项目需要 |
| 窗口形态 | 首版系统浏览器承载；二阶段可选 **pywebview** 原生窗口 | pywebview 的 WebView2 对 EventSource/SSE 支持需先实测【待验证】，异常则降级 fetch 轮询，故不进首版 |
| 分发方式 | **Inno Setup 安装包（主）+ zip 绿色单目录版（兜底）** | ❌ onefile 单文件：每次启动解压 %TEMP%（8-30 秒）、误报率显著更高、改一个资源重发整包 |
| 离线方案 | 保留 DeepSeek 云 API | ❌ 纯离线无 LLM 版 / 本地模型版：删除核心卖点；本地推理需数 GB~数十 GB 模型文件，与轻量 exe 冲突 |

### 2.3 体积与性能量级预估

| 指标 | 量级 | 说明 |
|------|------|------|
| 安装后占用 | **约 400-700 MB** | 大头是 Python 运行时 + numpy/pandas/scipy/sklearn/xgboost 科学计算栈（300 MB+ 无法本质压缩） |
| 安装包下载体积 | **约 150-300 MB** | LZMA2 压缩后 |
| 启动时间 | **1-5 秒**（onedir + SSD） | 拉起浏览器再加 1-2 秒 |

---

## 三、逐模块可行性判定

| 模块 | 判定 | 要点 |
|------|------|------|
| Flask 路由与模板 | 需改造（轻，小时级） | templates/static 打成 data 资源，代码统一走 `resource_path()`（`sys._MEIPASS` 适配）；Flask-CORS 单机同源可移除 |
| SQLite + SQLAlchemy | 需改造（必改） | 数据库文件迁出安装目录（Program Files 普通用户只读，首写即 `PermissionError`）；首次启动自动建库 + CSV 导入 |
| JWT + bcrypt 登录 | **建议降级** | 单机单用户场景 JWT 价值消失；可保留 bcrypt 本地口令防本机他人误入，或整体免登录（见裁决 #3） |
| ECharts / Bootstrap 前端 | 可直接打包 | 纯静态资源随包分发 |
| SSE 流式 + DeepSeek API | 可打包（网络条件） | 本机回环下 SSE 表现优于公网；**前提 `threaded=True`**（否则长连接阻塞全站假死）；Key 必须用户配置（见必改 #2） |
| XGBoost + pickle 模型 | 需打包配置 | `--collect-binaries xgboost` 解决 `libxgboost.dll` 收录（已知坑，可解）；**锁死训练版本 1.7.6 / sklearn 1.3.0**（pickle 对版本敏感） |
| Selenium + ChromeDriver 爬虫 | **建议剥离** | 版本强耦合 + 误报重灾区 + 体积负担；若为刚需，做外置独立工具而非打进主 exe |
| 多策略推荐算法 | 需改造 | **协同过滤（权重 0.25）在单机版失效**——单用户库无群体行为可挖；建议权重归零并重新归一化（内容匹配/热度/时间衰减为主） |

---

## 四、必改清单（不做必挂，工程侧 3 项 + 代码侧 2 项）

1. **数据/日志/配置目录外移**：安装版 `%APPDATA%\ResumeMatcher\`，绿色版 exe 同级（`portable.flag` 判定）；日志走 `RotatingFileHandler`（10MB×5）至 `%LOCALAPPDATA%`
2. **DeepSeek API Key 用户配置**：首启动检测 `config.json` 无 Key 弹设置引导；支持 `DEEPSEEK_API_KEY` 环境变量覆盖。**禁止硬编码进 exe**——PyInstaller 产物反解提取源码是公开工具链，几分钟可还原，Key 泄漏 = 他人刷你的账单
3. **Flask 启动参数**：`debug=False`、`use_reloader=False`（reloader 在打包下表现为双进程/闪退，Flask 打包第一大坑）、`threaded=True`、绑定 `127.0.0.1` 随机端口（**严禁 0.0.0.0**——否则局域网任何人可访问用户简历数据）；入口加 `multiprocessing.freeze_support()`
4. **统一资源路径解析**：`resource_path()` 处理 `_MEIPASS`，覆盖 templates/static/pickle 模型
5. **waitress 替换开发服务器**（见裁决 #1）

打包命令示例（节选，完整 spec 以实际源码目录结构为准）：

```bat
pyinstaller --noconfirm --clean --onedir --windowed ^
  --name ResumeMatcher ^
  --add-data "app\templates;templates" ^
  --add-data "app\static;static" ^
  --add-data "models\salary_model.pkl;models" ^
  --collect-binaries xgboost ^
  --hidden-import bcrypt ^
  run_app.py
```

---

## 五、工作量与排期估算（假设 1 名熟悉 Python 的开发）

| 里程碑 | 内容 | 量级估算 | 关键路径 |
|--------|------|---------|---------|
| M1 打包 spike | 最小可打包原型：xgboost DLL 收录、SSE 流式、（可选）pywebview 三项实测 | 1-2 天 | ✅ 是（三项均【待验证】） |
| M2 运行时改造 | 必改清单 5 项 + 爬虫剥离 + 协同过滤权重修正 | 1-2 人日 | ✅ 是 |
| M3 打包调试 | 打包→干净机验证→补 hidden imports，预计 3-5 轮（每轮 0.5-1 小时） | 1 天 | — |
| M4 分发与验收 | Inno Setup 安装包 + zip 版 + 双系统（Win10/11）干净机过验证清单 | 1-2 天 | — |
| **合计** | | **约 1-2 周**（不含爬虫外置工具与签名采购流程） | |

---

## 六、风险清单

| 风险 | 概率 | 影响 | 应对 |
|------|------|------|------|
| 杀毒软件误报（PyInstaller 无签名 bootloader 是行业现象） | 高 | 分发受阻、用户流失 | onedir + 最新 PyInstaller；采购代码签名证书（EV 可立即消除 SmartScreen 拦截，约几千元/年）；误报向 Microsoft 提交申诉 |
| SmartScreen "已保护你的电脑"拦截 | 高 | 首启转化损失 | 签名可解；无签名则使用手册引导"仍要运行"（自签名证书无效，勿浪费） |
| 目标机器无 Chrome / 版本失配（爬虫） | 高 | 功能崩溃 | 启动检测 Chrome 与 ChromeDriver 大版本匹配，不满足则禁用入口并引导，不让全应用崩溃；**根治 = 剥离爬虫** |
| XGBoost DLL 收录失败 | 中 | 启动即崩（WinError 126） | `--collect-binaries xgboost` → 手工 `--add-binary` 兜底 → M1 spike 实测 |
| pickle 模型版本不匹配 | 中 | 薪资预测启动崩溃 | 打包环境锁死训练版本；加载 try/except 给明确报错 |
| 体积膨胀（科学计算栈） | 中 | 下载转化率下降 | 安装包 LZMA2 控制在 300 MB 内；确认无 torch 等重库混入 |
| 端口冲突（5000 被占/保留） | 低 | 启动失败 | 随机端口探测 +1 重试 |

---

## 七、打包后验证清单（干净虚拟机执行，上线前逐项过）

- [ ] 双击启动无控制台黑框、无杀软拦截；首次启动 ≤5 秒（SSD）
- [ ] 数据目录与 `app.db` 创建在 %APPDATA%（或同级 data），**不在安装目录**
- [ ] 无 API Key 时弹设置引导而非崩溃；配置后可保存
- [ ] CSV 导入职位数据成功，重启后数据仍在
- [ ] **AI 分析逐字/逐段流式渲染**（非一次性整段返回）；分析期间其他页面可正常操作
- [ ] 薪资预测出数值，无 WinError 126（xgboost DLL 正常）
- [ ] ECharts 图表正常渲染
- [ ] 爬虫入口无 Chrome 时优雅降级，主功能不受影响
- [ ] 退出后任务管理器无残留进程（reloader 没关死的典型症状）
- [ ] 日志文件正常产生；卸载时 data/config 由用户选择保留
- [ ] 本机 Defender + 1-2 款第三方杀软扫描通过
- [ ] Windows 10 与 11 各一台干净机全流程通过

---

## 八、主理人裁决记录（成员分歧处理）

| # | 分歧点 | 梁承宇 | 陆上线 | 裁决 |
|---|--------|--------|--------|------|
| 1 | Web 服务器 | 必须换 waitress | werkzeug 单机可容忍，waitress "一行代码" | **第一天就换 waitress**——成本一行代码，消除一类风险，无需争论 |
| 2 | onedir 体积估算 | 150-300 MB | 400-700 MB | **取陆上线口径**（完整科学栈未裁剪的现实值），spike 后按实测修正 |
| 3 | 登录体系去留 | 剥离 JWT、bcrypt 可选 | 验证清单含登录功能 | **按架构师方案：剥离 JWT**（单用户价值消失）；若源码复用求快可暂留，标注为产品决策 |
| 4 | 爬虫去留 | 建议剥离 | 建议砍掉或可选化（长期维护包袱） | 两者一致：**剥离出主 exe**；是否做外置工具待用户确认 |

---

## 九、【待确认】开放问题（按优先级）

1. **是否持有完整源码？**（影响全局结论成立性）
2. DeepSeek API Key 与使用预算？（按 token 计费，频率决定成本）
3. 分发范围：仅自用还是分发给他人？（决定是否采购代码签名证书）
4. 爬虫功能是否刚需？（决定是否制作外置采集工具）
5. 目标 Windows 版本全覆盖 Win10/11，还是仅 Win11？
6. 初始职位数据：随包预置 CSV 快照，还是用户首启自行导入？
7. 【待验证】三项技术实测（M1 spike）：xgboost DLL 收录、SSE 流式、pywebview 对 EventSource 的支持
