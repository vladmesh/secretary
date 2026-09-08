"""The board store's connection file, its models and its Alembic scripts: everything without a server.

The resolver is a parse over a local file, the schema is a `MetaData` object and the script
directory is a directory. What genuinely needs a server — running the revision, counting what
PostgreSQL made of it, and asking Alembic whether the result still matches the models — is
`tests/test_board_store_schema.py`, which raises a throwaway `postgres:16` container.
"""

from __future__ import annotations

import os
import subprocess
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import secretary.board
from secretary import state_repo, upgrade
from secretary.board import migrate, provision, schema, store
from secretary.board.store import (
    ROLES,
    STORE_ENV,
    STORE_FILE,
    BoardStoreError,
    ensure_ignored,
    findings,
    materialize_fresh,
    resolve,
    resolve_role,
    resolve_with_lifecycle,
    store_path,
)

COMPLETE = {
    "SECRETARY_DB_HOST": "127.0.0.1",
    "SECRETARY_DB_PORT": "5432",
    "SECRETARY_DB_NAME": "secretary",
    "SECRETARY_DB_OWNER_USER": "secretary_owner",
    "SECRETARY_DB_OWNER_PASSWORD": "owner-secret",
    "SECRETARY_DB_APP_USER": "secretary_app",
    "SECRETARY_DB_APP_PASSWORD": "app-secret",
    "SECRETARY_DB_READ_USER": "secretary_read",
    "SECRETARY_DB_READ_PASSWORD": "read-secret",
}


def write_store(directory: Path, values: dict[str, str] | None = None, *, mode: int = 0o600) -> Path:
    path = store_path(directory)
    body = "".join(f"{key}={value}\n" for key, value in (COMPLETE if values is None else values).items())
    path.write_text(body, encoding="utf-8")
    path.chmod(mode)
    return path


class ConnectionFileTests(unittest.TestCase):
    """§5.4: nine keys, all required, all-or-nothing, and mode 0600."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = Path(self.tmp.name)

    def test_it_declares_exactly_the_nine_keys_of_the_document(self) -> None:
        self.assertEqual(
            STORE_ENV,
            (
                "SECRETARY_DB_HOST",
                "SECRETARY_DB_PORT",
                "SECRETARY_DB_NAME",
                "SECRETARY_DB_OWNER_USER",
                "SECRETARY_DB_OWNER_PASSWORD",
                "SECRETARY_DB_APP_USER",
                "SECRETARY_DB_APP_PASSWORD",
                "SECRETARY_DB_READ_USER",
                "SECRETARY_DB_READ_PASSWORD",
            ),
        )

    def test_a_complete_file_resolves_one_credential_per_role(self) -> None:
        write_store(self.instance)

        config = resolve(self.instance)

        self.assertEqual((config.host, config.port, config.dbname), ("127.0.0.1", 5432, "secretary"))
        self.assertEqual(
            [(role, resolve_role(self.instance, role).user) for role in ROLES],
            [("owner", "secretary_owner"), ("app", "secretary_app"), ("read", "secretary_read")],
        )
        self.assertNotEqual(
            resolve_role(self.instance, "app").password,
            resolve_role(self.instance, "read").password,
            "a single credential would make §5.5's three-role boundary unreachable",
        )

    def test_the_conninfo_escapes_a_password_that_carries_a_space_or_a_quote(self) -> None:
        values = dict(COMPLETE, SECRETARY_DB_APP_PASSWORD="a b'c\\d")
        write_store(self.instance, values)

        conninfo = resolve_role(self.instance, "app").conninfo()

        self.assertIn("password='a b\\'c\\\\d'", conninfo)
        self.assertIn("user='secretary_app'", conninfo)

    def test_a_missing_file_refuses_with_a_reason(self) -> None:
        with self.assertRaisesRegex(BoardStoreError, "board store configuration is missing"):
            resolve(self.instance)

    def test_a_partial_file_refuses_and_names_what_is_absent(self) -> None:
        for absent in STORE_ENV:
            with self.subTest(absent=absent):
                write_store(self.instance, {k: v for k, v in COMPLETE.items() if k != absent})
                with self.assertRaisesRegex(BoardStoreError, absent):
                    resolve(self.instance)

    def test_an_empty_value_is_a_partial_file_and_not_an_empty_password(self) -> None:
        write_store(self.instance, dict(COMPLETE, SECRETARY_DB_APP_PASSWORD=""))

        with self.assertRaisesRegex(BoardStoreError, "line 7 is invalid"):
            resolve(self.instance)

    def test_an_unknown_or_repeated_key_refuses_the_whole_file(self) -> None:
        path = store_path(self.instance)
        for body, line in (
            ("".join(f"{k}={v}\n" for k, v in COMPLETE.items()) + "SECRETARY_DB_EXTRA=x\n", 10),
            ("SECRETARY_DB_HOST=a\n" + "".join(f"{k}={v}\n" for k, v in COMPLETE.items()), 2),
        ):
            with self.subTest(line=line):
                path.write_text(body, encoding="utf-8")
                path.chmod(0o600)
                with self.assertRaisesRegex(BoardStoreError, f"line {line} is invalid"):
                    resolve(self.instance)

    def test_a_line_without_an_equals_sign_refuses(self) -> None:
        path = write_store(self.instance)
        path.write_text(path.read_text(encoding="utf-8") + "nonsense\n", encoding="utf-8")

        with self.assertRaisesRegex(BoardStoreError, "line 10 must use KEY=VALUE"):
            resolve(self.instance)

    def test_a_readable_by_others_file_refuses_until_it_is_chmodded(self) -> None:
        write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            resolve(self.instance)

    def test_a_symlink_is_refused_even_when_its_target_is_private(self) -> None:
        real = self.instance / "elsewhere.env"
        real.write_text("".join(f"{k}={v}\n" for k, v in COMPLETE.items()), encoding="utf-8")
        real.chmod(0o600)
        store_path(self.instance).symlink_to(real)

        with self.assertRaisesRegex(BoardStoreError, "regular file, not a symlink"):
            resolve(self.instance)

    def test_a_port_that_is_not_a_tcp_port_refuses(self) -> None:
        for port in ("0", "70000", "5432a"):
            with self.subTest(port=port):
                write_store(self.instance, dict(COMPLETE, SECRETARY_DB_PORT=port))
                with self.assertRaisesRegex(BoardStoreError, "not a TCP port"):
                    resolve(self.instance)

    def test_an_unknown_role_refuses_rather_than_falling_back_to_the_owner(self) -> None:
        write_store(self.instance)

        with self.assertRaisesRegex(BoardStoreError, "role must be one of"):
            resolve_role(self.instance, "admin")

    def test_a_checkout_with_no_store_and_no_ignore_entry_reports_nothing(self) -> None:
        """A pre-store installation is not an unhealthy one; only a lifecycle marker makes
        absence a finding, exactly as `board_transport.findings` decides it."""
        self.assertEqual(findings(self.instance), [])

    def test_findings_reports_a_broken_file_without_disclosing_it(self) -> None:
        write_store(self.instance, mode=0o644)

        reported = findings(self.instance)

        self.assertEqual(len(reported), 1)
        self.assertIn("permissions are too broad", reported[0])
        self.assertNotIn("owner-secret", reported[0])

    def test_resolving_never_writes_anything_into_the_installation(self) -> None:
        before = sorted(os.listdir(self.instance))

        with self.assertRaises(BoardStoreError):
            resolve(self.instance)
        findings(self.instance)

        self.assertEqual(sorted(os.listdir(self.instance)), before)

    def test_fresh_materialization_is_complete_private_and_never_rotates(self) -> None:
        subprocess.run(["git", "-C", str(self.instance), "init", "--quiet"], check=True)

        config = materialize_fresh(self.instance)
        path = store_path(self.instance)

        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(set(config.as_environ()), set(STORE_ENV))
        self.assertEqual(len({config.owner_password, config.app_password, config.read_password}), 3)
        self.assertTrue(state_repo.is_ignored(self.instance, f"/{STORE_FILE}"))
        before = path.read_bytes()
        with self.assertRaisesRegex(BoardStoreError, "rotation"):
            materialize_fresh(self.instance)
        self.assertEqual(path.read_bytes(), before)


class ProvisionDefinitionTests(unittest.TestCase):
    def test_compose_contract_is_pinned_loopback_and_persistent(self) -> None:
        self.assertIn("image: postgres:16", provision.COMPOSE_TEXT)
        self.assertIn("restart: unless-stopped", provision.COMPOSE_TEXT)
        self.assertIn("127.0.0.1:${SECRETARY_DB_PORT}:5432", provision.COMPOSE_TEXT)
        self.assertIn("board-db:/var/lib/postgresql/data", provision.COMPOSE_TEXT)
        self.assertNotIn("SECRETARY_DB_APP_PASSWORD", provision.COMPOSE_TEXT)
        self.assertNotIn("SECRETARY_DB_READ_PASSWORD", provision.COMPOSE_TEXT)

    def test_absent_config_is_an_upgrade_noop_before_touching_docker(self) -> None:
        with TemporaryDirectory() as temporary, mock.patch.object(provision, "_exists") as inspect:
            outcome = provision.provision(Path(temporary))

        self.assertIsNone(outcome)
        inspect.assert_not_called()

    def test_existing_volume_without_config_refuses_new_credentials(self) -> None:
        with (
            TemporaryDirectory() as temporary,
            mock.patch.object(provision, "_exists", return_value=True),
            mock.patch.object(store, "materialize_fresh") as materialize,
            self.assertRaisesRegex(BoardStoreError, "exists without board-store.env"),
        ):
            provision.provision(
                Path(temporary),
                allow_create=True,
                compose_path=Path(temporary) / "compose.yml",
            )

        materialize.assert_not_called()

    def test_compose_drift_is_refused_without_replacing_the_file(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            write_store(root)
            compose = root / "compose.yml"
            compose.write_text("services: {}\n", encoding="utf-8")
            compose.chmod(0o600)

            with self.assertRaisesRegex(BoardStoreError, "definition drift"):
                provision.provision(root, compose_path=compose)

            self.assertEqual(compose.read_text(encoding="utf-8"), "services: {}\n")

    def test_reconcile_passes_only_the_private_file_path_not_credentials_on_argv(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary)
            config_path = write_store(root)
            compose = root / "compose.yml"
            compose.write_text(provision.COMPOSE_TEXT, encoding="utf-8")
            compose.chmod(0o600)
            calls = []

            def command(arguments, **_kwargs):
                calls.append(arguments)
                if "inspect" in arguments:
                    return "[{}]"
                if "ps" in arguments:
                    return "container-id"
                return ""

            with (
                mock.patch.object(provision, "_run", side_effect=command),
                mock.patch.object(provision, "_inspect_container"),
                mock.patch.object(provision, "_wait_ready"),
            ):
                outcome = provision.provision(root, compose_path=compose)

        self.assertIsNotNone(outcome)
        arguments = " ".join(part for call in calls for part in call)
        self.assertIn(str(config_path), arguments)
        for secret in ("owner-secret", "app-secret", "read-secret"):
            self.assertNotIn(secret, arguments)


class SchemaModelTests(unittest.TestCase):
    """The models are the schema (§3), so what §3 constrains has to be *in* them.

    None of this needs a server: it reads `MetaData`. What needs one — running the revision and
    counting what PostgreSQL made of it — is `tests/test_board_store_schema.py`.
    """

    def test_it_declares_every_table_of_section_3_and_no_version_table(self) -> None:
        """23 tables; the 24th §3.13 counts is Alembic's own `alembic_version`.

        `product_comments` is the 23rd, added by `0004_product_issue_sql` so the SQL backend can
        serve the released Product comment vocabulary.
        """
        self.assertEqual(
            sorted(schema.metadata.tables),
            [
                "board_events",
                "issue_comments",
                "issues",
                "product_comments",
                "product_projects",
                "products",
                "projects",
                "repositories",
                "requests",
                "sprint_budget_events",
                "sprint_comments",
                "sprint_decisions",
                "sprint_issues",
                "sprint_projects",
                "sprint_repositories",
                "sprint_resumes",
                "sprints",
                "task_comments",
                "task_dependencies",
                "task_issues",
                "task_retry_heads",
                "task_supersessions",
                "tasks",
            ],
        )
        self.assertNotIn("schema_migrations", schema.metadata.tables)
        self.assertNotIn("alembic_version", schema.metadata.tables)

    def test_jsonb_is_exactly_the_seven_columns_section_3_10_names(self) -> None:
        """Seven since `0004` added lossless Product metadata as (J7)."""
        from sqlalchemy.dialects.postgresql import JSONB

        found = {
            (name, column.name)
            for name, table in schema.metadata.tables.items()
            for column in table.columns
            if isinstance(column.type, JSONB)
        }

        self.assertEqual(found, set(schema.JSONB_COLUMNS))

    def test_every_closed_vocabulary_is_a_check_constraint(self) -> None:
        """§3.12's rule: a closed vocabulary is a CHECK, never a reference table."""
        import sqlalchemy as sa

        checks = [
            str(constraint.sqltext)
            for table in schema.metadata.tables.values()
            for constraint in table.constraints
            if isinstance(constraint, sa.CheckConstraint)
        ]

        self.assertEqual(len(checks), 38, "§3.13 counts 38 CHECK constraints at the head revision")
        for vocabulary in (
            "state IN ('active','archived')",
            "priority IN ('P0','P1','P2','P3')",
            "task_type IN ('code','research')",
            "status IN ('staged','committed','discarded')",
        ):
            self.assertTrue(
                any(vocabulary in text for text in checks), f"{vocabulary} is not a CHECK anywhere"
            )

    def test_the_four_partial_unique_indexes_carry_their_predicate(self) -> None:
        partial = sorted(
            index.name
            for table in schema.metadata.tables.values()
            for index in table.indexes
            if index.unique and index.dialect_options["postgresql"]["where"] is not None
        )

        self.assertEqual(
            partial,
            [
                "repositories_one_primary",
                "sprint_decisions_one_per_card",
                "sprint_decisions_one_per_issue",
                "sprint_projects_one_live_reservation",
            ],
        )

    def test_the_generated_ref_columns_are_postgresql_generated_columns(self) -> None:
        """Four since `0004` added the Product comment request fence.

        `sprints.ref` and the three generated `sprint_ref` columns stopped being computed from
        `sprint_number`: the reference is now the stored identity, and it is the scoping column of
        every sprint child. `issue_comments.issue_ref` is the one new generated column, and it
        exists for the reason the sprint ones did — §3.9's claim key joins `requests.ref`, which
        spells an Issue `issue:<id>`.
        """
        for table, column, expression in (
            ("products", "ref", "'product:' || product_id"),
            ("issues", "ref", "'issue:' || issue_id"),
            ("issue_comments", "issue_ref", "'issue:' || issue_id"),
            ("product_comments", "product_ref", "'product:' || product_id"),
        ):
            with self.subTest(table=table):
                computed = schema.metadata.tables[table].columns[column].computed
                self.assertIsNotNone(computed)
                self.assertTrue(computed.persisted)
                self.assertEqual(str(computed.sqltext), expression)
        self.assertIsNone(
            schema.metadata.tables["sprints"].columns["ref"].computed,
            "the sprint's reference is stored, not derived: a reference with no number is a row",
        )

    def test_section_3_13_step_two_constraints_are_emitted_as_alter_table(self) -> None:
        """`use_alter` is what makes a forward or mutual reference expressible at all."""
        import sqlalchemy as sa

        altered = {
            constraint.name
            for table in schema.metadata.tables.values()
            for constraint in table.constraints
            if isinstance(constraint, sa.ForeignKeyConstraint) and constraint.use_alter
        }

        self.assertEqual(altered, set(schema.DEFERRED_CONSTRAINTS))

    def test_the_two_sprint_cursors_are_deferrable(self) -> None:
        import sqlalchemy as sa

        deferred = {
            constraint.name: constraint.initially
            for constraint in schema.metadata.tables["sprints"].constraints
            if isinstance(constraint, sa.ForeignKeyConstraint) and constraint.deferrable
        }

        self.assertEqual(
            deferred,
            {
                "sprint_current_task_is_in_this_sprint": "DEFERRED",
                "sprint_resume_is_of_this_sprint": "DEFERRED",
            },
        )


class MigrationScriptTests(unittest.TestCase):
    """Alembic's script directory as this product ships it — no server needed."""

    def test_the_tree_ships_exactly_the_revisions_this_build_expects(self) -> None:
        """Newest first, as `walk_revisions` returns them: each revision sits on the one before.

        The list grows by one whenever a revision ships, which is the point: a revision file
        added to the tree and not chained onto the head is exactly the mistake this catches.
        """
        revisions = [script.revision for script in migrate.script_directory().walk_revisions()]

        self.assertEqual(
            revisions,
            [
                "0006_sprint_transport_key",
                "0005_sprint_sql",
                "0004_product_issue_sql",
                "0003_task_type_optional",
                "0002_board_gaps",
                "0001_initial",
            ],
        )
        self.assertEqual(migrate.head_revision(), migrate.EXPECTED_SCHEMA_REVISION)

    def test_the_script_directory_ships_inside_the_installed_package(self) -> None:
        self.assertTrue((migrate.SCRIPT_LOCATION / "env.py").is_file())
        self.assertTrue((migrate.SCRIPT_LOCATION / "script.py.mako").is_file())
        self.assertEqual(migrate.SCRIPT_LOCATION.parent, Path(secretary.board.__file__).resolve().parent)

    def test_the_configuration_carries_no_connection_string_of_its_own(self) -> None:
        """§5.4 is the only place an installation's URL lives; an `alembic.ini` literal is not."""
        config = migrate.alembic_config()

        self.assertIsNone(config.get_main_option("sqlalchemy.url", None))
        self.assertIsNone(config.config_file_name)
        self.assertEqual(config.get_main_option("script_location"), str(migrate.SCRIPT_LOCATION))
        self.assertNotIn("connection", config.attributes)

    def test_the_connection_and_the_passwords_travel_in_attributes(self) -> None:
        sentinel = object()

        config = migrate.alembic_config(connection=sentinel, passwords={"app_password": "a"})

        self.assertIs(config.attributes["connection"], sentinel)
        self.assertEqual(config.attributes["passwords"], {"app_password": "a"})

    def test_no_password_is_a_literal_in_any_revision(self) -> None:
        """A revision that creates a role takes its passwords as parameters of the run (§5.5).

        Both halves are kept: no revision may carry a password literal at all, and a revision that
        issues `CREATE ROLE` has to reach its passwords through `PASSWORD_PARAMETERS`. Only
        `0001_initial` creates roles — `0002_board_gaps` adds tables to a store whose roles already
        exist, and `0001`'s `ALTER DEFAULT PRIVILEGES` is what makes them reachable — so the second
        assertion is asked of the revisions it is actually about.
        """
        revisions = sorted((migrate.SCRIPT_LOCATION / "versions").glob("*.py"))
        self.assertTrue(revisions)
        creating_roles = 0
        for path in revisions:
            with self.subTest(revision=path.name):
                text = path.read_text(encoding="utf-8")
                self.assertNotIn("PASSWORD '", text)
                if "CREATE ROLE" in text:
                    creating_roles += 1
                    self.assertIn("PASSWORD_PARAMETERS", text)
        self.assertEqual(creating_roles, 1, "§5.5's fence is built once, by the initial revision")

    def test_the_url_survives_a_password_a_url_would_otherwise_break(self) -> None:
        with TemporaryDirectory() as tmp:
            write_store(Path(tmp), dict(COMPLETE, SECRETARY_DB_APP_PASSWORD="p@ss/w:rd"))
            credentials = resolve_role(Path(tmp), "app")

        url = migrate.sqlalchemy_url(credentials)

        self.assertEqual(url.drivername, "postgresql+psycopg")
        self.assertEqual(url.password, "p@ss/w:rd")
        self.assertEqual(url.database, "secretary")
        self.assertNotIn("p@ss/w:rd", str(url))  # never rendered, and never split into two fields

    def test_an_invocation_with_no_injected_connection_refuses_and_names_the_entry_point(self) -> None:
        """There is one supported way to run these migrations, and `env.py` says which.

        The runner holds `pg_advisory_lock` on the session it migrates on (§7.4), so `env.py`
        opening a connection of its own would migrate outside the lock — and would then reach the
        initial revision with none of §5.5's generated passwords. It refuses here instead, before
        anything connects, naming `secretary.board.migrate`.
        """
        from alembic import command

        with self.assertRaisesRegex(RuntimeError, "migrations run only through secretary"):
            command.upgrade(migrate.alembic_config(), "heads")

    def test_the_advisory_key_is_a_fixed_literal(self) -> None:
        """Two upgrades of one installation contend only if every checkout uses one key."""
        self.assertEqual(migrate.ADVISORY_LOCK_KEY, 0x2C5B1F4A6E9D0713)


class InstanceRepository(unittest.TestCase):
    """A throwaway instance repository, for the tests that need one."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = Path(self.tmp.name)
        self.git("init", "-b", "main")
        self.git("config", "user.email", "test@example.invalid")
        self.git("config", "user.name", "Test")
        (self.instance / "README.md").write_text("instance\n", encoding="utf-8")
        self.git("add", "README.md")
        self.git("commit", "-m", "Initial")

    def git(self, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(self.instance), *args],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()

    def ignored(self) -> bool:
        return state_repo.is_ignored(self.instance, f"/{STORE_FILE}")


class IgnoreLifecycleTests(InstanceRepository):
    """The durable exclusion `board_transport.ensure` gives the transport (criterion 2, §5.4).

    A finding that the file is tracked is not a lifecycle; making `/board-store.env` excluded is.
    This card ships the operation and calls it against no live installation: the bootstrap path
    that generates the three passwords owns the call, and calls it before it writes the file.
    """

    def test_it_adds_the_exclusion_and_says_so_once(self) -> None:
        first = ensure_ignored(self.instance)

        self.assertTrue(first.ignore_added)
        self.assertTrue(first.changed)
        self.assertEqual(first.render(), "added board store ignore")
        self.assertTrue(self.ignored())

        second = ensure_ignored(self.instance)

        self.assertFalse(second.changed)
        self.assertEqual(second.render(), "unchanged")

    def test_a_dry_run_names_the_action_and_writes_nothing(self) -> None:
        outcome = ensure_ignored(self.instance, dry_run=True)

        self.assertTrue(outcome.ignore_added)
        self.assertEqual(outcome.render(dry_run=True), "would add board store ignore")
        self.assertFalse((self.instance / ".gitignore").exists())
        self.assertFalse(self.ignored())

    def test_it_refuses_a_configuration_anyone_could_read(self) -> None:
        write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            ensure_ignored(self.instance)

        self.assertEqual(store_path(self.instance).stat().st_mode & 0o777, 0o644)

    def test_a_dry_run_also_refuses_a_permissive_file(self) -> None:
        write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            ensure_ignored(self.instance, dry_run=True)

        self.assertEqual(store_path(self.instance).stat().st_mode & 0o777, 0o644)

    def test_an_already_private_configuration_needs_no_repair(self) -> None:
        write_store(self.instance)
        ensure_ignored(self.instance)

        outcome = ensure_ignored(self.instance)

        self.assertFalse(outcome.changed)

    def test_a_tracked_configuration_refuses_rather_than_pretending_to_hide_it(self) -> None:
        write_store(self.instance)
        self.git("add", "-f", STORE_FILE)
        self.git("commit", "-m", "Track it by mistake")

        with self.assertRaisesRegex(BoardStoreError, "tracked in the instance repository"):
            ensure_ignored(self.instance)

    def test_a_symlink_refuses_for_the_reason_the_parse_refuses_one(self) -> None:
        store_path(self.instance).symlink_to(self.instance / "elsewhere.env")

        with self.assertRaisesRegex(BoardStoreError, "regular file, not a symlink"):
            ensure_ignored(self.instance)

    def test_it_never_creates_the_configuration_itself(self) -> None:
        ensure_ignored(self.instance)

        self.assertFalse(store_path(self.instance).exists())

    def test_a_directory_without_a_repository_is_left_alone(self) -> None:
        with TemporaryDirectory() as plain:
            outcome = ensure_ignored(Path(plain))

        self.assertFalse(outcome.changed)


class ExclusionEnforcementTests(InstanceRepository):
    """The lifecycle stands *in front of* every read of a configured store, not beside it.

    Last round's gap was that a tracked `board-store.env` was only a passive finding: a store
    could be read and migrated on top of database credentials the instance repository was
    publishing. `resolve` is the one door — `resolve_role`, `migrate_instance` and `env.py` all
    go through it — so the enforcement is there, and these tests are what says so.
    """

    def test_resolving_a_tracked_configuration_refuses_with_its_reason(self) -> None:
        write_store(self.instance)
        self.git("add", "-f", STORE_FILE)
        self.git("commit", "-m", "credentials, by mistake")

        with self.assertRaisesRegex(BoardStoreError, "tracked in the instance repository"):
            resolve(self.instance)
        with self.assertRaisesRegex(BoardStoreError, "tracked in the instance repository"):
            resolve_role(self.instance, "owner")

    def test_a_configured_store_is_excluded_before_it_is_read(self) -> None:
        write_store(self.instance)
        self.assertFalse(self.ignored())

        config = resolve(self.instance)

        self.assertTrue(self.ignored(), "resolve must make the exclusion durable, not report it")
        self.assertEqual(config.owner_user, "secretary_owner")

    def test_the_lifecycle_outcome_is_visible_to_a_caller(self) -> None:
        write_store(self.instance)

        _, first = resolve_with_lifecycle(self.instance)
        _, second = resolve_with_lifecycle(self.instance)

        self.assertTrue(first.ignore_added)
        self.assertEqual(first.render(), "added board store ignore")
        self.assertFalse(second.changed)

    def test_the_read_path_refuses_a_broad_mode_rather_than_repairing_it(self) -> None:
        """`enforce_exclusion` is the git half of `ensure_ignored` and deliberately not the mode
        half: a credential file anyone could read has already been exposed, so `parse` refuses it
        instead of quietly chmodding it in the middle of a read. `ensure_ignored`, which the
        upgrade step calls, is what repairs it — visibly."""
        path = write_store(self.instance, mode=0o644)

        with self.assertRaisesRegex(BoardStoreError, "permissions are too broad"):
            resolve(self.instance)

        self.assertEqual(path.stat().st_mode & 0o777, 0o644)
        self.assertTrue(self.ignored())

    def test_migrating_a_tracked_configuration_refuses_before_it_connects(self) -> None:
        from secretary.board import migrate as board_migrate

        write_store(self.instance)
        self.git("add", "-f", STORE_FILE)
        self.git("commit", "-m", "credentials, by mistake")

        with (
            mock.patch.object(board_migrate.board_store, "resolve", wraps=resolve) as door,
            self.assertRaisesRegex(BoardStoreError, "tracked in the instance repository"),
        ):
            board_migrate.migrate_instance(self.instance)

        door.assert_called_once_with(self.instance)

    def test_the_upgrade_step_fails_rather_than_migrating_over_tracked_credentials(self) -> None:
        write_store(self.instance)
        self.git("add", "-f", STORE_FILE)
        self.git("commit", "-m", "credentials, by mistake")
        context = upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=self.instance,
            base_branch="main",
            dry_run=False,
            units=None,
            orca=None,
            automations=None,
        )

        with mock.patch.object(upgrade, "migrate_instance") as migrated:
            result = upgrade.step_board_store(context)

        migrated.assert_not_called()
        self.assertTrue(result.failed)
        self.assertIn("tracked in the instance repository", result.detail)

    def test_enforcement_is_the_only_door_to_a_configured_store(self) -> None:
        """The claim `resolve` is a chokepoint, checked against the source rather than asserted.

        Everything that opens a configured store reads it through `board_store.resolve`; the only
        callers of the underlying `parse` are `resolve_with_lifecycle` itself and the read-only
        `findings`, which does its own tracked check and never connects.
        """
        source = (Path(store.__file__)).read_text(encoding="utf-8")
        callers = [line.strip() for line in source.splitlines() if "parse(" in line and "def " not in line]

        self.assertEqual(callers, ["return parse(store_path(instance_dir)), outcome", "parse(path)"])
        self.assertIn("outcome = enforce_exclusion(instance_dir)", source)


class UpgradeStepTests(unittest.TestCase):
    """`step_board_store`: the three outcomes the observer decision defines for it."""

    def setUp(self) -> None:
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.instance = Path(self.tmp.name)

    def context(self, *, dry_run: bool = False):
        return upgrade.UpgradeContext(
            instance_path=self.instance,
            product_root=Path(self.tmp.name),
            base_branch="main",
            dry_run=dry_run,
            units=None,
            orca=None,
            automations=None,
        )

    def test_it_runs_immediately_after_the_step_that_installs_the_driver(self) -> None:
        """§7.4's placement: the driver has to exist before the step can connect."""
        names = [step.__name__ for step in upgrade.STEPS]

        dependency = names.index("step_dependencies")
        self.assertEqual(
            names[dependency : dependency + 4],
            [
                "step_dependencies",
                "step_dependency_provenance",
                "step_board_store_provision",
                "step_board_store",
            ],
        )

    def test_an_installation_with_no_store_is_a_no_op_that_never_connects(self) -> None:
        """Every installation until the store is provisioned, including the live one today."""
        with mock.patch.object(upgrade, "migrate_instance") as migrate:
            result = upgrade.step_board_store(self.context())

        migrate.assert_not_called()
        self.assertEqual((result.name, result.status), ("board-store", "skipped"))
        self.assertIn("not configured", result.detail)
        self.assertFalse(result.failed)

    def test_a_configured_store_is_migrated_and_the_versions_are_named(self) -> None:
        write_store(self.instance)

        with mock.patch.object(upgrade, "migrate_instance", return_value=("0001_initial",)) as migrate:
            result = upgrade.step_board_store(self.context())

        migrate.assert_called_once_with(self.instance, dry_run=False)
        self.assertEqual(result.status, "changed")
        self.assertIn("0001", result.detail)

    def test_a_current_store_reports_unchanged(self) -> None:
        write_store(self.instance)

        with mock.patch.object(upgrade, "migrate_instance", return_value=()):
            result = upgrade.step_board_store(self.context())

        self.assertEqual(result.status, "unchanged")

    def test_a_dry_run_says_what_it_would_apply_and_applies_nothing(self) -> None:
        write_store(self.instance)

        with mock.patch.object(upgrade, "migrate_instance", return_value=("0001_initial",)) as migrate:
            result = upgrade.step_board_store(self.context(dry_run=True))

        migrate.assert_called_once_with(self.instance, dry_run=True)
        self.assertEqual(result.status, "would-change")
        self.assertIn("would apply", result.detail)

    def test_a_store_that_is_configured_and_broken_fails_the_step(self) -> None:
        """Never walk past it: the next thing the upgrade would do is restart services."""
        with mock.patch.object(
            upgrade, "migrate_instance", side_effect=BoardStoreError("connection refused")
        ):
            write_store(self.instance)
            result = upgrade.step_board_store(self.context())

        self.assertTrue(result.failed)
        self.assertIn("connection refused", result.detail)

    def test_a_partial_configuration_fails_before_any_driver_is_reached(self) -> None:
        write_store(self.instance, {k: v for k, v in COMPLETE.items() if k != "SECRETARY_DB_NAME"})

        result = upgrade.step_board_store(self.context())

        self.assertTrue(result.failed)
        self.assertIn("SECRETARY_DB_NAME", result.detail)


if __name__ == "__main__":
    unittest.main()
