"""
Service layer for Apify Advanced Search integration.

- trigger_apify_run(search, user, triggered_by, workspace) — POSTs to Apify actor
- fetch_and_import_leads(run) — GETs dataset items and creates Contact records
"""
import base64
import json
import logging

import requests
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

ACTOR_ID = 'T1XDXWc1L92AfIJtd'

# Normalize old compact format → Apify's required spaced format
_EMPLOYEE_SIZE_MAP = {
    '0-1':       '0 - 1',
    '2-10':      '2 - 10',
    '11-50':     '11 - 50',
    '51-200':    '51 - 200',
    '201-500':   '201 - 500',
    '501-1000':  '501 - 1000',
    '1001-5000': '1001 - 5000',
    '5001-10000':'5001 - 10000',
}


def _normalize_filters(filters):
    """Fix any stored filter values that don't match Apify's expected format."""
    f = dict(filters)
    if f.get('companyEmployeeSize'):
        f['companyEmployeeSize'] = [
            _EMPLOYEE_SIZE_MAP.get(v, v) for v in f['companyEmployeeSize']
        ]
    if f.get('industry'):
        existing_kw = f.get('industryKeywords') or []
        merged = existing_kw + [v for v in f.pop('industry') if v not in existing_kw]
        f['industryKeywords'] = merged
    return f


def _encode_webhooks(webhooks):
    return base64.b64encode(json.dumps(webhooks).encode()).decode()


def trigger_apify_run(search, user, triggered_by, workspace):
    import uuid
    from crm.models import ApifyRun

    token          = getattr(settings, 'APIFY_API_TOKEN', '')
    webhook_secret = getattr(settings, 'APIFY_WEBHOOK_SECRET', '')
    site_url       = getattr(settings, 'SITE_URL', '').rstrip('/')

    webhook_url = f"{site_url}/apify/webhook/"
    if webhook_secret:
        webhook_url += f"?secret={webhook_secret}"

    ad_hoc_webhooks = [
        {
            "eventTypes": [
                "ACTOR.RUN.SUCCEEDED",
                "ACTOR.RUN.FAILED",
                "ACTOR.RUN.ABORTED",
            ],
            "requestUrl": webhook_url,
        }
    ]

    try:
        resp = requests.post(
            f'https://api.apify.com/v2/acts/{ACTOR_ID}/runs',
            json=_normalize_filters(search.filters),
            headers={
                'Authorization': f'Bearer {token}',
                'Content-Type': 'application/json',
            },
            params={'webhooks': _encode_webhooks(ad_hoc_webhooks)},
            timeout=30,
        )
        if not resp.ok:
            raise Exception(f"Apify {resp.status_code}: {resp.text[:500]}")
        data = resp.json().get('data', {})
        run_obj = ApifyRun.objects.create(
            search=search,
            user=user,
            workspace=workspace,
            apify_run_id=data['id'],
            status='RUNNING',
            triggered_by=triggered_by,
        )
    except Exception as exc:
        run_obj = ApifyRun.objects.create(
            search=search,
            user=user,
            workspace=workspace,
            apify_run_id=f'failed-{uuid.uuid4().hex[:12]}',
            status='FAILED',
            triggered_by=triggered_by,
            error_message=str(exc),
            completed_at=timezone.now(),
        )
        raise

    return run_obj
