import React, { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Card, Table, Tag, Button, Space, Drawer, Descriptions, Modal, Form, Input, Select,
  message, Typography, Divider, Popconfirm, InputNumber, Checkbox, Tabs, Empty, Spin,
} from 'antd'
import { PlusOutlined, EditOutlined, DeleteOutlined, ReloadOutlined } from '@ant-design/icons'
import { api, ReportPack, TargetSchemaField, ReconciliationRule, TableProfile } from '../api/client'
import { useTenant } from '../App'

const PACK_STATUS: Record<string, { color: string; text: string }> = {
  active: { color: 'success', text: '启用' },
  draft: { color: 'gold', text: '草稿' },
  disabled: { color: 'default', text: '停用' },
}

/** 当前用户是否 admin（写操作前端隐藏，后端仍强制鉴权） */
function isAdmin(): boolean {
  try {
    const u = JSON.parse(localStorage.getItem('current_user') || 'null')
    return u?.role === 'admin'
  } catch {
    return false
  }
}

const EMPTY_FIELD: TargetSchemaField = { field: '', data_type: 'DECIMAL(18,2)', required: true, caliber_text: '' }

/** 详情抽屉 · 源表探查 Tab：选源表 → 全字段画像（空值率/去重数/格式/枚举/样例值） */
const ProfileTab: React.FC<{ tenantId: string; pack: ReportPack }> = ({ tenantId, pack }) => {
  const [table, setTable] = useState<string | undefined>(undefined)
  const [profile, setProfile] = useState<TableProfile | null>(null)
  const [loading, setLoading] = useState(false)

  const runProfile = async (t: string) => {
    setTable(t)
    setLoading(true)
    try {
      setProfile(await api.profileTable(tenantId, pack.id, t))
    } catch (e: any) {
      setProfile(null)
      message.error(`探查失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  return (
    <>
      <Space style={{ marginBottom: 12 }}>
        <span>源表：</span>
        <Select
          style={{ width: 260 }} placeholder="选择要探查的源表" value={table}
          options={(pack.source_tables || []).map((t) => ({ value: t, label: t }))}
          onChange={runProfile} loading={loading}
        />
      </Space>
      {!profile && !loading && <Empty description="选择源表后展示字段画像" />}
      <Table
        rowKey="column_name" size="small" loading={loading} pagination={false}
        dataSource={profile?.columns || []}
        scroll={{ x: 900 }}
        columns={[
          { title: '字段', dataIndex: 'column_name', width: 150, render: (v: string) => <code>{v}</code> },
          { title: '类型', dataIndex: 'data_type', width: 110 },
          {
            title: '空值率', dataIndex: 'null_rate', width: 90,
            render: (v?: number) => (v != null ? `${(v * 100).toFixed(1)}%` : '-'),
          },
          { title: '去重数', dataIndex: 'distinct_count', width: 80, render: (v?: number) => v ?? '-' },
          {
            title: '格式', dataIndex: 'format_pattern', width: 110,
            render: (v?: string | null) => (v ? <Tag color="geekblue">{v}</Tag> : '-'),
          },
          {
            title: '枚举值', dataIndex: 'enum_values', width: 180,
            render: (v?: any[] | null) =>
              v && v.length ? (
                <Space size={4} wrap>{v.slice(0, 8).map((x) => <Tag key={String(x)}>{String(x)}</Tag>)}</Space>
              ) : '-',
          },
          {
            title: '样例值', dataIndex: 'sample_values',
            render: (v?: any[]) =>
              v && v.length ? (
                <Typography.Text type="secondary" style={{ fontSize: 12 }}>
                  {v.slice(0, 3).map((x) => String(x)).join('，')}
                </Typography.Text>
              ) : '-',
          },
        ]}
      />
      {profile?.columns?.[0]?.total_rows != null && (
        <Typography.Text type="secondary" style={{ fontSize: 12 }}>
          共 {profile.columns[0].total_rows} 行数据
        </Typography.Text>
      )}
    </>
  )
}

/**
 * 列表端点 GET /report-packs 只返回概要字段（不含 target_schema/reconciliation_rules/regulation_keywords）。
 * 完整详情需经 api.getReportPack 拉取。
 */
type ReportPackListItem = Omit<ReportPack, 'target_schema' | 'reconciliation_rules' | 'regulation_keywords'> &
  Partial<Pick<ReportPack, 'target_schema' | 'reconciliation_rules' | 'regulation_keywords'>>

/** P7 场景包管理：列表 + 详情抽屉 + 新建/编辑（admin） */
const ReportPacks: React.FC = () => {
  const { tenantId } = useTenant()
  const [packs, setPacks] = useState<ReportPackListItem[]>([])
  const [loading, setLoading] = useState(false)
  const [detail, setDetail] = useState<ReportPack | null>(null)
  const [detailLoading, setDetailLoading] = useState(false)
  const [editorOpen, setEditorOpen] = useState(false)
  const [editorLoading, setEditorLoading] = useState(false)
  const [editing, setEditing] = useState<ReportPack | null>(null) // null = 新建
  const [saving, setSaving] = useState(false)
  const [form] = Form.useForm()
  // target_schema / reconciliation_rules 用受控状态编辑（可编辑表格）
  const [schemaRows, setSchemaRows] = useState<TargetSchemaField[]>([])
  const [ruleRows, setRuleRows] = useState<ReconciliationRule[]>([])
  const admin = useMemo(isAdmin, [])

  const load = useCallback(async () => {
    setLoading(true)
    try {
      const r = await api.listReportPacks(tenantId)
      setPacks(r.report_packs)
    } catch (e: any) {
      message.error(`场景包加载失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }, [tenantId])

  useEffect(() => { load() }, [load])

  const openEditor = (pack: ReportPack | null) => {
    setEditing(pack)
    if (pack) {
      form.setFieldsValue({
        id: pack.id, report_name: pack.report_name, report_type: pack.report_type,
        target_table: pack.target_table, source_tables: pack.source_tables,
        trap_refs: pack.trap_refs, regulation_keywords: pack.regulation_keywords,
        status: pack.status,
      })
      setSchemaRows(pack.target_schema.map((f) => ({ ...f })))
      setRuleRows((pack.reconciliation_rules || []).map((r) => ({ ...r })))
    } else {
      form.resetFields()
      form.setFieldsValue({ status: 'draft', report_type: '1104' })
      setSchemaRows([{ ...EMPTY_FIELD }])
      setRuleRows([])
    }
    setEditorOpen(true)
  }

  /** 打开详情抽屉：列表项不含完整结构，先拉完整包再展示 */
  const openDetail = async (item: ReportPackListItem) => {
    setDetail(null)
    setDetailLoading(true)
    try {
      setDetail(await api.getReportPack(tenantId, item.id))
    } catch (e: any) {
      message.error(`场景包详情加载失败: ${e.message}`)
      setDetail(null)
    } finally {
      setDetailLoading(false)
    }
  }

  /** 打开编辑弹窗：列表项不含 target_schema 等，先拉完整包再编辑 */
  const openEditorFor = async (item: ReportPackListItem) => {
    setEditorLoading(true)
    try {
      openEditor(await api.getReportPack(tenantId, item.id))
    } catch (e: any) {
      message.error(`场景包详情加载失败: ${e.message}`)
    } finally {
      setEditorLoading(false)
    }
  }

  const save = async () => {
    const values = await form.validateFields()
    if (schemaRows.length === 0 || schemaRows.some((f) => !f.field.trim())) {
      message.warning('目标结构至少一行，且字段名不能为空')
      return
    }
    setSaving(true)
    try {
      const payload: Partial<ReportPack> = {
        ...values,
        source_tables: values.source_tables || [],
        trap_refs: values.trap_refs || [],
        target_schema: schemaRows,
        reconciliation_rules: ruleRows.filter((r) => r.name.trim() && r.expression.trim()),
      }
      if (editing) {
        await api.updateReportPack(tenantId, editing.id, payload)
        message.success(`场景包 ${editing.id} 已更新`)
      } else {
        await api.createReportPack(tenantId, payload)
        message.success(`场景包 ${values.id} 已创建`)
      }
      setEditorOpen(false)
      load()
    } catch (e: any) {
      message.error(`保存失败: ${e.message}`)
    } finally {
      setSaving(false)
    }
  }

  // ---- target_schema 可编辑表格行操作 ----
  const setRow = (i: number, patch: Partial<TargetSchemaField>) =>
    setSchemaRows((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))
  const setRule = (i: number, patch: Partial<ReconciliationRule>) =>
    setRuleRows((rows) => rows.map((r, idx) => (idx === i ? { ...r, ...patch } : r)))

  const schemaColumns = [
    {
      title: '字段名', key: 'field', width: 160,
      render: (_: any, __: any, i: number) => (
        <Input size="small" value={schemaRows[i].field} onChange={(e) => setRow(i, { field: e.target.value })} placeholder="loan_balance" />
      ),
    },
    {
      title: '类型', key: 'data_type', width: 140,
      render: (_: any, __: any, i: number) => (
        <Input size="small" value={schemaRows[i].data_type} onChange={(e) => setRow(i, { data_type: e.target.value })} />
      ),
    },
    {
      title: '必填', key: 'required', width: 60,
      render: (_: any, __: any, i: number) => (
        <Checkbox checked={schemaRows[i].required} onChange={(e) => setRow(i, { required: e.target.checked })} />
      ),
    },
    {
      title: '口径说明 caliber_text', key: 'caliber_text',
      render: (_: any, __: any, i: number) => (
        <Input size="small" value={schemaRows[i].caliber_text} onChange={(e) => setRow(i, { caliber_text: e.target.value })} placeholder="制度口径描述，映射推断语义锚点" />
      ),
    },
    {
      title: '期望值域', key: 'expected_domain', width: 180,
      render: (_: any, __: any, i: number) => (
        <Select
          size="small" mode="tags" style={{ width: '100%' }} open={false} suffixIcon={null}
          value={schemaRows[i].expected_domain || []}
          onChange={(v) => setRow(i, { expected_domain: v })}
          placeholder="枚举值，回车添加"
        />
      ),
    },
    {
      title: '', key: 'op', width: 50,
      render: (_: any, __: any, i: number) => (
        <Button size="small" type="text" danger icon={<DeleteOutlined />}
          onClick={() => setSchemaRows((rows) => rows.filter((_, idx) => idx !== i))} />
      ),
    },
  ]

  const columns = [
    { title: '代码', dataIndex: 'id', key: 'id', width: 110, render: (v: string) => <b>{v}</b> },
    { title: '报表名称', dataIndex: 'report_name', key: 'report_name' },
    { title: '类型', dataIndex: 'report_type', key: 'report_type', width: 90, render: (v: string) => <Tag color="blue">{v}</Tag> },
    { title: '目标表', dataIndex: 'target_table', key: 'target_table', width: 200, render: (v: string) => <code>{v}</code> },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (s: string) => <Tag color={PACK_STATUS[s]?.color}>{PACK_STATUS[s]?.text || s}</Tag>,
    },
    {
      title: '操作', key: 'op', width: 160,
      render: (_: any, r: ReportPackListItem) => (
        <Space>
          <Button size="small" type="link" onClick={() => openDetail(r)}>详情</Button>
          {admin && (
            <Button size="small" type="link" icon={<EditOutlined />} loading={editorLoading} onClick={() => openEditorFor(r)}>编辑</Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Card
      title="场景包管理"
      extra={
        <Space>
          <Button icon={<ReloadOutlined />} onClick={load}>刷新</Button>
          {admin && (
            <Button type="primary" icon={<PlusOutlined />} onClick={() => openEditor(null)}>新建场景包</Button>
          )}
        </Space>
      }
    >
      <Typography.Paragraph type="secondary" style={{ fontSize: 12 }}>
        场景包把报表定义从代码变成数据：目标结构、候选源表、勾稽规则、制度检索关键词。新增报表零代码。
      </Typography.Paragraph>
      <Table rowKey="id" loading={loading} columns={columns} dataSource={packs} pagination={false} />

      {/* 详情抽屉 */}
      <Drawer
        width={720}
        open={detailLoading || !!detail}
        onClose={() => { setDetail(null); setDetailLoading(false) }}
        title={detail ? `场景包 ${detail.id} · ${detail.report_name}` : '场景包详情'}
      >
        {detailLoading && (
          <div style={{ textAlign: 'center', padding: 64 }}>
            <Spin size="large" />
            <Typography.Paragraph type="secondary" style={{ marginTop: 16 }}>加载完整场景包…</Typography.Paragraph>
          </div>
        )}
        {detail && !detailLoading && (
          <Tabs
            defaultActiveKey="info"
            items={[
              {
                key: 'info',
                label: '基本信息',
                children: (
                  <>
            <Descriptions size="small" column={2} bordered>
              <Descriptions.Item label="类型">{detail.report_type}</Descriptions.Item>
              <Descriptions.Item label="状态">{PACK_STATUS[detail.status]?.text || detail.status}</Descriptions.Item>
              <Descriptions.Item label="目标表"><code>{detail.target_table}</code></Descriptions.Item>
              <Descriptions.Item label="候选源表">{detail.source_tables?.join('、') || '-'}</Descriptions.Item>
              <Descriptions.Item label="制度检索关键词" span={2}>{detail.regulation_keywords || '-'}</Descriptions.Item>
              <Descriptions.Item label="关联陷阱" span={2}>
                {(detail.trap_refs || []).map((t) => <Tag key={t} color="volcano">{t}</Tag>)}
                {(detail.trap_refs || []).length === 0 && '-'}
              </Descriptions.Item>
            </Descriptions>
            <Divider orientation="left" plain>目标结构（{detail.target_schema?.length ?? 0} 字段）</Divider>
            <Table
              rowKey="field" size="small" pagination={false}
              dataSource={detail.target_schema || []}
              columns={[
                { title: '字段', dataIndex: 'field', render: (v: string) => <code>{v}</code> },
                { title: '类型', dataIndex: 'data_type', width: 130 },
                { title: '必填', dataIndex: 'required', width: 60, render: (v: boolean) => (v ? '是' : '否') },
                { title: '口径说明', dataIndex: 'caliber_text' },
              ]}
            />
            <Divider orientation="left" plain>勾稽规则（{detail.reconciliation_rules?.length ?? 0}）</Divider>
            {(detail.reconciliation_rules || []).map((r) => (
              <div key={r.name} style={{ marginBottom: 8, fontSize: 13 }}>
                <Tag color="purple">{r.name}</Tag>
                <code>{r.expression}</code>
                <span style={{ color: '#999', marginLeft: 8 }}>容差 {r.tolerance}</span>
              </div>
            ))}
            {(detail.reconciliation_rules || []).length === 0 && <span style={{ color: '#bbb' }}>无</span>}
                  </>
                ),
              },
              {
                key: 'profile',
                label: '源表探查',
                children: <ProfileTab tenantId={tenantId} pack={detail} />,
              },
            ]}
          />
        )}
      </Drawer>

      {/* 新建/编辑弹窗（admin） */}
      <Modal
        title={editing ? `编辑场景包 ${editing.id}` : '新建场景包'}
        open={editorOpen}
        width={980}
        onCancel={() => setEditorOpen(false)}
        onOk={save}
        confirmLoading={saving}
        okText="保存"
      >
        <Form form={form} layout="vertical">
          <Space size={12} wrap style={{ display: 'flex' }}>
            <Form.Item name="id" label="代码" rules={[{ required: true, message: '如 G11' }]} style={{ width: 140 }}>
              <Input disabled={!!editing} placeholder="G11" />
            </Form.Item>
            <Form.Item name="report_name" label="报表名称" rules={[{ required: true }]} style={{ width: 320 }}>
              <Input placeholder="1104 资产质量五级分类" />
            </Form.Item>
            <Form.Item name="report_type" label="类型" rules={[{ required: true }]} style={{ width: 120 }}>
              <Select options={['1104', 'EAST', '其他'].map((v) => ({ value: v, label: v }))} />
            </Form.Item>
            <Form.Item name="status" label="状态" style={{ width: 120 }}>
              <Select options={Object.entries(PACK_STATUS).map(([v, m]) => ({ value: v, label: m.text }))} />
            </Form.Item>
          </Space>
          <Space size={12} wrap style={{ display: 'flex' }}>
            <Form.Item name="target_table" label="目标表" rules={[{ required: true }]} style={{ width: 280 }}>
              <Input placeholder="rpt_g11_five_class" />
            </Form.Item>
            <Form.Item name="source_tables" label="候选源表" style={{ width: 300 }}>
              <Select mode="tags" open={false} suffixIcon={null} placeholder="回车添加，如 loan_contract" />
            </Form.Item>
            <Form.Item name="trap_refs" label="关联陷阱关键词" style={{ width: 300 }}>
              <Select mode="tags" open={false} suffixIcon={null} placeholder="回车添加，如 逾期90天" />
            </Form.Item>
          </Space>
          <Form.Item name="regulation_keywords" label="制度检索关键词（Agent 1 检索用）">
            <Input placeholder="五级分类 逾期90天 资产质量" />
          </Form.Item>
        </Form>

        <Divider orientation="left" plain>目标结构 target_schema</Divider>
        <Table
          rowKey={(_, i) => String(i)} size="small" pagination={false}
          dataSource={schemaRows} columns={schemaColumns}
          footer={() => (
            <Button size="small" icon={<PlusOutlined />} onClick={() => setSchemaRows((r) => [...r, { ...EMPTY_FIELD }])}>
              添加字段
            </Button>
          )}
        />

        <Divider orientation="left" plain>勾稽规则 reconciliation_rules</Divider>
        {ruleRows.map((r, i) => (
          <Space key={i} size={8} style={{ display: 'flex', marginBottom: 8 }}>
            <Input size="small" placeholder="规则名" style={{ width: 160 }} value={r.name} onChange={(e) => setRule(i, { name: e.target.value })} />
            <Input size="small" placeholder="表达式，如 sum(正常)+sum(关注)+... = 合计" style={{ flex: 1 }} value={r.expression} onChange={(e) => setRule(i, { expression: e.target.value })} />
            <InputNumber size="small" placeholder="容差" style={{ width: 100 }} value={r.tolerance} onChange={(v) => setRule(i, { tolerance: v ?? 0 })} />
            <Button size="small" type="text" danger icon={<DeleteOutlined />} onClick={() => setRuleRows((rows) => rows.filter((_, idx) => idx !== i))} />
          </Space>
        ))}
        <Button size="small" icon={<PlusOutlined />} onClick={() => setRuleRows((r) => [...r, { name: '', expression: '', tolerance: 0 }])}>
          添加规则
        </Button>
        {!editing && (
          <Popconfirm title="创建后 Agent 即可按此场景包驱动任务" okText="知道了">
            <Typography.Text type="secondary" style={{ display: 'block', marginTop: 12, fontSize: 12 }}>
              提示：保存即生效，新建任务时可在场景包下拉中选择。
            </Typography.Text>
          </Popconfirm>
        )}
      </Modal>
    </Card>
  )
}

export default ReportPacks
