# Head scope ownership and memory exits

Every profile-backed observer, worker, reviewer and PO turn uses `ScopedHeadLifecycle`.
`memory_limit_mib` accepts an integer from 1 through 1048576, excluding booleans.
The default is 8192 MiB; shipped high profiles use 12288 MiB. The launch sets
`MemoryMax` to that many bytes and `MemorySwapMax=0`. Scope registration or limit
verification failure refuses the launch.

`spawn_head` writes and fsyncs `scope-owner.json` before starting a launcher. The
record holds the run ID, derived unit name, launch generation and admission gate.
The launcher starts a child behind a pipe barrier, writes its PID, boot ID and
process start ticks to the owner, then releases it to exec sudo/systemd-run.
Death before release closes the pipe and creates no scope work. Cleanup closes
admission under the same owner lock, terminates the recorded launch group with
identity fences, stops the unit and reads recursive `cgroup.events` membership.
The same nonblocking owner lock remains held from generation verification through
backend termination and durable empty proof. Admission and replacement use that
lock too, so a delayed old stop cannot target a same-run replacement. Contention
refuses without changing ownership and can be retried. Recording the proof does
not reacquire the lock.
An absent cgroup after admission closes and launch work ends, or `populated 0`,
is the empty proof. A missing membership file in an existing cgroup is an error.
Unreadable state, a failed command or interrupted cleanup leaves the owner for
retry. A new generation cannot replace it until its empty proof is durable;
late launchers and stale cleanup calls cannot act on the replacement generation.

`run.exited` says the head exited. It does not say the scope is empty. Runtime
stop, forgetting a scoped head, replacement spawn, PO settlement and PO retry
all go through this owner. Detached descendants remain in the cgroup even when
they create another process group. The dispatcher also checks settled HeadRuns
before removing their workspace: historical PID-based settlement is insufficient
for a scope owner.

Scoped HeadHandles and HeadRuns carry the launch generation, including retained
pre-start failures. Runtime stop consumes that matching owner's proof while its
lock is held. A failed launch that never published a heartbeat or journal can
therefore settle after cleanup succeeds. Missing head records alone cannot settle
a run; missing/malformed owners, generation mismatches and foreign/reused process
identities still refuse. Genuinely unscoped runs keep their identity-based check.

Product runs assign and save that generation in their write-ahead HeadRun before
calling start. The runtime and launcher preserve it, so a crash before saving the
returned handle still leaves a record that can stop the matching scope. A start
carrying a write-ahead generation cannot replace an existing scope owner. Product
recovery treats a scope owner as a launch trace even before any heartbeat or journal
exists; failed cleanup retains the unresolved run until matching proof succeeds.
Dispatcher observer/worker/reviewer intents use their newly allocated launch ID
as the generation, so both preflight calls preserve the original durable binding.
Provider/lifecycle handoff merges preserve it and refuse conflicting generations.
An ordinary runtime replacement that has no new write-ahead admission still gets
a fresh generation after proof of the previous owner's empty scope.

PO turns keep a symlink to the canonical supervisor run directory, not another
copy of the owner. Failed launch, failed waiter start, completion and owner stop
all reach the same terminal operation. It verifies the row is still running,
records/selects terminal intent, proves empty, and commits the selected outcome
to the store with the owner lock held throughout. Owner interruption overrides
uncommitted completion or failure. A stop after committed completion sees no
running turn and changes nothing. Recovery consumes that same intent and operation,
retaining a successful answer or interruption rather than rerunning the input.
The store's conditional running-row transition makes feed publication and failure
callbacks idempotent. Waiter exit retains its existing wake callback for bounded
recovery even when cleanup fails. The service schedules its existing bounded recovery
backoff after newly orphaned claims and waiter completion. A pending owner stop
is revisited even while its waiter remains blocked. Running rows fence their
session's next input and restart admission; other sessions can continue.

## Provider identity and scope ownership

`runtime.head_run_binding.head_run_binding` owns the provider/continuation digest.
Codex v1 fixes the input to `run_id`, the original workspace spelling, complete
serialized `task_ref`, role, and seven spec fields: `profile_id`, `adapter`,
`model`, `effort`, `resource`, `codex_mode`, `fallback`. The encoding is ASCII
JSON with sorted keys and compact separators, SHA-256 truncated to 32 hex digits.
The source descriptor separately carries the resolved workspace path. Addresses,
lifecycle, runtime, memory limit and scope generation do not enter this digest.
Full spec equality and scope generation remain fences in launch handoff; scope
admission and cleanup verify their canonical owner under its lock.

Preflight's `codex_provider_source_descriptor` calls the runtime implementation;
provider-event ingress verifies against that descriptor. The dispatch
`worker_lifecycle.head_run_binding` import exposes that same implementation to
provider cursor reads, retained-continuation liveness, observer/reviewer progress
and recovery. The provider-error reader shares the cursor reader's
`_codex_bound_source` verification of descriptor, journal root, session and initial
range. No reader discovers a replacement journal or changes a persisted digest.
Non-Codex sources retain their existing serialized-spec digest in the same runtime
implementation, preserving deployed Claude descriptors and continuation episodes.

Deployed Codex v1 descriptors retain their exact hash, including profile-derived
8192/12288 MiB runs. Old and new preflight producers therefore agree with the
corrected reader without migration. An old reader still rejects memory-bearing
Codex descriptors until it is refreshed. A persisted Codex continuation episode
carrying the divergent serialized-spec digest fails closed at episode admission;
it is never rewritten or tried as an alternate identity. Supported external runtime
refresh and settlement remain the observer's responsibility. Missing, malformed or
unsupported descriptors still refuse; this repair grants no recovery or rebind
capability.

## Causal OOM producer and consumer

The privileged scope bootstrap opens a read-only, nonblocking `/dev/kmsg` stream
and passes that descriptor to the trusted supervisor before dropping privileges.
Missing access refuses launch. The head child closes the descriptor, reserves
its new PID behind a barrier while still OOM-protected, and waits. The supervisor
seeks the stream to its end after fork, then releases the child to clear inherited
protection and exec. This excludes records for earlier uses of the PID or run.
The supervisor alone remains at `oom_score_adj=-1000`; `memory.oom.group=1`
includes the ordinary head, its children and threads in cgroup OOM termination.

On head exit the supervisor calls `waitid(WEXITED|WNOHANG|WNOWAIT)`, consumes
kernel records, then calls waitpid. It accepts only a kernel-facility
`Memory cgroup out of memory: Killed process <head PID>` record. Linux's
`__oom_kill_process` emits that record after sending SIGKILL while holding the
victim's task lock, before the victim can finish exit_mm. Thus it is available
before waitid reports completed exit. The zombie reserves the PID until reading
finishes. Observation can be delayed past a surviving child's subsequent OOM:
that kill record names the child, and cannot name this exited head. Operator
stop suppresses classification. Global OOM, victim-selection summaries,
oom_reaper messages, group counters and userspace kmsg records are insufficient.
Unreadable, overwritten or excessively large streams leave attribution unproved.
Positive evidence is journalled with the kernel sequence, boot timestamp and
head PID under this run's journal identity. The existing head-loss reader and
PO waiter consume `memory_limit` through normal recovery. A new `run.started`
ends the reader's search for an earlier incarnation's exit reason.

Producer sources: [Linux v6.8 oom_kill.c](https://github.com/torvalds/linux/blob/v6.8/mm/oom_kill.c)
(`__oom_kill_process` and `oom_kill_memcg_member`),
[memcontrol.c](https://github.com/torvalds/linux/blob/v6.8/mm/memcontrol.c)
(`mem_cgroup_scan_tasks` uses process iteration), and
[cgroup v2 membership](https://www.kernel.org/doc/html/latest/admin-guide/cgroup-v2.html).
The CI tiny-scope test proves a real head kill with children and threads. Required
backend cases also freeze the supervisor until an unrelated head SIGKILL and
subsequent child group OOM complete, exercise detached descendant stop/retry,
and exercise newly orphaned PO cleanup/recovery. Missing backend prerequisites
fail those CI cases; local broad checks exclude those integration cases.

## Existing state and upgrade boundary

Deployed main produced unscoped HeadRuns and PO process identities. Its readers
and stop paths remain for those genuine records. The three predecessor memory
candidates were never deployed, so their scope-owner formats are not supported
upgrade inputs; there is no fallback from a malformed scoped owner to PID-only
success. Supported PO upgrade requests idle restart, and the old service drains
its running turns before exiting. Systemd's service cgroup teardown covers its
descendants; the new service recovers persisted rows and new turns gain scopes.
Legacy dispatcher runs must drain/stop through their recorded runtime and identity
fences before replacement, under the installation's supported upgrade procedure.
An unscoped historical exit has no causal OOM evidence and gains no invented
`memory_limit` reason. This code does not retroactively manufacture scopes or
claim recursive scope proof for a legacy process-group stop. Installed drain,
limits and recovery evidence remain a separate authorized PO operation.
