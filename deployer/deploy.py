"""Manifest-driven, idempotent Canvas course deploy.

Reads  <repo>/course.json          the manifest the widget wrote
Writes <repo>/.canvas-deploy-state.json   key -> Canvas id, so re-runs update

Usage:
    python deploy.py --repo-dir . [--canvas-course-id 123] [--dry-run] [--prune]

Design rule carried over from earlier deploys: never record an object in the
state file until its children have been re-fetched and counted. The state file
is the idempotency key, so a half-built object recorded as done is a course
that no later run will ever repair.
"""
import argparse
import json
import os
import sys

from canvas_api import Canvas, CanvasError

STATE_NAME = ".canvas-deploy-state.json"
MANIFEST_NAME = "course.json"

DEFAULT_BASE = "https://optimaoaoteam.instructure.com"

IFRAME_WRAPPER = """<div style="margin: 24px 0; border: 2px solid #0E1C42; border-radius: 10px; overflow: hidden; background: #f8f9fa;">
  <div style="background: #0E1C42; color: #55C8E8; padding: 10px 18px; font-family: 'Segoe UI', Arial, sans-serif; font-size: 1.05em; border-bottom: 3px solid #55C8E8;">{title}</div>
  <iframe src="{url}" width="100%" height="{height}" style="border: none; display: block;" allowfullscreen="" loading="lazy"></iframe>
  <div style="padding: 6px 18px; font-family: 'Segoe UI', Arial, sans-serif; font-size: 0.8em; color: #666; background: #EEF1F8;">If the lesson doesn't load, <a href="{url}" target="_blank">open it in a new tab</a>.</div>
</div>"""


# ----------------------------------------------------------------- helpers

def read_text(path):
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def save_json(path, obj):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
        f.write("\n")


def is_full_document(text):
    """Full HTML doc -> host on Pages and iframe it. Fragment -> paste inline.

    Matches the existing build convention where GitHub-hosted interactive
    lessons are complete documents and Canvas-native content is a bare
    fragment.
    """
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def pages_url_for(pages_base, source):
    if not pages_base:
        return None
    return pages_base.rstrip("/") + "/" + source.replace("\\", "/").lstrip("/")


# ------------------------------------------------------------- the deployer

class Deployer:
    def __init__(self, repo_dir, canvas, manifest, state, dry_run=False, prune=False):
        self.dir = repo_dir
        self.cv = canvas
        self.m = manifest
        self.state = state
        self.dry = dry_run
        self.prune = prune
        self.pages_base = (manifest.get("course") or {}).get("pages_url")
        self.state.setdefault("modules", {})
        self.state.setdefault("items", {})
        self.state.setdefault("assignment_groups", {})
        self.created = 0
        self.updated = 0
        self.warnings = []

    def log(self, *a):
        print(*a, flush=True)

    def warn(self, msg):
        self.warnings.append(msg)
        print("  WARNING: " + msg, flush=True)

    def save(self):
        if not self.dry:
            save_json(os.path.join(self.dir, STATE_NAME), self.state)

    def src(self, rel):
        p = os.path.join(self.dir, rel.replace("/", os.sep))
        if not os.path.exists(p):
            raise CanvasError(f"manifest references a missing file: {rel}")
        return p

    # ------------------------------------------------------ assignment groups

    def ensure_assignment_groups(self):
        groups = self.m.get("assignment_groups") or []
        if not groups:
            return
        self.log("\n== Assignment groups ==")
        for i, g in enumerate(groups, 1):
            key = g["key"]
            rec = self.state["assignment_groups"].get(key)
            if rec:
                self.log(f"  = {g['name']} (exists, id {rec['id']})")
                continue
            if self.dry:
                self.log(f"  + {g['name']} (dry run)")
                continue
            gid = self.cv.create_assignment_group(
                g["name"], g.get("position", i), g.get("weight"))
            self.state["assignment_groups"][key] = {"id": gid, "name": g["name"]}
            self.save()
            self.created += 1
            self.log(f"  + {g['name']} -> id {gid}")

        if self.m.get("course", {}).get("weighted_groups"):
            if not self.dry:
                self.cv.api("PUT", self.cv._c(),
                            {"course": {"apply_assignment_group_weights": True}})

    def group_id(self, key):
        if not key:
            return None
        rec = self.state["assignment_groups"].get(key)
        return rec["id"] if rec else None

    # ---------------------------------------------------------------- content

    def body_for(self, item):
        """Canvas-side HTML body for a content item."""
        source = item.get("source")
        if not source:
            return item.get("body", "")
        text = read_text(self.src(source))
        mode = item.get("mode") or ("iframe" if is_full_document(text) else "inline")
        if mode == "iframe":
            url = pages_url_for(self.pages_base, source)
            if not url:
                raise CanvasError(
                    f"{item['key']}: needs an iframe embed but course.pages_url is unset")
            return IFRAME_WRAPPER.format(
                title=item.get("title", ""), url=url,
                height=item.get("iframe_height", 1000))
        return text

    def ensure_page(self, item, rec):
        body = self.body_for(item)
        title = item["title"]
        published = item.get("published", True)
        if rec and rec.get("page_url"):
            _, url = self.cv.update_page(rec["page_url"], title=title, body=body,
                                         published=published)
            return {"type": "Page", "page_url": url}
        pid, url = self.cv.create_page(title, body, published=published,
                                       front_page=item.get("front_page", False))
        return {"type": "Page", "page_id": pid, "page_url": url}

    def ensure_assignment(self, item, rec):
        body = self.body_for(item)
        fields = dict(
            name=item["title"],
            description=body,
            points_possible=item.get("points", 0),
            submission_types=item.get("submission_types") or ["online_upload"],
            grading_type=item.get("grading_type", "points"),
            published=item.get("published", True),
        )
        gid = self.group_id(item.get("group"))
        if gid:
            fields["assignment_group_id"] = gid
        if rec and rec.get("content_id"):
            self.cv.update_assignment(rec["content_id"], **fields)
            return {"type": "Assignment", "content_id": rec["content_id"]}
        aid = self.cv.create_assignment(
            item["title"], body, points=fields["points_possible"],
            submission_types=fields["submission_types"], group_id=gid,
            grading_type=fields["grading_type"], published=fields["published"])
        return {"type": "Assignment", "content_id": aid}

    def ensure_discussion(self, item, rec):
        body = self.body_for(item)
        if rec and rec.get("content_id"):
            self.cv.update_discussion(rec["content_id"], title=item["title"],
                                      message=body)
            return {"type": "Discussion", "content_id": rec["content_id"]}
        did = self.cv.create_discussion(
            item["title"], body, points=item.get("points"),
            group_id=self.group_id(item.get("group")),
            published=item.get("published", True))
        return {"type": "Discussion", "content_id": did}

    def ensure_file(self, item, rec):
        path = self.src(item["source"])
        result = self.cv.upload_file(path, item.get("folder", "/course files"))
        return {"type": "File", "content_id": result["id"]}

    # ------------------------------------------------------------------ quiz

    QTYPES = {
        "multiple_choice": "multiple_choice_question",
        "true_false": "true_false_question",
        "multiple_answers": "multiple_answers_question",
        "short_answer": "short_answer_question",
        "essay": "essay_question",
        "matching": "matching_question",
    }

    def quiz_question_payload(self, q, n):
        qtype = q.get("type", "multiple_choice")
        canvas_type = self.QTYPES.get(qtype)
        if not canvas_type:
            raise CanvasError(f"unknown quiz question type: {qtype}")
        stem = q.get("stem") or q.get("text") or f"Question {n}"
        payload = {
            "question_name": (q.get("name") or stem)[:80] or f"Question {n}",
            "question_text": stem,
            "question_type": canvas_type,
            "points_possible": q.get("points", 1),
            "position": n,
        }
        if qtype in ("multiple_choice", "multiple_answers"):
            payload["answers"] = [
                {"answer_text": c["text"], "answer_weight": 100 if c.get("correct") else 0}
                for c in q["choices"]
            ]
        elif qtype == "true_false":
            correct = q.get("correct")
            truth = correct is True or str(correct).strip().lower() == "true"
            payload["answers"] = [
                {"answer_text": "True", "answer_weight": 100 if truth else 0},
                {"answer_text": "False", "answer_weight": 0 if truth else 100},
            ]
        elif qtype == "short_answer":
            answers = q.get("answers") or ["response"]
            payload["answers"] = [{"answer_text": a, "answer_weight": 100} for a in answers]
        elif qtype == "matching":
            payload["answers"] = [
                {"answer_match_left": p["left"], "answer_match_right": p["right"]}
                for p in q["pairs"]
            ]
            payload["points_possible"] = q.get("points", len(payload["answers"]))
        elif qtype == "essay":
            payload["answers"] = []
        return payload

    def load_questions(self, item):
        if item.get("questions"):
            return item["questions"]
        if item.get("questions_source"):
            return load_json(self.src(item["questions_source"]), [])
        return []

    def ensure_quiz(self, item, rec):
        questions = self.load_questions(item)
        quiz_type = item.get("quiz_type", "assignment")
        description = self.body_for(item) if (item.get("source") or item.get("body")) else ""

        # Leak guard: a description built from an authoring file can carry an
        # answer key. Refuse rather than publish a quiz that shows the answers.
        low = description.lower()
        for marker in ("answer key", "&#10003;", "✓", "teacher note"):
            if marker in low:
                raise CanvasError(
                    f"{item['key']}: quiz description contains '{marker}' -- "
                    "looks like an answer key leaked in; fix the source file")

        if rec and rec.get("content_id"):
            qid = rec["content_id"]
            self.cv.update_quiz(qid, title=item["title"], description=description)
            for existing in self.cv.list_quiz_questions(qid):
                self.cv.delete_quiz_question(qid, existing["id"])
        else:
            qid = self.cv.create_quiz(
                item["title"], description, quiz_type=quiz_type,
                allowed_attempts=item.get("allowed_attempts", -1),
                group_id=self.group_id(item.get("group")),
                shuffle=item.get("shuffle_answers", False))

        for n, q in enumerate(questions, 1):
            self.cv.add_quiz_question(qid, self.quiz_question_payload(q, n))

        # Verify the children landed BEFORE this quiz is recorded as done.
        landed = self.cv.list_quiz_questions(qid)
        if len(landed) != len(questions):
            raise CanvasError(
                f"{item['key']}: expected {len(questions)} quiz questions, "
                f"Canvas has {len(landed)} -- not recording state")

        if questions and quiz_type == "assignment":
            weighted = [
                qq for qq in landed
                if any(a.get("weight") == 100 for a in (qq.get("answers") or []))
                or qq.get("question_type") == "essay_question"
            ]
            if len(weighted) < len(landed):
                self.warn(f"{item['key']}: {len(landed) - len(weighted)} question(s) "
                          "have no correct answer weighted -- check the source")

        if item.get("published", True):
            self.cv.republish_quiz(qid)

        fresh = self.cv.api("GET", f"{self.cv._c()}/quizzes/{qid}")
        if questions and not fresh.get("question_count"):
            self.warn(f"{item['key']}: question_count still 0 after republish")

        return {"type": "Quiz", "content_id": qid}

    # ------------------------------------------------------------- one item

    CONTENT_BUILDERS = {
        "Page": "ensure_page",
        "Assignment": "ensure_assignment",
        "Discussion": "ensure_discussion",
        "Quiz": "ensure_quiz",
        "File": "ensure_file",
    }

    def deploy_item(self, module_key, module_id, item, position):
        key = item["key"]
        itype = item["type"]
        rec = self.state["items"].get(key, {})
        existing = bool(rec.get("content_id") or rec.get("page_url"))

        if self.dry:
            self.log(f"    {'=' if existing else '+'} [{position}] {itype}: {item.get('title','')}")
            return

        if itype in ("SubHeader", "ExternalUrl"):
            content = {"type": itype}
        else:
            builder = self.CONTENT_BUILDERS.get(itype)
            if not builder:
                raise CanvasError(f"{key}: unsupported item type {itype}")
            content = getattr(self, builder)(item, rec)

        # attach to (or move within) the module
        mi_id = rec.get("module_item_id")
        if mi_id and rec.get("module_id") == module_id:
            self.cv.update_module_item(
                module_id, mi_id, position=position,
                indent=item.get("indent", 0), title=item.get("title"))
            self.updated += 1
            verb = "="
        else:
            mi_id = self.cv.add_module_item(
                module_id, itype, position,
                title=item.get("title"),
                content_id=content.get("content_id"),
                page_url=content.get("page_url"),
                external_url=item.get("url"),
                indent=item.get("indent", 0))
            self.created += 1
            verb = "+"

        rec = dict(rec)
        rec.update(content)
        rec.update({"module_item_id": mi_id, "module_id": module_id,
                    "module_key": module_key, "position": position,
                    "title": item.get("title")})
        self.state["items"][key] = rec
        self.save()
        self.log(f"    {verb} [{position}] {itype}: {item.get('title','')}")

    # ----------------------------------------------------------- one module

    def deploy_module(self, mod, position):
        key = mod["key"]
        rec = self.state["modules"].get(key)
        if rec:
            module_id = rec["id"]
            if not self.dry:
                self.cv.update_module(module_id, name=mod["title"], position=position)
            self.log(f"\n== [{position}] {mod['title']} (existing id {module_id}) ==")
        elif self.dry:
            module_id = None
            self.log(f"\n== [{position}] {mod['title']} (would create) ==")
        else:
            module_id = self.cv.create_module(mod["title"], position)
            self.state["modules"][key] = {"id": module_id, "title": mod["title"]}
            self.save()
            self.created += 1
            self.log(f"\n== [{position}] {mod['title']} -> id {module_id} ==")

        for i, item in enumerate(mod.get("items") or [], 1):
            self.deploy_item(key, module_id, item, i)

        if not self.dry and mod.get("published", True):
            self.cv.publish_module(module_id, True)

    # --------------------------------------------------------------- prune

    def prune_removed(self):
        live_keys = {
            it["key"] for mod in self.m.get("modules", [])
            for it in (mod.get("items") or [])
        }
        if self.m.get("front_page"):
            # the front page is a real deployed object, just not a module item
            live_keys.add(self.m["front_page"].get("key", "front-page"))
        stale = [k for k in self.state["items"] if k not in live_keys]
        if not stale:
            return
        if not self.prune:
            self.warn(f"{len(stale)} item(s) in state are no longer in the manifest "
                      f"(left in place; re-run with --prune to remove): "
                      f"{', '.join(sorted(stale)[:8])}")
            return
        for k in stale:
            rec = self.state["items"][k]
            try:
                if rec.get("module_id") and rec.get("module_item_id"):
                    self.cv.delete_module_item(rec["module_id"], rec["module_item_id"])
                self.log(f"  - pruned {k}")
            except CanvasError as e:
                self.warn(f"prune of {k} failed: {e}")
            del self.state["items"][k]
        self.save()

    # ----------------------------------------------------------------- run

    def ensure_front_page(self):
        item = self.m.get("front_page")
        if not item or self.dry:
            if item and self.dry:
                self.log(f"\n== Front page: {item.get('title')} (dry run) ==")
            return
        self.log("\n== Front page ==")
        rec = self.state["items"].get("front-page", {})
        item = dict(item)
        item.setdefault("key", "front-page")
        item["front_page"] = True
        content = self.ensure_page(item, rec)
        # front_page must be set explicitly on update, and default_view can only
        # flip to wiki once a front page actually exists.
        self.cv.update_page(content["page_url"], front_page=True)
        self.cv.set_front_page_view()
        self.state["items"]["front-page"] = content
        self.save()
        self.log(f"  + {item['title']} -> /{content['page_url']} (course home)")

    def run(self):
        self.ensure_front_page()
        self.ensure_assignment_groups()
        for i, mod in enumerate(self.m.get("modules") or [], 1):
            self.deploy_module(mod, i)
        self.prune_removed()
        return self.verify()

    def verify(self):
        """Re-read Canvas and confirm it matches the manifest."""
        self.log("\n== Verification ==")
        problems = []
        if self.dry:
            self.log("  (dry run -- skipped)")
            return problems

        live_modules = {m["id"]: m for m in self.cv.list_modules()}
        for i, mod in enumerate(self.m.get("modules") or [], 1):
            rec = self.state["modules"].get(mod["key"])
            if not rec:
                problems.append(f"module {mod['key']} never recorded")
                continue
            live = live_modules.get(rec["id"])
            if not live:
                problems.append(f"module {mod['key']} (id {rec['id']}) not in Canvas")
                continue
            if live.get("position") != i:
                problems.append(
                    f"module {mod['key']} at position {live.get('position')}, expected {i}")
            if mod.get("published", True) and not live.get("published"):
                problems.append(f"module {mod['key']} is unpublished")

            want = mod.get("items") or []
            got = self.cv.list_module_items(rec["id"])
            if len(got) != len(want):
                problems.append(
                    f"module {mod['key']}: {len(got)} items in Canvas, manifest has {len(want)}")
            by_pos = {it.get("position"): it for it in got}
            for n, item in enumerate(want, 1):
                live_item = by_pos.get(n)
                if not live_item:
                    problems.append(f"{mod['key']} position {n} empty (want {item.get('title')})")
                elif live_item.get("title") != item.get("title"):
                    problems.append(
                        f"{mod['key']} position {n}: Canvas has "
                        f"'{live_item.get('title')}', manifest has '{item.get('title')}'")

        if problems:
            for p in problems:
                self.log("  FAIL " + p)
        else:
            self.log(f"  OK  {len(self.m.get('modules') or [])} modules, "
                     f"{sum(len(m.get('items') or []) for m in self.m.get('modules') or [])} items "
                     "match the manifest in order")
        return problems


# ---------------------------------------------------------------------- cli

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-dir", default=".")
    ap.add_argument("--canvas-course-id", type=int, default=None)
    ap.add_argument("--base-url", default=os.environ.get("CANVAS_BASE_URL", DEFAULT_BASE))
    ap.add_argument("--token", default=os.environ.get("CANVAS_TOKEN"))
    ap.add_argument("--create-course", action="store_true",
                    help="create a new Canvas course if the manifest has no id")
    ap.add_argument("--account-id", type=int, default=1)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--prune", action="store_true")
    args = ap.parse_args()

    repo = os.path.abspath(args.repo_dir)
    manifest = load_json(os.path.join(repo, MANIFEST_NAME))
    if manifest is None:
        sys.exit(f"no {MANIFEST_NAME} in {repo}")
    state = load_json(os.path.join(repo, STATE_NAME), {}) or {}

    if not args.token and not args.dry_run:
        # Local convenience: fall back to the tokens file. In the Action the
        # token always arrives as CANVAS_TOKEN from repo secrets.
        try:
            from publish import load_token
            args.token = load_token("canvas")
        except Exception:
            sys.exit("no Canvas token (set CANVAS_TOKEN or pass --token)")

    course_id = (args.canvas_course_id
                 or state.get("canvas_course_id")
                 or (manifest.get("course") or {}).get("canvas_course_id"))

    cv = Canvas(args.base_url, args.token or "", course_id)

    if not course_id and not args.dry_run:
        if not args.create_course:
            sys.exit("no Canvas course id; pass --canvas-course-id or --create-course")
        name = (manifest.get("course") or {}).get("name") or "Untitled course"
        course_id = cv.create_course(name, account_id=args.account_id)
        cv.course_id = course_id
        print(f"created Canvas course {course_id}: {name}")

    state["canvas_course_id"] = course_id
    print(f"Canvas course {course_id} at {args.base_url}")
    print(f"repo: {repo}")

    d = Deployer(repo, cv, manifest, state, dry_run=args.dry_run, prune=args.prune)
    problems = d.run()

    print(f"\ncreated {d.created}, updated {d.updated}, warnings {len(d.warnings)}")
    if problems:
        print(f"{len(problems)} verification problem(s)")
        sys.exit(1)
    print("deploy OK")


if __name__ == "__main__":
    main()
