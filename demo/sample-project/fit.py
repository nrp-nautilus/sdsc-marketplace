"""Collect the per-task results and report how well the estimates match."""
import argparse
import json
from pathlib import Path

import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--results", default="results")
ap.add_argument("--out", default="results/summary.json")
args = ap.parse_args()

rows = [json.loads(p.read_text()) for p in sorted(Path(args.results).glob("task-*.json"))]
err = np.array([r["estimated_d"] / r["true_d"] - 1 for r in rows])
summary = {"tasks": len(rows), "mean_relative_error": float(err.mean()), "rms_relative_error": float(np.sqrt((err**2).mean())),
           "cpu_seconds": round(sum(r["seconds"] for r in rows), 1)}
Path(args.out).write_text(json.dumps(summary, indent=2))
print(json.dumps(summary, indent=2))
