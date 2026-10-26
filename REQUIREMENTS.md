# Installation, configuration and operation

## Scope

This application runs locally: discovery → job eligibility → employer verification
→ evidence-backed keyword planning → DOCX customization → optional email digest.
All inference uses downloaded Ollama models on loopback. Postings, state, audits,
notifications and resume revisions are ordinary files. There is no database,
feedback/learning service, scheduler, cloud-model dependency or application submission.

Web access is required for live discovery, model downloads and SMTP delivery.
Imported postings can replace discovery. Resumes are sent to local Ollama and, when
email is enabled, attached to the configured digest. Keep `.env`, configuration,
source documents and generated output private. The repository contains only generic
examples; no real candidate resume or account is bundled.

## Hardware sizing

These are planning estimates for one quantized model at a time, not measured minimums
or performance guarantees. Quantization, architecture, context, CPU/GPU and other apps
can change requirements substantially.

| Approximate model size | Practical system RAM planning range | Notes |
| --- | --- | --- |
| 3–4B, roughly 4-bit | 8–16 GB | Start with short contexts; structured editing quality may be limited. |
| 7–8B, roughly 4-bit | 16 GB or more | Reasonable starting tier for the generic example. |
| 14B, roughly 4-bit | 24–32 GB | More headroom for the OS and long resumes. |
| 20–32B, roughly 4-bit | 32–64 GB | Depends heavily on architecture and context; reduce context if memory constrained. |
| 70B, roughly 4-bit | 64–96 GB or more | Advanced use; check actual model/runtime memory requirements. |

Weights alone are insufficient for RAM sizing: allow space for the OS, model runtime,
context/KV cache and output. As one reference point, the default
[llama3.1:8b package is approximately 4.9 GB](https://ollama.com/library/llama3.1:8b).
That download size is not its total runtime memory requirement.
[Larger context windows consume more memory](https://docs.ollama.com/context-length).

CPU-only execution can work but may be slow. A supported GPU or Apple unified-memory
acceleration improves speed. Consult [Ollama GPU support](https://docs.ollama.com/gpu)
for current compatibility. `ollama ps` shows the active model's context and offloading.
Keep at least 10–20 GB free disk for a small-model setup, more for multiple or larger
models and accumulated resumes. These disk figures are practical estimates.

The runner invokes inference sequentially. `llm.keep_alive: '0'` unloads the model
after each call, reducing overlap at the cost of reload time. A longer keep-alive can
improve speed when the same model serves all roles, but requires more memory planning.

## Software and operating systems

- Python **3.11 or 3.12** and pip. Development verification used Python 3.12 on macOS.
- Git for cloning/version control.
- [Ollama](https://ollama.com/download) running locally on macOS, Linux or Windows.
  Check the installer documentation for current OS/GPU requirements.
- A Word-compatible editor to inspect and save your DOCX resume. Microsoft Word is
  not needed by the pipeline itself.
- Optional SMTP account permitting password/app-password authentication over TLS.
  Providers requiring OAuth-only login are not supported by this SMTP adapter.
- Network access to enabled boards and public employer pages. No browser driver,
  paid scraper account or database is required.

File locking uses `portalocker` for portability. Windows/Linux support is implemented
but was not exercised on those operating systems in this development environment.
Python dependencies are bounded in `requirements.txt`; use a fresh virtual environment.
The Windows/Linux CI targets are 64-bit x86 systems using standard hosted runners.
Other combinations, including Alpine/musl Linux and Windows ARM, need their own
dependency and runtime checks before being treated as supported.

## Install

Clone your copy of the repository and enter its directory:

```sh
git clone https://github.com/terminator2p0/resume-tailor-v1.git
cd resume-tailor-v1
```

macOS/Linux:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp config.example.yaml config.yaml
```

Windows PowerShell:

```powershell
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt
Copy-Item config.example.yaml config.yaml
```

If activation is unavailable, call `.venv/bin/python` (macOS/Linux) or
`.venv\Scripts\python.exe` (Windows) directly. Install dependencies using that same
interpreter. No step requires modifying Python source.

## Download local models

A generic starting example uses one model for all four roles:

```sh
ollama pull llama3.1:8b
ollama list
ollama serve
```

If the Ollama app already runs the server, do not start another `ollama serve`.
`ollama pull` downloads a model once; normal inference uses its local copy. The
[Ollama library](https://ollama.com/library) lists other models and quantizations.
Select a model with reliable JSON/schema output and adequate context for your resume.
This repository does not guarantee equivalent output quality across models.

Set `llm.models.job_parser`, `keyword_extractor`, `resume_tailorer` and
`resume_reviewer` to your installed model names. You can use one model or different
models per role. Names without a tag resolve to `:latest` for the installation check.
Only loopback server origins are allowed. Cloud-tagged and remotely backed models,
HTTP redirects and model-server proxy routing are rejected.

`llm.num_ctx`, `num_predict`, `max_input_characters`, timeout and per-agent overrides
control resource budgets. Context limits and input-character safeguards are different:
the application never slices source input silently, but character counts do not
exactly predict model token usage. Choose enough context for prompts, source text and
output. Full source context remains available when retrying smaller target groups.
Model reasoning effort is sent only for compatible `gpt-oss` tags; other local models
receive schema output requests without a model-specific effort flag.

## Configure your search

`config.example.yaml` documents every setting. YAML and JSON are supported through
`python main.py --config PATH`. `JOBFINDER_CONFIG` can select another file; explicit
`--config` takes precedence. Relative configuration paths and all relative data,
resume and template paths resolve from the repository root, not the shell directory.

Required sections are `search`, `llm` and `resume`. Required search values are
`job_titles`, `country` and `locations`; four model-role names and `resume.base_path`
are also required. Unknown fields are errors rather than silently ignored typos.
Booleans must be actual booleans, numbers must be numeric, and list fields need lists.
Duplicate YAML/JSON keys are rejected rather than silently replacing earlier settings.

- `job_titles`: any profession, including multiple related titles.
- `country`: the country spelling accepted by your installed JobSpy adapter, such as
  `UK`, `France`, `India`, `Canada` or `USA`. `locations` supply actual geographic
  search strings. The same country applies to all locations in a configuration;
  use separate files for different countries. See the
  [upstream supported-country list and search limitations](https://github.com/speedyapply/JobSpy#supported-countries-for-job-searching).
- `experience_levels`: optional descriptions such as entry level or senior; an empty
  list accepts all. LLM classification checks these against the posting.
- Mandatory experience bounds apply inclusively: an open-ended minimum qualifies
  by that minimum, while a closed range must fit entirely within the band. Preferred
  years are ignored. `keep_unknown_experience` controls unstated years.
- `work_arrangements` select remote, hybrid and/or onsite. `[remote]` also requests
  remote-only board searches. Location eligibility still checks geographic limits
  for remote jobs. Unknown work arrangements/employment types have separate toggles.
- `employment_types` selects full-time, contract, contract-to-hire, part-time,
  internship and/or temporary work.
- `exclusion_keywords` reject case-insensitive substrings in the title/description.
  `tracked_keywords` optionally influence initial ranking. The keyword-planning
  agent is open-ended and can identify relevant terms outside this list.
- Each `search.sites` entry has `enabled` and `request_interval_seconds`. Supported
  adapters: LinkedIn, Indeed, Glassdoor and public company-career pages. Disabled
  sources are not called. All can be disabled when using imported postings.
- Company discovery searches `company_domains` and can fetch individual structured
  posting URLs from `company_urls`. Lever/Greenhouse public APIs and generic
  `JobPosting` JSON-LD pages are supported; not every company site exposes one.
- `require_sponsorship` enables restriction filtering. When false, a sponsorship
  restriction does not by itself exclude a posting. When true, unknown policies
  follow `keep_unknown_sponsorship`. Employer verification can be enabled separately;
  inaccessible/silent pages remain explicitly unverified.

Site calls use bounded subprocesses, timeouts and configured pauses. Company page
fetches are paced too. Board libraries may perform internal requests; the pause is
between search operations, not a guaranteed cap on every internal HTTP request.
Scraping can fail because of rate limits, access blocks or markup changes. Failures
are logged and other sources continue. No CAPTCHA or access-control bypass is used.

`recent_share: 1.0` selects recent jobs only; `0.0` allows any age, including unknown
dates. Fractional values carry rounding across completed batches, prefer recent jobs
and never use older jobs to fill missing recent slots. Smaller/empty batches are
valid when supply is insufficient. Batch and candidate limits are configurable;
attachment-size and model-resource budgets still apply.

## Supply and inspect your resume

Place your DOCX in `templates/` and set `resume.base_path`. A real resume is deliberately
not bundled. The source must exist, be a DOCX package and remain outside generated
`data`, `logs` and `output` directories.

```sh
python main.py --inspect-resume
```

The parser discovers paragraphs in document-body and table XML order. It recognizes
configurable heading aliases, ordinary summaries, concise skill lists, and role/project
content. It does not depend on a personal name, employer, fixed paragraph count or
prebuilt profile file. Extend `resume.section_aliases` for different headings or
languages. The configured keys identify editable semantic categories; the document
does not have to contain all categories. Unknown sections stay protected.

Names, contact details, headings, employer/title/date lines and protected sections
remain unchanged. Text boxes, fields, hyperlinks, drawings, tabs and hard breaks
are protected rather than flattened. Image-only/scanned resumes and layouts with
no safely editable text fail with guidance to inspect headings/aliases. Parsing is
conservative, so inspect targets and your generated copy for unusual layouts.

Text replacements preserve existing XML runs, styles, tables and every package part
except the document text XML. A new uniquely named copy is written each time; no
source file is overwritten. Character length may change wrapping and pagination.
The audit reports `pagination_verified: false`: open every generated resume before
using it. Structural preservation is not visual-layout or factual certification.

`resume.factual_review_mode`:

| Mode | Behavior |
| --- | --- |
| `strict` (default) | Reject unsupported claims through deterministic checks plus local model factual review. Retain independently valid changes. |
| `advisory` | Apply structurally valid, quality-valid edits even if factual support is uncertain; save warnings and require manual review. |

Both modes reject invalid targets, changed protected content, skills prose and repeated
bullets/clauses. `source_context_scope: role` keeps strict review scoped to the role or
project; `full_resume` broadens that source context. Advisory review uses the full
source regardless of this setting. Models can make factual mistakes in either mode.
`save_updated_skills` optionally retains unverified generated skill context for advisory
runs; strict review ignores it. This is file-based context, not a feedback/learning loop.

## Configure email and templates

Leave `email.enabled: false` for local-only operation. A successful normal local run
saves a digest and records jobs as completed to prevent repeated tailoring. `--preview`
and sample runs do not mark new jobs completed. Enable notifications by setting your
own sender, recipient, host, TLS mode and port, and supplying a secret:

```sh
cp .env.example .env
```

Edit `.env` locally:

```dotenv
JOBFINDER_SMTP_PASSWORD=your-provider-app-password
```

Choose `security: ssl` (often port 465) or `security: starttls` (often 587), according
to your provider. The configured `username` defaults to sender; set it explicitly if
they differ. Credentials are transmitted only over verified TLS. Some providers
require enabling SMTP/app passwords or a verified sender. Ordinary account passwords
may not work. No authentication test or message is sent by `--check`. The check displays the
configuration file it loaded and the expected SMTP secret variable name. That variable
name must use uppercase letters, digits and underscores; it is not the password itself.

Email-enabled normal runs send automatically; use `--preview` while adjusting settings.
An explicit `--send` also requires `email.enabled: true`. Fictitious sample digests
cannot be sent. One digest contains the selected jobs and individual DOCX attachments.

Optional plain-text template files: `subject_template`, `intro_template`, `job_template`,
`footer_template`. `null` uses built-in content. Every template supports `$count`,
`$date` and `$titles`. Job templates also support:

```text
$index $job_title $company $location $work_arrangement $employment_type
$required_experience $posted $application_url $resume_filename
$matched_skills $gaps $sponsorship_status $verification_note $factual_review_note
```

Use `$$` for a literal dollar. The subject must be one non-empty line. Unknown
placeholders fail startup with the template field name. See
`templates/notifications/job.example.txt`. Policy-verification and advisory factual
warnings remain appended even with custom job templates.

## Gmail setup

1. Enable 2-Step Verification in your [Google Account security settings](https://myaccount.google.com/security).
   Gmail app passwords require it; see [Google's app-password instructions](https://support.google.com/accounts/answer/185833?hl=en).
2. Open [App passwords](https://myaccount.google.com/apppasswords), create an entry
   named `Local Job Finder`, and copy the generated 16-character app password.
   Use this app password, not your normal Google password. When entering it below,
   omit any spaces Google shows between groups of characters.
3. In the existing `email:` section of `config.yaml`, update these fields, keeping
   other settings such as notification templates and message-size limits:

   ```yaml
   email:
     enabled: true
     sender: your-address@gmail.com
     recipient: your-address@gmail.com
     username: your-address@gmail.com
     host: smtp.gmail.com
     port: 465
     security: ssl
     password_environment_variable: JOBFINDER_SMTP_PASSWORD
   ```

   Replace the dummy addresses with yours. Sender and username should identify
   the Gmail account that generated the app password. Recipient may be another
   address. Gmail supports SMTP SSL on port 465; see
   [Google's SMTP documentation](https://developers.google.com/workspace/gmail/imap/imap-smtp).
4. If `.env` does not already exist, copy `.env.example` to `.env`. Edit `.env`
   locally and replace the placeholder:

   ```dotenv
   JOBFINDER_SMTP_PASSWORD=your-16-character-app-password-without-spaces
   ```

   Keep the password out of `config.yaml`, Git and chat. On macOS/Linux, run
   `chmod 600 .env` to restrict file access. Existing process environment values
   override `.env`; update or unset a stale `JOBFINDER_SMTP_PASSWORD` environment
   variable if changing the file does not change authentication behavior.
5. Confirm the secret is detected, then prepare a real-job preview:

   ```sh
   python main.py --check
   python main.py --preview --limit 1
   ```

   `--check` should show `SMTP password: configured`. This only checks that the
   secret is present: it does not authenticate to Gmail or send a test email.
   Open the generated digest and resume before sending.
6. To send that exact real-job preview, use the digest ID printed by the command:

   ```sh
   python main.py --send-outbox DIGEST_ID
   ```

   Alternatively, `python main.py --limit 1` performs a new run and sends
   automatically while `email.enabled` is true. `--preview` suppresses sending;
   `--sample` digests cannot be emailed. Delivery commands cannot be combined with
   preview, sample or imported-job flags. SMTP acceptance does not guarantee inbox
   arrival; check the recipient's Inbox and Spam folders.

If App passwords is unavailable, Google notes that organization accounts,
Advanced Protection, or security-key-only 2-Step Verification may prevent it.
Check [Google's eligibility guidance](https://support.google.com/accounts/answer/185833?hl=en)
or ask your Workspace administrator. This adapter currently uses SMTP app-password
login and does not implement Google's OAuth sign-in. Changing your Google account
password revokes app passwords; generate a replacement and update `.env` afterward.

## Environment overrides

Precedence: process environment → repository `.env` → YAML/JSON → schema defaults.
For nested fields use `JOBFINDER__SECTION__FIELD`; double underscores separate keys.
Lists, numbers and booleans use JSON syntax; strings can be plain text:

```sh
export JOBFINDER__SEARCH__JOB_TITLES='["Product Designer"]'
export JOBFINDER__EMAIL__ENABLED=false
export JOBFINDER__LLM__MODELS__RESUME_TAILORER='llama3.1:8b'
export JOBFINDER__RESUME__FACTUAL_REVIEW_MODE=advisory
```

`.env` uses the same syntax without `export`. The SMTP secret uses the variable named
by `email.password_environment_variable`, default `JOBFINDER_SMTP_PASSWORD`. Passwords
are excluded from Settings representations, runtime dictionaries, model prompts,
scraper subprocess environments and saved artifacts. There is no YAML password field.

## Execution workflow

1. Activate your environment, finish configuration and supply your DOCX.
2. Start Ollama and pull the configured models.
3. Run `python main.py --check` to validate config, resume targets and installed models.
4. Run `python main.py --preview` to review real discovery and generated copies.
5. Open the digest text and DOCX files; review factual warnings and pagination.
6. Run `python main.py` for normal execution. Email is automatic only when enabled.

Additional commands:

```sh
python main.py --config another-config.yaml --preview
python main.py --search-only
python main.py --sample --limit 1
python main.py --jobs path/to/your-jobs.json --preview
python main.py --send-outbox DIGEST_ID
python main.py --resolve-outbox DIGEST_ID --delivery sent
python main.py --resolve-outbox DIGEST_ID --delivery not_sent
```

`--search-only` needs valid configuration and a resume file but does not contact
Ollama or SMTP. Imported jobs are a JSON array with `job_title`, `company`, `location`,
`job_description` (at least 120 characters), `application_url`, and optional source,
posting date, employment type, work arrangement and source URL. Any `source: sample`
job remains unsendable. `--sample` refreshes sample dates and forces preview behavior.
A sample must still match your search preferences to produce a tailored result.

Relative `--jobs` paths resolve from the project root. Running from elsewhere works
when invoking the absolute path to `main.py` with its environment's Python.
`--limit` reduces the configured batch size for this run. Exit codes: 0 for success
or no eligible matches; 1 for setup/runtime failure; 2 when eligible candidates could
not produce ready resumes; 130 when interrupted. One file lock prevents overlapping
runs against the same data directory.

## Output, deduplication and recovery

| Path | Contents |
| --- | --- |
| `data/jobs/{run_id}/` | Raw and enriched job records, exact evidence, employer diagnostics. |
| `data/state.json` | Completed job identity keys, allocation counts and digest IDs. |
| `logs/runs/` | Run status, candidate decisions, selected jobs and model statistics. |
| `logs/keywords/`, `logs/tailoring/` | Detailed extraction and replacement attempts. |
| `output/tailored_resumes/{posting_id}/{revision}/` | DOCX and factual/structural audit. |
| `output/outbox/{digest_id}/` | Digest text, .eml and delivery manifest. |

`paths.data`, `paths.logs` and `paths.output` change these roots. They must be distinct,
non-nested directories, not the project root. Protect custom directories from Git
and backup/exposure as appropriate. No old artifacts are automatically deleted.

Completed local jobs and SMTP-accepted jobs are deduplicated by URLs, posting IDs
and normalized title/company/location. Previews alone remain eligible. Prepared
messages and attachments are hash checked before sending. An SMTP refusal/authentication
failure is retryable; a disconnect after transmission begins is `uncertain` and blocks
resending. Check your provider's sent/delivery records before using `--resolve-outbox`;
only choose `not_sent` if you have established it was not accepted. SMTP acceptance
is not proof of inbox arrival. Changing recipient configuration does not rewrite an
existing prepared digest; create a new preview if the addresses no longer match.

## Troubleshooting

| Symptom | Likely cause | Resolution |
| --- | --- | --- |
| Missing config | `config.yaml` was not created | Copy the example and edit it, or pass `--config`. |
| Error names a config field | Missing value, typo, wrong type or conflicting limits | Follow the field error; use true/false booleans and numeric values without quotes. |
| DOCX not found or invalid | Wrong path, legacy .doc, PDF renamed as .docx | Set `resume.base_path`; save an actual Word DOCX outside output roots. |
| No safe editable text | Unknown headings, shapes, complex formatting | Inspect paragraphs, extend section aliases, or simplify those paragraphs in a copy. |
| Ollama connection failed | Server stopped or wrong local port | Open Ollama/run `ollama serve`; check `llm.base_url`. |
| Model not installed | Configured name differs from local tag | Run `ollama list`, pull the exact tag and update all model roles. |
| Out of memory/slow generation | Model/context too large or model overlap | Use a smaller quantization/model, lower context/input budgets together, keep sequential unloads. |
| Input exceeds character limit | Long resume or job | Use a shorter source copy or increase character and context limits within RAM capacity. |
| Repeated invalid JSON | Model not following the supplied schema | Try another local structured-output-capable model and inspect the logged agent error. |
| No jobs / 403 / 429 / timeout | Blocking, unsupported country or no available matches | Check board country spelling, increase pauses, disable failing sites, use public company URLs or imports. |
| Unknown/older jobs absent | Recent-only selection | Set recent_share to 0 or a fraction if older/undated jobs are wanted. |
| Fewer than batch_size resumes | Eligibility, recency or tailoring checks failed | Inspect candidate decisions and tailoring attempts; adjust preferences/limits deliberately. |
| SMTP password missing | Secret variable absent | Set the configured variable in repository `.env` or environment; previews still work. |
| SMTP authentication/STARTTLS error | Wrong credentials, port, provider policy or TLS mode | Use provider app password, correct SSL/STARTTLS settings, and check username/sender. |
| Digest too large | Attachments exceed configured/provider limit | Lower batch_size or raise max_message_bytes only within provider limits. |
| Delivery uncertain | Connection lost around SMTP transmission | Check provider records and reconcile before retrying. |
| No repeat of a previously completed job | Local or sent completion state deduplicates it | Use an isolated data/output/log configuration for a deliberate rerun; preserve delivery history. |
| Resume layout changed visually | Longer replacement wraps differently | Open Word and review pagination; structural checks do not render pages. |
| Another run active | Same data directory lock is held | Let the existing run finish or stop it; do not run concurrent pipelines sharing state. |

## Verification and limitations

Run `python -m unittest discover -s tests -v`. The suite uses synthetic DOCX fixtures
and mocks model output, scraping, and SMTP. It covers strict configuration, overrides,
generic search, open-ended keywords, employer-policy evidence, body/table editing,
source integrity, factual modes, delivery recovery and orchestrator decisions.

No real email is sent during tests. Live scraping, your chosen model's quality,
provider authentication, Windows/Linux execution and rendered document pagination
must be checked in your environment. A `--preview` run is the recommended first live
check. Keep human review before using a resume or submitting an application.

## Preparing a public GitHub release

Run `python scripts/check_release.py` before committing. It checks both tracked and
eligible untracked files, including files accidentally force-added despite ignore
rules. Keep the local config, .env, credentials, all personal DOCX resumes, .eml
messages, and data/log/output artifacts out of Git. DOCX, EML and resume-audit files
are ignored even in custom folders; custom JSON/state/log roots still need explicit
ignore rules. The focused checker does not replace careful review or GitHub secret
scanning. Never share a ZIP of your configured working directory.

The configured GitHub workflow tests Python 3.11 and 3.12 on Ubuntu, macOS and Windows
without scraping, model inference or SMTP. A separate job checks known runtime
dependency advisories. It uses no user secrets and read-only repository permissions.
These remote checks are pending until run on GitHub. Package-resolution checks are
not equivalent to running the application on another operating system.

There is currently no LICENSE file because the license choice is undecided.
[GitHub explains the implications of publishing without a license](https://docs.github.com/en/repositories/managing-your-repositorys-settings-and-features/customizing-your-repository/licensing-a-repository).
Choose usage terms before presenting the application as an open-source project.
