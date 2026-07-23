import React, { useEffect, useState } from 'react'
import { Card, Statistic, Row, Col, Table, Tag, Alert, Space, Button, message, Empty, List } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import { api } from '../api/client'
import { useTenant } from '../App'

const LEVEL_COLOR: Record<string, string> = {
  critical: 'red', high: 'orange', medium: 'gold', low: 'blue',
}

/** P4 数字孪生对比页：1104 vs EAST 双口径差异分析 */
const DigitalTwin: React.FC = () => {
  const { taskId } = useParams<{ taskId: string }>()
  const { tenantId } = useTenant()
  const [twin, setTwin] = useState<any>(null)
  const navigate = useNavigate()

  useEffect(() => {
    api.getTask(tenantId, taskId!)
      .then((t) => setTwin(t.outputs?.digital_twin || {}))
      .catch((e) => message.error(`加载失败: ${e.message}`))
  }, [taskId, tenantId])

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
      <Card
        title={`数字孪生对比 · ${twin.scenario || ''}`}
        extra={<Button onClick={() => navigate(`/execute/${taskId}`)}>返回执行页</Button>}
      >
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

export default DigitalTwin
