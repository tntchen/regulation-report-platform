import React from 'react'
import { Button, Result, Typography } from 'antd'

interface Props {
  children: React.ReactNode
}

interface State {
  hasError: boolean
  error: Error | null
}

/**
 * 全局错误边界：任一渲染异常兜底为组件级错误页，避免整页白屏。
 * dev 环境下额外展示错误堆栈。
 */
class ErrorBoundary extends React.Component<Props, State> {
  constructor(props: Props) {
    super(props)
    this.state = { hasError: false, error: null }
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error }
  }

  componentDidCatch(error: Error, info: React.ErrorInfo) {
    // 统一打日志，便于排查（也可在此接入上报）
    console.error('[ErrorBoundary] 渲染异常:', error, info.componentStack)
  }

  render() {
    if (this.state.hasError) {
      return (
        <div style={{ minHeight: '100vh', display: 'flex', alignItems: 'center', justifyContent: 'center', background: '#f0f2f5' }}>
          <Result
            status="500"
            title="页面出现异常"
            subTitle={
              <Typography.Text type="secondary">
                {this.state.error?.message || '未知错误'}
              </Typography.Text>
            }
            extra={
              <Button type="primary" onClick={() => window.location.reload()}>
                刷新页面
              </Button>
            }
          >
            {import.meta.env.DEV && this.state.error?.stack && (
              <pre style={{ textAlign: 'left', maxWidth: 720, overflow: 'auto', fontSize: 12, background: '#fff', padding: 12, borderRadius: 6 }}>
                {this.state.error.stack}
              </pre>
            )}
          </Result>
        </div>
      )
    }
    return this.props.children
  }
}

export default ErrorBoundary
