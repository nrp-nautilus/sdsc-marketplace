"""One task of the sweep: simulate a random walk for one seed and estimate the diffusion coefficient."""
import argparse
import json
import time
from pathlib import Path

import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--task", type=int, required=True, help="task index; also the random seed")
ap.add_argument("--out", required=True)
ap.add_argument("--data", default="data/walkers.npy")
ap.add_argument("--steps", type=int, default=200)
ap.add_argument("--walkers", type=int, default=20000)
args = ap.parse_args()

started = time.time()
rng = np.random.default_rng(args.task)
start = np.load(args.data, mmap_mode="r")
pos = np.array(start[rng.choice(len(start), size=args.walkers, replace=False)])
origin = pos.copy()
dt, true_d = 0.01, 0.5 + 0.01 * (args.task % 50)
for _ in range(args.steps):
    pos += rng.normal(scale=np.sqrt(2 * true_d * dt), size=pos.shape)
msd = float(((pos - origin) ** 2).sum(axis=1).mean())
result = {"task": args.task, "true_d": true_d, "estimated_d": msd / (6 * args.steps * dt),
          "seconds": round(time.time() - started, 2)}
Path(args.out).parent.mkdir(parents=True, exist_ok=True)
Path(args.out).write_text(json.dumps(result))
print(json.dumps(result))
