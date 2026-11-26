import json


def json_block(value):
    return "````json\n" + json.dumps(value, ensure_ascii=False, indent=2) + "\n````"


def markdown_report(result):
    lines = [
        "# 代码图谱查询报告", "", f"- 查询 ID `{result['id']}`", f"- 执行时间 {result['created_at']}",
        f"- 模式 `{result['mode']}`", f"- 状态 `{result['status']}`", f"- 耗时 {result['elapsed_ms']} 毫秒",
        "", "## 输入", "", json_block(result["input"]), "",
        "## 摘要", "", result.get("summary", result.get("error", "等待确认查询实体。")), "",
        "## 运行版本", "", json_block(result["versions"]), "",
    ]
    if result.get("bindings"):
        lines.extend(["## 实体绑定", "", json_block(result["bindings"]), ""])
    lines.extend(["## Cypher 与参数", ""])
    for query in result["queries"]:
        lines.extend([f"### {query['mechanism']}", "", "````cypher", query["cypher"], "````",
                      "", json_block({key: value for key, value in query.items() if key != "cypher"}), ""])
    if "records" in result:
        lines.extend(["## 查询结果与路径证据", "", json_block({
            "complete": result["complete"], "records": result["records"], "paths": result["paths"],
            "nodes": result["evidence"], "edges": result["evidence_edges"],
        }), ""])
    if result.get("clarification"):
        lines.extend(["## 待确认实体", "", json_block(result["clarification"]), ""])
    lines.extend(["## 执行追踪", "", json_block(result["trace"]), ""])
    return "\n".join(lines)
