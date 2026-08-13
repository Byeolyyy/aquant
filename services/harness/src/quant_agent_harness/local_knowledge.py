from __future__ import annotations

import hashlib
import math
import re
from typing import Iterable

from .models import EvidenceItem
from .repository import Repository


class LocalHashingEmbedder:
    """Dependency-free local demo embedder.

    This validates the ingestion/vector-storage/retrieval architecture without
    claiming neural semantic quality. It can later be replaced by Ollama or a
    SentenceTransformers provider while keeping the index contract unchanged.
    """

    model_name = "local-hashing-v1"

    def __init__(self, dimensions: int = 256):
        self.dimensions = dimensions

    def embed(self, text: str) -> list[float]:
        normalized = re.sub(r"\s+", " ", text.lower()).strip()
        features = _features(normalized)
        vector = [0.0] * self.dimensions
        for feature in features:
            digest = hashlib.blake2b(feature.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest, "big") % self.dimensions
            vector[index] += 1.0
        norm = math.sqrt(sum(value * value for value in vector))
        return [value / norm for value in vector] if norm else vector


class LocalKnowledgeIndex:
    def __init__(self, repository: Repository, embedder: LocalHashingEmbedder | None = None):
        self.repository = repository
        self.embedder = embedder or LocalHashingEmbedder()

    def index_evidence(self, evidence: Iterable[EvidenceItem]) -> int:
        inserted = 0
        for item in evidence:
            content = "\n".join(part for part in (item.title, item.excerpt) if part).strip()
            if not content:
                continue
            content_hash = hashlib.sha256(
                f"{item.url}\n{content}".encode("utf-8")
            ).hexdigest()
            document_id = "doc-" + content_hash[:24]
            chunk_texts = _chunk_text(content)
            chunks = [
                {
                    "chunk_id": f"{document_id}-{index}",
                    "chunk_index": index,
                    "chunk_text": chunk,
                    "embedding": self.embedder.embed(chunk),
                    "embedding_model": self.embedder.model_name,
                }
                for index, chunk in enumerate(chunk_texts)
            ]
            if self.repository.upsert_knowledge_document(
                {
                    "document_id": document_id,
                    "content_hash": content_hash,
                    "title": item.title,
                    "content": content,
                    "url": item.url,
                    "source_type": item.source_type,
                    "published_at": item.published_at,
                    "symbols": item.symbols,
                    "retrieved_at": item.retrieved_at,
                },
                chunks,
            ):
                inserted += 1
        return inserted

    def search(self, query: str, *, symbols: list[str], limit: int = 4) -> list[EvidenceItem]:
        query_vector = self.embedder.embed(query)
        query_features = set(_features(query.lower()))
        rows = self.repository.knowledge_candidates()
        scored = []
        for row in rows:
            stored_symbols = [str(value) for value in row["symbols"]]
            symbol_match = bool(set(symbols).intersection(stored_symbols))
            lexical_features = set(_features(str(row["chunk_text"]).lower()))
            overlap = len(query_features.intersection(lexical_features)) / max(1, len(query_features))
            cosine = sum(float(a) * float(b) for a, b in zip(query_vector, row["embedding"]))
            score = cosine * 0.65 + overlap * 0.25 + (0.10 if symbol_match else 0.0)
            if score > 0.08:
                scored.append((score, row))
        best_by_document: dict[str, tuple[float, dict]] = {}
        for score, row in sorted(scored, key=lambda item: item[0], reverse=True):
            document_id = str(row["document_id"])
            if document_id not in best_by_document:
                best_by_document[document_id] = (score, row)
        results = []
        for score, row in list(best_by_document.values())[:limit]:
            results.append(
                EvidenceItem(
                    source_type="local_history",
                    title="本地市场记忆 · " + str(row["title"]),
                    excerpt=f"混合检索分数 {score:.3f}。{str(row['chunk_text'])[:900]}",
                    url=str(row["url"] or ""),
                    published_at=str(row["published_at"] or ""),
                    symbols=[str(value) for value in row["symbols"]],
                )
            )
        return results


def _chunk_text(text: str, size: int = 700, overlap: int = 100) -> list[str]:
    if len(text) <= size:
        return [text]
    chunks = []
    start = 0
    while start < len(text):
        chunks.append(text[start : start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return chunks


def _features(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9.]+|[\u4e00-\u9fff]", text)
    compact = "".join(token for token in tokens if re.fullmatch(r"[\u4e00-\u9fff]", token))
    features = [token for token in tokens if not re.fullmatch(r"[\u4e00-\u9fff]", token)]
    features.extend(compact[index : index + 2] for index in range(max(0, len(compact) - 1)))
    features.extend(compact[index : index + 3] for index in range(max(0, len(compact) - 2)))
    return features
