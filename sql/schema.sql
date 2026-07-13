-- ame-kb consolidated schema. MySQL 8.0 (utf8mb4_0900_ai_ci).
-- Single authoritative DDL: applying this to an empty database reproduces the
-- exact live schema. Supersedes the old incremental migrations 001-009.
-- All statements are idempotent-friendly (init-db tolerates "already exists").
--
-- Layout:
--   Graph layer   : kg_graph_node (structural), kg_domain_entity (ontology),
--                   kg_graph_edge
--   Document layer: kg_doc, kg_doc_line, kg_doc_chunk, kg_doc_version
--   Retrieval     : kg_search_index (FULLTEXT ngram + vector JSON)
--   Fusion        : kg_entity_alias, kg_merge_log
--   Registry      : kg_graph, kg_graph_file
--   Pipeline      : kg_task, kg_pipeline_run, kg_pipeline_step

-- ============================================================
-- Graph layer
-- ============================================================

CREATE TABLE IF NOT EXISTS `kg_graph_node`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `graph_node_no` VARCHAR(191)    NOT NULL COMMENT '业务键 = type:slug(name)，按名去重',
    `name`          VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '节点名称',
    `type`          VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '结构层角色：NODE / ENTITY / SKILL（抽取产出恒为 ENTITY）',
    `description`   TEXT            NULL COMMENT '一句话描述',
    `properties`    JSON            NULL COMMENT '属性值兜底',
    `ref`           JSON            NULL COMMENT '来源指针 {docPath:[行范围]}',
    `deleted`       TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '软删除标志',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_node_no` (`graph_no`, `graph_version`, `graph_node_no`),
    KEY `idx_name` (`name`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 实例节点表';

CREATE TABLE IF NOT EXISTS `kg_domain_entity`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `graph_node_no` VARCHAR(191)    NOT NULL COMMENT '关联键 = kg_graph_node.graph_node_no（同一实体两副面孔）',
    `name`          VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '实体标准名',
    `type`          VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '本体分类：Asset / Relation / Event / Behavior',
    `entity_spec`   VARCHAR(32)     NULL COMMENT '原型：Mission/Solution/Implementation/ServiceInstance/Artifact（仅 type=Asset 时填）',
    `properties`    JSON            NULL COMMENT '属性值兜底',
    `deleted`       TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '软删除标志',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_domain_node` (`graph_no`, `graph_version`, `graph_node_no`),
    KEY `idx_type` (`graph_no`, `graph_version`, `type`, `deleted`),
    KEY `idx_name` (`name`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 领域实体实例表（本体分类层，与 kg_graph_node 同源）';

CREATE TABLE IF NOT EXISTS `kg_graph_edge`
(
    `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`       VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version`  BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `graph_edge_no`  VARCHAR(191)    NOT NULL COMMENT '业务键 = hash(src+label+dst)',
    `source_node_no` VARCHAR(191)    NOT NULL COMMENT '起点（引用 kg_graph_node.graph_node_no）',
    `target_node_no` VARCHAR(191)    NOT NULL COMMENT '终点（引用 kg_graph_node.graph_node_no）',
    `name`           VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '关系标签（snake_case）',
    `description`    TEXT            NULL COMMENT '一句话描述',
    `properties`     JSON            NULL COMMENT '属性值兜底',
    `ref`            JSON            NULL COMMENT '来源指针 {docPath:[行范围]}',
    `deleted`        TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '软删除标志',
    `create_time`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_edge_no` (`graph_no`, `graph_version`, `graph_edge_no`),
    KEY `idx_src` (`source_node_no`),
    KEY `idx_dst` (`target_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 实例边表';

-- ============================================================
-- Document layer
-- ============================================================

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
    `origin_url`    VARCHAR(1024)   NOT NULL DEFAULT '' COMMENT '溯源 URL（网页来源）',
    `workspace_id`  VARCHAR(128)    NULL COMMENT '租户/工作区（V7 预留，暂空）',
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

CREATE TABLE IF NOT EXISTS `kg_doc_version`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `doc_id`        VARCHAR(512)    NOT NULL COMMENT '文档业务键（相对路径）',
    `content_hash`  CHAR(64)        NOT NULL COMMENT '正文 SHA-256（hex）',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_doc` (`graph_no`, `graph_version`, `doc_id`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 文档内容快照（按 hash 做增量跳过）';

-- ============================================================
-- Retrieval
-- ============================================================

CREATE TABLE IF NOT EXISTS `kg_search_index`
(
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`        VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version`   BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `object_type`     VARCHAR(16)     NOT NULL COMMENT '对象类型：NODE / EDGE / DOC_CHUNK',
    `object_no`       VARCHAR(191)    NOT NULL COMMENT '对象业务键（graph_node_no / graph_edge_no / chunk_no）',
    `searchable_text` TEXT            NOT NULL COMMENT 'name + description + properties 拍平文本',
    `embedding`       JSON            NULL COMMENT '向量（JSON 数组）',
    `workspace_id`    VARCHAR(128)    NULL COMMENT '租户/工作区（V7 预留，暂空）',
    `updated_at`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '索引更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_object` (`graph_no`, `graph_version`, `object_type`, `object_no`),
    FULLTEXT KEY `ft_searchable` (`searchable_text`) WITH PARSER ngram
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 混合召回索引（FULLTEXT ngram + 向量）';

-- ============================================================
-- Entity fusion
-- ============================================================

CREATE TABLE IF NOT EXISTS `kg_entity_alias`
(
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`          VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version`     BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `canonical_node_no` VARCHAR(191)    NOT NULL COMMENT '主实体业务键（合并后的存活节点）',
    `alias`             VARCHAR(255)    NOT NULL COMMENT '别名（被合并实体的名字/旧名）',
    `source`            VARCHAR(64)     NOT NULL DEFAULT 'merge' COMMENT '别名来源：merge/manual',
    `create_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_alias` (`graph_no`, `graph_version`, `alias`),
    KEY `idx_canonical` (`graph_no`, `graph_version`, `canonical_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 实体别名（别名 -> 主实体，供别名搜到主实体）';

CREATE TABLE IF NOT EXISTS `kg_merge_log`
(
    `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`       VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version`  BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `merge_id`       VARCHAR(64)     NOT NULL COMMENT '合并事件 ID（uuid），回滚以此为键',
    `winner_node_no` VARCHAR(191)    NOT NULL COMMENT '存活的主实体业务键',
    `loser_node_no`  VARCHAR(191)    NOT NULL COMMENT '被合并（软删）的实体业务键',
    `snapshot`       JSON            NULL COMMENT '回滚快照：loser 原值 + winner 原值 + 重挂前的边端点',
    `status`         VARCHAR(16)     NOT NULL DEFAULT 'MERGED' COMMENT '状态：MERGED / ROLLED_BACK',
    `create_time`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_merge` (`graph_no`, `graph_version`, `merge_id`),
    KEY `idx_winner` (`graph_no`, `graph_version`, `winner_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 实体合并审计（含快照，支持回滚）';

-- ============================================================
-- Graph registry
-- ============================================================

CREATE TABLE IF NOT EXISTS `kg_graph`
(
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`        VARCHAR(128)    NOT NULL COMMENT '图谱 No（系统生成 graph_<8位uuid>）',
    `graph_version`   BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `name`            VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '图谱显示名',
    `status`          VARCHAR(16)     NOT NULL DEFAULT 'ACTIVE' COMMENT '状态：BUILDING / ACTIVE / FROZEN',
    `create_time`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    `projection_done` TINYINT(1)      NOT NULL DEFAULT 0 COMMENT 'vN -> vN+1 unchanged-slice projection checkpoint',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_graph_ver` (`graph_no`, `graph_version`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 图谱注册表（图谱 + 版本 + 生命周期状态）';

CREATE TABLE IF NOT EXISTS `kg_graph_file`
(
    `id`          BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`    VARCHAR(128)    NOT NULL COMMENT '所属图谱 No',
    `doc_no`      VARCHAR(512)    NOT NULL COMMENT '文档业务键（相对图谱根的路径 / url:...）',
    `path`        VARCHAR(1024)   NOT NULL DEFAULT '' COMMENT '源文件绝对路径（url 源为 URL）',
    `source_type` VARCHAR(32)     NOT NULL DEFAULT '' COMMENT '来源类型：md/txt/pdf/html/url',
    `origin_url`  VARCHAR(1024)   NOT NULL DEFAULT '' COMMENT 'URL 源的原始地址（文件源为空）',
    `deleted`     TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '软删除标志（从清单移除）',
    `create_time` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time` DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_graph_file` (`graph_no`, `doc_no`),
    KEY `idx_graph` (`graph_no`, `deleted`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 文件清单（跨版本存活，不带 graph_version）';

-- ============================================================
-- Durable async pipeline
-- ============================================================

CREATE TABLE IF NOT EXISTS `kg_task`
(
    `id`               BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `task_no`          VARCHAR(64)     NOT NULL,
    `task_type`        VARCHAR(32)     NOT NULL DEFAULT 'INGEST',
    `graph_no`         VARCHAR(128)    NOT NULL,
    `status`           VARCHAR(16)     NOT NULL DEFAULT 'QUEUED',
    `payload`          JSON            NULL,
    `progress_current` INT             NOT NULL DEFAULT 0,
    `progress_total`   INT             NOT NULL DEFAULT 0,
    `attempts`         INT             NOT NULL DEFAULT 0,
    `max_attempts`     INT             NOT NULL DEFAULT 3,
    `available_at`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `lease_expires_at` DATETIME        NULL,
    `worker_id`        VARCHAR(128)    NOT NULL DEFAULT '',
    `error`            TEXT            NULL,
    `active_key`       VARCHAR(128)    NULL COMMENT 'graph_no while active, NULL when terminal',
    `create_time`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_task_no` (`task_no`),
    UNIQUE KEY `uk_active_graph_task` (`active_key`),
    KEY `idx_task_claim` (`status`, `available_at`, `create_time`),
    KEY `idx_task_graph` (`graph_no`, `create_time`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG durable background tasks';

CREATE TABLE IF NOT EXISTS `kg_pipeline_run`
(
    `id`              BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `run_no`          VARCHAR(64)     NOT NULL,
    `task_no`         VARCHAR(64)     NOT NULL,
    `graph_no`        VARCHAR(128)    NOT NULL,
    `base_version`    BIGINT          NULL,
    `target_version`  BIGINT          NOT NULL DEFAULT 1,
    `status`          VARCHAR(16)     NOT NULL DEFAULT 'PENDING',
    `total_steps`     INT             NOT NULL DEFAULT 0,
    `completed_steps` INT             NOT NULL DEFAULT 0,
    `failed_steps`    INT             NOT NULL DEFAULT 0,
    `error`           TEXT            NULL,
    `started_at`      DATETIME        NULL,
    `finished_at`     DATETIME        NULL,
    `create_time`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`     DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_run_no` (`run_no`),
    UNIQUE KEY `uk_run_task` (`task_no`),
    KEY `idx_run_graph` (`graph_no`, `create_time`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG pipeline runs';

CREATE TABLE IF NOT EXISTS `kg_pipeline_step`
(
    `id`           BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `step_no`      VARCHAR(64)     NOT NULL,
    `run_no`       VARCHAR(64)     NOT NULL,
    `step_key`     VARCHAR(512)    NOT NULL COMMENT 'document doc_no',
    `status`       VARCHAR(16)     NOT NULL DEFAULT 'PENDING',
    `attempts`     INT             NOT NULL DEFAULT 0,
    `max_attempts` INT             NOT NULL DEFAULT 3,
    `error`        TEXT            NULL,
    `started_at`   DATETIME        NULL,
    `finished_at`  DATETIME        NULL,
    `create_time`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`  DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_step_no` (`step_no`),
    UNIQUE KEY `uk_run_step` (`run_no`, `step_key`),
    KEY `idx_step_run_status` (`run_no`, `status`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Per-document durable pipeline checkpoints';
