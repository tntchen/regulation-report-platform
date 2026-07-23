import React, { useCallback, useEffect, useState } from 'react'
import { Card, DatePicker, Button, Table, Tag, Space, message, Modal, Select, Tooltip } from 'antd'
import { ReloadOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import dayjs, { Dayjs } from 'dayjs'
import { api, LedgerEntry } from '../api/client'
import { useTenant } from '../App'

/** 状态 Tag 配色：pending 灰 / in_progress 蓝 / submitted 绿 / overdue 红 */
const STATUS_TAG: Record<string, { color: string; text: string }> = {
  pending: { color: 'default', text: '待处理' },
  in_progress: { color: 'processing', text: '进行中' },
  submitted: { color: 'success', text: '已报送' },
  overdue: { color: 'error', text: '已逾期' },
}

/** P8 报送台账页：按月查看报送截止期、关联任务、标记报送 */
const SubmissionLedger: React.FC = () => {
  const { tenantId } = useTenant()
  const navigate = useNavigate()
  const [period, setPeriod] = useState<Dayjs>(dayjs())       // 默认当月
  const [entries, setEntries] = useState<LedgerEntry[]>([])
  const [loading, setLoading] = useState(false)
  const [generating, setGenerating] = useState(false)
  // 关联任务弹窗状态
  const [bindEntry, setBindEntry] = useState<LedgerEntry | null>(null)
  const [taskOptions, setTaskOptions] = useState<{ value: string; label: string }[]>([])
  const [bindTaskId, setBindTaskId] = useState<string>()

  const periodStr = period.format('YYYY-MM')

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.listLedger(tenantId, periodStr)
      setEntries(r.entries)
    } catch (e: any) {
      message.error(`台账加载失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }, [tenantId, periodStr])

  useEffect(() => { load() }, [load])

  const generate = async () => {
    setGenerating(true)
    try {
      const r = await api.generateLedger(tenantId, periodStr)
      message.success(`已生成 ${periodStr} 台账，共 ${r.entries.length} 条`)
      await load()
    } catch (e: any) {
      message.error(`生成台账失败: ${e.message}`)
    } finally {
      setGenerating(false)
    }
  }

  const submit = async (entry: LedgerEntry) => {
    try {
      await api.submitLedgerEntry(tenantId, entry.id)
      message.success(`已标记报送：${entry.report_name}`)
      await load()
    } catch (e: any) {
      message.error(`标记报送失败: ${e.message}`)
    }
  }

  // 打开关联任务弹窗：加载本租户任务列表供选择
  const openBind = async (entry: LedgerEntry) => {
    setBindEntry(entry)
    setBindTaskId(undefined)
    try {
      const r = await api.listTasks(tenantId)
      setTaskOptions(r.tasks.map((t) => ({ value: t.task_id, label: `${t.task_id} ${t.name}（${t.status}）` })))
    } catch (e: any) {
      message.error(`任务列表加载失败: ${e.message}`)
    }
  }

  const doBind = async () => {
    if (!bindEntry || !bindTaskId) return
    try {
      await api.bindLedgerTask(tenantId, bindEntry.id, bindTaskId)
      message.success('已关联任务')
      setBindEntry(null)
      await load()
    } catch (e: any) {
      message.error(`关联任务失败: ${e.message}`)
    }
  }

  return (
    <Card
      title="P8 报送台账"
      extra={
        <Space>
          <DatePicker
            picker="month"
            value={period}
            allowClear={false}
            onChange={(d) => d && setPeriod(d)}
            format="YYYY-MM"
          />
          <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          <Button type="primary" loading={generating} onClick={generate}>生成台账</Button>
        </Space>
      }
    >
      <Table<LedgerEntry>
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={entries}
        locale={{ emptyText: '本月暂无台账，点击「生成台账」创建' }}
        columns={[
          { title: '报表', dataIndex: 'report_name', width: 220 },
          { title: '场景包', dataIndex: 'report_pack_id', width: 140 },
          { title: '期间', dataIndex: 'period', width: 90 },
          { title: '截止日', dataIndex: 'deadline', width: 110 },
          {
            title: '剩余天数', dataIndex: 'days_left', width: 100,
            sorter: (a, b) => a.days_left - b.days_left,
            render: (v: number, r) =>
              // 剩余 ≤2 天且未报送时红色警示
              r.status !== 'submitted' && v <= 2
                ? <span style={{ color: '#cf1322', fontWeight: 600 }}>{v} 天</span>
                : <span>{v} 天</span>,
          },
          {
            title: '状态', dataIndex: 'status', width: 100,
            filters: Object.entries(STATUS_TAG).map(([k, v]) => ({ text: v.text, value: k })),
            onFilter: (v, r) => r.status === v,
            render: (s: string) => {
              const t = STATUS_TAG[s] || { color: 'default', text: s }
              return <Tag color={t.color}>{t.text}</Tag>
            },
          },
          {
            title: '关联任务', dataIndex: 'task_id', width: 160,
            render: (v: string | undefined) =>
              v ? <a onClick={() => navigate(`/execute/${v}`)}>{v}</a> : <span style={{ color: '#bbb' }}>-</span>,
          },
          {
            title: '报送时间', dataIndex: 'submitted_at', width: 170,
            render: (v?: string) => v ? dayjs(v).format('YYYY-MM-DD HH:mm') : '-',
          },
          {
            title: '操作', key: 'op', width: 180, fixed: 'right',
            render: (_, r) => (
              <Space>
                {r.status !== 'submitted' && (
                  <Tooltip title="确认该报表已完成报送">
                    <Button size="small" onClick={() => submit(r)}>标记报送</Button>
                  </Tooltip>
                )}
                <Button size="small" onClick={() => openBind(r)}>关联任务</Button>
              </Space>
            ),
          },
        ]}
      />

      <Modal
        title={`关联任务：${bindEntry?.report_name ?? ''}`}
        open={!!bindEntry}
        onOk={doBind}
        okButtonProps={{ disabled: !bindTaskId }}
        onCancel={() => setBindEntry(null)}
      >
        <Select
          style={{ width: '100%' }}
          showSearch
          optionFilterProp="label"
          placeholder="选择要关联的生成任务"
          value={bindTaskId}
          onChange={setBindTaskId}
          options={taskOptions}
        />
      </Modal>
    </Card>
  )
}

export default SubmissionLedger
