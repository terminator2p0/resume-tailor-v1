# Public release review — 2026-10-26

Status: prepared for an initial public GitHub release, with the limits below.
Publication target: [terminator2p0/resume-tailor-v1](https://github.com/terminator2p0/resume-tailor-v1).
This report records the pre-publication checks; see GitHub Actions for subsequent
platform test results.

## Checks completed

| Check | Result |
| --- | --- |
| Fresh Python 3.12 environment on macOS | Runtime and development dependencies installed successfully. |
| Automated regression tests | 82 passed, including a synthetic pipeline that writes DOCX, digest, audits and completion state. |
| Clean public Git archive | Tests pass without local configuration, credentials, personal resume or saved runs. |
| Source checks | Ruff checks and formatting pass. |
| Dependency compatibility | `pip check` passes in the fresh environment. |
| Runtime dependency advisories | `pip-audit -r requirements.txt` checked 35 resolved packages; no known advisories were returned at review time. |
| Windows x86_64 / Python 3.11 | Runtime dependency wheel resolution succeeds; this is not a Windows execution test. |
| Linux x86_64 glibc / Python 3.12 | Runtime dependency wheel resolution succeeds; this is not a Linux execution test. |
| Prospective public files | Checked for private setup files, known local secrets/addresses and machine-specific home paths. |
| Local user setup | Kept outside the public file set; config, secrets and source resume preserved. |

## Issues fixed during review

- Reject conflicting delivery flags before loading credentials or contacting services.
  In particular, `--preview --send-outbox ID` cannot send a message.
- Protect unknown section headings following Skills while retaining bold skill-category lists.
- Reject duplicate YAML/JSON fields instead of silently applying the later value.
- Validate the SMTP environment variable name and explain that the secret belongs
  in `.env`, preventing a repeat of the password-in-variable-name setup error.
- Show the loaded config path and expected SMTP variable during `--check` without
  printing a password.
- Ignore DOCX, EML and resume-audit files even outside the default output folders.
- Add `scripts/check_release.py` and tests for accidental/force-added private files.
- Add read-only GitHub Actions jobs for Python 3.11/3.12 on Ubuntu/macOS/Windows,
  with separate runtime dependency auditing.

## Remaining limits and release decisions

- **License undecided**, intentionally. No LICENSE file was added. Choose usage terms
  before describing the repository as open source.
- **Remote CI pending.** The new GitHub jobs have not run yet. Require them to pass
  before advertising verified Windows/Linux support.
- **Live behavior varies.** Public job-board access, chosen-model quality and SMTP
  provider behavior are not established by mocked tests or dependency audits.
  Start with `--check` and `--preview` on each user's setup.
- **Visual review required.** DOCX structure/package integrity is checked; rendered
  pagination is not automatically certified. Open generated resumes before use.
- **Dependencies evolve.** The vulnerability result is a point-in-time check, not a
  guarantee about future versions or the absence of all vulnerabilities.

## Before committing or pushing

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m unittest discover -s tests -v
python scripts/check_release.py
git status --short
```

Review the files Git will include. Keep private configuration, credentials, resumes
and runtime output excluded. Push source files through Git rather than uploading a
ZIP of the configured local working directory. Follow the README setup steps on a
new machine; every user supplies their own config, DOCX, models and optional email.
