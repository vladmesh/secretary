---
id: operational-boundaries
title: Safe operating boundaries
---

Treat the card, sprint, and their durable audit as the operational record. Read the current state before acting, use the supported `secretary task` and `secretary sprint` interface, and make retries idempotent with the request identity issued for the operation.

Keep task descriptions, comments, reports, and review evidence free of credentials, access tokens, recovery material, and other secrets. Do not copy private installation details into a card merely to make a task easier to explain.

Stay inside the assigned project, card, and workspace. Do not widen scope, rewrite shared history, force-push, alter access configuration, or make external production changes unless the card explicitly authorizes that action and the responsible role permits it.

When state, authority, evidence, or safety is uncertain, preserve the evidence and report or escalate the uncertainty. A clear Blocked state is safer than an unrecorded workaround.
