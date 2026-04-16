"""Subprocess entry point for ingestion.

Isolates pyannote/Whisper memory from the long-running API server so an
OOM during diarization can't take the web UI down with it.
"""
import argparse
import json
import sys

from pep_oracle.ingest import ingest_all


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--diarize", action="store_true")
    parser.add_argument("--episode", type=int, action="append", default=[])
    args = parser.parse_args()

    def emit(step: str) -> None:
        print(f"PROGRESS: {step}", flush=True)

    try:
        result = ingest_all(
            force=args.force,
            confirm_cost=False,
            episode_numbers=args.episode or None,
            diarize=args.diarize,
            progress_callback=emit,
        )
    except Exception as e:
        print(f"ERROR: {e}", flush=True)
        return 1

    print(f"RESULT: {json.dumps(result)}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
