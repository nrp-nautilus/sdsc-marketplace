"""Write the input the simulation starts from: initial walker positions (float64, ~--mb megabytes)."""
import argparse
from pathlib import Path

import numpy as np

ap = argparse.ArgumentParser()
ap.add_argument("--mb", type=int, default=40, help="size of the file to write")
ap.add_argument("--out", default="data/walkers.npy")
args = ap.parse_args()

walkers = args.mb * 1024 * 1024 // (8 * 3)
Path(args.out).parent.mkdir(parents=True, exist_ok=True)
np.save(args.out, np.random.default_rng(0).normal(size=(walkers, 3)))
print(f"wrote {args.out}: {walkers:,} walkers, {Path(args.out).stat().st_size / 2**20:.0f} MiB")
