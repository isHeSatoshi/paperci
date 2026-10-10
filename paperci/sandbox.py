"""Docker sandbox. Third-party code only ever runs inside these containers.

Isolation: non-root, all capabilities dropped, no-new-privileges, CPU/mem/pid
limits, no host mounts, no credentials. Network is either an `--internal`
network whose only way out is an allowlist proxy (build phase), or none at all
(verification runs)."""
from __future__ import annotations

import io
import json
import re
import subprocess
import tarfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .config import (INTERNAL_NET, PROXY_IMAGE, PROXY_NAME, SANDBOX_CPUS, SANDBOX_IMAGE,
                     SANDBOX_MEM, SANDBOX_PIDS)

PROXY_URL = f"http://{PROXY_NAME}:8888"
SANDBOX_ENV = {
    "HTTP_PROXY": PROXY_URL, "HTTPS_PROXY": PROXY_URL, "http_proxy": PROXY_URL, "https_proxy": PROXY_URL,
    "NO_PROXY": "localhost,127.0.0.1", "PIP_DEFAULT_TIMEOUT": "60", "HF_HUB_DISABLE_TELEMETRY": "1",
    "CUDA_VISIBLE_DEVICES": "", "MPLBACKEND": "Agg", "PYTHONUNBUFFERED": "1",
}


class SandboxError(RuntimeError):
    pass


def _docker(*args: str, timeout: float = 120, input_bytes: bytes | None = None,
            check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(["docker", *args], capture_output=True, timeout=timeout, input=input_bytes)
    if check and p.returncode != 0:
        raise SandboxError(f"docker {args[0]} failed: {p.stderr.decode('utf-8', 'replace')[-500:]}")
    return p


def docker_ok() -> bool:
    try:
        return _docker("version", "--format", "{{.Server.Version}}", timeout=20, check=False).returncode == 0
    except Exception:
        return False


def ensure_infra() -> list[str]:
    """Create the internal network and (re)start the egress proxy. Returns the
    self-healing actions taken so they can be traced."""
    actions: list[str] = []
    nets = _docker("network", "ls", "--format", "{{.Name}}").stdout.decode().split()
    if INTERNAL_NET not in nets:
        _docker("network", "create", "--internal", "--label", "paperci=1", INTERNAL_NET)
        actions.append("created internal network")
    st = _docker("inspect", "-f", "{{.State.Running}}", PROXY_NAME, check=False)
    if st.returncode != 0:
        _docker("run", "-d", "--name", PROXY_NAME, "--label", "paperci=1", "--restart", "unless-stopped",
                "--memory", "256m", "--cpus", "1", PROXY_IMAGE)
        _docker("network", "connect", INTERNAL_NET, PROXY_NAME)
        actions.append("started egress proxy")
    elif st.stdout.decode().strip() != "true":
        _docker("start", PROXY_NAME)
        actions.append("restarted stopped egress proxy")
    info = json.loads(_docker("inspect", PROXY_NAME).stdout)[0]
    if INTERNAL_NET not in info["NetworkSettings"]["Networks"]:
        _docker("network", "connect", INTERNAL_NET, PROXY_NAME)
        actions.append("reattached proxy to internal network")
    return actions


_CONNECT_RE = re.compile(r"Connect \(file descriptor \d+\): ([\d.]+)")
_OK_RE = re.compile(r'Established connection to host "([^"]+)"')
_REFUSED_RE = re.compile(r'refused on filtered (?:domain|url) "([^"]+)"', re.I)


def egress_since(since_ts: float, client_ip: str | None = None) -> dict:
    """Parse tinyproxy logs: hosts reached and hosts refused, for one client IP."""
    p = _docker("logs", "--since", str(int(since_ts)), PROXY_NAME, check=False, timeout=30)
    text = (p.stdout + p.stderr).decode("utf-8", "replace")
    allowed: dict[str, int] = {}
    denied: dict[str, int] = {}
    cur_ip = None
    for line in text.splitlines():
        m = _CONNECT_RE.search(line)
        if m:
            cur_ip = m.group(1)
            continue
        if client_ip and cur_ip != client_ip:
            continue
        m = _OK_RE.search(line)
        if m:
            allowed[m.group(1).lower()] = allowed.get(m.group(1).lower(), 0) + 1
            continue
        m = _REFUSED_RE.search(line)
        if m:
            denied[m.group(1).lower()] = denied.get(m.group(1).lower(), 0) + 1
    return {"allowed": allowed, "denied": denied}


@dataclass
class ExecResult:
    exit_code: int
    output: str
    seconds: float
    timed_out: bool = False

    def tail(self, n: int = 3000) -> str:
        o = self.output
        if len(o) <= n:
            return o
        head = o[: n // 4]
        return head + f"\n... [{len(o) - n} chars elided] ...\n" + o[-(n - len(head)):]


@dataclass
class Sandbox:
    name: str
    network: str = "proxy"  # "proxy" | "none"
    image: str = SANDBOX_IMAGE
    log_dir: Path | None = None
    started_at: float = field(default_factory=time.time)
    _n: int = 0

    @classmethod
    def start(cls, tag: str, network: str = "proxy", image: str = SANDBOX_IMAGE,
              log_dir: Path | None = None) -> "Sandbox":
        name = f"paperci-{re.sub(r'[^a-z0-9]+', '-', tag.lower())[:40]}-{uuid.uuid4().hex[:6]}"
        args = ["run", "-d", "--name", name, "--label", "paperci=1",
                "--user", "1000:1000", "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
                "--cpus", SANDBOX_CPUS, "--memory", SANDBOX_MEM, "--memory-swap", SANDBOX_MEM,
                "--pids-limit", SANDBOX_PIDS, "-w", "/work"]
        if network == "none":
            args += ["--network", "none"]
        else:
            ensure_infra()
            args += ["--network", INTERNAL_NET]
            for k, v in SANDBOX_ENV.items():
                args += ["-e", f"{k}={v}"]
        args += [image, "sleep", "infinity"]
        _docker(*args, timeout=120)
        sb = cls(name=name, network=network, image=image, log_dir=log_dir)
        if log_dir:
            log_dir.mkdir(parents=True, exist_ok=True)
        return sb

    def ip(self) -> str | None:
        info = json.loads(_docker("inspect", self.name).stdout)[0]
        net = info["NetworkSettings"]["Networks"].get(INTERNAL_NET)
        return net["IPAddress"] if net else None

    def exec(self, cmd: str, timeout: int = 600, env: dict | None = None) -> ExecResult:
        self._n += 1
        args = ["exec", "-u", "1000:1000", "-w", "/work"]
        for k, v in (env or {}).items():
            args += ["-e", f"{k}={v}"]
        args += [self.name, "timeout", "-k", "10", str(timeout), "bash", "-lc", cmd]
        t0 = time.time()
        try:
            p = _docker(*args, timeout=timeout + 60, check=False)
            out = (p.stdout + p.stderr).decode("utf-8", "replace")
            code = p.returncode
        except subprocess.TimeoutExpired:
            out, code = "[host timeout]", 124
        dt = time.time() - t0
        res = ExecResult(exit_code=code, output=out, seconds=round(dt, 1), timed_out=(code == 124))
        if self.log_dir:
            (self.log_dir / f"cmd_{self._n:03d}.log").write_text(
                f"$ {cmd}\n# exit={code} seconds={dt:.1f}\n{out}", encoding="utf-8")
        return res

    def exec_root(self, cmd: str, timeout: int = 300) -> ExecResult:
        """Harness-only: root without capabilities, used to make root-owned snapshots
        the agent (uid 1000) cannot modify."""
        t0 = time.time()
        p = _docker("exec", "-u", "0", self.name, "bash", "-c", cmd, timeout=timeout, check=False)
        return ExecResult(p.returncode, (p.stdout + p.stderr).decode("utf-8", "replace"), round(time.time() - t0, 1))

    def write_file(self, path: str, content: str) -> None:
        if not path.startswith("/work/"):
            raise SandboxError("writes are restricted to /work")
        p = _docker("exec", "-i", "-u", "1000:1000", self.name, "bash", "-c",
                    'mkdir -p "$(dirname "$1")" && cat > "$1"', "_", path,
                    input_bytes=content.encode("utf-8"), check=False)
        if p.returncode != 0:
            raise SandboxError(p.stderr.decode("utf-8", "replace")[-300:])

    def read_file(self, path: str, max_bytes: int = 200_000) -> str:
        p = _docker("exec", "-u", "1000:1000", self.name, "head", "-c", str(max_bytes), path, check=False)
        if p.returncode != 0:
            raise SandboxError(p.stderr.decode("utf-8", "replace")[-300:])
        return p.stdout.decode("utf-8", "replace")

    def copy_out(self, container_path: str, host_dir: Path, max_mb: int = 50) -> list[Path]:
        """Copy a directory out as a tar stream and extract only regular files,
        refusing absolute paths, '..' and links (no tar-slip onto the host)."""
        du = self.exec(f"du -sm {container_path} 2>/dev/null | cut -f1", timeout=60).output.strip()
        if du.isdigit() and int(du) > max_mb:
            raise SandboxError(f"{container_path} is {du}MB > {max_mb}MB cap")
        p = _docker("cp", f"{self.name}:{container_path}", "-", timeout=300)
        host_dir.mkdir(parents=True, exist_ok=True)
        written = []
        with tarfile.open(fileobj=io.BytesIO(p.stdout)) as tf:
            for m in tf.getmembers():
                if not m.isfile() or m.name.startswith("/") or ".." in Path(m.name).parts:
                    continue
                parts = Path(m.name).parts[1:]  # drop top-level dir name
                if not parts:
                    continue
                dest = host_dir.joinpath(*parts)
                dest.parent.mkdir(parents=True, exist_ok=True)
                f = tf.extractfile(m)
                if f:
                    dest.write_bytes(f.read())
                    written.append(dest)
        return written

    def commit(self, tag: str) -> str:
        _docker("commit", self.name, tag, timeout=600)
        return tag

    def remove(self) -> None:
        _docker("rm", "-f", self.name, check=False, timeout=60)


def remove_image(tag: str) -> None:
    _docker("rmi", "-f", tag, check=False, timeout=120)


def cleanup_stale() -> int:
    ids = _docker("ps", "-aq", "--filter", "label=paperci=1", check=False).stdout.decode().split()
    proxy = _docker("ps", "-aq", "--filter", f"name={PROXY_NAME}", check=False).stdout.decode().split()
    victims = [i for i in ids if i not in proxy]
    if victims:
        _docker("rm", "-f", *victims, check=False)
    return len(victims)
