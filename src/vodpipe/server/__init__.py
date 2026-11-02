"""Queue + HTTP layer of vodpipe.

Split so the core stays dependency-free: `vodpipe` alone needs no FastAPI or
Celery, which is why the tests and a notebook can drive the pipeline directly.
This subpackage is loaded only when running the API or a worker
(`pip install "vodpipe[server]"`).
"""
