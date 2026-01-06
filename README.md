# GitHub PR Tracker & AI Reviewer (V2)

## Status: 🚧 Re-engineering in Progress

This repository is currently undergoing a complete rewrite. The original prototype has been archived in the [`legacy_prototype/`](./legacy_prototype) directory.

### Project Goals (V2)
The goal of this rewrite is to transition from a monolithic script-based prototype to a modular, production-grade microservices architecture.

**Key Improvements:**
- **Architecture:** Standard Go project layout (`cmd`, `internal`, `pkg`).
- **Database:** Transition from raw SQL strings to ORM (GORM/SQLAlchemy) with proper migrations.
- **Testing:** Comprehensive unit and integration tests.
- **Configuration:** Centralized configuration management.

### Structure
- `cmd/`: Entry points for the applications (Tracker, API, Web UI).
- `internal/`: Private application and library code (Database, GitHub API, AI Logic).
- `pkg/`: Library code allowed to be used by external applications.
- `configs/`: Configuration files and templates.
- `legacy_prototype/`: The original V1 implementation (Archived).

### Getting Started
*Coming soon...*
