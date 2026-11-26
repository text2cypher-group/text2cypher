import asyncio
from collections import defaultdict
from time import monotonic

from neo4j import AsyncGraphDatabase, Query as BoltQuery, READ_ACCESS
from neo4j.exceptions import Neo4jError, ServiceUnavailable, SessionExpired
from neo4j.graph import Node, Path

from .compiler import Step
from .models import Scope
from .schema import ActiveSchema, q


class GraphError(ValueError):
    pass


class DemoGraph:
    mode = "demo"

    def __init__(self, contract, data):
        self.schema = ActiveSchema(contract, contract.snapshot())
        self.nodes = {node["id"]: node for node in data["nodes"]}
        self.out = defaultdict(list)
        self.inc = defaultdict(list)
        for index, edge in enumerate(data["edges"]):
            item = {
                **edge, "id": f"edge-{index}",
                "label": contract.relations[edge["relation"]],
                "phase": contract.phases.get(edge["phase"], ""),
            }
            self.out[edge["source"]].append(item)
            self.inc[edge["target"]].append(item)

    async def snapshot(self):
        return self.schema

    async def close(self):
        pass

    def walk(self, start, step, visited):
        adjacency = self.out if step.direction == "out" else self.inc
        next_key = "target" if step.direction == "out" else "source"
        phase = self.schema.contract.phases.get(step.phase)

        def visit(current, path, edges):
            if len(edges) == step.max_hops:
                return
            for edge in adjacency[current]:
                target = edge[next_key]
                if edge["relation"] != step.relation or target in visited or target in path:
                    continue
                if phase and phase != edge["phase"]:
                    continue
                next_path = (*path, target)
                next_edges = (*edges, edge)
                if self.nodes[target]["kind"] == step.to:
                    yield next_path, next_edges
                yield from visit(target, next_path, next_edges)

        yield from visit(start, (), ())

    def details(self, node):
        id_ = node["id"]
        service_ids = [id_] if node["kind"] == "service" else sorted({
            ids[-1] for ids, _ in self.walk(id_, Step("owns", "service", max_hops=3), (id_,))
        })
        class_names = [self.nodes[ids[-1]]["name"] for ids, _ in
                       self.walk(id_, Step("owns", "class"), (id_,))]
        return {**node, "services": [self.nodes[i]["name"] for i in service_ids],
                "service_ids": service_ids, "classes": sorted(set(class_names)),
                "label": self.schema.contract.kinds[node["kind"]].label}

    @staticmethod
    def version_matches(node, scope):
        return all(not value or node[key] == value for key, value in
                   (("repository", scope.repository), ("branch", scope.branch)))

    async def resolve(self, schema, entity, name, scope):
        found = []
        for node in sorted(self.nodes.values(), key=lambda n: n["id"]):
            if node["kind"] != entity or node["name"] != name or not self.version_matches(node, scope):
                continue
            item = self.details(node)
            if not scope.service or scope.service in item["services"]:
                found.append(item)
        return found[:21]

    async def execute(self, schema, query):
        plan, scope, params = query.plan, query.scope, query.params
        matched = []
        for node in sorted(self.nodes.values(), key=lambda n: n["id"]):
            if node["kind"] != plan.entity or not self.version_matches(node, scope):
                continue
            if params.get("anchor") and params["anchor"] != node["id"]:
                continue
            if params.get("service") and scope.service not in self.details(node)["services"]:
                continue
            if params.get("term") and params["term"] not in node["name"].lower():
                continue
            matched.append(node["id"])
        if plan.operation == "count":
            return [{"count": len(matched)}]

        def expand(index, ids, edges):
            if index == len(query.pattern.steps):
                yield ids, edges
                return
            step = query.pattern.steps[index]
            for extra_nodes, extra_edges in self.walk(ids[-1], step, ids):
                if len(edges) + len(extra_edges) > 10:
                    continue
                if all(self.version_matches(self.nodes[n], scope) for n in extra_nodes):
                    yield from expand(index + 1, (*ids, *extra_nodes), (*edges, *extra_edges))

        rows = []
        for start in matched:
            for ids, edges in expand(0, (start,), ()):
                end = ids[-1]
                if end == params.get("origin") or (params.get("target") and end != params["target"]):
                    continue
                rows.append({"id": end, "name": self.nodes[end]["name"],
                             "nodes": [self.details(self.nodes[i]) for i in ids], "edges": list(edges)})
        rows.sort(key=lambda row: row["id"])
        return rows[:params["row_limit"]]


class Neo4jGraph:
    mode = "neo4j"

    def __init__(self, contract, uri, database, user, password):
        self.contract = contract
        self.database = database
        self.driver = AsyncGraphDatabase.driver(
            uri, auth=(user, password), connection_timeout=5, connection_acquisition_timeout=5,
            max_transaction_retry_time=0,
        )
        self.cached_schema = None
        self.expires = 0
        self.lock = asyncio.Lock()

    async def close(self):
        await self.driver.close()

    async def run(self, text, params=None, explain=False):
        try:
            async with self.driver.session(database=self.database, default_access_mode=READ_ACCESS) as session:
                result = await session.run(BoltQuery(text, timeout=5), params or {})
                if explain:
                    summary = await result.consume()
                    if summary.query_type != "r":
                        raise GraphError("Neo4j 查询计划没有确认只读模式")
                    return []
                return [record async for record in result]
        except Neo4jError as exc:
            code = exc.code.rsplit(".", 1)[-1] if exc.code else "Neo4jError"
            raise GraphError(f"Neo4j {code}，请检查 Schema、查询预算和只读权限") from exc
        except (ServiceUnavailable, SessionExpired) as exc:
            raise GraphError("Neo4j 暂不可用，检查连接与数据库状态") from exc

    async def snapshot(self):
        async with self.lock:
            if self.cached_schema and self.expires > monotonic():
                return self.cached_schema
            nodes = defaultdict(set)
            rels = defaultdict(set)
            node_rows = await self.run(
                "CALL db.schema.nodeTypeProperties() YIELD nodeLabels, propertyName RETURN nodeLabels, propertyName"
            )
            rel_rows = await self.run(
                "CALL db.schema.relTypeProperties() YIELD relType, propertyName RETURN relType, propertyName"
            )
            for row in node_rows:
                for label in row["nodeLabels"]:
                    keys = nodes[label]
                    if row["propertyName"]:
                        keys.add(row["propertyName"])
            for row in rel_rows:
                keys = rels[row["relType"].lstrip(":").strip("`")]
                if row["propertyName"]:
                    keys.add(row["propertyName"])
            snapshot = {"nodes": {k: sorted(v) for k, v in nodes.items()},
                        "relations": {k: sorted(v) for k, v in rels.items()}}
            self.cached_schema = ActiveSchema(self.contract, snapshot)
            self.expires = monotonic() + 300
            return self.cached_schema

    def node(self, value, preferred_kind=""):
        props = dict(value)
        fields = self.contract.fields
        kind = preferred_kind or next((self.contract.by_label[label] for label in sorted(value.labels)
                                      if label in self.contract.by_label), "")
        display = self.contract.kinds.get(kind)
        return {"id": str(props.get(fields["id"]) if props.get(fields["id"]) is not None
                          else "neo4j:" + value.element_id),
                "name": str(props.get(display.name) or value.element_id) if display else value.element_id,
                "kind": kind, "label": display.label if display else ", ".join(sorted(value.labels)),
                **{key: str(props.get(fields[key], "")) for key in ("repository", "branch", "path")}}

    async def resolve(self, schema, entity, name, scope):
        fields = [f"n.{schema.property(entity, 'name')} = $name",
                  f"n.{schema.property(entity, 'id')} IS NOT NULL"]
        params = {"name": name}
        for key in ("repository", "branch"):
            if value := getattr(scope, key):
                fields.append(f"n.{schema.property(entity, key)} = ${key}")
                params[key] = value
        start = f"MATCH (n:{schema.label(entity)}) WHERE " + " AND ".join(fields)
        if entity == "service":
            rows = await self.run(start + f" RETURN n, [n] AS owners, [] AS classes ORDER BY n.{schema.property(entity, 'id')} LIMIT 21", params)
        else:
            known = schema.snapshot["relations"]
            if self.contract.relations["owns"] in known and self.contract.kinds["service"].label in schema.snapshot["nodes"]:
                rel = schema.relationship("owns", "")
                service = schema.label("service")
                start += f" OPTIONAL MATCH (n)-[:{rel}*1..3]->(svc:{service})"
                if scope.service:
                    params["service"] = scope.service
                    start += f" WHERE svc.{schema.property('service', 'name')} = $service"
                start += " WITH n, collect(DISTINCT svc) AS owners"
            else:
                start += " WITH n, [] AS owners"
            if scope.service:
                start += " WHERE size(owners) > 0"
            if self.contract.relations["owns"] in known and self.contract.kinds["class"].label in schema.snapshot["nodes"]:
                rel = schema.relationship("owns", "")
                start += f" OPTIONAL MATCH (n)-[:{rel}]->(cls:{schema.label('class')})"
                ending = " RETURN n, owners, collect(DISTINCT cls) AS classes"
            else:
                ending = " RETURN n, owners, [] AS classes"
            rows = await self.run(start + ending + f" ORDER BY n.{schema.property(entity, 'id')} LIMIT 21", params)
        found = []
        for row in rows:
            node = self.node(row["n"], entity)
            owners = [self.node(val, "service") for val in row["owners"]]
            if scope.service and all(val["name"] != scope.service for val in owners):
                continue
            found.append({**node, "services": sorted({val["name"] for val in owners}),
                          "service_ids": sorted({val["id"] for val in owners}),
                          "classes": sorted({self.node(val, "class")["name"] for val in row["classes"]})})
        return found

    async def execute(self, schema, query):
        await self.run("EXPLAIN " + query.text, query.params, explain=True)
        rows = await self.run(query.text, query.params)
        formatted = []
        for row in rows:
            if "count" in row.keys():
                formatted.append({"count": row["count"]})
                continue
            evidence = row["evidence"]
            nodes = [self.node(evidence)] if isinstance(evidence, Node) else [self.node(n) for n in evidence.nodes]
            edges = []
            if isinstance(evidence, Path):
                edges = [{"id": e.element_id, "source": self.node(e.start_node)["id"],
                          "target": self.node(e.end_node)["id"], "label": e.type,
                          "phase": str(e.get(self.contract.fields["phase"], ""))}
                         for e in evidence.relationships]
            formatted.append({"id": str(row["id"]), "name": str(row["name"]),
                              "nodes": nodes, "edges": edges})
        return formatted
