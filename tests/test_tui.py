"""Terminal plumbing that is easy to get wrong and impossible to notice: key decoding."""

import curses

from csp.tui import HELP, numeric, read_key


class FakeScr:
    def __init__(self, seq):
        self.seq = list(seq)

    def getch(self):
        return self.seq.pop(0)


def test_raw_csi_arrows_are_decoded():
    assert read_key(FakeScr([27, 91, 66])) == curses.KEY_DOWN
    assert read_key(FakeScr([27, 91, 65])) == curses.KEY_UP


def test_unknown_escape_is_swallowed_not_quit():
    """ESC alone must never look like 'q': it would drop the user out of the app."""
    assert read_key(FakeScr([27, 91, 67])) == -1
    assert read_key(FakeScr([ord("q")])) == ord("q")


def test_numeric_accepts_prices_and_rejects_junk():
    assert numeric("2000") and numeric("0.35") and numeric("18.22")
    assert not numeric("") and not numeric("1.2.3") and not numeric("abc") and not numeric("-1")


def test_help_fits_a_24_row_terminal():
    assert len(HELP) <= 23  # 24 rows minus the "any key" footer
    assert all(len(line) <= 110 for line in HELP)
