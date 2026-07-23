import React, { useCallback, useEffect, useState } from 'react'
import {
  Card, Statistic, Row, Col, Table, Tag, Alert, Space, Button, message, Empty, List,
  Tabs, Select, Input, Typography, Divider,
} from 'antd'
import { PlayCircleOutlined, SwapOutlined } from '@ant-design/icons'
import { useNavigate, useParams } from 'react-router-dom'
import {
  api, RegulationDoc, RegulationDiffResult, RegressionResult, ReportPack,
} from '../api/client'
import { useTenant } from '../App'

const LEVEL_COLOR: Record<string, string> = {
  critical: 'red', high: 'orange', medium: 'gold', low: 'blue',
}

/** 制度 diff 段落渲染：后端返回段落可能为字符串或对象，兼容两种形态 */
function sectionText(s: any): string {
  if (typeof s === 'string') return s
  if (s && typeof s === 'object') return s.title || s.section || s.content || JSON.stringify(s)
  return String(s)
}

/** 制度预演 · 上栏：制度版本对比（选两份文档 → diff） */
const RegulationDiffPanel: React.FC<{ tenantId: string }> = ({ tenantId }) => {
  const [docs, setDocs] = useState<RegulationDoc[]>([])
  const [docOld, setDocOld] = useState<string>()
  const [docNew, setDocNew] = useState<string>()
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<RegulationDiffResult | null>(null)

  useEffect(() => {
    api.listDocuments(tenantId)
      .then((r) => setDocs(r.documents))
      .catch((e) => message.warning(`制度文档加载失败: ${e.message}`))
  }, [tenantId])

  const runDiff = async () => {
    if (!docOld || !docNew) {
      message.warning('请选择新旧两份制度文档')
      return
    }
    setLoading(true)
    try {
      setResult(await api.diffRegulations(tenantId, docOld, docNew))
    } catch (e: any) {
      setResult(null)
      message.error(`对比失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  const docOptions = docs.map((d) => ({ value: d.id, label: `${d.filename}（${d.doc_type}）` }))

  const sectionList = (title: string, color: string, items: any[]) => (
    <Col span={8}>
      <Divider orientation="left" plain>{title}（{items.length}）</Divider>
      {items.length === 0 && <Typography.Text type="secondary">无</Typography.Text>}
      <List
        size="small"
        dataSource={items}
        renderItem={(s: any) => (
          <List.Item style={{ fontSize: 12 }}>
            <Tag color={color} style={{ marginRight: 4 }}>{title.slice(0, 1)}</Tag>
            {sectionText(s)}
          </List.Item>
        )}
      />
    </Col>
  )

  return (
    <Card size="small" title="制度版本对比" style={{ marginBottom: 16 }}>
      <Space wrap style={{ marginBottom: 12 }}>
        <Select style={{ width: 320 }} placeholder="旧版制度文档" value={docOld}
          options={docOptions} onChange={setDocOld} showSearch optionFilterProp="label" />
        <SwapOutlined style={{ color: '#999' }} />
        <Select style={{ width: 320 }} placeholder="新版制度文档" value={docNew}
          options={docOptions} onChange={setDocNew} showSearch optionFilterProp="label" />
        <Button type="primary" icon={<PlayCircleOutlined />} loading={loading} onClick={runDiff}>
          对比
        </Button>
      </Space>
      {result && (
        <>
          <Alert type="info" showIcon message={result.summary} style={{ marginBottom: 12 }} />
          <Row gutter={16}>
            {sectionList('新增段落', 'green', result.added_sections || [])}
            {sectionList('删除段落', 'red', result.removed_sections || [])}
            {sectionList('修改段落', 'orange', result.changed_sections || [])}
          </Row>
          {(result.affected_keywords || []).length > 0 && (
            <div style={{ marginTop: 12 }}>
              <Typography.Text type="secondary" style={{ marginRight: 8 }}>受影响关键词：</Typography.Text>
              {result.affected_keywords.map((k) => <Tag key={k} color="volcano">{k}</Tag>)}
            </div>
          )}
        </>
      )}
      {!result && !loading && <Empty description="选择新旧制度文档后点击对比" image={Empty.PRESENTED_IMAGE_SIMPLE} />}
    </Card>
  )
}

/** 制度预演 · 下栏：新旧逻辑回归（两段 SQL 执行结果比对） */
const RegressionPanel: React.FC<{ tenantId: string; defaultPackId?: string }> = ({ tenantId, defaultPackId }) => {
  const [packs, setPacks] = useState<ReportPack[]>([])
  const [packId, setPackId] = useState<string | undefined>(defaultPackId)
  const [sqlOld, setSqlOld] = useState('')
  const [sqlNew, setSqlNew] = useState('')
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<RegressionResult | null>(null)

  useEffect(() => {
    api.listReportPacks(tenantId)
      .then((r) => setPacks(r.packs))
      .catch((e) => message.warning(`场景包加载失败: ${e.message}`))
  }, [tenantId])

  const run = async () => {
    if (!packId || !sqlOld.trim() || !sqlNew.trim()) {
      message.warning('请选择场景包并填写新旧两段 SQL')
      return
    }
    setLoading(true)
    try {
      setResult(await api.runRegression(tenantId, {
        report_pack_id: packId, sql_old: sqlOld, sql_new: sqlNew,
      }))
    } catch (e: any) {
      setResult(null)
      message.error(`回归执行失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  // top_diffs 行列结构由后端决定，动态取首行 keys 生成列
  const diffColumns = result?.top_diffs?.[0]
    ? Object.keys(result.top_diffs[0]).map((k) => ({
        title: k, dataIndex: k, key: k,
        render: (v: any) => (typeof v === 'number' ? v.toLocaleString() : String(v ?? '-')),
      }))
    : []

  return (
    <Card size="small" title="新旧逻辑回归">
      <Space wrap style={{ marginBottom: 12 }}>
        <span>场景包：</span>
        <Select
          style={{ width: 280 }} placeholder="选择场景包" value={packId} onChange={setPackId}
          options={packs.map((p) => ({ value: p.id, label: `${p.id} ${p.report_name}` }))}
        />
      </Space>
      <Row gutter={16}>
        <Col span={12}>
          <Typography.Text type="secondary">旧逻辑 SQL</Typography.Text>
          <Input.TextArea
            rows={5} value={sqlOld} onChange={(e) => setSqlOld(e.target.value)}
            placeholder="SELECT ...（现行口径）" style={{ fontFamily: 'monospace', marginTop: 4 }}
          />
        </Col>
        <Col span={12}>
          <Typography.Text type="secondary">新逻辑 SQL</Typography.Text>
          <Input.TextArea
            rows={5} value={sqlNew} onChange={(e) => setSqlNew(e.target.value)}
            placeholder="SELECT ...（拟变更口径）" style={{ fontFamily: 'monospace', marginTop: 4 }}
          />
        </Col>
      </Row>
      <Button
        type="primary" icon={<PlayCircleOutlined />} loading={loading} onClick={run}
        style={{ marginTop: 12 }}
      >
        执行回归比对
      </Button>
      {result && (
        <div style={{ marginTop: 16 }}>
          <Row gutter={16}>
            <Col span={6}><Statistic title="旧口径合计" value={result.old_total} precision={2} /></Col>
            <Col span={6}><Statistic title="新口径合计" value={result.new_total} precision={2} /></Col>
            <Col span={6}>
              <Statistic title="差异金额" value={result.diff_amount} precision={2}
                valueStyle={{ color: result.diff_amount !== 0 ? '#cf1322' : '#3f8600' }} />
            </Col>
            <Col span={6}>
              <Statistic title="差异率" value={(result.diff_rate ?? 0) * 100} precision={3} suffix="%"
                valueStyle={{ color: result.diff_rate !== 0 ? '#cf1322' : '#3f8600' }} />
            </Col>
          </Row>
          <Alert
            style={{ marginTop: 12 }} showIcon
            type={result.diff_rate !== 0 ? 'warning' : 'success'}
            message={result.conclusion}
          />
          {(result.top_diffs || []).length > 0 && (
            <>
              <Divider orientation="left" plain>差异明细（Top {result.top_diffs.length}）</Divider>
              <Table
                rowKey={(_, i) => String(i)} size="small" pagination={false}
                columns={diffColumns} dataSource={result.top_diffs}
              />
            </>
          )}
        </div>
      )}
    </Card>
  )
}

/** 制度预演 Tab：上栏制度版本对比 + 下栏新旧逻辑回归 */
const PreviewTab: React.FC<{ tenantId: string; packId?: string }> = ({ tenantId, packId }) => (
  <>
    <RegulationDiffPanel tenantId={tenantId} />
    <RegressionPanel tenantId={tenantId} defaultPackId={packId} />
  </>
)

/** P4 数字孪生对比页：1104 vs EAST 双口径差异分析 + 制度预演 */
const DigitalTwin: React.FC = () => {
  const { taskId } = useParams<{ taskId: string }>()
  const { tenantId } = useTenant()
  const [twin, setTwin] = useState<any>(null)
  const [packId, setPackId] = useState<string | undefined>(undefined)
  const navigate = useNavigate()

  const load = useCallback(() => {
    api.getTask(tenantId, taskId!)
      .then((t) => {
        setTwin(t.outputs?.digital_twin || {})
        // outputs 里若有场景包 id 则带入回归面板缺省值（无则由用户手选）
        setPackId(t.outputs?.report_pack_id || undefined)
      })
      .catch((e) => message.error(`加载失败: ${e.message}`))
  }, [taskId, tenantId])

  useEffect(() => { load() }, [load])

  const renderTwin = () => {
    if (!twin) return <Card loading />
    if (!twin.diff_analysis) {
      return <Card title="数字孪生对比"><Empty description="该任务尚无数字孪生产出" /></Card>
    }

    const da = twin.diff_analysis
    const attr = twin.attribution || {}

    const columns = [
      { title: '借据号', dataIndex: 'contract_no', key: 'contract_no', width: 110 },
      {
        title: '1104 口径(元)', dataIndex: 'value_1104', key: 'value_1104',
        render: (v: number) => v?.toLocaleString() ?? '-',
      },
      {
        title: 'EAST 口径(元)', dataIndex: 'value_east', key: 'value_east',
        render: (v: number) => v?.toLocaleString() ?? '-',
      },
      {
        title: '绝对差异(元)', dataIndex: 'abs_diff', key: 'abs_diff',
        render: (v: number) => v?.toLocaleString() ?? '-',
      },
      {
        title: '相对差异', dataIndex: 'rel_diff', key: 'rel_diff',
        render: (v: number) => (v != null ? `${(v * 100).toFixed(3)}%` : '-'),
      },
      {
        title: '等级', dataIndex: 'diff_level', key: 'diff_level',
        render: (l: string) => <Tag color={LEVEL_COLOR[l]}>{l}</Tag>,
      },
    ]

    return (
      <Space direction="vertical" style={{ width: '100%' }} size={16}>
        <Card title={`数字孪生对比 · ${twin.scenario || ''}`}>
          <Row gutter={16}>
            <Col span={6}>
              <Statistic title={`${twin.instance_a?.name || '口径A'}（${twin.instance_a?.record_count} 笔）`}
                value={twin.instance_a?.total_balance} suffix="元" precision={2} />
            </Col>
            <Col span={6}>
              <Statistic title={`${twin.instance_b?.name || '口径B'}（${twin.instance_b?.record_count} 笔）`}
                value={twin.instance_b?.total_balance} suffix="元" precision={2} />
            </Col>
            <Col span={6}>
              <Statistic title="差异总额 / 差异率" value={da.abs_diff_total} precision={2}
                suffix={`元 / ${(da.rel_diff_total * 100).toFixed(3)}%`}
                valueStyle={{ color: '#cf1322' }} />
            </Col>
            <Col span={6}>
              <Statistic title="差异记录 / 一致记录" value={da.diff_record_count}
                suffix={`/ ${da.match_record_count} 笔`} />
            </Col>
          </Row>
          <div style={{ marginTop: 12 }}>
            差异等级分布：
            {Object.entries(da.level_distribution || {}).map(([level, cnt]) => (
              <Tag key={level} color={LEVEL_COLOR[level]}>{level}: {cnt as number}</Tag>
            ))}
          </div>
        </Card>

        <Card title="逐笔差异明细（Top 5）">
          <Table
            rowKey="contract_no"
            size="small"
            columns={columns}
            dataSource={da.top_diff_samples || []}
            pagination={false}
          />
        </Card>

        <Card title="差异归因">
          <Alert type="info" showIcon message={`差异方向：${attr.direction || '-'}`}
            description={attr.conclusion} style={{ marginBottom: 12 }} />
          <List
            size="small"
            header={<b>归因分析</b>}
            dataSource={attr.reasons || []}
            renderItem={(r: string) => <List.Item>· {r}</List.Item>}
          />
          <List
            size="small"
            header={<b>制度依据</b>}
            dataSource={attr.regulation_basis || []}
            renderItem={(r: string) => <List.Item>· {r}</List.Item>}
          />
          <Alert type="success" message={`对账调节建议：${attr.suggestion || '-'}`} />
        </Card>
      </Space>
    )
  }

  return (
    <Card
      title="数字孪生"
      extra={<Button onClick={() => navigate(`/execute/${taskId}`)}>返回执行页</Button>}
    >
      <Tabs
        defaultActiveKey="twin"
        items={[
          { key: 'twin', label: '孪生对比', children: renderTwin() },
          { key: 'preview', label: '制度预演', children: <PreviewTab tenantId={tenantId} packId={packId} /> },
        ]}
      />
    </Card>
  )
}

export default DigitalTwin
