import { createRouter, createWebHistory } from 'vue-router'

import LineageView from '@/views/LineageView.vue'
import MigrationView from '@/views/MigrationView.vue'
import Overview from '@/views/Overview.vue'
import ReplayView from '@/views/ReplayView.vue'
import WorkbenchView from '@/views/WorkbenchView.vue'

const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: '/', name: 'overview', component: Overview },
    // 序号 23 的三个联动页面：必须排在 /:moduleKey 之前。
    { path: '/migration_deploy', name: 'migration_deploy', component: MigrationView },
    { path: '/lineage', name: 'lineage', component: LineageView },
    { path: '/replay', name: 'replay', component: ReplayView },
    { path: '/:moduleKey', name: 'module', component: WorkbenchView, props: true },
  ],
})

export default router
