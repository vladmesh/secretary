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

Target roots are written home-relative (`~/...`) and expand against the home of whoever runs the
sync, because the product does not know which account installs it. Sources do not move with the
home: a skill is always read from the `roles/` tree beside the manifest that declared it.

`--product-root <checkout>` reads the manifest of another checkout instead of the one this command
was installed from. `secretary upgrade --product-root` passes it for you, so an upgrade delivers the
skills of the version it is installing.

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

A role name and a skill name are directory names: one path component each, matching
`[A-Za-z0-9][A-Za-z0-9._-]*`. Both halves of the registry join them onto a root, one to read a skill
and one to write it, so a name carrying a separator would move the write somewhere nobody named. A
name outside that shape is refused rather than interpreted.

Skill directories are flat under a shell root, so a product skill and an installation skill of the
same name would claim the same directory. That pair is refused before any copy, naming both
manifests: burying one of them is not what either manifest asked for. Rename the installation skill,
or give it a target of its own.

A manifest that is not TOML, or that has a key of the wrong shape, fails the audit and the sync with
one line naming the file to open. Sync decides everything it can before it writes, so a rejected
registry leaves nothing half delivered.

## Command entry points

A skill may ship one command the operator runs by name: an executable `<skill>.sh` beside its
`SKILL.md`. `sync` links `~/bin/<skill>` (`SECRETARY_BIN_DIR` overrides the directory) at that
script and makes the script executable. Nothing declares it in a manifest, so a skill carries its
command with it when it moves between the product and an installation, and the documented entry
point survives the move without anyone editing a link.

Linking rather than copying is what makes the repair possible: a link into one of the `roles/` trees
this registry reads is repointed at the current source, which covers a skill that changed role and
one that moved between the product and an installation. Whether that old target still exists does
not enter into it; the tree it names is the evidence. Sync is idempotent: an entry point that is
already right is not touched, so the command never disappears from `PATH` mid-sync. It refuses,
before writing anything, to replace a real file or any link outside those trees, including a
dangling one whose path merely has the shape of a skill source.
Two skills of the same name would want the same link; that is refused too, naming both manifests.
`audit` reports an entry point that is missing, stale or blocked, with the manifest that owns it.
