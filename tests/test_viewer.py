"""Viewer generation smoke tests (ROADMAP 2.5).

The viewer's behavior (pan/zoom math, grouping, filtering) is JavaScript and
is exercised in a browser; these tests pin what Python can see: the template
carries the 2.5 controls, injection still works, and the file stays
self-contained (no external resources).
"""
import json
import re
import shutil

from wiremap import cli

from conftest import DEMO_DIR


def _generate(tmp_path, capsys):
    demo = tmp_path / "demo"
    shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))
    assert cli.main(["scan", str(demo)]) == 0
    capsys.readouterr()
    return (demo / ".wiremap" / "wiremap.html").read_text(encoding="utf-8")


def test_viewer_has_scalability_controls(tmp_path, capsys):
    html = _generate(tmp_path, capsys)
    assert 'id="search"' in html                     # text search box
    assert 'id="risk"' in html                       # risk-threshold slider
    assert 'id="fit"' in html                        # pan/zoom reset
    assert "setAttribute('viewBox'" in html          # viewBox pan/zoom
    assert "COLLAPSE_LIMIT = 60" in html             # collapse-by-file trigger


def test_graph_json_injected_and_escaped(tmp_path, capsys):
    html = _generate(tmp_path, capsys)
    assert "__GRAPH_JSON__" not in html
    payload = re.search(
        r'<script id="graph-data" type="application/json">(.*?)</script>',
        html, re.S).group(1)
    graph = json.loads(payload.replace("<\\/", "</"))
    assert graph["nodes"] and graph["edges"]


def test_history_json_injected(tmp_path, capsys):
    html = _generate(tmp_path, capsys)
    assert "__HISTORY_JSON__" not in html
    payload = re.search(
        r'<script id="history-data" type="application/json">(.*?)</script>',
        html, re.S).group(1)
    hist = json.loads(payload.replace("<\\/", "</"))
    assert isinstance(hist, list) and len(hist) == 1


def test_viewer_is_self_contained(tmp_path, capsys):
    html = _generate(tmp_path, capsys)
    assert not re.search(r'src="https?://', html)
    assert not re.search(r'href="https?://', html)
    assert "import(" not in html
