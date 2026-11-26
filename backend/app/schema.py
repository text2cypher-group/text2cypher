import hashlib
import json
from pathlib import Path
from typing import NamedTuple

DATA = Path(__file__).resolve().parents[1] / "data"


def load_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def digest(value):
    data = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode()).hexdigest()[:16]


def q(identifier):
    if not isinstance(identifier, str) or not identifier.strip() or any(
        ch in identifier for ch in ('`', '\\', '\n', '\r', '\0')
    ):
        raise ValueError("Schema 标识符包含不支持的字符")
    return "`" + identifier + "`"


class Kind(NamedTuple):
    label: str
    name: str


class Contract:
    def __init__(self, path):
        self.raw = load_json(path)
        self.kinds = {key: Kind(**item) for key, item in self.raw["entities"].items()}
        self.fields = self.raw["fields"]
        self.relations = self.raw["relations"]
        self.phases = self.raw["mq_phases"]
        self.by_label = {kind.label: key for key, kind in self.kinds.items()}
        if set(self.fields) != {"id", "repository", "branch", "path", "phase"}:
            raise ValueError("Schema 映射缺少公共字段")
        if len(self.by_label) != len(self.kinds) or any(not value for value in self.phases.values()):
            raise ValueError("Schema 映射包含重复标签或空阶段")
        for identifier in [
            *self.fields.values(), *self.relations.values(),
            *(name for kind in self.kinds.values() for name in kind),
        ]:
            q(identifier)

    def snapshot(self):
        common = [self.fields[key] for key in ("id", "repository", "branch", "path")]
        return {
            "nodes": {k.label: sorted(set(common + [k.name])) for k in self.kinds.values()},
            "relations": {
                v: [self.fields["phase"]] if key == "message" else []
                for key, v in self.relations.items()
            },
        }


class SchemaError(ValueError):
    pass


class ActiveSchema:
    def __init__(self, contract, snapshot):
        self.contract = contract
        self.snapshot = snapshot
        self.hash = digest({"actual": snapshot, "mapping": contract.raw})

    def label(self, entity):
        kind = self.contract.kinds[entity]
        if kind.label not in self.snapshot["nodes"]:
            raise SchemaError(f"图谱缺少节点标签 {kind.label}")
        return q(kind.label)

    def property(self, entity, field):
        kind = self.contract.kinds[entity]
        prop = kind.name if field == "name" else self.contract.fields[field]
        if prop not in self.snapshot["nodes"].get(kind.label, []):
            raise SchemaError(f"图谱缺少属性 {kind.label}.{prop}")
        return q(prop)

    def common(self, field):
        prop = self.contract.fields[field]
        if not any(prop in fields for fields in self.snapshot["nodes"].values()):
            raise SchemaError(f"图谱缺少公共字段 {prop}")
        return q(prop)

    def relationship(self, relation, phase):
        name = self.contract.relations[relation]
        active = self.snapshot["relations"]
        if name not in active:
            raise SchemaError(f"图谱缺少关系 {name}")
        if phase and self.contract.fields["phase"] not in active[name]:
            raise SchemaError(f"图谱缺少消息阶段字段 {self.contract.fields['phase']}")
        return q(name)
