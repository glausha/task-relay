-- task-relay SQLite schema. Source of truth: detailed-design v1.0 §2.1.
-- PRAGMAs (journal_mode/synchronous/foreign_keys/busy_timeout) are set on connection, not here.

CREATE TABLE IF NOT EXISTS schema_version (
    version INTEGER PRIMARY KEY
);
INSERT OR IGNORE INTO schema_version(version) VALUES (1);

CREATE TABLE IF NOT EXISTS tasks (
    task_id                  TEXT PRIMARY KEY,
    source_issue_id          TEXT,
    state                    TEXT NOT NULL CHECK (state IN (
        'new','planning','plan_pending_approval','plan_approved',
        'implementing','implementing_resume_pending','needs_fix',
        'reviewing','human_review_required','done','cancelled','system_degraded'
    )),
    state_rev                INTEGER NOT NULL CHECK (state_rev >= 0),
    critical                 INTEGER NOT NULL DEFAULT 0 CHECK (critical IN (0,1)),
    current_branch           TEXT,
    manual_gate_required     INTEGER NOT NULL DEFAULT 0 CHECK (manual_gate_required IN (0,1)),
    last_known_head_commit   TEXT,
    resume_target_state      TEXT CHECK (resume_target_state IS NULL OR resume_target_state IN (
        'new','planning','plan_pending_approval','plan_approved',
        'implementing','implementing_resume_pending','needs_fix',
        'reviewing','human_review_required','done','cancelled','system_degraded'
    )),
    requested_by             TEXT NOT NULL,
    created_at               TEXT NOT NULL,
    updated_at               TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
CREATE INDEX IF NOT EXISTS idx_tasks_branch ON tasks(current_branch);

CREATE TABLE IF NOT EXISTS plans (
    task_id           TEXT NOT NULL,
    plan_rev          INTEGER NOT NULL CHECK (plan_rev >= 1),
    planner_version   TEXT NOT NULL,
    plan_json         TEXT NOT NULL,
    validator_score   INTEGER NOT NULL CHECK (validator_score BETWEEN 0 AND 100),
    validator_errors  INTEGER NOT NULL CHECK (validator_errors >= 0),
    approved_by       TEXT,
    approved_at       TEXT,
    approved_kind     TEXT CHECK (approved_kind IS NULL OR approved_kind IN ('auto','manual')),
    created_at        TEXT NOT NULL,
    PRIMARY KEY (task_id, plan_rev),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS event_inbox (
    event_id        TEXT PRIMARY KEY,
    source          TEXT NOT NULL CHECK (source IN ('forgejo','discord','cli','internal')),
    delivery_id     TEXT NOT NULL,
    event_type      TEXT NOT NULL,
    payload_json    TEXT NOT NULL,
    journal_offset  INTEGER NOT NULL,
    received_at     TEXT NOT NULL,
    processed_at    TEXT,
    UNIQUE (source, delivery_id)
);
CREATE INDEX IF NOT EXISTS idx_inbox_unprocessed ON event_inbox(processed_at) WHERE processed_at IS NULL;

CREATE TABLE IF NOT EXISTS projection_outbox (
    outbox_id          INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id            TEXT NOT NULL,
    stream             TEXT NOT NULL CHECK (stream IN ('task_snapshot','task_comment','task_label_sync','discord_alert')),
    target             TEXT NOT NULL,
    origin_event_id    TEXT NOT NULL,
    payload_json       TEXT NOT NULL,
    state_rev          INTEGER NOT NULL,
    idempotency_key    TEXT NOT NULL,
    claimed_by         TEXT,
    claimed_at         TEXT,
    attempt_count      INTEGER NOT NULL DEFAULT 0,
    next_attempt_at    TEXT NOT NULL,
    sent_at            TEXT,
    UNIQUE (stream, target, idempotency_key),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_outbox_pending
    ON projection_outbox(task_id, stream, target, outbox_id)
    WHERE sent_at IS NULL;

CREATE TABLE IF NOT EXISTS projection_cursors (
    task_id              TEXT NOT NULL,
    stream               TEXT NOT NULL,
    target               TEXT NOT NULL,
    last_sent_state_rev  INTEGER NOT NULL,
    last_sent_outbox_id  INTEGER NOT NULL,
    updated_at           TEXT NOT NULL,
    PRIMARY KEY (task_id, stream, target),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS branch_waiters (
    branch        TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    queue_order   INTEGER NOT NULL,
    status        TEXT NOT NULL CHECK (status IN ('queued','leased','paused_system_degraded','removed')),
    PRIMARY KEY (branch, task_id),
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_waiters_queue ON branch_waiters(branch, queue_order);

CREATE TABLE IF NOT EXISTS branch_tokens (
    branch      TEXT PRIMARY KEY,
    last_token  INTEGER NOT NULL DEFAULT 0 CHECK (last_token >= 0)
);

CREATE TABLE IF NOT EXISTS journal_ingester_state (
    singleton_id INTEGER PRIMARY KEY CHECK (singleton_id = 1),
    last_file    TEXT,
    last_offset  INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL
);
INSERT OR IGNORE INTO journal_ingester_state(singleton_id, last_file, last_offset, updated_at)
VALUES (1, NULL, 0, '1970-01-01T00:00:00Z');

CREATE TABLE IF NOT EXISTS tool_calls (
    call_id       TEXT PRIMARY KEY,
    task_id       TEXT NOT NULL,
    stage         TEXT NOT NULL CHECK (stage IN ('planning','executing','reviewing')),
    tool_name     TEXT NOT NULL,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    duration_ms   INTEGER,
    success       INTEGER CHECK (success IS NULL OR success IN (0,1)),
    exit_code     INTEGER,
    failure_code  TEXT,
    log_path      TEXT,
    log_sha256    TEXT,
    log_bytes     INTEGER,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    FOREIGN KEY (task_id) REFERENCES tasks(task_id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_tool_calls_task_stage ON tool_calls(task_id, stage);

CREATE TABLE IF NOT EXISTS rate_windows (
    tool_name          TEXT PRIMARY KEY,
    window_started_at  TEXT NOT NULL,
    window_reset_at    TEXT NOT NULL,
    remaining          INTEGER NOT NULL,
    "limit"            INTEGER NOT NULL,
    updated_at         TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS system_events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id       TEXT,
    event_type    TEXT NOT NULL,
    severity      TEXT NOT NULL CHECK (severity IN ('info','warning','error')),
    payload_json  TEXT NOT NULL,
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_system_events_created ON system_events(created_at);
