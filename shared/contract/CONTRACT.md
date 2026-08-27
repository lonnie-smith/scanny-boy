# Mac app <-> CLI contract

The Swift app invokes the frozen `scanny-boy` binary as a subprocess. This
document is the source of truth for that interface; update it whenever the
CLI's args or output shape change, and update `schema.json` alongside it.

## Invocation

```
scanny-boy <command> [args...]
```

## Commands

### `scan <path>`

Scans `path` and reports the result.

**stdout** (on success and on failure): a single line of JSON matching
`schema.json`'s `ScanResult`.

**Exit code**: `0` if `ok` is `true`, `1` otherwise. Non-JSON stderr output
may contain diagnostic/log text and should not be parsed.

## Conventions

- All machine-readable output goes to stdout as one JSON object per line.
- Human-readable logs/diagnostics go to stderr.
- Exit code `0` means success; any non-zero code means the app should treat
  the operation as failed, even if stdout parses.
