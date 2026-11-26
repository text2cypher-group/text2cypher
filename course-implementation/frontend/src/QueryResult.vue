<script setup lang="ts">
import { computed, defineAsyncComponent, ref, watch } from 'vue'
import { statuses } from './api'
import type { GraphNode, Result } from './api'

const Topology = defineAsyncComponent(() => import('./Topology.vue'))
const props = defineProps<{ result: Result }>()
defineEmits<{ confirm: [candidate: GraphNode] }>()
const tab = ref<'graph' | 'trace'>('graph')
const chosen = ref<number>()
watch(() => props.result.id, () => { tab.value = 'graph'; chosen.value = undefined })
const names = computed(() => new Map(props.result.evidence?.map(node => [node.id, node.name])))
</script>

<template>
  <section class="panel result" aria-label="查询结果">
    <div class="panel-head result-head">
      <div><div class="panel-title">查询结果</div><p class="meta"><span :class="'status ' + result.status">{{ statuses[result.status] }}</span> {{ result.elapsed_ms }} ms</p></div>
      <a class="secondary button" :href="'/api/queries/' + result.id + '/report'" download>导出 Markdown</a>
    </div>
    <div class="result-body">
      <div v-if="result.clarification" class="clarifications">
        <h3>确认{{ result.clarification.slot === 'target' ? '目标' : '起始' }}实体</h3>
        <p>存在多个名为 <strong>{{ result.clarification.name }}</strong> 的实体。</p>
        <button class="candidate" v-for="node in result.clarification.candidates" :key="node.id" @click="$emit('confirm', node)">
          <strong>{{ node.services?.join('、') || node.name }}</strong>
          <span>{{ node.classes?.join('、') || node.label }} · {{ node.repository }} / {{ node.branch }}</span>
          <code>{{ node.path }}</code>
        </button>
        <p v-if="result.clarification.truncated" class="meta">更多候选可通过仓库、分支或服务范围过滤。</p>
      </div>
      <div v-if="result.error" class="callout error" role="alert">{{ result.error }}</div>
      <div v-if="result.summary" class="callout">{{ result.summary }}</div>
      <div class="tabs results-tabs">
        <button :class="{ active: tab === 'graph' }" @click="tab = 'graph'">结果与路径</button>
        <button :class="{ active: tab === 'trace' }" @click="tab = 'trace'">Cypher 与追踪</button>
      </div>
      <template v-if="tab === 'graph'">
        <template v-if="result.graph?.nodes.length">
          <p class="meta graph-meta">{{ result.graph.total_nodes }} 个节点 · {{ result.graph.total_edges }} 条边
            <span v-if="result.graph.truncated"> · 拓扑展示已截断，报告包含执行证据</span>
          </p>
          <Topology :graph="result.graph" :highlight="chosen !== undefined ? result.paths?.[chosen] : undefined" />
        </template>
        <details v-if="result.records?.length" class="details" open>
          <summary>结果记录（{{ result.records.length }}）</summary>
          <div class="table-scroll"><table>
            <thead><tr><th>名称 / 计数</th><th>全局 ID</th><th>证据类型</th></tr></thead>
            <tbody>
              <tr v-for="(record, i) in result.records" :key="record.id ?? i">
                <td>{{ record.name ?? record.count }}</td><td><code>{{ record.id ?? '—' }}</code></td>
                <td>{{ record.mechanisms?.join('、') || '去重计数' }}</td>
              </tr>
            </tbody>
          </table></div>
        </details>
        <details v-if="result.paths?.some(path => path.node_ids.length > 1)" class="details">
          <summary>路径证据（{{ result.paths.length }}）</summary>
          <ul class="path-list">
            <li v-for="(path, index) in result.paths" :key="index">
              <button class="path" :class="{ active: chosen === index }" @click="chosen = chosen === index ? undefined : index">
                <strong>{{ path.mechanism }}</strong> {{ path.node_ids.map(id => names.get(id) || id).join(' › ') }}
              </button>
            </li>
          </ul>
        </details>
      </template>
      <div v-else class="trace">
        <div v-for="(query, i) in result.queries" :key="i">
          <h3>{{ i + 1 }}. {{ query.mechanism }}</h3>
          <pre>{{ query.cypher }}</pre><pre>{{ JSON.stringify(query.parameters, null, 2) }}</pre>
        </div>
        <ol class="events">
          <li v-for="(event, i) in result.trace" :key="i">
            <strong>{{ event.stage }}</strong><span>{{ event.ms }} ms</span>
            <details><summary>阶段详情</summary><pre>{{ JSON.stringify(event, null, 2) }}</pre></details>
          </li>
        </ol>
        <details class="details"><summary>运行版本与查询 ID</summary><code>{{ result.id }}</code><pre>{{ JSON.stringify(result.versions, null, 2) }}</pre></details>
      </div>
    </div>
  </section>
</template>
