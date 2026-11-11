-- 仅在目标业务库、已审批且核对索引不存在时执行；不含业务数据修改。
-- 元数据锁等待超过5秒即失败，不长期阻塞业务。恢复：DROP INDEX同名索引（先撤回离线工具）。
SET SESSION lock_wait_timeout = 5;
ALTER TABLE iot_device_offline_monitor
    ADD INDEX aiops_offline_device_time (device_id, offline_time),
    ALGORITHM=INPLACE, LOCK=NONE;
