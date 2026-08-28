-- ame-kb V3 fresh-install tables for stable Node identity and provenance.
-- Existing databases are upgraded by ame_kb.migrations.ensure_v3_schema();
-- this file deliberately contains only CREATE TABLE IF NOT EXISTS statements.

CREATE TABLE IF NOT EXISTS `kg_node_alias`
(
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `graph_no`          VARCHAR(128)    NOT NULL DEFAULT 'default',
    `graph_version`     BIGINT          NOT NULL DEFAULT 1,
    `type`              VARCHAR(64)     NOT NULL DEFAULT '',
    `normalized_alias`  VARCHAR(255)    NOT NULL,
    `canonical_node_no` VARCHAR(191)    NOT NULL,
    `alias`             VARCHAR(255)    NOT NULL,
    `source`            VARCHAR(64)     NOT NULL DEFAULT 'migration',
    `create_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_node_alias` (`graph_no`, `graph_version`, `type`, `normalized_alias`, `canonical_node_no`),
    KEY `idx_alias_lookup` (`graph_no`, `graph_version`, `type`, `normalized_alias`),
    KEY `idx_alias_canonical` (`graph_no`, `graph_version`, `canonical_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Node aliases; one normalized alias may have multiple canonical candidates';

CREATE TABLE IF NOT EXISTS `kg_node_legacy_id`
(
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `graph_no`          VARCHAR(128)    NOT NULL DEFAULT 'default',
    `graph_version`     BIGINT          NOT NULL DEFAULT 1,
    `legacy_node_no`    VARCHAR(191)    NOT NULL,
    `canonical_node_no` VARCHAR(191)    NOT NULL,
    `create_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_legacy_node_id` (`graph_no`, `graph_version`, `legacy_node_no`),
    KEY `idx_legacy_canonical` (`graph_no`, `graph_version`, `canonical_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Permanent legacy Node ID redirects';

CREATE TABLE IF NOT EXISTS `kg_node_contribution`
(
    `id`                BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `graph_no`          VARCHAR(128)    NOT NULL DEFAULT 'default',
    `graph_version`     BIGINT          NOT NULL DEFAULT 1,
    `source_id`         VARCHAR(191)    NOT NULL DEFAULT 'default',
    `doc_id`            VARCHAR(512)    NOT NULL,
    `doc_key`           CHAR(64)        NOT NULL,
    `mention_key`       CHAR(64)        NOT NULL,
    `mention_id`        VARCHAR(191)    NULL COMMENT '文档内稳定 locator，用于改名后复用 Node UUID',
    `canonical_node_no` VARCHAR(191)    NOT NULL,
    `canonical_rank`    INT             NOT NULL DEFAULT 0 COMMENT 'canonical 聚合优先级，越小越优先',
    `name`              VARCHAR(255)    NOT NULL DEFAULT '',
    `type`              VARCHAR(64)     NOT NULL DEFAULT '',
    `properties`        JSON            NULL,
    `ref`               JSON            NULL,
    `extraction_hash`   CHAR(64)        NULL,
    `create_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`       DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_node_contribution` (`graph_no`, `graph_version`, `doc_key`, `mention_key`),
    KEY `idx_node_contribution_canonical` (`graph_no`, `graph_version`, `canonical_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Per-document Node mention contributions';

CREATE TABLE IF NOT EXISTS `kg_edge_contribution`
(
    `id`                   BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `graph_no`             VARCHAR(128)    NOT NULL DEFAULT 'default',
    `graph_version`        BIGINT          NOT NULL DEFAULT 1,
    `source_id`            VARCHAR(191)    NOT NULL DEFAULT 'default',
    `doc_id`               VARCHAR(512)    NOT NULL,
    `doc_key`              CHAR(64)        NOT NULL,
    `edge_key`             CHAR(64)        NOT NULL,
    `source_mention_key`   CHAR(64)        NOT NULL,
    `target_mention_key`   CHAR(64)        NOT NULL,
    `source_node_no`       VARCHAR(191)    NOT NULL,
    `target_node_no`       VARCHAR(191)    NOT NULL,
    `name`                 VARCHAR(64)     NOT NULL DEFAULT '',
    `confidence`           VARCHAR(32)     NOT NULL DEFAULT 'INFERRED',
    `properties`           JSON            NULL,
    `ref`                  JSON            NULL,
    `extraction_hash`      CHAR(64)        NULL,
    `create_time`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`          DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_edge_contribution` (`graph_no`, `graph_version`, `doc_key`, `edge_key`),
    KEY `idx_edge_contribution_nodes` (`graph_no`, `graph_version`, `source_node_no`, `target_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Per-document Edge contributions';

-- Compatible with the shared V6 table: no V3-only required columns.
CREATE TABLE IF NOT EXISTS `kg_merge_log`
(
    `id`             BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `graph_no`       VARCHAR(128)    NOT NULL DEFAULT 'default',
    `graph_version`  BIGINT          NOT NULL DEFAULT 1,
    `merge_id`       VARCHAR(64)     NOT NULL,
    `winner_node_no` VARCHAR(191)    NOT NULL,
    `loser_node_no`  VARCHAR(191)    NOT NULL,
    `snapshot`       JSON            NULL,
    `status`         VARCHAR(16)     NOT NULL DEFAULT 'MERGED',
    `create_time`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `update_time`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_merge` (`graph_no`, `graph_version`, `merge_id`),
    KEY `idx_winner` (`graph_no`, `graph_version`, `winner_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Node merge audit log compatible with shared V6 schema';

CREATE TABLE IF NOT EXISTS `kg_schema_migration`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `migration_key` VARCHAR(191)    NOT NULL,
    `checksum`      CHAR(64)        NULL,
    `applied_at`    DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_schema_migration` (`migration_key`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Applied schema migration markers';

CREATE TABLE IF NOT EXISTS `kg_graph_write_lock`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    `graph_no`      VARCHAR(128)    NOT NULL,
    `graph_version` BIGINT          NOT NULL,
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_graph_write_lock` (`graph_no`, `graph_version`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'Per-graph transaction serialization lock';
