import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import { ErrorBoundary } from './components/ErrorBoundary'
import './index.css'

// 全局兜底日志：只记录，不吞事件（不调 preventDefault），浏览器默认行为与上报链路保持不变
window.addEventListener('error', (event) => {
  console.error('[全局] 未捕获错误:', event.error ?? event.message)
})

window.addEventListener('unhandledrejection', (event) => {
  console.error('[全局] 未处理的 Promise 拒绝:', event.reason)
})

const rootElement = document.getElementById('root')
if (!rootElement) {
  // 挂载点缺失时给一条能读懂的错误，而不是让 createRoot 在 null 上报类型相关的崩溃
  throw new Error('找不到 #root 挂载点：请检查 index.html 中的 <div id="root">')
}

ReactDOM.createRoot(rootElement).render(
  <React.StrictMode>
    {/* 边界放在根位置：包住 App 才能捕获 App 自身的渲染期错误 */}
    <ErrorBoundary fallbackTitle="管理后台出现异常">
      <App />
    </ErrorBoundary>
  </React.StrictMode>,
)
