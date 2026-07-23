import React, { useState } from 'react'
import { Card, Form, Input, Button, Typography, message, Space } from 'antd'
import { UserOutlined, LockOutlined, ApartmentOutlined } from '@ant-design/icons'
import { useNavigate } from 'react-router-dom'
import { api, auth } from '../api/client'

/** 登录页：账号密码 → JWT 存 localStorage → 跳任务大厅 */
const Login: React.FC = () => {
  const [loading, setLoading] = useState(false)
  const navigate = useNavigate()

  const onFinish = async (values: { username: string; password: string }) => {
    setLoading(true)
    try {
      const r = await api.login(values.username, values.password)
      auth.setToken(r.access_token)
      localStorage.setItem('current_user', JSON.stringify(r.user))
      message.success(`欢迎，${r.user.display_name || r.user.username}`)
      navigate('/')
    } catch (e: any) {
      message.error('用户名或密码错误')
    } finally {
      setLoading(false)
    }
  }

  return (
    <div style={{
      minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center',
      background: 'linear-gradient(135deg, #0a1e4a 0%, #1d39c4 100%)',
    }}>
      <Card style={{ width: 400 }} styles={{ body: { padding: 32 } }}>
        <Space direction="vertical" align="center" style={{ width: '100%', marginBottom: 24 }}>
          <ApartmentOutlined style={{ fontSize: 36, color: '#1d39c4' }} />
          <Typography.Title level={3} style={{ margin: 0 }}>银行监管报送智能开发平台</Typography.Title>
          <Typography.Text type="secondary">请使用演示账号登录</Typography.Text>
        </Space>
        <Form onFinish={onFinish} size="large">
          <Form.Item name="username" rules={[{ required: true, message: '请输入用户名' }]}>
            <Input prefix={<UserOutlined />} placeholder="用户名：admin / zhangsan" />
          </Form.Item>
          <Form.Item name="password" rules={[{ required: true, message: '请输入密码' }]}>
            <Input.Password prefix={<LockOutlined />} placeholder="密码" />
          </Form.Item>
          <Button type="primary" htmlType="submit" block loading={loading}>登录</Button>
        </Form>
        <Typography.Paragraph type="secondary" style={{ fontSize: 12, marginTop: 16, marginBottom: 0 }}>
          演示账号：admin / Admin@1234（T001+T002）；zhangsan / Zhangsan@1234（仅 T001）
        </Typography.Paragraph>
      </Card>
    </div>
  )
}

export default Login
