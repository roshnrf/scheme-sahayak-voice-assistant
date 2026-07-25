"""Standalone text-based retrieval test - no voice. Phase 2 spec: verify retrieval quality
in both languages before wiring FAISS into the Pipecat voice pipeline (that's Phase 3/4).

Usage:
    python scripts/test_retrieval.py --lang hi
    python scripts/test_retrieval.py --lang ml
"""

import argparse
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_huggingface import HuggingFaceEmbeddings

INDEX_DIR = Path(__file__).resolve().parent.parent / "data"
EMBEDDING_MODEL = "BAAI/bge-m3"
TOP_K = 3


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone FAISS retrieval test")
    parser.add_argument("--lang", choices=["hi", "ml"], required=True)
    args = parser.parse_args()

    index_path = INDEX_DIR / f"faiss_index_{args.lang}"
    if not index_path.exists():
        raise SystemExit(f"No index at {index_path} - run scripts/build_index.py first")

    print(f"Loading {EMBEDDING_MODEL} and the {args.lang} index...")
    embeddings = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)
    store = FAISS.load_local(str(index_path), embeddings, allow_dangerous_deserialization=True)

    print(f"Ready. Type a query in {'Hindi' if args.lang == 'hi' else 'Malayalam'}, or 'quit'.\n")
    while True:
        query = input("> ").strip()
        if not query or query.lower() in {"quit", "exit"}:
            break

        results = store.similarity_search_with_relevance_scores(query, k=TOP_K)
        if not results:
            print("  (no results)")
            continue

        for rank, (doc, score) in enumerate(results, start=1):
            record = doc.metadata
            name = record[args.lang]["name"] or record["en"]["name"]
            print(f"  {rank}. {name}  [confidence={float(score):.4f}, slug={record['slug']}]")
        print()


if __name__ == "__main__":
    main()
