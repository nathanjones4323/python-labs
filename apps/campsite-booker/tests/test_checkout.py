from dataclasses import replace

import pytest
from campsite_booker.checkout import CheckoutStopped, unlock_wallet
from playwright.sync_api import TimeoutError as PlaywrightTimeout


class FakeLocator:
    def __init__(self, page, selector: str) -> None:
        self.page, self.selector = page, selector

    def count(self) -> int:
        if self.selector == "Unlock":
            return 1
        return 1 if self.selector == "#last4" else 0

    def fill(self, value: str) -> None:
        self.page.typed.append(value)

    def click(self) -> None:
        pass

    def wait_for(self, state: str, timeout: int) -> None:
        if not self.page.unlocks:
            raise PlaywrightTimeout("still there")

    def all(self):
        return [self] if self.page.error else []

    def inner_text(self, timeout: int) -> str:
        return self.page.error


class FakePage:
    """The wallet's unlock form: `unlocks` says whether the digits work,
    `error` is what the page shows when they don't."""

    def __init__(self, unlocks: bool, error: str = "") -> None:
        self.unlocks, self.error = unlocks, error
        self.typed: list[str] = []

    def get_by_role(self, role: str, name: str) -> FakeLocator:
        return FakeLocator(self, name)

    def locator(self, selector: str) -> FakeLocator:
        return FakeLocator(self, selector)


@pytest.fixture
def card(settings):
    return replace(settings, card_last4="4242")


def test_refused_digits_arent_sent_again(card):
    page = FakePage(unlocks=False, error="The card information doesn't match")
    with pytest.raises(CheckoutStopped, match="refused"):
        unlock_wallet(page, card)
    assert page.typed == ["4242"]

    # Every later checkout stops before typing them.
    again = FakePage(unlocks=True)
    with pytest.raises(CheckoutStopped, match="aren't tried again"):
        unlock_wallet(again, card)
    assert again.typed == []

    # New digits are tried, and clear the refusal once they work.
    fixed = replace(card, card_last4="1881")
    unlock_wallet(FakePage(unlocks=True), fixed)
    assert not fixed.wallet_refusal_file.exists()
    unlock_wallet(FakePage(unlocks=True), card)


@pytest.mark.parametrize("error", ["", "Something went wrong. Try again later."])
def test_a_blip_isnt_a_refusal(card, error):
    with pytest.raises(CheckoutStopped, match="may be tried again"):
        unlock_wallet(FakePage(unlocks=False, error=error), card)
    assert not card.wallet_refusal_file.exists()
    page = FakePage(unlocks=True)
    unlock_wallet(page, card)
    assert page.typed == ["4242"]
