-- ame-kb V3 migration: docs, source lines, and a hybrid search index.
--   kg_doc          = one row per ingested document (hash lives here now)
--   kg_doc_line     = numbered source lines, for Ref -> original-text lookup
--   kg_search_index = one row per node/edge, FULLTEXT(ngram) + embedding(JSON)
--                     for full-hybrid recall (FULLTEXT + vector cosine, RRF fused)

CREATE TABLE IF NOT EXISTS `kg_doc`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `doc_no`        VARCHAR(512)    NOT NULL COMMENT '文档业务键（相对路径）',
    `path`          VARCHAR(1024)   NOT NULL DEFAULT '' COMMENT '源文件绝对路径',
    `title`         VARCHAR(512)    NOT NULL DEFAULT '' COMMENT '文档标题',
    `sha256`        CHAR(64)        NOT NULL COMMENT '正文 SHA-256（hex），做增量跳过',
    `source_type`   VARCHAR(32)     NOT NULL DEFAULT '' COMMENT '来源类型：md/txt/pdf/html',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_doc` (`graph_no`, `graph_version`, `doc_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 文档表（含 sha256 增量指纹与溯源路径）';

CREATE TABLE IF NOT EXISTS `kg_doc_line`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `doc_no`        VARCHAR(512)    NOT NULL COMMENT '所属文档业务键',
    `line_no`       INT             NOT NULL COMMENT '行号（1-based）',
    `content`       TEXT            NULL COMMENT '该行原文',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_doc_line` (`graph_no`, `graph_version`, `doc_no`, `line_no`),
    KEY `idx_doc` (`graph_no`, `graph_version`, `doc_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 文档原文行（供 Ref 取回原文）';

CREATE TABLE IF NOT EXISTS `kg_search_index`
(
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`        VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version`   BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `object_type`     VARCHAR(16)     NOT NULL COMMENT '对象类型：NODE / EDGE',
    `object_no`       VARCHAR(191)    NOT NULL COMMENT '对象业务键（graph_node_no / graph_edge_no）',
    `searchable_text` TEXT            NOT NULL COMMENT 'name + description + properties 拍平文本',
    `embedding`       JSON            NULL COMMENT '向量（JSON 数组）',
    `updated_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '索引更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_object` (`graph_no`, `graph_version`, `object_type`, `object_no`),
    FULLTEXT KEY `ft_searchable` (`searchable_text`) WITH PARSER ngram
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 混合召回索引（FULLTEXT ngram + 向量）';

-- description columns for instance nodes/edges (idempotent; init-db tolerates
-- the "Duplicate column name" error when the column already exists).
ALTER TABLE `kg_graph_node` ADD COLUMN `description` TEXT NULL COMMENT '一句话描述' AFTER `type`;
ALTER TABLE `kg_graph_edge` ADD COLUMN `description` TEXT NULL COMMENT '一句话描述' AFTER `name`;
