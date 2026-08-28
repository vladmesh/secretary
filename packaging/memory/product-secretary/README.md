# Secretary product memory pack

This is the shipped source for the `product:secretary` memory scope. Install and
`secretary upgrade` validate this manifest and every declared file digest before
writing the private instance canon at `state/memory/facts/product-secretary`.

`state/memory/packs/product-secretary.json` is the installed ownership ledger.
It records the pack digest and the facts owned by this pack. A complete manifest
removes only formerly owned shipped facts. Local facts may overlay the same scope
under different ids; a local fact using a shipped id is rejected rather than
overwritten.

Reconciliation records `pending` until the derived export has been published and
handed to the memory service user, then records `ready`. A failed handoff is
therefore retried by the next `secretary upgrade --no-pull` instead of being
mistaken for an installed pack.

The instance canon is the source of truth. Its derived export feeds the existing
incremental memory index: unchanged shipped ids retain their content digest and
embedding, while added, changed, and removed facts reconcile incrementally.
