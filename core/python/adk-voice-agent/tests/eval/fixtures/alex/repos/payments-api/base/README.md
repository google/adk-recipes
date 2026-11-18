# payments-api

Lumen Pay's payments service: card processing fees and refunds.

## Tests

Standard library only:

```bash
python3 -m unittest discover -s tests -t .
```

CI runs the same command on every merge to `main`; the last run's log is in `ci/last-run.txt`.
