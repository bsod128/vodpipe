"""`python -m vodpipe` -> same entrypoint as the `vodpipe` console script."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
