"""Opt-in real native acceptance on a fresh owned disposable PostgreSQL instance."""

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from packages.desktop_runtime.resources import Bundle, Layout
from packages.desktop_runtime.supervisor import Events, Supervisor


def connect(runtime, database="postgres"):
    import psycopg
    return psycopg.connect(host="127.0.0.1", port=runtime.db_port, user="desktop",
        password=runtime.env["PGPASSWORD"], dbname=database, connect_timeout=3, autocommit=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--instance", required=True, type=Path)
    args = parser.parse_args()
    bundle = Bundle.load(args.bundle)
    layout = Layout(bundle.root, args.instance.resolve())
    layout.validate(ROOT)
    if layout.data.exists():
        raise ValueError("native acceptance requires fresh synthetic instance")
    evidence = {"real_postgresql": True, "real_packaged_python": True, "release_accepted": False}
    runtime = Supervisor(bundle, layout, ROOT, Events(sys.stdout), timeout=120)
    runtime.shell_token = "9" * 64  # Disposable synthetic local test token, never a personal credential.
    try:
        runtime.start()
        identity = runtime.events.instance_id
        with connect(runtime) as connection:
            evidence["postgres_version"] = connection.execute("SHOW server_version").fetchone()[0]
            evidence["vector_version"] = connection.execute("SELECT extversion FROM pg_extension WHERE extname='vector'").fetchone()[0]
            assert connection.execute("SELECT '[1,2,3]'::vector <-> '[1,2,3]'::vector").fetchone()[0] == 0
            evidence["migration_count"] = connection.execute("SELECT count(*) FROM schema_migrations").fetchone()[0]
            connection.execute("CREATE TABLE desktop_native_probe (id integer PRIMARY KEY, note text NOT NULL)")
            connection.execute("INSERT INTO desktop_native_probe VALUES (1, 'synthetic-native-restart')")
        browser_code = (
            "from playwright.sync_api import sync_playwright; "
            "p=sync_playwright().start(); b=p.chromium.launch(headless=True); "
            "page=b.new_page(); page.set_content('<title>owned-native-fixture</title><p>synthetic</p>'); "
            "assert page.title()=='owned-native-fixture'; b.close(); p.stop()")
        browser = runtime.tree.spawn(runtime.command("python", "-I", "-B", "-c", browser_code), layout.data, runtime.env)
        if browser.wait(60) != 0:
            raise RuntimeError("packaged_playwright_launch_failed")
        evidence["packaged_playwright_default_headless_launch"] = True
        crawler = runtime.tree.spawn(runtime.command("python", "-I", "-B", "-m", "scripts.run_agent_crawler", "--help"), layout.data, runtime.env)
        if crawler.wait(30) != 0:
            raise RuntimeError("packaged_candidate_crawler_entrypoint_missing")
        evidence["packaged_candidate_crawler_entrypoint"] = True
        runtime.backup()
    finally:
        runtime.stop()
    evidence["first_stop_state"] = json.loads((layout.data / "instance.json").read_text())["state"]
    if evidence["first_stop_state"] != "stopped":
        raise RuntimeError("native_shutdown_not_clean")
    restarted = Supervisor(bundle, layout, ROOT, Events(sys.stdout), timeout=120)
    restarted.shell_token = "8" * 64
    try:
        restarted.start()
        assert restarted.events.instance_id == identity
        with connect(restarted) as connection:
            assert connection.execute("SELECT note FROM desktop_native_probe WHERE id=1").fetchone()[0] == "synthetic-native-restart"
            connection.execute("CREATE DATABASE desktop_native_restore")
        backup = sorted((layout.data / "backups").glob("manual-*.dump"))[0]
        restored = restarted.tree.spawn(restarted.command("pg_restore", "-w", "--exit-on-error", "--no-owner",
            "--dbname", "desktop_native_restore", backup), layout.data, restarted.env)
        if restored.wait(120) != 0:
            raise RuntimeError("native_restore_failed")
        with connect(restarted, "desktop_native_restore") as connection:
            assert connection.execute("SELECT note FROM desktop_native_probe WHERE id=1").fetchone()[0] == "synthetic-native-restart"
        evidence.update(restart_retained_record=True, logical_backup_restore=True, identity_persisted=True)
    finally:
        restarted.stop()
    evidence["final_stop_state"] = json.loads((layout.data / "instance.json").read_text())["state"]
    evidence["release_accepted"] = evidence["final_stop_state"] == "stopped"
    output = layout.data / "native-acceptance.json"
    output.write_text(json.dumps(evidence, indent=2), encoding="utf-8")
    print(json.dumps(evidence), flush=True)


if __name__ == "__main__":
    main()
