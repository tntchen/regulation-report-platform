import React, { useEffect, useState } from 'react'
import {
  Card, Statistic, Row, Col, Table, Tag, Button, Space, Modal, Upload,
  Select, Input, message, Switch, Popconfirm, Drawer, List, Typography,
} from 'antd'
import {
  UploadOutlined, ReloadOutlined, SearchOutlined, InboxOutlined,
} from '@ant-design/icons'
import { api, RegulationDoc, VectorStats, RetrievalItem } from '../api/client'
import { useTenant } from '../App'

const STATUS_TAG: Record<string, { color: string; text: string }> = {
  indexed: { color: 'success', text: '已索引' },
  indexing: { color: 'processing', text: '索引中' },
  uploaded: { color: 'warning', text: '待索引' },
  failed: { color: 'error', text: '失败' },
}

const DOC_TYPES = ['1104', 'EAST', '利率报备', '征信', '反洗钱', '通用安全合规', '自定义']

/** P5 向量库维护页 */
const VectorLibrary: React.FC = () => {
  const { tenantId } = useTenant()
  const [stats, setStats] = useState<VectorStats | null>(null)
  const [docs, setDocs] = useState<RegulationDoc[]>([])
  const [logs, setLogs] = useState<any[]>([])
  const [loading, setLoading] = useState(false)
  const [uploadOpen, setUploadOpen] = useState(false)
  const [testOpen, setTestOpen] = useState(false)
  const [detail, setDetail] = useState<any>(null)
  const [docType, setDocType] = useState('自定义')
  const [file, setFile] = useState<File | null>(null)
  const [query, setQuery] = useState('')
  const [searching, setSearching] = useState(false)
  const [searchResult, setSearchResult] = useState<{ elapsed_ms: number; results: RetrievalItem[] } | null>(null)

  const load = async () => {
    setLoading(true)
    try {
      const [s, d, l] = await Promise.all([
        api.stats(tenantId), api.listDocuments(tenantId), api.indexLogs(tenantId, 10),
      ])
      setStats(s)
      setDocs(d.documents)
      setLogs(l.logs)
    } catch (e: any) {
      message.error(`加载失败: ${e.message}`)
    } finally {
      setLoading(false)
    }
  }

  useEffect(() => { load() }, [tenantId])

  const doUpload = async () => {
    if (!file) { message.warning('请选择文件'); return }
    try {
      const r = await api.uploadDocument(tenantId, file, docType)
      message.success(`上传成功，已自动索引（切片 ${r.chunk_count} 个）`)
      setUploadOpen(false)
      setFile(null)
      load()
    } catch (e: any) {
      message.error(e.message)
    }
  }

  const doRetrieval = async () => {
    if (!query.trim()) return
    setSearching(true)
    try {
      const r = await api.retrievalTest(tenantId, query, 5)
      setSearchResult(r)
    } catch (e: any) {
      message.error(e.message)
    } finally {
      setSearching(false)
    }
  }

  const columns = [
    { title: '文档名', dataIndex: 'filename', key: 'filename' },
    { title: '类型', dataIndex: 'doc_type', key: 'doc_type', width: 120, render: (t: string) => <Tag>{t}</Tag> },
    { title: '大小', dataIndex: 'size', key: 'size', width: 90, render: (s: number) => `${(s / 1024).toFixed(1)}KB` },
    { title: '切片数', dataIndex: 'chunk_count', key: 'chunk_count', width: 80 },
    {
      title: '状态', dataIndex: 'status', key: 'status', width: 90,
      render: (s: string) => <Tag color={STATUS_TAG[s]?.color}>{STATUS_TAG[s]?.text || s}</Tag>,
    },
    {
      title: '启用', dataIndex: 'is_active', key: 'is_active', width: 80,
      render: (v: boolean, r: RegulationDoc) => (
        <Switch checked={v} size="small" onChange={async (checked) => {
          await api.updateDocument(tenantId, r.id, checked)
          message.success(checked ? '已启用' : '已禁用（检索不再召回）')
          load()
        }} />
      ),
    },
    {
      title: '操作', key: 'op', width: 200,
      render: (_: any, r: RegulationDoc) => (
        <Space>
          <Button size="small" type="link" onClick={async () => {
            const d = await api.getDocument(tenantId, r.id)
            setDetail(d)
          }}>详情</Button>
          <Button size="small" type="link" onClick={async () => {
            const res = await api.reindexOne(tenantId, r.id)
            message.success(`重建完成（切片 ${res.chunk_count} 个）`)
            load()
          }}>索引</Button>
          <Popconfirm title="确认删除该文档及其向量索引？" onConfirm={async () => {
            await api.deleteDocument(tenantId, r.id)
            message.success('已删除')
            load()
          }}>
            <Button size="small" type="link" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" style={{ width: '100%' }} size={16}>
      {/* 统计概览 */}
      <Card title="统计概览">
        <Row gutter={16}>
          <Col span={5}><Statistic title="文档总数" value={stats?.doc_count ?? 0} /></Col>
          <Col span={5}><Statistic title="已索引" value={stats?.by_status?.indexed ?? 0} /></Col>
          <Col span={5}><Statistic title="向量总数" value={stats?.vector_count ?? 0} /></Col>
          <Col span={5}><Statistic title="向量维度" value={stats?.vector_dimension ?? 0} /></Col>
          <Col span={4}>
            <Statistic title="状态" value={(stats?.by_status?.failed ?? 0) === 0 ? '正常' : '异常'}
              valueStyle={{ color: (stats?.by_status?.failed ?? 0) === 0 ? '#3f8600' : '#cf1322' }} />
          </Col>
        </Row>
      </Card>

      {/* 文档列表 */}
      <Card
        title="文档列表"
        extra={
          <Space>
            <Button icon={<UploadOutlined />} onClick={() => setUploadOpen(true)}>上传文档</Button>
            <Popconfirm title="重建全部启用文档的索引？" onConfirm={async () => {
              const r = await api.reindexAll(tenantId)
              message.success(r.message)
              load()
            }}>
              <Button icon={<ReloadOutlined />}>重建索引</Button>
            </Popconfirm>
            <Button type="primary" icon={<SearchOutlined />} onClick={() => setTestOpen(true)}>检索测试</Button>
          </Space>
        }
      >
        <Table rowKey="id" loading={loading} columns={columns} dataSource={docs}
          pagination={{ pageSize: 10 }} size="middle" />
      </Card>

      {/* 索引日志 */}
      <Card title="索引日志（最近 10 条）" size="small">
        <Table
          rowKey={(r: any) => `${r.time}-${r.doc_name}`}
          size="small"
          pagination={false}
          dataSource={logs}
          columns={[
            { title: '时间', dataIndex: 'time', width: 180, render: (t: string) => t?.replace('T', ' ').slice(0, 19) },
            { title: '操作', dataIndex: 'operation', width: 120 },
            { title: '文档', dataIndex: 'doc_name' },
            { title: '状态', dataIndex: 'status', width: 80, render: (s: string) => <Tag color={s === 'success' ? 'success' : 'error'}>{s === 'success' ? '成功' : '失败'}</Tag> },
            { title: '说明', dataIndex: 'message' },
          ]}
        />
      </Card>

      {/* 上传弹窗 */}
      <Modal title="上传制度文档" open={uploadOpen} onCancel={() => setUploadOpen(false)}
        onOk={doUpload} okText="上传并索引">
        <Space direction="vertical" style={{ width: '100%' }}>
          <Select value={docType} onChange={setDocType} style={{ width: '100%' }}
            options={DOC_TYPES.map((t) => ({ value: t, label: t }))} />
          <Upload.Dragger maxCount={1} beforeUpload={(f) => { setFile(f); return false }}
            onRemove={() => setFile(null)}>
            <p className="ant-upload-drag-icon"><InboxOutlined /></p>
            <p>点击或拖拽文件到此上传（支持 TXT/MD，PDF/DOCX 视后端环境）</p>
          </Upload.Dragger>
        </Space>
      </Modal>

      {/* 检索测试弹窗 */}
      <Modal title="检索测试 - 验证制度检索效果" open={testOpen} width={760}
        onCancel={() => setTestOpen(false)} footer={null}>
        <Space.Compact style={{ width: '100%', marginBottom: 16 }}>
          <Input value={query} onChange={(e) => setQuery(e.target.value)}
            placeholder="输入测试问题，如：个人住房贷款逾期90天临界点怎么算？"
            onPressEnter={doRetrieval} />
          <Button type="primary" loading={searching} onClick={doRetrieval}>执行检索</Button>
        </Space.Compact>
        {searchResult && (
          <>
            <Typography.Text type="secondary">
              耗时 {searchResult.elapsed_ms}ms，Top {searchResult.results.length} 结果：
            </Typography.Text>
            <List
              style={{ marginTop: 8 }}
              size="small"
              dataSource={searchResult.results}
              renderItem={(r) => (
                <List.Item>
                  <Space direction="vertical" size={2} style={{ width: '100%' }}>
                    <Space>
                      <Tag color="blue">#{r.rank}</Tag>
                      <Tag color="geekblue">融合 {r.relevance_score}</Tag>
                      <Tag color="purple">向量 {r.vector_score ?? '-'}</Tag>
                      <Tag color="cyan">文本 {r.text_score ?? '-'}</Tag>
                      <b>{r.doc_title}</b>
                      <Tag>{r.doc_type}</Tag>
                    </Space>
                    <Typography.Paragraph type="secondary" style={{ margin: 0, fontSize: 12 }}
                      ellipsis={{ rows: 2, expandable: true }}>
                      {r.content}
                    </Typography.Paragraph>
                  </Space>
                </List.Item>
              )}
            />
          </>
        )}
      </Modal>

      {/* 文档详情抽屉 */}
      <Drawer title={detail?.filename} open={!!detail} onClose={() => setDetail(null)} width={560}>
        {detail && (
          <Space direction="vertical" style={{ width: '100%' }}>
            <Space wrap>
              <Tag>{detail.doc_type}</Tag>
              <Tag color={STATUS_TAG[detail.status]?.color}>{STATUS_TAG[detail.status]?.text}</Tag>
              <span>切片 {detail.chunk_count} 个</span>
              <span>{(detail.size / 1024).toFixed(1)}KB</span>
            </Space>
            <Typography.Paragraph type="secondary" style={{ whiteSpace: 'pre-wrap', fontSize: 12 }}>
              {detail.preview}
            </Typography.Paragraph>
          </Space>
        )}
      </Drawer>
    </Space>
  )
}

export default VectorLibrary
