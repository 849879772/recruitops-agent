"""Opt-in PG16 backup-policy acceptance using isolated synthetic data only.

Reuse verified native binaries without copying or changing an installed bundle.
The migration runner comes from this checkout, with synthetic migrations added
outside the bundle. This checks source behavior, not a packaged release build.
"""

import argparse
import io
import json
from pathlib import Path
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from packages.desktop_runtime.resources import Bundle, Layout
from packages.desktop_runtime.supervisor import Events, Supervisor
from scripts.apply_migrations import discover_migrations
from scripts.desktop.verify_native import connect


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--instance", required=True, type=Path)
    args = parser.parse_args()
    layout = Layout(args.bundle.resolve(), args.instance.resolve())
    layout.validate(ROOT)
    if layout.data.exists():
        raise ValueError("acceptance requires a fresh synthetic instance")
    bundle = Bundle.load(args.bundle)
    evidence = {"synthetic_database_only": True, "source_runtime": True, "packaged_release": False}
    streams = []

    class SourceSupervisor(Supervisor):
        def command(self, name, *args):
            command = super().command(name, *args)
            if name == "python" and str(bundle.resource("migration_script")) in command:
                command[command.index(str(bundle.resource("migration_script")))] = str(ROOT / "scripts/apply_migrations.py")
                if "--migrations-dir" in command:
                    migrations = layout.data / "tmp/acceptance-migrations"
                    command[command.index("--migrations-dir") + 1] = str(migrations if migrations.exists() else ROOT / "migrations")
            return command

    def start():
        stream = io.StringIO()
        streams.append(stream)
        runtime = SourceSupervisor(bundle, layout, ROOT, Events(stream))
        runtime.shell_token = "7" * 64
        started = time.monotonic()
        runtime.start()
        evidence.setdefault("startup_seconds", []).append(round(time.monotonic() - started, 2))
        return runtime

    def dumps():
        return set((layout.data / "backups").glob("pre-migration-*.dump"))

    first = start()
    try:
        assert not dumps()
        with connect(first) as connection:
            connection.execute("CREATE TABLE desktop_backup_probe (id integer PRIMARY KEY, note text NOT NULL)")
            connection.execute("INSERT INTO desktop_backup_probe VALUES (1, 'synthetic-retained')")
        identity = first.events.instance_id
        evidence["fresh_start_without_backup"] = True
    finally:
        first.stop()

    current = start()
    try:
        assert current.events.instance_id == identity and not dumps()
        with connect(current) as connection:
            assert connection.execute("SELECT note FROM desktop_backup_probe").fetchone()[0] == "synthetic-retained"
        evidence["current_database_restart_without_backup"] = True
        # Hand-created backups must survive automatic retention.
        current.backup()
        manual = next((layout.data / "backups").glob("manual-*.dump"))
    finally:
        current.stop()

    migrations = layout.data / "tmp/acceptance-migrations"
    shutil.copytree(ROOT / "migrations", migrations)
    version = max(item.version for item in discover_migrations(migrations)) + 1
    (migrations / f"{version:03d}_backup_acceptance.sql").write_text(
        "ALTER TABLE desktop_backup_probe ADD COLUMN upgraded integer NOT NULL DEFAULT 1;\n",
        encoding="utf-8",
    )
    upgraded = start()
    try:
        assert len(dumps()) == 1
        before_upgrade = next(iter(dumps()))
        with connect(upgraded) as connection:
            assert connection.execute("SELECT note, upgraded FROM desktop_backup_probe").fetchone() == ("synthetic-retained", 1)
            connection.execute("CREATE DATABASE desktop_backup_restore")
        upgraded.run_step("restore_test", upgraded.command("pg_restore", "-w", "--exit-on-error", "--no-owner",
            "--dbname", "desktop_backup_restore", before_upgrade), timeout=120,
            output=layout.data / "logs/acceptance-restore.log")
        with connect(upgraded, "desktop_backup_restore") as connection:
            assert connection.execute("SELECT note FROM desktop_backup_probe").fetchone()[0] == "synthetic-retained"
            assert connection.execute("SELECT count(*) FROM information_schema.columns WHERE table_name='desktop_backup_probe' AND column_name='upgraded'").fetchone()[0] == 0
        evidence["pre_upgrade_backup_restores_original_data_and_schema"] = True
        # Exercise rotation with real archives; no user database is used here.
        for _ in range(4):
            upgraded.backup("pre-migration")
        assert len(dumps()) == 3 and manual.exists()
        evidence["automatic_retention_three_manual_preserved"] = True
    finally:
        upgraded.stop()

    prior_dumps = dumps()
    orphan = layout.data / "backups" / ("pre-migration-" + "d" * 32 + "-11111111.partial")
    orphan.write_bytes(b"synthetic-incomplete")
    restarted = start()
    try:
        assert dumps() == prior_dumps and not orphan.exists() and manual.exists()
        with connect(restarted) as connection:
            assert connection.execute("SELECT note, upgraded FROM desktop_backup_probe").fetchone() == ("synthetic-retained", 1)
        evidence["post_upgrade_restart_skips_backup_and_cleans_orphan"] = True
    finally:
        restarted.stop()
    evidence["final_state"] = json.loads((layout.data / "instance.json").read_text())["state"]
    assert evidence["final_state"] == "stopped"
    evidence["passed"] = True
    (layout.data / "logs/backup-policy-events.jsonl").write_text("".join(s.getvalue() for s in streams), encoding="utf-8")
    (layout.data / "backup-policy-acceptance.json").write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
