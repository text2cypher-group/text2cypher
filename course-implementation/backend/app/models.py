from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

Level = Literal["simple", "medium", "complex"]
Label = Literal["service", "class", "method", "api", "exchange", "queue", "config", "test"]
Operation = Literal[
    "list", "count", "owner", "entry", "downstream", "callers", "impact",
    "call_chain", "publishers", "mq", "rpc", "dependencies", "imports",
    "implements", "inherits",
]
CATEGORY = {
    "list": "单节点查询", "count": "属性过滤", "owner": "单节点查询",
    "entry": "入口 API 发现", "downstream": "下游服务发现",
    "call_chain": "下游服务发现", "callers": "反向影响分析", "impact": "反向影响分析",
    "publishers": "MQ 发布者与完整链路", "mq": "MQ 发布者与完整链路",
    "rpc": "REST + MQ 综合依赖", "dependencies": "REST + MQ 综合依赖",
    "imports": "属性过滤", "implements": "属性过滤", "inherits": "属性过滤",
}
LabelSet = {
    "owner": {"method", "class", "api", "config", "test"},
    "entry": {"service", "method", "class"},
    "downstream": {"method"}, "callers": {"service"}, "impact": {"method"},
    "call_chain": {"method"}, "publishers": {"queue", "exchange"},
    "mq": {"service", "queue", "method", "exchange"},
    "rpc": {"service"}, "dependencies": {"service"}, "imports": {"class"},
    "implements": {"class"}, "inherits": {"class"},
}


class ContractModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Scope(ContractModel):
    service: str = Field(default="", max_length=180)
    repository: str = Field(default="", max_length=180)
    branch: str = Field(default="", max_length=180)


class Plan(ContractModel):
    operation: Operation
    entity: Label
    name: str = Field(default="", max_length=180)
    service: str = Field(default="", max_length=180)
    target_service: str = Field(default="", max_length=180)
    contains: str = Field(default="", max_length=180)
    depth: int = Field(default=6, ge=1, le=10)
    direction: Literal["forward", "reverse", "both"] = "forward"

    @model_validator(mode="after")
    def check_combination(self):
        if self.operation in LabelSet and (not self.name or self.entity not in LabelSet[self.operation]):
            raise ValueError("查询意图需要相应类型及名称的实体")
        if self.target_service and (self.entity != "service" or self.operation not in {"mq", "rpc", "dependencies"}):
            raise ValueError("目标服务仅用于跨服务依赖查询")
        if self.contains and self.operation not in {"list", "count"}:
            raise ValueError("名称包含过滤仅用于列表或计数")
        if self.direction != "forward" and self.operation != "call_chain":
            raise ValueError("方向选项用于方法调用链")
        return self


class QueryRequest(ContractModel):
    question: str = Field(min_length=1, max_length=1600)
    level: Level = "simple"
    scope: Scope = Field(default_factory=Scope)
    candidate_id: str = Field(default="", max_length=300)
    target_id: str = Field(default="", max_length=300)
    clarify_from: str = Field(default="", max_length=40)


class BatchRequest(ContractModel):
    questions: list[QueryRequest] = Field(min_length=1, max_length=50)


class Example(ContractModel):
    question: str = Field(min_length=1, max_length=400)
    level: Level
    plan: Plan
    review: Literal["draft", "approved"] = "draft"
