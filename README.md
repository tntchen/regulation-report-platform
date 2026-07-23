# 银行监管报送智能开发平台 (regulation-report-platform)

参赛 Demo：基于多 Agent 协作的监管报送 ETL 代码智能生成平台。
Python 3.10 + FastAPI + SQLite（平台库）+ MySQL（租户业务库，MCP 只读接入）。

## 里程碑状态

- **M1**：标准包结构重组 + Agent 1/2/3（制度解析 → 代码生成 → 质量校验）串行流程跑通，
  质量门禁支持 blocker 阻断回退重试、warning 放行记录。
- **M2**：Agent 4/5/6（测试验证 / 数字孪生 / 投产交付）真实实现，6Agent 完整链路打通：
  Agent 4 与 Agent 5 并行执行；Agent 4 关键项失败同样回退 Agent 2；
  Agent 6 汇总全链路产出生成 Markdown 交付物至 `data/tasks/{task_id}/`。
- **M3**：数据与向量库管线落地——任务状态 SQLite 持久化（重启可查）；
  38 份真实制度文档批量导入向量库；"上传→解析→切片→索引→检索测试"闭环；
  向量库维护 API 全量补齐（文档管理/索引重建/检索测试/统计/日志）。
- **M4（当前）**：React 18 + Ant Design 5 前端，5 个页面对接现有 API：
  任务大厅 / 任务执行（6Agent 流水线实时轮询）/ 六维校验报告 /
  数字孪生对比 / 向量库维护（含检索测试弹窗、文档详情、索引日志）。

## 目录结构

```
regulation-report-platform/
├── README.md
├── requirements.txt
├── .env.example
├── Dockerfile
├── docker-compose.yml
│
├── backend/                       # 后端服务包
│   ├── main.py                    # FastAPI 入口（/health + 挂载 api 路由）
│   ├── config.py                  # 配置管理（pydantic-settings，支持 .env）
│   ├── database.py                # 数据库连接（平台库 SQLite + 租户库动态引擎）
│   │
│   ├── api/                       # API 路由
│   │   ├── deps.py                # 公共依赖（租户上下文注入）
│   │   ├── tenants.py             # 租户管理
│   │   ├── tasks.py               # 任务管理（创建报送任务，触发 Agent 编排）
│   │   ├── regulations.py         # 制度文档上传/列表/重建索引/检索测试
│   │   └── mcp.py                 # MCP 服务（Schema 查询 / 只读 SQL / 制度检索）
│   │
│   ├── core/                      # 核心引擎
│   │   ├── tenant_context.py      # 多租户上下文（ContextVar 隔离 + 预置租户）
│   │   ├── orchestrator.py        # 任务编排引擎（DAG 调度 + 质量门禁回退重试）
│   │   └── ai_adapter.py          # AI 适配器（OpenAI 兼容 + 离线 MockAIAdapter）
│   │
│   ├── agents/                    # Agent 集群
│   │   ├── base.py                # Agent 基类（AgentResult/BaseAgent/AgentRegistry）
│   │   ├── regulation_parser.py   # Agent 1: 制度解析（检索制度、提取口径、识别陷阱）
│   │   ├── codegen.py             # Agent 2: 代码生成（按口径+Schema 生成转换 SQL）
│   │   ├── quality_gate.py        # Agent 3: 质量校验（六维校验 + 门禁判定）
│   │   ├── test_verify.py         # Agent 4: 测试验证（SQLite 真实执行 + 7 类校验脚本）
│   │   ├── digital_twin.py        # Agent 5: 数字孪生（1104 vs EAST 双口径差异归因）
│   │   └── deploy.py              # Agent 6: 投产交付（生成 Markdown 交付物）
│   │
│   ├── mcp/                       # MCP 服务
│   │   ├── database_mcp.py        # 数据库 MCP（白名单 Schema 查询 + 只读 SELECT，安全红线）
│   │   ├── regulation_rag.py      # 制度检索 RAG（内存向量 + 中文子串相关度）
│   │   └── demo_dataset.py        # SQLite 演示数据集（12 笔贷款种子，含可解释差异）
│   │
│   ├── models/                    # SQLAlchemy 数据模型
│   │   ├── tenant.py / task.py / document.py / regulation.py
│   │
│   ├── services/                  # 业务服务层
│   │   ├── task_service.py        # 任务状态持久化（SQLite，重启可查）
│   │   ├── vector_service.py      # 租户级向量索引（语义切片 + hash 向量 + 中文检索）
│   │   └── document_service.py    # 上传文档解析（TXT/MD 直读，PDF/DOCX 视环境）
│   └── utils/
│       └── exceptions.py          # 平台自定义异常
│
├── scripts/
│   ├── smoke_test.py              # M1 冒烟测试：3Agent 串行验证
│   ├── smoke_test_m2.py           # M2 冒烟测试：6Agent 全链路 + 门禁回退 + 数字孪生
│   ├── smoke_test_m3.py           # M3 冒烟测试：向量库管线 + 持久化验证
│   └── seed_regulations.py        # 38 份真实制度文档批量导入
│
├── frontend/                      # React 18 + AntD 5 前端（M4）
│   ├── src/App.tsx                # 布局：顶部租户切换 + 侧边菜单 + 路由
│   ├── src/api/client.ts          # API 封装（与后端契约对齐）
│   └── src/pages/                 # TaskHall/TaskExecute/QualityReport/DigitalTwin/VectorLibrary
│
└── data/                          # 运行时数据
    ├── platform.db                # 平台配置库（任务/文档元数据/索引日志）
    ├── demo_biz.db                # SQLite 演示数据集（首次运行自动播种）
    ├── tenants/{tenant_id}/       # 租户数据（regulations/ 文档 + vectors/ 索引）
    └── tasks/{task_id}/           # Agent 6 生成的投产交付物
```

## 快速开始

```bash
pip install -r requirements.txt
cp .env.example .env    # 可选；生产部署必须设置 SECRET_KEY

# 1. 导入预置制度文档（首次运行）
python scripts/seed_regulations.py

# 2. 启动后端（默认 8080 端口，首次启动自动建表并初始化演示用户）
python -m backend.main
# 或
uvicorn backend.main:app --host 0.0.0.0 --port 8080

# 3. 启动前端（另开终端，默认 5173 端口，/v1 与 /health 代理到 8080）
cd frontend
npm install
npm run dev
```

**演示账号**（启动时自动初始化，密码 bcrypt 哈希落库）：

| 账号 | 密码 | 可访问租户 |
| --- | --- | --- |
| admin | Admin@1234 | T001 + T002 |
| zhangsan | Zhangsan@1234 | 仅 T001 |

**认证说明**：除 `/health` 与 `/v1/auth/login` 外全部 API 需 `Authorization: Bearer <token>`；
token 为 JWT（HS256，默认 8 小时过期）；跨租户访问返回 403。
`SECRET_KEY` 必须从环境变量注入，非 debug 模式缺失时启动报错。

**审计日志（L2-D3）**：

- `audit_logs` 表记录 who / when / tenant / action / resource / detail / ip / result / duration_ms，
  每条带请求级 `trace_id`（响应头 `X-Trace-ID` 同步返回）。
- 审计中间件自动记录全部写操作（POST/PUT/DELETE，动作 `http.write`）；
  关键业务动作手动埋点：`auth.login`（成功/失败）、`task.create`、`mcp.execute_sql`、
  `document.upload/delete/disable/enable/reindex`、`regulations.reindex`。
- 审计写库容错：失败仅记 error 日志，不影响业务请求；detail 禁写密码/token（含兜底打码）。
- 查询接口：`GET /v1/tenants/{tid}/audit-logs`（分页 + action/username/时间过滤，挂租户鉴权）、
  `GET /v1/tenants/{tid}/audit-logs/actions`（动作类型清单）。
- 全局 logging（`backend/utils/logging.py`）：控制台 + `data/logs/platform.log`，格式含 trace_id；
  全局异常处理器对 5xx 返回带 trace_id 的 JSON。
- 前端侧边菜单"审计日志"页：AntD Table + 动作/用户名过滤 + 服务端分页 + detail 展开。

健康检查：

```bash
curl http://127.0.0.1:8080/health
# {"status":"ok","version":"2.0.0"}
```

**演示路径**：打开前端 → 登录（admin / Admin@1234）→ 任务大厅"新建任务"（选 EAST 或 1104 G01 模板）→
执行页观看 6 Agent 流水线实时跑完（Agent 4/5 并行）→
点击"六维校验报告"与"数字孪生对比"查看真实数据 →
侧边菜单进入"向量库维护"，用"检索测试"弹窗验证制度召回效果 →
侧边菜单进入"审计日志"，查看刚才每一步操作的留痕（动作/用户/结果/trace_id）。

## 冒烟测试（不依赖真实 AI Key、不依赖 MySQL）

```bash
# M1: 3Agent 串行（制度解析→代码生成→质量校验）
python scripts/smoke_test.py

# M2: 6Agent 全链路 + 门禁回退 + 数字孪生差异
python scripts/smoke_test_m2.py

# M3: 向量库管线（需先执行种子导入）
python scripts/seed_regulations.py   # 导入 38 份真实制度文档
python scripts/smoke_test_m3.py
```

M2 冒烟测试覆盖三个场景：
- **正向**：完整 6Agent 链路，Agent 4/5 并行，Agent 6 生成 4 份 Markdown 交付物并校验文件真实存在
- **门禁回退**：先坏后好的 Mock（验证 block → 回退 Agent2 → 重试通过，retry=1）；
  始终违规的 Mock（验证超过 3 次重试后任务 failed）
- **数字孪生**：校验两口径差异总额 = 15,600 元（与种子数据资本化利息之和勾稽）、归因文字完整

> 离线 Mock 模式：`config.py` 中 `ai_mock_mode=True`（默认开启），或未配置有效
> `AI_API_KEY` 时，`AIAdapterFactory` 自动返回 `MockAIAdapter`，产出确定性的演示 SQL。
> 接入真实 AI 服务时，在 `.env` 配置 `AI_BASE_URL` / `AI_API_KEY` 并设置 `AI_MOCK_MODE=false`。

## 通过 API 创建报送任务

```bash
curl -X POST http://127.0.0.1:8080/v1/tenants/T001/tasks \
  -H 'Content-Type: application/json' \
  -d '{
    "report_type": "EAST",
    "report_code": "EAST_LOAN_01",
    "section": "个人住房贷款",
    "source_tables": ["loan_contract"],
    "target_table": "rpt_east_housing_loan"
  }'
```

返回各 Agent 阶段执行明细与质量门禁报告（`outputs.quality_gate`）。

## Agent 3 质量校验（六维）

| 维度 | 校验要点 |
| --- | --- |
| 口径合规 | 贷款余额含利息调整部分；Agent 1 识别的 critical 陷阱在代码中有对应处理 |
| 类型安全 | 金额 ROUND 4 位；利率按 D20.6 ROUND 6 位 |
| 空值防御 | 可空字段参与运算必须有 IFNULL/COALESCE/CASE 防御 |
| 性能友好 | 禁止 SELECT *；WHERE 不得对索引列套函数；必须有过滤条件 |
| 安全合规 | 禁止危险 SQL；必须 is_deleted=0 / is_test=0 / org_no 过滤；C2/C3 字段脱敏 |
| 监管特殊 | 逾期 90 天分界；公积金组合贷 P001-G 纳入；利率报备 LPR 检查 |

门禁判定：任一 **blocker** → 阻断并回退 Agent 2 重试（最多 3 次，附带自动修正建议）；
无 blocker 有 **warning** → 放行并记录；全部通过 → 放行。

## Agent 4 测试验证（七项校验）

在 SQLite 演示数据集上真实执行 Agent 2 生成的 SQL（建目标表 → 执行 INSERT...SELECT），再逐项校验：

| 校验项 | 内容 | 关键项 |
| --- | --- | --- |
| 行数校验 | 目标表行数 > 0 且 ≤ 源表有效行数 | ✅ |
| 非空率 | contract_no/cust_id/loan_balance 非空率 100% | ✅ |
| 汇总对账 | SUM(loan_balance) 与源表按口径重算总额勾稽 | ✅ |
| 重复记录 | contract_no 主键唯一 | ✅ |
| 枚举值域 | five_classify ∈ 1-5（目标表无该列则 skipped） | |
| 90 天边界抽查 | od≥91 整笔本金、od=0 逾期本金为 0 | |
| 长度截断 | contract_no ≤ 32 字符 | |

关键项 fail → `critical_fail=True` → 编排器回退 Agent 2 重试。

## Agent 5 数字孪生（1104 vs EAST）

同一批贷款种子数据（12 笔，含资本化利息/逾期临界/应剔除样本）按两种口径模拟：

- 口径A（1104 G01）：纯本金余额
- 口径B（EAST）：账面余额 = 本金 + 资本化利息

差异分析引擎逐笔比对（contract_no 键）：绝对/相对差异、差异等级
（critical>5% / high>2% / medium>0.5%，阈值取自设计文档 §3.3），
并输出归因说明（差异方向、制度依据、跨表对账调节公式）。

## Agent 6 投产交付

汇总全部 Agent 产出，在 `data/tasks/{task_id}/` 生成：

- `01_转换逻辑说明.md` —— 任务概述 + 生成 SQL + 转换逻辑要点
- `02_口径映射表.md` —— 制度依据 + 陷阱清单 + 字段映射
- `03_校验结论摘要.md` —— 六维门禁结果 + 七项测试结果
- `04_投产Checklist.md` —— 自动校验项 + 人工确认项 + 孪生差异结论

## 向量库管线（M3）

**数据流**：上传 → 解析（TXT/MD 直读；PDF/DOCX 视环境，缺库时明确报"暂不支持"）
→ 语义切片（按标题/段落，chunk_size≈512，overlap≈50）→ 向量化索引。

**存储**：文档元数据落 SQLite（regulation_documents 表）；向量索引落租户独立目录
`data/tenants/{tenant_id}/vectors/chunks.json`（租户物理隔离）。
Embedding 为可替换点：默认 `embedding_provider=hash`（离线确定性伪向量），
接入真实 embedding 服务时替换 `VectorService.embed()` 即可。

**检索打分**：中文 bigram 子串命中 + 英文/数字词命中 + 标题加成，
支持 `active_doc_ids` 过滤实现禁用文档不召回。

**预置制度导入**：

```bash
python scripts/seed_regulations.py
# 38 份真实制度 txt → 复制到租户目录 + 元数据落库 + 索引，打印逐文档切片数
```

**向量库维护 API**：

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/v1/tenants/{tid}/regulations/documents` | 上传文档（自动索引） |
| GET | `.../regulations/documents` / `.../documents/{doc_id}` | 列表 / 详情（含切片） |
| PUT | `.../regulations/documents/{doc_id}` | 启用/禁用 |
| DELETE | `.../regulations/documents/{doc_id}` | 删除（索引+文件+元数据） |
| POST | `.../regulations/reindex` / `.../documents/{doc_id}/reindex` | 全量/单文档重建 |
| GET | `.../regulations/index-status` | 索引状态概览 |
| POST | `.../regulations/retrieval-test` | 检索测试（Top-K 排名/相关度/片段/耗时） |
| POST | `.../regulations/retrieval-feedback` | 检索反馈 |
| GET | `.../regulations/stats` / `.../index-logs` | 统计 / 索引日志 |

## 安全红线

- `database_mcp` 仅允许 SELECT 只读查询，内置危险关键字拦截与表白名单；
- 所有生成代码强制机构权限（org_no）过滤与逻辑删除/测试数据剔除；
- 测试环境不得使用生产真实客户信息（C2/C3 字段输出需脱敏）。
