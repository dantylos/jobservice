from jobservice import places
from jobservice.filters import FilterConfig, check_remote_region, senior_word


def test_applicant_location_is_compact():
    assert places.applicant_location(["Czech Republic", "Slovakia"]) == "CZ, SK"
    assert places.applicant_location(["European Union"]) == "European Union"
    many = ["Poland", "Ukraine", "---", "Czech Republic", "Germany", "Austria", "Spain", "France"]
    assert places.applicant_location(many) == "7 countries incl. CZ"
    assert places.applicant_location(["Poland"] * 2) == "PL"
    assert places.applicant_location([]) is None


def test_requirement_names_shapes():
    assert places.requirement_names({"@type": "Country", "name": "Germany"}) == ["Germany"]
    assert places.requirement_names([{"name": "Poland"}, "Spain", None]) == ["Poland", "Spain"]
    assert places.requirement_names({"address": {"addressCountry": "UA"}}) == ["UA"]
    assert places.requirement_names(None) == []


def test_open_to_czechia():
    for loc in (None, "", "CZ, SK", "European Union", "Remote - Europe", "Worldwide",
                "EMEA", "200 countries incl. CZ"):  # fmt: skip
        assert places.open_to_czechia(loc), loc
    for loc in ("PL", "UA", "DE, AT", "12 countries", "United States"):
        assert not places.open_to_czechia(loc), loc


def test_market():
    assert places.market("Kyiv", None) == "ua"
    assert places.market(None, "UA") == "ua"
    assert places.market("Praha", "UA, PL") == "cz"
    assert places.market(None, None) == "cz"


def test_job_posting_in_graph_list_or_top_level():
    graph = '<script type="application/ld+json">{"@graph": [{"@type": "Organization"}, {"@type": "JobPosting", "title": "A"}]}</script>'  # noqa: E501
    top = '<script type="application/ld+json" class="x">{"@type": "JobPosting", "title": "B"}</script>'  # noqa: E501
    bad = '<script type="application/ld+json">{oops</script>'
    assert places.job_posting(bad + graph)["title"] == "A"
    assert places.job_posting(top)["title"] == "B"
    assert places.job_posting("<html></html>") is None


def test_remote_region_filter():
    cfg = FilterConfig()
    row = {"market": "cz", "work_mode": "remote", "applicant_location": "PL"}
    assert check_remote_region(row, cfg) == "Место: удалённо только для PL"
    assert check_remote_region({**row, "applicant_location": "CZ, PL"}, cfg) is None
    assert check_remote_region({**row, "applicant_location": None}, cfg) is None
    assert check_remote_region({**row, "work_mode": "onsite"}, cfg) is None
    assert check_remote_region({**row, "market": "ua", "applicant_location": "UA"}, cfg) is None


def test_senior_word_reads_slugs():
    assert senior_word("principal-site-reliability-engineer") == "principal"
    assert senior_word("Lead Generation Specialist") is None
    assert senior_word("junior-qa") is None
