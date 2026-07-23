import React, { useEffect, useRef, useState } from 'react'
import { Table, Button, Tag, Modal, Form, Select, Input, message, Space, Card, Progress, Popconfirm } from 'antd'
import { PlusOutlined, RocketOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { api, TaskBrief } from '../api/client'
import { useTenant } from '../App'

// 预置报表类型模板（与后端 Mock 演示数据对齐）
const REPORT_TEMPLATES = [
  {
    label: 'EAST 个人信贷借据（住房贷款）',
    value: 'EAST_LOAN',
    payload: {
      report_type: 'EAST', report_code: 'EAST_LOAN_01', section: '个人住房贷款',
      source_tables: ['loan_contract'], target_table: 'rpt_east_housing_loan',
      output_mode: 'sql', dialect: 'mysql',
    },
  },
  {
    label: '1104 G01 个人住房贷款',
    value: '1104_G01',
    payload: {
      report_type: '1104', report_code: 'G01', section: '个人住房贷款',
      source_tables: ['loan_contract'], target_table: 'rpt_g01_housing_loan',
      output_mode: 'sql', dialect: 'mysql',
    },
  },
]

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  queued: { color: 'gold', text: '排队中' },
  executing: { color: 'processing', text: '执行中' },
  completed: { color: 'success', text: '已完成' },
  failed: { color: 'error', text: '失败' },
  cancelled: { color: 'default', text: '已取消' },
  created: { color: 'default', text: '已创建' },
}

/** P1 任务大厅：任务列表 + 新建任务 + 一键演示 */
const TaskHall: React.FC = () => {
  const { tenantId } = useTenant()
  const [tasks, setTasks] = useState<TaskBrief[]>([])
  const [loading, setLoading] = useState(false)
  const [modalOpen, setModalOpen] = useState(false)
  const [creating, setCreating] = useState(false)
  const [form] = Form.useForm()
  const navigate = useNavigate()
  // 幂等键：每次打开"新建任务"弹窗生成一次，弹窗内重复点击/重试都返回同一任务
  const clientRequestId = useRef<string>('')
  const openCreateModal = () => {
    clientRequestId.current = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    setModalOpen(true)
  }

  const load = async () => {
    setLoading(true)
    try {
      const r = await api.listTasks(tenantId)
      setTasks(r.tasks)
    } catch (e: any) {
      message.error(`任务列表加载失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [tenantId])

  // 创建任务并跳转执行页（每次打开弹窗生成一个幂等键，防止重复提交产生重复任务）
  const createTask = async (values: any) => {
    setCreating(true)
    try {
      const tpl = REPORT_TEMPLATES.find((t) => t.value === values.template)!
      const payload = { ...tpl.payload, client_request_id: clientRequestId.current }
      if (values.target_table) payload.target_table = values.target_table
      const r = await api.createTask(tenantId, payload)
      message.success('任务已排队，后台执行中')
      setModalOpen(false)
      navigate(`/execute/${r.task_id}`)
    } catch (e: any) {
      message.error(`任务创建失败: ${e.message}`)
    } finally {
      setCreating(false)
    }
  }

  // 取消任务（queued 立即 cancelled；executing 在阶段边界优雅终止）
  const cancelTask = async (taskId: string) => {
    try {
      const r = await api.cancelTask(tenantId, taskId)
      message.success(r.message)
      load()
    } catch (e: any) {
      message.error(`取消失败: ${e.message}`)
    }
  }

  const columns = [
    { title: '任务编号', dataIndex: 'task_id', key: 'task_id', width: 220 },
    { title: '任务名称', dataIndex: 'name', key: 'name' },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 100,
      render: (s: string) => <Tag color={STATUS_TAG[s]?.color}>{STATUS_TAG[s]?.text || s}</Tag>,
    },
    {
      title: '进度', dataIndex: 'progress', key: 'progress', width: 140,
      render: (p: number) => <Progress percent={p} size="small" />,
    },
    { title: '当前阶段', dataIndex: 'current_stage', key: 'current_stage', width: 140 },
    {
      title: '耗时', dataIndex: 'duration_ms', key: 'duration_ms', width: 100,
      render: (d: number) => (d ? `${(d / 1000).toFixed(1)}s` : '-'),
    },
    { title: '创建时间', dataIndex: 'created_at', key: 'created_at', width: 180,
      render: (t: string) => (t ? t.replace('T', ' ').slice(0, 19) : '-') },
    {
      title: '操作', key: 'op', width: 260,
      render: (_: any, r: TaskBrief) => (
        <Space>
          <Button size="small" type="link" onClick={() => navigate(`/execute/${r.task_id}`)}>执行</Button>
          <Button size="small" type="link" onClick={() => navigate(`/quality/${r.task_id}`)}>校验报告</Button>
          <Button size="small" type="link" onClick={() => navigate(`/twin/${r.task_id}`)}>孪生对比</Button>
          {(r.status === 'queued' || r.status === 'executing') && (
            <Popconfirm title="确认取消该任务？" onConfirm={() => cancelTask(r.task_id)}>
              <Button size="small" type="link" danger>取消</Button>
            </Popconfirm>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="任务大厅"
      extra={
        <Space>
          <Button onClick={load}>刷新</Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={openCreateModal}>
            新建任务
          </Button>
        </Space>
      }
    >
      <Table
        rowKey="task_id"
        loading={loading}
        columns={columns}
        dataSource={tasks}
        pagination={{ pageSize: 10 }}
        locale={{ emptyText: '暂无任务，点击"新建任务"开始演示' }}
      />

      <Modal
        title="新建报送任务"
        open={modalOpen}
        onCancel={() => setModalOpen(false)}
        onOk={() => form.submit()}
        confirmLoading={creating}
        okText="创建并执行"
      >
        <Form form={form} layout="vertical" onFinish={createTask}
          initialValues={{ template: 'EAST_LOAN' }}>
          <Form.Item name="template" label="报表类型" rules={[{ required: true }]}>
            <Select options={REPORT_TEMPLATES.map((t) => ({ value: t.value, label: t.label }))} />
          </Form.Item>
          <Form.Item name="target_table" label="目标表（可选，默认随模板）">
            <Input placeholder="如 rpt_east_housing_loan" />
          </Form.Item>
          <Form.Item label="数据源">
            <Input value="零售信贷主库 loan_contract（演示数据集）" disabled />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}

export default TaskHall
