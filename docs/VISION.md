# Vision

`secretary` turns a remote VPS into a personal command centre for working with several AI agents
across many projects. The owner sets goals, architectural decisions and quality bars. The system
keeps context, picks executors, launches work, organises review and recovers after losing the
machine.

## Who it is for

The first user profile is someone who:

- works mainly on a remote VPS;
- runs many projects in parallel;
- uses subscriptions and models from several providers;
- wants to keep control of product and architecture while agents do much of the implementation;
- wants to watch agents and step in without hand-operating an orchestrator.

The deployment profile is one trusted owner on one machine. That must not turn into hard-coding a
specific user, host, account or directory into product code.

## Target experience

A fresh VPS becomes a working appliance after a short bootstrap and install flow: board, session
manager, memory, dispatcher, background roles, schedules and observability. Agent heads and their
provider logins are connected separately.

The private installation Git repository is the durable recovery checkpoint, and installation
credentials are recoverable with it from a single recovery phrase. Moving to a new machine should need
only the product, access to that repository, the recovery phrase, and the credentials the product
deliberately does not keep. The runtime is rebuilt from portable state, not copied from host-local
debris.

## Several heads

A head is not a model provider. A head profile binds an agent runtime (Codex, Claude Code, Hermes),
an account pool, a model, launch parameters and roles.

Routing stays deterministic. A task carries an abstract capability level; policy picks family,
profile, account, model and effort from availability, limits and a preference for independent
re-checking. The owner can override routing explicitly, and every round records both the requested
level and the resolved decision.

Cross-family review is a preference until its value is measured by problems found, fix cycles, later
regressions, elapsed time and quota spend. When one family is exhausted, work degrades to the family
that is still available instead of stopping.

## The sprint as the unit of work

A Product groups the projects that deliver one product. Prioritised issues say why work matters; they
feed sprints and are not pre-sliced implementation. A sprint belongs to one Product, takes issues,
holds a goal and a Definition of Done, and reserves the projects it may change. Tasks are cut just in
time inside a sprint and are process records; issues stay open until the owner closes them after
checking product invariants.

An open sprint is run by a dedicated observer head, not by a person in a chat window. The observer
chooses tactics, task boundaries and routing levels inside the sprint contract. It cannot silently
change the Definition of Done or make a material product choice; for those it records a decision
request and waits for the owner.

Sprint state is stored where the cards are, because an agent's self-report is least reliable exactly
when the truth matters most. The links between tasks, sprint, issues, decisions and events must be
readable without the observer's memory or transcript.

Independent review reports what it finds and is never weakened to make work converge. The sprint
controller decides how to use that evidence: it can reslice a failed approach or accept a mechanically
green, architecturally sound increment with follow-up issues, but it cannot run fix rounds forever.

How this works today is in [Protocols](PROTOCOLS.md#sprints).

## Product principles

- Every new feature reduces the number of installation-specific assumptions.
- An opinionated default beats early support for many backends.
- Replaceable parts are separated by protocols; a public plugin API appears only after a real need
  for a second implementation.
- `secretary task` owns the normalised task model, transitions, audit and portable export, whatever
  backend holds live state.
- Product intent and execution are different planes: issues are durable and prioritised, tasks are cut
  just in time inside a sprint.
- The head runtime (`local-pty`) provides managed PTY sessions, streamed output, input, state,
  process-tree termination and recovery. A pretty live UI is a frontend capability.
- LLMs do and review the work. Routing, lifecycle, recovery and ownership are ordinary checkable
  protocols.
- The owner keeps product authority. The secretary is the interactive PO interface; an observer is an
  autonomous sprint controller with bounded authority, not a substitute product owner.
- Observability and recovery are part of the main user path.

## Delivery and direction

The appliance ships its board and head runtime out of the box. Their internals must not leak across
the product, so replacing either stays a decision that can be taken later. Heads remain the owner's
choice and are connected independently.

The project is developed as open source, with measurable results and without a hosted SaaS.

## Not now

No team platform, multi-tenant SaaS, general-purpose terminal multiplexer, public plugin ecosystem or
automatic storage of every provider credential. Telegram, voice input and moving configuration into a
database wait until the main install and recovery path is automated.