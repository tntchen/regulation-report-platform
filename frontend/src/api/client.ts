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

export interface AuditLogItem {
  id: number; timestamp: string; trace_id: string; username?: string
  tenant_id?: string; action: string; resource?: string
  detail: Record<string, any>; ip?: string; result: string; duration_ms?: number
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
