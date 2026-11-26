import argparse
import asyncio
import json
import math
from pathlib import Path

from app.main import app, lifespan
from app.models import Example, Plan, QueryRequest
from app.schema import DATA, load_json
from app.store import now

GOLD = Path(__file__).with_name("gold.demo.json")


def compare(entry, result):
    if result["status"] != "ok" or not result.get("complete") or not result.get("summary"):
        return False, result.get("error", result["status"])
    records = result["records"]
    expected = entry["expected"]
    if isinstance(expected, int):
        actual = records[0].get("count") if len(records) == 1 else None
        return actual == expected, actual
    actual = sorted(row.get("id", "") for row in records)
    if actual != sorted(expected):
        return False, actual
    mechanisms = {row["id"]: sorted(row["mechanisms"]) for row in records}
    for identity, values in entry.get("mechanisms", {}).items():
        if mechanisms.get(identity) != sorted(values):
            return False, mechanisms
    required_paths = {(item["mechanism"], tuple(item["node_ids"])) for item in entry.get("required_paths", [])}
    actual_paths = {(row["mechanism"], tuple(row["node_ids"])) for row in result["paths"]}
    return required_paths <= actual_paths, actual


async def evaluate(agent, entries, examples):
    rows = []
    for entry in entries:
        request = QueryRequest.model_validate({key: entry[key] for key in ("question", "level", "scope") if key in entry})
        result = await agent.run(request, example_limit=examples)
        for _ in range(2):
            if result["status"] != "clarify":
                break
            field = "candidate_id" if result["clarification"]["slot"] == "source" else "target_id"
            if not entry.get(field):
                break
            result = await agent.run(QueryRequest.model_validate({
                **result["input"], "clarify_from": result["id"], field: entry[field],
            }), example_limit=examples)
        passed, actual = compare(entry, result)
        rows.append({"id": entry["id"], "passed": passed, "expected": entry["expected"],
                     "actual": actual, "result": result})
    return rows


async def validate_kb(agent, path):
    rows = []
    for item in load_json(path):
        example = Example.model_validate({key: item[key] for key in ("question", "level", "plan", "review") if key in item})
        request = QueryRequest(question=example.question, level=example.level)
        result = await agent.run(request, forced_plan=Plan.model_validate(example.plan))
        records = result.get("records", [])
        nonempty = bool(records and ("count" not in records[0] or records[0]["count"] > 0))
        passed = result["status"] == "ok" and result.get("complete") and nonempty
        rows.append({"id": item["id"], "passed": bool(passed), "result": result})
    return rows


async def main(args):
    async with lifespan(app):
        agent = app.state.agent
        if args.kb:
            rows = await validate_kb(agent, args.kb)
        else:
            rows = await evaluate(agent, load_json(args.gold), 0 if args.no_examples else None)
        timings = sorted(row["result"]["elapsed_ms"] for row in rows)
        p95 = timings[math.ceil(len(timings) * .95) - 1] if timings else None
        score = sum(row["passed"] for row in rows)
        report = {"created_at": now(), "mode": agent.graph.mode, "model": agent.planner.version,
                  "dataset": str(args.kb or args.gold), "example_retrieval": not args.no_examples and not bool(args.kb),
                  "passed": score, "total": len(rows), "p95_ms": p95, "cases": rows}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"{agent.graph.mode} / {agent.planner.version} | {score}/{len(rows)} | P95 {p95} ms")
        print(args.output)
        for row in rows:
            if not row["passed"]:
                print(row["id"], row["result"]["status"], row["result"].get("error") or row.get("actual"))
        return 0 if rows and score == len(rows) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Text2Cypher 独立评测与知识库执行核验")
    parser.add_argument("--gold", type=Path, default=GOLD)
    parser.add_argument("--kb", type=Path, nargs="?", const=DATA / "kb.demo.json")
    parser.add_argument("--no-examples", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("../artifacts/evaluation.json"))
    raise SystemExit(asyncio.run(main(parser.parse_args())))
