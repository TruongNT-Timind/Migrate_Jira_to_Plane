#!/usr/bin/env python3
"""Web local để kéo một project Jira, chọn mapping, rồi ghi sang Plane.

Chỉ lắng nghe 127.0.0.1. Chạy từ thư mục jira-to-plane:

    python3 server.py
"""

from __future__ import annotations

import json
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from base64 import b64encode
from html import escape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"
STATIC = Path(__file__).resolve().parent / "static"
CONFIG_PATH = DATA / "config.json"
SNAPSHOT_PATH = DATA / "snapshot.json"
CHECKPOINT_PATH = DATA / "checkpoint.json"
HOST = "127.0.0.1"
PORT = 8765

STORY_POINTS_FIELD = "customfield_10016"
START_DATE_FIELD = "customfield_10015"
PRIORITY_MAP = {
    "highest": "urgent",
    "blocker": "urgent",
    "high": "high",
    "medium": "medium",
    "low": "low",
    "lowest": "none",
}
MODULE_STATUS = {
    "idea": "backlog",
    "backlog": "backlog",
    "to do": "planned",
    "pending": "planned",
    "in progress": "in-progress",
    "reopened": "in-progress",
    "fixing": "in-progress",
    "testing": "in-progress",
    "in testing": "in-progress",
    "verifying": "in-progress",
    "ready to golive": "in-progress",
    "done": "completed",
    "closed": "completed",
    "canceled": "cancelled",
    "cancelled": "cancelled",
}
SEARCH_FIELDS = [
    "summary",
    "description",
    "issuetype",
    "status",
    "priority",
    "labels",
    "components",
    "assignee",
    "reporter",
    "parent",
    "comment",
    "attachment",
    "issuelinks",
    "created",
    "updated",
    "duedate",
    STORY_POINTS_FIELD,
    START_DATE_FIELD,
]

job_lock = threading.Lock()
job = {"running": False, "done": False, "error": None, "lines": []}


def ssl_context():
    cafile = os.environ.get("SSL_CERT_FILE")
    if not cafile and os.path.isfile("/etc/ssl/cert.pem"):
        cafile = "/etc/ssl/cert.pem"
    if cafile:
        return ssl.create_default_context(cafile=cafile)
    return ssl.create_default_context()


def load_env(path: Path) -> dict:
    values = {}
    if not path.exists():
        return values
    for raw in path.read_text().splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def env_config() -> dict:
    env = load_env(ROOT / ".env")
    return {
        "jira_base_url": env.get("JIRA_BASE_URL", ""),
        "jira_email": env.get("JIRA_EMAIL", ""),
        "jira_api_token": env.get("JIRA_API_TOKEN", ""),
        "jira_project_key": env.get("JIRA_PROJECT_KEY", ""),
        "plane_base_url": env.get("PLANE_BASE_URL", ""),
        "plane_api_key": env.get("PLANE_API_KEY", ""),
        "plane_workspace_slug": env.get("PLANE_WORKSPACE_SLUG", ""),
        "plane_project_id": env.get("PLANE_PROJECT_ID", ""),
    }


def read_json(path: Path, default):
    if not path.exists():
        return default
    return json.loads(path.read_text())


def write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def current_config() -> dict:
    config = env_config()
    saved = read_json(CONFIG_PATH, {})
    config.update({key: value for key, value in saved.items() if value not in (None, "")})
    return config


class ApiError(RuntimeError):
    def __init__(self, message: str, status: int = 0):
        super().__init__(message)
        self.status = status


def request_json(method: str, url: str, headers: dict, body: dict | None = None, raw: bytes | None = None):
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    for attempt in range(6):
        req = urllib.request.Request(url, data=data, method=method)
        for key, value in headers.items():
            req.add_header(key, value)
        if body is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=90, context=ssl_context()) as response:
                payload = response.read()
                if not payload:
                    return response.status, None
                content_type = response.headers.get("Content-Type", "")
                if "application/json" in content_type or payload[:1] in (b"{", b"["):
                    return response.status, json.loads(payload.decode())
                return response.status, payload
        except urllib.error.HTTPError as error:
            payload = error.read().decode(errors="replace")
            if error.code == 429 and attempt < 5:
                retry_after = error.headers.get("Retry-After")
                wait = int(retry_after) if retry_after and retry_after.isdigit() else min(5 * (2**attempt), 60)
                log_line(f"Plane giới hạn tốc độ, chờ {wait} giây rồi thử lại.")
                time.sleep(wait)
                continue
            try:
                parsed = json.loads(payload) if payload else None
            except json.JSONDecodeError:
                parsed = payload[:500]
            raise ApiError(f"{method} {url} -> {error.code}: {parsed}", error.code) from error
        except urllib.error.URLError as error:
            if attempt < 5:
                wait = min(5 * (2**attempt), 60)
                log_line(f"Không phân giải được tên miền, chờ {wait} giây rồi thử lại.")
                time.sleep(wait)
                continue
            raise ApiError(f"{method} {url} -> {error}") from error
    raise ApiError(f"{method} {url} -> 429: hết số lần thử lại", 429)


class JiraClient:
    def __init__(self, config: dict):
        self.base = config["jira_base_url"].rstrip("/")
        token = b64encode(f"{config['jira_email']}:{config['jira_api_token']}".encode()).decode()
        self.headers = {"Authorization": f"Basic {token}", "Accept": "application/json"}
        self.project_key = config["jira_project_key"].upper()

    def get(self, path: str):
        _, payload = request_json("GET", self.base + path, self.headers)
        return payload

    def post(self, path: str, body: dict):
        _, payload = request_json("POST", self.base + path, self.headers, body)
        return payload

    def test(self) -> dict:
        me = self.get("/rest/api/3/myself")
        project = self.get("/rest/api/3/project/" + urllib.parse.quote(self.project_key))
        return {
            "account": me.get("displayName"),
            "email": me.get("emailAddress"),
            "project_key": project.get("key"),
            "project_name": project.get("name"),
        }

    def statuses(self) -> list[str]:
        rows = self.get("/rest/api/3/project/" + urllib.parse.quote(self.project_key) + "/statuses")
        names = []
        for issue_type in rows:
            for status in issue_type.get("statuses") or []:
                name = status.get("name")
                if name and name not in names:
                    names.append(name)
        return names

    def issues(self) -> list[dict]:
        try:
            return self._search(SEARCH_FIELDS)
        except ApiError:
            fields = [name for name in SEARCH_FIELDS if not name.startswith("customfield_")]
            return self._search(fields)

    def _search(self, fields: list[str]) -> list[dict]:
        found = []
        token = None
        while True:
            body = {
                "jql": f"project = {self.project_key} ORDER BY key ASC",
                "maxResults": 50,
                "fields": fields,
            }
            if token:
                body["nextPageToken"] = token
            page = self.post("/rest/api/3/search/jql", body)
            found.extend(page.get("issues") or [])
            if page.get("isLast") or not page.get("nextPageToken"):
                break
            token = page["nextPageToken"]
        return found

    def rendered_description(self, key: str) -> str:
        issue = self.get(
            f"/rest/api/3/issue/{urllib.parse.quote(key)}?fields=description&expand=renderedFields"
        )
        return (issue.get("renderedFields") or {}).get("description") or ""

    def comments(self, key: str) -> list[dict]:
        start = 0
        rows = []
        while True:
            page = self.get(
                f"/rest/api/3/issue/{urllib.parse.quote(key)}/comment"
                f"?expand=renderedBody&startAt={start}&maxResults=100"
            )
            batch = page.get("comments") or []
            rows.extend(batch)
            start += len(batch)
            if not batch or start >= page.get("total", 0):
                break
        return rows


class PlaneClient:
    def __init__(self, config: dict):
        self.base = config["plane_base_url"].rstrip("/")
        self.headers = {"X-API-Key": config["plane_api_key"], "Accept": "application/json"}
        self.slug = config["plane_workspace_slug"]
        self.project_id = config["plane_project_id"]

    def url(self, path: str) -> str:
        return self.base + path

    def get(self, path: str):
        _, payload = request_json("GET", self.url(path), self.headers)
        return payload

    def send(self, method: str, path: str, body: dict):
        time.sleep(0.4)
        _, payload = request_json(method, self.url(path), self.headers, body)
        return payload

    def project_path(self, suffix: str) -> str:
        return f"/api/v1/workspaces/{self.slug}/projects/{self.project_id}{suffix}"

    def test(self) -> dict:
        me = self.get("/api/v1/users/me/")
        project = self.get(self.project_path("/"))
        return {
            "account": me.get("email") or me.get("display_name"),
            "project_name": project.get("name"),
            "project_id": project.get("id"),
        }

    def states(self) -> list[dict]:
        payload = self.get(self.project_path("/states/?per_page=100"))
        return [
            {"id": row.get("id"), "name": row.get("name"), "group": row.get("group")}
            for row in payload.get("results") or []
        ]

    def members_by_email(self) -> dict[str, str]:
        payload = self.get(f"/api/v1/workspaces/{self.slug}/members/")
        rows = payload if isinstance(payload, list) else payload.get("results") or []
        found = {}
        for row in rows:
            email = (row.get("email") or "").lower()
            if email and row.get("id"):
                found[email] = row["id"]
        return found

    def labels_by_name(self) -> dict[str, str]:
        payload = self.get(self.project_path("/labels/?per_page=100"))
        return {row["name"]: row["id"] for row in payload.get("results") or [] if row.get("name")}

    def ensure_label(self, cache: dict[str, str], name: str) -> str:
        if name in cache:
            return cache[name]
        created = self.send("POST", self.project_path("/labels/"), {"name": name, "color": "#60646C"})
        cache[name] = created["id"]
        return created["id"]

    def create_module(self, fields: dict) -> dict:
        try:
            return self.send("POST", self.project_path("/modules/"), fields)
        except ApiError as error:
            if error.status == 400 and "external_id" in fields:
                slim = {key: value for key, value in fields.items() if key not in ("external_id", "external_source")}
                return self.send("POST", self.project_path("/modules/"), slim)
            raise

    def add_to_module(self, module_id: str, work_item_id: str) -> None:
        self.send(
            "POST",
            self.project_path(f"/modules/{module_id}/module-issues/"),
            {"issues": [work_item_id]},
        )

    def create_work_item(self, fields: dict) -> dict:
        try:
            return self.send("POST", self.project_path("/work-items/"), fields)
        except ApiError as error:
            if error.status == 400 and "external_id" in fields:
                slim = {key: value for key, value in fields.items() if key not in ("external_id", "external_source")}
                return self.send("POST", self.project_path("/work-items/"), slim)
            raise

    def create_comment(self, work_item_id: str, html: str, external_id: str) -> None:
        self.send(
            "POST",
            self.project_path(f"/work-items/{work_item_id}/comments/"),
            {
                "comment_html": html,
                "access": "INTERNAL",
                "external_source": "jira",
                "external_id": external_id,
            },
        )

    def create_link(self, work_item_id: str, url: str, title: str) -> None:
        self.send(
            "POST",
            self.project_path(f"/work-items/{work_item_id}/links/"),
            {"url": url, "title": title},
        )


def adf_to_text(node) -> str:
    if not isinstance(node, dict):
        return ""
    if node.get("type") == "text":
        return node.get("text") or ""
    chunks = []
    for child in node.get("content") or []:
        chunks.append(adf_to_text(child))
    text = "".join(chunks)
    if node.get("type") in ("paragraph", "heading", "listItem"):
        return text + "\n"
    return text


def text_to_html(text: str) -> str:
    parts = [f"<p>{escape(line)}</p>" for line in text.splitlines() if line.strip()]
    return "".join(parts) or "<p></p>"


def issue_fields(issue: dict) -> dict:
    return issue.get("fields") or {}


def issue_type_name(issue: dict) -> str:
    return ((issue_fields(issue).get("issuetype") or {}).get("name") or "").strip()


def issue_type_level(issue: dict) -> int | None:
    return (issue_fields(issue).get("issuetype") or {}).get("hierarchyLevel")


def is_epic(issue: dict) -> bool:
    info = issue_fields(issue).get("issuetype") or {}
    name = (info.get("untranslatedName") or info.get("name") or "").lower()
    return issue_type_level(issue) == 1 or name == "epic"


def is_subtask(issue: dict) -> bool:
    info = issue_fields(issue).get("issuetype") or {}
    name = (info.get("untranslatedName") or info.get("name") or "").lower()
    return bool(info.get("subtask")) or issue_type_level(issue) == -1 or name in ("subtask", "sub-task")


def parent_key(issue: dict) -> str | None:
    parent = issue_fields(issue).get("parent") or {}
    return parent.get("key")


def suggest_status_map(statuses: list[str], states: list[dict]) -> dict[str, str]:
    by_name = {state["name"].strip().lower(): state["id"] for state in states}
    aliases = {
        "idea": "backlog",
        "to do": "todo",
        "pending": "todo",
        "in progress": "in progress",
        "reopened": "in progress",
        "fixing": "in progress",
        "testing": "in testing",
        "verifying": "in testing",
        "canceled": "cancelled",
        "cancelled": "cancelled",
        "done": "done",
        "closed": "closed",
        "ready to golive": "ready to golive",
    }
    mapping = {}
    for status in statuses:
        key = status.strip().lower()
        target = aliases.get(key, key)
        mapping[status] = by_name.get(target, "")
    return mapping


def summarize(issues: list[dict]) -> list[dict]:
    rows = []
    for issue in issues:
        fields = issue_fields(issue)
        points = fields.get(STORY_POINTS_FIELD)
        rows.append(
            {
                "key": issue.get("key"),
                "id": issue.get("id"),
                "summary": fields.get("summary"),
                "type": (fields.get("issuetype") or {}).get("name"),
                "epic": is_epic(issue),
                "subtask": is_subtask(issue),
                "status": (fields.get("status") or {}).get("name"),
                "priority": (fields.get("priority") or {}).get("name"),
                "parent": parent_key(issue),
                "points": points,
                "comments": (fields.get("comment") or {}).get("total") or 0,
                "attachments": len(fields.get("attachment") or []),
                "created": fields.get("created"),
                "updated": fields.get("updated"),
            }
        )
    return rows


def log_line(text: str) -> None:
    with job_lock:
        job["lines"].append(text)
    print(text, flush=True)


def work_item_html(jira: JiraClient, issue: dict, assignee_note: str) -> str:
    fields = issue_fields(issue)
    rendered = jira.rendered_description(issue["key"])
    html = rendered or text_to_html(adf_to_text(fields.get("description")))
    points = fields.get(STORY_POINTS_FIELD)
    extras = [
        f"Jira key: {issue.get('key')}",
        f"Jira created: {fields.get('created')}",
        f"Jira updated: {fields.get('updated')}",
    ]
    if points not in (None, ""):
        extras.append(f"Story points: {points}")
    if assignee_note:
        extras.append(assignee_note)
    reporter = ((fields.get("reporter") or {}).get("displayName")) or ""
    if reporter:
        extras.append(f"Jira reporter: {reporter}")
    html += text_to_html("\n".join(extras))
    return html


def build_label_ids(plane: PlaneClient, issue: dict, cache: dict[str, str]) -> list[str]:
    fields = issue_fields(issue)
    names = list(fields.get("labels") or [])
    for component in fields.get("components") or []:
        if component.get("name"):
            names.append("component:" + component["name"])
    ids = []
    for name in names:
        ids.append(plane.ensure_label(cache, name))
    return ids


def issues_for_types(issues: list[dict], type_names: list[str] | None) -> tuple[list[dict], int]:
    if type_names is None:
        chosen = list(issues)
    else:
        wanted = {name.strip().lower() for name in type_names if str(name).strip()}
        if not wanted:
            raise RuntimeError("Chưa chọn loại issue nào.")
        chosen = [issue for issue in issues if issue_type_name(issue).lower() in wanted]
    chosen_ids = {issue["id"] for issue in chosen}
    by_key = {issue.get("key"): issue for issue in issues}
    kept = []
    skipped_children = 0
    for issue in chosen:
        if is_subtask(issue):
            parent = by_key.get(parent_key(issue) or "")
            if parent and parent.get("id") not in chosen_ids:
                skipped_children += 1
                continue
        kept.append(issue)
    return kept, skipped_children


def migrate(config: dict, status_map: dict, dry_run: bool, type_names: list[str] | None = None) -> None:
    snapshot = read_json(SNAPSHOT_PATH, {})
    issues = snapshot.get("issues") or []
    if not issues:
        raise RuntimeError("Chưa kéo dữ liệu Jira. Bấm Kéo dữ liệu trước.")
    selected, skipped_children = issues_for_types(issues, type_names)
    if not selected:
        raise RuntimeError("Không có issue nào thuộc loại đã chọn.")
    needed_statuses = sorted({
        (issue_fields(issue).get("status") or {}).get("name") or ""
        for issue in selected
    })
    missing = [name for name in needed_statuses if name and not status_map.get(name)]
    if missing:
        raise RuntimeError("Còn status chưa chọn state Plane: " + ", ".join(missing))

    jira = JiraClient(config)
    plane = PlaneClient(config)
    checkpoint = read_json(CHECKPOINT_PATH, {})
    epic_ids = {issue["key"]: issue["id"] for issue in selected if is_epic(issue)}
    members = {} if dry_run else plane.members_by_email()
    labels = {} if dry_run else plane.labels_by_name()

    def remember(jira_id: str, kind: str, plane_id: str) -> None:
        checkpoint[jira_id] = {"kind": kind, "plane_id": plane_id}
        write_json(CHECKPOINT_PATH, checkpoint)

    type_label = ", ".join(sorted({issue_type_name(issue) for issue in selected}))
    log_line(("Xem trước" if dry_run else "Bắt đầu ghi") + f" {len(selected)}/{len(issues)} issue. Loại: {type_label}.")
    if skipped_children:
        log_line(f"Bỏ qua {skipped_children} sub-task vì issue cha không nằm trong loại đã chọn.")
    for issue in selected:
        if not is_epic(issue):
            continue
        fields = issue_fields(issue)
        status_name = (fields.get("status") or {}).get("name") or ""
        module_status = MODULE_STATUS.get(status_name.strip().lower(), "backlog")
        if issue["id"] in checkpoint:
            log_line(f"Bỏ qua module đã có {issue['key']}")
            continue
        log_line(f"Module {issue['key']} {fields.get('summary')} ({module_status})")
        if dry_run:
            continue
        created = plane.create_module(
            {
                "name": fields.get("summary") or issue["key"],
                "description": f"{issue['key']}\n\n{adf_to_text(fields.get('description'))}".strip(),
                "status": module_status,
                "external_source": "jira",
                "external_id": issue["id"],
            }
        )
        remember(issue["id"], "module", created["id"])

    def create_item(issue: dict, plane_parent: str | None, module_jira_id: str | None) -> None:
        fields = issue_fields(issue)
        if issue["id"] in checkpoint:
            log_line(f"Bỏ qua work item đã có {issue['key']}")
            return
        status_name = (fields.get("status") or {}).get("name") or ""
        priority_name = ((fields.get("priority") or {}).get("name") or "").lower()
        email = ((fields.get("assignee") or {}).get("emailAddress") or "").lower()
        assignee_note = ""
        assignees = []
        if email and email in members:
            assignees = [members[email]]
        elif email:
            assignee_note = f"Jira assignee: {email}"
        label_ids = [] if dry_run else build_label_ids(plane, issue, labels)
        payload = {
            "name": fields.get("summary") or issue["key"],
            "description_html": "<p></p>" if dry_run else work_item_html(jira, issue, assignee_note),
            "priority": PRIORITY_MAP.get(priority_name, "none"),
            "state": status_map[status_name],
            "labels": label_ids,
            "external_source": "jira",
            "external_id": issue["id"],
        }
        if assignees:
            payload["assignees"] = assignees
        if plane_parent:
            payload["parent"] = plane_parent
        due = fields.get("duedate")
        if due:
            payload["target_date"] = due
        start = fields.get(START_DATE_FIELD)
        if isinstance(start, str) and len(start) >= 10:
            payload["start_date"] = start[:10]
        log_line(f"Work item {issue['key']} {fields.get('summary')}")
        if dry_run:
            return
        created = plane.create_work_item(payload)
        remember(issue["id"], "work_item", created["id"])
        if module_jira_id and module_jira_id in checkpoint:
            plane.add_to_module(checkpoint[module_jira_id]["plane_id"], created["id"])
        link = jira.base + "/browse/" + issue["key"]
        try:
            plane.create_link(created["id"], link, "Jira " + issue["key"])
        except ApiError as error:
            log_line(f"Link {issue['key']} lỗi: {error}")
        for comment in jira.comments(issue["key"]):
            author = ((comment.get("author") or {}).get("displayName")) or "Jira"
            body = comment.get("renderedBody") or text_to_html(adf_to_text(comment.get("body")))
            html = f"<p><strong>{escape(author)}</strong> · {escape(comment.get('created') or '')}</p>{body}"
            try:
                plane.create_comment(created["id"], html, str(comment.get("id")))
            except ApiError as error:
                log_line(f"Comment {issue['key']} lỗi: {error}")

    for issue in selected:
        if is_epic(issue) or is_subtask(issue):
            continue
        module_jira_id = epic_ids.get(parent_key(issue) or "")
        create_item(issue, None, module_jira_id)

    for issue in selected:
        if not is_subtask(issue):
            continue
        parent = parent_key(issue)
        parent_issue = next((row for row in issues if row.get("key") == parent), None)
        plane_parent = None
        module_jira_id = None
        if parent_issue:
            module_jira_id = epic_ids.get(parent_key(parent_issue) or "")
            if not dry_run:
                saved = checkpoint.get(parent_issue["id"]) or {}
                plane_parent = saved.get("plane_id")
        create_item(issue, plane_parent, module_jira_id)

    log_line("Xong.")


def start_job(target) -> None:
    with job_lock:
        if job["running"]:
            raise RuntimeError("Đang có một lệnh chạy.")
        job["running"] = True
        job["done"] = False
        job["error"] = None
        job["lines"] = []

    def run():
        try:
            target()
        except Exception as error:  # noqa: BLE001 - surface any failure in the job log
            log_line(str(error))
            with job_lock:
                job["error"] = str(error)
        finally:
            with job_lock:
                job["running"] = False
                job["done"] = True

    threading.Thread(target=run, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print("[web]", fmt % args, flush=True)

    def send_json(self, status: int, payload) -> None:
        raw = json.dumps(payload, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def read_body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length).decode())

    def do_GET(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        if path == "/api/config":
            self.send_json(200, current_config())
            return
        if path == "/api/job":
            with job_lock:
                self.send_json(200, dict(job))
            return
        if path == "/api/snapshot":
            snapshot = read_json(SNAPSHOT_PATH, {})
            self.send_json(
                200,
                {
                    "project_key": snapshot.get("project_key"),
                    "statuses": snapshot.get("statuses") or [],
                    "plane_states": snapshot.get("plane_states") or [],
                    "suggested_status_map": snapshot.get("suggested_status_map") or {},
                    "issues": snapshot.get("summary") or [],
                },
            )
            return
        if path in ("/", "/index.html"):
            self.serve_file(STATIC / "index.html", "text/html; charset=utf-8")
            return
        self.send_json(404, {"error": "Không thấy đường dẫn"})

    def serve_file(self, path: Path, content_type: str) -> None:
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_POST(self):  # noqa: N802
        path = urllib.parse.urlparse(self.path).path
        try:
            body = self.read_body()
            if path == "/api/config":
                write_json(CONFIG_PATH, body)
                self.send_json(200, {"ok": True})
                return
            config = current_config()
            config.update({key: value for key, value in body.items() if key in config or key.startswith(("jira_", "plane_"))})
            if path == "/api/test/jira":
                self.send_json(200, JiraClient(config).test())
                return
            if path == "/api/test/plane":
                self.send_json(200, PlaneClient(config).test())
                return
            if path == "/api/pull":
                self.pull(config)
                return
            if path == "/api/migrate":
                status_map = body.get("status_map") or {}
                dry_run = bool(body.get("dry_run"))
                issue_types = body.get("issue_types")
                if issue_types is not None and not isinstance(issue_types, list):
                    raise RuntimeError("Danh sách loại issue không hợp lệ.")
                start_job(lambda: migrate(config, status_map, dry_run, issue_types))
                self.send_json(202, {"ok": True})
                return
            self.send_json(404, {"error": "Không thấy đường dẫn"})
        except (ApiError, RuntimeError, KeyError, json.JSONDecodeError) as error:
            self.send_json(400, {"error": str(error)})

    def pull(self, config: dict) -> None:
        jira = JiraClient(config)
        plane = PlaneClient(config)
        issues = jira.issues()
        statuses = jira.statuses()
        states = plane.states()
        summary = summarize(issues)
        snapshot = {
            "project_key": config["jira_project_key"].upper(),
            "statuses": statuses,
            "plane_states": states,
            "suggested_status_map": suggest_status_map(statuses, states),
            "summary": summary,
            "issues": issues,
        }
        write_json(SNAPSHOT_PATH, snapshot)
        self.send_json(
            200,
            {
                "project_key": snapshot["project_key"],
                "total": len(issues),
                "statuses": statuses,
                "plane_states": states,
                "suggested_status_map": snapshot["suggested_status_map"],
                "issues": summary,
            },
        )


def main() -> None:
    DATA.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    print(f"Mở http://{HOST}:{PORT}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
