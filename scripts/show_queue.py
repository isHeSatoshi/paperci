"""Print the reproduction queue (newest first) for a split, excluding papers already done for a label."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from paperci.cli import _queue  # noqa: E402

split, label, n = sys.argv[1], sys.argv[2], int(sys.argv[3])
print(" ".join(_queue(split, n, None, label)))
