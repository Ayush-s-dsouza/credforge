from pathlib import Path

import pytest

from credforge.enums import CredentialType
from credforge.pipeline.discover_signup import (
    HardStopError,
    _build_anchored_regex,
    _build_recipe_from_classification,
    _build_selector,
    _check_llm_hard_stops,
    _check_structural_captcha_fields,
    _check_structural_payment_fields,
    _describe_intended_fills,
    _extract_anchored_value,
    load_generated_recipes,
    merge_recipes,
    save_generated_recipe,
)
from credforge.providers.playwright_browser import SignupRecipe
from credforge.providers.signup_generation import ClassifiedField, FormElement, SignupFormClassification


def _element(**overrides) -> FormElement:
    defaults = dict(selector="[name='email']", tag="input", type="email", name="email", required=True)
    defaults.update(overrides)
    return FormElement(**defaults)


def _classification(**overrides) -> SignupFormClassification:
    defaults = dict(
        field_map=[ClassifiedField(selector="[name='email']", purpose="email")],
        submit_selector="button[type='submit']",
        required_checkboxes=[],
        blockers=["none"],
        confidence=0.9,
    )
    defaults.update(overrides)
    return SignupFormClassification(**defaults)


# --- _build_selector: name preferred over id (the IPinfo lesson) --------


def test_build_selector_prefers_name_over_id() -> None:
    assert _build_selector({"tag": "input", "name": "email", "id": "headlessui-control-abc123"}) == "[name='email']"


def test_build_selector_falls_back_to_id_when_no_name() -> None:
    assert _build_selector({"tag": "input", "name": None, "id": "email-text"}) == "#email-text"


def test_build_selector_falls_back_to_tag_type_when_neither_present() -> None:
    assert _build_selector({"tag": "input", "name": None, "id": None, "type": "submit"}) == "input[type='submit']"


# --- structural payment-field hard stop (independent of the LLM) --------


def test_structural_payment_check_raises_on_card_number_field() -> None:
    elements = [_element(), _element(selector="#cc", name="cardNumber", type="text", required=False)]
    with pytest.raises(HardStopError, match="payment/card field"):
        _check_structural_payment_fields(elements)


def test_structural_payment_check_passes_clean_form() -> None:
    elements = [_element(), _element(selector="#pw", name="password", type="password")]
    _check_structural_payment_fields(elements)  # should not raise


# --- structural CAPTCHA hard stop (a hidden Turnstile field has no other DOM trace) --


def test_structural_captcha_check_raises_on_hidden_turnstile_response_field() -> None:
    # Real, live finding against twelvedata.com: a Cloudflare Turnstile
    # widget's only DOM trace is a hidden `cf-turnstile-response` input --
    # no visible widget for the LLM's own blockers judgment to have caught.
    elements = [
        _element(),
        _element(selector="#cf-chl-widget-x_response", name="cf-turnstile-response", type="hidden", required=False),
    ]
    with pytest.raises(HardStopError, match="CAPTCHA"):
        _check_structural_captcha_fields(elements)


def test_structural_captcha_check_raises_on_recaptcha_response_field() -> None:
    elements = [_element(), _element(selector="#g-recaptcha-response", name="g-recaptcha-response", type="hidden")]
    with pytest.raises(HardStopError, match="CAPTCHA"):
        _check_structural_captcha_fields(elements)


def test_structural_captcha_check_passes_clean_form() -> None:
    elements = [_element(), _element(selector="#pw", name="password", type="password")]
    _check_structural_captcha_fields(elements)  # should not raise


# --- LLM-reported hard stops ----------------------------------------------


def test_hard_stop_on_reported_captcha_blocker() -> None:
    with pytest.raises(HardStopError, match="captcha"):
        _check_llm_hard_stops(_classification(blockers=["captcha"]), confidence_threshold=0.75)


def test_hard_stop_on_low_confidence() -> None:
    with pytest.raises(HardStopError, match="confidence"):
        _check_llm_hard_stops(_classification(confidence=0.4), confidence_threshold=0.75)


def test_hard_stop_when_no_email_field_classified() -> None:
    classification = _classification(field_map=[ClassifiedField(selector="[name='x']", purpose="company")])
    with pytest.raises(HardStopError, match="email"):
        _check_llm_hard_stops(classification, confidence_threshold=0.75)


def test_hard_stop_when_no_submit_selector() -> None:
    with pytest.raises(HardStopError, match="submit"):
        _check_llm_hard_stops(_classification(submit_selector=None), confidence_threshold=0.75)


def test_clean_classification_raises_nothing() -> None:
    _check_llm_hard_stops(_classification(), confidence_threshold=0.75)  # should not raise


# --- anchored regex: built on stable prose, not the value's shape -------


def test_anchored_regex_extracts_value_after_literal_anchor_text() -> None:
    pattern = _build_anchored_regex("Your API key is:")
    value = _extract_anchored_value(pattern, "Welcome! Your API key is: XJ7QP2KD9M. Keep it safe.")
    # Trailing sentence punctuation ('.') is stripped -- it's part of the
    # prose, not the credential. See _extract_anchored_value's docstring.
    assert value == "XJ7QP2KD9M"


def test_anchored_regex_does_not_depend_on_a_specific_character_shape() -> None:
    # The whole point: a differently-shaped credential (lowercase, hyphens,
    # different length) still extracts correctly because the regex is
    # anchored on the surrounding words, not on assumptions about the
    # value's characters -- unlike the shape-anchored regexes D-061
    # documents breaking twice.
    pattern = _build_anchored_regex("API Key:")
    value = _extract_anchored_value(pattern, "Thanks for signing up.\nAPI Key: sk-live-abc123-def456\nDo not share this.")
    assert value == "sk-live-abc123-def456"


def test_anchored_regex_escapes_special_characters_in_anchor_text() -> None:
    # A real vendor's anchor text can itself contain regex metacharacters
    # (e.g. a parenthetical) -- must be treated literally, not as regex syntax.
    pattern = _build_anchored_regex("Your key (keep private):")
    value = _extract_anchored_value(pattern, "Your key (keep private): abc-123-XYZ")
    assert value == "abc-123-XYZ"


def test_extract_anchored_value_returns_none_on_no_match() -> None:
    pattern = _build_anchored_regex("Your API key is:")
    assert _extract_anchored_value(pattern, "This page says nothing relevant.") is None


def test_extract_anchored_value_strips_trailing_comma_too() -> None:
    pattern = _build_anchored_regex("token:")
    value = _extract_anchored_value(pattern, "your token: abc123XYZ, please store it safely")
    assert value == "abc123XYZ"


# --- recipe emission from a classification -------------------------------


def test_build_recipe_maps_known_purposes_to_named_recipe_fields() -> None:
    elements = [
        _element(selector="[name='email']", name="email", required=True),
        _element(selector="[name='password']", name="password", type="password", required=True),
        _element(selector="[name='company']", name="company", required=False),
    ]
    classification = _classification(
        field_map=[
            ClassifiedField(selector="[name='email']", purpose="email"),
            ClassifiedField(selector="[name='password']", purpose="password"),
            ClassifiedField(selector="[name='company']", purpose="company"),
        ],
    )
    recipe = _build_recipe_from_classification(
        classification, elements,
        domain="example.com", docs_url="https://example.com/docs", signup_url="https://example.com/signup",
        credential_type=CredentialType.API_KEY, page_regex=r"key:\s*(\S+)", email_regex=None,
        requires_email_verification=False,
    )
    assert recipe.email_field_selector == "[name='email']"
    assert recipe.password_field_selector == "[name='password']"
    assert recipe.company_field_selector == "[name='company']"
    assert recipe.generated_by == "discover_signup"
    assert recipe.generated_at is not None
    assert recipe.developer_portal_url_fallback == "https://example.com/signup"


def test_build_recipe_keeps_password_and_password_confirm_as_separate_selectors() -> None:
    # Real bug caught via live dry-run against api-ninjas.com: two distinct
    # password fields (password + confirm) both classified under a single
    # "password" purpose collapsed into one recipe field, silently losing
    # one selector. password_confirm is now its own purpose, mapped to its
    # own SignupRecipe field.
    elements = [
        _element(selector="[name='email']", name="email", required=True),
        _element(selector="#pw1", name="password", type="password", required=True),
        _element(selector="#pw2", name="password2", type="password", required=True),
    ]
    classification = _classification(
        field_map=[
            ClassifiedField(selector="[name='email']", purpose="email"),
            ClassifiedField(selector="#pw1", purpose="password"),
            ClassifiedField(selector="#pw2", purpose="password_confirm"),
        ],
    )
    recipe = _build_recipe_from_classification(
        classification, elements,
        domain="example.com", docs_url="https://example.com/docs", signup_url="https://example.com/signup",
        credential_type=CredentialType.API_KEY, page_regex=r"key:\s*(\S+)", email_regex=None,
        requires_email_verification=False,
    )
    assert recipe.password_field_selector == "#pw1"
    assert recipe.password_confirm_field_selector == "#pw2"


def test_build_recipe_routes_required_uncommon_purpose_to_extra_field_selectors() -> None:
    elements = [
        _element(selector="[name='email']", name="email", required=True),
        _element(selector="[name='phone']", name="phone", required=True),
    ]
    classification = _classification(
        field_map=[
            ClassifiedField(selector="[name='email']", purpose="email"),
            ClassifiedField(selector="[name='phone']", purpose="phone"),
        ],
    )
    recipe = _build_recipe_from_classification(
        classification, elements,
        domain="example.com", docs_url="https://example.com/docs", signup_url="https://example.com/signup",
        credential_type=CredentialType.API_KEY, page_regex=None, email_regex=r"key:\s*(\S+)",
        requires_email_verification=True,
    )
    assert recipe.extra_field_selectors == {"phone": "[name='phone']"}


def test_build_recipe_leaves_optional_uncommon_purpose_unfilled() -> None:
    elements = [
        _element(selector="[name='email']", name="email", required=True),
        _element(selector="[name='referral']", name="referral", required=False),
    ]
    classification = _classification(
        field_map=[
            ClassifiedField(selector="[name='email']", purpose="email"),
            ClassifiedField(selector="[name='referral']", purpose="other"),
        ],
    )
    recipe = _build_recipe_from_classification(
        classification, elements,
        domain="example.com", docs_url="https://example.com/docs", signup_url="https://example.com/signup",
        credential_type=CredentialType.API_KEY, page_regex=r"key:\s*(\S+)", email_regex=None,
        requires_email_verification=False,
    )
    assert recipe.extra_field_selectors == {}


# --- dry-run intended-fills preview --------------------------------------


def test_dry_run_intended_fills_shows_real_alias_but_never_a_real_password() -> None:
    classification = _classification(
        field_map=[
            ClassifiedField(selector="[name='email']", purpose="email"),
            ClassifiedField(selector="[name='password']", purpose="password"),
        ],
    )
    intended = _describe_intended_fills(classification, email_alias="composio.ftw+vendor-abc123@gmail.com")
    assert intended["[name='email']"] == "composio.ftw+vendor-abc123@gmail.com"
    assert "generated" in intended["[name='password']"].lower()
    assert intended["[name='password']"] != "composio.ftw+vendor-abc123@gmail.com"


# --- generated-recipe persistence + merge precedence ---------------------


def _sample_recipe(**overrides) -> SignupRecipe:
    defaults = dict(
        email_field_selector="[name='email']", submit_selector="button[type='submit']",
        credential_type=CredentialType.API_KEY, docs_url="https://example.com/docs",
    )
    defaults.update(overrides)
    return SignupRecipe(**defaults)


def test_save_and_load_generated_recipe_round_trips(tmp_path: Path) -> None:
    recipe = _sample_recipe(generated_by="discover_signup")
    save_generated_recipe(tmp_path, "example.com", recipe)
    loaded = load_generated_recipes(tmp_path)
    assert "example.com" in loaded
    assert loaded["example.com"].email_field_selector == "[name='email']"
    assert loaded["example.com"].generated_by == "discover_signup"


def test_load_generated_recipes_returns_empty_dict_when_directory_missing(tmp_path: Path) -> None:
    assert load_generated_recipes(tmp_path / "nonexistent") == {}


def test_load_generated_recipes_skips_a_corrupted_file_without_raising(tmp_path: Path) -> None:
    directory = tmp_path / "generated_recipes"
    directory.mkdir()
    (directory / "broken.json").write_text("{not valid json", encoding="utf-8")
    good_recipe = _sample_recipe()
    save_generated_recipe(tmp_path, "good.com", good_recipe)
    loaded = load_generated_recipes(tmp_path)
    assert "good.com" in loaded
    assert "broken" not in loaded


def test_merge_recipes_hand_authored_always_wins_on_collision() -> None:
    generated = {"example.com": _sample_recipe(email_field_selector="[name='generated-guess']")}
    hand_authored = {"example.com": _sample_recipe(email_field_selector="#hand-verified")}
    merged = merge_recipes(generated, hand_authored)
    assert merged["example.com"].email_field_selector == "#hand-verified"


def test_merge_recipes_keeps_generated_entries_with_no_hand_authored_collision() -> None:
    generated = {"generated-only.com": _sample_recipe()}
    hand_authored = {"other.com": _sample_recipe()}
    merged = merge_recipes(generated, hand_authored)
    assert "generated-only.com" in merged
    assert "other.com" in merged
