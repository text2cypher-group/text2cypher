export type Level = 'simple' | 'medium' | 'complex'
export const levels: Record<Level, string> = { simple: '简单', medium: '中等', complex: '复杂' }

export interface Scope { service: string; repository: string; branch: string }
export interface QueryInput {
  question: string; level: Level; scope?: Scope;
  candidate_id?: string; target_id?: string; clarify_from?: string;
}

export interface GraphNode {
  id: string; label: string; name: string; kind: string;
  repository: string; branch: string; path: string;
  classes?: string[]; services?: string[];
}
export interface GraphEdge { id: string; label: string; source: string; target: string; phase: string }
export interface Graph {
  nodes: GraphNode[]; edges: GraphEdge[]; total_nodes: number; total_edges: number; truncated: boolean;
}
export interface Row { id?: string; name?: string; mechanisms?: string[]; count?: number }
export interface PathEvidence { mechanism: string; node_ids: string[]; edge_ids: string[] }
export interface Result {
  id: string; question: string; status: string; error?: string; summary?: string; elapsed_ms: number;
  input: QueryInput; mode: string; complete?: boolean; graph?: Graph; records?: Row[]; paths?: PathEvidence[];
  evidence?: GraphNode[]; evidence_edges?: GraphEdge[];
  versions: Record<string, string>; trace: ({ stage: string; ms: number } & Record<string, unknown>)[];
  queries: { mechanism: string; cypher: string; parameters: Record<string, unknown>; status?: string }[];
  clarification?: { slot: 'source' | 'target'; name: string; candidates: GraphNode[]; truncated: boolean };
}
export interface Batch {
  id: string; status: string;
  items: { number: number; question: string; status: string; query_id?: string; error?: string }[];
}
export interface Plan {
  operation: string; entity: string; name: string; service: string;
  target_service: string; contains: string; depth: number; direction: 'forward' | 'reverse' | 'both';
}
export interface Example {
  id: string; question: string; level: Level; review: 'draft' | 'approved';
  category: string; version: number; plan: Plan; updated_at: string; deleted?: boolean;
}

export const statuses: Record<string, string> = {
  ok: '完成', error: '失败', rejected: '已拒绝', timeout: '超时', clarify: '待确认',
  running: '执行中', queued: '排队中', cancelled: '已取消', interrupted: '已中断', done: '已完成',
}

export async function api<T>(path: string, method = 'GET', body?: unknown): Promise<T> {
  const response = await fetch('/api' + path, {
    method,
    headers: body ? { 'Content-Type': 'application/json' } : undefined,
    body: body ? JSON.stringify(body) : undefined,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null)
    const detail = payload?.detail
    if (typeof detail === 'string') throw new Error(detail)
    if (Array.isArray(detail))
      throw new Error(detail.map(item => item.loc.slice(1).join('.') + ' ' + item.msg).join('，'))
    throw new Error('请求失败，请检查后端连接')
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}
