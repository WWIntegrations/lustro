"""places/lead-list with osmFile: a local OSM extract instead of an Overpass server.
The extract is written here with osmium; the guard on the file name is tested without it."""
import os

import pytest

from ww_scraper.actor import run_actor

osmium = pytest.importorskip("osmium")

CENTRE = (54.372, 18.638)


def _node(i, lat, lon, tags=()):
    return osmium.osm.mutable.Node(id=i, location=osmium.osm.Location(lon, lat), tags=dict(tags))


@pytest.fixture
def extract(tmp_path, monkeypatch):
    path = tmp_path / "test-latest.osm.pbf"
    w = osmium.SimpleWriter(str(path))
    try:
        w.add_node(_node(1, 54.3725, 18.6385, {"amenity": "dentist", "name": "Gabinet A",
                                              "website": "https://a.example", "phone": "+48 58 111 22 33",
                                              "addr:street": "Długa", "addr:housenumber": "1"}))
        w.add_node(_node(2, 54.3730, 18.6390, {"healthcare": "dentist", "name": "Bez strony"}))
        w.add_node(_node(3, 54.6000, 18.8000, {"amenity": "dentist", "name": "Daleko",
                                              "website": "https://far.example"}))
        w.add_node(_node(4, 54.3726, 18.6386, {"amenity": "cafe", "name": "Kawiarnia"}))
        # a building drawn as a way: centre of its bounding box, as Overpass reports it
        for i, (lat, lon) in enumerate([(54.3700, 18.6300), (54.3700, 18.6320), (54.3720, 18.6320)], 10):
            w.add_node(_node(i, lat, lon))
        w.add_way(osmium.osm.mutable.Way(id=5, nodes=[10, 11, 12, 10],
                                         tags={"amenity": "dentist", "name": "Klinika W",
                                               "contact:website": "https://w.example"}))
    finally:
        w.close()
    monkeypatch.setenv("WW_SCRAPER_OSM_DIR", str(tmp_path))
    return path


def _run(storage, actor, inp):
    rec = run_actor(actor, dict(inp, minIntervalSecs=0), storage, allow_loopback=True)
    return rec, (storage.dataset(rec["defaultDatasetId"]).items() if rec["status"] == "SUCCEEDED" else []), \
        storage.kv(rec["id"]).get("OUTPUT")


AREA = {"lat": CENTRE[0], "lon": CENTRE[1], "radiusMeters": 2000}


def test_reads_nodes_and_way_centres_in_the_circle(storage, extract):
    rec, items, out = _run(storage, "places", {"category": "dentist", "osmFile": extract.name,
                                               "redactPii": False, **AREA})
    assert rec["status"] == "SUCCEEDED", rec.get("statusMessage")
    by = {i["osmId"]: i for i in items}
    assert set(by) == {"node/1", "node/2", "way/5"}            # far node and the cafe left out
    a = by["node/1"]
    assert (a["name"], a["street"], a["website"], a["phone"]) == ("Gabinet A", "Długa", "https://a.example",
                                                                  "+48 58 111 22 33")
    assert a["osmUrl"] == "https://www.openstreetmap.org/node/1" and "ODbL" in a["license"]
    w = by["way/5"]
    assert (w["lat"], w["lon"], w["website"]) == (54.371, 18.631, "https://w.example")
    assert out["osmFile"] == extract.name and out["matched"] == 3 and "endpoint" not in out


def test_website_filter_bbox_limit_and_phone_mask(storage, extract):
    _, items, out = _run(storage, "places", {"category": "dentist", "osmFile": extract.name,
                                             "onlyWithWebsite": True, "bbox": [54.0, 18.0, 55.0, 19.0]})
    assert sorted(i["name"] for i in items) == ["Daleko", "Gabinet A", "Klinika W"]
    assert next(i for i in items if i["name"] == "Gabinet A")["phone"] == "[phone]"
    _, items, out = _run(storage, "places", {"tags": ["amenity=dentist"], "osmFile": extract.name,
                                             "maxResults": 1, "bbox": [54.0, 18.0, 55.0, 19.0]})
    assert len(items) == 1 and out["matched"] == 3


def test_city_name_is_refused_with_a_local_extract(storage, extract):
    rec, _, _ = _run(storage, "places", {"category": "dentist", "osmFile": extract.name, "city": "Gdańsk"})
    assert rec["status"] == "FAILED" and "lat+lon" in rec["statusMessage"]


@pytest.mark.parametrize("name", ["../test-latest.osm.pbf", "/etc/passwd", "sub/x.osm.pbf", ".hidden.osm.pbf",
                                  "notes.txt", "missing.osm.pbf", "a\\b.osm.pbf"])
def test_osm_file_is_a_name_inside_the_operator_directory(storage, extract, name):
    rec, _, _ = _run(storage, "places", {"category": "dentist", "osmFile": name, **AREA})
    assert rec["status"] == "FAILED" and "osmFile" in rec["statusMessage"]


def test_symlink_out_of_the_directory_is_refused(storage, extract, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "real.osm.pbf"
    outside.write_bytes(extract.read_bytes())
    os.symlink(outside, extract.parent / "link.osm.pbf")
    rec, _, _ = _run(storage, "places", {"category": "dentist", "osmFile": "link.osm.pbf", **AREA})
    assert rec["status"] == "FAILED" and "no extract" in rec["statusMessage"]


def test_without_the_directory_setting_osm_file_is_off(storage, extract, monkeypatch):
    monkeypatch.delenv("WW_SCRAPER_OSM_DIR")
    rec, _, _ = _run(storage, "places", {"category": "dentist", "osmFile": extract.name, **AREA})
    assert rec["status"] == "FAILED" and "WW_SCRAPER_OSM_DIR" in rec["statusMessage"]


def test_lead_list_passes_the_extract_to_places(storage, extract, monkeypatch):
    monkeypatch.delenv("WW_SCRAPER_OSM_DIR")
    rec, _, _ = _run(storage, "lead-list", {"category": "dentist", "osmFile": extract.name, **AREA})
    assert rec["status"] == "FAILED" and "WW_SCRAPER_OSM_DIR" in rec["statusMessage"]
