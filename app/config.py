from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    database_url: str = os.getenv("DATABASE_URL", "postgresql://dra:dra@localhost:5433/dra")
    qdrant_url: str = os.getenv("QDRANT_URL", "http://localhost:6333")
    qdrant_collection: str = os.getenv("QDRANT_COLLECTION", "dra_incidents")
    mock_llm: bool = os.getenv("MOCK_LLM", "true").lower() == "true"

    # M1: LLM Agent
    anthropic_api_key: str = os.getenv("ANTHROPIC_API_KEY", "")
    anthropic_model: str = os.getenv("ANTHROPIC_MODEL", "claude-opus-4-6")

    # M2: Embeddings
    openai_api_key: str = os.getenv("OPENAI_API_KEY", "")
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "text-embedding-3-small")
    embedding_dim: int = int(os.getenv("EMBEDDING_DIM", "1536"))

    # M3: GitHub
    github_token: str = os.getenv("GITHUB_TOKEN", "")
    github_repo: str = os.getenv("GITHUB_REPO", "")
    github_base_branch: str = os.getenv("GITHUB_BASE_BRANCH", "main")

    # M4: Slack
    slack_bot_token: str = os.getenv("SLACK_BOT_TOKEN", "")
    slack_channel_id: str = os.getenv("SLACK_CHANNEL_ID", "")

    # M5: dbt Validation
    dbt_project_dir: str = os.getenv("DBT_PROJECT_DIR", "")
    dbt_profiles_dir: str = os.getenv("DBT_PROFILES_DIR", "")


settings = Settings()
