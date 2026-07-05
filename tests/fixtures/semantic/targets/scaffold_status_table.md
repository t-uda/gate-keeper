# Scaffold status

Vocabulary:

- **functional** — the module implements its MVP responsibility.
- **deferred** — the module is a no-op or fake-result placeholder.

| module | status | note |
| --- | --- | --- |
| `src/pkg/parser.py` | functional | extracts rules from Markdown |
| `src/pkg/router.py` | functional | routes rules to backends |
| `src/pkg/transfer.py` | deferred | stub upload returns a fake URI |
