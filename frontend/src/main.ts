import { createApp } from 'vue'
import App from './App.vue'
import { initTheme } from './composables/useTheme'
import { initPresentation } from './composables/useLargeScreen'
// 样式分层：token 定义 -> 基础层 -> 页面/区块层（顺序不可颠倒）
import './styles/tokens.css'
import './styles/base.css'
import './style.css'

// F13：首屏渲染前应用主题/大屏偏好，避免闪烁。
initTheme()
initPresentation()

createApp(App).mount('#app')
