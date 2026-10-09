## What this changes and why

<!-- One or two sentences. Link the issue this closes, if there is one. -->

## How it was tested

<!-- Which tests you added or updated, and the exact command you ran. -->

```bash
.venv/bin/python -m pytest -q
```

## Checklist

- [ ] The core engine still has no runtime dependency outside the Python
      standard library (or the new one is behind an optional extra).
- [ ] Tests run against a temporary wiki, not `~/.shapa` or a real repo's
      `.shapa/`.
- [ ] Nothing in this diff adds a secret, a client name, another project's
      details, or private wiki content to a public file.
- [ ] `shapa.db`'s commit rule (default branch only) is unchanged, or the
      change to it is covered by a test.
- [ ] The full suite passes locally on the core install and on
      `[semantic,mcp]`.
