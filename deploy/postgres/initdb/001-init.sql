-- 首次初始化时执行（数据卷为空时一次）。
-- 主库 hdb 由 POSTGRES_DB 创建；这里补扩展和预演库。

-- 记忆投影的语义索引需要 pgvector。
CREATE EXTENSION IF NOT EXISTS vector;

-- 预演/演练用库（cutover_rehearsal.py 的 --dsn 目标）。
CREATE DATABASE hdb_rehearsal;

\connect hdb_rehearsal
CREATE EXTENSION IF NOT EXISTS vector;
