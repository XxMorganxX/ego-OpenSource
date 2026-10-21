You maintain a taxonomy of TYPES: broad, reusable kinds of thing found in a person's memories,
such as {{include:discover_classes.type_examples.md}}. A type answers "what KIND of thing is X?",
never "what is this memory about?" You are given the types that already exist.

The existing list may contain legacy malformed, duplicated, misspelled, overly specific, or
topic-like labels. Do not reuse a label merely because it appears in the list. Reuse an existing
name verbatim only when it is already a sound broad type. Otherwise ignore it; do not repair or
rename it in this response.

The memory's `Filed under` line is its folder path in the knowledge tree. Where a memory is filed
is strong evidence of what it is about and what kinds of things it contains — read the folder as
part of the memory, not as boilerplate.

Read this memory and identify only the sound broad types of important things it contains. Propose
a NEW type only when no sound existing type covers that kind of thing.

A valid type:

- is a singular lowercase common-noun phrase of one to four words;
- separates its words with single spaces (`research paper`), never by gluing them together
  with hyphens or underscores (`research-paper`, `research_paper`);
- naturally completes "X is a/an ___" for many unrelated possible X values;
- describes a durable kind of thing rather than this memory's subject;
- is broad enough to recur throughout the knowledge base.

{{include:discover_classes.examples.md}}

Return zero to three high-confidence types. When unsure, leave a type out.

For every type marked `new`, provide a one-line dictionary definition of what members of that type
are. The description must define the category, not a summary of this memory or a list of the
current memory's topics. Example: `organization` → `a company, nonprofit, institution, club, or
team`.
For an `existing` type, omit the description unless the supplied definition can be reused exactly.

Reply ONLY with JSON matching the schema, marking each type `existing` or `new` and giving each new
type its dictionary definition.

For each class, also report its structural `kind` — exactly one of: `person`, `organization`,
`place`, `event`, `project`, `artifact` (documents/papers/titled works), an idea, method, or
principle, `system` (software, tools, platforms, technical components), or `other`. The kind is
the broad sort of thing members of the class ARE, independent of domain.
