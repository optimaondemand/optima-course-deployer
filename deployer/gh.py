"""GitHub side of the course pipeline: create repo, commit a whole folder,
enable Pages, trigger the deploy workflow.

The widget performs these same calls from the browser in JS. This module is
the reference implementation and a CLI fallback.
"""
import base64
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.github.com"

SKIP_DIRS = {".git", "__pycache__", "node_modules", ".vs", ".idea"}
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}


class GitHubError(RuntimeError):
    pass


class GitHub:
    def __init__(self, token, owner, verbose=True):
        self.token = token
        self.owner = owner
        self.verbose = verbose

    def log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    def api(self, method, path, body=None, retries=3):
        url = path if path.startswith("http") else API + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        headers = {
            "Authorization": "token " + self.token,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "optima-course-pipeline",
        }
        if data:
            headers["Content-Type"] = "application/json"
        last = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=120) as resp:
                    raw = resp.read()
                    return resp.status, (json.loads(raw.decode("utf-8")) if raw.strip() else {})
            except urllib.error.HTTPError as e:
                body_txt = e.read().decode("utf-8", errors="replace")
                last = f"HTTP {e.code} on {method} {path}: {body_txt[:500]}"
                if e.code in (401, 403, 404, 409, 422):
                    return e.code, {"_error": last}
                time.sleep(2 * (attempt + 1))
            except Exception as e:
                last = f"{type(e).__name__}: {e}"
                time.sleep(2 * (attempt + 1))
        raise GitHubError(last or "unknown error")

    # ----------------------------------------------------------------- repo

    def repo_exists(self, repo):
        s, _ = self.api("GET", f"/repos/{self.owner}/{repo}")
        return s == 200

    def create_repo(self, repo, description="", private=False):
        # optimaondemand is a user account, not an org -- /user/repos, not /orgs/...
        s, b = self.api("POST", "/user/repos", {
            "name": repo,
            "description": description,
            "private": private,
            "auto_init": True,
        })
        if s not in (200, 201):
            raise GitHubError(f"create repo {repo} failed: {b.get('_error', b)}")
        return b

    def default_branch(self, repo):
        s, b = self.api("GET", f"/repos/{self.owner}/{repo}")
        if s != 200:
            raise GitHubError(f"repo lookup failed: {b.get('_error')}")
        return b.get("default_branch") or "main"

    # ------------------------------------------------------------- committing

    def collect_files(self, root):
        """Walk a folder into [(repo_relative_path, abs_path)], skipping junk."""
        out = []
        root = os.path.abspath(root)
        for dirpath, dirnames, filenames in os.walk(root):
            # .github must survive the hidden-directory filter or the workflow
            # silently never gets pushed
            dirnames[:] = [d for d in dirnames
                           if d not in SKIP_DIRS
                           and (not d.startswith(".") or d == ".github")]
            for fn in filenames:
                if fn.lower() in SKIP_NAMES:
                    continue
                ap = os.path.join(dirpath, fn)
                rel = os.path.relpath(ap, root).replace(os.sep, "/")
                out.append((rel, ap))
        return sorted(out)

    def commit_folder(self, repo, local_dir, message, branch=None, extra_files=None,
                      only_paths=None):
        """Commit a folder as one commit via the Git Data API.

        extra_files: {repo_path: str_content} written alongside the folder.
        only_paths: if given, an allowlist of repo-relative paths to upload.
            The pipeline passes the set of files the manifest references, so
            internal build notes and review documents never reach what is a
            publicly readable Pages repo.
        """
        branch = branch or self.default_branch(repo)

        s, ref = self.api("GET", f"/repos/{self.owner}/{repo}/git/ref/heads/{branch}")
        if s != 200:
            raise GitHubError(f"cannot read branch {branch}: {ref.get('_error')}")
        base_sha = ref["object"]["sha"]

        s, base_commit = self.api("GET", f"/repos/{self.owner}/{repo}/git/commits/{base_sha}")
        if s != 200:
            raise GitHubError(f"cannot read base commit: {base_commit.get('_error')}")
        base_tree = base_commit["tree"]["sha"]

        tree = []
        files = self.collect_files(local_dir)
        if only_paths is not None:
            allow = {p.replace("\\", "/") for p in only_paths}
            files = [(rel, ap) for rel, ap in files if rel in allow]
        self.log(f"  uploading {len(files)} files as blobs...")
        for i, (rel, ap) in enumerate(files, 1):
            with open(ap, "rb") as f:
                raw = f.read()
            s, blob = self.api("POST", f"/repos/{self.owner}/{repo}/git/blobs", {
                "content": base64.b64encode(raw).decode("ascii"),
                "encoding": "base64",
            })
            if s not in (200, 201):
                raise GitHubError(f"blob for {rel} failed: {blob.get('_error')}")
            tree.append({"path": rel, "mode": "100644", "type": "blob", "sha": blob["sha"]})
            if i % 25 == 0:
                self.log(f"    {i}/{len(files)}")

        for path, content in (extra_files or {}).items():
            s, blob = self.api("POST", f"/repos/{self.owner}/{repo}/git/blobs", {
                "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
                "encoding": "base64",
            })
            if s not in (200, 201):
                raise GitHubError(f"blob for {path} failed: {blob.get('_error')}")
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": blob["sha"]})

        s, new_tree = self.api("POST", f"/repos/{self.owner}/{repo}/git/trees", {
            "base_tree": base_tree, "tree": tree,
        })
        if s not in (200, 201):
            raise GitHubError(f"tree create failed: {new_tree.get('_error')}")

        s, commit = self.api("POST", f"/repos/{self.owner}/{repo}/git/commits", {
            "message": message, "tree": new_tree["sha"], "parents": [base_sha],
        })
        if s not in (200, 201):
            raise GitHubError(f"commit failed: {commit.get('_error')}")

        s, upd = self.api("PATCH", f"/repos/{self.owner}/{repo}/git/refs/heads/{branch}", {
            "sha": commit["sha"], "force": False,
        })
        if s not in (200, 201):
            raise GitHubError(f"ref update failed: {upd.get('_error')}")
        return commit["sha"]

    # ---------------------------------------------------------------- pages

    def enable_pages(self, repo, branch=None):
        branch = branch or self.default_branch(repo)
        s, b = self.api("POST", f"/repos/{self.owner}/{repo}/pages", {
            "source": {"branch": branch, "path": "/"},
        })
        if s in (200, 201):
            return b.get("html_url")
        if s == 409:  # already enabled
            s2, b2 = self.api("GET", f"/repos/{self.owner}/{repo}/pages")
            if s2 == 200:
                return b2.get("html_url")
        if s == 403:
            raise GitHubError(
                "Pages API returned 403 -- the token likely lacks the scope, "
                "or Pages must be enabled once by hand in repo Settings.")
        raise GitHubError(f"enable pages failed ({s}): {b.get('_error', b)}")

    def pages_url(self, repo):
        s, b = self.api("GET", f"/repos/{self.owner}/{repo}/pages")
        return b.get("html_url") if s == 200 else None

    # -------------------------------------------------------------- actions

    def dispatch_workflow(self, repo, workflow_file, inputs, ref="main"):
        s, b = self.api(
            "POST",
            f"/repos/{self.owner}/{repo}/actions/workflows/{workflow_file}/dispatches",
            {"ref": ref, "inputs": inputs})
        if s not in (204, 201, 200):
            raise GitHubError(f"workflow dispatch failed ({s}): {b.get('_error', b)}")
        return True

    def latest_run(self, repo, workflow_file):
        s, b = self.api(
            "GET",
            f"/repos/{self.owner}/{repo}/actions/workflows/{workflow_file}/runs?per_page=1")
        if s != 200:
            return None
        runs = b.get("workflow_runs") or []
        return runs[0] if runs else None
