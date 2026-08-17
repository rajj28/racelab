"""Run the PostgreSQL 16 control arm from portable binaries, no install.

Arm A needs a stock PostgreSQL 16 with its default isolation left alone. The
usual way to get one is a container, but Docker is not always available -- so
this drives the EDB binaries-only distribution directly: unzip, initdb into a
directory inside the repo, start it on localhost. Nothing is installed, no
service is registered, and `stop` plus deleting `data/pg` removes every trace.

The server it produces is deliberately unremarkable. No isolation settings are
touched, because the entire point of Arm A is to show what an application gets
by default.

    python scripts/pg_portable.py init     # unzip, initdb, start, create db
    python scripts/pg_portable.py start
    python scripts/pg_portable.py stop
    python scripts/pg_portable.py status
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
VENDOR = REPO_ROOT / "vendor"
ZIP = VENDOR / "pg16.zip"
PGHOME = VENDOR / "pgsql"
PGBIN = PGHOME / "bin"
DATA = REPO_ROOT / "data" / "pg"
LOGFILE = REPO_ROOT / "data" / "pg.log"

USER = "racelab"
PASSWORD = "racelab"
DBNAME = "racelab"
PORT = "5432"


def run(cmd: list[str], **kw) -> subprocess.CompletedProcess:
    print("$", " ".join(str(c) for c in cmd))
    return subprocess.run([str(c) for c in cmd], text=True, capture_output=True, **kw)


def require_binaries() -> None:
    if (PGBIN / "initdb.exe").exists() or (PGBIN / "initdb").exists():
        return
    if not ZIP.exists():
        raise SystemExit(
            f"Neither extracted binaries nor {ZIP.relative_to(REPO_ROOT)} found.\n"
            "Download the EDB binaries-only zip first, e.g.\n"
            "  https://get.enterprisedb.com/postgresql/"
            "postgresql-16.12-1-windows-x64-binaries.zip\n"
            f"and save it to {ZIP.relative_to(REPO_ROOT)}"
        )
    print(f"extracting {ZIP.name} ...")
    # The archive contains a top-level `pgsql/` directory, so extracting into
    # vendor/ lands the tree exactly where PGHOME expects it.
    with zipfile.ZipFile(ZIP) as zf:
        zf.extractall(VENDOR)
    if not (PGBIN / "initdb.exe").exists():
        raise SystemExit(f"extracted, but no initdb found under {PGBIN}")
    print(f"binaries ready at {PGHOME.relative_to(REPO_ROOT)}")


def bin_path(name: str) -> pathlib.Path:
    exe = PGBIN / f"{name}.exe"
    return exe if exe.exists() else PGBIN / name


def cmd_init(_: argparse.Namespace) -> int:
    require_binaries()

    if DATA.exists() and (DATA / "PG_VERSION").exists():
        print(f"data directory already initialised at {DATA.relative_to(REPO_ROOT)}")
    else:
        DATA.parent.mkdir(parents=True, exist_ok=True)
        if DATA.exists():
            shutil.rmtree(DATA)
        # initdb refuses --pwfile content on the command line, so the password
        # goes through a temp file that is deleted immediately afterwards.
        tmp = pathlib.Path(tempfile.mkdtemp()) / "pw"
        tmp.write_text(PASSWORD, encoding="utf-8")
        try:
            r = run([
                bin_path("initdb"),
                "-D", DATA,
                "-U", USER,
                "--pwfile", tmp,
                "--auth-local=trust",
                "--auth-host=scram-sha-256",
                "--encoding=UTF8",
                "--locale=C",
            ])
        finally:
            shutil.rmtree(tmp.parent, ignore_errors=True)
        if r.returncode != 0:
            print(r.stdout)
            print(r.stderr, file=sys.stderr)
            return r.returncode
        print("initdb complete")

    if cmd_start(_) != 0:
        return 1
    return _create_database()


def _create_database() -> int:
    r = run([bin_path("psql"), "-h", "127.0.0.1", "-p", PORT, "-U", USER,
             "-d", "postgres", "-tAc",
             f"SELECT 1 FROM pg_database WHERE datname='{DBNAME}'"],
            env=_env())
    if r.stdout.strip() == "1":
        print(f"database {DBNAME} already exists")
        return 0
    r = run([bin_path("createdb"), "-h", "127.0.0.1", "-p", PORT, "-U", USER, DBNAME],
            env=_env())
    if r.returncode != 0:
        print(r.stderr, file=sys.stderr)
        return r.returncode
    print(f"database {DBNAME} created")
    return 0


def _env() -> dict:
    import os

    env = dict(os.environ)
    env["PGPASSWORD"] = PASSWORD
    return env


def cmd_start(_: argparse.Namespace) -> int:
    require_binaries()
    if _is_running():
        print("already running")
        return 0
    LOGFILE.parent.mkdir(parents=True, exist_ok=True)

    # NOT via run(). `pg_ctl start` forks the server, and the server inherits
    # whatever handles pg_ctl was given. With `capture_output=True` that includes
    # the stdout pipe, so the pipe never reaches EOF, so `subprocess.run` waits
    # for a process that is designed to outlive it -- and `make` appears to hang
    # on the very first step. The fix is to give the child no pipe to inherit and
    # let the log file be the log file.
    cmd = [
        str(bin_path("pg_ctl")),
        "-D", str(DATA),
        "-l", str(LOGFILE),
        "-o", f"-p {PORT} -c listen_addresses=127.0.0.1",
        "start",
    ]
    print("$", " ".join(cmd))
    creation = getattr(subprocess, "DETACHED_PROCESS", 0)
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=creation,
        close_fds=True,
    )
    proc.wait(timeout=30)

    # `-w` is dropped along with the pipe, so readiness is confirmed by polling
    # the port rather than trusting pg_ctl's own wait.
    for _ in range(30):
        if _is_running():
            print(f"postgres listening on 127.0.0.1:{PORT}")
            return 0
        time.sleep(1)

    print("postgres did not become ready within 30s", file=sys.stderr)
    if LOGFILE.exists():
        print("--- server log ---")
        print(LOGFILE.read_text(encoding="utf-8", errors="replace")[-2000:])
    return 1


def cmd_stop(_: argparse.Namespace) -> int:
    if not _is_running():
        print("not running")
        return 0
    r = run([bin_path("pg_ctl"), "-D", DATA, "-m", "fast", "-w", "stop"])
    print(r.stdout.strip() or r.stderr.strip())
    return r.returncode


def cmd_status(_: argparse.Namespace) -> int:
    running = _is_running()
    print("running" if running else "not running")
    if running:
        r = run([bin_path("psql"), "-h", "127.0.0.1", "-p", PORT, "-U", USER,
                 "-d", DBNAME, "-tAc",
                 "SELECT version(); SHOW default_transaction_isolation;"],
                env=_env())
        print(r.stdout.strip() or r.stderr.strip())
    return 0 if running else 1


def _is_running() -> bool:
    if not (DATA / "PG_VERSION").exists():
        return False
    r = run([bin_path("pg_ctl"), "-D", DATA, "status"])
    return r.returncode == 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, fn in (("init", cmd_init), ("start", cmd_start),
                     ("stop", cmd_stop), ("status", cmd_status)):
        sub.add_parser(name).set_defaults(func=fn)
    args = ap.parse_args()
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
