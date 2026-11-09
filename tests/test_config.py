import os

from agent_auditor.config import _flag, get_settings


def test_flag_parsing():
    assert _flag(None, True) is True and _flag("", False) is False
    for text in ("1", "true", "YES", " on "):
        assert _flag(text, False) is True
    for text in ("0", "false", "No", "OFF"):
        assert _flag(text, True) is False
    try:
        _flag("maybe", True)
    except ValueError:
        return
    raise AssertionError("expected ValueError")


def test_settings_read_the_environment():
    keys = ("AUDITOR_DATABASE_URL", "AUDITOR_API_KEY", "AUDITOR_AUTO_CREATE_TABLES")
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ.update({"AUDITOR_DATABASE_URL": "postgresql+psycopg://u:p@db/x", "AUDITOR_API_KEY": "secret",
                           "AUDITOR_AUTO_CREATE_TABLES": "false"})
        s = get_settings()
        assert (s.database_url, s.api_key, s.auto_create_tables) == ("postgresql+psycopg://u:p@db/x", "secret", False)
        del os.environ["AUDITOR_AUTO_CREATE_TABLES"]
        os.environ["AUDITOR_API_KEY"] = ""
        s = get_settings()
        assert s.auto_create_tables is True and s.api_key is None
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
