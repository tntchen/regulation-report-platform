# 银行监管报送智能开发平台 (regulation-report-platform)

参赛 Demo：基于多 Agent 协作的监管报送 ETL 代码智能生成平台。
Python 3.10 + FastAPI + SQLite（平台库）+ MySQL（租户业务库，MCP 只读接入）。

## 边界与能力声明

为避免评审误读，平台当前能力边界如实声明如下。

**【L2 已完成的真实能力】**

- JWT 认证 + 租户鉴权：除 `/health` 与登录接口外全部 API 需 Bearer Token，跨租户访问 403。
- 审计留痕：`audit_logs` 全量记录 who/when/action/result，`trace_id` 贯穿请求、日志与响应头。
- 任务异步化 + 断点恢复 + 幂等 + 取消：创建秒回；Agent 层 checkpoint 落库，worker 重启续跑；
  `client_request_id` 幂等去重；阶段边界优雅取消。
- SQL 只读三层纵深：sqlglot AST 只读白名单（仅单语句 SELECT，fail-closed）
  → 只读账号最小权限 → 执行护栏（10s 超时 + 行数上限 + 错误脱敏）。
- 真实语义向量：BGE-small-zh-v1.5 本地模型（512 维）+ 双通道融合检索
  （语义余弦 0.7 + 中文 bigram 0.3），检索评测基线 **Top-1 81% / Top-3 100%**。
- 工程质量：76+ pytest 全绿 + CI；`SECRET_KEY` 等敏感配置全部环境变量注入。

**【仍为 Demo 裁剪项】**

- MySQL 真实链路代码就绪，但**待 NAS/Docker 环境实测**（见 `docs/环境验证清单_NAS.md`）；
  当前演示路径走 SQLite 演示数据集，Agent 4 测试验证属"语法级验证"。
- 数字孪生为单一场景（1104 G01 vs EAST 个人住房贷款）。
- Agent 4 对账口径与 Mock SQL 耦合（真实 AI 生成 SQL 的泛化验证有限）。
- 单进程内置 worker（生产需替换为外部队列，`run_task()` 入口已预留）。
- 权限体系已建立（认证 + 租户隔离 + 审计），但**角色权限矩阵未细化**（无 RBAC 分级授权）。
- 前端无自动化测试。

## 里程碑状态

- **M1**：标准包结构重组 + Agent 1/2/3（制度解析 → 代码生成 → 质量校验）串行流程跑通，
  质量门禁支持 blocker 阻断回退重试、warning 放行记录。
- **M2**：Agent 4/5/6（测试验证 / 数字孪生 / 投产交付）真实实现，6Agent 完整链路打通：
  Agent 4 与 Agent 5 并行执行；Agent 4 关键项失败同样回退 Agent 2；
  Agent 6 汇总全链路产出生成 Markdown 交付物至 `data/tasks/{task_id}/`。
- **M3**：数据与向量库管线落地——任务状态 SQLite 持久化（重启可查）；
  38 份真实制度文档批量导入向量库；"上传→解析→切片→索引→检索测试"闭环；
  向量库维护 API 全量补齐（文档管理/索引重建/检索测试/统计/日志）。
- **M4**：React 18 + Ant Design 5 前端，页面覆盖任务大厅 / 任务执行（6Agent 流水线实时轮询）/
  六维校验报告 / 数字孪生对比 / 向量库维护 / 审计日志。
- **L2 D1-D2**：JWT 认证 + 租户鉴权（bcrypt 密码、HS256 Token、跨租户 403）。
- **L2 D3**：审计日志（trace_id 贯穿 + 中间件自动埋点 + 前端审计日志页）。
- **L2 D4-D5**：任务异步化 + 断点恢复 + 幂等 + 取消。
- **L2 D6-D7**：database_mcp 真实执行 + sqlglot 只读纵深三层。
- **L2 D8-D9**：BGE-small-zh 真实向量 + 双通道融合检索（Top-1 81% 基线）。
- **L2 D10（进行中）**：配置外移 + 收口——租户动态化、死代码清理、Mock fail-fast、
  深度健康检查、CI、文档收口（详见 `docs/方案评审与改进路线图.md` §四 Day 10）。

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
│   │   ├── tenant_context.py      # 多租户上下文（ContextVar 隔离 + 租户表动态加载）
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
│   │   ├── regulation_rag.py      # 制度检索 RAG（双通道融合 + 禁用文档默认过滤）
│   │   └── demo_dataset.py        # SQLite 演示数据集（12 笔贷款种子，含可解释差异）
│   │
│   ├── models/                    # SQLAlchemy 数据模型
│   │   ├── tenant.py / task.py / document.py / regulation.py
│   │
│   ├── services/                  # 业务服务层
│   │   ├── task_service.py        # 任务状态持久化（SQLite，重启可查）
│   │   ├── embedding_service.py   # Embedding 服务（local=BGE 语义模型 / remote / tfidf 兜底）
│   │   ├── vector_service.py      # 租户级向量索引（SQLite 存储 + 双通道融合检索）
│   │   └── document_service.py    # 上传文档解析（TXT/MD 直读，PDF/DOCX 视环境）
│   └── utils/
│       └── exceptions.py          # 平台自定义异常
│
├── scripts/
│   ├── smoke_test.py              # M1 冒烟测试：3Agent 串行验证
│   ├── smoke_test_m2.py           # M2 冒烟测试：6Agent 全链路 + 门禁回退 + 数字孪生
│   ├── smoke_test_m3.py           # M3 冒烟测试：向量库管线 + 持久化验证
│   ├── seed_tenants.py            # 首次初始化：租户 + 演示用户 + 数据源 + 预置制度文档（L2-D10）
│   └── seed_regulations.py        # 38 份真实制度文档批量导入（向量库管线专用）
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

> 一键方式：`bash scripts/setup_dev.sh`（建 venv + 装依赖 + 灌演示数据，加 `--serve` 直接起服务）。
> 运行机制与参数详见 `docs/开发环境一键脚本说明.md`。以下为手工分步方式：

```bash
pip install -r requirements.txt
cp .env.example .env    # 可选；生产部署必须设置 SECRET_KEY

# 1. 首次初始化：租户 + 演示用户 + 数据源 + 预置制度文档（38 份）
python scripts/seed_tenants.py

# 2. 启动后端（默认 8080 端口，首次启动自动建表）
python -m backend.main
# 或
uvicorn backend.main:app --host 0.0.0.0 --port 8080

# 3. 启动前端（另开终端，默认 5173 端口，/v1 与 /health 代理到 8080）
cd frontend
npm install
npm run dev

# 4. 浏览器打开前端 → 登录（admin / Admin@1234）→ 任务大厅"新建任务"一键演示
```

**演示账号**（由 `scripts/seed_tenants.py` 初始化，密码 bcrypt 哈希落库）：

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

**异步任务模型（L2-D4）**：

- 创建任务异步化：POST 落库 `queued` 立即返回 task_id（秒回），内置 asyncio worker
  轮询取任务后台执行；并发上限 = 全局 `TASK_WORKER_MAX_CONCURRENCY`（默认 2）与
  租户 `tenants.max_concurrent_tasks`（无记录时回退全局默认）双重约束。
- 断点恢复：每个 Agent 层完成即落库 checkpoint（已完成阶段集合 + 下一阶段）；
  worker 启动时扫描 executing 遗留任务——有断点从下一阶段续跑（已完成阶段不重复执行），
  无断点从头重跑（各 Agent 产出幂等覆盖），死亡前已请求取消的直接终结 cancelled。
- 幂等：创建接口接受可选 `client_request_id`（租户+用户+该 ID 唯一），重复提交返回已有任务。
- 取消：`POST /v1/tenants/{tid}/tasks/{id}/cancel`——queued 直接 cancelled；
  executing 设置取消标记，编排器在阶段边界检查并优雅终止；审计动作 `task.cancel`。
- 门禁回退修正：block 回退时从 regulation_parser 重跑，刷新检索上下文，避免过期口径。
- worker 与请求处理解耦：`run_task()` 为独立执行入口，未来可替换为外部队列
  （关闭 `TASK_WORKER_ENABLED`，由外部消费者复用同一入口）。
- 任务状态机新增状态：`queued`（排队中）/`cancelled`（已取消）；前端任务大厅与执行页
  已适配排队状态展示与"取消任务"按钮。

**数据源与 SQL 只读纵深（L2-D6）**：

- `database_mcp` 真实化：schema 查询走真实元数据（SQLite PRAGMA / MySQL information_schema），
  `execute_sql` 真实执行并返回结果集（列名+行+截断标记），不再是硬编码桩。
- 只读三层纵深：
  1. **AST 白名单**（`utils/sql_guard.py`，sqlglot 解析）：仅放行单语句 SELECT（含 WITH...SELECT）；
     多语句、写操作、DDL/DCL、INTO OUTFILE/DUMPFILE、LOAD_FILE、危险函数（SLEEP/BENCHMARK/GET_LOCK）
     一律拒绝；注释不影响判定；解析失败 fail-closed 拒绝。
  2. **数据库侧最小权限**：生产 MySQL 用 `mcp_readonly` 账号（仅 SELECT，见 `scripts/seed_mysql.py`）；
     root 不进配置；演示环境物理隔离于 SQLite 演示数据集。
  3. **执行护栏**：语句超时 10s + 结果行数上限 + 错误信息脱敏（不泄露库表结构细节）。
- 方言支持矩阵：

  | 方言 | 状态 |
  | --- | --- |
  | sqlite_demo（演示数据集） | ✅ 默认路径，真实执行 |
  | MySQL | ✅ 代码就绪（information_schema + 异步驱动路由）；**真实链路验证待 Docker 环境** |
  | Oracle / GaussDB | ⚠️ 适配器扩展点，未实现（配置时明确报错而非静默失败） |

- Agent 4 测试验证当前在 SQLite 上执行，属"语法级验证"（代码注释已声明）；
  Docker MySQL 可用时执行 `scripts/seed_mysql.py` 灌种子后切换数据源 `db_type=mysql` 即可。
- T002 数据源已修复为 `sqlite_demo`（原 oracle 配置与实现自相矛盾，已消除）。
- 异步修复：`demo_dataset` 同步 sqlite3 调用全部改为线程池异步包装（aquery/aexecute_script 等），
  Agent 4/5 不再阻塞事件循环。

**真实向量检索与索引一致性（L2-D8）**：

- Embedding 真实化（`services/embedding_service.py`，provider 配置真正生效）：
  - `local`（默认）：sentence-transformers 本地模型 BAAI/bge-small-zh-v1.5（512 维，L2 归一化）；
    **首次运行需联网下载模型**（约 100MB，HF 缓存后离线可用）；模型加载失败自动降级 tfidf 并记日志。
  - `remote`：OpenAI 兼容 embedding 端点（`embedding_remote_base_url/api_key/model`），生产替换点。
  - `tfidf`：sklearn HashingVectorizer 字符 n-gram 兜底（无模型依赖、确定性，远优于原 md5 伪向量）。
- 双通道融合检索：`relevance = 0.7 × vector_score（语义余弦） + 0.3 × text_score（中文 bigram 文本命中）`，
  权重与阈值由 `retrieval_vector_weight / retrieval_text_weight / retrieval_threshold` 配置；
  同义召回实测："逾期90天的房贷如何分类" 命中 G11 五级分类 vector_score 0.697（旧纯文本通道低于阈值必漏）。
- 索引存储 SQLite 化：`data/tenants/{tid}/vectors/vectors.db`（aiosqlite，chunks 表；
  `index_document` 单事务原子写，崩溃不留半状态；旧 chunks.json 启动时一次性自动迁移）。
- 索引一致性修复：`rag.retrieve` 未显式传 `active_doc_ids` 时默认只查启用文档，
  修掉"禁用文档仍被 RAG 召回"的 bug（租户无登记文档时不过滤，保留预置文档兜底）。
- 检索评测基线（`scripts/eval_retrieval.py`，16 条评测集）：
  **Top-1 13/16 (81%) | Top-3 16/16 | Top-5 16/16**。
- 前端检索测试弹窗展示 融合/向量/文本 三通道得分 Tag。
- 推理稳定性：torch 限制单线程 + 进程级推理锁串行化 encode
  （torch 原生层在多事件循环/多线程并发推理下会 segfault，已在代码注释声明）。

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

# 检索评测基线（L2-D8，16 条评测集）
python scripts/eval_retrieval.py     # Top-1 81% / Top-3 100% / Top-5 100%

# pytest 全量（76 用例；离线环境建议加 HF_HUB_OFFLINE=1 避免 HF 网络探测）
HF_HUB_OFFLINE=1 python -m pytest tests/ -q
```

M2 冒烟测试覆盖三个场景：
- **正向**：完整 6Agent 链路，Agent 4/5 并行，Agent 6 生成 4 份 Markdown 交付物并校验文件真实存在
- **门禁回退**：先坏后好的 Mock（验证 block → 回退 Agent2 → 重试通过，retry=1）；
  始终违规的 Mock（验证超过 3 次重试后任务 failed）
- **数字孪生**：校验两口径差异总额 = 15,600 元（与种子数据资本化利息之和勾稽）、归因文字完整

> 离线 Mock 模式：演示默认走 `MockAIAdapter`（确定性演示 SQL，不依赖真实 AI Key）。
> Mock 仅在显式开启 `AI_MOCK_MODE=true` 时生效；接入真实 AI 服务时在 `.env` 配置
> `AI_BASE_URL` / `AI_API_KEY` 并设置 `AI_MOCK_MODE=false`——
> 非 Mock 模式且 Key 缺失/无效时启动即报错（fail-fast，配错立即暴露）。

## 通过 API 创建报送任务

```bash
# 1. 先登录获取 Token
TOKEN=$(curl -s -X POST http://127.0.0.1:8080/v1/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"Admin@1234"}' | python -c "import sys,json;print(json.load(sys.stdin)['access_token'])")

# 2. 携带 Token 创建任务（创建后立即返回 task_id，任务后台异步执行）
curl -X POST http://127.0.0.1:8080/v1/tenants/T001/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "report_type": "EAST",
    "report_code": "EAST_LOAN_01",
    "section": "个人住房贷款",
    "source_tables": ["loan_contract"],
    "target_table": "rpt_east_housing_loan"
  }'
```

创建接口秒回 `task_id`（状态 `queued`）；轮询 `GET /v1/tenants/{tid}/tasks/{task_id}`
获取各 Agent 阶段执行明细与质量门禁报告（`outputs.quality_gate`）。

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

**存储**：文档元数据落 SQLite（regulation_documents 表）；向量索引落租户独立 SQLite
`data/tenants/{tenant_id}/vectors/vectors.db`（租户物理隔离，单事务原子写）。
Embedding 方案见上文 L2-D8：默认 `embedding_provider=local`（BGE-small-zh-v1.5 真实语义向量），
可切换 `remote`（OpenAI 兼容端点）或降级 `tfidf`。

**检索打分**：双通道融合——语义余弦 × 0.7 + 中文 bigram 文本命中 × 0.3（阈值/权重可配置），
结果含 `vector_score / text_score / relevance_score` 三个字段；
支持 `active_doc_ids` 过滤实现禁用文档不召回（RAG 侧默认即只查启用文档）。

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

- `database_mcp` 只读纵深三层：sqlglot AST 白名单（仅单语句 SELECT）→ 数据库侧 readonly 账号
  （仅 SELECT 权限）→ 执行护栏（超时 10s + 行数上限 + 错误脱敏）；表白名单另行约束；
- 所有生成代码强制机构权限（org_no）过滤与逻辑删除/测试数据剔除；
- 测试环境不得使用生产真实客户信息（C2/C3 字段输出需脱敏）。
