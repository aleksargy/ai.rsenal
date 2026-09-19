"""Session capture and .env loading.

Both are small, both are done by hand under mild frustration, and both fail in
ways that look like something else — a mis-parsed cookie presents as "the API
rejected my session", and a .env that silently overrides a CI secret presents as
"the agent used the wrong team".
"""

from __future__ import annotations

import json

import pytest

from arsenal.config import load_dotenv
from arsenal.fpl.auth import Session, session_from_pasted_cookies
from arsenal.fpl.oidc import OidcTokens


class TestPastedCookies:
    @pytest.mark.parametrize(
        "raw",
        [
            "pl_profile=abc; sessionid=def",
            "pl_profile=abc;sessionid=def",
            "  pl_profile=abc;  sessionid=def  ",
        ],
    )
    def test_parses_a_single_cookie_header_line(self, raw: str) -> None:
        session = session_from_pasted_cookies(raw)
        assert session.cookies == {"pl_profile": "abc", "sessionid": "def"}

    def test_parses_one_pair_per_line(self) -> None:
        session = session_from_pasted_cookies("pl_profile=abc\nsessionid=def")
        assert session.cookies == {"pl_profile": "abc", "sessionid": "def"}

    def test_parses_whitespace_separated_pairs(self) -> None:
        """DevTools' cookie table copies as tab-separated name and value."""
        session = session_from_pasted_cookies("pl_profile\tabc\nsessionid\tdef")
        assert session.cookies == {"pl_profile": "abc", "sessionid": "def"}

    def test_parses_json(self) -> None:
        session = session_from_pasted_cookies('{"pl_profile": "abc", "sessionid": "def"}')
        assert session.cookies == {"pl_profile": "abc", "sessionid": "def"}

    def test_strips_surrounding_quotes(self) -> None:
        session = session_from_pasted_cookies('pl_profile="abc"; sessionid="def"')
        assert session.cookies["pl_profile"] == "abc"

    def test_empty_input_is_not_an_error(self) -> None:
        assert session_from_pasted_cookies("   ").cookies == {}

    def test_preserves_base64_padding_in_values(self) -> None:
        """`pl_profile` is a base64 blob and frequently ends in '='.

        Splitting on every '=' rather than the first would truncate it, and the
        session would be rejected for no visible reason.
        """
        session = session_from_pasted_cookies("pl_profile=eyJhIjoxfQ==; sessionid=def")
        assert session.cookies["pl_profile"] == "eyJhIjoxfQ=="


class TestSession:
    def test_any_captured_cookie_counts_as_credentials(self) -> None:
        """No cookie-name gate: the API is the only authority on what works."""
        assert Session(cookies={"pl_profile": "abc"}).has_credentials

    def test_reads_a_playwright_storage_state(self) -> None:
        state = {
            "cookies": [
                {"name": "pl_profile", "value": "abc", "domain": ".premierleague.com"},
                {"name": "sessionid", "value": "def", "domain": "fantasy.premierleague.com"},
                {"name": "irrelevant", "value": "x", "domain": ".google.com"},
            ],
            "origins": [],
        }
        session = Session.from_payload(state)
        assert session.cookies == {"pl_profile": "abc", "sessionid": "def"}
        assert session.storage_state is state

    def test_round_trips_through_the_env_value(self) -> None:
        original = Session(cookies={"pl_profile": "abc", "sessionid": "def"})
        assert Session.from_payload(json.loads(original.to_env_value())).cookies == (
            original.cookies
        )

    def test_storage_state_is_preferred_when_serialising(self) -> None:
        """Playwright needs the full state; a bare cookie map cannot be replayed."""
        state = {
            "cookies": [{"name": "sessionid", "value": "d", "domain": ".premierleague.com"}],
            "origins": [{"origin": "https://fantasy.premierleague.com"}],
        }
        assert "origins" in Session.from_payload(state).to_env_value()

    def test_saves_and_loads(self, tmp_path) -> None:
        path = tmp_path / "session.json"
        Session(cookies={"pl_profile": "a", "sessionid": "b"}).save(path)
        assert Session.load(path).cookies == {"pl_profile": "a", "sessionid": "b"}

    def test_missing_file_loads_as_none(self, tmp_path) -> None:
        assert Session.load(tmp_path / "nope.json") is None

    def test_corrupt_file_loads_as_none_rather_than_raising(self, tmp_path) -> None:
        path = tmp_path / "session.json"
        path.write_text("not json at all", encoding="utf-8")
        assert Session.load(path) is None


class TestDotenv:
    def test_loads_simple_pairs(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("FPL_TEAM_ID", raising=False)
        env = tmp_path / ".env"
        env.write_text("FPL_TEAM_ID=12345\n", encoding="utf-8")
        assert load_dotenv(env) == 1

        import os

        assert os.environ["FPL_TEAM_ID"] == "12345"

    def test_real_environment_wins(self, tmp_path, monkeypatch) -> None:
        """GitHub Actions injects secrets as env vars.

        A stale committed .env silently overriding them would be a miserable
        thing to debug, so the environment always takes precedence.
        """
        monkeypatch.setenv("FPL_TEAM_ID", "from-environment")
        env = tmp_path / ".env"
        env.write_text("FPL_TEAM_ID=from-file\n", encoding="utf-8")
        load_dotenv(env)

        import os

        assert os.environ["FPL_TEAM_ID"] == "from-environment"

    def test_skips_comments_and_blank_lines(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("REAL_KEY", raising=False)
        env = tmp_path / ".env"
        env.write_text("# a comment\n\nREAL_KEY=value\n", encoding="utf-8")
        assert load_dotenv(env) == 1

    def test_handles_export_prefix_and_quotes(self, tmp_path, monkeypatch) -> None:
        monkeypatch.delenv("QUOTED", raising=False)
        env = tmp_path / ".env"
        env.write_text('export QUOTED="hello world"\n', encoding="utf-8")
        load_dotenv(env)

        import os

        assert os.environ["QUOTED"] == "hello world"

    def test_preserves_json_values_intact(self, tmp_path, monkeypatch) -> None:
        """FPL_SESSION_JSON is a JSON blob full of quotes, colons and equals signs."""
        monkeypatch.delenv("FPL_SESSION_JSON", raising=False)
        blob = '{"pl_profile":"eyJhIjoxfQ==","sessionid":"abc"}'
        env = tmp_path / ".env"
        env.write_text(f"FPL_SESSION_JSON={blob}\n", encoding="utf-8")
        load_dotenv(env)

        import os

        assert json.loads(os.environ["FPL_SESSION_JSON"])["pl_profile"] == "eyJhIjoxfQ=="

    def test_missing_file_is_not_an_error(self, tmp_path) -> None:
        assert load_dotenv(tmp_path / "absent") == 0


class TestCredentialShapes:
    """FPL moved to OIDC this season and renamed its session cookies.

    The lesson encoded here: never gate on a cookie name. `pl_profile` and
    `sessionid` are gone, `ST`/`ST-NO-SS` took their place, and a check for the
    old names rejected a perfectly good logged-in session.
    """

    def test_recognises_the_current_season_cookies(self) -> None:
        session = Session(cookies={"ST": "abc", "ST-NO-SS": "def", "pl_guest_id": "x"})
        assert session.has_credentials
        assert "ST" in session.recognised_cookies

    def test_unknown_cookie_names_still_count_as_credentials(self) -> None:
        """A renamed cookie must not be discarded before the API sees it."""
        session = Session(cookies={"some_future_name": "abc"})
        assert session.has_credentials
        assert session.recognised_cookies == []
        assert "none recognised" in session.diagnose()

    def test_a_bearer_token_alone_is_credentials(self) -> None:
        session = Session(tokens=OidcTokens(access_token="eyJhbGciOi.payload.sig"))
        assert session.has_credentials
        assert "OIDC access token" in session.diagnose()

    def test_nothing_captured(self) -> None:
        assert Session().has_credentials is False
        assert Session().diagnose() == "nothing captured"


class TestAccessTokenIsNotTheIdToken:
    """Only the access token authenticates; the id token must not stand in.

    An earlier version fell back to `id_token` when no `access_token` was
    present. That is wrong: an id token asserts *who the user is* to the client,
    while an access token authorises API calls. Substituting one for the other
    produces a credential that looks plausible and always 403s.
    """

    def test_no_access_token_means_no_tokens(self) -> None:
        entries = {
            "oidc.user:https://account.premierleague.com/as:cid": json.dumps(
                {"id_token": "eyJ.id.only"}
            )
        }
        tokens = OidcTokens.from_local_storage(entries)
        assert tokens is not None
        assert tokens.access_token is None


class TestEnvFileWriting:
    """Writing .env directly, rather than printing a credential to paste."""

    def test_adds_a_new_key(self, tmp_path) -> None:
        from arsenal.config import update_env_file

        env = tmp_path / ".env"
        update_env_file({"FPL_TEAM_ID": "123"}, env)
        assert "FPL_TEAM_ID=123" in env.read_text(encoding="utf-8")

    def test_replaces_an_existing_key_and_keeps_the_rest(self, tmp_path) -> None:
        env = tmp_path / ".env"
        env.write_text("# comment\nFPL_TEAM_ID=old\nOTHER=keep\n", encoding="utf-8")

        from arsenal.config import update_env_file

        update_env_file({"FPL_TEAM_ID": "new"}, env)
        content = env.read_text(encoding="utf-8")
        assert "FPL_TEAM_ID=new" in content
        assert "FPL_TEAM_ID=old" not in content
        assert "OTHER=keep" in content
        assert "# comment" in content

    def test_a_long_json_value_round_trips(self, tmp_path, monkeypatch) -> None:
        """The failure that made this function necessary.

        Printing the value for manual pasting meant the terminal wrapped it, and
        the multi-line paste could not be parsed. Writing it directly keeps it on
        one line.
        """
        from arsenal.config import load_dotenv, update_env_file

        monkeypatch.delenv("FPL_SESSION_JSON", raising=False)
        blob = json.dumps({"cookies": {"ST": "x" * 400}, "access_token": "eyJ." * 200})
        env = tmp_path / ".env"
        update_env_file({"FPL_SESSION_JSON": blob}, env)

        assert len(env.read_text(encoding="utf-8").strip().splitlines()) == 1
        load_dotenv(env)

        import os

        assert json.loads(os.environ["FPL_SESSION_JSON"])["cookies"]["ST"] == "x" * 400


class TestSecretsFromEnv:
    """Direct coverage for `Secrets.from_env`.

    Two bugs shipped through this function in a row — a crash when no session was
    configured, and a crash when the session was malformed. Both were in the
    *degenerate* paths, which is exactly where a fresh checkout and a broken
    setup live. Those are the states a new user is actually in.
    """

    @pytest.fixture(autouse=True)
    def _isolated_env(self, tmp_path, monkeypatch):
        """Point .env somewhere empty so a developer's real one cannot leak in."""
        from arsenal import config

        monkeypatch.setattr(config, "DEFAULT_ENV_PATH", tmp_path / ".env")
        for key in ("FPL_TEAM_ID", "FPL_SESSION_JSON", "ANTHROPIC_API_KEY"):
            monkeypatch.delenv(key, raising=False)

    def test_no_session_configured(self, monkeypatch) -> None:
        """The normal state on a fresh checkout."""
        from arsenal.config import Secrets

        secrets = Secrets.from_env()
        assert secrets.session_cookies == {}
        assert secrets.access_token is None
        assert secrets.has_session is False
        assert secrets.team_id is None

    def test_malformed_session_degrades_instead_of_raising(self, monkeypatch) -> None:
        """A mangled value must not break the `auth` commands needed to fix it."""
        from arsenal.config import Secrets

        monkeypatch.setenv("FPL_SESSION_JSON", '{"cookies": {"ST": "trunc')
        secrets = Secrets.from_env()
        assert secrets.session_cookies == {}
        assert secrets.has_session is False

    def test_reads_a_cookie_map_and_token(self, monkeypatch) -> None:
        from arsenal.config import Secrets

        monkeypatch.setenv(
            "FPL_SESSION_JSON",
            json.dumps({"cookies": {"ST": "abc"}, "access_token": "eyJ.a.b"}),
        )
        secrets = Secrets.from_env()
        assert secrets.session_cookies == {"ST": "abc"}
        assert secrets.access_token == "eyJ.a.b"
        assert secrets.has_session is True

    def test_reads_a_playwright_storage_state(self, monkeypatch) -> None:
        from arsenal.config import Secrets

        monkeypatch.setenv(
            "FPL_SESSION_JSON",
            json.dumps(
                {
                    "cookies": [
                        {"name": "ST", "value": "abc", "domain": ".premierleague.com"},
                        {"name": "other", "value": "x", "domain": ".example.com"},
                    ],
                    "origins": [],
                }
            ),
        )
        assert Secrets.from_env().session_cookies == {"ST": "abc"}

    def test_a_token_alone_counts_as_a_session(self, monkeypatch) -> None:
        from arsenal.config import Secrets

        monkeypatch.setenv("FPL_SESSION_JSON", json.dumps({"access_token": "eyJ.a.b"}))
        secrets = Secrets.from_env()
        assert secrets.has_session is True
        assert secrets.session_cookies == {}

    def test_team_id_is_parsed_as_an_int(self, monkeypatch) -> None:
        from arsenal.config import Secrets

        monkeypatch.setenv("FPL_TEAM_ID", "5529035")
        assert Secrets.from_env().team_id == 5529035
