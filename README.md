# OPA Database

A research database for Fortaleza's public transit data. It turns fare
collection records, vehicle GPS positions, schedules, and reference lists
into a complete, trustworthy, queryable account of what every bus did.

## Status

The system is being built to a written specification.

| Where | What |
| --- | --- |
| [`docs/spec/`](docs/spec/README.md) | The specification. It is the source of truth for all new work. |
| `src/`, `ml/`, `tools/` | The previous implementation. It stays in the tree until the first phase of the roadmap replaces it, and it is not a design input. |
| [`docs/archive/`](docs/archive/README.md) | Documentation of the previous implementation, kept for reference. |

## Development

The only prerequisite is [uv](https://docs.astral.sh/uv/).

```bash
uv sync                        # install the pinned environment
uv run prek install            # install the pre-commit hooks
uv run ruff format --check .   # formatting
uv run ruff check .            # linting
uv run ty check                # type checking
uv run pytest                  # tests
uv run prek run --all-files    # every hook, as continuous integration runs them
```

Python is always run through `uv run`, and dependencies are changed with
`uv add` and `uv remove`.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md). The full standards are in
[`docs/spec/13-engineering.md`](docs/spec/13-engineering.md) and
[`docs/spec/14-delivery.md`](docs/spec/14-delivery.md).

## License

[MIT](LICENSE)
