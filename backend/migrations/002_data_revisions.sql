-- 数据修订与复核历史（MySQL 8.0 语法；运行时不执行，建表由 SQLAlchemy create_all 完成）
-- 既有库升级：先执行本文件，再启动新版本后端。

CREATE TABLE data_revisions (
  id            BIGINT PRIMARY KEY AUTO_INCREMENT,
  revision_no   VARCHAR(40) NOT NULL UNIQUE,
  data_cutoff   DATETIME NOT NULL UNIQUE,
  source        VARCHAR(12) NOT NULL DEFAULT 'mixed',
  status        VARCHAR(16) NOT NULL DEFAULT 'draft',
  note          TEXT,
  stats         JSON,
  created_by    VARCHAR(160) NOT NULL DEFAULT '',
  confirmed_by  VARCHAR(160),
  confirmed_at  DATETIME,
  seed_key      VARCHAR(120) UNIQUE,
  created_at    DATETIME NOT NULL,
  updated_at    DATETIME NOT NULL,
  INDEX ix_revisions_status (status)
);

CREATE TABLE review_archives (
  id             BIGINT PRIMARY KEY AUTO_INCREMENT,
  anomaly_id     BIGINT NOT NULL,
  revision_id    BIGINT NULL,
  status         VARCHAR(16) NOT NULL,
  reviewer       VARCHAR(160),
  review_note    TEXT,
  reviewed_at    DATETIME,
  prev_direction VARCHAR(8) NOT NULL,
  prev_severity  VARCHAR(12) NOT NULL,
  prev_impact    DOUBLE NOT NULL DEFAULT 0,
  prev_z         DOUBLE NOT NULL DEFAULT 0,
  new_direction  VARCHAR(8) NOT NULL,
  new_severity   VARCHAR(12) NOT NULL,
  new_impact     DOUBLE NOT NULL DEFAULT 0,
  new_z          DOUBLE NOT NULL DEFAULT 0,
  created_at     DATETIME NOT NULL,
  INDEX ix_review_archives_anomaly (anomaly_id),
  INDEX ix_review_archives_revision (revision_id),
  CONSTRAINT fk_archive_anomaly FOREIGN KEY (anomaly_id) REFERENCES anomalies(id) ON DELETE CASCADE,
  CONSTRAINT fk_archive_revision FOREIGN KEY (revision_id) REFERENCES data_revisions(id) ON DELETE SET NULL
);

ALTER TABLE anomalies
  ADD COLUMN revision_id BIGINT NULL AFTER run_id,
  ADD INDEX ix_anomalies_revision_id (revision_id),
  ADD CONSTRAINT fk_anomaly_revision FOREIGN KEY (revision_id) REFERENCES data_revisions(id) ON DELETE SET NULL;

ALTER TABLE reports
  ADD COLUMN revision_id BIGINT NULL AFTER snapshot,
  ADD COLUMN dedupe_key VARCHAR(120) NULL UNIQUE AFTER seed_key,
  ADD INDEX ix_reports_revision_id (revision_id),
  ADD CONSTRAINT fk_report_revision FOREIGN KEY (revision_id) REFERENCES data_revisions(id) ON DELETE SET NULL;
