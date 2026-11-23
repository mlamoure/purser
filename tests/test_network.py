"""PURSER_ALLOWED_HOSTS: the machine's other names (#633, decision 132).

Entries are exact names, because the MCP Host guard they feed exists to stop
DNS rebinding; a malformed list stops startup with a message that names the
entry. The guards themselves are tested in test_mcp_host_allowlist.py and
test_request_hardening.py.
"""

import pytest
from pydantic import ValidationError
from typer.testing import CliRunner

from purser import network
from purser.config import Settings


class TestParse:
    def test_a_home_list_is_normalized(self) -> None:
        assert network.parse_allowed_hosts(" 192.0.2.10, HomeServer ,homeserver.local,") == (
            "192.0.2.10",
            "homeserver",
            "homeserver.local",
        )

    def test_repeats_and_blank_entries_are_dropped(self) -> None:
        assert network.parse_allowed_hosts("a,,A, a ,") == ("a",)
        assert network.parse_allowed_hosts("") == ()
        assert network.parse_allowed_hosts(" , ") == ()

    def test_a_port_is_kept(self) -> None:
        assert network.parse_allowed_hosts("homeserver:8010") == ("homeserver:8010",)

    def test_ipv6_is_bracketed(self) -> None:
        assert network.parse_allowed_hosts("[FE80::0:1]:8000,2001:db8::5") == (
            "[fe80::1]:8000",
            "[2001:db8::5]",
        )

    @pytest.mark.parametrize(
        ("raw", "why"),
        [
            ("*.local", "wildcard"),
            ("homeserver*", "wildcard"),
            ("home?erver", "wildcard"),
            ("http://homeserver", "leave out http://"),
            ("homeserver/mcp", "leave out http://"),
            ("user@homeserver", "leave out http://"),
            ("homeserver:0", "port"),
            ("homeserver:99999", "port"),
            ("homeserver:http", "port"),
            ("192.0.2.300", "not a valid IPv4"),
            ("home server", "not a host name"),
            ("-homeserver", "not a host name"),
            ("homeserver..local", "not a host name"),
            ("[homeserver]", "not a host name"),
            ("[::g]", "not a valid IPv6"),
            ("fe80::zz", "not a valid IPv6"),
        ],
    )
    def test_a_malformed_entry_is_refused_by_name(self, raw: str, why: str) -> None:
        with pytest.raises(ValueError) as caught:
            network.parse_allowed_hosts(f"homeserver,{raw}")
        message = str(caught.value)
        assert message.startswith("PURSER_ALLOWED_HOSTS:")
        assert repr(raw.lower()) in message
        assert why in message
        assert network.EXAMPLE in message


class TestDerived:
    def test_origins_take_the_public_port_unless_pinned(self) -> None:
        origins = network.allowed_origins(
            "http://192.0.2.10:8000", ("homeserver", "192.0.2.11:8010", "[fe80::1]")
        )
        assert origins == {
            "http://homeserver:8000",
            "https://homeserver:8000",
            "http://192.0.2.11:8010",
            "https://192.0.2.11:8010",
            "http://[fe80::1]:8000",
            "https://[fe80::1]:8000",
        }

    def test_default_ports_are_left_out(self) -> None:
        assert network.allowed_origins("https://purser.example.com", ("alias.example.com",)) == {
            "http://alias.example.com:443",
            "https://alias.example.com",
        }
        assert network.normalize_origin("HTTP://Box:80/") == "http://box"
        assert network.normalize_origin("null") == "null"

    @pytest.mark.parametrize(
        ("host", "accepted"),
        [
            ("192.0.2.10:8000", True),
            ("homeserver", True),
            ("HOMESERVER.LOCAL:9999", True),  # the guard compares names, not ports
            ("localhost:8000", True),
            ("127.0.0.1", True),
            ("[::1]:8000", True),
            ("otherbox:8000", False),
            ("homeserver.local.example.net", False),
            ("", False),
        ],
    )
    def test_accepts_matches_the_guard(self, host: str, accepted: bool) -> None:
        names = ("homeserver", "homeserver.local")
        assert network.accepts(host, "http://192.0.2.10:8000", names) is accepted


class TestStartup:
    def test_settings_parse_the_environment(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PURSER_ALLOWED_HOSTS", "192.0.2.10,homeserver,homeserver.local")
        assert Settings().allowed_hosts == ("192.0.2.10", "homeserver", "homeserver.local")

    def test_unset_and_empty_mean_no_extra_names(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("PURSER_ALLOWED_HOSTS", raising=False)
        assert Settings().allowed_hosts == ()
        # Compose passes ${PURSER_ALLOWED_HOSTS:-}: an empty value.
        monkeypatch.setenv("PURSER_ALLOWED_HOSTS", "")
        assert Settings().allowed_hosts == ()

    def test_settings_refuse_a_wildcard(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("PURSER_ALLOWED_HOSTS", "*.local")
        with pytest.raises(ValidationError, match="wildcard"):
            Settings()

    def test_a_role_command_stops_with_one_clear_line(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No traceback: an `error:` line naming the variable and the entry,
        before the encryption key, the database or the port is touched."""
        from purser import cli
        from purser.config import get_settings

        monkeypatch.setenv("PURSER_ALLOWED_HOSTS", "homeserver,http://homeserver.local")
        get_settings.cache_clear()
        try:
            result = CliRunner().invoke(cli.app, ["serve"])
        finally:
            monkeypatch.delenv("PURSER_ALLOWED_HOSTS")
            get_settings.cache_clear()
        assert result.exit_code == 1
        assert result.exception is None or isinstance(result.exception, SystemExit)
        assert "error: PURSER_ALLOWED_HOSTS: 'http://homeserver.local'" in result.output
        assert "Traceback" not in result.output


class TestReachMode:
    """#632: how the install is reached, from what the deployment reports."""

    @pytest.mark.parametrize(
        ("public_url", "bind", "mode"),
        [
            ("http://localhost:8000", "127.0.0.1", "this_machine"),
            ("http://localhost:8000", "localhost", "this_machine"),
            ("http://localhost:8000", "::1", "this_machine"),
            ("http://192.0.2.10:8000", "0.0.0.0", "home_network"),
            ("http://192.0.2.10:8000", "192.0.2.10", "home_network"),
            ("http://192.0.2.10:8000", "[::]", "home_network"),
            # A proxy terminates TLS: Purser itself serves plain HTTP only.
            ("https://purser.example.com", "127.0.0.1", "reverse_proxy"),
            ("https://purser.example.com", None, "reverse_proxy"),
            # Nothing reported, or nothing readable: say so, never guess.
            ("http://localhost:8000", None, "unknown"),
            ("http://192.0.2.10:8000", "", "unknown"),
            ("http://192.0.2.10:8000", "eth0", "unknown"),
        ],
    )
    def test_modes(self, public_url: str, bind: str | None, mode: str) -> None:
        assert network.reach_mode(public_url, bind) == mode

    def test_a_local_public_url(self) -> None:
        assert network.public_url_is_local("http://localhost:8000")
        assert network.public_url_is_local("http://127.0.0.1:8010")
        assert not network.public_url_is_local("http://192.0.2.10:8000")

    def test_the_guide_link_follows_the_docs_setting(self) -> None:
        assert network.guide_url(None).startswith(network.DOCS_SITE + "/tutorials/ai-setup/#")
        assert network.guide_url("https://docs.example.com/purser/").startswith(
            "https://docs.example.com/purser/tutorials/ai-setup/#use-purser-from-another"
        )
