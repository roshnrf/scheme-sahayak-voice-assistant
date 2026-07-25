"""Build FAISS indices (one per language) over data/schemes.json for Phase 3 RAG retrieval.

Uses BAAI/bge-m3 (local, free, no Sarvam API calls - this step costs zero credits) via
LangChain's HuggingFaceEmbeddings. One document per scheme per language, combining
name + ministry + details + benefits + eligibility as the embedded text; the full record
is kept in metadata so a later grounded answer can cite/reference it.
"""

import json
from pathlib import Path

from langchain_community.vectorstores import FAISS
from langchain_core.documents import Document
from langchain_huggingface import HuggingFaceEmbeddings

DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "schemes.json"
INDEX_DIR = Path(__file__).resolve().parent.parent / "data"
EMBEDDING_MODEL = "BAAI/bge-m3"


def build_documents(records: list[dict], lang: str) -> list[Document]:
    docs = []
    for record in records:
        field = record[lang]
        text = "\n".join(
            part
            for part in [
                field.get("name", ""),
                field.get("ministry", ""),
                field.get("details", ""),
                field.get("benefits", ""),
                field.get("eligibility", ""),
            ]
            if part
        )
        if not text:
            continue
        docs.append(Document(page_content=text, metadata={"slug": record["slug"], **record}))
    return docs


def main() -> None:
    records = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    print(f"Loaded {len(records)} schemes")

    print(f"Loading embedding model {EMBEDDING_MODEL} (first run downloads it, ~2GB)...")
    embeddings = HuggingFaceEmbeddings(model_name=EMBEDDING_MODEL)

    for lang in ("hi", "ml"):
        docs = build_documents(records, lang)
        print(f"Building {lang} index over {len(docs)} documents...")
        store = FAISS.from_documents(docs, embeddings)
        out_dir = INDEX_DIR / f"faiss_index_{lang}"
        store.save_local(str(out_dir))
        print(f"  saved to {out_dir}")

    print("Done.")


if __name__ == "__main__":
    main()
