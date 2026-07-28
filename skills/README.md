# Role skills

The canon of skills is stored by role, not by shell:

- `skills/roles/secretary/*`
- `skills/roles/curator/*`
- `skills/roles/observer/*`
- `skills/roles/retro/*`
- `skills/roles/steward/*`

Shells receive copies of the canon. Symlinks are not used here: some runtimes mirror the home directory
and can lose the link or resolve it in a different context. Copies are easier to verify by hash.

Check:

```bash
secretary role-skills audit --json
```

Synchronise:

```bash
secretary role-skills sync
```

`skills/manifest.toml` declares the roles, their skills and the target directories. If a role needs a
skill, add it under `skills/roles/<role>/` and to the manifest. Do not duplicate it by hand across
shells. Several target groups for one shell may share a root, but not nested roots: recursive skill
discovery mixes the namespaces and produces wrong locators.

## The installation overlay

An installation may own skills the product does not ship: a bridge to one operator's accounts, a
helper for one host. Those live in the private instance repository, not here. `role-skills` reads a
second manifest at `<instance>/skills/manifest.toml` and layers it over this one:

```bash
secretary role-skills audit --instance PATH
secretary role-skills sync --instance PATH
```

`--instance` takes the instance directory or the `instance.yaml` inside it, and defaults to
`SECRETARY_INSTANCE`. An installation with no overlay file is a complete installation; nothing warns
about it.

The overlay adds to a role, it does not replace one: a product skill stays delivered when an
installation puts its own skills in the same role, so an upgrade that ships a new skill still reaches
the shells. A target of the same name is replaced whole, because a target is one shell root and
merging two of them means nothing.

Every skill is read from the `roles/` tree beside the manifest that declared it, so the two
repositories never have to agree about where sources live. The audit tags each finding with its
origin (`product` or `instance`) and the manifest that owns it.

A manifest that is not TOML, or that has a key of the wrong shape, fails the audit and the sync with
one line naming the file to open. Sync decides everything it can before it writes, so a rejected
registry leaves nothing half delivered.

## Command entry points

A skill may ship an executable helper that the operator runs by name. The manifest that owns the
skill declares where it goes:

```toml
[commands.<name>]
role = "<role>"
skill = "<skill>"
source = "<file inside the skill directory>"
dest = "~/bin/<name>"
```

`sync` links `dest` at the helper beside the skill and makes the helper executable. Linking rather
than copying means the entry point follows the skill's source: it can be repointed after the skill
moves between repositories, which is the state a stale link from an earlier layout is left in. Sync
is idempotent, and it refuses — before writing anything — to replace a real file or a link this
registry does not own. `audit` reports an entry point that is missing, stale or blocked.
