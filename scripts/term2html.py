"""Render a `paperci review` terminal log (ANSI colors) as a terminal-styled HTML page for screenshots.
Paper identity is replaced so the image can be published.
    python scripts/term2html.py runs/logs/live-review.log out.html --hide 2608.25907 "Some Title" owner/repo"""
import hashlib
import html
import re
import sys
from pathlib import Path

src, dst, hide = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[4:] if sys.argv[3:4] == ["--hide"] else []
text = src.read_text(encoding="utf-8", errors="replace")
text = "\n".join(l for l in text.splitlines() if " - neatlogs - " not in l and "https://" not in l)
for h in hide:
    text = text.replace(h, "P-" + hashlib.sha256(h.encode()).hexdigest()[:6] if re.fullmatch(r"\d{4}\.\d{4,5}", h) else "[hidden]")
COL = {"1": "font-weight:700", "2": "color:#8b949e", "31": "color:#ff7b72", "32": "color:#3fb950", "33": "color:#d29922",
       "35": "color:#d2a8ff", "36": "color:#79c0ff"}
out, open_spans = [], 0
for part in re.split(r"(\x1b\[[0-9;]*m)", html.escape(text)):
    m = re.fullmatch(r"\x1b\[([0-9;]*)m", part)
    if not m:
        out.append(part)
    elif m.group(1) in ("0", ""):
        out.append("</span>" * open_spans)
        open_spans = 0
    else:
        out.append(f'<span style="{";".join(COL.get(c, "") for c in m.group(1).split(";"))}">')
        open_spans += 1
out.append("</span>" * open_spans)
dst.write_text(f"""<!doctype html><meta charset="utf-8"><style>
body{{margin:0;background:#010409;padding:24px}}.t{{background:#0d1117;border:1px solid #30363d;border-radius:10px;max-width:1180px;
margin:auto;box-shadow:0 12px 40px #0008}}.bar{{padding:10px 14px;border-bottom:1px solid #30363d;color:#8b949e;font:12px system-ui}}
.bar i{{display:inline-block;width:11px;height:11px;border-radius:50%;margin-right:6px;background:#ff5f57}}.bar i+i{{background:#febc2e}}
.bar i+i+i{{background:#28c840;margin-right:14px}}pre{{margin:0;padding:16px 18px;color:#e6edf3;font:13px/1.5 ui-monospace,Consolas,monospace;
white-space:pre-wrap}}</style><div class=t><div class=bar><i></i><i></i><i></i>python -m paperci review</div><pre>{''.join(out)}</pre></div>""",
               encoding="utf-8")
print("wrote", dst)
