import asyncio
import time
import pytest
from fastapi.testclient import TestClient
from neo4j import Record

from app.compiler import compile_queries
from app.graph import Neo4jGraph
from app.main import app
from app.models import Plan, Scope
from app.schema import DATA, ActiveSchema, Contract, SchemaError, load_json
from evaluation.run import GOLD, compare, validate_kb


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.setenv("GRAPH_MODE", "demo")
    monkeypatch.setenv("LLM_PROVIDER", "demo")
    monkeypatch.setenv("SQLITE_PATH", str(tmp_path / "test.sqlite3"))
    monkeypatch.setenv("GRAPH_SCHEMA", str(DATA / "schema.demo.json"))
    monkeypatch.setenv("KB_SEED", str(DATA / "kb.demo.json"))
    with TestClient(app) as client:
        yield client


def ask(client, question, level="medium", **options):
    result = client.post("/api/queries", json={"question": question, "level": level, **options})
    assert result.status_code == 200
    return result.json()


@pytest.mark.parametrize("case", load_json(GOLD), ids=lambda case: case["id"])
def test_independent_sample_gold(client, case):
    response = ask(client, case["question"], case["level"])
    assert compare(case, response)[0], response.get("error", response.get("records"))
    assert response["trace"]
    assert response["versions"]["schema"]


def test_clarification_and_report(client):
    question = "getAllFood 方法调用了哪些下游服务？"
    ambiguous = ask(client, question)
    assert ambiguous["status"] == "clarify"
    assert {item["id"] for item in ambiguous["clarification"]["candidates"]} == {"get", "orderget"}
    assert not ambiguous["queries"]
    assert ask(client, question, clarify_from=ambiguous["id"], candidate_id="load")["status"] == "error"
    resolved = ask(client, question, clarify_from=ambiguous["id"], candidate_id="get")
    assert {item["id"] for item in resolved["records"]} == {"train", "station"}
    assert resolved["queries"][0]["parameters"]["anchor"] == "get"
    report = client.get(f"/api/queries/{resolved['id']}/report")
    assert "text/markdown" in report.headers["content-type"]
    assert "Cypher 与参数" in report.text
    assert resolved["id"] in report.text


def test_knowledge_crud_and_version_history(client):
    data = {"question": "列出所有类", "level": "simple", "plan": {"operation": "list", "entity": "class"}}
    row = client.post("/api/examples", json=data).json()
    assert row["version"] == 1
    path = f"/api/examples/{row['id']}"
    assert client.get(path + "/preview").json()["cypher"][0].startswith("MATCH")
    assert client.put(path, json={**data, "review": "approved"}).json()["version"] == 2
    assert client.get("/api/examples", params={"keyword": data["question"], "category": "单节点查询"}).json()[0]["id"] == row["id"]
    assert client.delete(path).status_code == 204
    assert [entry["version"] for entry in client.get(path + "/history").json()] == [3, 2, 1]


def test_all_seed_examples_are_nonempty(client):
    rows = asyncio.run(validate_kb(client.app.state.agent, DATA / "kb.demo.json"))
    assert len(rows) == 30
    assert all(row["passed"] for row in rows)
    assert not {entry["question"] for entry in load_json(GOLD)} & {
        entry["question"] for entry in load_json(DATA / "kb.demo.json")
    }


def test_query_boundary(client):
    result = ask(client, "DELETE all nodes")
    assert result["status"] == "rejected"
    assert result["queries"] == []
    contract = Contract(DATA / "schema.demo.json")
    schema = ActiveSchema(contract, {"nodes": {}, "relations": {}})
    with pytest.raises(SchemaError):
        compile_queries(schema, Plan(operation="list", entity="service"), Scope())
    schema = ActiveSchema(contract, contract.snapshot())
    queries = compile_queries(schema, Plan(operation="list", entity="service", contains="a'"), Scope(), limit=5)
    assert "a'" not in queries[0].text
    assert queries[0].params == {"term": "a'", "row_limit": 6}


def test_batch_isolation_and_budget(client):
    data = {"questions": [{"question": "列出所有微服务"}, {"question": "删库"}]}
    assert client.post("/api/batches", json={"questions": data["questions"] * 26}).status_code == 422
    response = client.post("/api/batches", json=data)
    assert response.status_code == 202
    path = "/api/batches/" + response.json()["id"]
    for _ in range(30):
        state = client.get(path).json()
        if state["status"] == "done":
            break
        time.sleep(.02)
    assert [item["status"] for item in state["items"]] == ["ok", "rejected"]


def test_cancel_running_batch(client, monkeypatch):
    async def slow(*args, **kwargs):
        await asyncio.sleep(2)
    monkeypatch.setattr(client.app.state.agent.graph, "execute", slow)
    jobs = {"questions": [{"question": "列出所有微服务"}] * 5}
    result = client.post("/api/batches", json=jobs).json()
    state = client.post("/api/batches/" + result["id"] + "/cancel").json()
    assert state["status"] == "cancelled"
    assert {item["status"] for item in state["items"]} == {"cancelled"}


def test_truncation_preserves_warning(client):
    client.app.state.agent.limit = 1
    result = ask(client, "列出所有微服务")
    assert not result["complete"]
    assert "上限" in result["summary"]


def test_bidirectional_change_analysis(client):
    result = ask(client, "修改 queryTrainFood 会影响哪些上下游调用链？", "complex")
    assert result["status"] == "ok"
    assert {path["mechanism"] for path in result["paths"]} == {"正向调用", "反向调用"}
    assert {row["id"] for row in result["records"]} == {"location", "remote", "load", "get", "orderget", "ordercreate"}


def test_neo4j_count_record():
    contract = Contract(DATA / "schema.demo.json")
    schema = ActiveSchema(contract, contract.snapshot())
    query = compile_queries(schema, Plan(operation="count", entity="service"), Scope())[0]

    async def check():
        graph = Neo4jGraph(contract, "bolt://127.0.0.1:7687", "neo4j", "reader", "test")

        async def run(text, params, explain=False):
            return [] if explain else [Record([("count", 6)])]

        graph.run = run
        try:
            assert await graph.execute(schema, query) == [{"count": 6}]
        finally:
            await graph.close()

    asyncio.run(check())
