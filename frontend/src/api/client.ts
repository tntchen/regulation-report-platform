/**
 * API 客户端封装
 * 统一封装后端 REST 调用；开发环境经 vite proxy 转发到 localhost:8080
 */

const BASE = ''
const TOKEN_KEY = 'access_token'

// ---------- token 存取 ----------
export const auth = {
  getToken: () => localStorage.getItem(TOKEN_KEY),
  setToken: (t: string) => localStorage.setItem(TOKEN_KEY, t),
  clear: () => {
    localStorage.removeItem(TOKEN_KEY)
    localStorage.removeItem('current_user')
  },
}

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = auth.getToken()
  const resp = await fetch(`${BASE}${path}`, {
    headers: {
      'Content-Type': 'application/json',
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
    ...options,
  })
  // 401 全局处理：清 token 并跳登录页
  if (resp.status === 401) {
    auth.clear()
    if (!location.pathname.startsWith('/login')) {
      location.href = '/login'
    }
    throw new Error('未认证或登录已过期')
  }
  if (!resp.ok) {
    const text = await resp.text()
    throw new Error(`API ${resp.status}: ${text.slice(0, 200)}`)
  }
  return resp.json()
}

// ---------- 类型定义（与后端契约对齐） ----------

export interface TenantBrief { id: string; name: string; code: string; status: string }

export interface TaskBrief {
  task_id: string; name: string; status: string; current_stage?: string
  progress: number; retry_count: number; duration_ms: number; created_at?: string
}

export interface StageRecord {
  agent_name: string; status: string; output: any
  duration_ms: number; error?: string; timestamp: string
}

export interface TaskDetail {
  task_id: string; tenant_id: string; status: string
  current_stage?: string; progress: number; retry_count: number
  error?: string; stages: StageRecord[]; outputs: Record<string, any>
  duration_ms: number
}

export interface RegulationDoc {
  id: string; filename: string; doc_type: string; size: number
  status: string; chunk_count: number; is_active: boolean; uploaded_at?: string
}

export interface VectorStats {
  tenant_id: string; doc_count: number; active_docs: number
  by_status: Record<string, number>; chunk_count: number
  vector_count: number; vector_dimension: number; storage_dir: string
}

export interface RetrievalItem {
  rank: number; doc_id: string; doc_type: string; doc_title: string
  content: string; relevance_score: number; source_file: string; chunk_index: number
  vector_score?: number; text_score?: number  // L2-D8 双通道得分
}

// ---------- 映射工作台 + 场景包（契约见 docs/映射工作台与场景包设计方案.md §一/§2.5） ----------

/** 场景包目标字段定义 */
export interface TargetSchemaField {
  field: string
  data_type: string
  required: boolean
  caliber_text: string           // 口径说明（制度语义通道的锚点）
  expected_domain?: string[]     // 期望值域/枚举（画像通道匹配用）
}

/** 勾稽规则 */
export interface ReconciliationRule {
  name: string
  expression: string
  tolerance: number
}

/** 场景包 Report Pack */
export interface ReportPack {
  id: string
  report_name: string
  report_type: string            // 1104 / EAST / ...
  target_table: string
  target_schema: TargetSchemaField[]
  source_tables: string[]
  reconciliation_rules: ReconciliationRule[]
  trap_refs: string[]
  regulation_keywords: string
  status: string                 // active / draft / disabled
  created_by?: string
  created_at?: string
  updated_at?: string
}

/** 五通道证据得分（缺失通道为 null，不计入融合权重） */
export interface MappingEvidence {
  name?: number | null
  comment?: number | null
  profile?: number | null
  semantic?: number | null
  history?: number | null
}

/** 源字段数据画像（并入 mappings 响应，不单独开 API） */
export interface ColumnProfile {
  null_rate?: number
  distinct_count?: number
  sample_values?: any[]
  min?: number | string | null
  max?: number | string | null
  format_pattern?: string | null // 证件号/手机号/日期/金额 等正则识别结果
  enum_values?: any[] | null     // 低基数字段的枚举值
}

/** 候选源字段（AI 推断候选，含得分） */
export interface CandidateField {
  source_table: string
  source_field: string
  comment?: string
  confidence: number
  evidence?: MappingEvidence
  profile?: ColumnProfile
}

/** 字段映射（状态机：ai_inferred/confirmed/modified/rejected/unmapped/needs_etl） */
export interface FieldMapping {
  id: string
  task_id: string
  report_pack_id: string
  target_field: string
  caliber_text?: string          // 目标字段口径说明（从场景包带入，便于工作台展示）
  source_table?: string | null   // 未映射时为空
  source_field?: string | null
  transform_rule: string         // "DIRECT" 或 SQL 表达式
  confidence: number             // 0-1
  evidence: MappingEvidence
  status: string
  confirmed_by?: string | null
  confirmed_at?: string | null
  profile?: ColumnProfile | null       // 已选源字段画像
  candidates?: CandidateField[]        // 候选源字段列表（工作台右列）
  created_at?: string
  updated_at?: string
}

/** 历史映射资产 */
export interface MappingAsset {
  id: string
  report_pack_id: string
  target_field: string
  source_table: string
  source_field: string
  transform_rule: string
  use_count: number
  last_confirmed_by?: string
  last_confirmed_at?: string
}

// ---------- 本轮新增契约（后端并行开发，类型按下述契约对齐） ----------

/** 源表探查：单字段画像 */
export interface ProfiledColumn {
  column_name: string
  data_type: string
  null_rate?: number          // 空值率 0-1
  distinct_count?: number     // 去重数
  sample_values?: any[]       // 样例值
  format_pattern?: string | null  // 格式识别（证件号/手机号/日期/金额…）
  enum_values?: any[] | null  // 低基数枚举值
  total_rows?: number         // 表总行数（各列同值）
}

/** 源表探查响应：GET /report-packs/{pack_id}/profile?table=xx */
export interface TableProfile {
  table: string
  columns: ProfiledColumn[]
}

/** 制度版本 diff 结果：POST /regulations/diff */
export interface RegulationDiffResult {
  summary: string
  added_sections: any[]      // 新增段落
  removed_sections: any[]    // 删除段落
  changed_sections: any[]    // 修改段落
  affected_keywords: string[] // 受影响关键词（Tag 云）
}

/** 新旧逻辑回归结果：POST /twin/regression */
export interface RegressionResult {
  old_total: number
  new_total: number
  diff_amount: number
  diff_rate: number
  top_diffs: any[]           // 差异明细行
  conclusion: string
}

/** 相似历史任务：GET /tasks/recommend?report_pack_id=xx */
export interface SimilarTask {
  task_id: string
  report_pack_id: string
  status: string
  created_at?: string
  similarity: number         // 0-1
  summary?: string
}

export interface AuditLogItem {
  id: number; timestamp: string; trace_id: string; username?: string
  tenant_id?: string; action: string; resource?: string
  detail: Record<string, any>; ip?: string; result: string; duration_ms?: number
}

// ---------- 报送台账 + 接口文件导出（P2 契约，后端并行开发，不 mock 进组件） ----------

/** 台账条目：GET /ledger?period=YYYY-MM */
export interface LedgerEntry {
  id: string
  report_pack_id: string
  report_name: string
  period: string
  deadline: string
  status: 'pending' | 'in_progress' | 'submitted' | 'overdue'
  task_id?: string
  submitted_at?: string
  days_left: number
}

/** 接口文件导出结果：POST /tasks/{task_id}/export-interface-file */
export interface ExportFileResult {
  file_name: string
  format: string
  row_count: number
  preview: string          // 文件内容预览（前几行）
}

/** 已生成导出文件列表项：GET /tasks/{task_id}/exports */
export interface ExportFileItem {
  file_name: string
  format: string
  row_count?: number
  created_at?: string
  size?: number
}

// ---------- API 方法 ----------

export const api = {
  health: () => request<{ status: string; version: string }>('/health'),

  // 认证
  login: (username: string, password: string) =>
    request<{ access_token: string; expires_in: number; user: any }>('/v1/auth/login', {
      method: 'POST', body: JSON.stringify({ username, password }),
    }),
  me: () => request<{ user: any; tenants: TenantBrief[] }>('/v1/auth/me'),

  listTenants: () => request<{ tenants: TenantBrief[] }>('/v1/tenants'),

  listTasks: (tid: string) =>
    request<{ total: number; tasks: TaskBrief[] }>(`/v1/tenants/${tid}/tasks`),

  createTask: (tid: string, payload: Record<string, any>) =>
    request<TaskDetail & { task_id: string }>(`/v1/tenants/${tid}/tasks`, {
      method: 'POST', body: JSON.stringify(payload),
    }),

  getTask: (tid: string, taskId: string) =>
    request<TaskDetail>(`/v1/tenants/${tid}/tasks/${taskId}`),

  cancelTask: (tid: string, taskId: string) =>
    request<{ task_id: string; status: string; message: string }>(
      `/v1/tenants/${tid}/tasks/${taskId}/cancel`, { method: 'POST' }),

  listDocuments: (tid: string) =>
    request<{ total: number; documents: RegulationDoc[] }>(`/v1/tenants/${tid}/regulations/documents`),

  getDocument: (tid: string, docId: string) =>
    request<any>(`/v1/tenants/${tid}/regulations/documents/${docId}`),

  updateDocument: (tid: string, docId: string, isActive: boolean) =>
    request<any>(`/v1/tenants/${tid}/regulations/documents/${docId}`, {
      method: 'PUT', body: JSON.stringify({ is_active: isActive }),
    }),

  deleteDocument: (tid: string, docId: string) =>
    request<any>(`/v1/tenants/${tid}/regulations/documents/${docId}`, { method: 'DELETE' }),

  reindexOne: (tid: string, docId: string) =>
    request<any>(`/v1/tenants/${tid}/regulations/documents/${docId}/reindex`, { method: 'POST' }),

  reindexAll: (tid: string) =>
    request<any>(`/v1/tenants/${tid}/regulations/reindex`, { method: 'POST' }),

  retrievalTest: (tid: string, query: string, topK = 5) =>
    request<{ elapsed_ms: number; total_found: number; results: RetrievalItem[] }>(
      `/v1/tenants/${tid}/regulations/retrieval-test?query=${encodeURIComponent(query)}&top_k=${topK}`,
      { method: 'POST' }),

  stats: (tid: string) => request<VectorStats>(`/v1/tenants/${tid}/regulations/stats`),

  indexLogs: (tid: string, limit = 10) =>
    request<{ total: number; logs: any[] }>(`/v1/tenants/${tid}/regulations/index-logs?limit=${limit}`),

  // ---------- 场景包（契约 §2.5，admin 才可写，后端强制鉴权） ----------
  listReportPacks: (tid: string) =>
    request<{ total: number; packs: ReportPack[] }>(`/v1/tenants/${tid}/report-packs`),

  getReportPack: (tid: string, packId: string) =>
    request<ReportPack>(`/v1/tenants/${tid}/report-packs/${packId}`),

  createReportPack: (tid: string, payload: Partial<ReportPack>) =>
    request<ReportPack>(`/v1/tenants/${tid}/report-packs`, {
      method: 'POST', body: JSON.stringify(payload),
    }),

  updateReportPack: (tid: string, packId: string, payload: Partial<ReportPack>) =>
    request<ReportPack>(`/v1/tenants/${tid}/report-packs/${packId}`, {
      method: 'PUT', body: JSON.stringify(payload),
    }),

  // ---------- 源表探查 / 制度预演 / 相似任务（本轮新增契约） ----------

  // 源表探查：场景包内某张源表的全字段画像
  profileTable: (tid: string, packId: string, table: string) =>
    request<TableProfile>(
      `/v1/tenants/${tid}/report-packs/${encodeURIComponent(packId)}/profile?table=${encodeURIComponent(table)}`),

  // 制度版本对比：两份文档 diff
  diffRegulations: (tid: string, docIdOld: string, docIdNew: string) =>
    request<RegulationDiffResult>(`/v1/tenants/${tid}/regulations/diff`, {
      method: 'POST', body: JSON.stringify({ doc_id_old: docIdOld, doc_id_new: docIdNew }),
    }),

  // 新旧逻辑回归：同一场景包下两段 SQL 的执行结果比对
  runRegression: (tid: string, payload: { report_pack_id: string; sql_old: string; sql_new: string }) =>
    request<RegressionResult>(`/v1/tenants/${tid}/twin/regression`, {
      method: 'POST', body: JSON.stringify(payload),
    }),

  // 相似历史任务推荐
  recommendTasks: (tid: string, reportPackId: string) =>
    request<{ similar_tasks: SimilarTask[] }>(
      `/v1/tenants/${tid}/tasks/recommend?report_pack_id=${encodeURIComponent(reportPackId)}`),

  // ---------- 映射（human-in-the-loop） ----------
  listTaskMappings: (tid: string, taskId: string) =>
    request<{ total: number; mappings: FieldMapping[] }>(
      `/v1/tenants/${tid}/tasks/${taskId}/mappings`),

  confirmMapping: (tid: string, taskId: string, mid: string, transformRule?: string) =>
    request<FieldMapping>(`/v1/tenants/${tid}/tasks/${taskId}/mappings/${mid}/confirm`, {
      method: 'POST', body: JSON.stringify(transformRule ? { transform_rule: transformRule } : {}),
    }),

  modifyMapping: (tid: string, taskId: string, mid: string,
    payload: { source_table: string; source_field: string; transform_rule: string }) =>
    request<FieldMapping>(`/v1/tenants/${tid}/tasks/${taskId}/mappings/${mid}/modify`, {
      method: 'POST', body: JSON.stringify(payload),
    }),

  rejectMapping: (tid: string, taskId: string, mid: string) =>
    request<FieldMapping>(`/v1/tenants/${tid}/tasks/${taskId}/mappings/${mid}/reject`, { method: 'POST' }),

  needsEtlMapping: (tid: string, taskId: string, mid: string) =>
    request<FieldMapping>(`/v1/tenants/${tid}/tasks/${taskId}/mappings/${mid}/needs-etl`, { method: 'POST' }),

  confirmAllMappings: (tid: string, taskId: string) =>
    request<{ task_id: string; status: string; message: string }>(
      `/v1/tenants/${tid}/tasks/${taskId}/mappings/confirm-all`, { method: 'POST' }),

  listMappingAssets: (tid: string, reportPackId?: string) =>
    request<{ total: number; assets: MappingAsset[] }>(
      `/v1/tenants/${tid}/mapping-assets${reportPackId ? `?report_pack_id=${encodeURIComponent(reportPackId)}` : ''}`),

  // 审计日志
  auditLogs: (tid: string, params: { page?: number; page_size?: number; action?: string; username?: string }) => {
    const qs = new URLSearchParams()
    if (params.page) qs.set('page', String(params.page))
    if (params.page_size) qs.set('page_size', String(params.page_size))
    if (params.action) qs.set('action', params.action)
    if (params.username) qs.set('username', params.username)
    return request<{ total: number; page: number; page_size: number; logs: AuditLogItem[] }>(
      `/v1/tenants/${tid}/audit-logs?${qs.toString()}`)
  },

  auditActions: (tid: string) =>
    request<{ actions: string[] }>(`/v1/tenants/${tid}/audit-logs/actions`),

  // ---------- P2 报送台账 ----------
  // 按月查询台账（period=YYYY-MM）
  listLedger: (tid: string, period: string) =>
    request<{ entries: LedgerEntry[] }>(
      `/v1/tenants/${tid}/ledger?period=${encodeURIComponent(period)}`),

  // 生成指定月份台账
  generateLedger: (tid: string, period: string) =>
    request<{ entries: LedgerEntry[] }>(`/v1/tenants/${tid}/ledger/generate`, {
      method: 'POST', body: JSON.stringify({ period }),
    }),

  // 标记台账条目已报送
  submitLedgerEntry: (tid: string, entryId: string) =>
    request<LedgerEntry>(`/v1/tenants/${tid}/ledger/${encodeURIComponent(entryId)}/submit`, { method: 'POST' }),

  // 台账条目关联生成任务
  bindLedgerTask: (tid: string, entryId: string, taskId: string) =>
    request<LedgerEntry>(`/v1/tenants/${tid}/ledger/${encodeURIComponent(entryId)}/bind-task`, {
      method: 'POST', body: JSON.stringify({ task_id: taskId }),
    }),

  // ---------- P2 监管接口文件输出 ----------
  // 生成接口文件（txt/xml），返回文件名/行数/预览
  exportInterfaceFile: (tid: string, taskId: string, format: 'txt' | 'xml') =>
    request<ExportFileResult>(`/v1/tenants/${tid}/tasks/${taskId}/export-interface-file`, {
      method: 'POST', body: JSON.stringify({ format }),
    }),

  // 已生成接口文件列表
  listTaskExports: (tid: string, taskId: string) =>
    request<{ files: ExportFileItem[] }>(`/v1/tenants/${tid}/tasks/${taskId}/exports`),

  // 接口文件下载（带认证头，返回 blob 触发浏览器保存）
  downloadExportFile: async (tid: string, taskId: string, fileName: string) => {
    const token = auth.getToken()
    const resp = await fetch(
      `${BASE}/v1/tenants/${tid}/tasks/${taskId}/exports/${encodeURIComponent(fileName)}`,
      { headers: token ? { Authorization: `Bearer ${token}` } : {} })
    if (resp.status === 401) {
      auth.clear()
      location.href = '/login'
      throw new Error('未认证或登录已过期')
    }
    if (!resp.ok) throw new Error(`下载失败: ${(await resp.text()).slice(0, 200)}`)
    const blob = await resp.blob()
    const url = URL.createObjectURL(blob)
    const a = document.createElement('a')
    a.href = url
    a.download = fileName
    a.click()
    URL.revokeObjectURL(url)
  },

  // 上传文档（multipart，不走 JSON 封装；需带认证头）
  uploadDocument: async (tid: string, file: File, docType: string) => {
    const form = new FormData()
    form.append('file', file)
    form.append('doc_type', docType)
    const token = auth.getToken()
    const resp = await fetch(`${BASE}/v1/tenants/${tid}/regulations/documents`, {
      method: 'POST',
      headers: token ? { Authorization: `Bearer ${token}` } : {},
      body: form,
    })
    if (resp.status === 401) {
      auth.clear()
      location.href = '/login'
      throw new Error('未认证或登录已过期')
    }
    if (!resp.ok) throw new Error(`上传失败: ${(await resp.text()).slice(0, 200)}`)
    return resp.json()
  },
}
