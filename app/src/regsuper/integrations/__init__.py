"""File-format adapters for the SME onboarding funnel.

Holds the accounting-export importers (POHODA, Money S3) that pre-fill a business profile from a
customer's accounting file. They are file-only and stdlib-only: they parse already-uploaded XML
bytes and make no network calls. Not implemented yet: the callables in `accounting.py` raise
NotImplementedError, no router is mounted, and `accounting_integration_enabled` stays off.
"""

from __future__ import annotations
