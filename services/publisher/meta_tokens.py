"""Turn a short-lived Meta token into Page tokens that last, and save them to .env.

    make meta-tokens                                            list the Pages your token can publish to
    make meta-tokens ARGS="--page <id or name> --as test --write"   save one Page's values to .env

The flow (Meta documents it; check the current docs if a step fails):
  1. you generate a short-lived user token in the Graph API Explorer (valid for an hour or two) and paste it when asked;
  2. it is exchanged, with the app id and secret, for a long-lived user token (about 60 days);
  3. that token lists your Pages. A Page token obtained this way has no expiry date (it stops working if you revoke the
     app, change your password, or lose your role on the Page);
  4. with --write, one Page's id and token go to the FB_* and IG_* variables that accounts.yaml reads.

Nothing secret is printed. The user token is used and thrown away: only Page tokens are saved, and only to .env.
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, TextIO

DEFAULT_URL = "https://graph.facebook.com"
DEFAULT_VERSION = "v25.0"
LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1"}
REQUIRED_SCOPES = {
    "pages_show_list",
    "pages_manage_posts",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_content_publish",
}
KINDS = {"test": "TEST", "mlbb": "MLBB"}
SAFE_VALUE = re.compile(r"[A-Za-z0-9_\-|.]+")
PAGE_LIMIT = 100
TIMEOUT_S = 30


class MetaError(RuntimeError):
    """Meta refused or could not be reached. The message never contains a token or the app secret."""


class Graph:
    def __init__(self, base: str, version: str = DEFAULT_VERSION) -> None:
        parts = urllib.parse.urlparse(base)
        local = parts.scheme == "http" and parts.hostname in LOCAL_HOSTS
        if not (parts.scheme == "https" or local) or not parts.hostname:
            raise ValueError(f"Graph API url {base!r} must be https (http only for localhost)")
        self._base, self._version = base.rstrip("/"), version

    def get(self, path: str, params: Mapping[str, str]) -> dict[str, Any]:
        url = f"{self._base}/{self._version}/{path}?{urllib.parse.urlencode(params)}"
        secrets = [v for v in params.values() if len(v) >= 6]
        try:
            with urllib.request.urlopen(url, timeout=TIMEOUT_S) as response:  # noqa: S310  # nosec B310
                body: dict[str, Any] = _json(response.read())
                return body
        except urllib.error.HTTPError as exc:
            error = _json(exc.read()).get("error", {})
            raise MetaError(
                _scrub(f"Meta error {error.get('code', exc.code)}: {error.get('message', 'no details')}", secrets)
            ) from None
        except urllib.error.URLError, TimeoutError, OSError:
            raise MetaError("could not reach the Graph API (network error)") from None


def _json(raw: bytes) -> dict[str, Any]:
    import json

    try:
        value = json.loads(raw or b"{}")
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def _scrub(text: str, secrets: list[str]) -> str:
    for secret in secrets:
        text = text.replace(secret, "***")
    return text[:300]


@dataclass(frozen=True)
class Token:
    token: str
    expires_in_s: int | None


@dataclass(frozen=True)
class TokenInfo:
    valid: bool
    expires_at: int  # unix time; 0 means it does not expire
    scopes: frozenset[str] | set[str]
    granular: dict[str, list[str]] = field(default_factory=dict)  # scope -> ids chosen in the login dialog

    @property
    def never_expires(self) -> bool:
        return self.expires_at == 0


@dataclass(frozen=True)
class Page:
    id: str
    name: str
    token: str
    instagram_id: str | None
    instagram_username: str | None
    tasks: tuple[str, ...] | None = None  # what the profile may do on the Page; None when Meta did not say


def exchange(graph: Graph, app_id: str, app_secret: str, short_token: str) -> Token:
    reply = graph.get(
        "oauth/access_token",
        {
            "grant_type": "fb_exchange_token",
            "client_id": app_id,
            "client_secret": app_secret,
            "fb_exchange_token": short_token,
        },
    )
    if "access_token" not in reply:
        raise MetaError("Meta's reply held no access token")
    expires = reply.get("expires_in")
    return Token(reply["access_token"], int(expires) if expires is not None else None)


def inspect(graph: Graph, app_id: str, app_secret: str, token: str) -> TokenInfo:
    data = graph.get("debug_token", {"input_token": token, "access_token": f"{app_id}|{app_secret}"}).get("data", {})
    granular = {g["scope"]: [str(i) for i in g.get("target_ids", [])] for g in data.get("granular_scopes", [])}
    return TokenInfo(
        valid=bool(data.get("is_valid")),
        expires_at=int(data.get("expires_at", 0)),
        scopes=set(data.get("scopes", [])),
        granular=granular,
    )


def list_pages(graph: Graph, user_token: str) -> list[Page]:
    pages: list[Page] = []
    after: str | None = None
    fields = "id,name,access_token,tasks,instagram_business_account{id,username}"
    while True:
        params = {"fields": fields, "limit": str(PAGE_LIMIT), "access_token": user_token}
        if after:
            params["after"] = after
        try:
            reply = graph.get("me/accounts", params)
        except MetaError as exc:
            if "tasks" not in fields or " 100:" not in str(exc):
                raise
            fields = fields.replace("tasks,", "")  # this Graph version does not offer tasks here
            continue
        pages.extend(_page_from(item) for item in reply.get("data", []))
        paging = reply.get("paging", {})
        after = paging.get("cursors", {}).get("after") if paging.get("next") else None
        if not after:
            return pages


PAGE_SCOPES = ("pages_show_list", "pages_manage_posts", "pages_read_engagement")
PAGE_FIELDS = "id,name,access_token,instagram_business_account{id,username}"


def _page_from(item: Mapping[str, Any]) -> Page:
    ig = item.get("instagram_business_account") or {}
    tasks = item.get("tasks")
    return Page(
        item["id"],
        item.get("name", ""),
        item["access_token"],
        ig.get("id"),
        ig.get("username"),
        tuple(tasks) if tasks is not None else None,
    )


def pages_from_grants(
    graph: Graph, user_token: str, info: TokenInfo, extra_ids: tuple[str, ...] = ()
) -> tuple[list[Page], dict[str, str]]:
    """Look the Pages up one by one by the ids the login dialog granted, plus any the operator names. This finds Pages
    that /me/accounts leaves out. Returns the Pages found and, for those that could not be read, id -> Meta's reason."""
    granted = (i for scope in PAGE_SCOPES for i in info.granular.get(scope, []))
    ids = list(dict.fromkeys([*granted, *extra_ids]))
    pages: list[Page] = []
    unreadable: dict[str, str] = {}
    for page_id in ids:
        try:
            pages.append(_page_from(graph.get(page_id, {"fields": PAGE_FIELDS, "access_token": user_token})))
        except (MetaError, KeyError) as exc:
            unreadable[page_id] = str(exc) if isinstance(exc, MetaError) else "Meta returned no Page token"
    return pages, unreadable


def explain_no_pages(
    graph: Graph, user_token: str, info: TokenInfo, unreadable: Mapping[str, str], out: TextIO
) -> None:
    try:
        me = graph.get("me", {"fields": "id,name", "access_token": user_token})
        who = f"{me.get('name', '?')} (id {me.get('id', '?')})"
    except MetaError:
        who = "unknown (Meta would not say)"
    granted_ids = sorted({i for scope in PAGE_SCOPES for i in info.granular.get(scope, [])})
    print("This token can reach no Pages. What Meta says about it:", file=out)
    print(f"  the token belongs to:        {who}", file=out)
    print(f"  permissions granted:         {', '.join(sorted(info.scopes)) or 'none'}", file=out)
    print(f"  Pages ticked in the dialog:  {', '.join(granted_ids) or 'none'}", file=out)
    for page_id, reason in unreadable.items():
        print(f"  could not read Page {page_id}: {reason}", file=out)
    print("Likely causes, most common first:", file=out)
    if not granted_ids:
        print(
            "  1. No Page was selected in the login dialog. Generate a new token; when the dialog lists your Pages,",
            file=out,
        )
        print("     tick the Page (and the Instagram account) before continuing.", file=out)
        print(
            "     If the dialog does not list your Pages again, Facebook kept your old choice: remove the app under",
            file=out,
        )
        print("     Facebook Settings > Business integrations, then generate the token again.", file=out)
    else:
        print(
            "  1. The Page was selected but Meta will not give its token (see above). Whoever generated the token",
            file=out,
        )
        print("     needs full control of the Page, not only partial task access.", file=out)
    print(
        "  2. The Page is managed through a business portfolio: add the business_management permission to the token",
        file=out,
    )
    print(
        "     and make sure that person has full (Facebook) access to the Page in Business settings > Pages.", file=out
    )
    print("  3. The token belongs to a different Facebook profile than the one that administers the Page.", file=out)


def env_updates(page: Page, kind: str) -> dict[str, str]:
    """The .env variables accounts.yaml reads for this kind of account. The Instagram token is the Page token."""
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {sorted(KINDS)}, not {kind!r}")
    tag = KINDS[kind]
    updates = {f"FB_{tag}_PAGE_ID": page.id, f"FB_{tag}_PAGE_TOKEN": page.token}
    if page.instagram_id:
        updates |= {f"IG_{tag}_TOKEN": page.token, f"IG_{tag}_USER_ID": page.instagram_id}
    return updates


_ENV_LINE = re.compile(
    r"^(?P<indent>[ \t]*)(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?P<eq>[ \t]*=)(?P<gap>[ \t]*)(?P<rest>.*?)(?P<eol>\r?\n?)$"
)


def _fill(match: re.Match[str], value: str) -> str:
    """The line with its value replaced and its trailing comment (and the spacing before it) kept."""
    rest = match.group("rest")
    if rest.startswith("#"):  # no value yet, only a comment
        tail = match.group("gap") + rest
    else:
        comment = re.match(r"^(.*?)(\s+#.*)?$", rest)
        tail = (comment.group(2) if comment else None) or ""
    return f"{match.group('indent')}{match.group('name')}{match.group('eq')}{value}{tail}{match.group('eol')}"


def _write_atomic(path: Path, text: str, mode: int) -> None:
    """Write a file so that it is never half-written and never readable by others while it is being written. An existing
    file at `path`, whatever its owner or permissions, is replaced as long as the folder is writable."""
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name + ".")  # created owner-only
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def update_env_file(path: Path, updates: Mapping[str, str]) -> Path:
    """Set variables in a .env file in place, keeping every other line, comment and the file's permissions. The old file
    is kept as <name>.bak with the same permissions. Returns the backup path."""
    for name, value in updates.items():
        if not SAFE_VALUE.fullmatch(value):
            raise ValueError(f"{name}: the value has characters that are not safe in a .env file")
    path = Path(path)
    original = path.read_text(encoding="utf-8")
    mode = path.stat().st_mode & 0o777
    backup = path.with_name(path.name + ".bak")
    pending = dict(updates)
    lines = original.splitlines(keepends=True)
    for index, line in enumerate(lines):
        match = _ENV_LINE.match(line)
        if match and match.group("name") in pending:
            lines[index] = _fill(match, pending.pop(match.group("name")))
    if pending and lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    lines += [f"{name}={value}\n" for name, value in pending.items()]

    _write_atomic(backup, original, mode)  # first: if this fails, .env has not been touched
    _write_atomic(path, "".join(lines), mode)
    return backup


PUBLISH_TASKS = {"CREATE_CONTENT", "MANAGE"}


def _tasks_note(page: Page) -> str:
    if page.tasks is None:
        return "tasks unknown"
    note = f"tasks {', '.join(page.tasks) or 'none'}"
    if not PUBLISH_TASKS & set(page.tasks):
        note += "  <-- this profile cannot publish to this Page (needs CREATE_CONTENT or MANAGE)"
    return note


def _expiry(info: TokenInfo) -> str:
    if info.never_expires:
        return "never expires"
    return "expires " + datetime.fromtimestamp(info.expires_at, UTC).strftime("%Y-%m-%d")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m services.publisher.meta_tokens", exit_on_error=False)
    parser.add_argument("--page", help="a Page id or exact name, from the list")
    parser.add_argument("--as", dest="kind", choices=sorted(KINDS), help="which accounts these are for")
    parser.add_argument("--write", action="store_true", help="save the chosen Page's values to the env file")
    parser.add_argument(
        "--page-id", help="a Page's numeric id: look it up directly (use when the account list is empty)"
    )
    parser.add_argument(
        "--no-exchange",
        action="store_true",
        help="the token is already long-lived (for example a Business system user token): skip the exchange",
    )
    parser.add_argument("--env-file", default=".env")
    return parser


def main(
    argv: list[str],
    environ: Mapping[str, str] = os.environ,
    out: TextIO = sys.stdout,
    *,
    read_secret: Callable[[str], str] = getpass.getpass,
) -> int:
    try:
        args = _parser().parse_args(argv)
    except (argparse.ArgumentError, SystemExit) as exc:
        print(f"usage error: {exc}", file=out)
        return 2
    app_id, app_secret = environ.get("META_APP_ID", ""), environ.get("META_APP_SECRET", "")
    missing = [n for n, v in (("META_APP_ID", app_id), ("META_APP_SECRET", app_secret)) if not v]
    if missing:
        print(f"set {' and '.join(missing)} in .env first (Meta developer app > App settings > Basic)", file=out)
        return 2
    if args.write and not (args.page and args.kind):
        print("--write needs --page <id or name> and --as test|mlbb", file=out)
        return 2
    if args.page_id is not None and not re.fullmatch(r"\d{3,25}", args.page_id):
        print("--page-id must be the Page's numeric id (digits only)", file=out)
        return 2
    if args.no_exchange and not args.page_id:
        print(
            "--no-exchange needs --page-id: a token used as given has no account list to read the Pages from", file=out
        )
        return 2
    prompt = (
        "Token to use as given (input is hidden): "
        if args.no_exchange
        else "Short-lived user token from the Graph API Explorer (input is hidden): "
    )
    short = read_secret(prompt).strip()
    if not short:
        print("no token given", file=out)
        return 2
    graph = Graph(environ.get("META_GRAPH_URL", DEFAULT_URL), environ.get("META_GRAPH_VERSION", DEFAULT_VERSION))
    try:
        return _run(graph, args, app_id, app_secret, short, out)
    except MetaError as exc:
        print(f"FAILED: {exc}", file=out)
        if " 190:" in str(exc):
            print(
                "The short-lived token has probably expired (they last an hour or two): generate a new one and retry.",
                file=out,
            )
        return 1


def _run(graph: Graph, args: argparse.Namespace, app_id: str, app_secret: str, short: str, out: TextIO) -> int:
    if args.no_exchange:
        long_lived = Token(short, None)
        user_info = inspect(graph, app_id, app_secret, short)
        print(f"Using the token as given, no exchange ({_expiry(user_info)}).", file=out)
    else:
        long_lived = exchange(graph, app_id, app_secret, short)
        user_info = inspect(graph, app_id, app_secret, long_lived.token)
        print(f"Exchanged for a long-lived user token ({_expiry(user_info)}).", file=out)
    absent = sorted(REQUIRED_SCOPES - set(user_info.scopes))
    if absent:
        print(
            f"Missing permissions: {', '.join(absent)}. Add them in the Explorer, generate a new token and retry.",
            file=out,
        )
    pages = [] if args.no_exchange else list_pages(graph, long_lived.token)
    unreadable: dict[str, str] = {}
    if not pages:  # /me/accounts leaves out some Pages (for example ones managed through a business portfolio)
        extra = (args.page_id,) if args.page_id else ()
        pages, unreadable = pages_from_grants(graph, long_lived.token, user_info, extra)
    if not pages:
        explain_no_pages(graph, long_lived.token, user_info, unreadable, out)
        return 1
    print("Pages this token can publish to:", file=out)
    for page in pages:
        lifetime = _expiry(inspect(graph, app_id, app_secret, page.token))
        instagram = (
            f"Instagram @{page.instagram_username} ({page.instagram_id})"
            if page.instagram_id
            else "no Instagram account linked"
        )
        print(f"  {page.name}   id {page.id}   {instagram}   token {lifetime}   {_tasks_note(page)}", file=out)
    if not args.page:
        print('To save one: make meta-tokens ARGS="--page <id or name> --as test --write"', file=out)
        return 0
    chosen = next((p for p in pages if args.page in (p.id, p.name) or args.page.lower() == p.name.lower()), None)
    if chosen is None:
        print(f"no page matches {args.page!r}: use an id or name from the list above", file=out)
        return 1
    updates = env_updates(chosen, args.kind or "test")
    if chosen.instagram_id is None:
        print("This Page has no linked Instagram account, so only the Facebook values are set.", file=out)
    if not args.write:
        print(f"Would set {', '.join(sorted(updates))}. Add --write to save.", file=out)
        return 0
    try:
        backup = update_env_file(Path(args.env_file), updates)
    except OSError as exc:
        print(f"FAILED: could not write {args.env_file}: {exc.strerror or exc}. Nothing was changed.", file=out)
        return 1
    print(f"Wrote {', '.join(sorted(updates))} to {args.env_file} (the old file is kept as {backup.name}).", file=out)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
