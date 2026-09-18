"""Fixed ontology schema: a site-style layered ontology.

The graph uses a fixed, closed set of *metatypes* (the ontology class, stored in
kg_domain_entity.type) and, for Asset nodes, a fixed set of *archetypes* (stored
in kg_domain_entity.entity_spec). The structural layer kg_graph_node.type is
always "ENTITY". Nothing is DB-driven or dynamic: business identity lives in the
open-vocabulary node `name`, while the metatype/archetype layers are closed and
validated in extract.validate().
"""
from __future__ import annotations

# Metatypes (ontology class, stored in kg_domain_entity.type). Closed set.
ENTITY_TYPES = ("Asset", "Relation", "Event", "Behavior")

# Archetypes (kg_domain_entity.entity_spec). Only meaningful when type == "Asset".
ENTITY_SPECS = ("Mission", "Solution", "Implementation", "ServiceInstance", "Artifact")


def schema_prompt_block() -> str:
    """Static ontology definition injected into the extraction prompt."""
    return (
        "## 元类型（entity_type 只能取以下之一）\n"
        "- Asset：一个资产实体。必须再指定一个 entity_spec 原型（见下）。\n"
        "- Relation：作为节点独立存在的关系/关联实体（不是边）。\n"
        "- Event：一次发生的事件，含触发条件/前置/后续时才抽。\n"
        "- Behavior：一条 when/if/do 三元组行为规则时才抽。\n"
        "\n"
        "## Asset 的原型（entity_spec，仅 entity_type=Asset 时填，5 选 1）\n"
        "- Mission：为什么做——目标/意图/动机。\n"
        "- Solution：怎么做——方案/策略/设计思路。\n"
        "- Implementation：静态产物——代码/文档/配置等落地物。\n"
        "- ServiceInstance：运行中的实例——已部署的服务/端点。\n"
        "- Artifact：兜底——套不进上面 4 种的普通成品。\n"
        "非 Asset 的节点不要输出 entity_spec（留空或省略）。"
    )
