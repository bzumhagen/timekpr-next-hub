"""agent.env round trip and lookup precedence (Phase 2 "single-command
enrollment" -- `enroll` writes this file itself, `run`'s argparse defaults
read it back)."""

from __future__ import annotations

import os

from timekpr_hub_agent import config as config_mod


def test_write_then_read_env_file_round_trips_all_fields(tmp_path):
    path = tmp_path / "agent.env"
    config_mod.write_env_file(
        hub_url="https://hub.lan",
        managed_users="alice,bob",
        tz="America/Denver",
        ca_cert="/etc/ssl/hub-ca.pem",
        path=path,
    )
    values = config_mod.read_env_file(path)
    assert values["TIMEKPR_HUB_URL"] == "https://hub.lan"
    assert values["TIMEKPR_HUB_MANAGED_USERS"] == "alice,bob"
    assert values["TIMEKPR_HUB_TZ"] == "America/Denver"
    assert values["TIMEKPR_HUB_CA_CERT"] == "/etc/ssl/hub-ca.pem"


def test_write_env_file_is_group_readable_only(tmp_path):
    path = tmp_path / "agent.env"
    config_mod.write_env_file(
        hub_url="https://hub.lan", managed_users="alice", tz="UTC", ca_cert=None, path=path
    )
    mode = path.stat().st_mode & 0o777
    assert mode == 0o640


def test_read_env_file_missing_file_returns_empty_dict(tmp_path):
    assert config_mod.read_env_file(tmp_path / "nope.env") == {}


def test_read_env_file_ignores_comments_and_blank_lines(tmp_path):
    path = tmp_path / "agent.env"
    path.write_text("# a comment\n\nTIMEKPR_HUB_URL=https://hub.lan\n# TIMEKPR_HUB_TZ=ignored-if-commented\n")
    values = config_mod.read_env_file(path)
    assert values == {"TIMEKPR_HUB_URL": "https://hub.lan"}


def test_env_default_prefers_real_environment_over_file(monkeypatch):
    monkeypatch.setenv("TIMEKPR_HUB_URL", "https://from-env")
    assert (
        config_mod.env_default("TIMEKPR_HUB_URL", {"TIMEKPR_HUB_URL": "https://from-file"})
        == "https://from-env"
    )


def test_env_default_falls_back_to_file_then_none(monkeypatch):
    monkeypatch.delenv("TIMEKPR_HUB_URL", raising=False)
    assert (
        config_mod.env_default("TIMEKPR_HUB_URL", {"TIMEKPR_HUB_URL": "https://from-file"})
        == "https://from-file"
    )
    assert config_mod.env_default("TIMEKPR_HUB_URL", {}) is None


def test_chown_to_service_user_is_a_noop_when_user_does_not_exist(tmp_path):
    path = tmp_path / "device_token"
    path.write_text("secret")
    # Must not raise even though "definitely-not-a-real-user" doesn't exist
    # (mirrors a dev/test run where the packaged service account was never
    # created via sysusers.d).
    config_mod.chown_to_service_user(path, username="definitely-not-a-real-user")
    assert path.read_text() == "secret"


def test_chown_to_service_user_is_a_noop_without_permission(tmp_path):
    path = tmp_path / "device_token"
    path.write_text("secret")
    # Running as a non-root user, chown to our own account should either
    # succeed harmlessly or raise -- either way this must not propagate.
    config_mod.chown_to_service_user(path, username=os.environ.get("USER", "root"))
    assert path.read_text() == "secret"
