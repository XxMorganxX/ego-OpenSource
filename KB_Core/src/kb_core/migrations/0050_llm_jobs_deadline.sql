-- The producer's window on each llm_jobs row: enqueue time + how long the
-- producer (AgentQueueClient) waits before expiring the job. The gateway hands it to the
-- agent in the claim, the agent sizes its run to what is left, and a job with too little
-- left is never claimed. NULL for rows enqueued before this column existed: no deadline.
ALTER TABLE llm_jobs ADD COLUMN deadline timestamptz;
