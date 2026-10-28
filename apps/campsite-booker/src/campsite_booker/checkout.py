"""Paying for a held site: order details, cart, payment, confirm.

recreation.gov's checkout, as its own code lays it out (the camping app's
`OrderDetailsPage` chunk and the cart bundle; read from the site's
JavaScript, not yet watched end to end signed in):

1. **Order details** (`/camping/reservations/orderdetails?id=`): a group size
   per site (`group_size_<campsite id>…` inputs, where the campground asks),
   the "Need to Know" acknowledgment (`#need-to-know-checkbox`, "I have read
   and agree to this important information") and `#action-bar-submit`
   ("Proceed to Cart"). Anything else a campground requires (vehicles,
   equipment, a permit holder) shows up as validation errors; we stop there.
2. **Cart** (`/cart`): "Proceed to Payment". A park-pass upsell page may
   come first; it's skipped.
3. **Payment**: "Saved Credit Card" from the account's digital wallet. The
   wallet may first ask to be unlocked with the last 4 digits of a saved
   card (and its expiry): answered once from `CAMPSITE_CARD_*`, since wrong
   answers lock it for 24 hours. Then "Next".
4. **Review and Confirm**: "Confirm", then `/cart/confirmation`.

Paying needs a card saved on the recreation.gov account: this never types a
card number. `Settings.submit_payment` off stops at step 4 (a dry run).
Anything unexpected stops the checkout, and the site stays in the cart for
you to finish.
"""

import re
from dataclasses import dataclass

from loguru import logger
from playwright.sync_api import Page
from playwright.sync_api import TimeoutError as PlaywrightTimeout

from campsite_booker.config import Settings
from campsite_booker.db import DRY_RUN, FAILED, PAID, UNKNOWN

STEP_TIMEOUT_MS = 20_000
# After Confirm the site polls the payment for a while before confirming.
CONFIRM_TIMEOUT_MS = 120_000
ORDER_NUMBER = re.compile(
    r"(?:Order|Confirmation)\s*(?:#|Number|No\.?)?\s*:?\s*([0-9][0-9-]{5,})", re.I
)
NEED_TO_KNOW = "I have read and agree to this important information"


class CheckoutStopped(Exception):
    """A step didn't go as expected; the message says which, in words for a
    person. The site is still in the cart."""


@dataclass(frozen=True)
class CheckoutResult:
    status: str  # PAID / DRY_RUN / FAILED / UNKNOWN
    detail: str
    confirmation: str | None = None


def _on(url: str, *parts: str) -> bool:
    return any(part in url for part in parts)


def _errors(page: Page) -> str | None:
    """The page's own error text, if it's showing any."""
    selectors = (
        "#order-details-violations, #order-details-error, "
        '#order-details-grid-error, [role="alert"], .sarsa-alert'
    )
    texts = []
    for node in page.locator(selectors).all():
        try:
            text = " ".join(node.inner_text(timeout=1_000).split())
        except PlaywrightTimeout:
            continue
        if text and text not in texts:
            texts.append(text)
    return "; ".join(texts)[:400] or None


def order_details(page: Page, group_size: int) -> None:
    page.locator("#action-bar-submit").wait_for(timeout=STEP_TIMEOUT_MS)
    for field in page.locator('input[id^="group_size_"]').all():
        if field.is_visible() and not field.input_value():
            field.fill(str(group_size))
    box = page.get_by_label(NEED_TO_KNOW)
    if box.count() and not box.first.is_checked():
        box.first.check()
    page.locator("#action-bar-submit").click()
    try:
        page.wait_for_url(lambda url: _on(url, "/cart"), timeout=STEP_TIMEOUT_MS)
    except PlaywrightTimeout:
        raise CheckoutStopped(
            "Order details need something filled in: "
            + (_errors(page) or "no error shown")
        ) from None


def to_payment(page: Page) -> None:
    page.get_by_role("button", name="Proceed to Payment").or_(
        page.get_by_role("link", name="Proceed to Payment")
    ).first.click(timeout=STEP_TIMEOUT_MS)
    skip = page.locator("#parkpass-upsell-page-skip")
    try:
        skip.wait_for(timeout=3_000)
        skip.click()
    except PlaywrightTimeout:
        pass
    try:
        page.get_by_text(
            re.compile("Select Payment|Pay with a Credit Card")
        ).first.wait_for(timeout=STEP_TIMEOUT_MS)
    except PlaywrightTimeout:
        raise CheckoutStopped(
            "The payment step didn't come up: " + (_errors(page) or "no error shown")
        ) from None


def unlock_wallet(page: Page, settings: Settings) -> None:
    """Answer the wallet's "last 4 digits" challenge, once, if it asks."""
    unlock = page.get_by_role("button", name="Unlock")
    if not unlock.count():
        return
    if not settings.card_last4:
        raise CheckoutStopped(
            "recreation.gov wants the saved card's last 4 digits to unlock the "
            "wallet; set CAMPSITE_CARD_LAST4 (and _EXP_MONTH / _EXP_YEAR)"
        )
    page.locator("#last4").fill(settings.card_last4)
    month, year = page.locator("#month"), page.locator("#year")
    if month.count() or year.count():
        if not (settings.card_exp_month and settings.card_exp_year):
            raise CheckoutStopped(
                "The wallet wants the card's expiry too; set CAMPSITE_CARD_EXP_MONTH "
                "and CAMPSITE_CARD_EXP_YEAR"
            )
        month.select_option(settings.card_exp_month)
        year.select_option(settings.card_exp_year)
    unlock.click()
    try:
        unlock.wait_for(state="detached", timeout=STEP_TIMEOUT_MS)
    except PlaywrightTimeout:
        raise CheckoutStopped(
            "The wallet didn't unlock (it locks for 24 hours after a few wrong "
            "tries, so this doesn't try again): " + (_errors(page) or "no error shown")
        ) from None


def choose_saved_card(page: Page) -> None:
    saved = page.get_by_text("Saved Credit Card")
    if not saved.count():
        raise CheckoutStopped(
            "No saved card on the account; save one in your recreation.gov "
            "account's wallet to pay automatically"
        )
    # The saved cards are radios under that heading; take the selected one,
    # or the first.
    radios = page.locator('input[type="radio"]:not(#existing):not(#new)')
    if radios.count() and not any(r.is_checked() for r in radios.all()):
        radios.first.check()
    page.get_by_role("button", name="Next", exact=True).click()
    try:
        page.get_by_text("Review and Confirm").first.wait_for(timeout=STEP_TIMEOUT_MS)
    except PlaywrightTimeout:
        raise CheckoutStopped(
            "Choosing the saved card didn't reach Review and Confirm: "
            + (_errors(page) or "no error shown")
        ) from None


def confirm(page: Page) -> CheckoutResult:
    page.get_by_role("button", name="Confirm", exact=True).click()
    try:
        page.wait_for_url(
            lambda url: _on(url, "/cart/confirmation"), timeout=CONFIRM_TIMEOUT_MS
        )
    except PlaywrightTimeout:
        error = _errors(page)
        if error:
            return CheckoutResult(FAILED, f"Payment refused: {error}")
        return CheckoutResult(
            UNKNOWN,
            "Pressed Confirm but no confirmation came up; check your "
            "reservations on recreation.gov before trying again",
        )
    page.wait_for_load_state("networkidle")
    if "checkout_still_pending" in page.url:
        return CheckoutResult(
            UNKNOWN, "recreation.gov is still confirming the payment; check your email"
        )
    match = ORDER_NUMBER.search(page.locator("body").inner_text())
    return CheckoutResult(PAID, "Paid", match.group(1) if match else None)


def complete_order(page: Page, settings: Settings, group_size: int) -> CheckoutResult:
    """Pay for what's in the cart, starting from the order details page the
    hold left open, or from the cart. Never raises for the site's reasons."""
    try:
        if not _on(page.url, "/cart"):
            order_details(page, group_size)
        to_payment(page)
        unlock_wallet(page, settings)
        choose_saved_card(page)
        if not settings.submit_payment:
            return CheckoutResult(
                DRY_RUN,
                "Dry run: got to Review and Confirm and stopped "
                "(CAMPSITE_SUBMIT_PAYMENT is off). Nothing paid; it's still in "
                "your cart.",
            )
        return confirm(page)
    except CheckoutStopped as e:
        return CheckoutResult(FAILED, str(e))
    except Exception as e:
        logger.exception("Checkout failed")
        return CheckoutResult(FAILED, f"{type(e).__name__}: {e}"[:300])
