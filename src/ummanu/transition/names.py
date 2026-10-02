"""Every old and every new name the transition touches, written out once (`docs/RENAME.md` §T3).

This is the transition's only source of names. The rename card rewrites the product's own constants
(`secret_store.VERIFIER_AAD`, `board.store`'s env keys, `runtime_preflight.PACKAGE`, …) and the
transition runs from the renamed tree, so a constant imported from the product would already mean
the *new* name when the transition needs the old one. The rename card skips this file (class T in
§T5); both columns stay literal here and nowhere else in the transition.

Paths are kept as names relative to their root (the runtime home, `/opt`, the units directory), so
a test can lay a fixture installation out under a temporary home and the live run gets the real one.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Names:
    """One column of the table: everything that carries the product name on this installation."""

    #: Python package, console script and `python -m` module.
    package: str
    #: Production checkout `~/<product_dir>` and data plane `~/<data_dir>`.
    product_dir: str
    data_dir: str
    #: Adapter tool cache `~/<tools_dir>` (uv, poetry for other projects).
    tools_dir: str
    #: `/opt/<opt_dir>` (board-store Compose file, Orca AppImage).
    opt_dir: str
    #: Role worktrees `~/orca/workspaces/<role_workspaces>/<role>`.
    role_workspaces: str
    #: systemd unit prefix and the environment variable prefix.
    unit_prefix: str
    env_prefix: str
    #: Board store: database, the three login roles, Compose project.
    db_name: str
    db_owner: str
    db_app: str
    db_read: str
    compose_project: str
    #: Board identity rows.
    product_id: str
    product_title: str
    project_id: str
    remote: str
    #: Dispatcher owner written into `production-state.json`.
    dispatcher_owner: str
    #: `instance.yaml` `name`.
    instance_name: str
    #: Secret store: key file format, verifier plaintext and AAD, envelope format, value KDF info.
    key_params_format: str
    verifier_plaintext: bytes
    verifier_aad: bytes
    envelope_format: str
    value_kdf_info: str
    #: Instance memory: project fact scope directory and the product pack/scope name.
    memory_scope_dir: str
    product_memory: str

    @property
    def board_store_env(self) -> tuple[str, ...]:
        """The nine `board-store.env` keys, in the order the store writes them."""
        return tuple(
            f"{self.env_prefix}DB_{suffix}"
            for suffix in (
                "HOST", "PORT", "NAME",
                "OWNER_USER", "OWNER_PASSWORD",
                "APP_USER", "APP_PASSWORD",
                "READ_USER", "READ_PASSWORD",
            )
        )


OLD = Names(
    package="secretary",
    product_dir="secretary",
    data_dir="secretary-data",
    tools_dir=".secretary-tools",
    opt_dir="secretary",
    role_workspaces="secretary",
    unit_prefix="secretary-",
    env_prefix="SECRETARY_",
    db_name="secretary",
    db_owner="secretary_owner",
    db_app="secretary_app",
    db_read="secretary_read",
    compose_project="secretary-board-store",
    product_id="secretary",
    product_title="Secretary",
    project_id="secretary",
    remote="https://github.com/vladmesh/secretary.git",
    dispatcher_owner="secretary-production",
    instance_name="vladmesh-secretary",
    key_params_format="secretary.installation-key",
    verifier_plaintext=b"secretary installation key v1",
    verifier_aad=b"secretary/installation-key/v1",
    envelope_format="secretary.secret-envelope",
    value_kdf_info="secretary/secret/v1",
    memory_scope_dir="secretary",
    product_memory="product-secretary",
)

NEW = Names(
    package="ummanu",
    product_dir="ummanu",
    data_dir="ummanu-data",
    tools_dir=".ummanu-tools",
    opt_dir="ummanu",
    role_workspaces="ummanu",
    unit_prefix="ummanu-",
    env_prefix="UMMANU_",
    db_name="ummanu",
    db_owner="ummanu_owner",
    db_app="ummanu_app",
    db_read="ummanu_read",
    compose_project="ummanu-board-store",
    product_id="ummanu",
    product_title="Ummanu",
    project_id="ummanu",
    remote="https://github.com/vladmesh/ummanu.git",
    dispatcher_owner="ummanu-production",
    instance_name="vladmesh-ummanu",
    key_params_format="ummanu.installation-key",
    verifier_plaintext=b"ummanu installation key v1",
    verifier_aad=b"ummanu/installation-key/v1",
    envelope_format="ummanu.secret-envelope",
    value_kdf_info="ummanu/secret/v1",
    memory_scope_dir="ummanu",
    product_memory="product-ummanu",
)

#: The instance repository keeps its name (class I): path, remote and project id `secretary-instance`.
INSTANCE_PROJECT = "secretary-instance"

#: `runtime.env` keys no current code reads; dropped instead of renamed (§3).
DROPPED_RUNTIME_KEYS = ("SECRETARY_CARD_BACKEND",)

#: Role worktrees and their Claude project directories (§5, §T4). Card worktrees under the same
#: root (`…-secretary-secretary-N-*`) and the instance's (`…-secretary-instance-*`) are history.
ROLES = ("curator", "pipeline", "retro", "steward")

#: The sprint comment markers the observer posts before the transition and the one it posts after.
BASELINE_MARKER = "[transition:baseline]"
COUNTS_MARKER = "[transition:counts]"
DONE_MARKER = "[transition:done]"

#: The transition's own files under the runtime home: the journal and the rollback copies.
JOURNAL_NAME = "ummanu-transition.json"
LOCK_NAME = "ummanu-transition.lock"
STATE_DIR_NAME = "ummanu-transition"

#: Tables whose counts must equal the snapshot after the restore and the translation (§T2.7), and
#: the rows the translation itself adds.
UNCHANGED_TABLES = ("tasks", "issues", "sprints", "board_events")
ADDED_ROWS = {"products": 1, "projects": 1, "repositories": 1, "product_projects": 2}

__all__ = [
    "ADDED_ROWS",
    "BASELINE_MARKER",
    "COUNTS_MARKER",
    "DONE_MARKER",
    "DROPPED_RUNTIME_KEYS",
    "INSTANCE_PROJECT",
    "JOURNAL_NAME",
    "LOCK_NAME",
    "NEW",
    "OLD",
    "ROLES",
    "STATE_DIR_NAME",
    "UNCHANGED_TABLES",
    "Names",
]
