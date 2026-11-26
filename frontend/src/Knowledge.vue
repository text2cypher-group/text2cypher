<script setup lang="ts">
import { onMounted, reactive, ref } from 'vue'
import { api, levels } from './api'
import type { Example, Level, Plan } from './api'

const categories = ['单节点查询','属性过滤','入口 API 发现','下游服务发现','反向影响分析','MQ 发布者与完整链路','REST + MQ 综合依赖']
const intents: Record<string,string> = {
  list: '节点列表', count: '实体计数', owner: '直属归属', entry: '入口 API', downstream: '下游服务',
  callers: '反向调用服务', impact: 'API 变更影响', call_chain: '正向调用链', publishers: 'MQ 发布者',
  mq: 'MQ 链路', rpc: 'RPC 依赖', dependencies: '综合依赖', imports: '导入关系',
  implements: '接口实现', inherits: '继承关系',
}
const kinds: Record<string,string> = { service: '微服务', class: '类', method: '方法', api: 'API',
  exchange: '交换机', queue: '队列', config: '配置文件', test: '测试用例' }
const list = ref<Example[]>([])
const filter = reactive({ keyword: '', level: '', category: '' })
const form = ref<{ question: string; level: Level; review: Example['review']; plan: Plan }>()
const editing = ref('')
const confirmingDelete = ref('')
const error = ref('')
const info = ref('')
const busy = ref(false)
const inspected = ref('')
const preview = ref<string[]>([])
const history = ref<Example[]>([])

async function refresh() {
  try { list.value = await api('/examples?' + new URLSearchParams(filter).toString()) }
  catch (e) { error.value = (e as Error).message }
}

function open(item?: Example) {
  editing.value = item?.id || ''
  form.value = item ? { question: item.question, level: item.level, review: item.review, plan: { ...item.plan } } :
    { question: '', level: 'simple', review: 'draft', plan: { operation: 'list', entity: 'service', name: '',
      service: '', target_service: '', contains: '', depth: 6, direction: 'forward' } }
  info.value = ''
  error.value = ''
}

async function save() {
  if (!form.value) return
  error.value = ''
  busy.value = true
  try {
    const row = await api<Example>(editing.value ? '/examples/' + editing.value : '/examples',
      editing.value ? 'PUT' : 'POST', form.value)
    form.value = undefined
    inspected.value = ''
    info.value = '已保存版本 ' + row.version
    await refresh()
  } catch (e) { error.value = (e as Error).message }
  finally { busy.value = false }
}

async function remove(item: Example) {
  try {
    await api('/examples/' + item.id, 'DELETE')
    confirmingDelete.value = ''
    form.value = undefined
    await refresh()
    await inspect(item.id, true)
    info.value = '示例已删除，历史版本仍可查看。'
  } catch (e) { error.value = (e as Error).message }
}

async function inspect(id: string, deleted = false) {
  inspected.value = id
  error.value = ''
  preview.value = []
  history.value = []
  try {
    history.value = await api('/examples/' + id + '/history')
    if (!deleted) {
      const value = await api<{ cypher: string[] }>('/examples/' + id + '/preview')
      preview.value = value.cypher
    }
  } catch (e) { error.value = (e as Error).message }
}
onMounted(refresh)
</script>

<template>
  <div class="stack">
    <section class="panel">
      <div class="panel-head"><div class="panel-title">示例库</div><button class="primary" @click="open()">新增示例</button></div>
      <div class="result-body">
        <form class="filters" @submit.prevent="refresh">
          <input v-model="filter.keyword" aria-label="关键词" placeholder="搜索关键词">
          <select v-model="filter.level" aria-label="按难度筛选" @change="refresh">
            <option value="">全部难度</option><option v-for="(label, key) in levels" :value="key">{{ label }}</option>
          </select>
          <select v-model="filter.category" aria-label="按分类筛选" @change="refresh">
            <option value="">全部分类</option><option v-for="category in categories" :value="category">{{ category }}</option>
          </select>
          <button class="secondary">搜索</button>
        </form>
        <p v-if="info" class="callout" role="status">{{ info }}</p>
        <p v-if="error && !form" class="callout error" role="alert">{{ error }}</p>
        <div class="table-scroll"><table>
          <thead><tr><th>自然语言问题</th><th>难度</th><th>类型</th><th>审核</th><th>操作</th></tr></thead>
          <tbody><tr v-for="item in list" :key="item.id">
            <td><button class="link plain" @click="inspect(item.id)">{{ item.question }}</button></td>
            <td>{{ levels[item.level] }}</td><td>{{ item.category }}</td>
            <td>{{ item.review === 'approved' ? '已启用' : '草稿' }}</td>
            <td class="row-actions"><button class="link" @click="open(item)">编辑</button>
              <button v-if="confirmingDelete !== item.id" class="link" @click="confirmingDelete = item.id">删除</button>
              <template v-else><button class="link danger" @click="remove(item)">确认删除</button>
                <button class="link" @click="confirmingDelete = ''">取消</button></template>
            </td>
          </tr></tbody>
        </table></div>
        <p v-if="!list.length" class="meta">当前筛选下没有示例。</p>
      </div>
    </section>

    <section v-if="form" class="panel">
      <div class="panel-head"><div class="panel-title">{{ editing ? '编辑示例' : '新增示例' }}</div></div>
      <form class="result-body" @submit.prevent="save">
        <label class="field-title">自然语言问题<input v-model="form.question" required maxlength="400"></label>
        <div class="form-grid">
          <label>难度<select v-model="form.level"><option v-for="(label, key) in levels" :value="key">{{ label }}</option></select></label>
          <label>审核状态<select v-model="form.review"><option value="draft">草稿</option><option value="approved">已启用</option></select></label>
          <label>意图<select v-model="form.plan.operation" @change="form.plan.direction = 'forward'"><option v-for="(label,key) in intents" :value="key">{{ label }}</option></select></label>
          <label>实体类型<select v-model="form.plan.entity"><option v-for="(label,key) in kinds" :value="key">{{ label }}</option></select></label>
          <label>精确名称<input v-model="form.plan.name" maxlength="180"></label>
          <label>所属服务<input v-model="form.plan.service" maxlength="180"></label>
          <label>目标服务<input v-model="form.plan.target_service" maxlength="180"></label>
          <label>名称包含<input v-model="form.plan.contains" maxlength="180"></label>
          <label>调用遍历深度<input v-model.number="form.plan.depth" type="number" min="1" max="10" required></label>
          <label v-if="form.plan.operation === 'call_chain'">调用链方向<select v-model="form.plan.direction">
            <option value="forward">下游</option><option value="reverse">上游</option><option value="both">上下游</option>
          </select></label>
        </div>
        <p v-if="error" class="callout error" role="alert">{{ error }}</p>
        <div class="row-actions"><button class="primary" :disabled="busy">保存示例</button>
          <button type="button" class="secondary" @click="form=undefined">取消</button></div>
      </form>
    </section>

    <section v-if="inspected" class="panel">
      <div class="panel-head"><div class="panel-title">Cypher 与版本记录</div><button class="secondary" @click="inspected = ''">关闭</button></div>
      <div class="result-body">
        <pre v-for="(entry, i) in preview" :key="i" class="cypher">{{ entry }}</pre>
        <ul class="history"><li v-for="row in history" :key="row.version">
          v{{ row.version }} · {{ row.updated_at }} · {{ row.deleted ? '已删除' : row.review === 'approved' ? '已启用' : '草稿' }} · {{ row.question }}
        </li></ul>
      </div>
    </section>
  </div>
</template>
