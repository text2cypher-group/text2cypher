from dataclasses import dataclass
from typing import NamedTuple

from .models import Plan, Scope
from .schema import q


class Step(NamedTuple):
    relation: str
    to: str = ""
    direction: str = "out"
    max_hops: int = 1
    phase: str = ""


class Pattern(NamedTuple):
    mechanism: str
    steps: tuple[Step, ...] = ()
    exclude_origin_service: bool = False


@dataclass(frozen=True)
class Query:
    text: str
    params: dict
    pattern: Pattern
    plan: Plan
    scope: Scope
    origin_service: str = ""


def patterns(plan):
    op, entity, depth = plan.operation, plan.entity, plan.depth
    owners = Step("owns", "service", max_hops=3)
    members = Step("owns", "method", "in", 3)
    callees = Step("calls", "method", max_hops=depth)
    callers = Step("calls", "method", "in", depth)
    api_callers = Step("calls", "api", "in", depth)
    publish = Step("message", "exchange", phase="publish")
    route = Step("message", "queue", phase="route")
    consume = Step("message", "method", phase="consume")
    exchange = Step("message", "exchange", "in", phase="route")
    producer = Step("message", "method", "in", phase="publish")
    rpc = Pattern("RPC", (members, callees, owners), True)
    service_mq = Pattern("MQ", (members, publish, route, consume, owners), True)
    if op in {"list", "count"}:
        return [Pattern("结构")]
    if op == "owner":
        return [Pattern("归属", (Step("owns", "class" if entity in {"method", "api", "test"} else "service"),))]
    if op == "entry":
        if entity == "method":
            return [Pattern("入口 API", (api_callers,))]
        return [Pattern("入口 API", (Step("owns", "api", "in", 3),))]
    if op == "downstream":
        return [Pattern("RPC", (callees, owners), True)]
    if op == "callers":
        return [Pattern("RPC", (members, callers, owners), True)]
    if op == "impact":
        return [Pattern("变更影响", (api_callers,))]
    if op == "call_chain":
        chains = []
        if plan.direction in {"forward", "both"}:
            chains.append(Pattern("正向调用", (callees,)))
        if plan.direction in {"reverse", "both"}:
            chains.append(Pattern("反向调用", (callers,)))
        return chains
    if op == "publishers":
        return [Pattern("MQ 发布", (exchange, producer) if entity == "queue" else (producer,))]
    if op == "mq":
        return {
            "service": [service_mq],
            "method": [Pattern("MQ", (publish, route, consume, owners))],
            "exchange": [Pattern("MQ 发布", (producer,)), Pattern("MQ 消费", (route, consume))],
            "queue": [Pattern("MQ 发布", (exchange, producer)), Pattern("MQ 消费", (consume,))],
        }[entity]
    if op == "rpc":
        return [rpc]
    if op == "dependencies":
        return [rpc, service_mq]
    if op in {"imports", "implements", "inherits"}:
        return [Pattern({"imports": "导入", "implements": "接口实现", "inherits": "继承"}[op],
                        (Step(op, "class"),))]
    raise ValueError("未知查询意图")


def compile_queries(schema, plan, scope, anchor="", target="", origin="", limit=1000):
    return [compile_query(schema, plan, scope, pattern, anchor, target, origin, limit)
            for pattern in patterns(plan)]


def compile_query(schema, plan, scope, pattern, anchor, target, origin, limit):
    params = {}
    filters = []
    start = f"(n:{schema.label(plan.entity)})"
    anchor_property = schema.property(plan.entity, "id")
    if anchor:
        params["anchor"] = anchor
        filters.append(f"toString(n.{anchor_property}) = $anchor")
    if plan.contains:
        params["term"] = plan.contains.lower()
        filters.append(f"toLower(n.{schema.property(plan.entity, 'name')}) CONTAINS $term")
    path = start
    result = "n"
    end_type = plan.entity
    for index, step in enumerate(pattern.steps):
        relationship = schema.relationship(step.relation, step.phase)
        repeat = f"*1..{step.max_hops}" if step.max_hops != 1 else ""
        edge_var = f"r{index}"
        body = f"[{edge_var}:{relationship}{repeat}]"
        link = f"<-{body}-" if step.direction == "in" else f"-{body}->"
        if step.phase:
            key = f"phase{index}"
            params[key] = schema.contract.phases[step.phase]
            filters.append(f"{edge_var}.{q(schema.contract.fields['phase'])} = ${key}")
        result = f"n{index + 1}"
        end_type = step.to
        path += f"{link}({result}:{schema.label(end_type)})"
    expression = "p = " + path if pattern.steps else path
    id_field = schema.property(end_type, "id")
    name_field = schema.property(end_type, "name")
    filters.append(f"{result}.{id_field} IS NOT NULL")
    if pattern.exclude_origin_service and origin:
        params["origin"] = origin
        filters.append(f"toString({result}.{id_field}) <> $origin")
    if target:
        if end_type != "service":
            raise ValueError("该路径无法按目标微服务过滤")
        params["target"] = target
        filters.append(f"toString({result}.{id_field}) = $target")
    for key in ("repository", "branch"):
        value = getattr(scope, key)
        if value:
            params[key] = value
            prop = schema.common(key) if pattern.steps else schema.property(plan.entity, key)
            if pattern.steps:
                filters.append(f"all(item IN nodes(p) WHERE item.{prop} = ${key})")
            else:
                filters.append(f"n.{prop} = ${key}")
    if pattern.steps:
        filters.append("length(p) <= 10")
        filters.append("all(node IN nodes(p) WHERE single(other IN nodes(p) WHERE other = node))")
    if scope.service and not anchor:
        svc_prop = schema.property("service", "name")
        svc_label = schema.label("service")
        params["service"] = scope.service
        if plan.entity == "service":
            filters.append(f"n.{svc_prop} = $service")
        else:
            relation = schema.relationship("owns", "")
            filters.append(
                f"EXISTS {{ MATCH (n)-[:{relation}*1..3]->(svc:{svc_label}) "
                f"WHERE svc.{svc_prop} = $service }}"
            )
    query = f"MATCH {expression}\nWHERE " + "\n  AND ".join(filters)
    if plan.operation == "count":
        query += f"\nRETURN count(DISTINCT n.{id_field}) AS count"
    else:
        params["row_limit"] = limit + 1
        evidence = "p" if pattern.steps else "n"
        query += (
            f"\nRETURN {result}.{id_field} AS id, {result}.{name_field} AS name, {evidence} AS evidence"
            "\nORDER BY id LIMIT $row_limit"
        )
    return Query(query, params, pattern, plan, scope, origin)
