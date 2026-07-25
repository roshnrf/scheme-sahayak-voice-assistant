"""Standalone RAG test - no voice. Phase 3 spec: verify grounded answers + confidence
threshold behave correctly before wiring into the Pipecat voice pipeline (Phase 4).

Usage:
    python scripts/test_rag.py --lang hi
    python scripts/test_rag.py --lang ml
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))

import rag  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone RAG test")
    parser.add_argument("--lang", choices=["hi", "ml"], required=True)
    args = parser.parse_args()

    print(f"Confidence threshold: {rag.CONFIDENCE_THRESHOLD}")
    print(f"Type a query in {'Hindi' if args.lang == 'hi' else 'Malayalam'}, or 'quit'.\n")

    while True:
        query = input("> ").strip()
        if not query or query.lower() in {"quit", "exit"}:
            break

        result = rag.answer(query, args.lang)
        if result.grounded:
            print(f"  [grounded, confidence={result.confidence:.4f}, source={result.source_slug}]")
        else:
            print(f"  [NOT CERTAIN, confidence={result.confidence:.4f}, no LLM call made]")
        print(f"  {result.answer}\n")


if __name__ == "__main__":
    main()
