## Restaurant Assistant

Python backend + SPA frontend application that helps users choose a restaurant, book a table, and place a pre-order via an LLM-driven dialog and a configurable graph of steps.

- **Backend**: FastAPI, LangGraph/LangChain-based graph engine, PostgreSQL for session and graph state, Toka (Evotor Horeca) Backoffice API adapter, Cloud.ru Foundation Models (OpenAI-compatible API).
- **Frontend**: React SPA with a mobile-first layout, communicating with the backend over REST.

This repository is structured to support horizontal scaling in the future (stateless API instances, shared PostgreSQL).

