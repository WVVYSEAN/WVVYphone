"""
ZeroBounce email validation integration.

Used during Apify lead import to verify emails before storing/sending outreach.
"""
import logging

import requests
from django.conf import settings

logger = logging.getLogger(__name__)

BATCH_URL = 'https://api.zerobounce.net/v2/validatebatch'
SINGLE_URL = 'https://api.zerobounce.net/v2/validate'

# Statuses where we keep the email on the contact record
KEEP_EMAIL_STATUSES = frozenset({'valid', 'catch-all'})

# Statuses eligible for automated outreach
OUTREACH_EMAIL_STATUSES = frozenset({'valid'})


def _api_key():
    return getattr(settings, 'ZEROBOUNCE_API_KEY', '')


def is_configured():
    return bool(_api_key())


def keep_email(status):
    return (status or '').lower() in KEEP_EMAIL_STATUSES


def outreach_allowed(status):
    return (status or '').lower() in OUTREACH_EMAIL_STATUSES


def validate_batch(emails):
    """
    Validate up to 200 emails via ZeroBounce batch API.
    Returns {email_lower: {status, sub_status, ...}}.
    On API failure, returns empty dict (caller should fall back to single or skip).
    """
    key = _api_key()
    if not key or not emails:
        return {}

    unique = list(dict.fromkeys(e.strip().lower() for e in emails if e and e.strip()))
    if not unique:
        return {}

    try:
        resp = requests.post(
            BATCH_URL,
            json={
                'api_key':     key,
                'email_batch': [{'email_address': e} for e in unique[:200]],
            },
            timeout=120,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:
        logger.error('ZeroBounce batch validation failed: %s', exc)
        return {}

    if data.get('errors'):
        logger.warning('ZeroBounce batch errors: %s', data['errors'][:3])

    out = {}
    for row in data.get('email_batch') or []:
        addr = (row.get('address') or '').strip().lower()
        if addr:
            out[addr] = row
    return out


def validate_email(email):
    """Validate a single email. Returns result dict or None on failure."""
    key = _api_key()
    addr = (email or '').strip().lower()
    if not key or not addr:
        return None

    try:
        resp = requests.get(
            SINGLE_URL,
            params={'api_key': key, 'email': addr},
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error('ZeroBounce validate failed for %s: %s', addr, exc)
        return None


def apply_validation(email, zb_result):
    """
    Given an email and ZeroBounce result, return (email_to_store, email_status, send_outreach).
    """
    if not email:
        return '', '', False

    if not zb_result:
        # No validation available — keep email but don't auto-send outreach
        return email, 'unverified', False

    status = (zb_result.get('status') or 'unknown').lower()
    sub    = (zb_result.get('sub_status') or '').strip()
    label  = f"{status}:{sub}" if sub else status

    if keep_email(status):
        return email, label, outreach_allowed(status)
    return '', label, False
