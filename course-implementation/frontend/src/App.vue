<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import QueryResult from './QueryResult.vue'
import Knowledge from './Knowledge.vue'
import { api, levels, statuses } from './api'
import type { Batch, GraphNode, Level, QueryInput, Result } from './api'

const pages = [
  { id: 'query', title: '单条查询', icon: '⌘' },
  { id: 'batch', title: '批量查询', icon: '▤' },
  { id: 'knowledge', title: '查询知识库', icon: '▦' },
  { id: 'schema', title: '图谱 Schema', icon: '◇' },
] as const
const examples: Record<Level, string[]> = {
  simple: ['列出所有微服务', '查找 Controller 类型的类', '统计微服务总数量'],
  medium: ['getAllFood 方法调用了哪些下游服务？', '哪些服务调用了 ts-train-food-service？'],
  complex: ['ts-food-service 有哪些跨服务依赖？', '修改 publishFoodDelivery 会影响哪些上游 API？'],
}
const page = ref<(typeof pages)[number]['id']>('query')
const heading = computed(() => pages.find(item => item.id === page.value)?.title)
const question = ref('')
const level = ref<Level>('simple')
const scope = reactive({ service: '', repository: '', branch: '' })
const health = ref<{ graph: string; model: string }>()
const healthError = ref(false)
const result = ref<Result>()
const busy = ref(false)
const error = ref('')
const batchText = ref('')
const batchFile = ref('')
const batch = ref<Batch>()
const batchResult = ref<Result>()
const batchBusy = ref(false)
const batchError = ref('')
const schema = ref<{ nodes: Record<string, string[]>; relations: Record<string, string[]>; hash: string }>()
const schemaError = ref('')
let timer: ReturnType<typeof setTimeout> | undefined

onMounted(async () => {
  try { health.value = await api('/health') }
  catch { healthError.value = true }
})
onBeforeUnmount(() => clearTimeout(timer))

async function submit(input: QueryInput, fromBatch = false) {
  if (busy.value) return
  busy.value = true
  error.value = ''
  batchError.value = ''
  try {
    const value = await api<Result>('/queries', 'POST', input)
    if (fromBatch) batchResult.value = value
    else result.value = value
  } catch (exception) {
    if (fromBatch) batchError.value = (exception as Error).message
    else error.value = (exception as Error).message
  } finally { busy.value = false }
}

function confirm(previous: Result, node: GraphNode, fromBatch = false) {
  const slot = previous.clarification?.slot === 'target' ? 'target_id' : 'candidate_id'
  submit({ ...previous.input, [slot]: node.id, clarify_from: previous.id }, fromBatch)
}

async function navigate(id: typeof page.value) {
  page.value = id
  if (id === 'schema' && !schema.value) {
    schemaError.value = ''
    try { schema.value = await api('/schema') }
    catch (e) { schemaError.value = (e as Error).message }
  }
}

async function readBatch(event: Event) {
  const input = event.target as HTMLInputElement
  const file = input.files?.[0]
  input.value = ''
  if (!file) return
  if (file.size > 256 * 1024) { batchError.value = '文件大小上限为 256 KB'; return }
  batchError.value = ''
  batchFile.value = file.name
  batchText.value = await file.text()
}

async function startBatch() {
  batchError.value = ''
  batchBusy.value = true
  try {
    const text = batchText.value.trim().replace(/^\uFEFF/, '')
    let entries: unknown = text.startsWith('[') ? JSON.parse(text) : text.split(/\r?\n/).filter(line => line.trim())
    if (!Array.isArray(entries)) throw new Error('JSON 文件需要使用数组')
    const questions = entries.map(item => typeof item === 'string'
      ? { question: item, level: level.value } : { level: level.value, ...item })
    batch.value = await api('/batches', 'POST', { questions })
    batchResult.value = undefined
    clearTimeout(timer)
    timer = setTimeout(poll, 200)
  } catch (e) { batchError.value = (e as Error).message }
  finally { batchBusy.value = false }
}

async function poll() {
  if (!batch.value) return
  try {
    const value = await api<Batch>('/batches/' + batch.value.id)
    batch.value = value
    if (['queued', 'running'].includes(value.status)) timer = setTimeout(poll, 650)
  } catch (e) { batchError.value = (e as Error).message }
}

async function cancel() {
  if (!batch.value) return
  try {
    clearTimeout(timer)
    batch.value = await api('/batches/' + batch.value.id + '/cancel', 'POST')
  } catch (e) { batchError.value = (e as Error).message }
}

async function loadBatchResult(id: string) {
  try { batchResult.value = await api('/queries/' + id) }
  catch (e) { batchError.value = (e as Error).message }
}
</script>

<template>
  <div class="shell">
    <aside class="sidebar">
      <div class="brand"><span class="brand-mark"><i></i><i></i><i></i></span>
        <div><strong>代码图谱</strong><small>Text2Cypher</small></div>
      </div>
      <div class="nav-label">工作台</div>
      <nav aria-label="主导航">
        <button v-for="item in pages" :key="item.id" :class="{ active: page === item.id }"
                :aria-current="page === item.id ? 'page' : undefined" @click="navigate(item.id)">
          <span aria-hidden="true">{{ item.icon }}</span>{{ item.title }}
        </button>
      </nav>
      <div class="sidebar-foot">高级软件工程 · 第五组</div>
    </aside>

    <main>
      <header class="topbar">
        <span>代码图谱工作台 <span class="slash">/</span> {{ heading }}</span>
        <span v-if="health" class="environment"><i></i>{{ health.graph === 'demo' ? '样例图谱' : 'Neo4j 图谱' }} <span>· {{ health.model }}</span></span>
        <span v-else-if="healthError" class="environment offline">后端未连接</span>
      </header>

      <div class="content">
        <h1>{{ heading }}</h1>
        <p v-if="page === 'query'" class="lede">查找代码实体、追踪调用链，或分析改动可能影响的接口。</p>
        <p v-if="page === 'batch'" class="lede">一次提交最多 50 道问题，逐题查看执行状态和结果。</p>
        <p v-if="page === 'knowledge'" class="lede">管理自然语言问题与可复用的 Cypher 查询示例。</p>
        <p v-if="page === 'schema'" class="lede">查看当前图谱中的节点类型、关系和属性。</p>

        <div v-if="page === 'query'" class="workspace-grid">
          <section class="panel compose">
            <form @submit.prevent="submit({ question, level, scope: { ...scope } })">
              <div class="panel-title">查询条件</div>
              <label class="field-title">查询难度</label>
              <div class="segmented" role="group" aria-label="查询难度">
                <button v-for="(label, key) in levels" :key="key" type="button" :class="{ active: level === key }"
                        :aria-pressed="level === key" @click="level = key">{{ label }}</button>
              </div>
              <label class="field-title" for="question">自然语言问题</label>
              <textarea id="question" v-model="question" required maxlength="1600" rows="5"
                        placeholder="例如，getAllFood 方法调用了哪些下游服务？"></textarea>
              <details class="scope">
                <summary>限定查询范围</summary>
                <label>微服务<input v-model="scope.service" maxlength="180" placeholder="ts-food-service"></label>
                <label>仓库<input v-model="scope.repository" maxlength="180" placeholder="train-ticket-demo"></label>
                <label>分支<input v-model="scope.branch" maxlength="180" placeholder="main"></label>
              </details>
              <button class="primary run" type="submit" :disabled="busy || !question.trim()">
                {{ busy ? '查询执行中…' : '运行查询' }} <span v-if="!busy" aria-hidden="true">→</span>
              </button>
              <p class="callout error" role="alert" v-if="error">{{ error }}</p>
            </form>
            <div class="suggestions">
              <div class="field-title">示例问题</div>
              <button type="button" v-for="item in examples[level]" :key="item" @click="question = item">
                <span>{{ item }}</span><span aria-hidden="true">↗</span>
              </button>
            </div>
          </section>
          <QueryResult v-if="result" :result="result" @confirm="confirm(result, $event)" />
          <section v-else class="panel empty-panel" aria-label="查询结果">
            <div class="panel-head"><div class="panel-title">查询结果</div></div>
            <div class="empty-grid">
              <div class="empty-state">
                <span class="brand-mark illustration" aria-hidden="true"><i></i><i></i><i></i></span>
                <h2>{{ busy ? '正在查询图谱' : '等待查询' }}</h2>
                <p>{{ busy ? '查询完成后将在此展示路径证据。' : '运行查询后，可在这里查看结果、拓扑与 Cypher。' }}</p>
              </div>
            </div>
          </section>
        </div>

        <div v-if="page === 'batch'" class="workspace-grid">
          <section class="panel compose">
            <div class="panel-title">问题列表</div>
            <form @submit.prevent="startBatch">
              <label class="upload button secondary">上传 .txt / .json<input type="file" accept=".txt,.json" @change="readBatch"></label>
              <p v-if="batchFile" class="meta">{{ batchFile }}</p>
              <label class="field-title" for="batch-text">每行一个问题，或粘贴 JSON 数组</label>
              <textarea id="batch-text" v-model="batchText" required rows="7" maxlength="262144"
                        placeholder="列出所有微服务&#10;ts-food-service 有哪些跨服务依赖？"></textarea>
              <label class="field-title" for="batch-level">默认难度</label>
              <select id="batch-level" v-model="level"><option v-for="(label, key) in levels" :value="key">{{ label }}</option></select>
              <button class="primary run" :disabled="batchBusy || ['queued', 'running'].includes(batch?.status || '') || !batchText.trim()">提交批量查询 <span>→</span></button>
            </form>
            <p v-if="batchError" class="callout error" role="alert">{{ batchError }}</p>
          </section>
          <div class="stack">
            <section class="panel">
              <div class="panel-head">
                <div class="panel-title">逐题状态</div>
                <button v-if="batch && ['queued','running'].includes(batch.status)" class="secondary" @click="cancel">取消批次</button>
              </div>
              <div v-if="batch" class="result-body">
                <p class="meta">{{ statuses[batch.status] }} · {{ batch.items.length }} 题</p>
                <div class="table-scroll"><table>
                  <thead><tr><th>问题</th><th>状态</th><th>结果</th></tr></thead>
                  <tbody><tr v-for="item in batch.items" :key="item.number">
                    <td>{{ item.question }}<small v-if="item.error" class="item-error">{{ item.error }}</small></td>
                    <td><span :class="'status ' + item.status">{{ statuses[item.status] }}</span></td>
                    <td><button v-if="item.query_id" class="link" @click="loadBatchResult(item.query_id)">查看</button></td>
                  </tr></tbody>
                </table></div>
              </div>
              <p v-else class="no-data">批次提交后，可在此查看进度。</p>
            </section>
            <QueryResult v-if="batchResult" :result="batchResult" @confirm="confirm(batchResult, $event, true)" />
          </div>
        </div>

        <Knowledge v-if="page === 'knowledge'" />

        <section v-if="page === 'schema'" class="panel">
          <div class="panel-head"><div class="panel-title">Schema 快照</div><code v-if="schema">{{ schema.hash }}</code></div>
          <div v-if="schema" class="result-body schema-grid">
            <div v-for="(rows, type) in { 节点: schema.nodes, 关系: schema.relations }" :key="type">
              <h2>{{ type }}</h2>
              <table><thead><tr><th>名称</th><th>属性</th></tr></thead><tbody>
                <tr v-for="(props, label) in rows" :key="label"><td>{{ label }}</td><td>{{ props.join('、') || '—' }}</td></tr>
              </tbody></table>
            </div>
          </div>
          <p v-else-if="schemaError" class="no-data" role="alert">{{ schemaError }}</p>
          <p v-else class="no-data">正在读取图谱…</p>
        </section>
      </div>
    </main>
  </div>
</template>
