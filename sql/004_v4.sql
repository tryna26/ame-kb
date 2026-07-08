-- ame-kb V4 migration: doc chunks, provenance URL, dormant workspace scoping.
--   kg_doc_chunk    = sliding-window chunks of a doc, own retrieval channel
--   kg_doc.origin_url / workspace_id  = provenance + (dormant) tenant scope
--   kg_search_index.object_type now also carries DOC_CHUNK rows
--   kg_search_index.workspace_id = dormant tenant scope (V7)
-- All statements are idempotent (init-db tolerates "already exists" /
-- "duplicate column" errors).

CREATE TABLE IF NOT EXISTS `kg_doc_chunk`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `doc_no`        VARCHAR(512)    NOT NULL COMMENT '所属文档业务键',
    `chunk_no`      VARCHAR(191)    NOT NULL COMMENT 'chunk 业务键（doc_no#index 的 hash）',
    `chunk_index`   INT             NOT NULL COMMENT 'chunk 序号（0-based）',
    `content`       TEXT            NULL COMMENT 'chunk 正文',
    `origin_url`    VARCHAR(1024)   NOT NULL DEFAULT '' COMMENT '溯源 URL（网页来源）',
    `file_path`     VARCHAR(1024)   NOT NULL DEFAULT '' COMMENT '溯源文件路径',
    `line_start`    INT             NOT NULL DEFAULT 0 COMMENT '起始行号（1-based）',
    `line_end`      INT             NOT NULL DEFAULT 0 COMMENT '结束行号（1-based）',
    `sha256`        CHAR(64)        NOT NULL DEFAULT '' COMMENT '所属文档正文 SHA-256（增量跳过）',
    `workspace_id`  VARCHAR(128)    NULL COMMENT '租户/工作区（V7 预留，暂空）',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_chunk` (`graph_no`, `graph_version`, `chunk_no`),
    KEY `idx_doc` (`graph_no`, `graph_version`, `doc_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 文档切片（doc chunk 召回第三通道）';

-- provenance + dormant tenant scope on kg_doc
ALTER TABLE `kg_doc` ADD COLUMN `origin_url` VARCHAR(1024) NOT NULL DEFAULT '' COMMENT '溯源 URL（网页来源）' AFTER `source_type`;
ALTER TABLE `kg_doc` ADD COLUMN `workspace_id` VARCHAR(128) NULL COMMENT '租户/工作区（V7 预留，暂空）' AFTER `origin_url`;

-- dormant tenant scope on the search index
ALTER TABLE `kg_search_index` ADD COLUMN `workspace_id` VARCHAR(128) NULL COMMENT '租户/工作区（V7 预留，暂空）' AFTER `embedding`;
