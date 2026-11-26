import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .agent import Agent, query_examples
from .graph import DemoGraph, GraphError, Neo4jGraph
from .models import BatchRequest, Example, QueryRequest
from .planner import make_planner
from .reports import markdown_report
from .schema import DATA, Contract, SchemaError, load_json
from .store import Store, now

ROOT = Path(__file__).resolve().parents[2]
load_dotenv(ROOT / ".env")


@asynccontextmanager
async def lifespan(app):
    contract = Contract(Path(os.getenv("GRAPH_SCHEMA") or DATA / "schema.demo.json"))
    graph_mode = os.getenv("GRAPH_MODE", "demo")
    if graph_mode == "demo":
        graph = DemoGraph(contract, load_json(DATA / "graph.demo.json"))
    elif graph_mode == "neo4j":
        uri, user, password = (os.environ.get(key, "") for key in ("NEO4J_URI", "NEO4J_USER", "NEO4J_PASSWORD"))
        if not uri or not user or not password:
            raise ValueError("Neo4j 模式需要连接 URI 和独立只读账号")
        graph = Neo4jGraph(contract, uri, os.getenv("NEO4J_DATABASE", "neo4j"), user, password)
    else:
        raise ValueError("GRAPH_MODE 仅接受 demo 或 neo4j")
    planner = make_planner()
    seeds = Path(os.getenv("KB_SEED") or DATA / ("kb.demo.json" if graph_mode == "demo" else "kb.empty.json"))
    location = os.getenv("SQLITE_PATH") or ROOT / "artifacts" / f"{graph_mode}.sqlite3"
    try:
        store = Store(location, seeds)
        app.state.agent = Agent(graph, planner, store)
        app.state.jobs = {}
        app.state.batch_slots = asyncio.Semaphore(2)
        yield
    finally:
        tasks = list(getattr(app.state, "jobs", {}).values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await planner.close()
        await graph.close()


app = FastAPI(title="CodeGraph Text2Cypher", version="1.0.0", lifespan=lifespan)


def agent(request: Request):
    return request.app.state.agent


def require(store, kind, id_):
    value = store.get(kind, id_)
    if value is None:
        raise HTTPException(404, "未找到指定记录")
    return value


async def snapshot(service):
    try:
        return await service.graph.snapshot()
    except GraphError as exc:
        raise HTTPException(503, str(exc)) from exc


@app.get("/api/health")
async def health(request: Request):
    service = agent(request)
    return {"status": "ok", "graph": service.graph.mode, "model": service.planner.version}


@app.get("/api/schema")
async def schema(request: Request):
    active = await snapshot(agent(request))
    return {"hash": active.hash, **active.snapshot, "mapping": active.contract.raw}


@app.post("/api/queries")
async def query(body: QueryRequest, request: Request):
    return await agent(request).run(body)


@app.get("/api/queries/{id_}")
async def query_details(id_: str, request: Request):
    return require(agent(request).store, "query", id_)


@app.get("/api/queries/{id_}/report")
async def report(id_: str, request: Request):
    value = require(agent(request).store, "query", id_)
    return Response(markdown_report(value), media_type="text/markdown",
                    headers={"Content-Disposition": f'attachment; filename="query-{id_}.md"'})


async def run_batch(app, batch, questions):
    service = app.state.agent
    store = service.store

    async def run_item(index, question):
        item = batch["items"][index]
        async with app.state.batch_slots:
            item["status"] = "running"
            store.put("batch", batch["id"], batch)
            try:
                result = await service.run(question)
                item.update(status=result["status"], query_id=result["id"], elapsed_ms=result["elapsed_ms"],
                            error=result.get("error"))
            except Exception:
                item.update(status="error", error="任务发生内部错误")
            finally:
                store.put("batch", batch["id"], batch)

    batch["status"] = "running"
    store.put("batch", batch["id"], batch)
    workers = [asyncio.create_task(run_item(index, question)) for index, question in enumerate(questions)]
    try:
        await asyncio.gather(*workers)
        batch["status"] = "done"
    except asyncio.CancelledError:
        for worker in workers:
            worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        for item in batch["items"]:
            if item["status"] in {"queued", "running"}:
                item["status"] = "cancelled"
        batch["status"] = "cancelled"
    finally:
        store.put("batch", batch["id"], batch)


@app.post("/api/batches", status_code=202)
async def create_batch(body: BatchRequest, request: Request):
    app_ = request.app
    store = agent(request).store
    id_ = uuid4().hex
    batch = {"id": id_, "created_at": now(), "status": "queued",
             "items": [{"number": i + 1, "question": q.question, "level": q.level, "status": "queued"}
                       for i, q in enumerate(body.questions)]}
    store.put("batch", id_, batch)
    task = asyncio.create_task(run_batch(app_, batch, body.questions))
    app_.state.jobs[id_] = task
    task.add_done_callback(lambda _: app_.state.jobs.pop(id_, None))
    return batch


@app.get("/api/batches/{id_}")
async def batch_details(id_: str, request: Request):
    return require(agent(request).store, "batch", id_)


@app.post("/api/batches/{id_}/cancel")
async def cancel_batch(id_: str, request: Request):
    store = agent(request).store
    batch = require(store, "batch", id_)
    task = request.app.state.jobs.get(id_)
    if task and not task.done():
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        batch = require(store, "batch", id_)
        if batch["status"] == "queued":
            batch["status"] = "cancelled"
            for item in batch["items"]:
                item["status"] = "cancelled"
            store.put("batch", id_, batch)
    return batch


@app.get("/api/examples")
async def examples(request: Request, keyword: str = "", level: str = "", category: str = ""):
    rows = agent(request).store.list("example")
    keyword = keyword.casefold()
    return [row for row in rows if (not level or row["level"] == level)
            and (not category or row["category"] == category)
            and (not keyword or any(keyword in text.casefold() for text in
                 (row["question"], row["category"], row["plan"]["name"])))]


@app.get("/api/examples/{id_}/preview")
async def example_preview(id_: str, request: Request):
    service = agent(request)
    row = require(service.store, "example", id_)
    active = await snapshot(service)
    try:
        return {"cypher": query_examples(active, row)}
    except (SchemaError, ValueError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/examples/{id_}/history")
async def example_history(id_: str, request: Request):
    return agent(request).store.history(id_)


@app.post("/api/examples", status_code=201)
async def create_example(body: Example, request: Request):
    return agent(request).store.write_example(body)


@app.put("/api/examples/{id_}")
async def update_example(id_: str, body: Example, request: Request):
    value = agent(request).store.write_example(body, id_)
    if not value:
        raise HTTPException(404, "未找到指定示例")
    return value


@app.delete("/api/examples/{id_}", status_code=204)
async def delete_example(id_: str, request: Request):
    if not agent(request).store.delete_example(id_):
        raise HTTPException(404, "未找到指定示例")
    return Response(status_code=204)


DIST = ROOT / "frontend" / "dist"
if (DIST / "index.html").is_file():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def home():
        return FileResponse(DIST / "index.html")
