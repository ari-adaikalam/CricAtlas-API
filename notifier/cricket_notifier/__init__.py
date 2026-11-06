"""Private monitoring emails for the Cricket API.

Three kinds of message (alerts, recovery notices and one daily summary) and three housekeeping kinds, sent through Resend, with a persistent ledger that bounds duplicates, quota use and storage.
See README.md for the behaviour and its limits.
"""
__version__ = "0.1.0"
