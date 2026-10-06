"""Link previews for tree pages: Open Graph tags and the focal-clade card.

Covers social_preview_service.py (genus, tip selection, rendering, cache) and
the two routes that use it: the tags in /job/<id>/view and the image at
/job/<id>/social-preview.png. Trees are synthetic Newick in a temp JOB_DIR.
"""

import hashlib
import io
import json
import os
import re
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from PIL import Image

from app.config import Config
from app.services import social_preview_service as sp

DESCRIPTION = "View this phylogenetic tree created with Dikarya"
REPO = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Tree builders
# ---------------------------------------------------------------------------

def _q(name):
    return "'" + name.replace("'", "''") + "'"


def balanced(names, length=0.01):
    """A balanced binary tree over `names`, every branch `length` (None: no lengths)."""
    suffix = "" if length is None else f":{length}"

    def build(items):
        if len(items) == 1:
            return _q(items[0]) + suffix
        mid = len(items) // 2
        return f"({build(items[:mid])},{build(items[mid:])}){suffix}"

    return build(list(names)) + ";"


def caterpillar(names, length=0.01):
    """((((a,b),c),d),e) -- nests as deep as there are names."""
    suffix = "" if length is None else f":{length}"
    text = _q(names[0]) + suffix
    for name in names[1:]:
        text = f"({text},{_q(name)}{suffix}){suffix}"
    return text + ";"


def make_job(root, job_id, newick, state=None, pruned=False):
    job_dir = root / job_id
    (job_dir / "tree").mkdir(parents=True)
    name = "tree_pruned.newick" if pruned else "tree_original.newick"
    (job_dir / "tree" / name).write_text(newick)
    if state is not None:
        (job_dir / "tree_state.json").write_text(
            state if isinstance(state, str) else json.dumps(state))
    return job_dir


def shown_names(model):
    return [t.name for t in sp._tips_under(model.root)]


def pleurotus_names(count, start=0):
    return [f"KM{100000 + i} Pleurotus ostreatus Oregon US" for i in range(start, start + count)]


# ---------------------------------------------------------------------------
# Genus
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label, genus", [
    ("KM387401 Pleurotus ostreatus MICH:139631 Michigan US", "Pleurotus"),
    ("iNat254003039 Pleurotus pulmonarius Port St. Lucie Florida US RiC 7", "Pleurotus"),
    ("iNat 264154811 Amanita amerirubescens02", "Amanita"),
    ("PZ011194.1_Cubamyces_lactineus_voucher_UNCA00454_small_subunit", "Cubamyces"),
    ("iNat191244590 Russula sp. 'IN27' Centre Hall Pennsylvania US", "Russula"),
    ('PX289274 Tubaria "sp-IN01" California US', "Tubaria"),
    ("MO123456 Pleurotus", "Pleurotus"),
    ("KX899654 uncultured Russulaceae Florida US", None),
    ("uncultured fungus clone OTU_12", None),
    ("KM1 environmental sample Pleurotus", None),
    ("PV995514.1", None),
    ("MH016920", None),
    ("KM1 Agaricales sp. Oregon", None),
    ("KM1 Russulaceae Florida US", None),
    ("KM1 Fungal sp. isolate 7", None),
    ("iNat311446634 Helvella Petoskey Michigan US", None),  # next word is a place
    ("", None),
    (None, None),
    ("\x00\x01 ((;;", None),
])
def test_extract_genus(label, genus):
    assert sp.extract_genus(label) == genus


def test_dominant_genus_majority_and_ties():
    labels = ["KM1 Pleurotus ostreatus"] * 3 + ["KM2 Lentinus tigrinus"] * 2
    assert sp.dominant_genus(labels) == "Pleurotus"
    # A tie goes to the genus met first -- the focal tip's, which is listed first.
    assert sp.dominant_genus(["KM9 Lentinus tigrinus", "KM1 Pleurotus ostreatus",
                              "KM2 Lentinus tigrinus", "KM3 Pleurotus ostreatus"]) == "Lentinus"
    # A single identifiable tip among many is not "the" genus of the tree.
    assert sp.dominant_genus(["KM1 Pleurotus ostreatus"] + ["PV99551%d.1" % i for i in range(8)]) is None


def test_titles():
    assert sp.title_for_genus("Pleurotus") == "Pleurotus phylogenetic tree — Dikarya"
    assert sp.title_for_genus(None) == "Phylogenetic tree — Dikarya"


def test_pleurotus_dominated_tree_title(tmp_path):
    names = pleurotus_names(30) + ["KM9 Lentinus tigrinus Peru", "KM8 Panus conchatus Peru"]
    focal = names[3]
    job_dir = make_job(tmp_path, "aaaa", balanced(names), {"sequence_of_interest": focal})
    meta = sp.social_metadata(job_dir)
    assert meta["title"] == "Pleurotus phylogenetic tree — Dikarya"
    assert meta["description"] == DESCRIPTION
    assert meta["has_preview"] is True


def test_no_usable_genus_falls_back(tmp_path):
    names = [f"PV99{i:04d}.1" for i in range(12)] + ["uncultured fungus clone 4"]
    job_dir = make_job(tmp_path, "aaab", balanced(names))
    meta = sp.social_metadata(job_dir)
    assert meta["title"] == "Phylogenetic tree — Dikarya"
    assert meta["has_preview"] is True


def test_genus_comes_from_the_preview_clade_not_the_whole_tree(tmp_path):
    # 200 Lentinus elsewhere in the tree; the focal neighbourhood is Pleurotus.
    local = pleurotus_names(12)
    far = [f"KM{i} Lentinus tigrinus Peru" for i in range(200)]
    newick = f"({balanced(local)[:-1]},{balanced(far)[:-1]});"
    job_dir = make_job(tmp_path, "aaac", newick, {"sequence_of_interest": local[0]})
    model = sp.build_preview_model(job_dir)
    assert model.mode == "focal"
    assert model.title == "Pleurotus phylogenetic tree — Dikarya"


# ---------------------------------------------------------------------------
# Tip selection
# ---------------------------------------------------------------------------

def test_small_tree_is_not_pruned(tmp_path):
    names = pleurotus_names(10)
    job_dir = make_job(tmp_path, "aaad", balanced(names), {"sequence_of_interest": names[4]})
    model = sp.build_preview_model(job_dir)
    assert model.mode == "full"
    assert model.shown == model.total == 10
    assert sorted(shown_names(model)) == sorted(names)
    assert model.focal is not None and model.focal.name == names[4]


@pytest.mark.parametrize("shape", [balanced, caterpillar])
@pytest.mark.parametrize("size", [60, 600, 3000])
def test_large_tree_is_reduced_and_keeps_focal(tmp_path, shape, size):
    names = pleurotus_names(size)
    focal = names[size // 3]
    job_dir = make_job(tmp_path, "aaae", shape(names), {"sequence_of_interest": focal})
    model = sp.build_preview_model(job_dir)
    assert model.mode == "focal"
    assert model.total == size
    assert 12 <= model.shown <= sp.TARGET_TIPS + 1
    assert model.focal is not None and model.focal.name == focal
    assert focal in shown_names(model)


def test_closest_relatives_are_kept(tmp_path):
    # The focal tip's sister is identical; a long branch separates the rest.
    sister, focal = "KM2 Pleurotus ostreatus sister", "KM1 Pleurotus ostreatus focal"
    rest = pleurotus_names(100)
    newick = f"(('{focal}':0.0001,'{sister}':0.0001):0.5,{balanced(rest, 0.02)[:-1]});"
    job_dir = make_job(tmp_path, "aaaf", newick, {"sequence_of_interest": focal})
    model = sp.build_preview_model(job_dir)
    assert {focal, sister} <= set(shown_names(model))
    assert model.shown <= sp.TARGET_TIPS + 1


def test_sister_context_is_shown(tmp_path):
    # focal clade (8) + sister clade (8) + a big outer group (300).
    inner = [f"KM{i} Pleurotus ostreatus" for i in range(8)]
    sister = [f"KM{100 + i} Pleurotus pulmonarius" for i in range(8)]
    outer = [f"KM{1000 + i} Lentinus tigrinus" for i in range(300)]
    newick = f"(({balanced(inner)[:-1]},{balanced(sister)[:-1]}):0.1,{balanced(outer)[:-1]});"
    job_dir = make_job(tmp_path, "aaag", newick, {"sequence_of_interest": inner[0]})
    names = set(shown_names(sp.build_preview_model(job_dir)))
    assert set(inner) <= names and set(sister) <= names
    assert any(n.startswith("KM1") and "Lentinus" in n for n in names)  # outer context


def test_trees_without_branch_lengths(tmp_path):
    names = pleurotus_names(200)
    job_dir = make_job(tmp_path, "aaah", balanced(names, length=None),
                       {"sequence_of_interest": names[7]})
    model = sp.build_preview_model(job_dir)
    shown = shown_names(model)
    assert names[7] in shown and names[6] in shown  # topological sister
    assert model.shown <= sp.TARGET_TIPS + 1
    png = sp.render_preview_png(model)
    assert Image.open(io.BytesIO(png)).size == (1200, 630)


def test_polytomy(tmp_path):
    star = pleurotus_names(150)
    newick = "(" + ",".join(f"{_q(n)}:0.0" for n in star) + ");"
    job_dir = make_job(tmp_path, "aaai", newick, {"sequence_of_interest": star[90]})
    model = sp.build_preview_model(job_dir)
    assert star[90] in shown_names(model)
    assert model.shown <= sp.TARGET_TIPS + 1
    sp.render_preview_png(model)


def test_focal_on_a_deep_branch(tmp_path):
    focal = "KM1 Pleurotus ostreatus outlier"
    rest = pleurotus_names(400, start=10)
    newick = f"('{focal}':1.5,{balanced(rest)[:-1]});"
    job_dir = make_job(tmp_path, "aaaj", newick, {"sequence_of_interest": focal})
    model = sp.build_preview_model(job_dir)
    assert focal in shown_names(model)
    assert 10 <= model.shown <= sp.TARGET_TIPS + 1


def test_missing_focal_large_tree_gives_bounded_overview(tmp_path):
    names = pleurotus_names(500)
    job_dir = make_job(tmp_path, "aaak", balanced(names))  # no tree_state.json at all
    model = sp.build_preview_model(job_dir)
    assert model.mode == "overview"
    assert model.focal is None
    assert 2 <= model.shown <= sp.TARGET_TIPS
    assert not (job_dir / "tree_state.json").exists()  # never created as a side effect


def test_stale_focal_is_treated_as_missing(tmp_path):
    names = pleurotus_names(100)
    job_dir = make_job(tmp_path, "aaal", balanced(names), {"sequence_of_interest": "gone"})
    model = sp.build_preview_model(job_dir)
    assert model.mode == "overview" and model.focal is None


def test_inat_blue_highlight_is_the_focal(tmp_path):
    names = pleurotus_names(100)
    state = {"selection_sets": {"Default": [names[42]]},
             "selection_set_colors": {"Default": "#1f77b4"}}
    job_dir = make_job(tmp_path, "aaam", balanced(names), state)
    model = sp.build_preview_model(job_dir)
    assert model.focal is not None and model.focal.name == names[42]


def test_renames_are_displayed_and_focal_still_matches(tmp_path):
    names = pleurotus_names(40)
    state = {"sequence_of_interest": names[5], "renames": {names[5]: "My Pleurotus ostreatus find"}}
    job_dir = make_job(tmp_path, "aaan", balanced(names), state, pruned=True)
    model = sp.build_preview_model(job_dir)
    assert model.labels[id(model.focal)] == "My Pleurotus ostreatus find"


def test_unusual_labels_render(tmp_path):
    names = ["", "KM1 Pleurotus ostreatus (x) [y]; z", "×Hybrid name", "Ünïcödé Pleurötus",
             "a" * 900, "tab\there", "KM2 'quoted' label", "KM3 Pleurotus ostreatus"] + pleurotus_names(30)
    newick = balanced([n or "x" for n in names])
    job_dir = make_job(tmp_path, "aaao", newick, {"sequence_of_interest": names[1]})
    model = sp.build_preview_model(job_dir)
    assert all(len(label) <= sp.MAX_LABEL_CHARS for label in model.labels.values())
    assert Image.open(io.BytesIO(sp.render_preview_png(model))).size == (1200, 630)


# ---------------------------------------------------------------------------
# Rendering, cache, failure handling
# ---------------------------------------------------------------------------

def test_preview_png_is_1200x630_and_cached(tmp_path):
    names = pleurotus_names(80)
    job_dir = make_job(tmp_path, "aaap", balanced(names), {"sequence_of_interest": names[0]})
    data, signature = sp.preview_png(job_dir)
    image = Image.open(io.BytesIO(data))
    assert image.format == "PNG" and image.size == (1200, 630)
    assert (job_dir / "social" / f"preview-{signature}.png").is_file()
    # Second request is served from the cache without drawing.
    with patch.object(sp, "render_preview_png", side_effect=AssertionError("redrawn")):
        again, same = sp.preview_png(job_dir)
    assert again == data and same == signature


def test_tree_edit_invalidates_cache(tmp_path):
    names = pleurotus_names(80)
    job_dir = make_job(tmp_path, "aaaq", balanced(names), {"sequence_of_interest": names[0]})
    _, first = sp.preview_png(job_dir)
    state_path = job_dir / "tree_state.json"
    state_path.write_text(json.dumps({"sequence_of_interest": names[50]}))
    os.utime(state_path, ns=(state_path.stat().st_atime_ns, state_path.stat().st_mtime_ns + 10**9))
    _, second = sp.preview_png(job_dir)
    assert second != first
    assert sorted(p.name for p in (job_dir / "social").glob("preview-*.png")) == [f"preview-{second}.png"]
    assert sp.social_metadata(job_dir)["version"] == second


def test_preview_never_modifies_the_tree(tmp_path):
    names = pleurotus_names(300)
    job_dir = make_job(tmp_path, "aaar", balanced(names), {"sequence_of_interest": names[9]})

    def digest():
        return {p.name: hashlib.sha256(p.read_bytes()).hexdigest()
                for p in job_dir.rglob("*") if p.is_file() and "social" not in p.parts}

    before = digest()
    sp.social_metadata(job_dir)
    sp.preview_png(job_dir, force=True)
    assert digest() == before


@pytest.mark.parametrize("newick", ["((((((", "this is not a tree", "", "(onlyone);"])
def test_corrupt_or_degenerate_tree_fails_gracefully(tmp_path, newick):
    job_dir = make_job(tmp_path, "aaas", newick, "{not json")
    meta = sp.social_metadata(job_dir)
    assert meta["has_preview"] is False
    assert meta["title"] == "Phylogenetic tree — Dikarya"
    assert sp.preview_png(job_dir) == (None, None)


def test_corrupt_state_still_draws_tree(tmp_path):
    names = pleurotus_names(50)
    job_dir = make_job(tmp_path, "aaat", balanced(names), "{corrupt")
    meta = sp.social_metadata(job_dir)
    assert meta["has_preview"] is True
    assert meta["title"] == "Pleurotus phylogenetic tree — Dikarya"


def test_job_without_tree_has_no_preview(tmp_path):
    job_dir = tmp_path / "aaau"
    job_dir.mkdir()
    assert sp.social_metadata(job_dir)["has_preview"] is False
    assert sp.preview_png(job_dir) == (None, None)


def test_committed_generic_image_matches_renderer():
    committed = REPO / "app" / "static" / sp.DEFAULT_IMAGE_STATIC_PATH
    assert Image.open(committed).size == (1200, 630)
    assert sp.generic_png(str(REPO / "app" / "static")) == committed.read_bytes()


def test_robots_lets_link_preview_bots_read_tree_pages():
    robots = (REPO / "app" / "static" / "robots.txt").read_text()
    group = robots.split("User-agent: Twitterbot", 1)[1].split("\n\n", 1)[0]
    for agent in ("facebookexternalhit", "Discordbot"):
        assert f"User-agent: {agent}" in group
    assert "Allow: /job/*/view" in group
    assert "Allow: /job/*/social-preview.png" in group
    assert "Disallow: /api/" in group and "Disallow: /job" in group


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@pytest.fixture
def client(tmp_path, monkeypatch):
    from app.config import config as config_map
    from app.extensions import limiter

    monkeypatch.setenv("ALLOW_SQLITE_FALLBACK", "1")
    config_class = config_map["development"]
    monkeypatch.setattr(config_class, "ERROR_LOG_PATH", tmp_path / "errors.log", raising=False)
    monkeypatch.setattr(config_class, "SQLALCHEMY_DATABASE_URI",
                        f"sqlite:///{tmp_path / 'app.db'}", raising=False)
    # Redis backs the limiter and is not reachable from the test sandbox.
    monkeypatch.setattr(config_class, "RATELIMIT_ENABLED", False, raising=False)
    from app import create_app

    saved = limiter.enabled
    app = create_app("development")
    app.config.update(TESTING=True)
    jobs = tmp_path / "jobs"
    jobs.mkdir()
    record = SimpleNamespace(user_id=None, status="completed", metrics={})
    monkeypatch.setattr(Config, "JOB_DIR", jobs)
    monkeypatch.setattr("app.main.routes.check_job_access",
                        lambda job_id, mode="view": (record, None, 200))
    yield app.test_client(), jobs
    limiter.enabled = saved


def _meta(html, attr, key):
    match = re.search(rf'<meta {attr}="{re.escape(key)}" content="([^"]*)"', html)
    assert match, f"missing {attr}={key}"
    return match.group(1)


def test_job_page_emits_open_graph_tags(client):
    test_client, jobs = client
    names = pleurotus_names(40) + ["KM9 Lentinus tigrinus"]
    make_job(jobs, "q0xa", balanced(names), {"sequence_of_interest": names[2]})
    html = test_client.get("/job/q0xa/view").get_data(as_text=True)

    title = "Pleurotus phylogenetic tree — Dikarya"
    assert f"<title>{title}</title>" in html
    assert _meta(html, "name", "description") == DESCRIPTION
    assert _meta(html, "property", "og:type") == "website"
    assert _meta(html, "property", "og:site_name") == "Dikarya"
    assert _meta(html, "property", "og:title") == title
    assert _meta(html, "property", "og:description") == DESCRIPTION
    assert _meta(html, "property", "og:url").startswith("https://")
    assert _meta(html, "property", "og:url").endswith("/job/q0xa/view")
    image = _meta(html, "property", "og:image")
    assert re.match(r"^https://[^/]+/job/q0xa/social-preview\.png\?v=[0-9a-f]{16}$", image)
    assert _meta(html, "property", "og:image:width") == "1200"
    assert _meta(html, "property", "og:image:height") == "630"
    assert _meta(html, "name", "twitter:card") == "summary_large_image"
    assert _meta(html, "name", "twitter:title") == title
    assert _meta(html, "name", "twitter:description") == DESCRIPTION
    assert _meta(html, "name", "twitter:image") == image


def test_preview_endpoint_returns_png(client):
    test_client, jobs = client
    names = pleurotus_names(60)
    make_job(jobs, "q0xb", balanced(names), {"sequence_of_interest": names[2]})
    response = test_client.get("/job/q0xb/social-preview.png?v=whatever")
    assert response.status_code == 200
    assert response.mimetype == "image/png"
    assert "public" in response.headers["Cache-Control"]
    # No session is issued. (The app answers every anonymous request -- even
    # /robots.txt -- with an expiry for an empty session cookie; that sets nothing.)
    cookie = response.headers.get("Set-Cookie", "")
    assert not cookie or "Max-Age=0" in cookie
    assert Image.open(io.BytesIO(response.data)).size == (1200, 630)
    etag = response.headers["ETag"]
    assert test_client.get("/job/q0xb/social-preview.png",
                           headers={"If-None-Match": etag}).status_code == 304


def test_preview_endpoint_serves_generic_image_when_no_card(client):
    test_client, jobs = client
    make_job(jobs, "q0xc", "garbage(((")
    response = test_client.get("/job/q0xc/social-preview.png")
    assert response.status_code == 200 and response.mimetype == "image/png"
    committed = REPO / "app" / "static" / sp.DEFAULT_IMAGE_STATIC_PATH
    assert response.data == committed.read_bytes()


def test_corrupt_job_page_still_renders_with_generic_card(client):
    test_client, jobs = client
    make_job(jobs, "q0xd", "garbage(((", "{bad json")
    response = test_client.get("/job/q0xd/view")
    assert response.status_code == 200
    html = response.get_data(as_text=True)
    assert "<title>Phylogenetic tree — Dikarya</title>" in html
    image = _meta(html, "property", "og:image")
    assert image.startswith("https://") and image.endswith("/static/img/social-preview-default.png")
    assert _meta(html, "property", "og:description") == DESCRIPTION


def test_metadata_failure_does_not_break_page(client):
    test_client, jobs = client
    make_job(jobs, "q0xe", balanced(pleurotus_names(5)))
    with patch.object(sp, "_social_metadata", side_effect=RuntimeError("boom")):
        response = test_client.get("/job/q0xe/view")
    assert response.status_code == 200
    assert "<title>Phylogenetic tree — Dikarya</title>" in response.get_data(as_text=True)


def test_preview_endpoint_hides_inaccessible_job(client, monkeypatch):
    test_client, _ = client
    monkeypatch.setattr("app.main.routes.check_job_access",
                        lambda job_id, mode="view": (None, "Job not found", 404))
    assert test_client.get("/job/zzzz/social-preview.png").status_code == 404
