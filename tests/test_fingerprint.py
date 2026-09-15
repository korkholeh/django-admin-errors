import pytest

from admin_errors import fingerprint as fp

# Golden values, pinned from the real implementation at authoring time. Changing the algorithm
# changes these and is a breaking change (ADR 0003, spec section 8).
GOLDEN_EXCEPTION_PARTS = [
    "builtins.ValueError",
    "shop.views.checkout",
    "invalid literal for int() with base #: <str>",
]
GOLDEN_EXCEPTION_HASH = "502198a1c1e09eebccfcfa5d04df6f22b120857e"
GOLDEN_MESSAGE_HASH = "20c64a1c3b95ab94797016508a05773164b0cdb7"
GOLDEN_OVERRIDE_HASH = "51cf5102e59056840195368b656125320d3d814a"


def test_golden_exception_fingerprint():
    fingerprint = fp.for_exception(
        ValueError,
        "shop.views.checkout",
        "invalid literal for int() with base 10: 'abc'",
    )
    assert fingerprint == GOLDEN_EXCEPTION_HASH


def test_golden_exception_fingerprint_matches_the_documented_parts_string():
    assert fp.qualified_type_name(ValueError) == GOLDEN_EXCEPTION_PARTS[0]
    assert (
        fp.normalize_message("invalid literal for int() with base 10: 'abc'")
        == (GOLDEN_EXCEPTION_PARTS[2])
    )
    assert fp.compute(GOLDEN_EXCEPTION_PARTS) == GOLDEN_EXCEPTION_HASH


def test_golden_message_fingerprint():
    assert fp.for_message("payments", "ERROR", "Payment failed for order %s") == (
        GOLDEN_MESSAGE_HASH
    )


def test_golden_override_fingerprint():
    assert fp.for_override("payments-timeout") == GOLDEN_OVERRIDE_HASH


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Order 12345 failed", "Order # failed"),
        ("User 123e4567-e89b-12d3-a456-426614174000 not found", "User <uuid> not found"),
        ("Hash deadbeefcafebabe mismatch", "Hash <hex> mismatch"),
        ("Segfault at 0x7ffeefbff5a8", "Segfault at <addr>"),
        ("Failed at 2026-09-15T10:22:31Z", "Failed at <ts>"),
        ("Failed at 2026-09-15", "Failed at <ts>"),
        # Hex beats digits for runs >= 8 chars, even when purely numeric (spec section 8.1 order).
        ("Order 12345678 failed", "Order <hex> failed"),
        ("KeyError: 'foo'", "KeyError: <str>"),
        ('KeyError: "foo"', "KeyError: <str>"),
        ("a   b\tc", "a b c"),
        (
            "\n  Configuration invalid: missing KEY\n  fix it",
            "Configuration invalid: missing KEY",
        ),
    ],
)
def test_normalizes_each_family(message, expected):
    assert fp.normalize_message(message) == expected


def test_normalize_message_skips_leading_blank_lines():
    # A leading blank line (e.g. a dedented triple-quoted message) is not the message: distinct
    # multi-line messages at the same culprit must not collapse into one issue.
    a = fp.for_exception(ValueError, "shop.views.checkout", "\nfoo")
    b = fp.for_exception(ValueError, "shop.views.checkout", "\nbar")
    assert a != b


def test_normalize_message_keeps_only_the_first_line():
    assert fp.normalize_message("first line\nsecond line") == "first line"


def test_normalize_message_strips_surrounding_whitespace():
    assert fp.normalize_message("  padded line  \nsecond") == "padded line"


def test_normalize_message_empty_string():
    assert fp.normalize_message("") == ""


def test_normalize_message_truncates_to_200_chars():
    result = fp.normalize_message("x" * 300)
    assert len(result) == 200


def test_normalize_message_survives_unicode():
    assert fp.normalize_message("Помилка during оплата 42") == "Помилка during оплата #"


def test_same_message_two_culprits_do_not_merge():
    a = fp.for_exception(ValueError, "shop.views.checkout", "boom")
    b = fp.for_exception(ValueError, "shop.views.cart", "boom")
    assert a != b


def test_two_exception_types_one_message_do_not_merge():
    a = fp.for_exception(ValueError, "shop.views.checkout", "boom")
    b = fp.for_exception(TypeError, "shop.views.checkout", "boom")
    assert a != b


def test_message_records_use_the_raw_template_not_the_formatted_message():
    # Two "records" sharing a template but with different args must fingerprint identically,
    # because for_message takes the raw template (record.msg), not record.getMessage().
    formatted_1 = f"Payment failed for order {1}"
    formatted_2 = f"Payment failed for order {2}"
    assert formatted_1 != formatted_2

    fingerprint_1 = fp.for_message("payments", "ERROR", "Payment failed for order %s")
    fingerprint_2 = fp.for_message("payments", "ERROR", "Payment failed for order %s")
    assert fingerprint_1 == fingerprint_2 == GOLDEN_MESSAGE_HASH


def test_non_str_record_msg_is_normalized_through_str():
    class Template:
        def __str__(self):
            return "job 12345 failed"

    fingerprint = fp.for_message("celery", "ERROR", Template())
    assert fingerprint == fp.for_message("celery", "ERROR", "job # failed")


def test_explicit_override_wins_over_natural_inputs():
    natural = fp.for_exception(ValueError, "shop.views.checkout", "boom")
    override = fp.for_override("payments-timeout")
    assert override != natural
    assert override == fp.compute(["payments-timeout"])


def test_determinism_same_inputs_twice():
    a = fp.for_exception(ValueError, "shop.views.checkout", "boom 123")
    b = fp.for_exception(ValueError, "shop.views.checkout", "boom 123")
    assert a == b


def test_qualified_type_name_includes_builtins():
    assert fp.qualified_type_name(ValueError) == "builtins.ValueError"


class _CustomError(Exception):
    pass


def test_qualified_type_name_custom_exception():
    assert fp.qualified_type_name(_CustomError) == f"{__name__}._CustomError"
