-- 首次启动时执行：知识库独立数据库 + pgvector 扩展
CREATE DATABASE kb;
\c kb
CREATE EXTENSION IF NOT EXISTS vector;
