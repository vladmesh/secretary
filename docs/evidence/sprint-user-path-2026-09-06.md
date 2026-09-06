# The sprint form as a path: what was walked on 2026-09-06, and what was not

Sprint 1428 gave this product a way to open a sprint from a browser: `GET /sprints/new`,
`POST /sprints`, `GET /sprints/{ref}`. This file is the acceptance record for that path, taken on
2026-09-06 from the branch of `secretary-1571`. It has two halves, and they prove different things.

**Half one — the path, walked over a socket on an isolated installation.** `tests/sprint_user_path.py`
builds a temporary instance, a temporary data plane and an installed head registry, puts the
repository's in-process board fake where a Kanboard would be, binds the real transport
(`secretary.web.server.build_server`) to a loopback port, and drives it with `http.client`. Every
request id it sends came out of the markup of the page before it, exactly as a browser's would. It
is run in CI by `tests/test_sprint_user_path.py`, and reproduced with:

```bash
python3 -P -m tests.sprint_user_path            # the transcript below
```

**Half two — the installed web, read only.** Everything that can be checked on the running
installation without publishing anything: the routes are behind the password, the pages that existed
before are alive, and the running transport is provably older than the merged code.

**Neither half is a live end-to-end.** The published web still runs the code its process started
with, so the form below has never been served by it. What that leaves unproven is listed at the end.

## Half one: the transcript

```
The sprint form, walked as a path over a real socket.
walked at 2026-09-06T15:36:43Z by tests/sprint_user_path.py, on an isolated installation: temporary instance,
temporary data plane, the repository's in-process board fake. No live installation, no Kanboard, no restart.

1. The owner opens “New sprint”
   → GET /sprints/new
   ← HTTP 200
   ✓ the form is served
   ✓ it offers this installation's own observer codex-observer
   ✓ it offers the open issue issue:open
   ✓ it offers the registered project secretary
   ✓ the observer select does not offer “none”
   ✓ worker and reviewer default to “the observer chooses”
   ✓ the page carries the request id web-sprint-0d5532a0-3173-48a3-b256-23c5ac7062bf the submission will use
2. The owner fills it in and presses “Start this sprint”
   → POST /sprints
   ← HTTP 303
   ✓ the submission is accepted and redirects to the sprint
   ✓ it lands on /sprints/sprint%3A1
   ✓ exactly one sprint row exists on the board
   ✓ the row is sprint:1
   ✓ no executor was pinned, so the row carries neither sprint_worker nor sprint_reviewer
3. The page it lands on
   → GET /sprints/sprint%3A1
   ← HTTP 200
   ✓ the sprint page is served
   ✓ it shows the goal that was typed
   ✓ it shows the definition of done that was typed
   ✓ the launch state reads “saved — no observer is up for it yet”
   ✓ it names the observer codex-observer
4. The owner submits the same form again (a double click, a retry)
   → POST /sprints
   ← HTTP 303
   ✓ it is accepted rather than refused
   ✓ it lands on the sprint that already exists
   ✓ the board still holds exactly one sprint
5. A sprint reference the board does not hold
   → GET /sprints/sprint:9999
   ← HTTP 404
   ✓ it is a 404 page rather than an empty one
   ✓ the page says what was not found
6. A verb no route publishes
   → DELETE /sprints
   ← HTTP 501
   ✓ it reaches no handler and writes nothing (the standard-library adapter answers 501 where the application's own table answers 405 — a deferred finding, not a defect of the path)
   ✓ the board is unchanged
7. The dashboard the new routes were added beside
   → GET /
   ← HTTP 200
   ✓ it is still served by the same application
   ✓ and it offers the form
8. Isolation, checked when the server is down
   → (no request: the fixture is inspected)
   ← HTTP 0
   ✓ all four layers read and wrote inside /tmp/tmpaldje62b only
   ✓ the board was the repository's in-process fake, so no Kanboard was reached
   ✓ sprints this scenario wrote, all on the fake's Secretary sprints board: ['sprint:1']
9. A sprint opened with both executors chosen explicitly
   → POST /sprints
   ← HTTP 303
   ✓ the submission is accepted
   ✓ one sprint row exists: sprint:1
   ✓ its row pins sprint_worker=claude-worker
   ✓ its row pins sprint_reviewer=codex-reviewer
10. Its page
   → GET /sprints/sprint%3A1
   ← HTTP 200
   ✓ the sprint page is served
   ✓ it shows the goal that was typed
   ✓ it shows both pinned heads
11. Isolation, checked when the server is down
   → (no request: the fixture is inspected)
   ← HTTP 0
   ✓ all four layers read and wrote inside /tmp/tmpigw1wz2u only
   ✓ the board was the repository's in-process fake, so no Kanboard was reached
   ✓ sprints this scenario wrote, all on the fake's Secretary sprints board: ['sprint:1']
12. A submission naming a profile this installation does not have
   → POST /sprints
   ← HTTP 400
   ✓ it is refused
   ✓ the refusal names the profile that is gone
   ✓ it comes back as a form, not as a blank error
   ✓ everything typed is still on it
   ✓ no sprint was opened
   ✓ the spent request id is replaced
13. The corrected form, submitted as it came back
   → POST /sprints
   ← HTTP 303
   ✓ it is accepted
   ✓ it opens exactly one sprint
   ✓ and lands on /sprints/sprint%3A1
14. Isolation, checked when the server is down
   → (no request: the fixture is inspected)
   ← HTTP 0
   ✓ all four layers read and wrote inside /tmp/tmpgth38cqv only
   ✓ the board was the repository's in-process fake, so no Kanboard was reached
   ✓ sprints this scenario wrote, all on the fake's Secretary sprints board: ['sprint:1']
15. A submission that fails after the sprint row was written
   → POST /sprints (the request index is made to fail)
   ← HTTP 503
   ✓ the answer is the backend being unavailable, not a refusal
   ✓ the page states the durable fact first: the sprint exists and the request did not finish
   ✓ it says submitting the same form again is safe
   ✓ the request id is kept, because only that id reaches the sprint
   ✓ one sprint row was written
16. The same submission again, as the page says to
   → POST /sprints
   ← HTTP 303
   ✓ it completes rather than refusing
   ✓ it opens no second sprint
   ✓ and lands on /sprints/sprint%3A1
17. Isolation, checked when the server is down
   → (no request: the fixture is inspected)
   ← HTTP 0
   ✓ all four layers read and wrote inside /tmp/tmppo0jj1gp only
   ✓ the board was the repository's in-process fake, so no Kanboard was reached
   ✓ sprints this scenario wrote, all on the fake's Secretary sprints board: ['sprint:1']

17 steps, 62 checks held, 0 did not
```

## Half two: the installed web, read only

Nothing below writes: every probe is a GET except the one `POST /sprints`, which the password
refuses before it reaches the transport, and the guard check, which reads a file. No service was restarted, no `upgrade` and
no `reconcile adopt` was run, and no sprint was created anywhere.

```
checked at 2026-09-06T15:54:15Z
$ git -C ~/secretary rev-parse --short HEAD
2235518
$ git -C ~/secretary reflog show --date=iso -1 HEAD
2235518 HEAD@{2026-09-06 15:24:17 +0000}: merge origin/main: Fast-forward
$ systemctl show -p ExecMainStartTimestamp secretary-web.service
ExecMainStartTimestamp=Sun 2026-09-06 06:45:20 UTC
$ systemctl is-active secretary-web.service secretary-web-front.service
active
active
--- loopback transport (read-only GETs)
/ -> 200
/tasks/secretary-1571 -> 200
/api/system -> 200
/sprints/new -> 404
--- the published front, unauthenticated
GET / -> 401
GET /tasks/secretary-1571 -> 401
GET /api/system -> 401
GET /sprints/new -> 401
GET /sprints/sprint:1428 -> 401
POST /sprints -> 401
http://.../sprints/new -> 308 https://5uoc.l.time4vps.cloud/sprints/new
--- unguarded_routes over the live Caddyfile, with this branch ROUTES
routes in the table: 12
unguarded: () — none
upstreams: ('127.0.0.1:8787',)
```

Read together, those say four things:

- **The published surface is guarded.** `unguarded_routes` — the check that reads the live Caddyfile
  and asks it about every entry of `secretary.web.app.ROUTES` rather than about a hand-written list
  — names none of the twelve routes, the three sprint routes among them, and the only upstream is
  `127.0.0.1:8787`. Unauthenticated requests to all of them answer `401`, including `POST /sprints`,
  and plain `http://` is a `308` to `https://`.
- **The pages that existed before are alive.** The dashboard and a card page answer `200` on the
  loopback transport and `401` (rather than `404` or `502`) through the front.
- **The installed web cannot be showing the new form, and says so itself.** `/sprints/new` is `404`
  on the running transport while `~/secretary/` is at `2235518`, which contains all three merged
  cards.
- **Why, as a comparison of two times rather than of a time and a revision:** the checkout last
  moved at `2026-09-06 15:24:17 +0000` (its reflog) and the transport process started at
  `2026-09-06 06:45:20 UTC` — nearly nine hours *earlier*. A process serves the checkout it started
  with, so this one cannot be serving the merged code, and the `404` above is that fact observed
  rather than inferred.

## What is NOT proved here

- **The live end-to-end was not run.** Nobody has opened a sprint through the installed web. Doing so
  needs `sudo systemctl restart secretary-web.service`, which is the owner's step; the sequence is
  *Finishing the sprint-form acceptance* in `docs/OPERATIONS.md`.
- **The live Kanboard's behaviour on the new keys is unverified.** `sprint_worker` and
  `sprint_reviewer` were written and read back on the in-process fake only.
- **So is the readability of a fresh row.** That a sprint row can be read back the instant it is
  written — what the redirect to `/sprints/{ref}` depends on — is a property of the fake here.
- **So is the board's behaviour under the mutations.** The interrupted-create scenario injects its
  failure into this product's own request index, not into a Kanboard, and the resumption it
  demonstrates is `SprintWriter`'s staged transaction against the fake.
- **Nothing here observes a real observer head being raised.** Every sprint the walk opens reads
  `saved — no observer is up for it yet`, which is what a create answers before the production tick
  reaches it. No tick ran.
