/**
 * API 客户端封装
 * 统一封装后端 REST 调用；开发环境经 vite proxy 转发到 localhost:8080
 */

const BASE = ''

async function request<T>(path: string, options: RequestInit = {}): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { 'Content-Type': 'application/json' },
    ...options,
  })
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
}

// ---------- API 方法 ----------

export const api = {
  health: () => request<{ status: string; version: string }>('/health'),

  listTenants: () => request<{ tenants: TenantBrief[] }>('/v1/tenants'),

  listTasks: (tid: string) =>
    request<{ total: number; tasks: TaskBrief[] }>(`/v1/tenants/${tid}/tasks`),

  createTask: (tid: string, payload: Record<string, any>) =>
    request<TaskDetail & { task_id: string }>(`/v1/tenants/${tid}/tasks`, {
      method: 'POST', body: JSON.stringify(payload),
    }),

  getTask: (tid: string, taskId: string) =>
    request<TaskDetail>(`/v1/tenants/${tid}/tasks/${taskId}`),

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

  // 上传文档（multipart，不走 JSON 封装）
  uploadDocument: async (tid: string, file: File, docType: string) => {
    const form = new FormData()
    form.append('file', file)
    form.append('doc_type', docType)
    const resp = await fetch(`${BASE}/v1/tenants/${tid}/regulations/documents`, {
      method: 'POST', body: form,
    })
    if (!resp.ok) throw new Error(`上传失败: ${(await resp.text()).slice(0, 200)}`)
    return resp.json()
  },
}
