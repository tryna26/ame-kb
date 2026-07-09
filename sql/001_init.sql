-- ame-kb V1 schema. MySQL 8.0 (utf8mb4_0900_ai_ci).
-- Three-table design adapted from oceanai_site:
--   kg_domain_entity = schema layer (type definitions, seeded in V1)
--   kg_graph_node    = instance nodes
--   kg_graph_edge    = instance edges

CREATE TABLE IF NOT EXISTS `kg_domain_entity`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `entity_name`   VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '类型英文名（唯一标识，如 Person）',
    `cn_name`       VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '类型中文名',
    `entity_type`   VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '大类：Node / Relation',
    `description`   TEXT            NULL COMMENT '类型描述',
    `core_schema`   LONGTEXT        NULL COMMENT '字段定义（JSON 数组）',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `deleted`       TINYINT(1)      NOT NULL DEFAULT 0 COMMENT '软删除标志',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_entity_name` (`graph_no`, `graph_version`, `entity_name`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 类型定义表（schema 层）';

CREATE TABLE IF NOT EXISTS `kg_graph_node`
(
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `graph_node_no` VARCHAR(191)    NOT NULL COMMENT '业务键 = type:slug(name)，按名去重',
    `name`          VARCHAR(255)    NOT NULL DEFAULT '' COMMENT '节点名称',
    `type`          VARCHAR(64)     NOT NULL DEFAULT '' COMMENT '节点类型（引用 kg_domain_entity.entity_name）',
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
