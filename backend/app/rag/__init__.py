"""Retrieval-augmented generation (RAG) over the shop's knowledge base.

Pipeline: ``loaders`` -> ``chunking`` -> ``embeddings`` -> ``vector_store``
(ingestion, ``scripts/ingest.py``), then ``retriever`` -> ``reranker`` ->
``context`` (every search). ``factory.build_retriever`` wires them together from
settings. ``docs/rag.md`` explains each step.
"""
