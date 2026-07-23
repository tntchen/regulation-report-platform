import React, { useEffect, useRef, useState } from 'react'
import { Table, Button, Tag, Modal, Form, Select, Input, message, Space, Card, Progress, Popconfirm, Alert, List, Typography } from 'antd'
import { PlusOutlined, NodeIndexOutlined, HistoryOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { api, TaskBrief, ReportPack, SimilarTask } from '../api/client'
import { useTenant } from '../App'

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  queued: { color: 'gold', text: '排队中' },
  executing: { color: 'processing', text: '执行中' },
  waiting_confirmation: { color: 'gold', text: '待确认映射' },
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
  const [packs, setPacks] = useState<ReportPack[]>([])
  const [form] = Form.useForm()
  const navigate = useNavigate()
  // 幂等键：每次打开"新建任务"弹窗生成一次，弹窗内重复点击/重试都返回同一任务
  const clientRequestId = useRef<string>('')
  // 相似历史任务：选定场景包后拉取推荐，有结果时在弹窗内提示
  const [similarTasks, setSimilarTasks] = useState<SimilarTask[]>([])
  const [similarLoading, setSimilarLoading] = useState(false)

  const openCreateModal = () => {
    clientRequestId.current = `web-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    setSimilarTasks([])
    setModalOpen(true)
    // 场景包下拉从后端加载（替换原硬编码模板），缺省 G01
    api.listReportPacks(tenantId)
      .then((r) => setPacks(r.packs.filter((p) => p.status !== 'disabled')))
      .catch((e) => message.warning(`场景包加载失败: ${e.message}`))
    // 弹窗缺省场景包也预取一次相似任务
    onPackChange(form.getFieldValue('report_pack_id') || 'G01')
  }

  // 场景包选择变化时查询相似历史任务（失败静默降级为不提示）
  const onPackChange = async (packId: string) => {
    setSimilarLoading(true)
    try {
      const r = await api.recommendTasks(tenantId, packId)
      setSimilarTasks(r.similar_tasks || [])
    } catch {
      setSimilarTasks([])
    } finally {
      setSimilarLoading(false)
    }
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
      // 场景包驱动：后端按 report_pack_id 读取目标结构/源表/勾稽规则（缺省 G01 保持兼容）
      const payload: Record<string, any> = {
        report_pack_id: values.report_pack_id || 'G01',
        client_request_id: clientRequestId.current,
      }
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
          {r.status === 'waiting_confirmation' && (
            <Button size="small" type="link" icon={<NodeIndexOutlined />}
              style={{ color: '#d48806' }}
              onClick={() => navigate(`/mapping/${r.task_id}`)}>
              确认映射
            </Button>
          )}
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
          initialValues={{ report_pack_id: 'G01' }}>
          <Form.Item name="report_pack_id" label="场景包" rules={[{ required: true }]}
            extra="报表定义数据化：目标结构/候选源表/勾稽规则由场景包驱动">
            <Select
              loading={packs.length === 0}
              onChange={onPackChange}
              options={packs.map((p) => ({
                value: p.id,
                label: `${p.id} ${p.report_name}（${p.report_type}）`,
                disabled: p.status === 'draft',
              }))}
            />
          </Form.Item>
          {similarTasks.length > 0 && (
            <Alert
              type="info" showIcon icon={<HistoryOutlined />}
              style={{ marginBottom: 16 }}
              message={`发现 ${similarTasks.length} 个历史相似任务，可参考其执行情况`}
              description={
                <List
                  size="small" loading={similarLoading}
                  dataSource={similarTasks}
                  renderItem={(t: SimilarTask) => (
                    <List.Item style={{ padding: '4px 0' }}>
                      <Space size={8} wrap>
                        <Typography.Text code style={{ fontSize: 12 }}>{t.task_id}</Typography.Text>
                        <Tag color={STATUS_TAG[t.status]?.color}>{STATUS_TAG[t.status]?.text || t.status}</Tag>
                        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                          {t.created_at ? t.created_at.replace('T', ' ').slice(0, 19) : '-'}
                        </Typography.Text>
                        <Tag color="blue">相似度 {(t.similarity * 100).toFixed(0)}%</Tag>
                        {t.summary && (
                          <Typography.Text type="secondary" style={{ fontSize: 12 }}>{t.summary}</Typography.Text>
                        )}
                      </Space>
                    </List.Item>
                  )}
                />
              }
            />
          )}
          <Form.Item name="target_table" label="目标表（可选，默认随场景包）">
            <Input placeholder="如 rpt_g01_housing_loan" />
          </Form.Item>
          <Form.Item label="数据源">
            <Input value="由场景包 source_tables 决定（演示数据集）" disabled />
          </Form.Item>
        </Form>
      </Modal>
    </Card>
  )
}

export default TaskHall
