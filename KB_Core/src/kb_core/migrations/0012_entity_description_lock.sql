-- Class descriptions are SLM-drafted but human-curatable. `description_locked` records who
-- owns the text: false (default) means the discover pass may (re)draft it; true means a human
-- edited it via set_class_description and the SLM must never overwrite it again.
ALTER TABLE entities ADD COLUMN description_locked boolean NOT NULL DEFAULT false;
