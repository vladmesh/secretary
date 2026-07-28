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
- is stronger at product and architecture than at writing and reading code by hand;
- wants to watch agents and intervene without hand-operating an orchestrator.

Early releases support one trusted owner on one machine. That is a deployment profile, not a licence
to hard-code a specific user, host, account or directory into product code.

## Product promise

On a fresh VPS the owner runs one or two commands and gets a working appliance: board, session
manager, memory, dispatcher, background roles, schedules and observability. On a from-scratch
install, credentials and `.env` are filled in by hand once; agent heads are connected after
bootstrap.

The private installation Git repository is the durable recovery checkpoint. Installation credentials
are recoverable with it: the canonical values live in an encrypted secret store and are rebuilt from
a single recovery phrase, without retyping them. Moving to a new machine should require only
installing the product, access to that repository, the store's recovery phrase, and re-entering the
credentials the product deliberately does not keep, which today means the first-time install entry
and the agent-head logins. The active runtime is rebuilt from portable state rather than restored by
copying host-local debris.

## Several heads

A head is not a model provider. Codex, Claude Code, Gemini CLI and Hermes are agent runtimes; each
runtime can use the accounts, subscriptions, API keys and models available to it. A head profile
binds a runtime, an account pool, a model, launch parameters and roles.

Routing stays deterministic. A card states the capability it needs, and policy picks the profile,
account and model given availability, limits and a preference for independent re-checking. The owner
or an operator can override the choice explicitly. The actual decision and its reason are recorded in
the audit log.

The value of reviewing with a different model family has to be measured by problems found, fix
cycles, later regressions, elapsed time and quota spend. Until that data exists, diversity is a
preference, not a quality guarantee.

## The sprint as the unit of work

A sprint holds a goal and a Definition of Done. A card can live inside a sprint, but standalone cards
remain valid. An open sprint is run by a dedicated observer head that the production dispatcher
launches, not by a person in a chat window. You talk to a running sprint through entries on its
entity, and read its status from board data.

The entity follows from a product principle: sprint state is stored where the cards are, because a
working agent's self-report is least reliable exactly when the truth matters most. The link between
cards and their sprint, and the events of the work, must be readable independently of the observer's
memory or transcript.

## Product principles

- Every new feature reduces the number of installation-specific assumptions.
- An opinionated default beats early support for many backends.
- Replaceable parts are separated by protocols, but a public plugin API appears only after a real
  need for a second implementation.
- The board backend holds live task state; `secretary task` owns the normalised model, transitions,
  audit and the portable export.
- The session manager provides managed PTY sessions, streamed output, input, state, process-tree
  termination and recovery. A pretty live UI is a frontend capability.
- LLMs do and review the work. Routing, lifecycle, recovery and ownership are ordinary checkable
  protocols.
- Observability and recovery are part of the main user path.

## Delivery and direction

The first appliance ships Kanboard and Orca out of the box. Their internals must not leak across the
product, so that replacing either stays a decision that can be taken later. Heads remain the owner's
choice and are connected independently.

The intended distribution model is open source: a public repository and measurable results, without a
hosted SaaS.

## Not now

Near-term releases do not build a team platform, a multi-tenant SaaS, an in-house Orca replacement, a
public plugin ecosystem or automatic storage of every provider credential. Telegram, voice input, a
first-party control-plane UI and moving configuration into a database stay on the list for after the
main install and recovery path is automated.
