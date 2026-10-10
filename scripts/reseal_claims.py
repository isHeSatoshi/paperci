"""Re-validate sealed claims with the stricter grounding check (no LLM cost).
Skips any paper that already has a build stage, so running builds are unaffected."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paperci import db, sources  # noqa: E402
from paperci.schemas import Claim  # noqa: E402
from paperci.stages.claims import grounded, seal, targets  # noqa: E402

rows = db.q("SELECT arxiv_id, payload FROM sealed_claims WHERE arxiv_id NOT IN "
            "(SELECT arxiv_id FROM stages WHERE stage='build')")
for r in rows:
    claims = [Claim.model_validate(c) for c in json.loads(r["payload"])]
    if not claims:
        continue
    text, src = sources.arxiv_fulltext(r["arxiv_id"])
    kept = [c for c in claims if grounded(c, text)]
    for i, c in enumerate(kept, 1):
        c.claim_id = f"c{i}"
    digest = seal(r["arxiv_id"], kept)
    st = db.get_stage(r["arxiv_id"], "claims", "shared")
    out = json.loads(st["output"])
    out.update(sealed_sha256=digest, n_claims=len(kept), n_dropped=out.get("n_dropped", 0) + len(claims) - len(kept),
               targets=[t.model_dump() for t in targets(kept)], grounding="quote-v2")
    db.save_stage(r["arxiv_id"], "claims", "shared", output=out)
    db.log_event(r["arxiv_id"], "shared", "reseal", {"before": len(claims), "after": len(kept)})
    print(r["arxiv_id"], len(claims), "->", len(kept), src, flush=True)
