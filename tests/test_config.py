from dataclasses import fields
from pathlib import Path

import pytest

from core.config import (HOST, SCHEMA, Config, ConfigError, config_path, example_data, load, parse,
                         read_secret)

REPO = Path(__file__).resolve().parent.parent
GOOGLE = {"token_file": "/srv/token.json"}


def conf(**sections):
    """The example configuration with some keys replaced: conf(llm={"mode": "live"})."""
    data = example_data()
    data["server"]["data_dir"] = "/var/lib/arta-test"
    for section, values in sections.items():
        data[section].update(values)
    return data


# --- the example is the documentation of every setting -------------------------------------

def test_the_example_parses_and_has_exactly_the_schema():
    data = example_data()
    assert {s: set(t) for s, t in data.items()} == {s: set(k) for s, k in SCHEMA.items()}
    cfg = parse(data)
    assert cfg.llm == "mock" and cfg.intake == ("fixtures",)
    assert (cfg.updates, cfg.projects, cfg.cv) == ("fixtures", "fixtures", "fixtures")


def test_every_config_field_is_reachable_from_the_file():
    # A field nothing in the file can set would be a setting with a hidden default.
    local = vars(parse(conf(
        projects={"source": "local", "local_dir": "/srv/projects", "registry_file": "/srv/registry.json"},
        cv={"source": "local", "local_file": "/srv/cv/master.json"},
        portfolio_cover={"file": "/srv/p.pdf", "text_file": "/srv/cover.json"},
        llm={"mode": "live", "model": "m", "api_key_file": "/srv/key", "artist_profile_file": "/srv/p.md"},
        google={"token_file": "/srv/token.json", "client_secret_file": "/srv/secret.json"},
    )))
    drive = vars(parse(conf(
        projects={"source": "drive", "drive_folder": "Projects", "drive_categories": ["Main"]},
        cv={"source": "drive", "drive_file": "0000000000EXAMPLE-file_id"},
        portfolio_cover={"drive_file": "0000000000EXAMPLE-file_id"}, google=GOOGLE,
    )))
    set_somewhere = {k for k, v in local.items() if v not in (None, ())} | \
                    {k for k, v in drive.items() if v not in (None, ())}
    assert set_somewhere == {f.name for f in fields(Config)}


# --- keys: none missing, none unknown, right types -----------------------------------------

def test_a_missing_key_fails_loudly_and_names_it():
    data = conf()
    del data["llm"]["max_usd"]
    with pytest.raises(ConfigError, match=r"\[llm\] is missing max_usd"):
        parse(data)


def test_a_missing_section_fails_loudly():
    data = conf()
    del data["google"]
    with pytest.raises(ConfigError, match=r"\[google\] is missing"):
        parse(data)


def test_an_unknown_key_is_a_typo_not_ignored():
    with pytest.raises(ConfigError, match="unknown key.*sorce"):
        parse(conf(projects={"sorce": "drive"}))
    data = conf()
    data["sources"] = {}
    with pytest.raises(ConfigError, match="Unknown section"):
        parse(data)


@pytest.mark.parametrize("section,key,value", [
    ("server", "port", "8454"), ("server", "port", True), ("llm", "cache", "yes"),
    ("intake", "sources", "gmail"), ("llm", "max_usd", "1"), ("projects", "drive_categories", "Main"),
])
def test_wrong_types_are_refused(section, key, value):
    with pytest.raises(ConfigError, match=f"{section}.{key} has the wrong type"):
        parse(conf(**{section: {key: value}}))


# --- every kind of content names its source; nothing falls back --------------------------

@pytest.mark.parametrize("section", ["updates", "projects", "cv"])
@pytest.mark.parametrize("value", ["", "  ", "drives", "fixture"])
def test_a_source_has_no_default_and_no_guess(section, value):
    with pytest.raises(ConfigError, match=f"{section}.source="):
        parse(conf(**{section: {"source": value}}))


@pytest.mark.parametrize("section", ["updates", "projects", "cv"])
def test_none_means_not_configured(section):
    assert getattr(parse(conf(**{section: {"source": "none"}})), section) is None


def test_local_projects_need_their_folder_outside_the_repo():
    with pytest.raises(ConfigError, match="needs projects.local_dir"):
        parse(conf(projects={"source": "local"}))
    with pytest.raises(ConfigError, match="inside the repository"):
        parse(conf(projects={"source": "local", "local_dir": str(REPO / "tests" / "fixtures" / "projects")}))
    cfg = parse(conf(projects={"source": "local", "local_dir": "/srv/projects"}))
    assert cfg.projects == "local" and cfg.projects_local_dir == Path("/srv/projects")


def test_drive_projects_need_the_folder_and_take_categories():
    with pytest.raises(ConfigError, match="needs projects.drive_folder"):
        parse(conf(projects={"source": "drive"}, google=GOOGLE))
    with pytest.raises(ConfigError, match="google.token_file is required"):
        parse(conf(projects={"source": "drive", "drive_folder": "Projects"}))
    cfg = parse(conf(projects={"source": "drive", "drive_folder": "Projects", "drive_categories": [" Main ", "Old"]},
                     google=GOOGLE))
    assert cfg.drive_folder == "Projects" and cfg.drive_categories == ("Main", "Old")
    assert parse(conf(projects={"source": "drive", "drive_folder": "Projects"}, google=GOOGLE)).drive_categories == ()


@pytest.mark.parametrize("categories", [[""], ["Main", "Main"], [1]])
def test_bad_categories_are_refused(categories):
    with pytest.raises(ConfigError, match="drive_categories"):
        parse(conf(projects={"source": "drive", "drive_folder": "P", "drive_categories": categories}, google=GOOGLE))


@pytest.mark.parametrize("projects", [
    {"source": "fixtures", "local_dir": "/srv/projects"},
    {"source": "local", "local_dir": "/srv/projects", "drive_folder": "Projects"},
    {"source": "fixtures", "drive_categories": ["Main"]},
])
def test_a_setting_for_a_source_not_chosen_is_refused(projects):
    with pytest.raises(ConfigError, match="Clear it"):
        parse(conf(projects=projects))


def test_cv_sources():
    with pytest.raises(ConfigError, match="needs cv.local_file"):
        parse(conf(cv={"source": "local"}))
    with pytest.raises(ConfigError, match="cv.local_file"):
        parse(conf(cv={"source": "local", "local_file": "cv/master.json"}))
    assert parse(conf(cv={"source": "local", "local_file": "/srv/cv.json"})).cv_local_file == Path("/srv/cv.json")
    file_id = "0000000000EXAMPLE-file_id"
    with pytest.raises(ConfigError, match="needs cv.drive_file"):
        parse(conf(cv={"source": "drive"}, google=GOOGLE))
    with pytest.raises(ConfigError, match="google.token_file"):
        parse(conf(cv={"source": "drive", "drive_file": file_id}))
    assert parse(conf(cv={"source": "drive", "drive_file": file_id}, google=GOOGLE)).cv_drive_file == file_id
    with pytest.raises(ConfigError, match="Drive file id"):
        parse(conf(cv={"source": "drive", "drive_file": "https://drive.google.com/file/d/x"}, google=GOOGLE))
    with pytest.raises(ConfigError, match="Clear it"):
        parse(conf(cv={"source": "local", "local_file": "/srv/cv.json", "drive_file": file_id}, google=GOOGLE))


def test_intake_is_unset_when_empty_and_keeps_its_order():
    assert parse(conf(intake={"sources": []})).intake is None
    assert parse(conf(intake={"sources": ["artconnect"]})).intake == ("artconnect",)
    assert parse(conf(intake={"sources": ["gmail", "artconnect"]}, google=GOOGLE)).intake == ("gmail", "artconnect")


@pytest.mark.parametrize("value", [["imap"], ["gmail", "imap"], ["gmail", ""], ["gmail", "gmail"], [1]])
def test_intake_rejects_unknown_empty_and_repeated_names(value):
    with pytest.raises(ConfigError, match="intake.sources"):
        parse(conf(intake={"sources": value}, google=GOOGLE))


@pytest.mark.parametrize("sections", [{"intake": {"sources": ["gmail"]}}, {"updates": {"source": "gmail"}},
                                      {"projects": {"source": "drive", "drive_folder": "P"}},
                                      {"cv": {"source": "drive", "drive_file": "0000000000EXAMPLE"}},
                                      {"portfolio_cover": {"drive_file": "0000000000EXAMPLE"}}])
def test_a_google_source_needs_the_token_file(sections):
    with pytest.raises(ConfigError, match="google.token_file is required"):
        parse(conf(**sections))
    assert parse(conf(**sections, google=GOOGLE)).uses_google


# --- other values -------------------------------------------------------------------------

def test_llm_mode_is_required_and_checked():
    with pytest.raises(ConfigError, match="llm.mode"):
        parse(conf(llm={"mode": ""}))
    with pytest.raises(ConfigError, match="invalid"):
        parse(conf(llm={"mode": "maybe"}))


LIVE = {"mode": "live", "model": "some-model", "api_key_file": "/srv/arta/key", "artist_profile_file": "/srv/p.md"}


def test_live_is_accepted_and_holds_no_key():
    cfg = parse(conf(llm=LIVE))
    assert cfg.llm == "live" and cfg.api_key_file == Path("/srv/arta/key")


@pytest.mark.parametrize("missing", ["api_key_file", "model", "artist_profile_file"])
def test_live_without_a_required_setting_fails_loudly(missing):
    with pytest.raises(ConfigError, match=f"llm.{missing}"):
        parse(conf(llm={**LIVE, missing: ""}))


@pytest.mark.parametrize("port", [0, 70000])
def test_bad_port(port):
    with pytest.raises(ConfigError, match="server.port"):
        parse(conf(server={"port": port}))


def test_data_dir_is_required_absolute_and_outside_the_repo():
    with pytest.raises(ConfigError, match="no default"):
        parse(conf(server={"data_dir": ""}))
    with pytest.raises(ConfigError, match="absolute"):
        parse(conf(server={"data_dir": "data"}))
    with pytest.raises(ConfigError, match="inside the repository"):
        parse(conf(server={"data_dir": str(REPO / ".data")}))


@pytest.mark.parametrize("section,key", [("llm", "api_key_file"), ("google", "token_file"),
                                         ("google", "client_secret_file"), ("projects", "registry_file")])
def test_secret_and_personal_files_must_be_outside_the_repo(section, key):
    with pytest.raises(ConfigError, match="inside the repository"):
        parse(conf(**{section: {key: str(REPO / "secret.json")}}))


def test_host_is_loopback():
    assert HOST == "127.0.0.1"


# --- the file -----------------------------------------------------------------------------

def test_config_path_prefers_arta_config_then_the_home_default():
    assert config_path({"ARTA_CONFIG": "/etc/arta.toml", "HOME": "/home/x"}) == Path("/etc/arta.toml")
    assert config_path({"HOME": "/home/x"}) == Path("/home/x/.config/arta/config.toml")
    with pytest.raises(ConfigError, match="absolute"):
        config_path({"ARTA_CONFIG": "arta.toml"})


def test_a_missing_file_says_to_copy_the_example(tmp_path):
    with pytest.raises(ConfigError, match="Copy config.example.toml"):
        load(tmp_path / "config.toml")


def test_a_file_that_is_not_toml_fails_loudly(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("[server\nport = 1\n")
    with pytest.raises(ConfigError, match="not valid TOML"):
        load(p)


def test_load_reads_the_example_file(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text((REPO / "config.example.toml").read_text())
    assert load(p).port == 8454


# --- secret files -------------------------------------------------------------------------

def test_read_secret_refuses_open_missing_and_empty_files_without_echoing(tmp_path):
    p = tmp_path / "key"
    with pytest.raises(ConfigError, match="does not exist"):
        read_secret("llm.api_key_file", p)
    p.write_text("sk-secret-value\n")
    p.chmod(0o644)
    with pytest.raises(ConfigError, match="chmod 600") as e:
        read_secret("llm.api_key_file", p)
    assert "sk-secret-value" not in str(e.value)
    p.chmod(0o600)
    assert read_secret("llm.api_key_file", p) == "sk-secret-value"
    p.write_text("  \n")
    with pytest.raises(ConfigError, match="empty"):
        read_secret("llm.api_key_file", p)


def test_demo_is_a_source_of_its_own():
    cfg = parse(conf(intake={"sources": ["demo"]}, updates={"source": "demo"}, projects={"source": "demo"},
                     cv={"source": "demo"}))
    assert (cfg.intake, cfg.updates, cfg.projects, cfg.cv) == (("demo",), "demo", "demo", "demo")
    assert cfg.demo_projects_dir == Path("/var/lib/arta-test/demo-projects") and not cfg.uses_google
