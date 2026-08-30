"""Tests for serving under a subpath (BASE_PATH=/sfconcert).

The failure mode this guards against is silent: the app boots fine at a subpath
and every page loads without CSS, or sign-in bounces to the wrong URL. So these
assert on the exact strings the browser will see.
"""

import importlib
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config as config_module

STATIC = Path(__file__).resolve().parent.parent / "app" / "static"


@pytest.fixture
def app_at(monkeypatch):
    """Boot the app under a given BASE_PATH, then put the modules back."""
    from app import main as main_module

    def boot(base):
        monkeypatch.setenv("BASE_PATH", base)
        config_module.get_settings.cache_clear()
        importlib.reload(main_module)
        return main_module

    yield boot

    monkeypatch.delenv("BASE_PATH", raising=False)
    config_module.get_settings.cache_clear()
    importlib.reload(main_module)


# --- normalization -----------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", ""),
        ("   ", ""),
        ("/", ""),
        ("sfconcert", "/sfconcert"),
        ("/sfconcert", "/sfconcert"),
        ("/sfconcert/", "/sfconcert"),
        ("  /sfconcert/  ", "/sfconcert"),
        ("a/b", "/a/b"),
    ],
)
def test_base_path_normalization(raw, expected):
    assert config_module._base_path(raw) == expected


# --- the served page ---------------------------------------------------------


def test_root_deploy_serves_a_plain_base_tag(app_at):
    main = app_at("")
    body = TestClient(main.app).get("/").text
    assert '<base href="/" />' in body
    assert "__BASE__" not in body
    assert main.COOKIE_PATH == "/"


def test_subpath_deploy_rewrites_the_base_tag(app_at):
    main = app_at("/sfconcert")
    body = TestClient(main.app).get("/").text
    assert '<base href="/sfconcert/" />' in body
    assert "__BASE__" not in body


def test_subpath_deploy_scopes_the_session_cookie(app_at):
    """A cookie at path=/ would leak to everything else on the domain."""
    main = app_at("/sfconcert")
    assert main.COOKIE_PATH == "/sfconcert/"

    response = TestClient(main.app).get(
        "/callback?error=access_denied", follow_redirects=False
    )
    assert response.headers["location"] == "/sfconcert/?error=access_denied"


def test_subpath_deploy_redirects_sign_in_failures_under_the_prefix(app_at):
    main = app_at("/sfconcert")
    response = TestClient(main.app).get("/callback?code=x", follow_redirects=False)
    assert response.headers["location"] == "/sfconcert/?error=state_mismatch"


def test_root_deploy_redirects_to_the_root(app_at):
    main = app_at("")
    response = TestClient(main.app).get("/callback?code=x", follow_redirects=False)
    assert response.headers["location"] == "/?error=state_mismatch"


def test_routes_are_not_themselves_prefixed(app_at):
    """The proxy strips the prefix, so the app must still answer at bare paths."""
    main = app_at("/sfconcert")
    client = TestClient(main.app)
    assert client.get("/healthz").status_code == 200
    assert client.get("/static/app.js").status_code == 200


# --- regression guard on the frontend ----------------------------------------


def test_frontend_uses_no_root_absolute_urls():
    """One absolute "/static/..." or "/api/..." breaks the whole subpath deploy."""
    offenders = {}
    for name in ("index.html", "app.js"):
        text = (STATIC / name).read_text(encoding="utf-8")
        hits = re.findall(r'["`(]/(?:static|api|login|callback)\b[^"`)\s]*', text)
        if hits:
            offenders[name] = hits
    assert not offenders, f"root-absolute URLs would break subpath deploys: {offenders}"


def test_index_html_carries_the_base_placeholder():
    text = (STATIC / "index.html").read_text(encoding="utf-8")
    assert text.count("__BASE__") == 1
