import React, { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import {
  Card, Tag, Button, Space, Progress, Input, message, Tooltip, Empty, Popconfirm, Alert, Typography,
} from 'antd'
import {
  CheckOutlined, EditOutlined, CloseOutlined, ToolOutlined, RocketOutlined, ArrowLeftOutlined,
} from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import { api, FieldMapping, MappingEvidence, TaskDetail } from '../api/client'
import { useTenant } from '../App'

// 映射状态 → 展示（终态：confirmed/modified/needs_etl；unmapped/rejected 必须处理）
const STATUS_META: Record<string, { color: string; text: string; terminal: boolean }> = {
  ai_inferred: { color: 'processing', text: 'AI 推断', terminal: false },
  confirmed:   { color: 'success',    text: '已确认',  terminal: true },
  modified:    { color: 'geekblue',   text: '已修改',  terminal: true },
  rejected:    { color: 'error',      text: '已拒绝',  terminal: false },
  unmapped:    { color: 'error',      text: '未映射',  terminal: false },
  needs_etl:   { color: 'warning',    text: '需 ETL',  terminal: true },
}

// 五通道元信息
const CHANNELS: { key: keyof MappingEvidence; label: string; weight: string }[] = [
  { key: 'name', label: '名称相似', weight: '0.2' },
  { key: 'comment', label: '注释语义', weight: '0.2' },
  { key: 'profile', label: '数据画像', weight: '0.3' },
  { key: 'semantic', label: '制度语义', weight: '0.2' },
  { key: 'history', label: '历史命中', weight: '0.1' },
]

/** 连线样式：实线=高置信 / 虚线=待确认 / 红色=未映射或已拒绝 */
function lineStyle(m: FieldMapping): { stroke: string; dash?: string } {
  const noSource = !m.source_table || !m.source_field
  if (noSource || m.status === 'unmapped' || m.status === 'rejected') {
    return { stroke: '#f5222d', dash: '6 4' }
  }
  if (m.confidence >= 0.85 || m.status === 'confirmed' || m.status === 'modified' || m.status === 'needs_etl') {
    return { stroke: m.status === 'ai_inferred' ? '#2f54eb' : '#52c41a' }
  }
  return { stroke: '#fa8c16', dash: '6 4' }
}

interface Line { x1: number; y1: number; x2: number; y2: number; key: string }

/** P6 映射工作台：目标字段 ↔ 候选源字段连线 + 五通道证据详情 + 人工确认 */
const MappingWorkbench: React.FC = () => {
  const { taskId } = useParams<{ taskId: string }>()
  const { tenantId } = useTenant()
  const navigate = useNavigate()
  const [task, setTask] = useState<TaskDetail | null>(null)
  const [mappings, setMappings] = useState<FieldMapping[]>([])
  const [loading, setLoading] = useState(false)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [lines, setLines] = useState<Line[]>([])
  // 详情面板编辑态
  const [editRule, setEditRule] = useState('')
  const [editTable, setEditTable] = useState('')
  const [editField, setEditField] = useState('')
  const [acting, setActing] = useState(false)

  const containerRef = useRef<HTMLDivElement>(null)
  const leftRefs = useRef<Record<string, HTMLDivElement | null>>({})
  const rightRefs = useRef<Record<string, HTMLDivElement | null>>({})

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const [t, r] = await Promise.all([
        api.getTask(tenantId, taskId!),
        api.listTaskMappings(tenantId, taskId!),
      ])
      setTask(t)
      setMappings(r.mappings)
      if (!selectedId && r.mappings.length > 0) setSelectedId(r.mappings[0].id)
    } catch (e: any) {
      message.error(`映射清单加载失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }, [tenantId, taskId])

  useEffect(() => { load() }, [load])

  const selected = mappings.find((m) => m.id === selectedId) || null

  // 选中项变化时同步编辑态
  useEffect(() => {
    if (selected) {
      setEditRule(selected.transform_rule || 'DIRECT')
      setEditTable(selected.source_table || '')
      setEditField(selected.source_field || '')
    }
  }, [selectedId, mappings])

  // 计算连线坐标（DOM 测量，CSS/SVG 自绘，无图库依赖）
  const measure = useCallback(() => {
    const box = containerRef.current
    if (!box) return
    const base = box.getBoundingClientRect()
    const next: Line[] = []
    for (const m of mappings) {
      const l = leftRefs.current[m.id]
      const r = rightRefs.current[m.id]
      if (!l || !r) continue
      const lb = l.getBoundingClientRect()
      const rb = r.getBoundingClientRect()
      next.push({
        key: m.id,
        x1: lb.right - base.left,
        y1: lb.top - base.top + lb.height / 2,
        x2: rb.left - base.left,
        y2: rb.top - base.top + rb.height / 2,
      })
    }
    setLines(next)
  }, [mappings])

  useLayoutEffect(() => { measure() }, [measure])
  useEffect(() => {
    window.addEventListener('resize', measure)
    return () => window.removeEventListener('resize', measure)
  }, [measure])

  // 终态统计：confirmed / modified / needs_etl 视为已处理
  const terminalCount = mappings.filter((m) => STATUS_META[m.status]?.terminal).length
  const allTerminal = mappings.length > 0 && terminalCount === mappings.length

  // ---- 操作动作（每次操作后整体刷新，保证与后端状态一致） ----
  const doAction = async (fn: () => Promise<any>, ok: string) => {
    setActing(true)
    try {
      await fn()
      message.success(ok)
      await load()
    } catch (e: any) {
      message.error(e.message)
    } finally {
      setActing(false)
    }
  }

  const onConfirm = () => doAction(
    () => api.confirmMapping(tenantId, taskId!, selected!.id, editRule !== selected!.transform_rule ? editRule : undefined),
    '映射已确认')

  const onModify = () => {
    if (!editTable.trim() || !editField.trim()) {
      message.warning('修改映射需填写源表与源字段')
      return
    }
    doAction(
      () => api.modifyMapping(tenantId, taskId!, selected!.id, {
        source_table: editTable.trim(), source_field: editField.trim(), transform_rule: editRule,
      }),
      '映射已修改并确认')
  }

  const onReject = () => doAction(
    () => api.rejectMapping(tenantId, taskId!, selected!.id), '已拒绝 AI 推断')

  const onNeedsEtl = () => doAction(
    () => api.needsEtlMapping(tenantId, taskId!, selected!.id), '已标记需 ETL 加工')

  const onConfirmAll = () => doAction(async () => {
    const r = await api.confirmAllMappings(tenantId, taskId!)
    message.success(r.message || '全部映射已确认，任务已恢复执行')
    navigate(`/execute/${taskId}`)
  }, '')

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={12}>
      <Card
        size="small"
        title={
          <Space>
            <Button icon={<ArrowLeftOutlined />} type="text" onClick={() => navigate(`/execute/${taskId}`)} />
            映射工作台
            <Typography.Text type="secondary" style={{ fontWeight: 'normal', fontSize: 12 }}>
              任务 {taskId}{task?.outputs?.regulation_parser?.report_pack_id ? ` · 场景包 ${task.outputs.regulation_parser.report_pack_id}` : ''}
            </Typography.Text>
          </Space>
        }
        extra={
          <Space size={16}>
            <span>
              已确认 <b style={{ color: allTerminal ? '#52c41a' : '#fa8c16' }}>{terminalCount}</b> / {mappings.length}
            </span>
            <Progress
              percent={mappings.length ? Math.round((terminalCount / mappings.length) * 100) : 0}
              size="small" style={{ width: 140, marginBottom: 0 }}
              status={allTerminal ? 'success' : 'active'}
            />
            <Popconfirm
              title="确认全部映射并继续执行任务？"
              description="任务将从断点恢复，进入代码生成阶段"
              onConfirm={onConfirmAll}
              disabled={!allTerminal}
            >
              <Button type="primary" icon={<RocketOutlined />} disabled={!allTerminal} loading={acting}>
                全部确认并继续执行
              </Button>
            </Popconfirm>
          </Space>
        }
      >
        {task?.status === 'waiting_confirmation' && (
          <Alert type="warning" showIcon style={{ marginBottom: 4 }}
            message="任务已挂起等待映射确认：请逐项确认/修改/标记，全部有终态后点击右上角继续执行" />
        )}
      </Card>

      {/* 连线区：左列目标字段 / 右列候选源字段 */}
      <Card size="small" loading={loading && mappings.length === 0}>
        {mappings.length === 0 && !loading ? (
          <Empty description="暂无映射数据（映射推断阶段完成后生成）" />
        ) : (
          <div ref={containerRef} style={{ position: 'relative' }}>
            {/* SVG 连线层 */}
            <svg style={{ position: 'absolute', inset: 0, width: '100%', height: '100%', pointerEvents: 'none', zIndex: 1 }}>
              {lines.map((ln) => {
                const m = mappings.find((x) => x.id === ln.key)!
                const st = lineStyle(m)
                const mx = (ln.x1 + ln.x2) / 2
                return (
                  <path
                    key={ln.key}
                    d={`M ${ln.x1} ${ln.y1} C ${mx} ${ln.y1}, ${mx} ${ln.y2}, ${ln.x2} ${ln.y2}`}
                    fill="none"
                    stroke={st.stroke}
                    strokeWidth={selectedId === ln.key ? 2.5 : 1.5}
                    strokeDasharray={st.dash}
                    opacity={selectedId && selectedId !== ln.key ? 0.35 : 0.9}
                  />
                )
              })}
            </svg>
            <div style={{ display: 'flex', gap: 80, position: 'relative', zIndex: 2 }}>
              {/* 左列：目标字段 */}
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ color: '#999', fontSize: 12 }}>目标字段（{mappings.length}）</div>
                {mappings.map((m) => {
                  const meta = STATUS_META[m.status] || { color: 'default', text: m.status, terminal: false }
                  return (
                    <div
                      key={m.id}
                      ref={(el) => { leftRefs.current[m.id] = el }}
                      onClick={() => setSelectedId(m.id)}
                      style={{
                        border: `1px solid ${selectedId === m.id ? '#2f54eb' : '#e8e8e8'}`,
                        borderRadius: 6, padding: '8px 12px', cursor: 'pointer', background: '#fff',
                        boxShadow: selectedId === m.id ? '0 0 6px #2f54eb44' : undefined,
                      }}
                    >
                      <Space style={{ width: '100%', justifyContent: 'space-between' }}>
                        <b>{m.target_field}</b>
                        <Tag color={meta.color} style={{ marginRight: 0 }}>{meta.text}</Tag>
                      </Space>
                      {m.caliber_text && (
                        <Tooltip title={m.caliber_text}>
                          <div style={{ color: '#999', fontSize: 12, marginTop: 4, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                            口径：{m.caliber_text}
                          </div>
                        </Tooltip>
                      )}
                    </div>
                  )
                })}
              </div>
              {/* 右列：候选源字段 */}
              <div style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 10 }}>
                <div style={{ color: '#999', fontSize: 12 }}>候选源字段（实线=高置信 / 虚线=待确认 / 红色=未映射）</div>
                {mappings.map((m) => (
                  <div
                    key={m.id}
                    ref={(el) => { rightRefs.current[m.id] = el }}
                    onClick={() => setSelectedId(m.id)}
                    style={{
                      border: `1px solid ${selectedId === m.id ? '#2f54eb' : '#e8e8e8'}`,
                      borderRadius: 6, padding: '8px 12px', cursor: 'pointer', background: '#fff',
                    }}
                  >
                    {m.source_table && m.source_field ? (
                      <Space style={{ width: '100%', justifyContent: 'space-between' }}>
                        <span>
                          <b>{m.source_table}.{m.source_field}</b>
                          <Tag style={{ marginLeft: 8 }} color={m.confidence >= 0.85 ? 'green' : m.confidence >= 0.5 ? 'orange' : 'red'}>
                            {(m.confidence * 100).toFixed(0)}%
                          </Tag>
                        </span>
                        <span style={{ color: '#999', fontSize: 12 }}>{m.transform_rule}</span>
                      </Space>
                    ) : (
                      <span style={{ color: '#f5222d' }}>未找到候选源字段</span>
                    )}
                    {(m.candidates || []).length > 1 && (
                      <div style={{ color: '#999', fontSize: 12, marginTop: 4 }}>
                        其他候选：{m.candidates!.slice(1, 4).map((c) => `${c.source_table}.${c.source_field}(${(c.confidence * 100).toFixed(0)}%)`).join('、')}
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          </div>
        )}
      </Card>

      {/* 底部详情面板 */}
      {selected && (
        <Card
          size="small"
          title={
            <Space>
              映射详情：<b>{selected.target_field}</b>
              {selected.source_table && <span>→ {selected.source_table}.{selected.source_field}</span>}
              <Tag color={STATUS_META[selected.status]?.color}>{STATUS_META[selected.status]?.text || selected.status}</Tag>
              <Tag>综合置信度 {(selected.confidence * 100).toFixed(0)}%</Tag>
            </Space>
          }
        >
          <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap' }}>
            {/* 五通道证据条形 */}
            <div style={{ minWidth: 260, flex: 1 }}>
              <div style={{ color: '#999', fontSize: 12, marginBottom: 8 }}>五通道证据</div>
              {CHANNELS.map((c) => {
                const v = selected.evidence?.[c.key]
                return (
                  <div key={c.key} style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
                    <span style={{ width: 90, fontSize: 12 }}>
                      {c.label} <span style={{ color: '#bbb' }}>×{c.weight}</span>
                    </span>
                    {v === null || v === undefined ? (
                      <span style={{ color: '#bbb', fontSize: 12 }}>无数据（不计入权重）</span>
                    ) : (
                      <>
                        <Progress
                          percent={Math.round(v * 100)} size="small" style={{ flex: 1, marginBottom: 0 }}
                          strokeColor={v >= 0.85 ? '#52c41a' : v >= 0.5 ? '#fa8c16' : '#f5222d'}
                        />
                      </>
                    )}
                  </div>
                )
              })}
            </div>
            {/* 源字段画像 */}
            <div style={{ minWidth: 240, flex: 1 }}>
              <div style={{ color: '#999', fontSize: 12, marginBottom: 8 }}>源字段画像</div>
              {selected.profile ? (
                <div style={{ fontSize: 12, lineHeight: 1.9 }}>
                  <div>空值率：{selected.profile.null_rate !== undefined ? `${(selected.profile.null_rate * 100).toFixed(1)}%` : '-'}</div>
                  <div> distinct 数：{selected.profile.distinct_count ?? '-'}</div>
                  {selected.profile.format_pattern && <div>格式识别：<Tag>{selected.profile.format_pattern}</Tag></div>}
                  {(selected.profile.enum_values || []).length > 0 && (
                    <div>枚举值：{selected.profile.enum_values!.slice(0, 8).map((v) => <Tag key={String(v)}>{String(v)}</Tag>)}</div>
                  )}
                  {(selected.profile.sample_values || []).length > 0 && (
                    <div style={{ color: '#666' }}>
                      样例值：{selected.profile.sample_values!.slice(0, 5).map((v) => String(v)).join('、')}
                    </div>
                  )}
                </div>
              ) : (
                <span style={{ color: '#bbb', fontSize: 12 }}>暂无画像数据</span>
              )}
            </div>
            {/* 转换规则 + 操作 */}
            <div style={{ minWidth: 320, flex: 1.4 }}>
              <div style={{ color: '#999', fontSize: 12, marginBottom: 8 }}>源字段 / 转换规则</div>
              <Space size={8} style={{ marginBottom: 8 }}>
                <Input addonBefore="源表" value={editTable} onChange={(e) => setEditTable(e.target.value)}
                  placeholder="loan_contract" style={{ width: 220 }} />
                <Input addonBefore="字段" value={editField} onChange={(e) => setEditField(e.target.value)}
                  placeholder="loan_amt" style={{ width: 200 }} />
              </Space>
              <Input.TextArea
                value={editRule}
                onChange={(e) => setEditRule(e.target.value)}
                rows={3}
                placeholder='DIRECT 或 SQL 表达式，如 CASE WHEN overdue_days > 90 THEN "次级" ... END'
                style={{ fontFamily: 'monospace', marginBottom: 8 }}
              />
              <Space wrap>
                <Button type="primary" icon={<CheckOutlined />} loading={acting} onClick={onConfirm}
                  disabled={!selected.source_table}>确认</Button>
                <Button icon={<EditOutlined />} loading={acting} onClick={onModify}>修改并确认</Button>
                <Popconfirm title="拒绝该 AI 推断？需重新指定源字段" onConfirm={onReject}>
                  <Button danger icon={<CloseOutlined />} loading={acting}>拒绝</Button>
                </Popconfirm>
                <Button icon={<ToolOutlined />} loading={acting} onClick={onNeedsEtl}
                  disabled={!selected.source_table}>需 ETL</Button>
              </Space>
            </div>
          </div>
        </Card>
      )}
    </Space>
  )
}

export default MappingWorkbench
