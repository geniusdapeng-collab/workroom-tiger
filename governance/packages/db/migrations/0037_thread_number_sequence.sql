-- 0037 · Restore the atomic, durable T-* source expected by readable-id.ts.
-- 0016 is immutable. Its MAX(id) function was still installed even though the
-- shared insert helper already consumes an allocated number without adding one.

CREATE SEQUENCE IF NOT EXISTS public.thread_no_seq
  AS BIGINT START WITH 101 MINVALUE 101 MAXVALUE 9007199254740991
  INCREMENT BY 1 CACHE 1 NO CYCLE;

-- ALTER SEQUENCE holds a transaction lock against nextval/setval. Take it before
-- the table lock so an in-flight allocator can finish its INSERT without a lock
-- inversion. Then block manual inserts while computing the historical high-water
-- mark. RESTART is transactional, unlike setval; a failed migration changes none
-- of the source definition or state.
ALTER SEQUENCE public.thread_no_seq
  AS BIGINT INCREMENT BY 1 MINVALUE 101 MAXVALUE 9007199254740991
  CACHE 1 NO CYCLE;
LOCK TABLE public.threads IN SHARE ROW EXCLUSIVE MODE;

DO $$
DECLARE
  historical_next NUMERIC;
  reserved_next NUMERIC;
  next_number NUMERIC;
BEGIN
  SELECT COALESCE(MAX(SUBSTRING(id FROM 3)::numeric), 100) + 1
    INTO historical_next
    FROM public.threads
   WHERE id ~ '^T-[0-9]+$';
  SELECT last_value::numeric + CASE WHEN is_called THEN 1 ELSE 0 END
    INTO reserved_next
    FROM public.thread_no_seq;
  next_number := GREATEST(101, historical_next, reserved_next);
  -- makeReadableId accepts a JavaScript number. Refuse loss of precision instead
  -- of silently rounding a historical or newly allocated identifier.
  IF next_number > 9007199254740991 THEN
    RAISE EXCEPTION '0037: thread number source exceeds the exact supported integer range; reconcile historical identifiers before retrying';
  END IF;
  EXECUTE format('ALTER SEQUENCE public.thread_no_seq RESTART WITH %s', next_number);
END $$;

-- Keep the established function name for callers, but return the allocated value
-- directly. Never read MAX(id) in the allocation path: nextval alone arbitrates
-- concurrent callers; savepoint retries handle later manual-ID collisions.
CREATE OR REPLACE FUNCTION public.threads_max_t_no()
RETURNS BIGINT
LANGUAGE sql
VOLATILE
SECURITY DEFINER
SET search_path TO 'pg_catalog', 'pg_temp'
AS $function$
  SELECT pg_catalog.nextval('public.thread_no_seq'::pg_catalog.regclass);
$function$;

REVOKE ALL ON SEQUENCE public.thread_no_seq FROM PUBLIC, workloom_app, workloom_gateway;
REVOKE ALL ON FUNCTION public.threads_max_t_no() FROM PUBLIC;
GRANT EXECUTE ON FUNCTION public.threads_max_t_no() TO workloom_app, workloom_gateway;
