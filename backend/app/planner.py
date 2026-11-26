import json
import os
import re
from pathlib import Path

import httpx
from pydantic import ValidationError

from .models import LabelSet, Plan
from .schema import DATA

SKILL_VERSION = "1.0.0"
SKILL = (Path(__file__).resolve().parents[1] / "skills" / "text2cypher" / "SKILL.md").read_text(encoding="utf-8")
PROVIDERS = {
    "deepseek": ("https://api.deepseek.com/chat/completions", "deepseek-flash", "deepseek-"),
    "glm": ("https://open.bigmodel.cn/api/paas/v4/chat/completions", "glm-4.7-flash", "glm-"),
    "kimi": ("https://api.moonshot.cn/v1/chat/completions", "kimi-k2.5", "kimi-"),
}


class IntentError(ValueError):
    pass


def reject_write_intent(question):
    # This is a UX check. The plan contract and compiler are the execution boundary.
    without_impact = re.sub(r"(?:如果)?(?:修改|删除|移除).{0,120}?(?=影响)", "", question)
    if re.search(
        r"\b(?:DETACH\s+DELETE|DELETE\s+\w+|CREATE\s*\(|MERGE\s*\(|DROP\s+|CALL\s+\w+\.)"
        r"|(?:清空|删除所有|删库|写入数据库|新建节点|更新数据库|删除节点|上传数据)",
        without_impact, re.IGNORECASE,
    ):
        raise IntentError("仅支持代码图谱的只读查询")


def demo_plan(question):
    services = re.findall(r"\bts-[a-z0-9-]+-service\b", question, re.IGNORECASE)
    terms = [item for item in re.findall(r"[A-Za-z_][\w.-]*", question, flags=re.ASCII)
             if item.lower() not in {"controller", "api", "rest", "rpc", "mq", "id", "class"} and item not in services]
    service = services[0] if services else ""
    second = services[1] if len(services) > 1 else ""
    term = terms[0] if terms else ""
    mq = bool(re.search(r"mq|消息|队列|发布|消费|交换机|投递", question, re.IGNORECASE))
    if service and re.search(r"跨服务|综合依赖|同步.*异步|rpc\s*\+\s*mq", question, re.IGNORECASE):
        return Plan(operation="dependencies", entity="service", name=service, target_service=second)
    if mq and service and not term:
        return Plan(operation="mq", entity="service", name=service, target_service=second)
    if mq and term:
        entity = "exchange" if "交换机" in question or term.endswith(".events") else (
            "queue" if "队列" in question or "." in term else "method")
        op = "publishers" if entity in {"queue", "exchange"} and re.search(r"谁发布|谁发送|谁生产|发布者|生产者", question) else "mq"
        return Plan(operation=op, entity=entity, name=term, service=service)
    if term and "调用链" in question and re.search(r"上下游|上游|反向", question):
        direction = "both" if "上下游" in question else "reverse"
        return Plan(operation="call_chain", entity="method", name=term, service=service, direction=direction)
    if term and "影响" in question:
        return Plan(operation="impact", entity="method", name=term, service=service)
    if service and re.search(r"哪些.*调用了|谁调用|反向依赖|上游服务", question):
        return Plan(operation="callers", entity="service", name=service)
    if term and re.search(r"下游服务|调用了哪些服务|调用哪些服务", question):
        return Plan(operation="downstream", entity="method", name=term, service=service)
    if service and re.search(r"RPC|REST|远程调用|同步依赖", question, re.IGNORECASE):
        return Plan(operation="rpc", entity="service", name=service, target_service=second)
    if re.search(r"API|接口|端点", question, re.IGNORECASE) and re.search(r"入口|暴露", question):
        if term:
            entity = "class" if term[0].isupper() else "method"
            return Plan(operation="entry", entity=entity, name=term, service=service)
        if service:
            return Plan(operation="entry", entity="service", name=service)
    if term and re.search(r"调用链|调用了哪些方法|调用路径", question):
        return Plan(operation="call_chain", entity="method", name=term, service=service)
    if term and re.search(r"属于|归属|所属", question):
        entity = "test" if "测试" in question else "config" if "配置" in question else (
            "class" if term[0].isupper() else "method")
        return Plan(operation="owner", entity=entity, name=term, service=service)
    for keyword, op in (("导入", "imports"), ("继承", "inherits"), ("实现", "implements")):
        if term and keyword in question:
            return Plan(operation=op, entity="class", name=term, service=service)
    if re.search(r"列出|查找|查询|多少|数量|有哪些|有几个|显示|统计", question):
        labels = [
            (r"配置", "config"), (r"测试", "test"), (r"交换机", "exchange"), (r"队列", "queue"),
            (r"API|端点|接口", "api"), (r"方法|函数", "method"), (r"类|Controller", "class"),
            (r"微服务|服务", "service"),
        ]
        for expression, entity in labels:
            if re.search(expression, question, re.IGNORECASE):
                op = "count" if re.search(r"多少|数量|几个|统计", question) else "list"
                contains = "Controller" if "controller" in question.lower() else (
                    term if re.search(r"含|包含|带有", question) else "")
                return Plan(operation=op, entity=entity, contains=contains, service=service)
    raise IntentError("演示解析器未识别该问题。请使用示例句式，或连接课程允许的模型")


class ModelPlanner:
    def __init__(self, provider, model, key, semantic_rerank=False):
        uri, default, prefix = PROVIDERS[provider]
        self.model = model or default
        if not self.model.casefold().startswith(prefix) or not key:
            raise ValueError("模型型号或 API Key 与配置的课程提供方不匹配")
        self.provider = provider
        self.version = f"{provider}/{self.model}"
        self.url = uri
        self.client = httpx.AsyncClient(timeout=httpx.Timeout(16, connect=5), trust_env=False,
                                        headers={"Authorization": f"Bearer {key}"})
        self.semantic_rerank = semantic_rerank

    async def close(self):
        await self.client.aclose()

    async def request(self, system, data, max_tokens):
        payload = {
            "model": self.model, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": json.dumps(data, ensure_ascii=False)}],
            "max_tokens": max_tokens, "thinking": {"type": "disabled"},
        }
        if self.provider != "kimi":
            payload["temperature"] = 0
        try:
            response = await self.client.post(self.url, json=payload)
            response.raise_for_status()
            body = response.json()
            if body["choices"][0]["finish_reason"] != "stop":
                raise ValueError("模型输出未完整结束")
            parsed = json.loads(body["choices"][0]["message"]["content"])
            if not isinstance(parsed, dict):
                raise ValueError("需要 JSON 对象")
            return parsed
        except (httpx.HTTPError, ValueError, TypeError, KeyError, IndexError) as exc:
            raise IntentError("模型请求异常或未返回完整 JSON") from exc

    async def plan(self, question, schema, examples, scope, feedback=""):
        if self.semantic_rerank and len(examples) > 3 and not feedback:
            try:
                response = await self.request(
                    "仅按自然语言含义重排候选示例。返回 JSON 对象 ids，为最多三个候选 ID 的数组。",
                    {"question": question, "examples": [
                        {"id": row["id"], "question": row["question"]} for row in examples]}, 180)
                order = response.get("ids", [])
                if isinstance(order, list):
                    rank = {item: pos for pos, item in enumerate(order) if isinstance(item, str)}
                    examples = sorted(examples, key=lambda row: rank.get(row["id"], len(order)))
            except IntentError:
                pass
        result = await self.request(
            SKILL,
            {"question": question, "scope": scope.model_dump(), "schema": schema.snapshot,
             "mapping": schema.contract.raw, "plan_json_schema": Plan.model_json_schema(),
             "examples": [{key: item[key] for key in ("question", "plan", "cypher") if key in item}
                          for item in examples[:3]], "validation_feedback": feedback,
             "intent_entity_constraints": {key: sorted(value) for key, value in LabelSet.items()}}, 600,
        )
        try:
            return Plan.model_validate(result)
        except ValidationError as exc:
            raise IntentError("模型输出未通过查询意图契约") from exc


class DemoPlanner:
    version = "demo-rules/1.0.0"

    async def close(self):
        pass

    async def plan(self, question, schema, examples, scope, feedback=""):
        for example in examples:
            if example["question"] == question:
                return Plan.model_validate(example["plan"])
        return demo_plan(question)


def make_planner():
    provider = os.getenv("LLM_PROVIDER", "demo").strip().casefold()
    if provider == "demo":
        return DemoPlanner()
    if provider not in PROVIDERS:
        raise ValueError("LLM_PROVIDER 仅接受 demo、deepseek、glm、kimi")
    return ModelPlanner(provider, os.getenv("LLM_MODEL"), os.getenv("LLM_API_KEY"),
                        os.getenv("LLM_SEMANTIC_RERANK", "0") == "1")
