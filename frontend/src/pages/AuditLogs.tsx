/**
 * 审计日志页
 * 展示当前租户的操作留痕：时间/用户/动作/资源/结果，支持动作与用户名过滤、服务端分页、detail 展开
 */
import React, { useEffect, useState } from 'react'
import { Table, Tag, Select, Input, Space, Button, Typography } from 'antd'
import { ReloadOutlined, SearchOutlined } from '@ant-design/icons'
import { api, AuditLogItem } from '../api/client'
import { useTenant } from '../App'

const AuditLogs: React.FC = () => {
  const { tenantId } = useTenant()
  const [logs, setLogs] = useState<AuditLogItem[]>([])
  const [actions, setActions] = useState<string[]>([])
  const [total, setTotal] = useState(0)
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [action, setAction] = useState<string | undefined>()
  const [username, setUsername] = useState('')
  const [loading, setLoading] = useState(false)

  const load = async (p = page, ps = pageSize) => {
    if (!tenantId) return
    setLoading(true)
    try {
      const r = await api.auditLogs(tenantId, {
        page: p, page_size: ps,
        action: action || undefined,
        username: username || undefined,
      })
      setLogs(r.logs)
      setTotal(r.total)
    } finally {
      setLoading(false)
    }
  }

  // 租户切换时重置并加载
  useEffect(() => {
    setPage(1)
    setAction(undefined)
    setUsername('')
    api.auditActions(tenantId).then((r) => setActions(r.actions)).catch(() => {})
  }, [tenantId])

  useEffect(() => { load(page, pageSize) }, [tenantId, page, pageSize])

  const columns = [
    {
      title: '时间', dataIndex: 'timestamp', width: 180,
      render: (v: string) => (v ? new Date(v).toLocaleString('zh-CN') : '-'),
    },
    { title: '用户', dataIndex: 'username', width: 110, render: (v: string) => v || <Tag>匿名</Tag> },
    {
      title: '动作', dataIndex: 'action', width: 150,
      render: (v: string) => <Tag color="blue">{v}</Tag>,
    },
    { title: '资源', dataIndex: 'resource', ellipsis: true, render: (v: string) => v || '-' },
    { title: 'IP', dataIndex: 'ip', width: 110, render: (v: string) => v || '-' },
    {
      title: '结果', dataIndex: 'result', width: 80,
      render: (v: string) => <Tag color={v === 'success' ? 'success' : 'error'}>{v}</Tag>,
    },
    {
      title: '耗时', dataIndex: 'duration_ms', width: 90,
      render: (v?: number) => (v != null ? `${v}ms` : '-'),
    },
  ]

  return (
    <div>
      <Typography.Title level={4}>审计日志</Typography.Title>
      <Space style={{ marginBottom: 16 }} wrap>
        <Select
          allowClear
          placeholder="动作类型"
          style={{ width: 200 }}
          value={action}
          onChange={(v) => { setAction(v); setPage(1) }}
          options={actions.map((a) => ({ value: a, label: a }))}
        />
        <Input
          placeholder="用户名"
          style={{ width: 160 }}
          value={username}
          onChange={(e) => setUsername(e.target.value)}
          onPressEnter={() => { setPage(1); load(1, pageSize) }}
        />
        <Button
          type="primary"
          icon={<SearchOutlined />}
          onClick={() => { setPage(1); load(1, pageSize) }}
        >
          查询
        </Button>
        <Button icon={<ReloadOutlined />} onClick={() => load(page, pageSize)}>
          刷新
        </Button>
      </Space>
      <Table<AuditLogItem>
        rowKey="id"
        size="small"
        loading={loading}
        columns={columns as any}
        dataSource={logs}
        expandable={{
          expandedRowRender: (r) => (
            <pre style={{ margin: 0, fontSize: 12 }}>
              {JSON.stringify({ trace_id: r.trace_id, detail: r.detail }, null, 2)}
            </pre>
          ),
        }}
        pagination={{
          current: page,
          pageSize,
          total,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 条`,
          onChange: (p, ps) => { setPage(p); setPageSize(ps) },
        }}
      />
    </div>
  )
}

export default AuditLogs
