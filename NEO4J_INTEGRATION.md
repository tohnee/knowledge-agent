# Neo4j 接入说明 + 抽取引擎预留口子

本次改动：把 `KnowledgeStore` 抽象成接口，新增 `Neo4jKnowledgeStore` 生产实现，
内存版保留给测试。**系统其余部分一行没改**——只在组合根按配置切后端。

## 改了什么（最小侵入）

```
action_engine/
├── store_base.py      ★ 新增：KnowledgeStoreBase 抽象契约（Action 引擎只认这个）
├── store.py           InMemoryKnowledgeStore（原实现，现继承 base；KnowledgeStore 别名保留）
└── store_neo4j.py     ★ 新增：Neo4jKnowledgeStore 生产实现（双时间轴关系建模）

api/system.py          System(store_backend='memory'|'neo4j', neo4j_driver=...) 可切换
tests/
├── fake_neo4j.py      解释本 store 发出的 Cypher 的内存假驱动（免起 Neo4j 可测）
└── test_neo4j_parity.py  16/16：证明 Neo4j 后端与内存后端行为一致（含双时间轴 append-only）
```

契约方法（两个实现都遵守）：`get_object / put_object / merge_object / set_controlled /
bump_confidence / put_link / put_wiki / get_wiki / append_capacity / capacity_asof /
capacity_truth / append_status / count_objects_by_title`。

## 图模型（store_neo4j.py 落地）

```cypher
(:Object {id, objectType, title, confidence, controlled, eccn, status, aliases, doc_ids})
(:Object)-[:HAS_FACT]->(:Fact {key, value, controlled, sourceTier, confidence, asOf})
(:Object)-[:LINK {lt, docId}]->(:Object)                         // 动态关系类型放属性，防注入
(:Object)-[:HAS_CAPACITY {capacityWSPM, validTime, transactionTime,
            sourceTier, confidence, supersededBy}]->(:Observation) // ★ 双时间轴，append-only
(:Object)-[:STATUS_CHANGE {status, eventDate, transactionTime}]->(:Event)
(:Object)-[:HAS_EVENT]->(:ExportControlEvent {markedAt, eccn, newStatus})  // 合规审计
(:WikiPage {id, docId, title, summary, body, controlled, entities})        // 父跨度
```

**双时间轴铁律（已被 parity 测试验证）**：观测只追加不覆盖；同一 validTime 的旧"最新"
被打 `supersededBy`；as-of = validTime≤year 的最新；truth = supersededBy IS NULL 的最新。
parity 测试专门验证 95K 被 supersede 而非覆盖。

## 生产接线（三步）

```python
# 1) 真实驱动注入
from neo4j import GraphDatabase
from api.system import System
driver = GraphDatabase.driver("bolt://wka-neo4j:7687", auth=("neo4j", PW))
sys_ = System(store_backend="neo4j", neo4j_driver=driver)

# 2) 首次部署建约束/索引（幂等）
sys_.store.init_schema()

# 3) 其余完全不变：sys_.ingest_doc(...) / sys_.ask(...) / sys_.run_action(...)
```

docker-compose 里 Neo4j 已在 `wka` 工程中（`wka-neo4j`，7474/7687）。依赖：

```bash
pip install neo4j
# 注：merge_object 的并集去重在有 APOC 时用 apoc.coll.toSet，无 APOC 自动走纯 Cypher 回退
```

环境变量：`NEO4J_URI`（默认 `bolt://wka-neo4j:7687`）、`NEO4J_PASSWORD`。

## 注意事项

- **facts 是子图不是属性**：Neo4j 节点属性不能存嵌套 dict，所以每条 fact 落成 `:Fact`
  节点（`HAS_FACT` 关系），`get_object` 会拼回 `facts` 列表，shape 与内存版一致。
- **动态 Link 类型**：用 `:LINK {lt:...}` 把关系类型放属性，避免把外部字符串拼进 Cypher
  关系类型（注入风险）。需要按 lt 走原生关系类型时，可在 init 阶段用白名单 + APOC `apoc.merge.relationship`。
- **事务边界**：当前每个方法各开 session。要做"一个 Action = 一个 Neo4j 事务"，把
  `ActionEngine._apply` 包进 `store.driver.session().begin_transaction()`，store 方法
  改为接收 tx——这是上生产时的一个小重构点（已在代码注释标注）。

## 抽取引擎的预留口子（等你的开源 LLM API + coding agent）

`KnowledgeStore` 这块已经稳了。抽取引擎那块**接口已经预留好**，你换实现时不用动别的：

```
engine/ingest/extract/extractor.py
├── class Extractor(ABC)         ← 唯一契约：extract(doc, tier) -> {chunks, wiki_pages, entities, relations}
├── class StubExtractor          ← 当前测试用（确定性，零网络）
└── class ClaudeExtractor        ← 当前是 Claude Code 调用骨架；你可新增：
```

你要接开源 LLM API + coding agent 时，只需新增一个类实现同一个 `extract()`：

```python
class AgentExtractor(Extractor):
    """开源 LLM API + coding agent 作为 wiki 构建引擎。"""
    def __init__(self, llm_api, agent_runner): ...
    def extract(self, doc, tier):
        # 1) 按 tier 决定预算（Tier-C 仍可只切块不抽取）
        # 2) 让 coding agent 用你的开源 LLM 跑整章抽取，产出结构化 JSON
        # 3) 映射成 {chunks, wiki_pages, entities, relations}（与 StubExtractor 同形）
        return {"chunks": [...], "wiki_pages": [...], "entities": [...], "relations": [...]}
```

然后在 `api/system.py` 把 `StubExtractor()` 换成 `AgentExtractor(...)` 即可——
`GovernedIngest`、Action 落库、检索、安全全都不用动。这就是为什么抽取那块"可以重构"
而不影响闭环：**它被 `Extractor` 抽象隔离了**。

建议你的 agent 输出严格遵守这个 schema（沿用 `prompts/semi_enhanced.md` 的约束）：
```json
{"objects":[{"id","type","interfaces":[],"facts":[{"key","value","sourceTier","confidence","asOf","exportControlled"}]}],
 "links":[{"lt","from","to","card"}]}
```
`extractor.py` 里再写一个 `_assemble(doc, json, budget)` 把它转成四元组即可（ClaudeExtractor 已留了 `_assemble` 桩）。
