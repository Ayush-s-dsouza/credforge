from credforge.providers.signup_recipes import LIVE_SIGNUP_RECIPES
from credforge.utils.domains import registrable_domain


def test_recipe_keys_are_registrable_domains_not_full_hostnames() -> None:
    # Real bug caught before ever running live: PlaywrightBrowserDriver
    # looks recipes up by registrable_domain(developer_portal_url), which
    # drops subdomains (registrable_domain("https://api.nasa.gov/") ==
    # "nasa.gov", not "api.nasa.gov"). A recipe keyed on the full
    # hostname would silently never match and always fall through to
    # "no recipe registered."
    for key in LIVE_SIGNUP_RECIPES:
        assert registrable_domain(f"https://{key}/") == key, (
            f"recipe key {key!r} is not a registrable domain -- PlaywrightBrowserDriver will never find it"
        )
