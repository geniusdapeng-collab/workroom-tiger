-- 0036 · Restore the existing Quest/review-console persistence contract.
-- Runtime already reads/writes these fields and states. Historical migrations stay
-- immutable; this forward migration preserves rows, RLS policies, grants and events.

-- Older approval snapshots may already carry a one-time consumption marker.
-- Validate it before changing the schema: malformed evidence must stop the atomic
-- migration, not silently become a fresh authorization. Report only the approval ID.
DO $$
DECLARE
  legacy RECORD;
  consumed TIMESTAMPTZ;
BEGIN
  FOR legacy IN
    SELECT approval_id, snapshot->'used_at' AS marker, snapshot->>'used_at' AS used_at
      FROM approvals
     WHERE snapshot ? 'used_at' AND snapshot->'used_at' <> 'null'::jsonb
  LOOP
    IF jsonb_typeof(legacy.marker) <> 'string'
       OR legacy.used_at !~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?(Z|[+-][0-9]{2}:[0-9]{2})$' THEN
      RAISE EXCEPTION '0036: approval % has invalid snapshot.used_at; reconcile the historical evidence before retrying', legacy.approval_id;
    END IF;
    BEGIN
      consumed := legacy.used_at::timestamptz;
    EXCEPTION WHEN OTHERS THEN
      RAISE EXCEPTION '0036: approval % has invalid snapshot.used_at; reconcile the historical evidence before retrying', legacy.approval_id;
    END;
  END LOOP;
END $$;

-- NULL means no plan was persisted. The runtime starts a new plan at version 1,
-- reuses a nonempty saved array on replay, and advances the version on replan.
ALTER TABLE threads
  ADD COLUMN IF NOT EXISTS plan JSONB,
  ADD COLUMN IF NOT EXISTS plan_version INTEGER NOT NULL DEFAULT 0;
ALTER TABLE threads ADD CONSTRAINT threads_plan_array_check
  CHECK (plan IS NULL OR jsonb_typeof(plan) = 'array');
ALTER TABLE threads ADD CONSTRAINT threads_plan_version_check
  CHECK (plan_version >= 0);

-- NULL remains the never-consumed state used by the gateway's transactional CAS.
-- Preserve a legacy consumption time without modifying the original snapshot.
ALTER TABLE approvals ADD COLUMN IF NOT EXISTS consumed_at TIMESTAMPTZ;
UPDATE approvals
   SET consumed_at = (snapshot->>'used_at')::timestamptz
 WHERE consumed_at IS NULL
   AND snapshot ? 'used_at' AND snapshot->'used_at' <> 'null'::jsonb;

-- Match the existing shared enums exactly. Unknown states remain rejected.
ALTER TABLE approvals DROP CONSTRAINT approvals_status_check;
ALTER TABLE approvals ADD CONSTRAINT approvals_status_check
  CHECK (status IN ('pending','approved','edited','rejected','expired','superseded'));

ALTER TABLE threads DROP CONSTRAINT threads_status_check;
ALTER TABLE threads ADD CONSTRAINT threads_status_check
  CHECK (status IN ('queued','running','pending_review','completed','failed','paused','cancelled'));
