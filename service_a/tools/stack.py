"""The fault actions of fault_demo.py and consistency_demo.py, for the two ways LAME runs.

LocalStack: A, C and D as Windows processes started by run_local.ps1 (PIDs in pids.json), B and
PostgreSQL in the containers lame-b and lame-pg.
ComposeStack: everything from the root docker-compose.yml (containers lame-service-*), started with
FAULTS_ENABLED=true so that D's /admin/faults exists.

Both expose A on 127.0.0.1:8000 and D on 127.0.0.1:8400 to the scripts.
"""
import ctypes
import json
import os
import subprocess
from pathlib import Path


def run(*args) -> str:
    return subprocess.run(list(args), capture_output=True, text=True, encoding="utf-8", errors="replace").stdout


class ComposeStack:
    """Containers of the root compose file."""

    name = "compose"

    def __init__(self, workspace: Path):
        self.workspace = workspace

    def kill_d(self):
        run("docker", "kill", "lame-service-d")

    def start_d(self):
        run("docker", "start", "lame-service-d")

    def freeze_d(self, on: bool):
        # docker pause freezes D's processes; the kernel still accepts the connection, so A gets no reply.
        # docker pause 凍結 D 的行程;核心仍接受連線,所以 A 收不到回應。
        run("docker", "pause" if on else "unpause", "lame-service-d")

    def kill_a(self):
        run("docker", "kill", "lame-service-a")

    def start_a(self):
        run("docker", "start", "lame-service-a")

    def stop_b(self):
        run("docker", "stop", "lame-service-b")

    def start_b(self):
        run("docker", "start", "lame-service-b")

    def a_sql(self, query: str) -> list[list[str]]:
        out = run("docker", "exec", "lame-service-a-db", "psql", "-U", "lame", "-d", "lame", "-At", "-F", "|", "-c", query).strip()
        return [row.split("|") for row in out.splitlines()] if out else []

    def d_sql(self, query: str, *args) -> list:
        code = ("import json,sqlite3,sys; c=sqlite3.connect('file:/data/d.db?mode=ro',uri=True); "
                "print(json.dumps(c.execute(sys.argv[1], json.loads(sys.argv[2])).fetchall()))")
        return [tuple(r) for r in json.loads(run("docker", "exec", "lame-service-d", "python", "-c", code, query, json.dumps(args)))]

    def d_log(self) -> list[str]:
        done = subprocess.run(["docker", "logs", "--tail", "400", "lame-service-d"], capture_output=True, text=True,
                              encoding="utf-8", errors="replace")
        return (done.stdout + done.stderr).splitlines()


class LocalStack(ComposeStack):
    """Processes started by run_local.ps1."""

    name = "local"

    def _pids(self) -> dict:
        return json.loads((self.workspace / "pids.json").read_text(encoding="utf-8-sig"))

    def _save(self, key: str, pid: int):
        p = self._pids()
        p[key] = pid
        (self.workspace / "pids.json").write_text(json.dumps(p, indent=4), encoding="utf-8")

    def _spawn(self, args, cwd, env, log):
        out = open(self.workspace / "logs" / log, "ab")
        return subprocess.Popen(args, cwd=cwd, env={**os.environ, **env}, stdout=out, stderr=subprocess.STDOUT,
                                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP).pid

    def kill_d(self):
        run("taskkill", "/F", "/T", "/PID", str(self._pids()["D"]))

    def start_d(self):
        env = {"B_CTRL_URL": "ws://127.0.0.1:11659/d", "A_NOTIFY_URL": "http://127.0.0.1:8000/internal/order-events",
               "DATA_DIR": str(self.workspace / "d-data"), "PYTHONUTF8": "1", "FAULTS_ENABLED": "true"}
        self._save("D", self._spawn([str(self.workspace / "venv-ad" / "Scripts" / "python.exe"), "-m", "d.main"],
                                    self.workspace / "d" / "service_d", env, "d.log"))

    def freeze_d(self, on: bool):
        # Freezes D's process: its socket still accepts, but nothing answers.
        # 凍結 D 的行程:socket 仍接受連線,但不會有回應。
        pid = self._listening_pid(8400)
        ntdll, kernel = ctypes.WinDLL("ntdll"), ctypes.WinDLL("kernel32")
        handle = kernel.OpenProcess(0x0800, False, pid)          # PROCESS_SUSPEND_RESUME
        if not handle:
            raise OSError(f"cannot open D's process {pid}")
        try:
            status = (ntdll.NtSuspendProcess if on else ntdll.NtResumeProcess)(handle)
            if status != 0:
                raise OSError(f"NtSuspend/ResumeProcess({pid}) returned {status:#x}")
        finally:
            kernel.CloseHandle(handle)

    @staticmethod
    def _listening_pid(port: int) -> int:
        for line in run("netstat", "-ano", "-p", "TCP").splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3] == "LISTENING":
                return int(parts[4])
        raise RuntimeError(f"nothing listens on {port}")

    def kill_a(self):
        run("taskkill", "/F", "/T", "/PID", str(self._pids()["A"]))

    def start_a(self):
        env = {"DATABASE_URL": "postgresql+asyncpg://lame:lame@127.0.0.1:5432/lame", "D_URL": "http://127.0.0.1:8400",
               "B_MD_URL": "ws://127.0.0.1:9999", "PYTHONUTF8": "1",
               "JWT_SECRET": (self.workspace / "jwt_secret.txt").read_text(encoding="utf-8-sig").strip()}
        self._save("A", self._spawn([str(self.workspace / "venv-ad" / "Scripts" / "python.exe"), "-m", "uvicorn", "app.main:app",
                                     "--host", "127.0.0.1", "--port", "8000", "--workers", "1"],
                                    self.workspace / "a" / "service_a", env, "a.log"))

    def stop_b(self):
        run("docker", "stop", "lame-b")

    def start_b(self):
        run("docker", "start", "lame-b")

    def a_sql(self, query: str) -> list[list[str]]:
        out = run("docker", "exec", "lame-pg", "psql", "-U", "lame", "-d", "lame", "-At", "-F", "|", "-c", query).strip()
        return [row.split("|") for row in out.splitlines()] if out else []

    def d_sql(self, query: str, *args) -> list:
        import sqlite3
        with sqlite3.connect(f"file:{self.workspace / 'd-data' / 'd.db'}?mode=ro", uri=True) as c:
            return c.execute(query, args).fetchall()

    def d_log(self) -> list[str]:
        text = (self.workspace / "logs" / "d.log").read_text(encoding="utf-8", errors="replace")
        return text.splitlines()[-400:]


def make(compose: bool, workspace: Path):
    return ComposeStack(workspace) if compose else LocalStack(workspace)
