-- ame-kb V5 migration: cross-document entity fusion.
--   kg_entity_alias = alias -> canonical node_no, so a merged-away name still
--                     resolves to the surviving (canonical) entity and is
--                     folded into that entity's searchable_text at reindex time.
--   kg_merge_log    = one row per merge, with a full snapshot of the loser node
--                     and the edges it touched, so a merge can be rolled back.
-- All statements are idempotent (init-db tolerates "already exists" errors).

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
    `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT '自增主键',
    `graph_no`      VARCHAR(128)    NOT NULL DEFAULT 'default' COMMENT '所属图谱 No',
    `graph_version` BIGINT          NOT NULL DEFAULT 1 COMMENT '图谱版本',
    `merge_id`      VARCHAR(64)     NOT NULL COMMENT '合并事件 ID（uuid），回滚以此为键',
    `winner_node_no` VARCHAR(191)   NOT NULL COMMENT '存活的主实体业务键',
    `loser_node_no`  VARCHAR(191)   NOT NULL COMMENT '被合并（软删）的实体业务键',
    `snapshot`      JSON            NULL COMMENT '回滚快照：loser 原值 + winner 原值 + 重挂前的边端点',
    `status`        VARCHAR(16)     NOT NULL DEFAULT 'MERGED' COMMENT '状态：MERGED / ROLLED_BACK',
    `create_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
    `update_time`   DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',
    PRIMARY KEY (`id`),
    UNIQUE KEY `uk_merge` (`graph_no`, `graph_version`, `merge_id`),
    KEY `idx_winner` (`graph_no`, `graph_version`, `winner_node_no`)
) ENGINE = InnoDB
  DEFAULT CHARSET = utf8mb4
  COLLATE = utf8mb4_0900_ai_ci
  COMMENT = 'KG 实体合并审计（含快照，支持回滚）';

-- Semi-dynamic schema (V5): kg_domain_entity's uniqueness was global on
-- entity_name, which prevents two graphs from registering the same type name.
-- Re-scope it to (graph_no, graph_version, entity_name). Both statements are
-- idempotent-tolerant (init-db skips "already exists" / "can't drop" errors).
ALTER TABLE `kg_domain_entity` DROP INDEX `uk_entity_name`;
ALTER TABLE `kg_domain_entity`
    ADD UNIQUE KEY `uk_entity_name` (`graph_no`, `graph_version`, `entity_name`);
