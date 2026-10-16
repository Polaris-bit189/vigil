-- 云侧 RDS 的表结构
--
-- ⚠️ 只有**四张表**。`messages` / `sender_names` / `msg_media` **不上云**：
--    · `sender_names` 是全库唯一含真实姓名的表
--    · `messages` / `msg_media` 含原文，违背「原文不出本机」
--    见 docs/design/隐私边界.md §四。
--
-- ⚠️ 列名与类型**逐列对齐 vigil/store.py 的 DDL** —— 这样下游查询
--    （`store.search_items` / `deadline_in_range` / `list_digests` 等）
--    一行都不用改，只有连接层换方言。
--
-- 用法（`vigil_ro` 是只读账号，对应 SQLite 那边的 `mode=ro`）：
--     mysql -h <RDS> -u root -p < deploy/mysql/schema.sql

CREATE DATABASE IF NOT EXISTS vigil
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;
CREATE DATABASE IF NOT EXISTS ov
  DEFAULT CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_ai_ci;

USE vigil;

-- ── items：日报与 Web 的唯一视图源 ────────────────────────────
CREATE TABLE IF NOT EXISTS items (
  item_id     BIGINT       PRIMARY KEY,
  kind        VARCHAR(64)  NOT NULL,
  title       TEXT         NOT NULL,
  detail      TEXT,
  event_ts    BIGINT       NOT NULL,
  deadline_ts BIGINT,
  group_id    BIGINT       NOT NULL,
  actor_uid   VARCHAR(64),
  place       TEXT,
  links       TEXT,
  amount      VARCHAR(255),
  confidence  DOUBLE       NOT NULL,
  model       VARCHAR(191) NOT NULL,
  prompt_ver  VARCHAR(64)  NOT NULL,
  created_at  BIGINT       NOT NULL
) ENGINE=InnoDB;

-- ⚠️ `deadline_ts` 在 SQLite 那边是**部分索引**（`WHERE deadline_ts IS NOT NULL`），
--    MySQL **不支持**部分索引 ⇒ 退化成普通索引。
--    代价很小：MySQL 的索引对 NULL 的处理比 SQLite 的部分索引更宽，
--    而在 items 这个量级上扫描差异可忽略。
CREATE INDEX items_kind_ts  ON items(kind, event_ts DESC);
CREATE INDEX items_group_ts ON items(group_id, event_ts DESC);
CREATE INDEX items_deadline ON items(deadline_ts);

-- ── item_sources：条目 ↔ 源消息（可回溯）──────────────────────
CREATE TABLE IF NOT EXISTS item_sources (
  item_id BIGINT NOT NULL,
  msg_id  BIGINT NOT NULL,
  PRIMARY KEY (item_id, msg_id)
) ENGINE=InnoDB;
CREATE INDEX item_sources_msg ON item_sources(msg_id);

-- ── digests：日报成品 ─────────────────────────────────────────
CREATE TABLE IF NOT EXISTS digests (
  digest_id   BIGINT       PRIMARY KEY,
  window_from BIGINT       NOT NULL,
  window_to   BIGINT       NOT NULL,
  body_md     MEDIUMTEXT   NOT NULL,
  model       VARCHAR(191) NOT NULL,
  prompt_ver  VARCHAR(64)  NOT NULL,
  created_at  BIGINT       NOT NULL
) ENGINE=InnoDB;
CREATE UNIQUE INDEX digests_window ON digests(window_from, window_to);

-- ── digest_items：日报 ↔ 条目互跳 ─────────────────────────────
CREATE TABLE IF NOT EXISTS digest_items (
  digest_id BIGINT NOT NULL,
  item_id   BIGINT NOT NULL,
  PRIMARY KEY (digest_id, item_id)
) ENGINE=InnoDB;
CREATE INDEX digest_items_item ON digest_items(item_id);

-- ── 人工干预层（overrides.py 的四张表）────────────────────────
USE ov;

CREATE TABLE IF NOT EXISTS item_edits (
  edit_id    BIGINT      PRIMARY KEY AUTO_INCREMENT,
  item_id    BIGINT      NOT NULL,
  msg_id     BIGINT,
  action     VARCHAR(32) NOT NULL,
  field      VARCHAR(32),
  old_value  TEXT,
  new_value  TEXT,
  actor      VARCHAR(64),
  created_at BIGINT      NOT NULL
) ENGINE=InnoDB;
CREATE INDEX item_edits_item ON item_edits(item_id);

CREATE TABLE IF NOT EXISTS item_state (
  item_id BIGINT      PRIMARY KEY,
  kind    VARCHAR(64),
  deleted TINYINT     NOT NULL DEFAULT 0
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS config_snapshots (
  snapshot_id BIGINT PRIMARY KEY AUTO_INCREMENT,
  saved_at    BIGINT NOT NULL,
  body        MEDIUMTEXT NOT NULL,
  aliases     TEXT
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS kind_alias (
  old_kind   VARCHAR(64) PRIMARY KEY,
  new_kind   VARCHAR(64) NOT NULL,
  created_at BIGINT      NOT NULL
) ENGINE=InnoDB;
