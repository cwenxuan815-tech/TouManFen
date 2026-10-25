/** F20 阶段 5C：挂载真实 App，访问 API/业务 API 全部指向本地 mock。 */
import { createApp } from 'vue'
import App from '../../src/App.vue'
import '../../src/styles/tokens.css'
import '../../src/styles/base.css'
import '../../src/style.css'

createApp(App).mount('#app')
