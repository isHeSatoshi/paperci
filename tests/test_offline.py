"""Offline unit checks (no LLM, no Docker): python tests/test_offline.py"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from paperci.pipeline import _apply_spec  # noqa: E402
from paperci.recovery import classify  # noqa: E402
from paperci.schemas import Claim  # noqa: E402
from paperci.stages.claims import grounded, targets, value_in_text  # noqa: E402
from paperci.stages.tasks import extraction_spec  # noqa: E402
from paperci.stages.verifier import candidates, compare, extract, integrity, normalize, verdict  # noqa: E402

LOG = """epoch 1 | train loss 0.91 | test accuracy 0.7012
epoch 2 | train loss 0.55 | test accuracy 0.8123
epoch 3 | train loss 0.41 | test accuracy 0.8650
final: coverage=0.9612 avg_set_size=1.62
"""
arts = {"run.log": LOG, "metrics.json": '{"test": {"rmse": 0.231, "n": 100}}'}


def test_extraction():
    c = Claim(claim_id="c1", metric="test accuracy", value=86.9, unit="%", higher_is_better=True)
    e = extract("x", "t", c, arts)  # deterministic path: one template repeated -> last value
    assert e["method"] == "deterministic" and abs(e["value"] - 0.865) < 1e-9, e
    v, how = normalize(c, e["value"])
    assert abs(v - 86.5) < 1e-9 and how.startswith("x100")
    cmp_ = compare(c, [v])
    assert cmp_["within"], cmp_  # |86.5-86.9| = 0.4 <= 1 point floor for percents
    spec = extraction_spec("c1", e, e["value"])
    assert spec and _apply_spec(spec, arts) == 0.865, spec
    cj = Claim(claim_id="c2", metric="RMSE", value=0.23)
    ej = extract("x", "t", cj, arts)
    assert ej["found"] and ej["value"] == 0.231, ej
    sj = extraction_spec("c2", ej, ej["value"])
    assert sj["type"] == "json" and _apply_spec(sj, arts) == 0.231, sj
    cov = candidates("coverage", arts)
    assert cov and cov[-1]["value"] == 0.9612


def test_verdicts():
    c = Claim(claim_id="c1", metric="accuracy", value=0.95, higher_is_better=True)
    run = [{"exit_code": 0, "copied_from_repo": []}] * 3
    clean = {"flags": [], "egress_denied": {}}
    far = {"c1": {"extraction": {"method": "deterministic"}, "comparison": compare(c, [0.80, 0.81, 0.80])}}
    near = {"c1": {"extraction": {"method": "deterministic"}, "comparison": compare(c, [0.949, 0.951, 0.95])}}
    assert verdict({"status": "built", "scale": "full"}, run, near, clean)[0] == "verified"
    assert verdict({"status": "built", "scale": "full"}, run, far, clean)[0] == "falsified-candidate"
    assert verdict({"status": "built", "scale": "reduced"}, run, near, clean)[0] == "toy-scale"
    assert verdict({"status": "gave_up"}, [], {}, clean)[0] == "blocked-infra"
    assert verdict({"status": "built", "scale": "full"}, run, near, {"flags": ["hardcoded_claim_value"]})[0] == "inconclusive"


def test_integrity(tmp=Path(__file__).parent / "_tmp_build"):
    tmp.mkdir(exist_ok=True)
    (tmp / "paperci_run.sh").write_text("python train.py --epochs 1\necho 'accuracy: 91.37'\n")
    c = Claim(claim_id="c1", metric="accuracy", value=91.37, unit="%")
    r = integrity(tmp, [c], [{"copied_from_repo": ["./results/final.json"]}], {"denied": {"evil.com": 2}})
    assert "hardcoded_claim_value" in r["flags"] and "prebuilt_result_files" in r["flags"], r
    assert r["hardcoded"][0]["literal"] == "[sealed value]"  # never echo the sealed number
    for f in tmp.iterdir():
        f.unlink()
    tmp.rmdir()


def test_claims_and_classifier():
    text = "Our method reaches a test accuracy of 91.37% on CIFAR-10, beating the baseline (88.1%)."
    good = Claim(claim_id="c1", metric="test accuracy", value=91.37, unit="%", quote="reaches a test accuracy of 91.37% on CIFAR-10")
    bad = Claim(claim_id="c2", metric="test accuracy", value=95.0, unit="%", quote="we reach 95.0% accuracy on CIFAR-10 test set")
    assert grounded(good, text) and not grounded(bad, text)
    t = targets([Claim(claim_id="c1", metric="acc", value=10.5, setting="10.5k samples")])[0]
    assert not value_in_text(10.5, t.metric + " " + t.setting), t  # setting that would leak the value is dropped
    assert classify("Traceback (most recent call last):\nModuleNotFoundError: No module named 'x'", 0, "python a.py | tail") == "missing_module"
    assert classify("raise FileNotFoundError(path)", 0, "cat /work/repo/a.py") is None
    assert classify("curl: (56) CONNECT tunnel failed, response 403\nTunnel connection failed: 403 Filtered", 1) == "egress_denied"


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("PASS", name)
