"""Push a scanned course folder to GitHub and enable Pages.

CLI twin of the widget's push step. Only files the manifest references are
uploaded, so build notes and internal review documents stay out of what is a
publicly readable Pages repo.

Usage:
    python publish.py --dir "<course folder>" --repo hs-career-research \
        --manifest course.json [--private]
"""
import argparse
import json
import os
import sys

from gh import GitHub, GitHubError

TOKENS = r"C:\Users\JessicaDrexel\OneDrive - OptimaEd\Academic Design & Curriculum\Access tokens.txt"


def load_token(kind):
    txt = open(TOKENS, encoding="utf-8").read()
    lines = txt.split("\n")
    for i, l in enumerate(lines):
        low = l.lower()
        if kind in low and "token" in low:
            rest = l.split(":", 1)[1].strip() if ":" in l else ""
            return rest or lines[i + 1].strip()
    raise SystemExit(f"{kind} token not found in {TOKENS}")


def manifest_paths(manifest):
    """Every repo-relative file the manifest points at."""
    paths = set()
    fp = manifest.get("front_page")
    if fp and fp.get("source"):
        paths.add(fp["source"])
    for mod in manifest.get("modules", []):
        for it in mod.get("items", []):
            for field in ("source", "questions_source"):
                if it.get(field):
                    paths.add(it[field])
    return paths


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", required=True)
    ap.add_argument("--repo", required=True)
    ap.add_argument("--manifest", default="course.json")
    ap.add_argument("--owner", default="optimaondemand")
    ap.add_argument("--private", action="store_true")
    ap.add_argument("--message", default="Deploy course content")
    ap.add_argument("--no-pages", action="store_true")
    args = ap.parse_args()

    course_dir = os.path.abspath(args.dir)
    manifest_path = (args.manifest if os.path.isabs(args.manifest)
                     else os.path.join(course_dir, args.manifest))
    with open(manifest_path, encoding="utf-8") as f:
        manifest = json.load(f)

    gh = GitHub(load_token("github"), args.owner)

    if gh.repo_exists(args.repo):
        print(f"repo {args.owner}/{args.repo} exists")
    else:
        print(f"creating repo {args.owner}/{args.repo}...")
        gh.create_repo(args.repo, manifest.get("course", {}).get("name", ""),
                       private=args.private)

    wanted = manifest_paths(manifest)
    missing = [p for p in wanted if not os.path.exists(os.path.join(course_dir, p))]
    if missing:
        sys.exit("manifest references missing files:\n  " + "\n  ".join(missing))
    print(f"manifest references {len(wanted)} files")

    extra = {
        "course.json": json.dumps(manifest, indent=2, ensure_ascii=False) + "\n",
        ".nojekyll": "",
    }
    sha = gh.commit_folder(args.repo, course_dir, args.message,
                           extra_files=extra, only_paths=wanted)
    print(f"committed {sha[:8]}")

    if not args.no_pages:
        try:
            url = gh.enable_pages(args.repo)
            print(f"pages: {url}")
        except GitHubError as e:
            print(f"pages not enabled: {e}")
    print(f"repo: https://github.com/{args.owner}/{args.repo}")


if __name__ == "__main__":
    main()
