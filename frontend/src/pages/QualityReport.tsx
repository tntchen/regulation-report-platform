import React, { useEffect, useState } from 'react'
import { Card, Tag, Alert, List, Space, Button, message, Empty } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import { api, TestVerifyOutput, TestVerifyCheck } from '../api/client'
import { useTenant } from '../App'

// 六维元信息
const DIMENSIONS: { key: string; name: string }[] = [
  { key: 'caliber_compliance', name: '口径合规' },
  { key: 'type_safety', name: '类型安全' },
  { key: 'null_defense', name: '空值防御' },
  { key: 'performance', name: '性能友好' },
  { key: 'security_compliance', name: '安全合规' },
  { key: 'regulatory_special', name: '监管特殊' },
]

const LEVEL_COLOR: Record<string, string> = { blocker: 'error', warning: 'warning', pass: 'success' }
const GATE_TEXT: Record<string, { color: string; text: string }> = {
  pass: { color: 'success', text: '门禁通过' },
  warn: { color: 'warning', text: '带警告放行' },
  block: { color: 'error', text: '门禁阻断' },
}
const CHECK_STATUS: Record<string, { color: string; text: string }> = {
  pass: { color: 'success', text: '通过' },
  fail: { color: 'error', text: '失败' },
  skipped: { color: 'default', text: '跳过' },
}

/** 勾稽对账区块：渲染 outputs.test_verify 中 check_id === 'reconcile' 的校验项 */
const ReconcileSection: React.FC<{ verify: TestVerifyOutput | null }> = ({ verify }) => {
  if (!verify || !verify.checks?.length) {
    return (
      <Card title="勾稽对账">
        <Empty description="该任务尚无测试验证产出（勾稽结果由测试验证 Agent 生成）" />
      </Card>
    )
  }
  const reconcile: TestVerifyCheck | undefined = verify.checks.find((c) => c.check_id === 'reconcile')
  if (!reconcile) {
    return (
      <Card title="勾稽对账">
        <Empty description="测试验证结果中无勾稽对账项" />
      </Card>
    )
  }
  const st = CHECK_STATUS[reconcile.status] || { color: 'default', text: reconcile.status }
  const m = reconcile.metrics || {}
  const rules = m.rule_results

  return (
    <Card
      title={`勾稽对账 · ${reconcile.name}`}
      extra={<Tag color={st.color}>{st.text}</Tag>}
    >
      <Space direction="vertical" style={{ width: '100%' }} size={12}>
        <span style={{ color: '#666' }}>{reconcile.detail}</span>

        {rules ? (
          <List
            size="small"
            header={<span>共 {m.rule_count ?? rules.length} 条规则，{m.failed_count ?? rules.filter((r) => !r.passed).length} 条未通过</span>}
            dataSource={rules}
            renderItem={(r) => (
              <List.Item>
                <Space direction="vertical" size={2} style={{ width: '100%' }}>
                  <Space wrap>
                    <Tag color={r.passed ? 'success' : 'error'}>{r.passed ? '通过' : '失败'}</Tag>
                    <span style={{ fontWeight: 500 }}>{r.name}</span>
                    <span style={{ color: '#999', fontSize: 12 }}>容差 {r.tolerance}</span>
                  </Space>
                  <span style={{ fontSize: 12, color: '#666' }}>表达式：{r.expression}</span>
                  <span style={{ fontSize: 12 }}>
                    实测值 {r.actual ?? '—'} / 期望值 {r.expected ?? '—'}
                    {r.abs_diff != null && <>，绝对差异 {r.abs_diff}</>}
                    {r.rel_diff != null && <>，相对差异 {(r.rel_diff * 100).toFixed(4)}%</>}
                    {r.error && <span style={{ color: '#cf1322' }}>，错误：{r.error}</span>}
                  </span>
                </Space>
              </List.Item>
            )}
          />
        ) : (
          <Space wrap size={16}>
            <span>目标表总额：{m.target_total ?? '—'}</span>
            <span>源表重算总额：{m.source_total ?? '—'}</span>
            <span>绝对差异：{m.abs_diff ?? '—'}</span>
            <span>相对差异：{m.rel_diff != null ? `${(m.rel_diff * 100).toFixed(4)}%` : '—'}</span>
          </Space>
        )}

        {reconcile.samples?.length > 0 && (
          <List
            size="small"
            header="异常样本"
            dataSource={reconcile.samples}
            renderItem={(s: string) => <List.Item style={{ fontSize: 12 }}>{s}</List.Item>}
          />
        )}
      </Space>
    </Card>
  )
}

/** P3 六维校验报告页 */
const QualityReport: React.FC = () => {
  const { taskId } = useParams<{ taskId: string }>()
  const { tenantId } = useTenant()
  const [gate, setGate] = useState<any>(null)
  const [verify, setVerify] = useState<TestVerifyOutput | null>(null)
  const navigate = useNavigate()

  useEffect(() => {
    api.getTask(tenantId, taskId!)
      .then((t) => {
        setGate(t.outputs?.quality_gate || {})
        setVerify(t.outputs?.test_verify || null)
      })
      .catch((e) => message.error(`加载失败: ${e.message}`))
  }, [taskId, tenantId])

  if (!gate) return <Card loading />
  if (!gate.gate_result) {
    return <Card title="六维校验报告"><Empty description="该任务尚无质量校验产出" /></Card>
  }

  const gateInfo = GATE_TEXT[gate.gate_result] || { color: 'default', text: gate.gate_result }

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Card
        title={`六维校验报告 · ${taskId}`}
        extra={<Button onClick={() => navigate(`/execute/${taskId}`)}>返回执行页</Button>}
      >
        <Alert
          type={gate.gate_result === 'pass' ? 'success' : gate.gate_result === 'warn' ? 'warning' : 'error'}
          showIcon
          message={<Space>
            <span>门禁判定：</span>
            <Tag color={gateInfo.color}>{gateInfo.text}</Tag>
            <span>blocker {gate.blocker_count} 个 / warning {gate.warning_count} 个</span>
          </Space>}
          description={gate.summary}
        />
      </Card>

      <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
        {DIMENSIONS.map((dim) => {
          const d = gate.dimensions?.[dim.key] || { status: 'pass', issues: [] }
          return (
            <Card
              key={dim.key}
              size="small"
              style={{ width: 380, flex: '1 1 360px' }}
              title={dim.name}
              extra={<Tag color={LEVEL_COLOR[d.status]}>
                {d.status === 'pass' ? '通过' : d.status === 'warning' ? '警告' : '阻断'}
              </Tag>}
            >
              {d.issues.length === 0 ? (
                <span style={{ color: '#999' }}>未发现问题</span>
              ) : (
                <List
                  size="small"
                  dataSource={d.issues}
                  renderItem={(issue: any) => (
                    <List.Item>
                      <Space direction="vertical" size={2} style={{ width: '100%' }}>
                        <Space>
                          <Tag color={LEVEL_COLOR[issue.level]}>{issue.level}</Tag>
                          <span>{issue.message}</span>
                        </Space>
                        {issue.suggestion && (
                          <span style={{ color: '#1d39c4', fontSize: 12 }}>建议：{issue.suggestion}</span>
                        )}
                      </Space>
                    </List.Item>
                  )}
                />
              )}
            </Card>
          )
        })}
      </div>

      <ReconcileSection verify={verify} />

      {(gate.auto_fix_suggestions || []).length > 0 && (
        <Card title="自动修复建议（回退 Agent 2 时参考）">
          <List
            size="small"
            dataSource={gate.auto_fix_suggestions}
            renderItem={(s: string, i: number) => <List.Item>{i + 1}. {s}</List.Item>}
          />
        </Card>
      )}
    </Space>
  )
}

export default QualityReport
