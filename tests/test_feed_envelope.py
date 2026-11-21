"""SimpleFIN AccountSet parsing, v1 and v2 (#479): pure functions, no DB."""

from typing import Any

import pytest

from purser.feed import envelope

V2 = {
    "errlist": [
        {"code": "con.auth", "msg": "Log in again", "conn_id": "CON-1"},
        {"code": "act.failed", "msg": "Try later", "account_id": "ACT-2"},
        {"code": "gen.api", "msg": "Requested date range exceeds recommended range"},
    ],
    "connections": [
        {
            "conn_id": "CON-1",
            "name": "Demo Bank - Pat",
            "org_id": "ORG-1",
            "org_name": "Demo Bank",
            "org_url": "https://bank.example.com",
            "sfin_url": "https://sfin.example.com",
        },
        {"conn_id": "CON-2", "name": "Other Bank", "org_id": "ORG-2", "sfin_url": "x"},
    ],
    "accounts": [
        {"id": "ACT-1", "name": "Checking", "conn_id": "CON-1", "currency": "USD"},
        {"id": "ACT-2", "name": "Card", "conn_id": "CON-2", "currency": "USD"},
    ],
}
V1 = {
    "errors": ["Connection to Demo Bank may need attention. Auth required"],
    "accounts": [
        {
            "id": "ACT-1",
            "name": "Checking",
            "org": {"id": "demo.bank", "name": "Demo Bank", "domain": "bank.example.com"},
        },
        {
            "id": "ACT-3",
            "name": "Savings",
            "org": {"id": "demo.bank", "name": "Demo Bank", "domain": "bank.example.com"},
        },
    ],
}


def test_version_detection() -> None:
    assert envelope.version(V2) == 2
    assert envelope.version(V1) == 1
    assert envelope.version({"errlist": [], "accounts": []}) == 2
    assert envelope.version({"accounts": []}) == 1


def test_v2_issues_keep_code_and_scope() -> None:
    issues = envelope.issues(V2)
    assert [i.code for i in issues] == ["con.auth", "act.failed", "gen.api"]
    assert issues[0].conn_id == "CON-1" and issues[0].account_id is None
    assert issues[1].account_id == "ACT-2"
    assert envelope.messages(V2)[0] == "Log in again"


def test_v1_strings_become_naked_gen_issues() -> None:
    (issue,) = envelope.issues(V1)
    assert issue.code == "gen." and issue.prefix == "gen"
    assert issue.msg.startswith("Connection to Demo Bank")


@pytest.mark.parametrize(
    ("codes", "attention"),
    [
        ([], False),
        (["gen.api"], False),
        (["gen."], False),
        (["gen.auth"], True),
        (["con.auth"], True),
        (["con.somethingnew"], True),  # unknown subcode: treated like its prefix
        (["act.missingdata"], True),
        (["zzz.unknown"], False),  # unknown prefix: a naked gen.
    ],
)
def test_attention_by_code(codes: list[str], attention: bool) -> None:
    payload = {"errlist": [{"code": c, "msg": "m"} for c in codes], "accounts": []}
    assert envelope.needs_attention(payload) is attention


def test_v1_any_error_flags_as_before() -> None:
    assert envelope.needs_attention(V1)
    assert not envelope.needs_attention({"errors": [], "accounts": []})


def test_malformed_errlist_entries_degrade_to_gen() -> None:
    payload: dict[str, Any] = {"errlist": ["plain text", {"msg": "no code"}], "accounts": []}
    issues = envelope.issues(payload)
    assert [(i.code, i.msg) for i in issues] == [("gen.", "plain text"), ("gen.", "no code")]


def test_v2_institutions_and_lookup() -> None:
    first, second = envelope.institutions(V2)
    assert (first.conn_id, first.name, first.org_name) == ("CON-1", "Demo Bank - Pat", "Demo Bank")
    assert second.org_name == "Other Bank"  # org_name falls back to the name
    account = V2["accounts"][1]
    assert envelope.institution_of(V2, account) == second
    orphan = {"id": "X", "conn_id": "CON-9"}
    found = envelope.institution_of(V2, orphan)
    assert found is not None and found.conn_id == "CON-9" and found.name is None


def test_v1_institutions_derived_from_org_once() -> None:
    (only,) = envelope.institutions(V1)
    assert only.conn_id is None
    assert (only.name, only.org_id, only.org_url) == ("Demo Bank", "demo.bank", "bank.example.com")
    assert envelope.institution_of(V1, V1["accounts"][1]) == only


def test_duplicate_account_ids() -> None:
    payload = {
        "accounts": [
            {"id": "A", "conn_id": "CON-1"},
            {"id": "A", "conn_id": "CON-2"},
            {"id": "B", "conn_id": "CON-1"},
        ]
    }
    assert envelope.duplicate_account_ids(payload) == {"A"}
    assert envelope.duplicate_account_ids(V2) == set()


def test_api_messages() -> None:
    assert envelope.api_messages({"x-api-message": ["note"]}) == ["note"]
    assert envelope.api_messages({"x-api-message": "one"}) == ["one"]
    assert envelope.api_messages({}) == []


def test_login_of_issue_exact_then_unprefixed() -> None:
    first, second = envelope.institutions(V2)
    exact = envelope.Issue(code="con.auth", msg="m", conn_id="CON-2")
    assert envelope.login_of_issue(V2, exact) == second
    # The error list names the login without the prefix its entry carries.
    prefixed = dict(
        V2, connections=[dict(c, conn_id=f"MX-{c['conn_id']}") for c in V2["connections"]]
    )
    found = envelope.login_of_issue(
        prefixed, envelope.Issue(code="con.auth", msg="m", conn_id="CON-1")
    )
    assert found is not None and (found.conn_id, found.name) == ("MX-CON-1", first.name)


def test_login_of_issue_refuses_a_guess() -> None:
    unknown = envelope.Issue(code="con.auth", msg="m", conn_id="CON-9")
    assert envelope.login_of_issue(V2, unknown) is None
    assert envelope.login_of_issue(V2, envelope.Issue(code="gen.auth", msg="m")) is None
    # "1" is the tail of one id only at a separator; two logins ending the
    # same way are not told apart.
    twins = dict(V2, connections=[{"conn_id": "A-CON-7"}, {"conn_id": "B-CON-7"}])
    assert (
        envelope.login_of_issue(twins, envelope.Issue(code="con.auth", msg="m", conn_id="CON-7"))
        is None
    )
    assert (
        envelope.login_of_issue(V2, envelope.Issue(code="con.auth", msg="m", conn_id="ON-1"))
        is None
    )
