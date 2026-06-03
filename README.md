# Workspace Knowledge Agent — 融合工程（前端 + wka 业务 + wka-scale 引擎）

源码级完整闭环。三层焊死成一个系统，**三道接缝（适配层）** 是本工程的核心交付。
零第三方依赖即可跑核心闭环；装了 FastAPI 则多一条真实 HTTP 链路。

## 一句话说清融合关系

```
前端(展示)  ──fetch /api/v1/*──▶  wka 业务主干(本体/Action/安全/双时间轴)
                                        │持有
                                        ▼调用
                                  wka-scale 引擎(分级ingest + 三段漏斗 + HNSW/量化)
```

- **不是替换**：wka-scale 是引擎，嵌入 wka，升级它的 ingest 与检索。
- **wka 独有的全保留**：Action 引擎（唯一写入通道）、OPA/Vault 动态安全、双时间轴、本体。
- **接缝才是关键**：模型映射、检索过 OPA、ingest 走 Action —— 这三道适配是真正的闭环。

## 运行

```bash
cd wka-fused
python -m tests.test_closed_loop     # 20/20 · 无依赖 · 验证六道接缝（内存后端）
python -m tests.test_neo4j_parity    # 16/16 · 无依赖 · Neo4j 后端与内存后端行为一致（含双时间轴）
python -m tests.test_http            # 9/9 · 需 fastapi+httpx · 真实 HTTP 链路（缺依赖自动跳过）

# 起真实服务（可选）
pip install fastapi uvicorn httpx
uvicorn api.main:app --port 8000
# 浏览器开 frontend/index.html，控制台执行 WKA.setRole('analyst') 后调 WKA.ask('N3 状态')

# 切 Neo4j 生产后端：见 NEO4J_INTEGRATION.md
#   System(store_backend="neo4j", neo4j_driver=GraphDatabase.driver(...))
```

## 目录结构

```
wka-fused/
├── frontend/
│   ├── index.html              展示层（六视图原型）
│   └── wka-client.js           ★ 前端 API 客户端：把 mock 数据层换成 fetch /api/v1/*
│
├── api/                        ── wka 业务主干 + 网关 ──
│   ├── main.py                 FastAPI 网关：六视图路由 → System（缺 FastAPI 自动降级为可 import）
│   ├── system.py               ★ 组合根：构建唯一共享对象图（ingest 与 retrieval 共用引擎 stores）
│   ├── services_ask.py         ★ 接缝2：GroundedQA = 引擎 Retriever + 权威 OPA/Vault 安全过滤
│   └── security/rbac.py        Dynamic Security：RBAC + OPA field_visibility + Vault decrypt
│
├── adapters/                   ── 三道接缝（融合的核心）──
│   ├── model_map.py            ★ 接缝1：引擎 dataclass ⇄ wka 存储 schema（唯一翻译点）
│   └── governed_ingest.py      ★ 接缝3：ingest 产出经 Action 引擎落库（审计写入）
│
├── action_engine/              ── wka 业务：唯一写入通道 ──
│   ├── engine.py               8 Action · validate→sandbox→单事务双时间轴写→writeback
│   ├── store_base.py         ★ KnowledgeStoreBase 抽象契约（Action 引擎只认它）
│   ├── store.py                InMemoryKnowledgeStore（测试/无依赖；KnowledgeStore 别名）
│   └── store_neo4j.py        ★ Neo4jKnowledgeStore 生产实现（双时间轴关系建模）
│
├── engine/                     ── wka-scale 引擎（原 pillar1/2/3，已重命名挂载）──
│   ├── ingest/                 §支柱一 分级抽取 + 多级产物 + 批量 + 增量实体消解
│   ├── retrieval/              §支柱二 路由 → 混合召回+RRF → Cross-Encoder 精排 → 压缩+置信度门
│   └── infra/                  §支柱三 HNSW(O(logN)) + int8 量化 + 分片 + BM25 + 图/社区
│
├── common/models.py            引擎共享 dataclass（Chunk/WikiPage/Entity/...）
└── tests/
    ├── test_closed_loop.py     20 项接缝断言（无依赖）
    └── test_http.py            9 项 HTTP 断言（TestClient）
```

## 六道接缝（test_closed_loop 逐项验证）

| 接缝 | 实现位置 | 验证内容 |
|---|---|---|
| **1. ingest 只经 Action 写本体** | `adapters/governed_ingest.py` | 每次本体写入都过 Action 引擎、进审计日志、带角色 |
| **2. 增量合并不重复** | `governed_ingest` + `store.merge_object` | d4 重提 N3/代工厂A/EUV → merged=3 created=0；库里 N3 恰好 1 个 |
| **3. 受控待审不自动** | `governed_ingest.pending_controls` | 受控文档入队 MarkExportControlled，**不自动标记**，需合规 Action |
| **4. 检索读同一份 stores** | `api/system.py` 组合根 | Retriever 与 ingest 共用引擎 vstore/bm25/graph/wiki |
| **5. 权威 OPA/Vault 过滤** | `api/services_ask.py` + `rbac.py` | 同一 EUV 查询：compliance 明文 / analyst 脱敏 / viewer 隐去 |
| **6. Action 强制权限+双时间轴** | `action_engine/engine.py` | analyst 不能 mark(403)；mark 高风险先沙箱；as-of 78K vs 真值 120K |

## 数据如何流过整条链路

```
上传文档
  → GovernedIngest.ingest()                              [接缝3]
      → 引擎 IngestPipeline：分级→多级产物→批量嵌入→增量消解   (填充引擎 stores)
      → 每个实体 entity_to_object_candidate()             [接缝1: model_map]
      → ActionEngine.execute('create_object')             (审计 + 单事务)
      → KnowledgeStore.put_object()                        (本体落库)
      → WikiPage → wiki_to_mongo() → store.put_wiki()      (父跨度)
      → 受控信号 → pending_controls (待审)

提问
  → GroundedQA.answer()                                   [接缝2]
      → Retriever.retrieve()：路由→混合召回+RRF→精排→压缩门  (读同一引擎 stores) [接缝4]
      → contexts 带 controlled 标记                         (已修复：标记贯穿漏斗)
      → apply_field_security() 逐块过 OPA/Vault             [接缝5: 权威安全]
      → 接地生成 + 引用回链 → 前端

写入(任何修改)
  → ActionEngine.execute()                                [接缝6]
      → 权限校验 → 高风险沙箱 → 单事务双时间轴写 → writeback → 审计
```

## 已修复的真实接缝 bug（构建中发现并修正）

1. **受控标记未贯穿漏斗**：`compress_and_gate` 原来没把 `controlled` 带进 context，导致接缝5 安全过滤"空过"（安全隐患）。已修复为标记贯穿。
2. **网关权限双重校验且 split 错误**：`name.split('-')[0]` 把 `revise-capacity` 切成 `revise` 误判 403。已改为**权限唯一由 Action 引擎判定**（单一事实源）。
3. **as-of 双时间轴语义**：统一为 validTime 的"决策时点"语义，匹配前端滑杆直觉。

## 生产替换（接口不变，换实现）

| 参考实现 | 生产 |
|---|---|
| `HashEmbedder` | BGE-M3 / OpenAI（批量 GPU） |
| `StubExtractor` | `ClaudeExtractor`（Claude Code + llm-wiki skill，骨架已在 engine/ingest/extract/extractor.py） |
| `LexicalCrossEncoder` | ms-marco cross-encoder |
| `VectorStore`(内存) | Qdrant(≤10M) / Milvus IVF-PQ(≥100M) |
| `KnowledgeStore`(内存) | Neo4j(Object/Link+双时间轴) + Mongo(Wiki) |
| `_opa_decide`/`_vault_decrypt` | httpx → OPA(:8281) / Vault(:8200) |
| `ActionEngine`(进程内) | wka-action 服务(:8300) behind /api/v1/actions/* |
| `_role()` 读 header | JWT 解码（**前端不可自封角色**） |

接口签名都没变，换实现即可上生产。
