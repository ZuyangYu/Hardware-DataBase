-- 首次初始化（数据卷为空时执行一次）。
-- 控制面 + 记忆投影的语义索引需要 pgvector；普通 alpine 镜像没有这个扩展。
CREATE EXTENSION IF NOT EXISTS vector;
