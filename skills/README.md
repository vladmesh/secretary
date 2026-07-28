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
