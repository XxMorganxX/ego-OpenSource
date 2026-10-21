-- Provider `agent` (hybrid tenancy): a completion the engine wants
-- is a row here; the tenant's own kb-agent claims it through the gateway, runs it on the
-- tenant's Claude login, and posts the reply. Present in every database; inert for the
-- owner, whose routing never names provider `agent`.
CREATE TABLE llm_jobs (
    id            bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    task          text        NOT NULL,              -- routing task name
    system_prompt text        NOT NULL,
    user_prompt   text        NOT NULL,
    schema        jsonb,                             -- JSON Schema or NULL
    model_hint    text,                              -- advisory for the agent
    status        text        NOT NULL DEFAULT 'pending'
                  CHECK (status IN ('pending','claimed','done','failed','expired')),
    attempts      int         NOT NULL DEFAULT 0,
    claim_token   text,                              -- echoed by the agent on completion
    claimed_at    timestamptz,
    result        text,                              -- raw reply text
    error         text,
    retry_after   timestamptz,                       -- set by the agent on a quota failure
    created_at    timestamptz NOT NULL DEFAULT now(),
    finished_at   timestamptz
);
CREATE INDEX llm_jobs_open ON llm_jobs (status, created_at)
    WHERE status IN ('pending', 'claimed');

-- Producers wait on this channel (kb_core.ingest.agent_queue_client.JobWaiter).
CREATE OR REPLACE FUNCTION kb_notify_llm_job() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    PERFORM pg_notify('kb_llm_jobs',
                      json_build_object('id', NEW.id, 'status', NEW.status)::text);
    RETURN NULL;
END;
$$;
CREATE TRIGGER llm_jobs_notify
    AFTER INSERT OR UPDATE OF status ON llm_jobs
    FOR EACH ROW EXECUTE FUNCTION kb_notify_llm_job();

-- One row: when this tenant's agent last spoke to the gateway.
CREATE TABLE agent_state (
    id            int PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    last_seen_at  timestamptz,
    agent_version text,
    hostname      text
);
INSERT INTO agent_state (id) VALUES (1);
