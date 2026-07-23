import React, { createContext, useContext, useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, useNavigate, useLocation } from 'react-router-dom'
import { Layout, Menu, Select, Tag, Space, Typography } from 'antd'
import {
  HomeOutlined, DatabaseOutlined, ApartmentOutlined,
} from '@ant-design/icons'
import { api, TenantBrief } from './api/client'
import TaskHall from './pages/TaskHall'
import TaskExecute from './pages/TaskExecute'
import QualityReport from './pages/QualityReport'
import DigitalTwin from './pages/DigitalTwin'
import VectorLibrary from './pages/VectorLibrary'

const { Header, Sider, Content } = Layout

// ---------- 租户上下文 ----------
interface TenantCtx {
  tenantId: string
  tenants: TenantBrief[]
  setTenantId: (id: string) => void
}
const TenantContext = createContext<TenantCtx>({ tenantId: 'T001', tenants: [], setTenantId: () => {} })
export const useTenant = () => useContext(TenantContext)

const AppShell: React.FC = () => {
  const [tenants, setTenants] = useState<TenantBrief[]>([])
  const [tenantId, setTenantId] = useState(localStorage.getItem('tenant_id') || 'T001')
  const [healthy, setHealthy] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    api.listTenants().then((r) => setTenants(r.tenants)).catch(() => {})
    api.health().then(() => setHealthy(true)).catch(() => setHealthy(false))
  }, [])

  const switchTenant = (id: string) => {
    setTenantId(id)
    localStorage.setItem('tenant_id', id)
  }

  return (
    <TenantContext.Provider value={{ tenantId, tenants, setTenantId: switchTenant }}>
      <Layout style={{ minHeight: '100vh' }}>
        <Header style={{ display: 'flex', alignItems: 'center', background: '#0a1e4a' }}>
          <ApartmentOutlined style={{ color: '#fff', fontSize: 20, marginRight: 12 }} />
          <Typography.Title level={4} style={{ color: '#fff', margin: 0, flex: 1 }}>
            银行监管报送智能开发平台
          </Typography.Title>
          <Space>
            <Tag color={healthy ? 'success' : 'error'}>{healthy ? '后端在线' : '后端离线'}</Tag>
            <span style={{ color: '#aab' }}>租户</span>
            <Select
              value={tenantId}
              style={{ width: 180 }}
              onChange={switchTenant}
              options={tenants.map((t) => ({ value: t.id, label: `${t.id} ${t.name}` }))}
            />
          </Space>
        </Header>
        <Layout>
          <Sider width={200} theme="light">
            <Menu
              mode="inline"
              selectedKeys={[location.pathname]}
              style={{ height: '100%', borderRight: 0 }}
              items={[
                { key: '/', icon: <HomeOutlined />, label: '任务大厅' },
                { key: '/vectors', icon: <DatabaseOutlined />, label: '向量库维护' },
              ]}
              onClick={({ key }) => navigate(key)}
            />
          </Sider>
          <Content style={{ padding: 20, background: '#f0f2f5' }}>
            <Routes>
              <Route path="/" element={<TaskHall />} />
              <Route path="/execute/:taskId" element={<TaskExecute />} />
              <Route path="/quality/:taskId" element={<QualityReport />} />
              <Route path="/twin/:taskId" element={<DigitalTwin />} />
              <Route path="/vectors" element={<VectorLibrary />} />
            </Routes>
          </Content>
        </Layout>
      </Layout>
    </TenantContext.Provider>
  )
}

const App: React.FC = () => (
  <BrowserRouter>
    <AppShell />
  </BrowserRouter>
)

export default App
