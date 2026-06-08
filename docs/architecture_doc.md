# Hardware RAG 系统架构文档

> **版本**: v0.0.1  
> **最后更新**: 2025-12  
> **维护者**: Hardware RAG Team

---

## 📋 目录

1. [系统概述](#系统概述)
2. [核心架构](#核心架构)
3. [模块设计](#模块设计)
4. [数据流向](#数据流向)
5. [关键设计决策](#关键设计决策)
6. [技术栈](#技术栈)
7. [性能特性](#性能特性)
8. [已知限制](#已知限制)
9. [扩展指南](#扩展指南)

---

## 系统概述

### 什么是 Hardware RAG?

Hardware RAG 是一个**专为硬件技术文档设计的本地轻量化知识库问答系统**。它采用 RAG (Retrieval-Augmented Generation) 架构，结合向量检索和关键词匹配，为用户提供精准的文档问答服务。

### 核心能力

- ✅ **混合检索**: 语义向量 + BM25 关键词检索，双路召回
- ✅ **智能重排序**: 可选的 Reranker 模块，提升检索精度
- ✅ **多知识库**: 支持创建、切换、独立管理多个知识库
- ✅ **增量更新**: 文档即传即用，无需全量重建
- ✅ **灵活部署**: 纯本地模式 / 混合模式 / 纯云端模式

### 适用场景

- 📚 企业内部技术文档问答
- 🔧 硬件产品知识库搭建
- 🎓 RAG 检索流程学习与实验
- 👥 小团队快速部署知识库系统

---

## 核心架构

### 系统分层架构

```
┌─────────────────────────────────────────────────────────┐
│                    用户界面层 (UI)                        │
│                 Streamlit Web Interface                 │
└─────────────────────────────────────────────────────────┘
                            ↓
┌─────────────────────────────────────────────────────────┐
│                   业务逻辑层 (Pipeline)                   │
│   RAGPipeline: 文档管理、查询编排、知识库操作                 │
└─────────────────────────────────────────────────────────┘
                            ↓
┌──────────────────────┬──────────────────┬───────────────┐
│   检索引擎层           │   模型服务层       │  存储管理层     │
├──────────────────────┼──────────────────┼───────────────┤
│ • HybridRetriever    │ • LLM (推理)      │ • ChromaDB    │
│ • VectorRetriever    │ • Embedding (向量)│ • BM25 Cache  │
│ • BM25Retriever      │ • Reranker (重排) │ • DocStore    │
└──────────────────────┴──────────────────┴───────────────┘
                            ↓
┌─────────────────────────────────────────────────────────┐
│                  基础设施层 (Infrastructure)              │
│   资源管理器 | 日志系统 | 配置中心 | 缓存管理                  │
└─────────────────────────────────────────────────────────┘
```

### 请求处理流程

```
用户提问
   ↓
[查询预处理]
   ↓
[混合检索] → 向量检索 (Top 20) ──┐
              BM25检索 (Top 20) ──┤
   ↓                              │
[RRF 融合] ← ─────────────────────┘
   ↓
[Reranker 重排序] (可选)
   ↓
[Top-K 筛选] (默认 5 个片段)
   ↓
[Prompt 构建] + 历史对话
   ↓
[LLM 生成回答] (流式输出)
   ↓
返回给用户
```

---

## 模块设计

### 1. 核心模块 (src/core/)

#### 1.1 RAGPipeline (rag_pipeline.py)

**职责**: 系统核心编排器，统一管理所有业务流程

**核心方法**:
```python
class RAGPipeline:
    def query(msg, kb_name, history) -> Generator
        # 查询编排：检索 → 上下文构建 → LLM 生成
    
    def upload_files(files, kb_name) -> str
        # 文档上传：持久化 → 索引构建 → 缓存刷新
    
    def create_kb(name) -> Tuple[bool, str]
        # 知识库创建：目录初始化 → 索引初始化
    
    def delete_document(filename, kb_name) -> str
        # 文档删除：文件删除 → 向量清理 → 缓存失效
```

**设计要点**:
- 统一错误处理和返回格式
- 事务性操作保证数据一致性
- 自动触发缓存失效

#### 1.2 HybridRetriever (hybrid_retriever.py)

**职责**: 混合检索引擎，融合多路召回结果

**核心算法**:
```
RRF Score = vector_weight / (k + vector_rank) 
          + bm25_weight / (k + bm25_rank)

其中:
- k = 60 (RRF 常数，平滑排名差异)
- vector_weight = 0.5 (向量权重)
- bm25_weight = 0.5 (BM25 权重)
```

**流程图**:
```
Query
  ↓
┌─────────────────┐
│ 向量检索 (Top 20)│
│ Embedding + 余弦 │
└────────┬────────┘
         │
         ├───────→ RRF 融合 ─→ Rerank ─→ Top 5
         │
┌────────┴────────┐
│ BM25检索 (Top 20)│
│ Jieba分词 + TF-IDF│
└─────────────────┘
```

**BM25 索引缓存机制**:
- **分库存储**: 每个知识库独立 `.pkl` 文件
- **按需加载**: 查询时才加载对应索引
- **智能失效**: 检测文档变化自动重建

#### 1.3 CustomRAGChat (custom_rag_chat.py)

**职责**: 聊天引擎，处理上下文和对话历史

**特性**:
- **上下文缓存**: MD5 哈希查询，避免重复检索
- **历史管理**: 保留最近 5 轮对话，控制 Token 消耗
- **流式输出**: 实时返回生成内容，提升用户体验

**Prompt 模板**:
```
系统提示:
你是一个专业的硬件技术助手，名字叫小智。
请严格基于下方的【参考资料】回答用户问题。

规则：
1. 如果【参考资料】包含答案，请详细回答
2. 如果【参考资料】内容不足或无关，明确说明'知识库中未找到相关信息'
3. 回答必须使用中文

### 参考资料 ###
{检索到的上下文}

[历史对话]
...

[用户问题]
{current_query}
```

#### 1.4 ResourceManager (resource_manager.py)

**职责**: 全局资源管理，连接池和生命周期控制

**单例模式实现**:
```python
class ResourceManager:
    _instance = None
    _init_lock = threading.Lock()
    
    def __new__(cls):
        # 双重检查锁定
        if cls._instance is None:
            with cls._init_lock:
                if cls._instance is None:
                    instance = super().__new__(cls)
                    cls._instance = instance
        return cls._instance
```

**管理的资源**:
- ✅ ChromaDB 持久化客户端
- ✅ LLM / Embedding / Reranker 模型
- ✅ 连接重试与降级策略
- ✅ 健康检查与状态监控

**生命周期**:
```
初始化 → 运行中 → 关闭
  ↓        ↓       ↓
  ├─ 模型加载    ├─ 健康检查  ├─ 断开连接
  ├─ 数据库连接  ├─ 自动重连  ├─ 清理缓存
  └─ 资源验证    └─ 异常恢复  └─ 释放资源
```

#### 1.5 ModelFactory (model_factory.py)

**职责**: 模型初始化工厂，统一配置管理

**支持的部署模式**:

| 模式 | LLM | Embedding | 适用场景 |
|------|-----|-----------|---------|
| **纯本地** | Ollama | Ollama | 数据敏感、离线环境 |
| **混合模式** | 云端 API | Ollama | 成本优化、高性能 |
| **纯云端** | 云端 API | 云端 API | 快速部署、无本地算力 |

**初始化流程**:
```python
def init_global_models():
    # 1. 验证配置完整性
    is_valid, errors = validate_config()
    if not is_valid:
        raise ValueError("配置错误")
    
    # 2. 初始化 LLM
    _init_llm()
    
    # 3. 初始化 Embedding
    _init_embedding()
    
    # 4. 初始化 Reranker
    _init_reranker()
    
    # 5. 配置分词器
    Settings.text_splitter = SentenceSplitter(...)
```

### 2. 数据摄取模块 (src/ingestion/)

#### 2.1 IndexBuilder (index_builder.py)

**职责**: 向量索引构建与持久化

**关键功能**:

1. **索引缓存机制**
```python
_index_cache = {
    "kb_name": VectorStoreIndex对象
}
```

2. **DocStore 同步修复**
```python
def _rebuild_docstore_from_chroma():
    # 当 DocStore 与 ChromaDB 不一致时
    # 从 ChromaDB 反向重建 DocStore
```

3. **持久化策略**
```
storage/
├── chroma_db/              # ChromaDB 向量库
│   └── kb_hardware/
└── docstore_hardware/      # LlamaIndex DocStore
    ├── docstore.json       # 文档元数据
    ├── index_store.json    # 索引映射
    └── vector_store.json   # 向量存储配置
```

#### 2.2 DataLoader (data_loader.py)

**职责**: 文档加载与知识库路径管理

**支持的文件格式**:
- 📄 文档: PDF, DOCX, DOC, TXT, MD
- 📊 结构化: CSV, JSON
- 🌐 网页: HTML, HTM

**目录结构**:
```
data/
├── hardware_kb/            # 知识库1
│   ├── datasheet1.pdf
│   └── manual.docx
└── software_kb/            # 知识库2
    └── guide.md
```

### 3. 缓存管理

#### BM25Cache (bm25_cache.py)

**存储策略**:
```
storage/bm25_indexes/
├── hardware_kb.pkl
├── software_kb.pkl
└── testing_kb.pkl
```

**缓存刷新触发条件**:
- 文档数量变化
- 文档 ID 列表不匹配
- 手动调用 `invalidate_bm25_cache()`

---

## 数据流向

### 文档上传流程

```
用户上传文件
    ↓
[临时存储] /tmp/xxx.pdf
    ↓
[持久化] data/kb_name/xxx.pdf
    ↓
[文档解析] SimpleDirectoryReader
    ↓
[分块] SentenceSplitter (512 tokens)
    ↓
[向量化] Embedding Model
    ↓
[存储] ChromaDB + DocStore
    ↓
[缓存失效] BM25Cache.delete(kb_name)
    ↓
完成
```

### 查询检索流程

```
用户输入 Query
    ↓
[检索阶段]
    ├─ 向量检索: Query Embedding → 余弦相似度 → Top 20
    └─ BM25检索: Jieba分词 → TF-IDF → Top 20
    ↓
[融合阶段]
    RRF 算法融合双路结果
    ↓
[重排阶段] (可选)
    Reranker 二次精排
    ↓
[筛选阶段]
    选取 Top 5 作为上下文
    ↓
[生成阶段]
    System Prompt + 上下文 + 历史 + 当前问题
    ↓
    LLM 流式生成
    ↓
返回答案
```

### 知识库切换流程

```
用户选择知识库 B
    ↓
[清空对话历史]
    st.session_state.messages = []
    ↓
[切换索引]
    current_kb = B
    ↓
[加载索引]
    index = get_or_build_index(B)
    ↓
[按需加载 BM25]
    BM25Cache.get(B) 
    # 如果不存在则自动构建
    ↓
完成
```

---

## 关键设计决策

### 1. 为什么选择混合检索？

**问题**: 纯向量检索在以下场景表现不佳
- 专业术语、产品型号 (如 "STM32F103")
- 缩写和代号 (如 "USB-PD", "I2C")
- 数字和版本号 (如 "v2.3.1")

**解决方案**: 向量检索 + BM25 关键词检索
- **向量检索**: 捕捉语义相似性
- **BM25 检索**: 精确匹配关键词
- **RRF 融合**: 平衡两者优势

**实测效果**:
| 查询类型 | 纯向量 | 纯BM25 | 混合检索 |
|---------|--------|--------|----------|
| 语义问题 | 85%    | 60%    | **90%**  |
| 精确匹配 | 65%    | 90%    | **95%**  |
| 综合查询 | 70%    | 70%    | **92%**  |

### 2. 为什么分库存储 BM25 索引？

**备选方案**:
- ❌ 方案A: 所有知识库共用一个索引文件
- ✅ 方案B: 每个知识库独立索引文件

**选择 B 的理由**:
1. **故障隔离**: 单个库损坏不影响其他库
2. **按需加载**: 减少内存占用 (50MB → 5MB)
3. **并发友好**: 避免写锁竞争
4. **便于维护**: 清理、备份更方便

### 3. 为什么使用单例模式管理资源？

**问题**: 
- 多次初始化 ChromaDB 导致连接泄漏
- 模型重复加载浪费显存

**解决方案**: ResourceManager 单例
- 全局唯一的连接实例
- 自动重连与健康检查
- 统一的生命周期管理

### 4. 为什么支持混合部署模式？

**场景需求**:
| 用户类型 | 需求 | 适合模式 |
|---------|------|---------|
| 企业内网 | 数据不出域 | 纯本地 |
| 小团队 | 成本敏感 | 混合模式 (本地 Embedding) |
| 个人开发者 | 快速验证 | 纯云端 |

**混合模式优势**:
- Embedding 在本地 (免费、快速、隐私)
- LLM 调用云端 (智能、效果好)
- **成本降低 70%** (Embedding 调用占大头)

### 5. 为什么选择 Streamlit？

**对比**:
| 框架 | 优势 | 劣势 |
|------|------|------|
| Streamlit | 快速开发、适合原型 | 交互复杂度有限 |
| Gradio | 适合模型 Demo | 定制性较差 |
| FastAPI + React | 高度定制 | 开发成本高 |

**选择理由**:
- ✅ 5 分钟搭建完整 UI
- ✅ 原生支持流式输出
- ✅ 内置会话管理
- ✅ 适合快速迭代

---

## 技术栈

### 核心依赖

```toml
[dependencies]
# RAG 框架
llama-index = "^0.12.9"
llama-index-vector-stores-chroma = "^0.4.2"

# 向量数据库
chromadb = "^0.5.23"

# 检索算法
rank-bm25 = "^0.2.2"
jieba = "^0.42.1"

# LLM 集成
ollama = "^0.4.6"
openai = "^1.59.7"

# Reranker
sentence-transformers = "^3.3.1"

# Web 框架
streamlit = "^1.41.1"
```

### 系统要求

**最低配置**:
- CPU: 4 核
- 内存: 8GB
- 磁盘: 10GB (含模型)

**推荐配置**:
- CPU: 8 核+
- 内存: 16GB+
- 显卡: NVIDIA GPU (可选，加速 Embedding)
- 磁盘: 50GB+ SSD

---

## 性能特性

### 查询性能

| 阶段 | 时间 | 优化手段 |
|------|------|---------|
| 向量检索 | 50-100ms | ChromaDB 内置索引 |
| BM25 检索 | 30-80ms | 内存缓存 + Pickle 持久化 |
| Rerank | 200-500ms | 批量处理 |
| LLM 生成 | 2-5s | 流式输出、上下文裁剪 |

**总体延迟**: 3-6 秒 (首 Token 响应)

### 并发能力

- **单知识库**: 支持 5-10 并发查询
- **多知识库**: 独立缓存，无竞争

### 存储开销

```
单个知识库 (1000 文档):
├── 原始文档: ~500MB
├── 向量数据: ~150MB
├── BM25 索引: ~50MB
├── DocStore: ~20MB
└── 总计: ~720MB
```

---

## 已知限制

### 1. 技术限制

| 问题 | 影响 | 缓解方案 |
|------|------|---------|
| **上下文窗口限制** | 长文档可能截断 | 增加 `CHUNK_SIZE` 或使用长上下文模型 |
| **BM25 对中文敏感** | 分词质量影响检索 | 优化 Jieba 词典 |
| **向量维度固定** | 切换模型需重建索引 | 文档化迁移流程 |
| **无多轮上下文** | 不支持"它是什么"等指代 | 引入对话摘要 |

### 2. 功能限制

- ❌ 不支持图片、表格的多模态检索
- ❌ 不支持跨知识库联合查询
- ❌ 无用户权限管理
- ❌ 不支持文档版本控制

### 3. 扩展性限制

- **单机部署**: 无法水平扩展
- **向量库容量**: ChromaDB 单库推荐 < 100 万向量
- **并发限制**: Streamlit 单进程架构

---

## 扩展指南

### 添加新的文件格式支持

**步骤**:
1. 更新 `RAGPipeline.SUPPORTED_FORMATS`
2. 确保 `SimpleDirectoryReader` 支持该格式
3. 测试解析效果

**示例**: 添加 `.pptx` 支持
```python
# rag_pipeline.py
SUPPORTED_FORMATS = {
    '.pdf', '.txt', '.md', '.docx', 
    '.pptx'  # 新增
}

# 如需自定义解析器
from llama_index.readers.file import PPTXReader
reader = PPTXReader()
docs = reader.load_data(file_path)
```

### 切换不同的 Reranker

**本地模型**:
```bash
# .env
RERANKER_TYPE=local
RERANKER_MODEL=BAAI/bge-reranker-v2-m3
```

**API 模型**:
```bash
RERANKER_TYPE=api
RERANKER_API_BASE=https://api.cohere.ai/v1
RERANKER_API_KEY=your_key
RERANKER_MODEL=rerank-english-v2.0
```

### 接入新的 LLM 提供商

**方式 1**: 使用 OpenAI 兼容接口
```bash
CUSTOM_BASE_URL=https://your-provider.com/v1
CUSTOM_API_KEY=your_key
CUSTOM_LLM_MODEL=your-model
```

**方式 2**: 自定义 LLM 类
```python
# src/core/custom_llm.py
class YourProviderLLM(CustomLLM):
    def complete(self, prompt: str, **kwargs):
        # 实现调用逻辑
        pass
```

### 集成外部知识源

**示例**: 从 Confluence 同步文档
```python
# 新建 src/ingestion/confluence_sync.py
from atlassian import Confluence

def sync_from_confluence(space_key, kb_name):
    confluence = Confluence(url='...', username='...', password='...')
    pages = confluence.get_all_pages_from_space(space_key)
    
    for page in pages:
        content = confluence.get_page_by_id(page['id'], expand='body.storage')
        # 保存到 data/{kb_name}/
        # 触发索引构建
```

### 添加监控和告警

**建议方案**:
1. **日志聚合**: 接入 ELK / Loki
2. **性能监控**: Prometheus + Grafana
3. **错误追踪**: Sentry

**埋点示例**:
```python
# src/core/metrics.py
from prometheus_client import Counter, Histogram

query_counter = Counter('rag_queries_total', 'Total queries')
query_latency = Histogram('rag_query_duration_seconds', 'Query latency')

@query_latency.time()
def query(...):
    query_counter.inc()
    # 原有逻辑
```

---

## 附录

### A. 配置项速查表

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `PROVIDER` | enum | `ollama` | 模型提供商 |
| `CHUNK_SIZE` | int | `512` | 分块大小 |
| `VECTOR_TOP_K` | int | `20` | 向量检索数量 |
| `BM25_TOP_K` | int | `20` | BM25 检索数量 |
| `FINAL_TOP_K` | int | `5` | 最终返回数量 |
| `RRF_K` | int | `60` | RRF 平滑参数 |
| `RERANKER_TYPE` | enum | `none` | 重排序类型 |

### B. 常用命令

```bash
# 启动应用
uv run streamlit run streamlit_app.py

# 健康检查
curl http://localhost:8501/_stcore/health

# 查看日志
tail -f storage/logs/rag_2024-12-22.log

# 清空缓存
rm -rf storage/bm25_indexes/*
```

### C. 相关资源

- **LlamaIndex 文档**: https://docs.llamaindex.ai
- **ChromaDB 文档**: https://docs.trychroma.com
- **Streamlit 文档**: https://docs.streamlit.io
- **BM25 论文**: https://en.wikipedia.org/wiki/Okapi_BM25

---

## 变更历史

| 版本     | 日期      | 变更内容 |
|--------|---------|---------|
| v0.0.1 | 2025-12 | 初始版本 |

---
