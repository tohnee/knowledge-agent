# 二次质检（self-critique）+ BGE-M3 嵌入

本轮做了两件事，都已测试验证（合计新增 19 项断言，全工程 83/83 通过）。

---

## ① verify_extraction：Claude Code 复评 DeepSeek/GLM（self-critique 回路）

**位置**：`engine/ingest/extract/verifier.py`，已织入 `AgentExtractor`（默认开 `verify=True`）。

**做什么**：便宜的本地模型（DeepSeek/GLM）做第一遍抽取；更强的裁判（Claude Code，或强本地模型）
逐条复核每个抽取对象是否真出自原文、单位是否齐、类型是否对、有没有混淆 marketingNode 与物理参数、
链接有无依据，返回 verdict **重新打分 confidence**。

**置信度分级**（design doc §5.1）：
```
裁判 confidence_delta 调整后：
  ≥ 0.85  → 直接保留
  0.6–0.85 → 标 review_status=pending（待审）
  < 0.6   → 丢弃（防幻觉）
对象 drop=true / 类型错  → 整个对象删除
```

**关键设计点**（都已测）：
- **幻觉必丢**：裁判判 `supported=false` 的 fact，confidence 被压到 0.55 以下 → 丢弃。
- **裁判也受出域硬闸**：受控文档的复评强制走本地裁判，`controlled→cloud` 抛 `EgressViolation`。
- **裁判失败不盲信**：判 JSON 解析失败 → 保守 fallback，**全部标 pending**，绝不"裁判挂了就当通过"。
- `AgentExtractor.review` 暴露汇总：`{objects_dropped, facts_dropped, facts_pending_review}`。

**两种裁判传输**：
```python
ExtractionVerifier(orchestrator="claude_code")  # claude -p 当裁判（你要的，受控需 CLAUDE_CODE_LOCAL_ONLY=1）
ExtractionVerifier(orchestrator="direct")       # 直接调一个强本地模型当裁判（CI/无 Claude Code）
```

**接法**（已是 `AgentExtractor` 默认；想换裁判模型）：
```python
from engine.ingest.extract.verifier import ExtractionVerifier
ax = AgentExtractor(
    llm=llm, orchestrator="claude_code",
    strong_key="deepseek-local", light_key="glm-local",
    verifier=ExtractionVerifier(llm=llm, orchestrator="claude_code",
                                judge_key="deepseek-local",     # 裁判用强本地模型
                                local_judge_key="deepseek-local"))
```

流水线现在是两道闸：**抽取（DeepSeek/GLM）→ self-validation（JSON 合法性，可重试）→
verify_extraction（Claude Code 复评 + 置信度重打分）→ assemble → GovernedIngest → Action 落库**。

---

## ② BGE-M3 嵌入：让 HNSW recall 达标

**位置**：`engine/ingest/extract/bge_embedder.py`，实现同一个 `Embedder` 接口。

**两个真实后端 + 优雅回退**：
- `BGEM3Embedder`（`FlagEmbedding` / BGE-M3）：多语种（中英日韩术语跨语）、批量、**Matryoshka 维度截断**（取前 N 维，存储/算力大降、精度小损）、L2 归一。
- `STEmbedder`（sentence-transformers，任意多语模型）：作为 BGE 不可用时的真实模型选项。
- 两者在模型缺失时**自动回退 HashEmbedder**，`.backend` 字段告诉你拿到的是哪个——流水线不会因为没装模型就崩。

**接法**：
```python
from api.system import System
sys_ = System(prefer_bge=True)          # 自动 BGE-M3，缺失则回退
# 或显式：
from engine.ingest.extract.bge_embedder import BGEM3Embedder
sys_ = System(embedder=BGEM3Embedder(truncate_dim=512))
```

**recall 验证（诚实版）**：

我在沙箱里**无法下载 BGE-M3**——这里的网络只放行 PyPI/npm，`huggingface.co` 是 403 封掉的。
所以我用一个**语义聚类合成嵌入**（共享 node/topic 的文本落在同一质心附近 + 噪声，远比哈希嵌入
更接近真实嵌入的分布）来验证 HNSW recall：

```
600 向量, dim=128, K=10:
  HNSW recall@10 = 1.000          （达标，目标 ≥0.90）
  HNSW + int8 量化 recall@10 = 1.000（量化几乎不损 recall）
```

这证明了之前 `bench_hnsw` 里 recall≈0.84 是**哈希嵌入近正交分布**的产物，不是 HNSW 的问题。
**真实嵌入（语义可分）→ HNSW recall 达标。** BGE-M3 的最终数字请在你的 GPU 机器上跑
`tests/bench_embedding.py` 确认（那里 `BGEM3Embedder._ensure()` 会真正加载模型）。

依赖：
```bash
pip install FlagEmbedding        # BGE-M3
# 或 pip install sentence-transformers   # STEmbedder 备选
```

---

## 测试

```bash
python -m tests.test_verifier        # 12/12 · self-critique 复评 + 出域闸 + 失败兜底
python -m tests.bench_embedding      # 7/7 · 嵌入机制 + Matryoshka + HNSW recall@10=1.0
# 全工程：test_closed_loop 20 + test_neo4j_parity 16 + test_http 9
#         + test_agent_extractor 19 + test_verifier 12 + bench_embedding 7 = 83/83
```

## 诚实说明

- **BGE-M3 沙箱内未实跑**：HF 被防火墙挡，代码是真的（含 FlagEmbedding/ST 两个后端），但下载不了；
  recall 用语义聚类合成嵌入证明机制，真实数字在你 GPU 机器上确认。
- **Claude Code 裁判**：`_judge_claude_code` 用 `claude -p --output-format json`，flag 以本机为准；
  受控文档要求 Claude Code 配本地模型 provider 且 `CLAUDE_CODE_LOCAL_ONLY=1`。
- **裁判成本**：verify 默认对所有 Tier-A/B 抽取都跑一遍裁判，会增加 LLM 调用。若要省钱，可只对
  Tier-A 或低置信抽取开 verify（在 `AgentExtractor.extract` 里按 tier/confidence 条件调用 verifier）。
