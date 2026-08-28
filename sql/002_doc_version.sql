-- ame-kb V2 migration: content-hash snapshot table for incremental ingest.
-- Adapted from oceanai_site DocSnapshotServiceImpl.saveIfChanged:
--   compute sha256(content); if unchanged since last snapshot, skip extraction.

CREATE TABLE IF NOT EXISTS `kg_doc_version`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `source_id`     VARCHAR(191)    NOT NULL DEFAULT 'default' COMMENT '来源命名空间',
    `doc_id`        VARCHAR(512)    NOT NULL COMMENT '文档业务键（相对路径）',
    `doc_key`       CHAR(64)        NULL COMMENT 'SHA-256(source_id\0doc_id)',
    `content_hash`  CHAR(64)        NOT NULL COMMENT '正文 SHA-256（hex）',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_doc_key` (`graph_no`, `graph_version`, `doc_key`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 文档内容快照（按 hash 做增量跳过）';
