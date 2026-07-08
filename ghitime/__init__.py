"""GHI-TIME — payroll-PREP time tracking for Gustafson Home Improvements.
It prepares payroll inputs; it never executes payroll.

AI-assisted (Claude Code); operator-reviewed before use.
"""
from __future__ import annotations

import os
import secrets
from pathlib import Path

import click
from flask import Flask, g, request

from . import db as db_mod


def create_app(config: dict | None = None) -> Flask:
    app = Flask(__name__)
    data_dir = Path(os.environ.get("GHITIME_DATA", "instance"))
    data_dir.mkdir(parents=True, exist_ok=True)
    app.config.update(
        DATABASE=os.environ.get("GHITIME_DB", str(data_dir / "ghitime.db")),
        COOKIE_SECURE=os.environ.get("GHITIME_COOKIE_SECURE", "0") == "1",
        MAX_CONTENT_LENGTH=1024 * 1024,
    )
    if config:
        app.config.update(config)

    secret_file = data_dir / "secret_key"
    if app.config.get("SECRET_KEY") is None:
        if not secret_file.exists():
            secret_file.write_text(secrets.token_hex(32))
            secret_file.chmod(0o600)
        app.secret_key = secret_file.read_text().strip()

    from . import admin, auth, entries, reports, sync

    app.register_blueprint(auth.bp)
    app.register_blueprint(entries.bp)
    app.register_blueprint(sync.bp)
    app.register_blueprint(admin.bp)
    app.register_blueprint(reports.bp)

    app.teardown_appcontext(db_mod.close_db)

    @app.before_request
    def _before():
        auth.load_current_user()
        auth.check_csrf()

    @app.context_processor
    def _ctx():
        return {
            "csrf_token": auth.csrf_token,
            "theme": request.cookies.get("ghitime_theme", ""),
            "user": g.get("user"),
        }

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    @app.get("/manifest.webmanifest")
    def manifest():
        return app.send_static_file("manifest.webmanifest")

    @app.get("/sw.js")
    def sw():
        # served from site root so its scope covers /capture
        resp = app.send_static_file("sw.js")
        resp.headers["Cache-Control"] = "no-cache"
        return resp

    _register_cli(app)
    return app


def _register_cli(app: Flask) -> None:
    @app.cli.command("migrate")
    def migrate_cmd():
        """Apply pending numbered forward-only migrations."""
        conn = db_mod.connect(app.config["DATABASE"])
        applied = db_mod.migrate(conn)
        click.echo(f"applied: {applied or 'nothing pending'}")

    @app.cli.command("create-admin")
    @click.argument("username")
    @click.argument("display_name")
    @click.option("--worker/--no-worker", default=True,
                  help="also give the worker role (owner logs own hours)")
    @click.option("--admin/--no-admin", default=True)
    def create_admin_cmd(username, display_name, worker, admin):
        """Bootstrap an account; prints a temp password (forced change)."""
        from argon2 import PasswordHasher

        conn = db_mod.connect(app.config["DATABASE"])
        temp = secrets.token_urlsafe(9)
        conn.execute(
            "INSERT INTO person (username, password_hash, display_name, is_worker,"
            " is_admin, worker_type, active, must_change_pw, created_at)"
            " VALUES (?,?,?,?,?,?,1,1,?)",
            (username, PasswordHasher().hash(temp), display_name,
             1 if worker else 0, 1 if admin else 0, "employee", db_mod.utcnow()),
        )
        db_mod.audit(conn, None, "person.create_cli", "person", username)
        conn.commit()
        click.echo(f"created {username}; temp password: {temp}")
        click.echo("They must change it at first login.")

    @app.cli.command("seed-demo")
    def seed_cmd():
        """Fake-name seed data demonstrating every state (dev/demo only)."""
        from .seed import seed_demo

        conn = db_mod.connect(app.config["DATABASE"])
        summary = seed_demo(conn)
        click.echo(summary)

    @app.cli.command("export-all")
    @click.argument("directory")
    def export_all_cmd(directory):
        """Full export: every table to CSV and JSON in one command."""
        from .reports import full_export_files

        conn = db_mod.connect(app.config["DATABASE"])
        out = Path(directory)
        out.mkdir(parents=True, exist_ok=True)
        files = full_export_files(conn)
        for name, content in files.items():
            (out / name).write_text(content)
        (out / "EXPORTED_AT.txt").write_text(db_mod.utcnow())
        click.echo(f"wrote {len(files) + 1} files to {out}")
