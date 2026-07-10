-- ame-kb V6 migration: graph registry + cross-version file manifest.
--   kg_graph      = one row per (graph_no, graph_version). status tracks the
--                   build lifecycle: BUILDING (a version being assembled) ->
--                   ACTIVE (queryable latest) ; older versions become FROZEN.
--   kg_graph_file = the user-maintained file manifest. It is NOT versioned
--                   (no graph_version): it persists across versions so a graph
--                   keeps a single evolving file list. Each ingest snapshots the
--                   live (deleted=0) rows into a new version.
-- All statements are idempotent (init-db tolerates "already exists" errors).

CREATE TABLE IF NOT EXISTS `kg_graph`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL COMMENT '图谱 No（系统生成 graph_<8位uuid>）',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `name`          VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '图谱显示名',
    `status`        VARCHAR(16)     NOT NULL DEFAULT 'ACTIVE' COMMENT '状态：BUILDING / ACTIVE / FROZEN',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
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
