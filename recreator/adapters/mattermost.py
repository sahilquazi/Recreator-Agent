"""
mattermost.py - Reproduces Mattermost bugs in a throwaway Docker sandbox.

The agent gets three kinds of tools, mirroring what a human tester uses:
  * API calls   - create users/teams/channels, read what the server returns
  * A browser   - open the real web app, click, read text, take screenshots,
                  and see every API call the page made (like DevTools)
  * Server logs - errors the server printed while the bug happened

Safety rules enforced in code (not just in the prompt):
  * API calls must go to /api/v4 on the sandbox URL.
  * The browser blocks every request that isn't to the sandbox.
  * Sandbox accounts use example.test emails and a sandbox-only password.
"""
import base64
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import requests

from ..config import Settings
from ..llm import Tool, truncate
from ..schemas import Ticket
from .base import AppAdapter

SANDBOX_PASSWORD = "Sandbox-Only-Pass-1!"   # never a real credential
ADMIN = "sysadmin"
_USERNAME = re.compile(r"^[a-z][a-z0-9._-]{2,21}$")  # Mattermost's username rules


class MattermostAdapter(AppAdapter):
    environment_notes = (
        "Mattermost mapping of ticket fields: tenant_setup.policies/enabled_addins = server "
        "config and plugins (change them with PUT /api/v4/config); affected_setup.user_roles = "
        "system and channel roles; 'structure' = teams and channels (public 'O', private 'P'). "
        f"A system admin called '{ADMIN}' already exists. Create other users with create_user."
    )

    @classmethod
    def handles(cls, app_name: str) -> bool:
        return "mattermost" in app_name.lower()

    def __init__(self, settings: Settings, run_dir: Path):
        self.s = settings
        self.base = settings.mm_url
        self.run_dir = run_dir
        self.build: str | None = None
        self.sessions: dict[str, requests.Session] = {}   # username -> logged-in session
        self.network: list[dict] = []                     # API calls seen by the browser
        self._pw = self._browser = self._context = self._page = None

    # ======================================================================
    # Sandbox lifecycle
    # ======================================================================
    def images_for(self, ticket: Ticket) -> tuple[str, str | None]:
        tmpl = self.s.mm_image_template
        current = self.s.mm_image or (tmpl.format(version=ticket.version) if tmpl and ticket.version else None)
        if not current:
            raise RuntimeError("Set MM_IMAGE, or MM_IMAGE_TEMPLATE plus a version in the ticket.")
        good = self.s.mm_last_good_image or (
            tmpl.format(version=ticket.last_good_version) if tmpl and ticket.last_good_version else None)
        return current, good

    def _compose(self, *args: str, timeout: int = 600) -> str:
        env = {**os.environ, "MM_IMAGE": self.build or ""}
        done = subprocess.run(["docker", "compose", "-f", str(self.s.mm_compose_file), *args],
                              env=env, capture_output=True, text=True, timeout=timeout)
        if done.returncode != 0:
            raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{done.stderr[-2000:]}")
        return done.stdout + done.stderr

    def reset(self, build: str) -> None:
        """Delete the old sandbox (including its database) and start fresh on `build`."""
        self.close()
        self.sessions.clear()
        self.network.clear()
        self._compose("down", "-v")      # -v also deletes the database volume
        self.build = build
        self._compose("up", "-d")
        self._wait_until_ready()
        # Create the admin through mmctl's local mode (works before any user exists).
        self._compose("exec", "-T", "mattermost", "mmctl", "--local", "user", "create",
                      "--email", f"{ADMIN}@example.test", "--username", ADMIN,
                      "--password", SANDBOX_PASSWORD, "--system-admin")
        self._session(ADMIN)

    def _wait_until_ready(self, timeout: int = 240) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                if requests.get(f"{self.base}/api/v4/system/ping", timeout=5).ok:
                    return
            except requests.RequestException:
                pass
            time.sleep(3)
        raise TimeoutError("Mattermost sandbox did not start in time")

    def _session(self, username: str) -> requests.Session:
        """Log in as a sandbox user once and reuse the session (token + cookies)."""
        if username not in self.sessions:
            s = requests.Session()
            r = s.post(f"{self.base}/api/v4/users/login",
                       json={"login_id": username, "password": SANDBOX_PASSWORD}, timeout=30)
            r.raise_for_status()
            s.headers.update({"Authorization": f"Bearer {r.headers['Token']}",
                              "X-Requested-With": "XMLHttpRequest"})
            self.sessions[username] = s
        return self.sessions[username]

    # ======================================================================
    # Tools: API
    # ======================================================================
    def create_user(self, username: str, system_admin: bool = False) -> dict:
        if not _USERNAME.match(username):
            raise ValueError("Use a lowercase placeholder username like user_1 (3-22 chars).")
        admin = self._session(ADMIN)
        r = admin.post(f"{self.base}/api/v4/users", timeout=30,
                       json={"username": username, "email": f"{username}@example.test",
                             "password": SANDBOX_PASSWORD})
        r.raise_for_status()
        user_id = r.json()["id"]
        if system_admin:
            admin.put(f"{self.base}/api/v4/users/{user_id}/roles", timeout=30,
                      json={"roles": "system_user system_admin"}).raise_for_status()
        return {"username": username, "id": user_id, "system_admin": system_admin}

    def api_request(self, method: str, path: str, body: dict | list | None = None,
                    as_user: str = ADMIN) -> dict:
        method = method.upper()
        if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
            raise ValueError("Unsupported method")
        if not path.startswith("/api/v4/") or ".." in path:
            raise ValueError("Path must start with /api/v4/ (sandbox API only)")
        r = self._session(as_user).request(method, f"{self.base}{path}", json=body, timeout=60)
        try:
            payload = r.json()
        except ValueError:
            payload = r.text
        return {"status": r.status_code, "body": payload}

    # ======================================================================
    # Tools: browser (Playwright)
    # ======================================================================
    def browser_open(self, as_user: str = ADMIN, path: str = "/") -> str:
        """Open the web app logged in as a sandbox user, reusing the API login cookies."""
        from playwright.sync_api import sync_playwright  # imported here so API-only use needs no browser

        if self._pw is None:
            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
        if self._context:
            self._context.close()
        self._context = self._browser.new_context(viewport={"width": 1400, "height": 900})
        session = self._session(as_user)
        self._context.add_cookies([{"name": c.name, "value": c.value, "url": self.base}
                                   for c in session.cookies])

        # Hard block: anything not on the sandbox origin is aborted.
        self._context.route("**/*", lambda route: route.continue_()
                            if route.request.url.startswith(self.base) else route.abort())

        self._page = self._context.new_page()
        self._page.on("response", self._record_response)
        self._page.goto(f"{self.base}{path}", wait_until="networkidle")
        return f"Opened {path} as {as_user}. Title: {self._page.title()}"

    def _record_response(self, response) -> None:
        """Keep a DevTools-style log of the page's API calls."""
        if "/api/v4/" not in response.url:
            return
        try:
            body = response.text()[:1500]
        except Exception:  # noqa: BLE001 - some bodies (redirects, streams) can't be read
            body = ""
        self.network.append({"method": response.request.method,
                             "path": response.url.replace(self.base, ""),
                             "status": response.status, "body": body})

    def _need_page(self):
        if self._page is None:
            raise RuntimeError("Call browser_open first")
        return self._page

    def browser_goto(self, path: str) -> str:
        page = self._need_page()
        page.goto(f"{self.base}{path}", wait_until="networkidle")
        return f"At {page.url.replace(self.base, '')}"

    def browser_click(self, selector: str | None = None, text: str | None = None) -> str:
        page = self._need_page()
        target = page.get_by_text(text, exact=False).first if text else page.locator(selector).first
        target.click(timeout=10_000)
        page.wait_for_load_state("networkidle")
        return "Clicked"

    def browser_type(self, selector: str, text: str, press_enter: bool = False) -> str:
        page = self._need_page()
        page.locator(selector).first.fill(text, timeout=10_000)
        if press_enter:
            page.keyboard.press("Enter")
            page.wait_for_load_state("networkidle")
        return "Typed"

    def browser_read(self, selector: str = "body") -> str:
        return truncate(self._need_page().locator(selector).first.inner_text(timeout=10_000), 8000)

    def browser_find(self, query: str) -> list[dict]:
        """List elements whose aria-label, title or text mentions `query`, to find selectors."""
        return self._need_page().evaluate(
            """q => [...document.querySelectorAll('[aria-label],[title],button,a,h1,h2,h3,span')]
                 .filter(e => (e.getAttribute('aria-label')||e.title||e.innerText||'').toLowerCase()
                              .includes(q.toLowerCase()))
                 .slice(0, 25)
                 .map(e => ({tag: e.tagName.toLowerCase(), aria: e.getAttribute('aria-label'),
                             id: e.id, cls: e.className.toString().slice(0, 80),
                             text: (e.innerText||'').slice(0, 80)}))""", query)

    def browser_screenshot(self, name: str) -> list[dict]:
        """Save a screenshot to the run folder AND show it to the agent as an image."""
        safe = re.sub(r"[^\w-]", "_", name)[:50] + ".png"
        path = self.run_dir / safe
        png = self._need_page().screenshot(path=str(path), full_page=False)
        return [{"type": "text", "text": f"Saved {safe}"},
                {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                             "data": base64.b64encode(png).decode()}}]

    def network_log(self, contains: str | None = None, last: int = 30) -> list[dict]:
        calls = [c for c in self.network if not contains or contains in c["path"]]
        return calls[-last:]

    # ======================================================================
    # Tools: server side
    # ======================================================================
    def server_logs(self, lines: int = 200, grep: str | None = None) -> str:
        out = self._compose("logs", "--no-color", "--tail", str(min(lines, 2000)), "mattermost", timeout=60)
        if grep:
            out = "\n".join(l for l in out.splitlines() if grep.lower() in l.lower())
        return truncate(out, 12_000)

    def run_repro_script(self, code: str) -> tuple[int, str]:
        """
        Runs the agent's script with only the sandbox URL and sandbox admin login.
        Convention (same as the pilot): exit 1 = bug present, exit 0 = bug absent.
        NOTE: this runs on the host. For stronger isolation, run it inside a
        container on the sandbox's Docker network instead.
        """
        script = self.run_dir / "repro.py"
        script.write_text(code)
        env = {"PATH": os.environ.get("PATH", ""), "MM_URL": self.base,
               "MM_ADMIN_USER": ADMIN, "MM_PASSWORD": SANDBOX_PASSWORD}
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, str(script)], cwd=tmp, env=env,
                                  capture_output=True, text=True, timeout=180)
        return done.returncode, truncate(done.stdout + done.stderr, 6000)

    def _repro_tool(self, code: str) -> str:
        rc, out = self.run_repro_script(code)
        meaning = {1: "bug PRESENT", 0: "bug ABSENT"}.get(rc, "script ERROR (fix it)")
        return f"exit {rc} ({meaning})\n{out}"

    # ======================================================================
    # Tool list handed to the agent
    # ======================================================================
    def tools(self) -> list[Tool]:
        obj = lambda props, req=(): {"type": "object", "properties": props, "required": list(req)}  # noqa: E731
        s, b, i = {"type": "string"}, {"type": "boolean"}, {"type": "integer"}
        return [
            Tool("create_user", "Create a sandbox user (placeholder name like user_1).",
                 obj({"username": s, "system_admin": b}, ["username"]), self.create_user),
            Tool("api_request", "Call the sandbox REST API (/api/v4/...) as a sandbox user. "
                 "Use it to build teams/channels/posts and to read what the server returns.",
                 obj({"method": s, "path": s, "body": {"type": ["object", "array", "null"]}, "as_user": s},
                     ["method", "path"]), self.api_request),
            Tool("browser_open", "Open the web app logged in as a sandbox user.",
                 obj({"as_user": s, "path": s}), self.browser_open),
            Tool("browser_goto", "Navigate to a path, e.g. /teamname/channels/channelname.",
                 obj({"path": s}, ["path"]), self.browser_goto),
            Tool("browser_find", "Find elements by aria-label/title/text to get selectors.",
                 obj({"query": s}, ["query"]), self.browser_find),
            Tool("browser_click", "Click an element by CSS selector or visible text.",
                 obj({"selector": s, "text": s}), self.browser_click),
            Tool("browser_type", "Type into an element.",
                 obj({"selector": s, "text": s, "press_enter": b}, ["selector", "text"]), self.browser_type),
            Tool("browser_read", "Read the visible text of an element (default: whole page).",
                 obj({"selector": s}), self.browser_read),
            Tool("browser_screenshot", "Take a screenshot (you will see it) and save it as evidence.",
                 obj({"name": s}, ["name"]), self.browser_screenshot),
            Tool("network_log", "API calls the browser page made, with status and body.",
                 obj({"contains": s, "last": i}), self.network_log),
            Tool("server_logs", "Recent Mattermost server logs, optionally filtered.",
                 obj({"lines": i, "grep": s}), self.server_logs),
            Tool("run_repro_script", "Run a self-contained Python repro script against the sandbox. "
                 "It gets MM_URL, MM_ADMIN_USER, MM_PASSWORD env vars and may use `requests`. "
                 "It must create its own data and exit 1 if the bug is present, 0 if not.",
                 obj({"code": s}, ["code"]), self._repro_tool),
        ]

    def close(self) -> None:
        for thing in (self._context, self._browser):
            try:
                thing and thing.close()
            except Exception:  # noqa: BLE001
                pass
        if self._pw:
            self._pw.stop()
        self._pw = self._browser = self._context = self._page = None
