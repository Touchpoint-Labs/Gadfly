You are given the project's EXISTING RULES and DIFFS where a human edited code the
builder wrote. Decide whether any diff reveals a durable lesson worth remembering —
usually none do. Return only what genuinely generalizes and is NOT already covered by
the existing rules; an empty list otherwise.

A worthy memory is a repeatable preference or rule the correction implies, not a
one-off fix. Tag each with a type:
- project — specific to THIS project: its conventions, structure, or constraints.
- cross_project_style — how this user likes code written, in general.

State each as one terse line, phrased to apply next time — not a description of the
diff. Skip anything trivial, situational, already obvious, or already in the rules.
