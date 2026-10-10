"""Sandbox isolation self-test: egress allowlist, non-root, no-network mode."""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paperci.sandbox import Sandbox, egress_since, ensure_infra  # noqa: E402

print("infra actions:", ensure_infra())
sb = Sandbox.start("selftest", network="proxy")
try:
    checks = {
        "whoami": sb.exec("id -u").output.strip(),
        "allowed_pypi": sb.exec("curl -s -o /dev/null -w '%{http_code}' https://pypi.org/simple/six/", timeout=60).output.strip(),
        "denied_example": sb.exec("curl -s -o /dev/null -w '%{http_code}' https://example.com/", timeout=60).output.strip(),
        "direct_bypass": sb.exec("curl -s --noproxy '*' -m 10 -o /dev/null -w '%{http_code}' https://example.com/ || echo blocked", timeout=60).output.strip(),
        "pip_install": sb.exec("pip install -q six && python -c 'import six;print(six.__version__)'", timeout=120).exit_code,
        "host_env_leak": sb.exec("env | grep -ci -E 'LITELLM|NEATLOGS|CRUSTDATA|GH_TOKEN|GITHUB_TOKEN' || true").output.strip(),
    }
    time.sleep(1)
    checks["egress"] = egress_since(sb.started_at - 5, sb.ip())
finally:
    sb.remove()
off = Sandbox.start("selftest-offline", network="none")
try:
    checks["offline_curl"] = off.exec("curl -s -m 5 https://pypi.org/ >/dev/null && echo reachable || echo unreachable", timeout=30).output.strip()
finally:
    off.remove()
print(json.dumps(checks, indent=1))
