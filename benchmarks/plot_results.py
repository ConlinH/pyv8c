"""Render lifecycle measurements; matplotlib is an optional dependency."""
import argparse
import json
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="lifecycle JSON report")
    parser.add_argument("--output-prefix", type=Path, required=True,
                        help="output path without .png or .svg suffix")
    args = parser.parse_args()

    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    # Accept either a direct lifecycle report or a unified benchmark.py output.
    if "samples" in payload:
        samples = payload["samples"]
    else:
        cases = payload.get("cases", {})
        result = cases.get("lifecycle_pages", {}).get("result") or {}
        report = result.get("report") or {}
        samples = report.get("samples")
        if not samples:
            raise SystemExit("no lifecycle samples found in " + str(args.input))
    rounds = [sample["created"] for sample in samples]
    figure, axes = plt.subplots(2, 1, figsize=(8, 6), sharex=True, layout="constrained")
    axes[0].plot(rounds, [None if sample["private_bytes"] is None else sample["private_bytes"] / 2**20
                          for sample in samples], marker="o")
    axes[0].set_ylabel("Process private memory (MiB)")
    axes[0].set_title("Page create/close: periodic collection")
    axes[1].plot(rounds, [sample["python_bytes_delta"] / 1024 for sample in samples],
                 marker="o", color="tab:green")
    axes[1].set_ylabel("Python allocation delta (KiB)")
    axes[1].set_xlabel("Completed page lifecycles")
    for axis in axes:
        axis.grid(alpha=0.25)
    args.output_prefix.parent.mkdir(parents=True, exist_ok=True)
    figure.savefig(str(args.output_prefix) + ".png", dpi=160)
    figure.savefig(str(args.output_prefix) + ".svg")
    plt.close(figure)


if __name__ == "__main__":
    main()
