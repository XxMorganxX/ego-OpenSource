-- Document-kind facet: what a memory IS (meeting notes, implementation plan, …), as
-- opposed to what it involves (entity classes) or what it is about (concepts). Coined
-- open-vocabulary by the doc_kind enrichment pass; NULL = not yet classified or the
-- model declined. Single-valued on purpose: a document primarily is one thing.
ALTER TABLE nodes ADD COLUMN doc_kind text;
CREATE INDEX nodes_doc_kind_idx ON nodes (doc_kind) WHERE doc_kind IS NOT NULL;
