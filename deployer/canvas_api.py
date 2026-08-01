"""Canvas REST API wrapper for the Optima course pipeline.

Generalized from the per-course _deploy_* scripts: base URL, token and course id
are all parameters, so one engine serves every course.

Stdlib only (urllib) -- no pip install, so the GitHub Action needs no deps.
"""
import json
import mimetypes
import os
import time
import urllib.error
import urllib.parse
import urllib.request


class CanvasError(RuntimeError):
    pass


class Canvas:
    def __init__(self, base_url, token, course_id=None, verbose=True):
        base_url = base_url.rstrip("/")
        if not base_url.endswith("/api/v1"):
            base_url += "/api/v1"
        self.base = base_url
        self.token = token
        self.course_id = course_id
        self.verbose = verbose

    def log(self, *a):
        if self.verbose:
            print(*a, flush=True)

    # ---------------------------------------------------------------- core

    def api(self, method, path, body=None, params=None, retries=4):
        url = self.base + path
        if params:
            url += "?" + urllib.parse.urlencode(params, doseq=True)

        data = None
        headers = {"Authorization": "Bearer " + self.token}
        if body is not None:
            # Always send explicit UTF-8 bytes. Passing a str lets urllib pick
            # latin-1 and Canvas answers a bare 500 on any non-ASCII character.
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json; charset=utf-8"

        last = None
        for attempt in range(retries):
            req = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(req, timeout=90) as resp:
                    raw = resp.read()
                    return json.loads(raw.decode("utf-8")) if raw.strip() else {}
            except urllib.error.HTTPError as e:
                err = e.read().decode("utf-8", errors="replace")
                last = f"HTTP {e.code} on {method} {path}: {err[:600]}"
                # Canvas signals throttling with a 403 whose body says so --
                # that one is retryable, a real permission 403 is not.
                throttled = e.code == 403 and "rate limit" in err.lower()
                if e.code in (401, 404) or (e.code == 403 and not throttled):
                    raise CanvasError(last)
                if e.code == 400:
                    raise CanvasError(last)
                time.sleep(2 * (attempt + 1))
            except Exception as e:  # transient network
                last = f"{type(e).__name__} on {method} {path}: {e}"
                time.sleep(2 * (attempt + 1))
        raise CanvasError(last or "unknown error")

    def paged(self, path, params=None):
        """GET every page of a Canvas list endpoint."""
        out = []
        p = dict(params or {})
        p.setdefault("per_page", 100)
        page = 1
        while True:
            p["page"] = page
            batch = self.api("GET", path, params=p)
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < p["per_page"]:
                break
            page += 1
            if page > 50:
                break
        return out

    def _c(self):
        if self.course_id is None:
            raise CanvasError("course_id not set on this Canvas client")
        return f"/courses/{self.course_id}"

    # ------------------------------------------------------------- courses

    def create_course(self, name, account_id=1, course_code=None, published=False):
        r = self.api("POST", f"/accounts/{account_id}/courses", {
            "course": {
                "name": name,
                "course_code": course_code or name,
                "is_public": False,
            },
            "offer": bool(published),
        })
        return r["id"]

    def get_course(self):
        return self.api("GET", self._c())

    def set_front_page_view(self):
        """Only valid once a wiki front page exists, else Canvas 400s."""
        self.api("PUT", self._c(), {"course": {"default_view": "wiki"}})

    # ------------------------------------------------------------- modules

    def list_modules(self):
        return self.paged(f"{self._c()}/modules")

    def create_module(self, name, position):
        r = self.api("POST", f"{self._c()}/modules",
                     {"module": {"name": name, "position": position}})
        return r["id"]

    def update_module(self, module_id, **fields):
        return self.api("PUT", f"{self._c()}/modules/{module_id}", {"module": fields})

    def publish_module(self, module_id, published=True):
        # POST /modules ignores module[published] on create, so publishing is
        # always a separate PUT pass.
        return self.update_module(module_id, published=bool(published))

    def delete_module(self, module_id):
        return self.api("DELETE", f"{self._c()}/modules/{module_id}")

    def list_module_items(self, module_id):
        return self.paged(f"{self._c()}/modules/{module_id}/items")

    def add_module_item(self, module_id, item_type, position, title=None,
                        content_id=None, page_url=None, external_url=None, indent=0):
        mi = {"type": item_type, "position": position, "indent": indent}
        if title:
            mi["title"] = title
        # Pages are addressed by slug; content_id silently fails for them.
        if item_type == "Page":
            if not page_url:
                raise CanvasError("Page module items require page_url")
            mi["page_url"] = page_url
        elif item_type == "ExternalUrl":
            mi["external_url"] = external_url
        elif content_id is not None:
            mi["content_id"] = content_id
        r = self.api("POST", f"{self._c()}/modules/{module_id}/items", {"module_item": mi})
        return r["id"]

    def update_module_item(self, module_id, item_id, **fields):
        return self.api("PUT", f"{self._c()}/modules/{module_id}/items/{item_id}",
                        {"module_item": fields})

    def delete_module_item(self, module_id, item_id):
        return self.api("DELETE", f"{self._c()}/modules/{module_id}/items/{item_id}")

    # --------------------------------------------------------------- pages

    def create_page(self, title, body, published=True, front_page=False):
        r = self.api("POST", f"{self._c()}/pages", {"wiki_page": {
            "title": title, "body": body,
            "published": published, "front_page": front_page,
        }})
        return r["page_id"], r["url"]

    def update_page(self, page_url, **fields):
        r = self.api("PUT", f"{self._c()}/pages/{page_url}", {"wiki_page": fields})
        return r.get("page_id"), r.get("url", page_url)

    def get_page(self, page_url):
        return self.api("GET", f"{self._c()}/pages/{page_url}")

    # --------------------------------------------------------- assignments

    def list_assignment_groups(self):
        return self.paged(f"{self._c()}/assignment_groups")

    def create_assignment_group(self, name, position, weight=None):
        payload = {"name": name, "position": position}
        if weight is not None:
            payload["group_weight"] = weight
        return self.api("POST", f"{self._c()}/assignment_groups", payload)["id"]

    def delete_assignment_group(self, group_id):
        return self.api("DELETE", f"{self._c()}/assignment_groups/{group_id}")

    def create_assignment(self, name, description, points=0,
                          submission_types=None, group_id=None,
                          grading_type="points", published=True):
        payload = {
            "name": name,
            "description": description,
            "points_possible": points,
            "submission_types": submission_types or ["online_upload"],
            "grading_type": grading_type,
            "published": published,
        }
        if group_id:
            payload["assignment_group_id"] = group_id
        return self.api("POST", f"{self._c()}/assignments", {"assignment": payload})["id"]

    def update_assignment(self, assignment_id, **fields):
        return self.api("PUT", f"{self._c()}/assignments/{assignment_id}",
                        {"assignment": fields})

    # --------------------------------------------------------- discussions

    def create_discussion(self, title, message, points=None, group_id=None,
                          published=True):
        payload = {"title": title, "message": message, "published": published}
        if points is not None:
            payload["assignment"] = {"points_possible": points, "grading_type": "points"}
            if group_id:
                payload["assignment"]["assignment_group_id"] = group_id
        return self.api("POST", f"{self._c()}/discussion_topics", payload)["id"]

    def update_discussion(self, topic_id, **fields):
        return self.api("PUT", f"{self._c()}/discussion_topics/{topic_id}", fields)

    # -------------------------------------------------------------- quizzes

    def create_quiz(self, title, description, quiz_type="assignment",
                    allowed_attempts=-1, scoring_policy="keep_highest",
                    group_id=None, shuffle=False):
        payload = {
            "title": title,
            "description": description,
            "quiz_type": quiz_type,
            "published": False,          # publish only after questions land
            "shuffle_answers": shuffle,
        }
        if quiz_type == "assignment":
            payload["allowed_attempts"] = allowed_attempts
            payload["scoring_policy"] = scoring_policy
            if group_id:
                payload["assignment_group_id"] = group_id
        return self.api("POST", f"{self._c()}/quizzes", {"quiz": payload})["id"]

    def update_quiz(self, quiz_id, **fields):
        return self.api("PUT", f"{self._c()}/quizzes/{quiz_id}", {"quiz": fields})

    def list_quiz_questions(self, quiz_id):
        return self.paged(f"{self._c()}/quizzes/{quiz_id}/questions")

    def delete_quiz_question(self, quiz_id, question_id):
        return self.api("DELETE", f"{self._c()}/quizzes/{quiz_id}/questions/{question_id}")

    def add_quiz_question(self, quiz_id, payload):
        return self.api("POST", f"{self._c()}/quizzes/{quiz_id}/questions",
                        {"question": payload})

    def republish_quiz(self, quiz_id):
        """Force Canvas to regenerate quiz_data.

        Without this cycle question_count and points_possible can stay pinned
        at 0 forever after questions are POSTed.
        """
        self.update_quiz(quiz_id, published=False)
        self.update_quiz(quiz_id, published=True)

    def delete_quiz(self, quiz_id):
        return self.api("DELETE", f"{self._c()}/quizzes/{quiz_id}")

    # ---------------------------------------------------------------- files

    def upload_file(self, local_path, folder_path="/"):
        name = os.path.basename(local_path)
        size = os.path.getsize(local_path)
        r = self.api("POST", f"{self._c()}/files", {
            "name": name, "size": size,
            "parent_folder_path": folder_path,
            "on_duplicate": "overwrite",
        })
        upload_url = r["upload_url"]
        upload_params = r["upload_params"]

        boundary = "----OptimaCoursePipeline"
        body = bytearray()
        for k, v in upload_params.items():
            body += (f"--{boundary}\r\n"
                     f'Content-Disposition: form-data; name="{k}"\r\n\r\n{v}\r\n'
                     ).encode("utf-8")
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        body += (f"--{boundary}\r\n"
                 f'Content-Disposition: form-data; name="file"; filename="{name}"\r\n'
                 f"Content-Type: {ctype}\r\n\r\n").encode("utf-8")
        with open(local_path, "rb") as f:
            body += f.read()
        body += f"\r\n--{boundary}--\r\n".encode("utf-8")

        req = urllib.request.Request(upload_url, data=bytes(body), method="POST")
        req.add_header("Content-Type", f"multipart/form-data; boundary={boundary}")
        with urllib.request.urlopen(req, timeout=300) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
        result = json.loads(raw) if raw.strip() else {}
        if "id" not in result:
            raise CanvasError(f"file upload for {name} returned no id: {raw[:300]}")
        return result
