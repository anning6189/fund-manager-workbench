PRAGMA foreign_keys = ON;

INSERT INTO workflow_events(run_id,task_id,event_type,severity,occurred_at,detail_json)
SELECT run_id,NULL,'human_review_gate_removed','info',strftime('%Y-%m-%dT%H:%M:%fZ','now'),
       '{"release_mode":"automatic_quality_gates"}'
FROM workflow_runs
WHERE status = 'pending_human_review';

UPDATE workflow_tasks
SET status = 'skipped',
    completed_at = COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    output_json = '{"reason":"human_review_gate_removed","release_mode":"automatic_quality_gates"}'
WHERE task_id = 'human_review_gate'
  AND status = 'pending';

DELETE FROM workflow_reviews
WHERE review_type = 'human_publication_gate'
  AND decision = 'pending';

UPDATE workflow_runs
SET status = 'completed',
    completed_at = COALESCE(completed_at, strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    publication_status = 'internal_research_ready',
    human_review_required = 0
WHERE status = 'pending_human_review'
  AND EXISTS (
      SELECT 1 FROM workflow_tasks t
      WHERE t.run_id = workflow_runs.run_id
        AND t.task_id = 'compliance_review'
        AND t.status = 'completed'
  );

UPDATE workflow_runs
SET publication_status = 'internal_research_ready',
    human_review_required = 0
WHERE status = 'completed'
  AND publication_status IN ('draft_pending_review','approved_for_publication');

UPDATE workflow_runs
SET human_review_required = 0
WHERE human_review_required <> 0;

UPDATE model_runs
SET human_review_required = 0,
    publication_status = CASE
        WHEN publication_status = 'draft_pending_review' THEN 'internal_research_ready'
        ELSE publication_status
    END
WHERE human_review_required <> 0 OR publication_status = 'draft_pending_review';

UPDATE workflow_claims
SET status = 'internal_research_ready'
WHERE run_id IN (
    SELECT run_id FROM workflow_runs
    WHERE status = 'completed' AND publication_status = 'internal_research_ready'
);

INSERT OR IGNORE INTO schema_migrations(version, applied_at)
VALUES ('009_remove_human_review_gate', strftime('%Y-%m-%dT%H:%M:%fZ','now'));
