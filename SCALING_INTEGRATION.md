# Scaling 方案在融合工程里的落点 + 你的 Claude Code + DeepSeek/GLM 接法

回答你的问题：**scaling 三大支柱都在 `engine/` 里**，本次把两个被简化的点补强了，并按你的栈
（Claude Code 编排 + 本地 DeepSeek/GLM）接通了抽取引擎。

## 三大支柱在融合工程里的确切位置

| 支柱 | 文件 | 状态（本次后） |
|---|---|---|
| **一·分级 ingest 编译** | `engine/ingest/extract/classifier.py`（A/B/C + TIER_BUDGET） | ✅ 完整，且现在真正驱动模型路由 |
| · 多级产物 | `engine/ingest/extract/extractor.py`（chunk+wiki+entity+relation） | ✅ |
| · 批量嵌入 + 增量实体消解 | `engine/ingest/extract/embedding.py` | ✅ |
| · **真实抽取引擎** | `engine/ingest/extract/agent_extractor.py` ★新增 | ✅ Claude Code + DeepSeek/GLM |
| **二·三段漏斗检索** | `engine/retrieval/`（router→recall+RRF→rerank→压缩门） | ✅ 完整 |
| **三·HNSW + 量化 + 分片** | `engine/infra/hnsw.py`、`vector_store.py` | ✅ **HNSW 现已默认接进闭环** |

### 本次补强的两点（之前被简化）

1. **HNSW 接进闭环**：`System(use_hnsw=True)` 现在是**默认**。之前融合工程默认走暴力 O(N)，
   上万文档会慢；现在新建的每个 shard 都用 HNSW O(log N)。已验证 64 文档检索仍 grounded、所有 shard use_hnsw=True。

2. **抽取从桩 → 真引擎**：`StubExtractor` 仍是零依赖默认（测试用），新增 `AgentExtractor`
   接你的 Claude Code + DeepSeek/GLM。`System(extractor=AgentExtractor(...))` 一行切换。

## 你的栈怎么接（已按你的三个选择落地）

你的选择 → 设计：
- **本地 vLLM/Ollama + OpenAI 兼容 API** → `engine/ingest/extract/llm_client.py` 一个
  `OpenAICompatClient`，模型名一换即 DeepSeek↔GLM。
- **Claude Code 全程编排 + 自校验，模型可换** → `AgentExtractor(orchestrator="claude_code")`：
  Claude Code 读章节、调模型、校验 JSON、不合格自我重试；模型在 Claude Code 设置里指向本地端点。
- **受控必须本地，绝不出域** → `llm_client.py` 的 **EgressViolation 硬闸**：受控内容路由到
  非本地端点直接抛异常，不可绕过。已测试：受控→cloud 必抛、受控强制走 local。

### 模型分工（落在 classifier.TIER_BUDGET + AgentExtractor）

```
Tier-A 核心(财报/标准/受控)  → Claude Code 编排 + strong 后端(你最强的本地, 默认 deepseek-local) 满血抽取+建链接
Tier-B 一般(海量)            → Claude Code 编排 + light 后端(glm-local) 轻抽取  ← scaling 省钱主力
Tier-C 低价值/重复           → 不调 LLM, 仅向量化(懒编译)
受控文档(任何 tier)          → 强制 local_key(deepseek-local), EgressViolation 兜底
```

### 接通三步

```python
from engine.ingest.extract.agent_extractor import AgentExtractor
from engine.ingest.extract.llm_client import OpenAICompatClient, LLMEndpoint
from api.system import System

# 1) 配置本地 DeepSeek/GLM（vLLM/Ollama OpenAI 兼容端点）
llm = OpenAICompatClient({
  "deepseek-local": LLMEndpoint("deepseek-local","http://vllm:8000/v1","deepseek-v3",local=True),
  "glm-local":      LLMEndpoint("glm-local","http://ollama:11434/v1","glm-4",local=True),
})

# 2) Claude Code 全程编排（模型可换；受控时要求 CLAUDE_CODE_LOCAL_ONLY=1）
ax = AgentExtractor(llm=llm, orchestrator="claude_code",
                    strong_key="deepseek-local", light_key="glm-local",
                    local_key="deepseek-local")

# 3) 一行切换，其余全不动（GovernedIngest/Action/检索/安全/Neo4j 都不变）
sys_ = System(extractor=ax, use_hnsw=True, store_backend="neo4j", neo4j_driver=driver)
```

### 环境变量

```bash
# 本地模型端点（vLLM/Ollama）
DEEPSEEK_LOCAL_URL=http://vllm:8000/v1
DEEPSEEK_MODEL=deepseek-v3
GLM_LOCAL_URL=http://ollama:11434/v1
GLM_MODEL=glm-4
LOCAL_LLM_HOSTS=vllm,ollama,wka-vllm     # egress 白名单（受控只允许这些 host）
CLAUDE_CODE_LOCAL_ONLY=1                  # Claude Code 编排受控文档时必须置 1
```

## 关于 orchestrator 的两种模式

- `orchestrator="claude_code"`（你要的）：`claude -p --output-format json` 驱动模型、自校验、返回 JSON。
  **Claude Code 的模型 provider 指向你的本地 DeepSeek/GLM**（在 Claude Code 设置里配 OpenAI 兼容 endpoint）。
  受控文档要求 `CLAUDE_CODE_LOCAL_ONLY=1`，否则 `AgentExtractor._via_claude_code` 抛 EgressViolation。
  具体 flag 以本机 `claude --help` 为准。
- `orchestrator="direct"`（CI/无 Claude Code 时）：本类直接 读章节→调 DeepSeek/GLM→校验→重试，同契约。

## 测试（全部无需真实 LLM/Neo4j，假驱动验证契约）

```bash
python -m tests.test_agent_extractor   # 19/19：tier 路由 + 4元组形状 + 自校验重试 + ★出域硬闸
python -m tests.test_closed_loop       # 20/20：六道接缝（HNSW 默认开）
python -m tests.test_neo4j_parity      # 16/16：Neo4j 后端行为一致
python -m tests.test_http              # 9/9：HTTP 链路
# 合计 64/64
```

## 诚实说明

- **HashEmbedder 仍是占位**：HNSW 已接，但嵌入还是确定性哈希（零依赖可测）。上生产把
  `System(embedder=...)` 换成 BGE-M3，HNSW recall 会显著好于测试里的哈希分布。
- **Claude Code flag**：`_via_claude_code` 用的 `claude -p --output-format json` 是稳定模式，
  具体参数以你本机为准；也可换 Claude Agent SDK 以便流式回传进度。
- **Tier-A 用 strong 后端**：我把 `strong_key` 默认设成 `deepseek-local`（你最强的本地模型）。
  若你想 Tier-A 用 Claude 本体抽取、Tier-B 用 DeepSeek，把 `strong_key` 指向一个 Claude 端点即可
  （但注意受控文档仍会被 `local_key` 覆盖，不会出域）。
```
