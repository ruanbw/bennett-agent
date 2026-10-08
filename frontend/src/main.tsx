import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import './index.css'

// index.html 里必然有 #root 挂载点；`!` 只是把「查不到返回 null」的联合收窄
ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
)
