import argparse
import os
from urllib.parse import urlsplit

from neo4j import GraphDatabase

from app.schema import DATA, Contract, load_json, q


def load_fixture(tx, contract, fixture):
    if tx.run("MATCH (n) RETURN count(n) AS size").single()["size"]:
        raise RuntimeError("只能向空的本地样例数据库导入数据")
    id_field = q(contract.fields["id"])
    for node in fixture["nodes"]:
        kind = contract.kinds[node["kind"]]
        properties = {contract.fields[key]: node[key] for key in ("id", "repository", "branch", "path")}
        properties[kind.name] = node["name"]
        tx.run(f"CREATE (n:{q(kind.label)}) SET n = $properties", properties=properties).consume()
    for edge in fixture["edges"]:
        relation = q(contract.relations[edge["relation"]])
        properties = {contract.fields["phase"]: contract.phases[edge["phase"]]} if edge["phase"] else {}
        tx.run(
            f"MATCH (a), (b) WHERE a.{id_field} = $source AND b.{id_field} = $target "
            f"CREATE (a)-[r:{relation}]->(b) SET r = $properties",
            source=edge["source"], target=edge["target"], properties=properties,
        ).consume()


def main():
    parser = argparse.ArgumentParser(description="导入本项目的合成样例图谱")
    parser.add_argument("--uri", default="bolt://127.0.0.1:7687")
    parser.add_argument("--database", default="neo4j")
    args = parser.parse_args()
    uri = urlsplit(args.uri)
    if uri.scheme not in {"bolt", "neo4j"} or uri.hostname not in {"localhost", "127.0.0.1", "::1"}:
        parser.error("样例导入仅支持本机回环 Neo4j")
    auth = None
    if os.getenv("NEO4J_SEED_PASSWORD"):
        auth = (os.getenv("NEO4J_SEED_USER", "neo4j"), os.environ["NEO4J_SEED_PASSWORD"])
    contract = Contract(DATA / "schema.demo.json")
    fixture = load_json(DATA / "graph.demo.json")
    with GraphDatabase.driver(args.uri, auth=auth) as driver:
        with driver.session(database=args.database) as session:
            session.execute_write(load_fixture, contract, fixture)
    print(f"导入 {len(fixture['nodes'])} 个节点，{len(fixture['edges'])} 条关系")


if __name__ == "__main__":
    main()
