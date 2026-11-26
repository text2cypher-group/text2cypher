<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from 'vue'
import cytoscape from 'cytoscape'
import type { Core, ElementDefinition } from 'cytoscape'
import type { Graph, GraphEdge, GraphNode, PathEvidence } from './api'

const props = defineProps<{ graph: Graph; highlight?: PathEvidence }>()
const container = ref<HTMLElement>()
const selected = ref<GraphNode | GraphEdge>()
let cy: Core | undefined
let observer: ResizeObserver | undefined

const palette: Record<string, string> = {
  service: '#2367c9', method: '#6547ac', api: '#007c70', class: '#995124',
  exchange: '#a43b6b', queue: '#a43b6b', config: '#526478', test: '#526478',
}

watch([() => props.graph, container], ([graph, host]) => {
  cy?.destroy()
  observer?.disconnect()
  selected.value = undefined
  if (!host) return
  const elements: ElementDefinition[] = [
    ...graph.nodes.map(node => ({ data: { ...node, id: 'n:' + node.id, original: node,
      color: palette[node.kind] || '#526478' } })),
    ...graph.edges.map(edge => ({ data: { ...edge, id: 'e:' + edge.id,
      source: 'n:' + edge.source, target: 'n:' + edge.target,
      caption: edge.phase || edge.label, original: edge } })),
  ]
  cy = cytoscape({ container: host, elements, minZoom: .12, maxZoom: 3,
    layout: { name: graph.edges.length > 0 ? 'breadthfirst' : 'grid', padding: 45, directed: true, circle: false },
    style: [
      { selector: 'node', style: { 'background-color': 'data(color)', label: 'data(name)', 'font-size': 11,
        'text-wrap': 'ellipsis', 'text-max-width': '145px', color: '#233851',
        'text-valign': 'bottom', 'text-margin-y': 8, 'width': 21, 'height': 21, 'border-width': 2,
        'border-color': '#ffffff', 'border-opacity': .85 } },
      { selector: 'edge', style: { width: 1.4, 'curve-style': 'bezier', 'target-arrow-shape': 'triangle',
        'line-color': '#8c9fba', 'target-arrow-color': '#8c9fba', label: 'data(caption)',
        'font-size': 9, color: '#4d5970', 'text-background-color': '#f8fbff',
        'text-background-opacity': .88, 'text-background-padding': '2px' } },
      { selector: ':selected', style: { 'overlay-color': '#2e6fcb', 'overlay-opacity': .22 } },
      { selector: '.faded', style: { opacity: .2 } },
    ],
  })
  cy.on('tap', 'node, edge', event => { selected.value = event.target.data('original') })
  cy.on('tap', event => { if (event.target === cy) selected.value = undefined })
  observer = new ResizeObserver(() => cy?.resize())
  observer.observe(host)
}, { flush: 'post', immediate: true })

watch(() => props.highlight, (path) => {
  cy?.elements().removeClass('faded')
  if (!path || !cy) return
  const allowed = new Set([...path.node_ids.map(id => 'n:' + id), ...path.edge_ids.map(id => 'e:' + id)])
  cy.elements().filter(item => !allowed.has(item.id())).addClass('faded')
})

function zoom(factor: number) {
  if (cy) cy.zoom({ level: cy.zoom() * factor, renderedPosition: { x: cy.width() / 2, y: cy.height() / 2 } })
}

onBeforeUnmount(() => { observer?.disconnect(); cy?.destroy() })
</script>

<template>
  <div class="graph-view">
    <div ref="container" class="graph-canvas" role="img" aria-label="代码图谱拓扑，路径可在下方查看"></div>
    <div class="graph-buttons">
      <button class="zoom" title="放大" aria-label="放大" @click="zoom(1.25)">+</button>
      <button class="zoom" title="缩小" aria-label="缩小" @click="zoom(.8)">−</button>
      <button class="zoom fit" title="适应画布" aria-label="适应画布" @click="cy?.fit(undefined, 40)">适应</button>
    </div>
  </div>
  <dl v-if="selected" class="node-details">
    <template v-if="'name' in selected">
      <dt>实体</dt><dd>{{ selected.label }} · {{ selected.name }}</dd>
      <dt>全局 ID</dt><dd><code>{{ selected.id }}</code></dd>
      <dt>仓库 / 分支</dt><dd>{{ selected.repository || '—' }} / {{ selected.branch || '—' }}</dd>
      <dt>文件</dt><dd>{{ selected.path || '—' }}</dd>
    </template>
    <template v-else>
      <dt>关系</dt><dd>{{ selected.label }} {{ selected.phase }}</dd>
      <dt>证据</dt><dd><code>{{ selected.source }} → {{ selected.target }}</code></dd>
    </template>
  </dl>
</template>
