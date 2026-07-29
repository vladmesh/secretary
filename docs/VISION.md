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
- wants to keep control of product and architecture while agents handle much of the implementation;
- wants to watch agents and intervene without hand-operating an orchestrator.

The current implementation supports one trusted owner on one machine. That is a deployment profile,
not a licence to hard-code a specific user, host, account or directory into product code.

## Target experience

The goal is for a fresh VPS to become a working appliance after a short bootstrap and install flow:
board, session manager, memory, dispatcher, background roles, schedules and observability. Today the
host bootstrap is supported on Ubuntu 24.04, while the clean-machine end-to-end gate and minimum host
requirements are still being established. Agent heads and their provider logins are connected
separately.

The private installation Git repository is the durable recovery checkpoint. Installation credentials
are recoverable with it: the canonical values live in an encrypted secret store and are rebuilt from
a single recovery phrase, without retyping them. Moving to a new machine should require only
installing the product, access to that repository, the store's recovery phrase, and re-entering the
credentials the product deliberately does not keep, which today means the first-time install entry
and the agent-head logins. The active runtime is rebuilt from portable state rather than restored by
copying host-local debris.

## Several heads

A head is not a model provider. Codex, Claude Code and Hermes are the agent runtimes represented by
the current adapters; each runtime can use the accounts, subscriptions, API keys and models available
to it. A head profile binds a runtime, an account pool, a model, launch parameters and roles.

Routing stays deterministic. An observer assigns each executable task an abstract capability level;
policy picks the family, profile, account, model and explicit effort given availability, limits and a
preference for independent re-checking. Concrete models and effort values remain configuration rather
than planning vocabulary. The owner or an operator can override the routing intent explicitly. Every
round records both the requested level and the resolved decision.

The value of reviewing with a different model family has to be measured by problems found, fix
cycles, later regressions, elapsed time and quota spend. Until that data exists, diversity is a
preference, not a quality guarantee. An exhausted family degrades work to the family that remains
available instead of stopping delivery merely because cross-family review is unavailable.

## The sprint as the unit of work

A Product groups the projects that together deliver one product. Durable prioritised issues describe
why work matters; they are fuel for a sprint, not pre-sliced implementation. A sprint belongs to one
Product, takes one or more issues, holds a goal and a Definition of Done, and reserves the projects it
may change. Its observer cuts executable tasks only when the next step is known. Tasks are process
records and disappear from the live board after sprint close; issues remain open until the owner,
through the secretary acting as the PO interface, explicitly closes them after checking product
invariants.

An open sprint is run by a dedicated observer head that the production dispatcher launches, not by a
person in a chat window. The observer chooses implementation tactics, task boundaries and routing
levels inside the sprint contract. It cannot silently change the Definition of Done or make a material
product choice. If the Definition of Done proves impossible or materially incomplete, the observer
records a durable decision request and waits for the owner through the secretary. You talk to a
running sprint through entries on its entity, and read its status from board data.

The entity follows from a product principle: sprint state is stored where the cards are, because a
working agent's self-report is least reliable exactly when the truth matters most. The link between
tasks and their sprint, its source issues, product decisions and the events of the work must be
readable independently of the observer's memory or transcript.

Independent review reports what it finds; it is not weakened to make work converge. The sprint
controller separately decides how to use that evidence. A bounded review cycle can reslice a failed
approach or accept a mechanically green, architecturally sound increment while preserving remaining
findings as prioritised issues. It cannot continue ordinary fix rounds indefinitely.

## Product principles

- Every new feature reduces the number of installation-specific assumptions.
- An opinionated default beats early support for many backends.
- Replaceable parts are separated by protocols, but a public plugin API appears only after a real
  need for a second implementation.
- The board backend holds live task state; `secretary task` owns the normalised model, transitions,
  audit and the portable export.
- Product intent and execution are different planes: issues are durable and prioritised, while tasks
  are cut just in time inside a sprint.
- The session manager provides managed PTY sessions, streamed output, input, state, process-tree
  termination and recovery. A pretty live UI is a frontend capability.
- LLMs do and review the work. Routing, lifecycle, recovery and ownership are ordinary checkable
  protocols.
- The owner retains product authority. The secretary is the interactive PO interface; an observer is
  an autonomous sprint controller with bounded authority, not a substitute product owner.
- Observability and recovery are part of the main user path.

## Delivery and direction

The first appliance ships Kanboard and Orca out of the box. Their internals must not leak across the
product, so that replacing either stays a decision that can be taken later. Heads remain the owner's
choice and are connected independently.

The project is developed as open source, with measurable results and without a hosted SaaS.

## Not now

Near-term releases do not build a team platform, a multi-tenant SaaS, an in-house Orca replacement, a
public plugin ecosystem or automatic storage of every provider credential. Telegram, voice input, a
first-party control-plane UI and moving configuration into a database stay on the list for after the
main install and recovery path is automated.
