from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from qdrant_client import QdrantClient
from qdrant_client.http import models

from app.config import settings


class VectorStore:
    def __init__(self) -> None:
        self.client = QdrantClient(url=settings.qdrant_url)
        self.collection = settings.qdrant_collection
        self._openai_client = None
        if settings.openai_api_key:
            import openai

            self._openai_client = openai.OpenAI(api_key=settings.openai_api_key)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        existing = [c.name for c in self.client.get_collections().collections]
        if self.collection in existing:
            info = self.client.get_collection(self.collection)
            current_size = info.config.params.vectors.size
            if current_size != settings.embedding_dim:
                self.client.delete_collection(self.collection)
            else:
                return
        self.client.create_collection(
            collection_name=self.collection,
            vectors_config=models.VectorParams(
                size=settings.embedding_dim,
                distance=models.Distance.COSINE,
            ),
        )

    def _embed(self, text: str) -> list[float]:
        if self._openai_client:
            response = self._openai_client.embeddings.create(
                input=text,
                model=settings.embedding_model,
            )
            return response.data[0].embedding
        # Fallback: deterministic hash embedding padded to embedding_dim
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        return [digest[i % len(digest)] / 255.0 for i in range(settings.embedding_dim)]

    def _embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        if self._openai_client:
            response = self._openai_client.embeddings.create(
                input=texts,
                model=settings.embedding_model,
            )
            return [d.embedding for d in sorted(response.data, key=lambda x: x.index)]
        return [self._embed(t) for t in texts]

    def _point_id(self, key: str) -> int:
        return int(hashlib.md5(key.encode("utf-8")).hexdigest()[:8], 16)

    def upsert_evidence(self, incident_id: str, evidence: dict[str, Any]) -> None:
        serialized = json.dumps(evidence, default=str)
        vector = self._embed(serialized)
        self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=self._point_id(f"evidence:{incident_id}"),
                    vector=vector,
                    payload={
                        "incident_id": incident_id,
                        "doc_type": "evidence",
                        "text": serialized[:5000],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
            ],
        )

    def upsert_triage_result(self, incident_id: str, triage: dict[str, Any]) -> None:
        serialized = json.dumps(triage, default=str)
        vector = self._embed(serialized)
        self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=self._point_id(f"triage:{incident_id}"),
                    vector=vector,
                    payload={
                        "incident_id": incident_id,
                        "doc_type": "triage",
                        "text": serialized[:5000],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
            ],
        )

    def upsert_dbt_docs(self, model_id: str, description: str, columns: dict[str, str]) -> None:
        text = f"Model: {model_id}\nDescription: {description}\nColumns: {json.dumps(columns)}"
        vector = self._embed(text)
        self.client.upsert(
            collection_name=self.collection,
            points=[
                models.PointStruct(
                    id=self._point_id(f"dbt_doc:{model_id}"),
                    vector=vector,
                    payload={
                        "model_id": model_id,
                        "doc_type": "dbt_doc",
                        "text": text[:5000],
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                    },
                )
            ],
        )

    def search(self, query: str, limit: int = 5, doc_type: str | None = None) -> list[dict[str, Any]]:
        vector = self._embed(query)
        query_filter = None
        if doc_type:
            query_filter = models.Filter(
                must=[models.FieldCondition(key="doc_type", match=models.MatchValue(value=doc_type))]
            )
        hits = self.client.search(
            collection_name=self.collection,
            query_vector=vector,
            query_filter=query_filter,
            limit=limit,
        )
        return [{"score": hit.score, **hit.payload} for hit in hits]

    def search_similar_incidents(self, description: str, limit: int = 3) -> list[dict[str, Any]]:
        return self.search(description, limit=limit, doc_type="triage")
