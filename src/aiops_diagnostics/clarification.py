"""What a `clarification` reply is actually asking for (#636).

**The defect this exists for.** A clarification reply carried one machine-
readable thing: nothing. It had `missing_fields` (a list of *context* names the
shipping client switches on) and `message` (a localized sentence). Neither
answers "which branch produced this", so on the production host a live
clarification left one localizable sentence and a `route_type='clarification'`
metric row and nothing else — the read of that incident came down to comparing
message byte-lengths across branches, which is both laborious and unsound (the
same 200 could be any of them; that read was wrong once).

**Why a code and not a longer message.** The message is for a person and is
translated per request language, so it cannot be an identifier. The code is
stable, never localized, and is the same string in the response body, in the
metric row and in the log line — one string an operator greps for and finds all
three.

**Why `missing_fields` was not enough.** It describes what the reply asks the
*client* to collect (`order_no`, `context`, `language`), which is a different
question. One of the codes below reports something `missing_fields` can never
express — "your account cannot see any orders" is not a missing field, and the
old reply listed nothing missing while telling the user an order was not theirs.

**The vocabulary.** `CLARIFY_*` names the *decision*, not the message key, so a
reworded message never renames a code and a new branch cannot quietly reuse an
old one.
"""

from __future__ import annotations

from aiops_diagnostics.routing import MISSING_CONTEXT, MISSING_ORDER_NO

#: The context name that means "this request cannot be answered in this
#: language" — a member of `missing_fields`, not a `CLARIFY_*` code, because the
#: client does react to it by re-asking in another language.
MISSING_LANGUAGE = "language"

#: The assistant entry is not where this request belongs (a clicked jump action).
CLARIFY_WRONG_ENTRY = "CLARIFY_WRONG_ENTRY"
#: An order-bound request arrived with no recognisable order number.
CLARIFY_ORDER_REQUIRED = "CLARIFY_ORDER_REQUIRED"
#: A money question needs context the request did not carry.
CLARIFY_CONTEXT_REQUIRED = "CLARIFY_CONTEXT_REQUIRED"
#: An order was named and the caller may not see it.
CLARIFY_ORDER_NOT_YOURS = "CLARIFY_ORDER_NOT_YOURS"
#: The caller's account resolves to an EMPTY operator site set (#637), so no
#: order is visible to it. Distinct from the line above on purpose: that one is a
#: statement about the ORDER, this one about the ACCOUNT — and the account is
#: the one a person can act on (ask for the shop binding to be added).
CLARIFY_ACCOUNT_HAS_NO_SITES = "CLARIFY_ACCOUNT_HAS_NO_SITES"
#: This language can be rendered but not routed, so no answer can be promised.
CLARIFY_LANGUAGE_UNROUTABLE = "CLARIFY_LANGUAGE_UNROUTABLE"

#: The context names `missing_fields` may carry, including the legacy
#: `"context"` spelling the money-question table has always used.
MISSING_FIELD_NAMES = frozenset({MISSING_ORDER_NO, MISSING_CONTEXT, MISSING_LANGUAGE})

#: Every code a clarification reply may carry. The registry is what keeps the set
#: closed: a test asserts each code is produced by some branch AND that the
#: structural log line knows it, so a new branch cannot ship a code nobody can
#: look up.
CLARIFICATION_CODES = (
    CLARIFY_WRONG_ENTRY,
    CLARIFY_ORDER_REQUIRED,
    CLARIFY_CONTEXT_REQUIRED,
    CLARIFY_ORDER_NOT_YOURS,
    CLARIFY_ACCOUNT_HAS_NO_SITES,
    CLARIFY_LANGUAGE_UNROUTABLE,
)

#: `missing_fields` per code. Not derivable from the code, and not a second
#: authority either: the request handler passes this to the response and the
#: codes above name the branch, so the two travel together by construction.
MISSING_FIELDS_BY_CODE: dict[str, tuple[str, ...]] = {
    CLARIFY_WRONG_ENTRY: (),
    CLARIFY_ORDER_REQUIRED: (MISSING_ORDER_NO,),
    CLARIFY_CONTEXT_REQUIRED: (MISSING_CONTEXT,),
    CLARIFY_ORDER_NOT_YOURS: (),
    CLARIFY_ACCOUNT_HAS_NO_SITES: (),
    CLARIFY_LANGUAGE_UNROUTABLE: (MISSING_LANGUAGE,),
}

#: The message key each code answers with. `wrong_entry` and `not_yours` are the
#: established keys; the two account/order statements are the #637 split of what
#: used to be one `not_yours`.
MESSAGE_KEY_BY_CODE: dict[str, str] = {
    CLARIFY_WRONG_ENTRY: "wrong_entry",
    CLARIFY_ORDER_REQUIRED: MISSING_ORDER_NO,
    CLARIFY_CONTEXT_REQUIRED: MISSING_CONTEXT,
    CLARIFY_ORDER_NOT_YOURS: "not_yours",
    CLARIFY_ACCOUNT_HAS_NO_SITES: "account_no_sites",
    # Rendered by ``free_text_unavailable_message``, which has its own table: the
    # reply must be in the requested language's own words, and that table exists
    # for exactly this case. The key is named here so the mapping stays total.
    CLARIFY_LANGUAGE_UNROUTABLE: "free_text_unavailable",
}


def missing_fields_for(code: str) -> list[str]:
    """The `missing_fields` a reply carrying ``code`` must report."""
    try:
        return list(MISSING_FIELDS_BY_CODE[code])
    except KeyError as exc:  # a code without a declaration is a defect, not a case
        raise ValueError(f"clarification 码未登记 missing_fields: {code}") from exc
