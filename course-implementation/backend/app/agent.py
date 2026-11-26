import asyncio
import os
from time import monotonic
from uuid import uuid4

from pydantic import ValidationError

from .compiler import compile_queries
from .graph import GraphError
from .knowledge import retrieve, version
from .models import Plan, Scope
from .planner import DemoPlanner, IntentError, SKILL_VERSION, reject_write_intent
from .schema import SchemaError
from .store import now


class ClarificationError(ValueError):
    pass


def query_examples(schema, example):
    plan = Plan.model_validate(example["plan"])
    queries = compile_queries(schema, plan, Scope(service=plan.service),
                              anchor="DEMO_ENTITY" if plan.name else "",
                              target="DEMO_TARGET" if plan.target_service else "")
    return [query.text for query in queries]


def summarize(results, rows, complete):
    prefix = "结果达到执行行数上限，汇总仅覆盖已返回的证据。" if not complete else ""
    if rows and "count" in rows[0]:
        return prefix + f"符合条件的唯一实体共 {rows[0]['count']} 个。"
    if not results:
        return prefix + "当前图谱范围内未发现符合条件的记录。请核对实体、方向、版本或遍历深度。"
    groups = {}
    for result in results:
        for mechanism in result["mechanisms"]:
            groups.setdefault(mechanism, []).append(result["name"])
    lines = []
    for mechanism, names in groups.items():
        visible = "、".join(names[:8])
        extra = f"等 {len(names)} 个实体" if len(names) > 8 else ""
        lines.append(f"{mechanism}匹配 {len(names)} 个实体（{visible}{extra}）")
    return prefix + "，".join(lines) + "。"


def format_rows(groups, limit):
    results, all_nodes, all_edges, paths = {}, {}, {}, {}
    counts = []
    complete = True
    for mechanism, rows in groups:
        if len(rows) > limit:
            complete = False
        for row in rows[:limit]:
            if "count" in row:
                counts.append({"count": row["count"]})
                continue
            identity = row["id"]
            if not row["nodes"] or row["nodes"][-1]["id"] != identity:
                raise GraphError("图谱返回的目标与路径末端不一致")
            ids = tuple(node["id"] for node in row["nodes"])
            edge_ids = tuple(edge["id"] for edge in row["edges"])
            if len(ids) != len(edge_ids) + 1 or len(set(ids)) != len(ids):
                raise GraphError("图谱返回的路径长度或环路不符合约束")
            for index, edge in enumerate(row["edges"]):
                if {edge["source"], edge["target"]} != set(ids[index:index + 2]):
                    raise GraphError("图谱返回的边与相邻节点不一致")
                all_edges[edge["id"]] = edge
            for node in row["nodes"]:
                all_nodes[node["id"]] = node
            entry = results.setdefault(identity, {"id": identity, "name": row["name"], "mechanisms": []})
            if mechanism not in entry["mechanisms"]:
                entry["mechanisms"].append(mechanism)
            key = (mechanism, ids, edge_ids)
            paths[key] = {"mechanism": mechanism, "node_ids": ids, "edge_ids": edge_ids}
    records = list(results.values())
    shown = list(all_nodes.values())[:100]
    visible_ids = {node["id"] for node in shown}
    shown_edges = [edge for edge in all_edges.values()
                   if {edge["source"], edge["target"]} <= visible_ids][:500]
    return {
        "records": counts or records, "complete": complete,
        "graph": {"nodes": shown, "edges": shown_edges, "total_nodes": len(all_nodes),
                  "total_edges": len(all_edges), "truncated": len(shown) < len(all_nodes) or len(shown_edges) < len(all_edges)},
        "paths": list(paths.values()), "evidence": list(all_nodes.values()),
        "evidence_edges": list(all_edges.values()), "summary": summarize(records, counts, complete),
    }


class Agent:
    def __init__(self, graph, planner, store, limit=1000):
        self.graph = graph
        self.planner = planner
        self.store = store
        self.limit = limit

    async def run(self, request, forced_plan=None, example_limit=None):
        begin = monotonic()
        response = {
            "id": uuid4().hex, "question": request.question, "level": request.level,
            "status": "error", "mode": self.graph.mode, "created_at": now(),
            "trace": [], "queries": [], "input": request.model_dump(),
            "versions": {"model": self.planner.version, "skill": SKILL_VERSION,
                         "code": os.getenv("CODE_COMMIT", "local")},
        }

        def trace(stage, started, **details):
            response["trace"].append({"stage": stage, "ms": round((monotonic() - started) * 1000, 1), **details})

        try:
            async with asyncio.timeout(30):
                await self.pipeline(request, response, trace, forced_plan, example_limit)
        except TimeoutError:
            response.update(error="单题超过 30 秒执行预算", status="timeout")
        except (GraphError, SchemaError, IntentError, ClarificationError, ValidationError) as exc:
            response["error"] = str(exc)
            if isinstance(exc, IntentError) and "只读" in str(exc):
                response["status"] = "rejected"
        except asyncio.CancelledError:
            response.update(error="任务已取消", status="cancelled")
            raise
        finally:
            response["elapsed_ms"] = round((monotonic() - begin) * 1000, 1)
            if response.get("error"):
                trace("结束", monotonic(), status=response["status"], message=response["error"])
            self.store.put("query", response["id"], response)
        return response

    async def pipeline(self, request, response, trace, forced_plan, example_limit):
        reject_write_intent(request.question)
        begin = monotonic()
        schema = await self.graph.snapshot()
        response["versions"]["schema"] = schema.hash
        trace("获取 Schema", begin, hash=schema.hash)
        prior = self.previous(request, schema)
        examples = self.store.list("example")
        response["versions"]["kb"] = version(examples)
        count = example_limit if example_limit is not None else (8 if getattr(self.planner, "semantic_rerank", False) else 3)
        begin = monotonic()
        retrieved = retrieve(request.question, examples, request.level, count)
        compatible = []
        for item in retrieved:
            try:
                compatible.append({**item, "cypher": query_examples(schema, item)})
            except (SchemaError, ValueError):
                continue
        trace("构建 Prompt", begin, examples=[item["id"] for item in compatible],
              scope=request.scope.model_dump(exclude_defaults=True))
        feedback = ""
        attempts = 1 if forced_plan or prior or isinstance(self.planner, DemoPlanner) else 3
        for attempt in range(attempts):
            started = monotonic()
            try:
                plan = forced_plan or (Plan.model_validate(prior["plan"]) if prior else
                       await self.planner.plan(request.question, schema, compatible, request.scope, feedback))
                response["plan"] = plan.model_dump()
                trace("生成查询计划", started, intent=plan.operation, attempt=attempt + 1, depth=plan.depth)
                started = monotonic()
                scope = request.scope.model_copy(update={"service": request.scope.service or plan.service})
                source = await self.bind(schema, plan.entity, plan.name, scope, request.candidate_id,
                                         "source", response, trace)
                if response["status"] == "clarify":
                    return
                target = await self.bind(schema, "service", plan.target_service,
                                         Scope(repository=scope.repository, branch=scope.branch),
                                         request.target_id, "target", response, trace)
                if response["status"] == "clarify":
                    return
                origin = source["service_ids"][0] if source and len(source["service_ids"]) == 1 else ""
                response["bindings"] = {key: item for key, item in [("source", source), ("target", target)] if item}
                if plan.operation == "downstream" and not origin:
                    raise GraphError("无法唯一确认方法所属服务，不能判定跨服务调用")
                queries = compile_queries(schema, plan, scope, source["id"] if source else "",
                                          target["id"] if target else "", origin, self.limit)
                trace("校验 Cypher", started, subqueries=len(queries), read_only=True, row_limit=self.limit)
                groups = []
                for query in queries:
                    started = monotonic()
                    item = {"mechanism": query.pattern.mechanism, "cypher": query.text,
                            "parameters": query.params, "attempt": attempt + 1}
                    response["queries"].append(item)
                    try:
                        rows = await self.graph.execute(schema, query)
                    except GraphError as exc:
                        item.update(status="error", error=str(exc))
                        raise
                    item.update(status="ok", row_count=len(rows))
                    trace("执行查询", started, mechanism=query.pattern.mechanism, rows=len(rows),
                          explain=self.graph.mode == "neo4j")
                    groups.append((query.pattern.mechanism, rows))
                started = monotonic()
                result = format_rows(groups, self.limit)
                if not result["records"] and attempt + 1 < attempts and not feedback:
                    feedback = "有效查询返回空结果。检查实体名、方向和意图；保持用户原意。"
                    trace("结果诊断", started, message=feedback)
                    continue
                if not result["records"]:
                    trace("结果诊断", started, message="在当前实体、方向和深度内未匹配到路径")
                response.update(status="ok", **result)
                trace("格式化证据", started, total_paths=len(result["paths"]), complete=result["complete"])
                return
            except (IntentError, SchemaError, GraphError, ValidationError) as exc:
                feedback = str(exc)
                trace("查询诊断", started, attempt=attempt + 1, message=feedback)
                if attempt + 1 == attempts:
                    raise

    def previous(self, request, schema):
        if not request.clarify_from:
            if request.candidate_id or request.target_id:
                raise ClarificationError("候选确认需要原查询 ID")
            return None
        prior = self.store.get("query", request.clarify_from)
        if not prior or prior["status"] != "clarify" or prior["versions"].get("schema") != schema.hash:
            raise ClarificationError("澄清请求已过期，请重新提问")
        for field in ("question", "level", "scope"):
            if request.model_dump()[field] != prior["input"][field]:
                raise ClarificationError("请为原问题选择候选")
        slot = prior["clarification"]["slot"]
        selected = request.candidate_id if slot == "source" else request.target_id
        if selected not in {item["id"] for item in prior["clarification"]["candidates"]}:
            raise ClarificationError("请选择原查询给出的候选实体")
        other = "target_id" if slot == "source" else "candidate_id"
        if getattr(request, other) != prior["input"][other]:
            raise ClarificationError("此前确认的实体已变更，请重新提问")
        return prior

    async def bind(self, schema, entity, name, scope, selected, slot, response, trace):
        if not name:
            return None
        start = monotonic()
        candidates = await self.graph.resolve(schema, entity, name, scope)
        trace("解析实体", start, entity=entity, name=name, candidates=len(candidates), slot=slot)
        if selected:
            for item in candidates:
                if item["id"] == selected:
                    return item
            raise ClarificationError("所选实体在当前图谱范围内已不可用")
        if not candidates:
            raise GraphError(f"当前范围未找到实体 {name}")
        if len(candidates) > 1:
            response.update(status="clarify", clarification={"slot": slot, "name": name,
                            "candidates": candidates[:20], "truncated": len(candidates) > 20})
            return None
        return candidates[0]
