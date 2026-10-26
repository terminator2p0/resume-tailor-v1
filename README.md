# Local Job Finder & Resume Tailorer

Search configurable job boards, tailor copies of your DOCX resume with local
Ollama models, and save results locally or send a digest through your SMTP provider.
Use any profession, supported country, preferred locations and experience levels.
No source-code edits or database are needed. Applications remain your decision.

## Quickstart

Use Python 3.11 or 3.12 and install [Ollama](https://ollama.com/download).
Clone this repository and open its directory:

```sh
git clone https://github.com/terminator2p0/resume-tailor-v1.git
cd resume-tailor-v1
```

Then:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
ollama pull llama3.1:8b
```

On Windows, use `py -3.12 -m venv .venv`, `.venv\Scripts\Activate.ps1`, and
`Copy-Item config.example.yaml config.yaml` instead of the corresponding shell commands.

1. Put your own resume at `templates/base_resume.docx` or update `resume.base_path`.
2. Edit `config.yaml`: titles, country, locations, enabled sites and local model names.
3. Start Ollama (open its application or run `ollama serve` in another terminal).
4. Validate, preview, then run:

```sh
python main.py --check
python main.py --preview
python main.py
```

Email is disabled in the example. To enable it, configure your SMTP host, sender,
recipient, TLS mode and port, copy `.env.example` to `.env`, and set your provider's
app password there. Set `email.enabled: true`. **Normal runs then send automatically.**
`--preview` always suppresses sending. `--sample` uses explicitly fictitious jobs
and cannot send; it still needs your DOCX and local models.

For Gmail, follow the [Gmail setup steps](REQUIREMENTS.md#gmail-setup), including
2-Step Verification, an app password, and a preview before your first send.

`resume.factual_review_mode` defaults to `strict`, which requires source support.
Choose `advisory` to allow unverified edits with warnings for manual review.
Both modes enforce protected content, document structure and content-quality checks.

See [REQUIREMENTS.md](REQUIREMENTS.md) for hardware sizing, full installation,
configuration, execution modes, resume layouts and troubleshooting.

## Layout

```text
config.example.yaml          Documented generic setup
config_loader.py             YAML/JSON + .env/environment loader and validation
main.py                      Local command-line orchestrator
jobfinder/
  config/models.py           Pydantic configuration models
  agents/                    Search, employer, keyword, resume and notification agents
  scrapers/sources.py         Isolated job-board and public employer/ATS adapters
  llm.py                     Local-only Ollama client
  models.py                  Job records
  resume_layout.py           Dynamic body/table DOCX discovery
  batch.py, state.py          Selection, retries, locking and file-based state
skills/                      Explicit agent instructions
examples/sample_jobs.json    Fictitious postings only
templates/                   Your source DOCX + editable notification templates
data/jobs/{run_id}/          Raw/enriched job JSON; data/state.json tracks completion
logs/                        Run reports and keyword/tailoring attempt records
output/tailored_resumes/     New DOCX revisions and their audit files
output/outbox/               Digest text, .eml files and delivery manifests
tests/                       Synthetic, offline regression suite
```

Configuration, credentials, personal DOCX files and generated artifacts are ignored
by Git. Keep custom output directories outside source control too. The source resume
is read-only to the pipeline; every tailored revision gets a separate path.

## Public release checks

See [RELEASE_REVIEW.md](RELEASE_REVIEW.md) for the completed checks and remaining limits.

Before publishing, run:

```sh
python scripts/check_release.py
python -m unittest discover -s tests -v
```

The release checker inspects the files Git would include and flags private files,
known local secrets, personal email addresses and home-directory paths without
printing their values. It also catches accidentally force-added runtime files.
It is a focused safeguard, not a guarantee that all possible secrets are detected.
Do not upload the whole working folder as a ZIP; publish Git-selected files only.

GitHub Actions is configured to test Python 3.11/3.12 on macOS, Linux and Windows,
plus audit runtime dependencies. Those remote jobs must pass after publication;
local macOS checks alone do not establish support on every operating system.

No license has been selected yet. Do not describe this repository as open source
until its usage terms are chosen. See [GitHub's licensing explanation](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).

## Development

```sh
python -m pip install -r requirements-dev.txt
python -m ruff check .
python -m unittest discover -s tests -v
```

Tests use synthetic resumes and mocked inference, scraping and SMTP. They send no
email and require no Ollama server. Model output quality, live site availability,
SMTP account acceptance and rendered pagination require local end-to-end review.
