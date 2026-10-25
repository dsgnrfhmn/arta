# CLAUDE.md — arta

Standing rules for anyone (people or agents) changing this repository. What arta
is and how to run it: README.md.

## This repository is public

- No real configuration, key, token, personal data or artwork in the tree, ever.
  Real values live in `~/.config/arta/` and in `server.data_dir`, both outside
  the repository. Test and demo data are made up (`tests/fixtures/`).
- `.gitignore` is hygiene, not a control. The control is that nothing private is
  written into the tree, plus the pre-commit hook (`.githooks/pre-commit`:
  gitleaks and a private denylist, fail closed) and gitleaks in CI.
- Never paste the content of a credential or the denylist into a session, a log,
  an error message or a commit. Name the path and the permissions instead.

## Demo

- `python -m demo` runs the dashboard on `demo/`: made-up records and texts in
  JSON, pictures drawn by `demo/paint.py` into the demo's data folder. No image
  file is ever committed for the demo, and nothing in it is a real person's work.
- `demo/config.toml` uses only the `"demo"` sources and the mock client;
  `demo/__main__.py` refuses to start otherwise. `tests/test_demo.py` keeps
  every page of the demo working as the code changes.
- Everything in the repository is in English, demo data included.
- README screenshots come only from `scripts/demo_screenshots.py` (demo data, headless
  browser), never from a hand-made screenshot of a real session or a real browser window.

## Configuration

- One TOML file: `$ARTA_CONFIG`, else `~/.config/arta/config.toml`
  (`core/config.py`). No settings from environment variables.
- Every key must be present; missing or unknown keys stop startup. No silent
  defaults: an optional value is `""` or `[]`.
- Each kind of content (`[intake]`, `[updates]`, `[projects]`, `[cv]`) names its
  own source; nothing follows anything else, and nothing falls back to the test
  fixtures. A setting that belongs to a source that is not chosen is refused,
  because a value without effect would only look configured.
- A new setting goes into `core/config.py` (`SCHEMA` and `parse`) and
  `config.example.toml` in the same change. `tests/test_config.py` parses the
  example, so the two cannot drift.
- Secrets are separate files referenced by path, mode 600, outside the
  repository, read where they are used (`core.config.read_secret`), never kept in
  `Config`.

## Credentials and external services

- One dedicated Google account, read-only scopes only (Gmail read, Drive read).
  Nothing in arta sends mail or writes to Drive. If a write scope ever looks
  necessary, that is a design question to raise, not a scope to add.
- Intake deduplication uses a local seen-set, not mailbox flags.
- A missing, revoked or too-open token fails closed; nothing falls back to a
  cache or to mock data while pretending to be live.
- The Claude API runs on the mock (`llm.mode = "mock"`) unless the configuration
  says `live` and names a key file and a model. Mock output is labelled mock and
  is never presented as a real analysis.

## Untrusted input

Open-call emails and web listings come from strangers:

- Sanitize markup; delimit email bodies as data in prompts and say so in the
  system prompt (prompt injection).
- Analysis output fills fields. It never triggers an action, selects a tool or
  decides to spend money.
- Links from emails open with `rel="noopener noreferrer"` in a new tab and are
  never fetched automatically.
- Drive file names and description files are user content, treated the same way.
- Log message identifiers, never message bodies.

## The dashboard server

- Binds `127.0.0.1` only (`core.config.HOST`); there is no setting for it.
- Host and Origin checks live in one place, `_dispatch` in `web/server.py`,
  before any route or file is looked at. Handlers never re-implement them.
- No CORS headers. Security headers and the CSP come from `web/static.py`, the
  one source for the dashboard and the PDF print server.
- Pages load every script from a file: the CSP allows no inline script.

## Design system

`dis-system/` is a git submodule (github.com/dsgnrfhmn/dis-system). Pages use
only its `var(--ds-*)` tokens and components; page `<style>` blocks hold layout
only. A change that belongs in the design system is made there, not patched
locally. Both themes, light and dark, are required.

## Tests

`python -m pytest` from the repository root. Behaviour changes come with tests;
security checks also get a test that fails when the check is removed.
