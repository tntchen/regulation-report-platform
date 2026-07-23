import React, { useEffect, useRef, useState } from 'react'
import { Card, Steps, Tag, Button, Space, Alert, Descriptions, message } from 'antd'
import { useNavigate, useParams } from 'react-router-dom'
import { api, TaskDetail, StageRecord } from '../api/client'
import { useTenant } from '../App'

// 6 Agent 元信息（与后端编排器 DAG 对齐）
const AGENTS = [
  { key: 'regulation_parser', name: 'Agent 1 制度解析', parallel: false },
  { key: 'codegen', name: 'Agent 2 代码生成', parallel: false },
  { key: 'quality_gate', name: 'Agent 3 质量校验', parallel: false },
  { key: 'test_verify', name: 'Agent 4 测试验证', parallel: true },
  { key: 'digital_twin', name: 'Agent 5 数字孪生', parallel: true },
  { key: 'deploy', name: 'Agent 6 投产交付', parallel: false },
]

/** 单 Agent 阶段摘要（从产出中提取一句话说明） */
function stageSummary(name: string, output: any): string {
  if (!output) return ''
  switch (name) {
    case 'regulation_parser':
      return `检索制度 ${output.retrieved_count ?? 0} 条，识别陷阱 ${(output.traps_identified || []).length} 个`
    case 'codegen':
      return `生成 SQL ${(output.generated_code || '').length} 字符`
    case 'quality_gate':
      return `门禁判定 ${output.gate_result}（blocker ${output.blocker_count ?? 0} / warning ${output.warning_count ?? 0}）`
    case 'test_verify':
      return output.summary || ''
    case 'digital_twin': {
      const d = output.diff_analysis || {}
      return `两口径差异 ${Number(d.abs_diff_total || 0).toLocaleString()} 元（${((d.rel_diff_total || 0) * 100).toFixed(3)}%）`
    }
    case 'deploy':
      return output.summary || ''
    default:
      return ''
  }
}

/** P2 任务执行页：6 Agent 流水线实时状态（1.5s 轮询） */
const TaskExecute: React.FC = () => {
  const { taskId } = useParams<{ taskId: string }>()
  const { tenantId } = useTenant()
  const [task, setTask] = useState<TaskDetail | null>(null)
  const timer = useRef<number>()
  const navigate = useNavigate()

  const poll = async () => {
    try {
      const t = await api.getTask(tenantId, taskId!)
      setTask(t)
      if (t.status === 'completed' || t.status === 'failed') {
        window.clearInterval(timer.current)
      }
    } catch (e: any) {
      window.clearInterval(timer.current)
      message.error(`任务查询失败: ${e.message}`)
    }
  }

  useEffect(() => {
    poll()
    timer.current = window.setInterval(poll, 1500)
    return () => window.clearInterval(timer.current)
  }, [taskId, tenantId])

  if (!task) return <Card loading />

  // 计算每个 Agent 的状态：取最后一次执行记录；未执行且为下一阶段则"执行中"
  const lastStageOf = (key: string): StageRecord | undefined =>
    [...task.stages].reverse().find((s) => s.agent_name === key)
  const runCountOf = (key: string) => task.stages.filter((s) => s.agent_name === key).length

  const completedKeys = new Set(task.stages.map((s) => s.agent_name))
  const nextKeys = (() => {
    // 依据 DAG 推导当前应执行的层
    if (task.status !== 'executing') return new Set<string>()
    if (!completedKeys.has('regulation_parser')) return new Set(['regulation_parser'])
    if (!completedKeys.has('codegen')) return new Set(['codegen'])
    if (!completedKeys.has('quality_gate')) return new Set(['quality_gate'])
    if (!completedKeys.has('test_verify') || !completedKeys.has('digital_twin'))
      return new Set(['test_verify', 'digital_twin'])
    if (!completedKeys.has('deploy')) return new Set(['deploy'])
    return new Set<string>()
  })()

  const hasRollback = task.retry_count > 0

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      <Card
        title={`任务执行 ${task.task_id}`}
        extra={
          <Space>
            <Tag color={task.status === 'completed' ? 'success' : task.status === 'failed' ? 'error' : 'processing'}>
              {task.status === 'completed' ? '已完成' : task.status === 'failed' ? '失败' : '执行中'}
            </Tag>
            <Button onClick={() => navigate('/')}>返回大厅</Button>
            <Button type="primary" disabled={!task.outputs?.quality_gate}
              onClick={() => navigate(`/quality/${task.task_id}`)}>六维校验报告</Button>
            <Button type="primary" disabled={!task.outputs?.digital_twin}
              onClick={() => navigate(`/twin/${task.task_id}`)}>数字孪生对比</Button>
          </Space>
        }
      >
        <Descriptions size="small" column={4}>
          <Descriptions.Item label="进度">{task.progress}%</Descriptions.Item>
          <Descriptions.Item label="当前阶段">{task.current_stage || '-'}</Descriptions.Item>
          <Descriptions.Item label="门禁重试">{task.retry_count} 次</Descriptions.Item>
          <Descriptions.Item label="耗时">{(task.duration_ms / 1000).toFixed(1)}s</Descriptions.Item>
        </Descriptions>
        {hasRollback && (
          <Alert style={{ marginTop: 12 }} type="warning" showIcon
            message={`质量门禁触发阻断回退：已回退代码生成 Agent 重试 ${task.retry_count} 次`} />
        )}
        {task.error && <Alert style={{ marginTop: 12 }} type="error" showIcon message={task.error} />}
      </Card>

      <Card title="Agent 流水线">
        <Row_AgentCards task={task} lastStageOf={lastStageOf} runCountOf={runCountOf} nextKeys={nextKeys} />
      </Card>
    </Space>
  )
}

/** Agent 卡片行：并行层（4/5）并排展示 */
const Row_AgentCards: React.FC<{
  task: TaskDetail
  lastStageOf: (k: string) => StageRecord | undefined
  runCountOf: (k: string) => number
  nextKeys: Set<string>
}> = ({ lastStageOf, runCountOf, nextKeys }) => {
  return (
    <div style={{ display: 'flex', gap: 12, flexWrap: 'wrap' }}>
      {AGENTS.map((a) => {
        const stage = lastStageOf(a.key)
        const running = nextKeys.has(a.key)
        const retries = runCountOf(a.key)
        let status: 'wait' | 'process' | 'finish' | 'error' = 'wait'
        if (stage) status = stage.status === 'success' ? 'finish' : 'error'
        else if (running) status = 'process'

        const statusTag = stage
          ? <Tag color={stage.status === 'success' ? 'success' : 'error'}>{stage.status === 'success' ? '成功' : '失败'}</Tag>
          : running
            ? <Tag color="processing">执行中</Tag>
            : <Tag>等待</Tag>

        return (
          <Card
            key={a.key}
            size="small"
            style={{
              width: 250,
              borderColor: a.key === 'quality_gate' && retries > 1 ? '#faad14' : undefined,
              boxShadow: running ? '0 0 8px #1d39c455' : undefined,
            }}
            title={
              <Space>
                {a.name}
                {a.parallel && <Tag color="blue">并行</Tag>}
              </Space>
            }
            extra={statusTag}
          >
            <div style={{ minHeight: 60 }}>
              {stage ? (
                <>
                  <div style={{ color: '#666', fontSize: 12, marginBottom: 6 }}>
                    耗时 {stage.duration_ms}ms
                    {retries > 1 && <Tag color="warning" style={{ marginLeft: 6 }}>重试×{retries}</Tag>}
                  </div>
                  <div style={{ fontSize: 13 }}>{stageSummary(a.key, stage.output)}</div>
                  {a.key === 'quality_gate' && stage.output?.gate_result === 'block' && (
                    <Alert style={{ marginTop: 8 }} type="error" message="门禁阻断 → 回退 Agent 2" />
                  )}
                </>
              ) : (
                <span style={{ color: '#999' }}>{running ? '正在执行…' : '等待上游完成'}</span>
              )}
            </div>
          </Card>
        )
      })}
    </div>
  )
}

export default TaskExecute
