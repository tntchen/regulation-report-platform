import React, { createContext, useContext, useEffect, useState } from 'react'
import { BrowserRouter, Routes, Route, useNavigate, useLocation, Navigate } from 'react-router-dom'
import { Layout, Menu, Select, Tag, Space, Typography, Dropdown, Spin } from 'antd'
import {
  HomeOutlined, DatabaseOutlined, ApartmentOutlined, UserOutlined, LogoutOutlined,
  FileSearchOutlined,
} from '@ant-design/icons'
import { api, auth, TenantBrief } from './api/client'
import TaskHall from './pages/TaskHall'
import TaskExecute from './pages/TaskExecute'
import QualityReport from './pages/QualityReport'
import DigitalTwin from './pages/DigitalTwin'
import VectorLibrary from './pages/VectorLibrary'
import AuditLogs from './pages/AuditLogs'
import Login from './pages/Login'

const { Header, Sider, Content } = Layout

// ---------- 租户上下文 ----------
interface TenantCtx {
  tenantId: string
  tenants: TenantBrief[]
  setTenantId: (id: string) => void
}
const TenantContext = createContext<TenantCtx>({ tenantId: 'T001', tenants: [], setTenantId: () => {} })
export const useTenant = () => useContext(TenantContext)

/** 路由守卫：未登录跳登录页 */
const RequireAuth: React.FC<{ children: React.ReactElement }> = ({ children }) => {
  if (!auth.getToken()) return <Navigate to="/login" replace />
  return children
}

const AppShell: React.FC = () => {
  const [tenants, setTenants] = useState<TenantBrief[]>([])
  const [tenantId, setTenantId] = useState(localStorage.getItem('tenant_id') || '')
  const [user, setUser] = useState<any>(null)
  const [healthy, setHealthy] = useState(false)
  const [ready, setReady] = useState(false)
  const navigate = useNavigate()
  const location = useLocation()

  useEffect(() => {
    api.health().then(() => setHealthy(true)).catch(() => setHealthy(false))
    // 从 /auth/me 加载当前用户与有权租户（租户切换器只显示有权限的租户）
    api.me()
      .then((r) => {
        setUser(r.user)
        setTenants(r.tenants)
        // 当前租户不在权限范围内时，切换到第一个有权租户
        const ids = r.tenants.map((t) => t.id)
        const saved = localStorage.getItem('tenant_id') || ''
        const next = ids.includes(saved) ? saved : ids[0] || ''
        setTenantId(next)
        localStorage.setItem('tenant_id', next)
      })
      .catch(() => {})  // 401 已由封装层统一跳登录
      .finally(() => setReady(true))
  }, [])

  const switchTenant = (id: string) => {
    setTenantId(id)
    localStorage.setItem('tenant_id', id)
  }

  const logout = () => {
    auth.clear()
    navigate('/login')
  }

  if (!ready) {
    return <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      <Spin size="large" tip="加载中…" />
    </div>
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
              value={tenantId || undefined}
              style={{ width: 180 }}
              onChange={switchTenant}
              options={tenants.map((t) => ({ value: t.id, label: `${t.id} ${t.name}` }))}
            />
            <Dropdown menu={{
              items: [{ key: 'logout', icon: <LogoutOutlined />, label: '退出登录', onClick: logout }],
            }}>
              <Space style={{ color: '#fff', cursor: 'pointer' }}>
                <UserOutlined />
                {user?.display_name || user?.username}
              </Space>
            </Dropdown>
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
                { key: '/audit', icon: <FileSearchOutlined />, label: '审计日志' },
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
              <Route path="/audit" element={<AuditLogs />} />
            </Routes>
          </Content>
        </Layout>
      </Layout>
    </TenantContext.Provider>
  )
}

const App: React.FC = () => (
  <BrowserRouter>
    <Routes>
      <Route path="/login" element={<Login />} />
      <Route path="/*" element={<RequireAuth><AppShell /></RequireAuth>} />
    </Routes>
  </BrowserRouter>
)

export default App
