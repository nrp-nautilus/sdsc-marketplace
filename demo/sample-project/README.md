# diffusion-sweep (demo project)

A stand-in for "my research code": a random-walk diffusion simulation that estimates a diffusion
coefficient for one seed, and a script that fits the results. CPU only, needs numpy.

```bash
pip install -r requirements.txt
python make_dataset.py --mb 40                 # writes data/walkers.npy (initial positions)
python run_task.py --task 0 --out results/task-0.json
python fit.py --results results --out results/summary.json
```
