"""
scripts/run_stock_agent.py - the background process the Windows runner starts (scripts/start_stock_agent.ps1).

It runs the normal dashboard server (uvicorn api.server:app, the same app and lifespan as `uvicorn api.server:app`) without a terminal:

  * loads the normal project environment from ONE .env file (python-dotenv, the same loader config.py uses); values are never logged;
  * writes stdout / stderr to log files and redacts any secret value that would ever appear in them;
  * writes runtime/stock_agent.pid (JSON: pid, instance id, port, ...) and holds runtime/stock_agent.lock so a second process refuses to start
    (an OS-level guard only; the app's own `paper_executor` lease is unchanged);
  * logs startup, the execution mode, the paper broker configuration (no broker call), the scheduler threads, shutdown and errors;
  * stops gracefully when runtime/stock_agent.stop carries its instance id (scripts/stop_stock_agent.ps1 writes it).

It never places, previews or cancels an order and adds no endpoint setting: the Stage 6.0 executor decides, behind its own startup
reconciliation and the Stage 5.9.1 safety gate, on the Alpaca PAPER host only. `--loopback-only` refuses every non-loopback connection
(offline checks and tests).
"""
from __future__ import annotations

import argparse
import io
import json
import logging
import os
import re
import sys
import threading
import time
import traceback
from datetime import datetime, timezone
from pathlib import Path

LOG = logging.getLogger("stock_agent.runner")
SECRET_NAME = re.compile(r"(KEY|SECRET|TOKEN|PASSWORD|PASSWD|AUTH|COOKIE|SESSION|USERNAME|EMAIL|USER_AGENT)", re.I)
INFO_LOGGERS = ("stock_agent.runner", "execution", "setups.scheduler", "safety.service", "uvicorn.error")
MAX_LOG_BYTES = 20 * 1024 * 1024
LOOPBACK = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}
EXIT_OK, EXIT_FAILED, EXIT_DUPLICATE = 0, 1, 3


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------- secret redaction ------------------------------------------------------
class Redactor:
    """Replaces every known secret value (environment variables whose NAME looks secret, at least 6 characters) with ***."""

    def __init__(self):
        self.values: list = []

    def refresh(self, environ=None) -> int:
        env = os.environ if environ is None else environ
        vals = {v for k, v in env.items() if SECRET_NAME.search(k) and isinstance(v, str) and len(v.strip()) >= 6}
        self.values = sorted(vals, key=len, reverse=True)
        return len(self.values)

    def __call__(self, text: str) -> str:
        if not text or not self.values:
            return text
        for v in self.values:
            if v in text:
                text = text.replace(v, "***")
        return text


REDACT = Redactor()


class RedactingStream(io.TextIOBase):
    def __init__(self, raw):
        self.raw = raw

    def write(self, s):
        out = REDACT(s)
        self.raw.write(out)
        self.raw.flush()
        return len(s)

    def flush(self):
        self.raw.flush()

    def fileno(self):
        return self.raw.fileno()

    @property
    def encoding(self):
        return "utf-8"


class RedactingFormatter(logging.Formatter):
    def format(self, record):
        return REDACT(super().format(record))


# ---------------------------------------------------------------- files ------------------------------------------------------------------
def _rotate(path: Path) -> None:
    try:
        if path.exists() and path.stat().st_size > MAX_LOG_BYTES:
            old = path.with_suffix(path.suffix + ".1")
            if old.exists():
                old.unlink()
            path.rename(old)
    except OSError:
        pass


def _redirect_output(log_dir: Path):
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = log_dir / "stock_agent_stdout.log", log_dir / "stock_agent_stderr.log"
    for p in (out_path, err_path):
        _rotate(p)
    out_f = open(out_path, "a", encoding="utf-8", buffering=1, errors="replace")
    err_f = open(err_path, "a", encoding="utf-8", buffering=1, errors="replace")
    for fd, f in ((1, out_f), (2, err_f)):                          # C-level writes (and child libraries) land in the same files
        try:
            os.dup2(f.fileno(), fd)
        except OSError:
            pass
    sys.stdout, sys.stderr = RedactingStream(out_f), RedactingStream(err_f)
    return out_path, err_path


def _configure_logging() -> None:
    fmt = RedactingFormatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    out = logging.StreamHandler(sys.stdout)
    out.setFormatter(fmt)
    err = logging.StreamHandler(sys.stderr)
    err.setFormatter(fmt)
    err.setLevel(logging.ERROR)                                     # errors appear in BOTH logs
    root.addHandler(out)
    root.addHandler(err)
    root.setLevel(logging.WARNING)                                  # the app's own default (api/server.py basicConfig WARNING)
    for name in INFO_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)
    for noisy in ("urllib3", "requests", "httpx", "httpcore"):    # never debug-log requests (headers stay out of the logs)
        logging.getLogger(noisy).setLevel(logging.WARNING)


class InstanceLock:
    """An exclusive, non-blocking lock on runtime/stock_agent.lock (released by the OS when the process ends, even on a crash)."""

    def __init__(self, path: Path):
        self.path, self.fh = path, None

    def acquire(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.fh = open(self.path, "a+")
        try:
            import msvcrt
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except ImportError:                                        # not Windows: fcntl
            import fcntl
            try:
                fcntl.flock(self.fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except OSError:
                return False
        except OSError:
            return False

    def release(self) -> None:
        if self.fh is None:
            return
        try:
            import msvcrt
            self.fh.seek(0)
            msvcrt.locking(self.fh.fileno(), msvcrt.LK_UNLCK, 1)
        except Exception:  # noqa: BLE001 - the OS releases it on exit anyway
            pass
        self.fh.close()
        self.fh = None


def write_pid(path: Path, info: dict) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(info, indent=1), encoding="utf-8")
    os.replace(tmp, path)


def remove_pid_if_ours(path: Path, instance_id: str) -> bool:
    try:
        cur = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    if cur.get("instance_id") == instance_id:
        try:
            path.unlink()
            return True
        except OSError:
            return False
    return False


def stop_requested(path: Path, instance_id: str) -> bool:
    try:
        return instance_id in path.read_text(encoding="utf-8")
    except OSError:
        return False


# ---------------------------------------------------------------- loopback-only network guard ---------------------------------------
BLOCKED: list = []


def install_loopback_guard() -> None:
    import socket
    real = socket.getaddrinfo

    def guarded(host, port, *a, **k):
        h = host.decode() if isinstance(host, bytes) else (host or "")
        if h not in LOOPBACK:
            BLOCKED.append(f"{h}:{port}")
            LOG.warning("network guard: blocked %s:%s (loopback-only run)", h, port)
            raise socket.gaierror(socket.EAI_NONAME, "blocked by the runner's loopback-only guard")
        return real(host, port, *a, **k)
    socket.getaddrinfo = guarded


# ---------------------------------------------------------------- main -------------------------------------------------------------------
def parse(argv=None):
    ap = argparse.ArgumentParser(description="Stock Agent background server process (started by scripts/start_stock_agent.ps1).")
    ap.add_argument("--app-root", required=True)
    ap.add_argument("--instance-id", required=True)
    ap.add_argument("--env-file", default="")
    ap.add_argument("--overrides-file", default="", help="optional second env file applied after --env-file (diagnostics / tests)")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--runtime-dir", default="")
    ap.add_argument("--log-dir", default="")
    ap.add_argument("--db-path", default="", help="database file (default: the app's own data/stock_agent.db)")
    ap.add_argument("--loopback-only", action="store_true", help="refuse every non-loopback connection (offline checks / tests)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    a = parse(argv)
    app_root = Path(a.app_root).resolve()
    runtime = Path(a.runtime_dir).resolve() if a.runtime_dir else app_root / "runtime"
    logs = Path(a.log_dir).resolve() if a.log_dir else app_root / "logs"
    runtime.mkdir(parents=True, exist_ok=True)
    out_path, err_path = _redirect_output(logs)
    _configure_logging()
    pid_path, stop_path = runtime / "stock_agent.pid", runtime / "stock_agent.stop"
    if a.host not in ("127.0.0.1", "localhost", "::1"):
        LOG.error("runner: refusing host %s - the background server is local-machine only", a.host)
        return EXIT_FAILED
    lock = InstanceLock(runtime / "stock_agent.lock")
    if not lock.acquire():
        LOG.error("runner: another Stock Agent runner already holds %s - refusing a duplicate process", lock.path)
        return EXIT_DUPLICATE
    try:
        return _run(a, app_root, runtime, pid_path, stop_path, out_path, err_path)
    except SystemExit as exc:
        code = exc.code if isinstance(exc.code, int) else EXIT_FAILED
        LOG.error("runner: exited during startup (code %s)", code)
        return code or EXIT_FAILED
    except BaseException:  # noqa: BLE001 - logged (redacted), then a clear non-zero exit
        LOG.error("runner: unexpected failure:\n%s", traceback.format_exc())
        return EXIT_FAILED
    finally:
        remove_pid_if_ours(pid_path, a.instance_id)
        lock.release()
        LOG.info("runner: process exit (pid %s)", os.getpid())


def _run(a, app_root: Path, runtime: Path, pid_path: Path, stop_path: Path, out_path: Path, err_path: Path) -> int:
    LOG.info("runner: startup - pid %s, instance %s, app root %s, python %s", os.getpid(), a.instance_id, app_root, sys.executable)
    os.chdir(app_root)
    if str(app_root) not in sys.path:
        sys.path.insert(0, str(app_root))
    from dotenv import dotenv_values, load_dotenv
    if a.env_file:
        envp = Path(a.env_file)
        if not envp.exists():
            LOG.error("runner: env file %s does not exist", envp)
            return EXIT_FAILED
        names = sorted(dotenv_values(envp).keys())
        load_dotenv(envp, override=False)                         # the same loader and semantics config.py uses
        LOG.info("runner: loaded %d environment names from %s (values never logged)", len(names), envp)
    else:
        LOG.warning("runner: no env file given - the app starts with the process environment only")
    if a.overrides_file:
        load_dotenv(Path(a.overrides_file), override=True)
        LOG.info("runner: applied overrides file %s", a.overrides_file)
    n = REDACT.refresh()
    LOG.info("runner: %d secret value(s) registered for log redaction", n)
    if os.environ.get("STOCK_AGENT_ENV", "") in ("test", "browser_test"):
        LOG.warning("runner: STOCK_AGENT_ENV=%s - the setup evaluator and the paper executor stay off in this environment", os.environ["STOCK_AGENT_ENV"])
    if a.loopback_only:
        install_loopback_guard()
        LOG.info("runner: loopback-only network guard installed")
    if a.db_path:
        import database.database as dbm
        dbm._db_instance = dbm.ResearchDatabase(Path(a.db_path))
        LOG.info("runner: database %s", Path(a.db_path).resolve())

    import config
    from execution import broker as XB
    from execution.store import ExecutionStore
    assert XB.PAPER_HOST == "paper-api.alpaca.markets" and XB.BASE == "https://paper-api.alpaca.markets", "paper host changed"
    from database.database import get_db
    LOG.info("runner: database in use %s", get_db().db_path)
    LOG.info("runner: market data mode %s | setup evaluator %s | paper executor loop %s", config.TRADER_DATA_MODE,
             "enabled" if config.SETUP_EVALUATOR_ENABLED else "disabled", "enabled" if config.EXECUTION_SCHEDULER_ENABLED else "disabled")
    paper_ok = bool(os.environ.get(XB.KEY_ENV) and os.environ.get(XB.SECRET_ENV))
    LOG.info("runner: paper broker - Alpaca PAPER host %s only, credentials configured: %s (no broker call at startup)", XB.PAPER_HOST, "yes" if paper_ok else "no")
    m = ExecutionStore().mode()
    LOG.info("runner: execution mode at startup: %s (%s)%s", m["mode"], "persisted" if m["persisted"] else "default",
             "; the executor reconciles with the broker before any write" if m["mode"] == "PAPER_AUTO" else "")

    import uvicorn
    from api.server import app
    server = uvicorn.Server(uvicorn.Config(app, host=a.host, port=a.port, log_level="info", access_log=False, lifespan="on",
                                           log_config=None))             # keep the runner's redacting handlers (uvicorn propagates to them)
    try:
        stop_path.unlink()                                         # a stale stop request never stops a fresh instance
    except OSError:
        pass
    info = {"pid": os.getpid(), "instance_id": a.instance_id, "app_root": str(app_root), "host": a.host, "port": a.port, "started_at": _now(),
            "python": sys.executable, "stdout_log": str(out_path), "stderr_log": str(err_path), "db": str(get_db().db_path)}
    write_pid(pid_path, info)

    def watcher():
        t0 = time.time()
        while not server.started and not server.should_exit and time.time() - t0 < 180:
            time.sleep(0.25)
        if server.started:
            LOG.info("runner: server listening on http://%s:%s", a.host, a.port)
            try:
                from execution.scheduler import status as xs
                from setups.scheduler import status as ss
                x, s = xs(), ss()
                LOG.info("runner: paper executor loop %s (lease %s)", "running" if x.get("running") else "NOT running", x.get("lease"))
                LOG.info("runner: setup evaluator loop %s", "running" if s.get("running") else "NOT running")
                from account_refresh.scheduler import status as ars
                ar = ars()
                LOG.info("runner: account refresh loop %s (lease %s, every %s s in session / %s s outside; read only)", "running" if ar.get("running") else "NOT running",
                         ar.get("lease"), ar.get("interval_s"), ar.get("idle_interval_s"))
            except Exception as exc:  # noqa: BLE001
                LOG.warning("runner: scheduler status unavailable (%s)", type(exc).__name__)
        last_beat = time.time()
        while not server.should_exit:
            if stop_requested(stop_path, a.instance_id):
                LOG.info("runner: stop requested by scripts/stop_stock_agent.ps1 - shutting down")
                server.should_exit = True
                break
            if time.time() - last_beat > 1800:
                last_beat = time.time()
                try:
                    from execution.scheduler import status as xs
                    from api.routes.trader_view import stream as _stream
                    fr = _stream().account_refresh.freshness()
                    LOG.info("runner: heartbeat - account data %s (age %s s, %s consecutive failures)", fr["status"], fr["age_seconds"], fr["consecutive_failures"])
                    LOG.info("runner: heartbeat - execution mode %s, executor loop %s, last cycle %s", ExecutionStore().mode()["mode"],
                             "running" if xs().get("running") else "NOT running", (xs().get("last_result") or {}).get("result"))
                except Exception as exc:  # noqa: BLE001
                    LOG.warning("runner: heartbeat failed (%s)", type(exc).__name__)
            time.sleep(1.0)

    threading.Thread(target=watcher, name="stock-agent-runner-watcher", daemon=True).start()
    server.run()
    if not server.started:
        LOG.error("runner: the server did not start (port %s busy or startup failed; see %s)", a.port, err_path)
        return EXIT_FAILED
    LOG.info("runner: shutdown complete")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
