"""Minimal Home Assistant client: WebSocket API, REST API and File editor add-on access."""

import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid

import websockets

FILE_EDITOR_SLUG = "core_configurator"
CONFIG_ROOT = "/homeassistant"

SECRET_KEY = re.compile(r"pass|secret|token|key|login|credential|auth|cert|psk|private", re.I)
URL_CREDENTIALS = re.compile(r"(://)[^/\s:@]+:[^@\s]+@")


class HAError(Exception):
    def __init__(self, command, error):
        super().__init__(f"{command}: {error.get('code')} {error.get('message')}")
        self.code = error.get("code")


def redact(value, key=""):
    """Replace anything that looks like a credential with a placeholder."""
    if key and SECRET_KEY.search(key) and value not in (None, "", [], {}):
        return "<redacted>"
    if isinstance(value, dict):
        return {k: redact(v, k) for k, v in value.items()}
    if isinstance(value, list):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return URL_CREDENTIALS.sub(r"\1<redacted>@", value)
    return value


class HomeAssistant:
    def __init__(self):
        self.url = os.environ.get("HA_URL", "http://homeassistant.local:8123").rstrip("/")
        self.token = os.environ.get("HA_TOKEN") or sys.exit("HA_TOKEN is not set (see README).")
        self._ws = None
        self._id = 0
        self._file_editor = None

    async def __aenter__(self):
        ws_url = re.sub(r"^http", "ws", self.url) + "/api/websocket"
        for attempt in range(3):
            try:
                self._ws = await websockets.connect(ws_url, max_size=None, open_timeout=15)
                break
            except TimeoutError:
                if attempt == 2:
                    raise
        await self._ws.recv()
        await self._ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        msg = json.loads(await self._ws.recv())
        if msg["type"] != "auth_ok":
            sys.exit(f"Authentication failed: {msg.get('message')}")
        self.version = msg["ha_version"]
        return self

    async def __aexit__(self, *exc):
        await self._ws.close()

    async def call(self, command, **kwargs):
        self._id += 1
        await self._ws.send(json.dumps({"id": self._id, "type": command, **kwargs}))
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") == self._id and msg.get("type") == "result":
                break
        if not msg["success"]:
            raise HAError(command, msg["error"])
        return msg["result"]

    async def supervisor(self, endpoint, method="get", data=None):
        kwargs = {"data": data} if data is not None else {}
        return await self.call("supervisor/api", endpoint=endpoint, method=method, **kwargs)

    def _http(self, method, url, body=None, headers=None, content_type="application/json"):
        headers = dict(headers or {})
        if body is not None:
            headers["Content-Type"] = content_type
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()

    def rest(self, method, path, data=None):
        body = json.dumps(data).encode() if data is not None else None
        raw = self._http(method, f"{self.url}/api{path}", body, {"Authorization": f"Bearer {self.token}"})
        return json.loads(raw) if raw else None

    def download(self, path):
        return self._http("GET", f"{self.url}{path}", headers={"Authorization": f"Bearer {self.token}"})

    def upload_image(self, filename, data, content_type):
        boundary = uuid.uuid4().hex
        body = (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: {content_type}\r\n\r\n"
        ).encode() + data + f"\r\n--{boundary}--\r\n".encode()
        raw = self._http("POST", f"{self.url}/api/image/upload", body, {"Authorization": f"Bearer {self.token}"},
                         f"multipart/form-data; boundary={boundary}")
        return json.loads(raw)

    # --- config directory access through the File editor add-on (ingress) ---

    async def _file_editor_session(self):
        if self._file_editor is None:
            info = await self.supervisor(f"/addons/{FILE_EDITOR_SLUG}/info")
            if info.get("state") != "started":
                raise RuntimeError("The File editor add-on must be installed and running.")
            session = (await self.supervisor("/ingress/session", "post"))["session"]
            self._file_editor = (self.url + info["ingress_entry"], {"Cookie": f"ingress_session={session}"})
        return self._file_editor

    async def list_dir(self, path):
        base, headers = await self._file_editor_session()
        query = urllib.parse.urlencode({"path": f"{CONFIG_ROOT}/{path}".rstrip("/")})
        try:
            return json.loads(self._http("GET", f"{base}/api/listdir?{query}", headers=headers))["content"]
        except (ValueError, KeyError):  # the File editor answers a missing folder with a non-JSON body
            raise FileNotFoundError(path) from None

    async def read_file(self, path):
        """Return the text of a file in the config folder, or None if it does not exist."""
        base, headers = await self._file_editor_session()
        # The File editor answers "File not found" with HTTP 200, so check the folder listing first.
        parent, _, name = path.rpartition("/")
        try:
            if not any(e["name"] == name and e["type"] == "file" for e in await self.list_dir(parent)):
                return None
        except FileNotFoundError:
            return None
        query = urllib.parse.urlencode({"filename": f"{CONFIG_ROOT}/{path}"})
        return self._http("GET", f"{base}/api/file?{query}", headers=headers).decode("utf-8")

    async def write_file(self, path, text):
        base, headers = await self._file_editor_session()
        parent, _, name = f"{CONFIG_ROOT}/{path}".rpartition("/")
        if "/" in path:
            # Create missing parent folders one level at a time (no-op when they exist).
            current = CONFIG_ROOT
            for part in path.split("/")[:-1]:
                body = urllib.parse.urlencode({"path": current, "name": part}).encode()
                try:
                    self._http("POST", f"{base}/api/newfolder", body, headers, "application/x-www-form-urlencoded")
                except urllib.error.HTTPError:
                    pass
                current = f"{current}/{part}"
        body = urllib.parse.urlencode({"filename": f"{parent}/{name}", "text": text}).encode()
        result = json.loads(self._http("POST", f"{base}/api/save", body, headers, "application/x-www-form-urlencoded"))
        if result.get("error"):
            raise RuntimeError(f"Saving {path} failed: {result.get('message')}")
        return True
