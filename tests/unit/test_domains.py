from credforge.utils.domains import parse_bare_domain


def test_a_bare_registrable_domain_is_trusted() -> None:
    assert parse_bare_domain("nasa.gov") == "nasa.gov"
    assert parse_bare_domain("github.com") == "github.com"


def test_case_and_surrounding_whitespace_are_normalized() -> None:
    assert parse_bare_domain("  GitHub.COM  ") == "github.com"


def test_a_multi_part_public_suffix_is_handled_correctly() -> None:
    assert parse_bare_domain("amazon.co.uk") == "amazon.co.uk"


def test_a_subdomain_is_not_bare() -> None:
    # Has a real domain+suffix, but also a subdomain component -- not
    # "bare," so it must not be blindly trusted as the vendor's identity.
    assert parse_bare_domain("api.nasa.gov") is None
    assert parse_bare_domain("www.github.com") is None


def test_a_plain_company_name_with_no_recognized_suffix_is_not_a_domain() -> None:
    assert parse_bare_domain("Sage") is None
    assert parse_bare_domain("NASA API") is None


def test_a_dotted_string_with_no_real_public_suffix_is_not_a_domain() -> None:
    # Naive "has a dot" checks would wrongly accept this -- tldextract's
    # real public-suffix list correctly rejects "14" as a TLD.
    assert parse_bare_domain("3.14") is None
