-- Chunked embeddings: one whole-note vector cannot represent a long note —
-- KB_Inference clips embed input to ~6000 chars, so a long note's tail never
-- reached the model, and even
-- fully-embedded long notes dilute specific passages into a topic average.
-- node_chunks stores overlapping-window vectors; the search kNN arm scores a note
-- by the BEST of its whole-note vector and its chunk vectors (max-sim). Rows are
-- derived data — rebuilt by the embedding enricher whenever the embed-text hash
-- changes; char offsets recorded for provenance/snippeting.
CREATE TABLE node_chunks (
    node_id     uuid NOT NULL REFERENCES nodes(id) ON DELETE CASCADE,
    seq         int NOT NULL,
    char_start  int NOT NULL,
    char_end    int NOT NULL,
    embedding   vector(1024) NOT NULL,
    PRIMARY KEY (node_id, seq)
);
