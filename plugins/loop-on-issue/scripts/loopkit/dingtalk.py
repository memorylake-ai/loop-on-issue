"""Outbound DingTalk, using nothing but the standard library.

Sending is `urllib` and `hmac`, so the core CLI keeps its no-dependency property.
Only *receiving* needs the Stream SDK, and that lives in the optional listener.

Two send paths, and the difference matters:

* **App robot** (`groupMessages/send`, `oToMessages/batchSend`) returns a
  `processQueryKey`. That key coming back on a quote-reply is what routes an
  answer to the right question, so this is the path that makes conversation work.
* **Custom webhook** is send-only and returns no key. Kept as a fallback for a
  machine with no app credentials: a notification still arrives, but nobody can
  answer it in DingTalk — they answer on the issue instead.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, List, Optional

API = "https://api.dingtalk.com"
TOKEN_URL = API + "/v1.0/oauth2/accessToken"
GROUP_URL = API + "/v1.0/robot/groupMessages/send"
DM_URL = API + "/v1.0/robot/oToMessages/batchSend"

#: Searched in order; the first file that exists wins, then the process
#: environment. Credentials never live in a repository.
def default_env_paths() -> List[str]:
    explicit = os.environ.get("LOOP_DINGTALK_ENV")
    if explicit:
        # An explicit path means *that* file and no other. Falling through to the
        # machine-level one anyway would let a test — or a second workspace —
        # silently pick up credentials it was pointed away from.
        return [explicit]
    paths = [os.path.join(os.path.expanduser("~"), ".loop-on-issue", "dingtalk.env")]
    loops = os.environ.get("LOOPS_DIR")
    if loops:
        # Compatibility with an existing deployment that already keeps DingTalk
        # credentials for other tooling.
        paths.append(os.path.join(loops, ".env.dingtalk"))
    return paths


def load_env(paths: Optional[List[str]] = None, environ: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    """Read shell-style `KEY="value"` files, most significant first.

    A value in a file beats one exported in the process environment: the file is
    deliberate configuration, and a stray variable in somebody's shell profile
    should not silently redirect the bot to another workspace.
    """
    environ = os.environ if environ is None else environ
    result: Dict[str, str] = {}
    for path in (default_env_paths() if paths is None else paths):
        try:
            with open(path) as fh:
                lines = fh.readlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[len("export "):].strip()
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip()
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result.setdefault(key, value)
    for key, value in environ.items():
        if key.startswith(("DINGTALK_", "LOOP_DINGTALK_")):
            result.setdefault(key, value)
    return result


def dm_users(env: Dict[str, str]) -> List[str]:
    """staffIds to send a card to one-to-one.

    A group conversation and a private chat need different endpoints, and their
    ids look alike, so which one to use cannot be inferred — it is configured.
    Set this for a bot used in a private chat; leave it empty for a group.
    """
    raw = (env.get("LOOP_DINGTALK_DM_USERS") or "").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def conversations(env: Dict[str, str]) -> List[str]:
    """The allow-listed group conversations.

    Empty means *no* conversations, never "all of them": a bot that answers any
    group it happens to be added to is a bot anyone can put work into.
    """
    raw = (env.get("LOOP_DINGTALK_CONVERSATIONS") or "").strip()
    return [part.strip() for part in raw.split(",") if part.strip()]


def sign_webhook(url: str, secret: str, timestamp_ms: Optional[int] = None) -> str:
    if not secret:
        return url
    ts = timestamp_ms if timestamp_ms is not None else int(time.time() * 1000)
    payload = "{}\n{}".format(ts, secret)
    digest = hmac.new(secret.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).digest()
    sign = urllib.parse.quote_plus(base64.b64encode(digest).decode("utf-8"))
    joiner = "&" if "?" in url else "?"
    return "{}{}timestamp={}&sign={}".format(url, joiner, ts, sign)


def _http(url: str, payload: Optional[Dict[str, Any]], headers: Dict[str, str], method: str = "POST") -> Any:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(url, data=body, method=method)
    request.add_header("Content-Type", "application/json")
    for key, value in headers.items():
        request.add_header(key, value)
    with urllib.request.urlopen(request, timeout=20) as response:
        raw = response.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else {}


class DingTalk:
    def __init__(self, env: Dict[str, str], http: Optional[Callable] = None):
        self.env = env or {}
        self._http = http or _http
        self._token: Optional[str] = None
        self._token_expires = 0.0

    # -- capability ---------------------------------------------------------
    @property
    def configured(self) -> bool:
        """Can this send *and* be answered in DingTalk?"""
        return bool(self.env.get("DINGTALK_CLIENT_ID") and self.env.get("DINGTALK_CLIENT_SECRET"))

    @property
    def can_send(self) -> bool:
        return self.configured or bool(self.env.get("LOOP_DINGTALK_WEBHOOK"))

    @property
    def robot_code(self) -> str:
        return self.env.get("DINGTALK_ROBOT_CODE") or self.env.get("DINGTALK_CLIENT_ID") or ""

    # -- transport ----------------------------------------------------------
    def access_token(self, now: Optional[float] = None) -> str:
        now = now if now is not None else time.time()
        if self._token and now < self._token_expires:
            return self._token
        data = self._http(
            TOKEN_URL,
            {"appKey": self.env.get("DINGTALK_CLIENT_ID"), "appSecret": self.env.get("DINGTALK_CLIENT_SECRET")},
            {},
        )
        self._token = data.get("accessToken") or ""
        # Refresh a little early rather than discovering expiry mid-send.
        self._token_expires = now + max(int(data.get("expireIn") or 7200) - 300, 60)
        return self._token

    def _send(self, url: str, payload: Dict[str, Any]) -> Optional[str]:
        headers = {"x-acs-dingtalk-access-token": self.access_token()}
        data = self._http(url, payload, headers)
        return (data or {}).get("processQueryKey")

    def send_group(self, conversation_id: str, title: str, text: str) -> Optional[str]:
        return self._send(
            GROUP_URL,
            {
                "openConversationId": conversation_id,
                "robotCode": self.robot_code,
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
            },
        )

    def send_dm(self, staff_id: str, title: str, text: str) -> Optional[str]:
        return self._send(
            DM_URL,
            {
                "userIds": [staff_id],
                "robotCode": self.robot_code,
                "msgKey": "sampleMarkdown",
                "msgParam": json.dumps({"title": title, "text": text}, ensure_ascii=False),
            },
        )

    def send_webhook(self, title: str, text: str) -> None:
        """Send-only fallback. Returns no routing key, so nobody can quote-reply it."""
        url = self.env.get("LOOP_DINGTALK_WEBHOOK")
        if not url:
            return
        signed = sign_webhook(url, self.env.get("LOOP_DINGTALK_WEBHOOK_SECRET") or "")
        self._http(signed, {"msgtype": "markdown", "markdown": {"title": title, "text": text}}, {})

    def send(self, title: str, text: str, conversation_id: Optional[str] = None) -> Optional[str]:
        """Best available path.

        Returns the routing key when there is one, an empty string when the
        message was delivered without one (the send-only webhook), and **None**
        when nothing was sent — credentials present but no conversation
        configured is exactly that case, and reporting it as delivered would let
        half of a dual write vanish unnoticed.
        """
        if self.configured:
            # A private chat first, when one is configured: a card sent to a group
            # endpoint with a one-to-one conversation id is silently accepted and
            # never delivered.
            users = dm_users(self.env)
            if users and not conversation_id:
                return self.send_dm(users[0], title, text) or ""
            target = conversation_id or (conversations(self.env)[:1] or [None])[0]
            if target:
                return self.send_group(target, title, text) or ""
        if self.env.get("LOOP_DINGTALK_WEBHOOK"):
            self.send_webhook(title, text)
            return ""
        return None


# --------------------------------------------------------------------------- #
# what the group actually sees
# --------------------------------------------------------------------------- #


def question_card(repo: str, number: int, url: str, question: str, options: List[str]) -> str:
    lines = ["**{}#{}**".format(repo, number), "", question.strip(), ""]
    if options:
        for index, option in enumerate(options, 1):
            lines.append("{}. {}".format(index, option))
        lines.append("")
        lines.append("_引用回复本条消息，回编号（可一次回多个，如 `2`、`1 3`）或直接写下你的答复。_")
    else:
        lines.append("_引用回复本条消息写下你的答复。_")
    lines.append("")
    lines.append("[#{} 在这里]({})".format(number, url))
    return "\n".join(lines)


def report_card(title: str, summary: str) -> str:
    return "**{}**\n\n{}".format(title, summary.strip())
