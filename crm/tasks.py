"""
Background tasks for long-running operations.

Tasks:
  run_apify_import(run_pk, job_pk)  — Phase 1: import leads from Apify + ZeroBounce clean
                                       Phase 2: send outreach emails
  run_backup_outreach_task(...)      — Re-send to missed contacts
"""
import logging
import re

import requests
from celery import shared_task
from django.conf import settings
from django.utils import timezone

logger = logging.getLogger(__name__)

_PROGRESS_INTERVAL = 50


def _parse_item_to_contact_kwargs(item, workspace, email_override=None, email_status_override=None):
    """Convert a raw Apify dataset item dict to Contact.objects.create kwargs."""
    first   = (item.get('firstName') or '').strip()
    last    = (item.get('lastName')  or '').strip()
    name    = (f"{first} {last}".strip()
               or (item.get('fullName') or '').strip()
               or (item.get('name')     or '').strip())
    email   = email_override if email_override is not None else (item.get('email') or '').strip().lower()
    company = (item.get('organizationName') or item.get('companyName') or '').strip()

    location = ', '.join(filter(None, [
        (item.get('city')    or '').strip(),
        (item.get('state')   or '').strip(),
        (item.get('country') or '').strip(),
    ]))

    _phone = item.get('phone_numbers') or item.get('phone') or ''
    if isinstance(_phone, list):
        _first = _phone[0] if _phone else ''
        if isinstance(_first, dict):
            _phone = _first.get('sanitizedNumber') or _first.get('number') or ''
        else:
            _phone = _first
    if not isinstance(_phone, str):
        _phone = ''

    _website = (item.get('organizationWebsite') or item.get('companyWebsite') or '').strip()
    if _website:
        _website = re.sub(r'^https?://', '', _website).rstrip('/').split('/')[0]

    _connections = item.get('connections') or item.get('connectionCount') or ''
    if isinstance(_connections, int):
        _connections = str(_connections)

    _notes_parts = []
    _summary = (item.get('summary') or item.get('headline') or item.get('about') or '').strip()
    _org_desc = (item.get('organizationDescription') or item.get('companyDescription') or '').strip()
    if _summary:
        _notes_parts.append(f'**About {(first or name).split()[0]}:** {_summary}')
    if _org_desc:
        _org_name = company or 'the company'
        _notes_parts.append(f'**About {_org_name}:** {_org_desc}')

    email_status = email_status_override or ''

    return dict(
        workspace        = workspace,
        name             = name[:200],
        email            = email[:254],
        phone            = _phone.strip()[:50],
        role             = (item.get('position') or item.get('title') or '').strip()[:200],
        company          = company[:200],
        company_domain   = _website[:200],
        linkedin         = (item.get('linkedinUrl') or '').strip()[:200],
        location         = location[:200],
        industry         = (item.get('organizationIndustry') or item.get('industry') or '').strip()[:200],
        company_size     = (item.get('organizationSize') or item.get('organizationHeadcount') or item.get('companySize') or '').strip()[:100],
        org_type         = (item.get('organizationType') or item.get('companyType') or '').strip()[:200],
        org_founded_year = str(item.get('organizationFoundedYear') or item.get('organizationFounded') or '').strip()[:20],
        org_revenue      = (item.get('organizationRevenue') or item.get('companyRevenue') or '').strip()[:200],
        connections      = _connections[:50],
        email_status     = email_status[:100],
        notes            = '\n\n'.join(_notes_parts),
        source           = 'apify_advanced_search',
        stage            = 'cold_lead',
    ), name, email, company


def _fetch_dataset_pages(dataset_id, token):
    """Generator: yield lists of items from the Apify dataset, page by page."""
    offset = 0
    limit  = 1000
    while True:
        try:
            resp = requests.get(
                f'https://api.apify.com/v2/datasets/{dataset_id}/items',
                params={'format': 'json', 'clean': 'true',
                        'offset': offset, 'limit': limit},
                headers={'Authorization': f'Bearer {token}'},
                timeout=60,
            )
            resp.raise_for_status()
            items = resp.json()
        except Exception as exc:
            logger.error('Error fetching Apify dataset %s at offset %s: %s',
                         dataset_id, offset, exc)
            return

        if not items:
            return

        yield items

        if len(items) < limit:
            return
        offset += limit


def _validate_page_emails(items):
    """Batch-validate all emails in a dataset page. Returns {email: zb_result}."""
    from crm.services.zerobounce import is_configured, validate_batch, validate_email

    if not is_configured():
        return {}

    emails = [
        (item.get('email') or '').strip().lower()
        for item in items
        if (item.get('email') or '').strip()
    ]
    if not emails:
        return {}

    results = validate_batch(emails)
    if results:
        return results

    # Batch failed — fall back to single validation
    out = {}
    for addr in dict.fromkeys(emails):
        row = validate_email(addr)
        if row:
            out[addr] = row
    return out


@shared_task(bind=True, max_retries=0, ignore_result=True)
def run_apify_import(self, run_pk, job_pk):
    """
    Phase 1 — Fetch Apify dataset items, validate emails via ZeroBounce, create Contacts.
    Phase 2 — Send outreach emails to newly imported contacts with valid emails.
    """
    from crm.models import ApifyRun, Contact, TaskJob, HeatSettings
    from crm.services.zerobounce import apply_validation, is_configured

    job = TaskJob.objects.get(pk=job_pk)
    run = ApifyRun.objects.get(pk=run_pk)
    workspace = run.workspace
    user      = run.user

    try:
        TaskJob.objects.filter(pk=job_pk).update(status='running', phase='importing')

        token      = getattr(settings, 'APIFY_API_TOKEN', '')
        dataset_id = run.apify_dataset_id

        existing_emails = set(
            Contact.objects
            .filter(workspace=workspace, email__gt='')
            .values_list('email', flat=True)
        )

        new_contacts = []
        imported     = 0

        for page in _fetch_dataset_pages(dataset_id, token):
            zb_by_email = _validate_page_emails(page) if is_configured() else {}

            for item in page:
                raw_email = (item.get('email') or '').strip().lower()
                kwargs, name, email, company = _parse_item_to_contact_kwargs(item, workspace)

                if not name:
                    continue
                if not kwargs.get('phone'):
                    continue

                send_outreach = False
                if raw_email:
                    stored_email, email_status, send_outreach = apply_validation(
                        raw_email, zb_by_email.get(raw_email),
                    )
                    kwargs['email']        = stored_email
                    kwargs['email_status'] = email_status
                    email = stored_email

                if email and email in existing_emails:
                    continue
                if not email and Contact.objects.filter(
                    workspace=workspace, name=name, company=company
                ).exists():
                    continue

                contact = Contact.objects.create(**kwargs)
                if email:
                    existing_emails.add(email)
                if send_outreach and email:
                    new_contacts.append(contact)
                imported += 1

                if imported % _PROGRESS_INTERVAL == 0:
                    TaskJob.objects.filter(pk=job_pk).update(leads_imported=imported)

        TaskJob.objects.filter(pk=job_pk).update(
            leads_imported=imported,
            leads_total=imported,
            emails_total=len(new_contacts),
            phase='emailing',
        )
        run.leads_imported = imported
        run.save(update_fields=['leads_imported'])

        from crm.views import _maybe_send_outreach

        cfg  = HeatSettings.get_for_workspace(workspace)
        sent = skipped = 0

        for contact in new_contacts:
            try:
                ok, _ = _maybe_send_outreach(contact, workspace, user, cfg)
            except Exception as exc:
                logger.warning('Outreach failed for contact %s: %s', contact.pk, exc)
                ok = False
            if ok:
                sent += 1
            else:
                skipped += 1

            if (sent + skipped) % _PROGRESS_INTERVAL == 0:
                TaskJob.objects.filter(pk=job_pk).update(
                    emails_sent=sent, emails_skipped=skipped,
                )

        now = timezone.now()
        TaskJob.objects.filter(pk=job_pk).update(
            emails_sent=sent,
            emails_skipped=skipped,
            status='succeeded',
            phase='',
            completed_at=now,
        )
        run.status       = 'SUCCEEDED'
        run.completed_at = now
        run.save(update_fields=['status', 'completed_at'])

    except Exception as exc:
        logger.exception('run_apify_import failed for run %s', run_pk)
        now = timezone.now()
        TaskJob.objects.filter(pk=job_pk).update(
            status='failed',
            error_message=str(exc),
            completed_at=now,
        )
        run.status        = 'FAILED'
        run.error_message = str(exc)
        run.completed_at  = now
        run.save(update_fields=['status', 'error_message', 'completed_at'])
        raise


@shared_task(bind=True, max_retries=0, ignore_result=True)
def run_backup_outreach_task(self, workspace_pk, user_pk, job_pk):
    """Send outreach to contacts from the last Apify import that were missed."""
    from crm.models import TaskJob, Contact, EmailThread, HeatSettings, ApifyRun
    from crm.views import _maybe_send_outreach
    from django.contrib.auth import get_user_model

    User      = get_user_model()
    job       = TaskJob.objects.get(pk=job_pk)
    workspace = job.workspace
    user      = User.objects.get(pk=user_pk)

    try:
        TaskJob.objects.filter(pk=job_pk).update(status='running', phase='emailing')

        last_run = (
            ApifyRun.objects
            .filter(workspace=workspace, status='SUCCEEDED')
            .order_by('-started_at')
            .first()
        )
        if not last_run:
            TaskJob.objects.filter(pk=job_pk).update(
                status='failed',
                error_message='No completed Advanced Search found.',
                completed_at=timezone.now(),
            )
            return

        emailed_ids = set(
            EmailThread.objects
            .filter(contact__workspace=workspace, direction='outbound')
            .values_list('contact_id', flat=True)
        )
        candidates = list(
            Contact.objects.filter(
                workspace=workspace,
                source='apify_advanced_search',
                stage='cold_lead',
                email__gt='',
                created_at__gte=last_run.started_at,
            ).exclude(pk__in=emailed_ids)
        )
        TaskJob.objects.filter(pk=job_pk).update(emails_total=len(candidates))

        cfg  = HeatSettings.get_for_workspace(workspace)
        sent = skipped = 0

        for contact in candidates:
            try:
                ok, _ = _maybe_send_outreach(contact, workspace, user, cfg)
            except Exception as exc:
                logger.warning('Outreach failed for contact %s: %s', contact.pk, exc)
                ok = False
            if ok:
                sent += 1
            else:
                skipped += 1

            if (sent + skipped) % _PROGRESS_INTERVAL == 0:
                TaskJob.objects.filter(pk=job_pk).update(
                    emails_sent=sent, emails_skipped=skipped,
                )

        TaskJob.objects.filter(pk=job_pk).update(
            emails_sent=sent,
            emails_skipped=skipped,
            status='succeeded',
            phase='',
            completed_at=timezone.now(),
        )

    except Exception as exc:
        logger.exception('run_backup_outreach_task failed for job %s', job_pk)
        TaskJob.objects.filter(pk=job_pk).update(
            status='failed',
            error_message=str(exc),
            completed_at=timezone.now(),
        )
        raise
