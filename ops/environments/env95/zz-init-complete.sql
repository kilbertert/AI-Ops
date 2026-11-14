-- 按官方entrypoint文件顺序在完整Snail schema之后执行；中途失败不会写完成标记。
CREATE TABLE snail_ai.aiops_schema_ready (version INT PRIMARY KEY);
INSERT INTO snail_ai.aiops_schema_ready (version) VALUES (1);
