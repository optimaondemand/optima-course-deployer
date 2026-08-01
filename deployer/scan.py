"""Scan a course folder into a draft course.json manifest.

This is the CLI twin of the widget's classify.js -- same rules, so a course can
go through the pipeline with or without the browser. The rules themselves are
deliberately small and declarative; see RULES below.

Usage:
    python scan.py --dir "<course folder>" --out course.json \
        --name "HS Career Research" --pages-url https://owner.github.io/repo/
"""
import argparse
import html
import json
import os
import re
import sys

# --------------------------------------------------------------- rule table

# filename prefix / suffix -> Canvas item type
RULES = [
    (r"-CANVAS-NATIVE-SPEC\.md$", "NeedsReview"),   # prose build spec, not deployable as-is
    (r"-CANVAS-RCE\.html$",       "Page"),
    (r"^lesson[-_]",              "Page"),
    (r"^assignment[-_]",          "Assignment"),
    (r"^quiz[-_]",                "Quiz"),
    (r"^discussion[-_]",          "Discussion"),
    (r"\.(pdf|docx|pptx|xlsx)$",  "File"),
    (r"\.html?$",                 "Page"),
    (r"\.quiz\.json$",            "Quiz"),
]

# never deployed: build notes, internal review docs, scratch
EXCLUDE = [
    r"^_",
    r"^\.",
    r"readme",
    r"\bREVIEW\b",
    r"-BUILD-NOTES?\b",
    r"^course\.json$",
    r"^\.canvas-deploy-state\.json$",
]

MODULE_DIR_HINTS = r"^(module|unit|week|quarter|semester|m|u|w)\b"


# ----------------------------------------------------------------- helpers

def natural_key(s):
    """Sort 'Module 2' before 'Module 10'."""
    return [int(t) if t.isdigit() else t.lower()
            for t in re.split(r"(\d+)", s)]


def excluded(name):
    return any(re.search(p, name, re.I) for p in EXCLUDE)


def item_type(name):
    for pattern, t in RULES:
        if re.search(pattern, name, re.I):
            return t
    return None


def clean(text):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", text))).strip()


def meta_field(doc, label):
    """Pull a value out of the Optima metadata strip.

    The strip is a run of <div>LABEL</div><div>VALUE</div> pairs, so match the
    label div then take the next div's text.
    """
    m = re.search(
        r">\s*" + re.escape(label) + r"\s*</div>\s*<div[^>]*>(.*?)</div>",
        doc, re.I | re.S)
    return clean(m.group(1)) if m else None


def extract_points(doc):
    m = re.search(r"(\d+)\s*pts?\b", doc, re.I)
    return int(m.group(1)) if m else None


def humanize(filename):
    stem = re.sub(r"\.[a-z0-9]+$", "", filename, flags=re.I)
    stem = re.sub(r"[-_](CANVAS-RCE|CANVAS-NATIVE-SPEC|CANVAS-paste-ready)$", "",
                  stem, flags=re.I)
    stem = re.sub(r"^(lesson|assignment|quiz|discussion)[-_]", "", stem, flags=re.I)
    # leading numeric ordinals: 1-2, 03_, 2.4
    stem = re.sub(r"^[\d]+([-_.][\d]+)*[-_.]?", "", stem)
    words = re.split(r"[-_\s]+", stem)
    return " ".join(w.capitalize() if w.islower() else w for w in words if w).strip() or filename


def is_full_document(text):
    head = text.lstrip()[:200].lower()
    return head.startswith("<!doctype html") or head.startswith("<html")


def title_for(path, name, itype):
    """Best available human title: metadata strip Topic, then <title>, then filename."""
    if itype == "File":
        return humanize(name)
    try:
        doc = open(path, "r", encoding="utf-8", errors="replace").read()
    except OSError:
        return humanize(name)

    topic = meta_field(doc, "Topic")
    if topic and len(topic) > 2:
        return topic

    m = re.search(r"<title[^>]*>(.*?)</title>", doc, re.I | re.S)
    if m:
        t = clean(m.group(1))
        # strip a leading course-name prefix: "Course Name - Lesson 1.1"
        t = re.split(r"\s+[-–—]\s+|\s*&mdash;\s*", t)[-1].strip()
        if t and not re.fullmatch(r"(lesson|assignment)\s*[\d.]+", t, re.I):
            return t
    return humanize(name)


# Within one ordinal slot, teach before you assess: the lesson page comes
# first, then its quiz/discussion, then the assignment it feeds.
TYPE_RANK = {"Page": 0, "Quiz": 1, "Discussion": 2, "Assignment": 3, "File": 4}


def ordinal_prefix(name):
    """Leading numbers used for ordering inside a module: lesson-1-2-x -> (1,2,999).

    Missing trailing components sort last, so a module-level capstone named
    `assignment-3-your-path-comparison` lands after `assignment-3-9-...`
    rather than ahead of the whole module.
    """
    stem = re.sub(r"^(lesson|assignment|quiz|discussion)[-_]", "", name, flags=re.I)
    nums = [int(n) for n in re.findall(r"\d+", stem[:12])[:3]]
    return tuple(nums + [999] * (3 - len(nums)))


def sort_key(name):
    return (ordinal_prefix(name), TYPE_RANK.get(item_type(name), 5), natural_key(name))


# ------------------------------------------------------------------- scan

def scan_dir(root, pages_url=None, course_name=None, canvas_course_id=None):
    root = os.path.abspath(root)
    modules = []
    unmapped = []
    skipped = []

    entries = sorted(os.listdir(root), key=natural_key)
    subdirs = [e for e in entries if os.path.isdir(os.path.join(root, e))
               and not excluded(e)]
    rootfiles = [e for e in entries if os.path.isfile(os.path.join(root, e))]

    def build_items(dirpath, filenames, mod_key):
        items = []
        # lessons and assignments interleave by their own numbering
        ordered = sorted(filenames, key=sort_key)
        for n, fn in enumerate(ordered, 1):
            if excluded(fn):
                skipped.append(os.path.relpath(os.path.join(dirpath, fn), root))
                continue
            t = item_type(fn)
            rel = os.path.relpath(os.path.join(dirpath, fn), root).replace(os.sep, "/")
            if t is None:
                skipped.append(rel)
                continue
            if t == "NeedsReview":
                unmapped.append(rel)
                continue

            full = os.path.join(dirpath, fn)
            item = {
                "key": f"{mod_key}-{n:02d}",
                "type": t,
                "title": title_for(full, fn, t),
                "source": rel,
                "indent": 0,
            }
            if t in ("Page", "Assignment", "Discussion"):
                try:
                    text = open(full, "r", encoding="utf-8", errors="replace").read()
                except OSError:
                    text = ""
                item["mode"] = "iframe" if is_full_document(text) else "inline"
                pts = extract_points(text)
                if t == "Assignment":
                    item["points"] = pts if pts is not None else 0
                    item["submission_types"] = ["online_upload"]
            items.append(item)
        return items

    for i, d in enumerate(subdirs, 1):
        dirpath = os.path.join(root, d)
        filenames = [f for f in sorted(os.listdir(dirpath), key=natural_key)
                     if os.path.isfile(os.path.join(dirpath, f))]
        mod_key = "m%02d" % i
        items = build_items(dirpath, filenames, mod_key)
        if not items:
            continue
        modules.append({
            "key": mod_key,
            "title": re.sub(r"^\d+[-_.\s]*", "", d).replace("-", " ").strip() or d,
            "published": True,
            "items": items,
        })

    # root-level files become a trailing resources module
    res_items = build_items(root, rootfiles, "m99")
    front_page = None
    keep = []
    for it in res_items:
        if re.search(r"course[-_ ]?home", it["source"], re.I):
            front_page = it
        else:
            keep.append(it)
    if keep:
        modules.append({
            "key": "m99", "title": "Course Resources",
            "published": False, "items": keep,
        })

    manifest = {
        "schema": 1,
        "course": {
            "name": course_name or os.path.basename(root),
            "pages_url": pages_url,
        },
        "modules": modules,
    }
    if canvas_course_id:
        manifest["course"]["canvas_course_id"] = canvas_course_id
    if front_page:
        front_page["front_page"] = True
        front_page["key"] = "front-page"
        manifest["front_page"] = front_page
    return manifest, unmapped, skipped


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--out", default="course.json")
    ap.add_argument("--name")
    ap.add_argument("--pages-url")
    ap.add_argument("--canvas-course-id", type=int)
    ap.add_argument("--only", nargs="*", help="only these module folder names")
    args = ap.parse_args()

    manifest, unmapped, skipped = scan_dir(
        args.dir, args.pages_url, args.name, args.canvas_course_id)

    if args.only:
        wanted = {o.lower() for o in args.only}
        manifest["modules"] = [m for m in manifest["modules"]
                               if m["title"].lower() in wanted
                               or m["key"].lower() in wanted]

    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
        f.write("\n")

    n_items = sum(len(m["items"]) for m in manifest["modules"])
    print(f"{len(manifest['modules'])} modules, {n_items} items -> {args.out}")
    for m in manifest["modules"]:
        print(f"  {m['key']}  {m['title']}  ({len(m['items'])} items)")
        for it in m["items"]:
            pts = f" [{it['points']}pts]" if it.get("points") else ""
            print(f"      {it['type']:<11} {it['title']}{pts}")
    if unmapped:
        print(f"\nNEEDS REVIEW ({len(unmapped)}) -- prose specs, not deployed:")
        for u in unmapped:
            print("   " + u)
    if skipped:
        print(f"\nskipped ({len(skipped)}): " + ", ".join(skipped[:10])
              + (" ..." if len(skipped) > 10 else ""))


if __name__ == "__main__":
    main()
