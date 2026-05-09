# AGENTS.md

## Project
- Django 5.2.13 + CLI tool with Python 3.11
- Package manager: `uv`
- Entry point: `src/pat-sig/main.py` (Django), `pat-sig` (CLI)

## Structure
- `cli/` - CLI package (installed as `pat-sig` command)
- `src/pat-sig/` - Django project root
- `src/pat-sig/common/`, `module/`, `view/` - Django apps (empty)

## Commands
- Install Django deps: `uv sync` (deps in `.venv`)
- Install CLI: `pip install -e cli/`
- Run CLI: `pat-sig`
- Create Django app: `django-admin startproject pat_sig .` (from src/)

## Notes
- No tests configured yet
- No CI workflows yet