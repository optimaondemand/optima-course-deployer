# Optima Course Deployer

Upload a folder of course materials, get a sequenced Canvas course.

**Widget:** https://optimaondemand.github.io/optima-course-deployer/

## How it works

```
folder picker (browser)
  │  GitHub REST API — create repo, commit content, enable Pages
  ▼
course repo (e.g. optimaondemand/hs-career-research)
  │  workflow_dispatch
  ▼
this repo's Action → Canvas API
  modules, pages, assignments, quizzes, files — created in order
```

The Canvas token never reaches the browser. The Canvas REST API returns no
`Access-Control-Allow-Origin` header, so a hosted page cannot call it at all;
the token lives in this repo's secrets and is used only by the Action.

## Setup (once)

Repo secrets:

| Secret | What |
| --- | --- |
| `CANVAS_TOKEN` | Canvas API token with account-admin rights |
| `GH_PAT` | GitHub PAT with `repo` scope, used to check out and write back to course repos |

Optional repo variable `CANVAS_BASE_URL` (defaults to
`https://optimaoaoteam.instructure.com`).

## What the widget deploys

| Source | Becomes |
| --- | --- |
| `lesson-*.html` (full HTML document) | Canvas Page wrapping a GitHub Pages iframe |
| `assignment-*.html` (fragment) | Canvas Assignment, description inline |
| `*-CANVAS-RCE.html` | Canvas Page, body inline |
| `*.quiz.json` | Canvas Classic Quiz with questions |
| `*.pdf` `.docx` `.pptx` `.xlsx` | Canvas File |
| `*Course-Home*.html` | Course front page + `default_view: wiki` |
| `_*`, `README*`, `*REVIEW*` | not deployed, not uploaded |

Ordering is by the numbers in the filename, and within one slot the lesson page
comes before the quiz, discussion and assignment that follow it. A module-level
file with no sub-number (`assignment-3-your-path-comparison.html`) sorts last in
its module. Everything is editable in the review table before anything is sent.

Titles and point values come from the Optima template's metadata strip
(`Topic` and the `N pts` badge), falling back to `<title>` then the filename.

### Quizzes need a structured source

`*-CANVAS-NATIVE-SPEC.md` prose specs are flagged and **not** deployed. Quiz
content must come from a `.quiz.json` file so questions, point values and
correct answers are unambiguous:

```json
[
  {"type":"multiple_choice","stem":"...","points":1,
   "choices":[{"text":"...","correct":true},{"text":"..."}]},
  {"type":"true_false","stem":"...","correct":false,"points":1},
  {"type":"essay","stem":"...","points":5}
]
```

Types: `multiple_choice`, `multiple_answers`, `true_false`, `short_answer`,
`essay`, `matching`.

## Re-running

`.canvas-deploy-state.json` in the course repo maps manifest keys to Canvas
ids, so a second run updates in place rather than duplicating. Items removed
from the folder are reported and only deleted if you tick prune.

An object is recorded in the state file only after its children have been
re-fetched and counted — a quiz whose questions failed to attach is never
marked done, so a later run repairs it instead of skipping it forever.

## CLI

The same pipeline without the browser:

```bash
python deployer/scan.py    --dir "<course folder>" --out course.json \
                           --name "Course name" --pages-url https://owner.github.io/repo/
python deployer/publish.py --dir "<course folder>" --repo <repo> --manifest course.json
python deployer/deploy.py  --repo-dir <staged folder> --canvas-course-id 224 [--dry-run] [--prune]
```
