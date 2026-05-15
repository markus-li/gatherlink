fmt:
    cargo fmt
    ruff format python

test:
    cargo test
    pytest

check:
    cargo check
    ruff check python
