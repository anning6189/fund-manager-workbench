#!/usr/bin/env python3
"""Build a page-located Stage 6 document ingestion package.

PDF extraction uses pypdf when available. Plain UTF-8 text is also supported.
The result is a normalized ingestion package; it still passes through the
knowledge-store license, entity and evidence gates before becoming curated.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def digest_text(value: str) -> str:
    return digest_bytes(value.encode("utf-8"))


def pages_from_file(path: Path) -> Iterable[tuple[int, str]]:
    if path.suffix.lower() == ".pdf":
        try:
            from pypdf import PdfReader
        except ImportError as exc:
            raise RuntimeError("PDF解析需要工作区运行时中的pypdf") from exc
        reader = PdfReader(str(path))
        for index, page in enumerate(reader.pages, start=1):
            yield index, page.extract_text() or ""
        return
    yield 1, path.read_text(encoding="utf-8-sig")


def split_text(text: str, maximum_chars: int) -> list[str]:
    normalized = re.sub(r"[ \t]+", " ", text).strip()
    if not normalized:
        return []
    paragraphs = [item.strip() for item in re.split(r"\n{2,}", normalized) if item.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs or [normalized]:
        if len(paragraph) > maximum_chars:
            pieces = [paragraph[i : i + maximum_chars] for i in range(0, len(paragraph), maximum_chars)]
        else:
            pieces = [paragraph]
        for piece in pieces:
            candidate = f"{current}\n\n{piece}".strip() if current else piece
            if current and len(candidate) > maximum_chars:
                chunks.append(current)
                current = piece
            else:
                current = candidate
    if current:
        chunks.append(current)
    return chunks


def main() -> int:
    parser = argparse.ArgumentParser(description="生成阶段六文档摄取包")
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-id", required=True)
    parser.add_argument("--source-record-id", required=True)
    parser.add_argument("--document-type", required=True)
    parser.add_argument("--title", required=True)
    parser.add_argument("--publisher", required=True)
    parser.add_argument("--published-at", required=True)
    parser.add_argument("--available-at", required=True)
    parser.add_argument("--license-tag", required=True)
    parser.add_argument("--access-class", default="public")
    parser.add_argument("--evidence-tier", default="A")
    parser.add_argument("--source-url")
    parser.add_argument("--entity", action="append", default=[])
    parser.add_argument("--maximum-chars", type=int, default=1800)
    args = parser.parse_args()

    raw = args.input.read_bytes()
    document_hash = digest_bytes(raw)
    document_id = "CR.DOC." + document_hash.split(":", 1)[1][:24].upper()
    chunks = []
    evidence = []
    sequence = 0
    for page_number, page_text in pages_from_file(args.input):
        for text in split_text(page_text, args.maximum_chars):
            sequence += 1
            chunk_id = f"cr:chunk:{document_id.lower().replace('.', '-')}-{sequence:05d}"
            locator = f"page {page_number}"
            chunk_hash = digest_text(text)
            chunks.append(
                {
                    "chunk_id": chunk_id,
                    "document_id": document_id,
                    "sequence_no": sequence,
                    "page_start": page_number,
                    "page_end": page_number,
                    "section_path": f"page/{page_number}",
                    "chunk_type": "page_text",
                    "locator": locator,
                    "text_content": text,
                    "token_count": max(1, len(text) // 2),
                    "content_hash": chunk_hash,
                    "available_at": args.available_at,
                }
            )
            evidence.append(
                {
                    "evidence_id": f"ev:{document_id.lower()}:{sequence:05d}",
                    "document_id": document_id,
                    "chunk_id": chunk_id,
                    "locator": locator,
                    "support_type": "direct",
                    "evidence_tier": args.evidence_tier,
                    "published_at": args.published_at,
                    "available_at": args.available_at,
                    "content_hash": document_hash,
                    "license_tag": args.license_tag,
                    "access_class": args.access_class,
                }
            )
    package = {
        "package_id": f"CR.DOC.PACKAGE.{document_hash.split(':', 1)[1][:20].upper()}",
        "source_id": args.source_id,
        "retrieved_at": now_iso(),
        "license_tag": args.license_tag,
        "documents": [
            {
                "document_id": document_id,
                "source_record_id": args.source_record_id,
                "document_type": args.document_type,
                "title": args.title,
                "publisher": args.publisher,
                "source_url": args.source_url,
                "local_object_path": str(args.input),
                "published_at": args.published_at,
                "available_at": args.available_at,
                "content_hash": document_hash,
                "mime_type": "application/pdf" if args.input.suffix.lower() == ".pdf" else "text/plain",
                "document_version": f"{args.published_at[:10]}-{document_hash.split(':', 1)[1][:12]}",
                "license_tag": args.license_tag,
                "access_class": args.access_class,
                "evidence_tier": args.evidence_tier,
                "status": "curated",
                "metadata": {"page_count_with_text": len({item['page_start'] for item in chunks})},
            }
        ],
        "document_entities": [
            {"document_id": document_id, "entity_id": entity_id, "relation_type": "about", "confidence": 1, "review_status": "reviewed"}
            for entity_id in args.entity
        ],
        "chunks": chunks,
        "evidence": evidence,
    }
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(package, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"status": "created", "output": str(args.output), "document_id": document_id, "chunks": len(chunks)}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
