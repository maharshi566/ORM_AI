# The agent graph

Drawn from the code by `python -m scripts.draw_graph --write`; do not edit by hand.
Solid arrows always happen; dotted arrows are decisions (conditional edges). The
supervisor is the hub that every specialist reports back to. See
[agents.md](agents.md) for what each node does.

```mermaid
---
config:
  flowchart:
    curve: linear
---
graph TD;
	__start__([<p>__start__</p>]):::first
	triage(triage)
	supervisor(supervisor)
	data_retrieval(data_retrieval)
	knowledge(knowledge)
	investigation(investigation)
	human_review(human_review)
	action(action)
	respond(respond)
	validate(validate)
	clarify(clarify)
	finalize(finalize)
	__end__([<p>__end__</p>]):::last
	__start__ --> triage;
	action --> respond;
	clarify --> finalize;
	data_retrieval --> supervisor;
	human_review -.-> action;
	human_review -.-> respond;
	investigation -.-> data_retrieval;
	investigation -.-> human_review;
	investigation -.-> respond;
	knowledge --> supervisor;
	respond --> validate;
	supervisor -.-> clarify;
	supervisor -.-> data_retrieval;
	supervisor -.-> finalize;
	supervisor -.-> investigation;
	supervisor -.-> knowledge;
	supervisor -.-> respond;
	triage --> supervisor;
	validate -.-> finalize;
	validate -.-> respond;
	finalize --> __end__;
	classDef default fill:#f2f0ff,line-height:1.2
	classDef first fill-opacity:0
	classDef last fill:#bfb6fc
```
