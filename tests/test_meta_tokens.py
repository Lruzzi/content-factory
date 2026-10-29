"""Getting long-lived Meta tokens: exchange the short-lived token, list the Pages, save the Page token (FR-9)."""

from __future__ import annotations

import io
import os
import stat
from pathlib import Path
from typing import Any

import pytest
from fake_graph_server import fake_graph

from services.publisher import meta_tokens as mt

V = mt.DEFAULT_VERSION
APP_ID, APP_SECRET = "1234567890", "appsecret0123456789abcdef"  # gitleaks:allow - fake, for a fake server
SHORT, LONG, PAGE_TOKEN = "EAAshort0123456789", "EAAlong0123456789abc", "EAApage0123456789xyz"  # gitleaks:allow
SCOPES = [
    "pages_show_list",
    "pages_manage_posts",
    "pages_read_engagement",
    "instagram_basic",
    "instagram_content_publish",
]


def exchange_reply(expires_in=5_183_944):
    return {"access_token": LONG, "token_type": "bearer", "expires_in": expires_in}


def debug_reply(expires_at=1_800_000_000, scopes=None, valid=True, granular=None):
    data = {
        "app_id": APP_ID,
        "type": "USER",
        "is_valid": valid,
        "expires_at": expires_at,
        "scopes": SCOPES if scopes is None else scopes,
    }
    if granular is not None:
        data["granular_scopes"] = [{"scope": scope, "target_ids": ids} for scope, ids in granular]
    return {"data": data}


def pages_reply(*pages, after=None):
    body: dict[str, Any] = {"data": list(pages)}
    if after:
        body["paging"] = {"cursors": {"after": after}, "next": "https://example.invalid/next"}
    return body


PAGE_A = {
    "id": "111",
    "name": "Wiwaw Moment Test",
    "access_token": PAGE_TOKEN,
    "instagram_business_account": {"id": "17841400000000001", "username": "wiwaw.test"},
}
PAGE_B = {"id": "222", "name": "Plain Page", "access_token": "EAAother0123456789xx"}  # gitleaks:allow


def client(base: str) -> mt.Graph:
    return mt.Graph(base, V)


# ---------- talking to Meta ----------------------------------------------------------------------------------
def test_the_short_lived_token_is_exchanged_for_a_long_lived_one_with_the_app_credentials():
    with fake_graph() as (graph, base):
        graph.on(f"/{V}/oauth/access_token", exchange_reply())
        token = mt.exchange(client(base), APP_ID, APP_SECRET, SHORT)
    call = graph.calls[0]
    assert call.query == {
        "grant_type": "fb_exchange_token",
        "client_id": APP_ID,
        "client_secret": APP_SECRET,
        "fb_exchange_token": SHORT,
    }
    assert (token.token, token.expires_in_s) == (LONG, 5_183_944)


def test_a_meta_error_becomes_a_readable_error_with_its_code_and_no_secrets():
    with fake_graph() as (graph, base):
        graph.on(
            f"/{V}/oauth/access_token",
            {"error": {"message": f"bad token {SHORT} for {APP_SECRET}", "type": "OAuthException", "code": 190}},
            400,
        )
        with pytest.raises(mt.MetaError) as caught:
            mt.exchange(client(base), APP_ID, APP_SECRET, SHORT)
    text = str(caught.value)
    assert "190" in text
    assert SHORT not in text
    assert APP_SECRET not in text


def test_an_unreachable_graph_api_is_a_meta_error():
    with fake_graph() as (_, base):
        pass
    with pytest.raises(mt.MetaError, match="reach"):
        mt.exchange(client(base), APP_ID, APP_SECRET, SHORT)


def test_token_details_are_read_with_the_app_token_and_expiry_zero_means_never():
    with fake_graph() as (graph, base):
        graph.on(f"/{V}/debug_token", debug_reply(expires_at=0))
        info = mt.inspect(client(base), APP_ID, APP_SECRET, PAGE_TOKEN)
    call = graph.calls[0]
    assert call.query["input_token"] == PAGE_TOKEN
    assert call.query["access_token"] == f"{APP_ID}|{APP_SECRET}"
    assert (info.valid, info.expires_at, info.never_expires) == (True, 0, True)
    assert info.scopes == set(SCOPES)


def test_pages_come_with_their_token_and_linked_instagram_account():
    with fake_graph() as (graph, base):
        graph.on(f"/{V}/me/accounts", pages_reply(PAGE_A, PAGE_B))
        pages = mt.list_pages(client(base), LONG)
    assert [(p.id, p.name, p.instagram_id, p.instagram_username) for p in pages] == [
        ("111", "Wiwaw Moment Test", "17841400000000001", "wiwaw.test"),
        ("222", "Plain Page", None, None),
    ]
    assert pages[0].token == PAGE_TOKEN
    assert graph.calls[0].query["access_token"] == LONG


def test_a_long_list_of_pages_is_followed_to_the_end():
    with fake_graph() as (graph, base):
        graph.on(f"/{V}/me/accounts", pages_reply(PAGE_A, after="cursor1"))
        graph.on(f"/{V}/me/accounts", pages_reply(PAGE_B))
        pages = mt.list_pages(client(base), LONG)
    assert [p.id for p in pages] == ["111", "222"]
    assert graph.calls[1].query["after"] == "cursor1"


def test_only_https_endpoints_are_used_except_this_machine():
    with pytest.raises(ValueError, match="https"):
        mt.Graph("http://graph.facebook.com", V)
    mt.Graph("https://graph.facebook.com", V)
    mt.Graph("http://127.0.0.1:9", V)


# ---------- what goes into .env ----------------------------------------------------------------------------------
def page(instagram=True) -> mt.Page:
    return mt.Page("111", "P", PAGE_TOKEN, "17841400000000001" if instagram else None, "ig" if instagram else None)


@pytest.mark.parametrize(
    ("kind", "names"),
    [
        ("test", ["FB_TEST_PAGE_ID", "FB_TEST_PAGE_TOKEN", "IG_TEST_TOKEN", "IG_TEST_USER_ID"]),
        ("mlbb", ["FB_MLBB_PAGE_ID", "FB_MLBB_PAGE_TOKEN", "IG_MLBB_TOKEN", "IG_MLBB_USER_ID"]),
    ],
)
def test_the_variables_match_the_names_accounts_yaml_reads(kind, names):
    assert sorted(mt.env_updates(page(), kind)) == names


def test_a_page_with_no_instagram_account_only_sets_the_facebook_variables():
    assert sorted(mt.env_updates(page(instagram=False), "test")) == ["FB_TEST_PAGE_ID", "FB_TEST_PAGE_TOKEN"]


def test_the_instagram_token_is_the_page_token_and_the_ids_are_the_pages():
    updates = mt.env_updates(page(), "test")
    assert updates["IG_TEST_TOKEN"] == PAGE_TOKEN
    assert updates["FB_TEST_PAGE_TOKEN"] == PAGE_TOKEN
    assert updates["FB_TEST_PAGE_ID"] == "111"
    assert updates["IG_TEST_USER_ID"] == "17841400000000001"


def test_an_unknown_kind_is_rejected():
    with pytest.raises(ValueError, match="kind"):
        mt.env_updates(page(), "prod")


def write_env(tmp_path: Path, text: str, mode: int = 0o600) -> Path:
    path = tmp_path / ".env"
    path.write_text(text)
    path.chmod(mode)
    return path


def test_existing_variables_are_filled_in_place_and_everything_else_is_untouched(tmp_path):
    path = write_env(tmp_path, "# my env\nGROQ_API_KEY=keep-me  # groq\nFB_TEST_PAGE_ID=\nIG_TEST_TOKEN=old\nOTHER=1\n")
    mt.update_env_file(path, {"FB_TEST_PAGE_ID": "111", "IG_TEST_TOKEN": "newtoken"})
    assert (
        path.read_text()
        == "# my env\nGROQ_API_KEY=keep-me  # groq\nFB_TEST_PAGE_ID=111\nIG_TEST_TOKEN=newtoken\nOTHER=1\n"
    )


def test_a_trailing_comment_on_an_updated_line_is_kept(tmp_path):
    path = write_env(tmp_path, "FB_TEST_PAGE_ID=   # the test page\n")
    mt.update_env_file(path, {"FB_TEST_PAGE_ID": "111"})
    assert path.read_text() == "FB_TEST_PAGE_ID=111   # the test page\n"


def test_a_missing_variable_is_appended(tmp_path):
    path = write_env(tmp_path, "A=1\n")
    mt.update_env_file(path, {"IG_TEST_USER_ID": "17841400000000001"})
    assert path.read_text() == "A=1\nIG_TEST_USER_ID=17841400000000001\n"


def test_a_backup_is_kept_and_the_file_permissions_are_preserved(tmp_path):
    path = write_env(tmp_path, "A=1\nFB_TEST_PAGE_ID=\n", mode=0o600)
    mt.update_env_file(path, {"FB_TEST_PAGE_ID": "111"})
    backup = tmp_path / ".env.bak"
    assert backup.read_text() == "A=1\nFB_TEST_PAGE_ID=\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600


@pytest.mark.parametrize("bad", ["has space", "new\nline", 'quo"te', "back\\slash", "dollar$x", "hash#x", ""])
def test_values_that_could_break_the_file_are_refused(tmp_path, bad):
    path = write_env(tmp_path, "FB_TEST_PAGE_ID=\n")
    with pytest.raises(ValueError, match="FB_TEST_PAGE_ID"):
        mt.update_env_file(path, {"FB_TEST_PAGE_ID": bad})
    assert path.read_text() == "FB_TEST_PAGE_ID=\n"


# ---------- the command, end to end -----------------------------------------------------------------------------
def run(base: str, tmp_path: Path, *args: str, env: dict[str, str] | None = None, short: str = SHORT):
    out = io.StringIO()
    environ = {"META_APP_ID": APP_ID, "META_APP_SECRET": APP_SECRET, "META_GRAPH_URL": base} | (env or {})
    code = mt.main(list(args), environ, out, read_secret=lambda prompt: short)
    return code, out.getvalue()


def script(graph, *, expires_at=1_800_000_000, scopes=None, pages=(PAGE_A, PAGE_B)):
    graph.on(f"/{V}/oauth/access_token", exchange_reply())
    graph.on(f"/{V}/debug_token", debug_reply(expires_at=expires_at, scopes=scopes))
    graph.on(f"/{V}/me/accounts", pages_reply(*pages))


def test_listing_shows_the_pages_and_changes_nothing(tmp_path):
    env_path = write_env(tmp_path, "FB_TEST_PAGE_ID=\n")
    with fake_graph() as (graph, base):
        script(graph, expires_at=0)
        code, text = run(base, tmp_path, "--env-file", str(env_path))
    assert code == 0
    assert "Wiwaw Moment Test" in text
    assert "111" in text
    assert "wiwaw.test" in text
    assert "Plain Page" in text
    assert "never expires" in text
    assert env_path.read_text() == "FB_TEST_PAGE_ID=\n"
    assert "--write" in text  # tells the operator how to save


def test_no_token_or_secret_is_ever_printed(tmp_path):
    env_path = write_env(tmp_path, "X=1\n")
    with fake_graph() as (graph, base):
        script(graph)
        _, text = run(base, tmp_path, "--env-file", str(env_path), "--page", "111", "--as", "test", "--write")
    for secret in (SHORT, LONG, PAGE_TOKEN, APP_SECRET):
        assert secret not in text


def test_writing_saves_the_chosen_pages_values_to_env_and_nothing_else_from_meta(tmp_path):
    env_path = write_env(tmp_path, "GROQ_API_KEY=keep\nFB_TEST_PAGE_TOKEN=\nIG_TEST_TOKEN=\n")
    with fake_graph() as (graph, base):
        script(graph)
        code, text = run(
            base, tmp_path, "--env-file", str(env_path), "--page", "Wiwaw Moment Test", "--as", "test", "--write"
        )
    saved = env_path.read_text()
    assert code == 0
    assert f"FB_TEST_PAGE_TOKEN={PAGE_TOKEN}" in saved
    assert f"IG_TEST_TOKEN={PAGE_TOKEN}" in saved
    assert "FB_TEST_PAGE_ID=111" in saved
    assert "IG_TEST_USER_ID=17841400000000001" in saved
    assert "GROQ_API_KEY=keep" in saved
    assert SHORT not in saved
    assert LONG not in saved  # the user token is not kept: Page tokens are what the publisher needs
    assert "wrote" in text.lower()


def test_a_page_can_be_chosen_by_id(tmp_path):
    env_path = write_env(tmp_path, "")
    with fake_graph() as (graph, base):
        script(graph)
        code, _ = run(base, tmp_path, "--env-file", str(env_path), "--page", "222", "--as", "mlbb", "--write")
    assert code == 0
    saved = env_path.read_text()
    assert "FB_MLBB_PAGE_ID=222" in saved
    assert "IG_MLBB" not in saved  # this page has no Instagram account


def test_an_unknown_page_is_an_error_and_nothing_is_written(tmp_path):
    env_path = write_env(tmp_path, "A=1\n")
    with fake_graph() as (graph, base):
        script(graph)
        code, text = run(base, tmp_path, "--env-file", str(env_path), "--page", "999", "--as", "test", "--write")
    assert code == 1
    assert "999" in text
    assert env_path.read_text() == "A=1\n"


def test_write_needs_both_a_page_and_a_kind(tmp_path):
    env_path = write_env(tmp_path, "A=1\n")
    with fake_graph() as (graph, base):
        script(graph)
        code, text = run(base, tmp_path, "--env-file", str(env_path), "--write")
    assert code == 2
    assert "--page" in text
    assert env_path.read_text() == "A=1\n"


def test_missing_scopes_are_reported_by_name(tmp_path):
    env_path = write_env(tmp_path, "")
    with fake_graph() as (graph, base):
        script(graph, scopes=["pages_show_list"])
        code, text = run(base, tmp_path, "--env-file", str(env_path))
    assert code == 0
    assert "instagram_content_publish" in text
    assert "missing" in text.lower()


def test_a_page_token_that_expires_is_flagged_with_its_date(tmp_path):
    env_path = write_env(tmp_path, "")
    with fake_graph() as (graph, base):
        script(graph, expires_at=1_800_000_000)
        _, text = run(base, tmp_path, "--env-file", str(env_path))
    assert "expires" in text
    assert "never expires" not in text


def test_the_app_credentials_are_required_and_named(tmp_path):
    with fake_graph() as (_, base):
        code, text = run(base, tmp_path, env={"META_APP_ID": "", "META_APP_SECRET": ""})
    assert code == 2
    assert "META_APP_ID" in text
    assert "META_APP_SECRET" in text


def test_an_expired_or_wrong_short_token_is_explained(tmp_path):
    with fake_graph() as (graph, base):
        graph.on(f"/{V}/oauth/access_token", {"error": {"message": "Session has expired", "code": 190}}, 400)
        code, text = run(base, tmp_path)
    assert code == 1
    assert "190" in text
    assert "short-lived" in text.lower()
    assert SHORT not in text


def test_an_empty_token_is_refused_before_any_request(tmp_path):
    with fake_graph() as (graph, base):
        code, text = run(base, tmp_path, short="   ")
    assert code == 2
    assert graph.calls == []


def test_the_default_environment_is_not_touched_by_the_tests():
    assert "META_APP_SECRET" not in os.environ or os.environ["META_APP_SECRET"] != APP_SECRET


# ---------- when /me/accounts comes back empty ----------------------------------------------------------------------
GRANTED_111 = [("pages_show_list", ["111"]), ("instagram_basic", ["17841400000000001"])]


def empty_accounts_script(graph, *, granular, person=True):
    graph.on(f"/{V}/oauth/access_token", exchange_reply())
    graph.on(f"/{V}/debug_token", debug_reply(granular=granular))
    graph.on(f"/{V}/me/accounts", pages_reply())
    if person:
        graph.on(f"/{V}/me", {"id": "42", "name": "Some Person"})


def test_pages_missing_from_the_accounts_list_are_found_through_the_ids_the_login_granted(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=GRANTED_111)
        graph.on(f"/{V}/111", PAGE_A)
        code, text = run(base, tmp_path)
    assert code == 0
    assert "Wiwaw Moment Test" in text
    assert "wiwaw.test" in text
    lookup = next(call for call in graph.calls if call.path == f"/{V}/111")
    assert lookup.query["access_token"] == LONG
    assert "access_token" in lookup.query["fields"]


def test_a_page_found_that_way_can_be_saved_like_any_other(tmp_path):
    env_path = write_env(tmp_path, "")
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=GRANTED_111)
        graph.on(f"/{V}/111", PAGE_A)
        code, _ = run(base, tmp_path, "--env-file", str(env_path), "--page", "111", "--as", "test", "--write")
    assert code == 0
    assert f"FB_TEST_PAGE_TOKEN={PAGE_TOKEN}" in env_path.read_text()


def test_no_pages_chosen_in_the_login_dialog_is_explained_with_who_and_what_was_granted(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", [])])
        code, text = run(base, tmp_path)
    assert code == 1
    assert "Some Person" in text
    assert "pages_show_list" in text
    assert "login dialog" in text
    assert "business_management" in text


def test_granted_pages_that_cannot_be_read_are_named_with_the_reason(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", ["999"])])
        graph.on(
            f"/{V}/999", {"error": {"message": "(#10) Not enough permission to access this page", "code": 10}}, 400
        )
        code, text = run(base, tmp_path)
    assert code == 1
    assert "999" in text
    assert "could not read" in text.lower()
    assert "Not enough permission" in text


def test_one_unreadable_page_does_not_hide_the_others(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", ["999", "111"])])
        graph.on(f"/{V}/999", {"error": {"message": "nope", "code": 10}}, 400)
        graph.on(f"/{V}/111", PAGE_A)
        code, text = run(base, tmp_path)
    assert code == 0
    assert "Wiwaw Moment Test" in text


def test_the_explanation_never_prints_a_token_or_secret(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", [])])
        _, text = run(base, tmp_path)
    for secret in (SHORT, LONG, APP_SECRET):
        assert secret not in text


def test_the_explanation_still_appears_if_meta_will_not_say_who_the_token_is(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", [])], person=False)
        code, text = run(base, tmp_path)
    assert code == 1
    assert "login dialog" in text


def test_the_explanation_says_how_to_make_facebook_show_the_page_dialog_again(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", [])])
        _, text = run(base, tmp_path)
    assert "Business integrations" in text
    assert "again" in text


# ---------- asking about a Page you know the id of ------------------------------------------------
def test_a_page_id_given_on_the_command_line_is_looked_up_directly_when_the_account_list_is_empty(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", [])])
        graph.on(f"/{V}/111", PAGE_A)
        code, text = run(base, tmp_path, "--page-id", "111")
    assert code == 0
    assert "Wiwaw Moment Test" in text


def test_meta_s_own_reason_is_shown_when_that_page_cannot_be_read(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", [])])
        graph.on(
            f"/{V}/61595829686432",
            {"error": {"message": "(#200) The user must be an admin of the page", "code": 200}},
            403,
        )
        code, text = run(base, tmp_path, "--page-id", "61595829686432")
    assert code == 1
    assert "61595829686432" in text
    assert "must be an admin" in text


def test_a_page_named_twice_is_only_looked_up_once(tmp_path):
    with fake_graph() as (graph, base):
        empty_accounts_script(graph, granular=[("pages_show_list", ["111"])])
        graph.on(f"/{V}/111", PAGE_A)
        run(base, tmp_path, "--page-id", "111")
    assert sum(call.path == f"/{V}/111" for call in graph.calls) == 1


@pytest.mark.parametrize("bad", ["me/accounts", "../me", "12 34", "abc", "1", ""])
def test_a_page_id_must_be_plain_digits(tmp_path, bad):
    with fake_graph() as (graph, base):
        code, text = run(base, tmp_path, "--page-id", bad)
    assert code == 2
    assert "--page-id" in text
    assert graph.calls == []


def test_a_token_that_is_already_long_lived_can_be_used_without_the_exchange(tmp_path):
    with fake_graph() as (graph, base):
        graph.on(f"/{V}/debug_token", debug_reply(expires_at=0))
        graph.on(f"/{V}/111", PAGE_A)
        code, text = run(base, tmp_path, "--no-exchange", "--page-id", "111")
    assert code == 0
    assert all(call.path != f"/{V}/oauth/access_token" for call in graph.calls)
    assert "as given" in text
    assert "Wiwaw Moment Test" in text
    assert SHORT not in text


def test_without_the_exchange_a_page_id_is_required_because_there_is_no_account_list(tmp_path):
    with fake_graph() as (graph, base):
        code, text = run(base, tmp_path, "--no-exchange")
    assert code == 2
    assert "--page-id" in text
    assert graph.calls == []


# ---------- what the profile may do on each Page -----
PAGE_WITH_TASKS = PAGE_A | {"tasks": ["ANALYZE", "CREATE_CONTENT", "MODERATE"]}
PAGE_VIEW_ONLY = PAGE_B | {"tasks": ["ANALYZE"]}


def test_each_pages_tasks_are_read_from_the_account_list_and_shown(tmp_path):
    with fake_graph() as (graph, base):
        script(graph, pages=(PAGE_WITH_TASKS, PAGE_VIEW_ONLY))
        code, text = run(base, tmp_path)
    assert code == 0
    assert "CREATE_CONTENT" in text
    assert "ANALYZE" in text


def test_the_account_list_asks_for_tasks(tmp_path):
    with fake_graph() as (graph, base):
        script(graph, pages=(PAGE_WITH_TASKS,))
        run(base, tmp_path)
    listing = next(c for c in graph.calls if c.path == f"/{V}/me/accounts")
    assert "tasks" in listing.query["fields"]


def test_a_page_the_profile_cannot_post_to_is_flagged(tmp_path):
    with fake_graph() as (graph, base):
        script(graph, pages=(PAGE_WITH_TASKS, PAGE_VIEW_ONLY))
        _, text = run(base, tmp_path)
    flagged = [line for line in text.splitlines() if "Plain Page" in line or "cannot publish" in line.lower()]
    assert any("cannot publish" in line.lower() for line in flagged)
    assert not any("cannot publish" in line.lower() for line in text.splitlines() if "Wiwaw Moment Test" in line)


def test_manage_alone_is_enough_to_publish(tmp_path):
    with fake_graph() as (graph, base):
        script(graph, pages=(PAGE_A | {"tasks": ["MANAGE"]},))
        _, text = run(base, tmp_path)
    assert "cannot publish" not in text.lower()


def test_a_page_with_no_task_information_is_not_blamed(tmp_path):
    with fake_graph() as (graph, base):
        script(graph, pages=(PAGE_A,))
        _, text = run(base, tmp_path)
    assert "cannot publish" not in text.lower()
    assert "tasks unknown" in text


def test_if_meta_rejects_the_tasks_field_the_list_is_asked_again_without_it(tmp_path):
    with fake_graph() as (graph, base):
        graph.on(f"/{V}/oauth/access_token", exchange_reply())
        graph.on(f"/{V}/debug_token", debug_reply())
        graph.on(
            f"/{V}/me/accounts",
            {"error": {"message": "(#100) Tried accessing nonexisting field (tasks)", "code": 100}},
            400,
        )
        graph.on(f"/{V}/me/accounts", pages_reply(PAGE_A))
        code, text = run(base, tmp_path)
    assert code == 0
    assert "Wiwaw Moment Test" in text
    accounts_calls = [c for c in graph.calls if c.path == f"/{V}/me/accounts"]
    assert "tasks" in accounts_calls[0].query["fields"]
    assert "tasks" not in accounts_calls[1].query["fields"]


# ---------- files that are in the way -------------------------------------------------------------
def test_an_old_read_only_backup_is_replaced_instead_of_stopping_the_write(tmp_path):
    path = write_env(tmp_path, "A=1\nFB_TEST_PAGE_ID=\n")
    stale = tmp_path / ".env.bak"
    stale.write_text("stale")
    stale.chmod(0o444)
    mt.update_env_file(path, {"FB_TEST_PAGE_ID": "111"})
    assert stale.read_text() == "A=1\nFB_TEST_PAGE_ID=\n"
    assert path.read_text() == "A=1\nFB_TEST_PAGE_ID=111\n"
    assert stat.S_IMODE(stale.stat().st_mode) == 0o600


def test_no_temporary_files_are_left_in_the_folder(tmp_path):
    path = write_env(tmp_path, "FB_TEST_PAGE_ID=\n")
    mt.update_env_file(path, {"FB_TEST_PAGE_ID": "111"})
    assert sorted(p.name for p in tmp_path.iterdir()) == [".env", ".env.bak"]


@pytest.mark.skipif(os.geteuid() == 0, reason="root ignores folder permissions")
def test_a_folder_that_cannot_be_written_ends_with_a_clear_message_and_leaves_env_alone(tmp_path):
    folder = tmp_path / "locked"
    folder.mkdir()
    env_path = write_env(folder, "FB_TEST_PAGE_ID=\n")
    folder.chmod(0o500)
    try:
        with fake_graph() as (graph, base):
            script(graph)
            code, text = run(base, tmp_path, "--env-file", str(env_path), "--page", "111", "--as", "test", "--write")
    finally:
        folder.chmod(0o700)
    assert code == 1
    assert "could not write" in text.lower()
    assert "Traceback" not in text
    assert env_path.read_text() == "FB_TEST_PAGE_ID=\n"
