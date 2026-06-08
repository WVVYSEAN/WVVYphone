"""
Drip campaign engine + training data pipeline.

Public API
----------
run_drip_cycle(workspace)               — hourly scheduler; finds eligible cold leads.
generate_drip_for_contact(contact, ws)  — on-demand generation for one contact.
send_drip_email(drip_email)             — sends a DripEmail record via Resend.
compute_outcome_score(example) -> float — compute quality score; does NOT save.
update_outcome_scores_for_contact(contact) — mark reply + recompute scores.
"""

import logging
import os
import uuid

log = logging.getLogger(__name__)

INBOUND_DOMAIN = os.environ.get('REPLY_TO_DOMAIN', '')

# ── Helpers ────────────────────────────────────────────────────────────────────

def _first_name(contact):
    return (contact.name or '').split(' ', 1)[0].strip() or contact.name or 'there'


def _get_few_shot_examples(workspace):
    """Return up to 3 most recent *human-edited* examples for few-shot prompting."""
    from .models import DripEditExample
    # Fetch a batch and filter to ones where the user actually edited the body
    candidates = DripEditExample.objects.filter(
        workspace=workspace,
    ).order_by('-created_at')[:30]
    edited = [e for e in candidates if e.original_body != e.edited_body]
    return edited[:3]


def _log_activity(contact, summary, notes=''):
    """Create a TouchPoint activity record on a Contact."""
    from datetime import date
    from django.contrib.contenttypes.models import ContentType
    from .models import TouchPoint

    ct = ContentType.objects.get_for_model(contact.__class__)
    TouchPoint.objects.create(
        content_type=ct,
        object_id=contact.pk,
        touchpoint_type='other',
        date=date.today(),
        summary=summary,
        notes=notes,
        logged_by='WVVYphone (Drip)',
    )


# ── AI generation ──────────────────────────────────────────────────────────────

def _build_contact_history_block(contact):
    """Return a formatted string of notes + recent touchpoints, or '' if nothing to show."""
    from .models import TouchPoint
    from django.contrib.contenttypes.models import ContentType

    parts = []

    if contact.notes and contact.notes.strip():
        parts.append(f'Notes: {contact.notes.strip()}')

    ct = ContentType.objects.get_for_model(contact.__class__)
    touchpoints = (
        TouchPoint.objects
        .filter(content_type=ct, object_id=contact.pk)
        .order_by('-date', '-created_at')[:8]
    )
    if touchpoints:
        tp_lines = []
        for tp in touchpoints:
            label = tp.get_touchpoint_type_display()
            date_str = str(tp.date) if tp.date else '?'
            outcome_str = f' | Outcome: {tp.outcome}' if tp.outcome else ''
            summary_str = f' | "{tp.summary.strip()}"' if tp.summary and tp.summary.strip() else ''
            notes_str   = f' — {tp.notes.strip()}' if tp.notes and tp.notes.strip() else ''
            tp_lines.append(f'- {date_str} | {label}{outcome_str}{summary_str}{notes_str}')
        parts.append('Recent interactions:\n' + '\n'.join(tp_lines))

    if not parts:
        return ''
    return '\n\n## Contact History\n' + '\n\n'.join(parts)


def _build_prompts(contact, sequence_number, cfg, workspace, sender_name, sender_first,
                   booking_url, few_shot_examples):
    """
    Build system_prompt and user_prompt strings.
    Shared between Claude and OpenAI paths so training data is model-agnostic.
    """
    first_name = _first_name(contact)

    context_parts = []
    if contact.role:
        context_parts.append(f'Title: {contact.role}')
    if contact.company:
        context_parts.append(f'Company: {contact.company}')
    if contact.industry:
        context_parts.append(f'Industry: {contact.industry}')
    if contact.location:
        context_parts.append(f'Location: {contact.location}')
    context_block = '\n'.join(context_parts) if context_parts else 'No additional context.'

    contact_history_block = _build_contact_history_block(contact)

    # Initial email template as the style anchor
    initial_email_block = ''
    if cfg.outreach_body and cfg.outreach_body.strip():
        initial_email_block = (
            f'\n\n## Initial Email (for style and tone — do NOT repeat it)\n'
            f'Subject: {cfg.outreach_subject.strip()}\n\n'
            f'{cfg.outreach_body.strip()}'
            if cfg.outreach_subject and cfg.outreach_subject.strip()
            else f'\n\n## Initial Email (for style and tone — do NOT repeat it)\n{cfg.outreach_body.strip()}'
        )

    few_shot_block = ''
    if few_shot_examples:
        parts = []
        for ex in few_shot_examples:
            parts.append(
                f'ORIGINAL:\n{ex.original_body.strip()}\n\nEDITED (use this style):\n{ex.edited_body.strip()}'
            )
        few_shot_block = (
            '\n\n## Style Examples (previous human edits — match this tone)\n'
            + '\n\n---\n\n'.join(parts)
        )

    system_prompt = (
        f"You are a friendly email assistant writing a follow-up outreach email on behalf of {sender_name}.\n\n"
        f"## Your One Job\n"
        f"Get the prospect to book a short call. Every email must end with the calendar link "
        f"(or ask for their availability if none is provided). "
        f"Keep the same voice and energy as the initial email — this is a natural continuation, not a new pitch.\n\n"
        f"## Tone & Length\n"
        f"- Match the length and style of the initial email exactly\n"
        f"- Warm, direct, and human — 3 to 5 sentences maximum\n"
        f"- Reference or riff on the same angle as the initial email; do not introduce new ideas\n"
        f"- Never use salesy or pushy language\n"
        f"- Do not start with 'Hi {first_name}' every time — vary the opener\n\n"
        f"## What NOT to Do\n"
        f"- Do not repeat or paraphrase the initial email verbatim\n"
        f"- Do not use filler openers like 'Just following up' or 'Circling back'\n"
        f"- Never use em dashes. Use a comma or period instead.\n"
        f"- Never use placeholder text, brackets, or templated phrases\n"
        f"- Do not start with 'I'\n\n"
        f"## Recipient\n"
        f"Name: {contact.name}\n"
        f"{context_block}"
        f"{contact_history_block}\n\n"
        f"## Email Details\n"
        f"This is follow-up email #{sequence_number}.\n"
        f"{f'Calendar link: {booking_url}' if booking_url else 'No calendar link configured — ask them to share their availability instead.'}\n"
        f"Sign off as '{sender_first}' only — no last name, no title."
        f"{initial_email_block}"
        f"{few_shot_block}"
    )

    user_prompt = f'Write a short follow-up email to {first_name}.'
    return system_prompt, user_prompt


def _generate_drip_email(contact, sequence_number, cfg, workspace):
    """
    Call the AI to produce a drip email.

    Returns a dict:
    {
      "subject": str,
      "body": str,
      "call_log": AICallLog,
      "full_system_prompt": str,
      "full_user_prompt": str,
      "ai_raw_response": str,
      "model_used": str,
    }

    Raises RuntimeError on unrecoverable failure.
    """
    from .models import AICallLog, UserProfile

    # Sender identity
    owner_profile = UserProfile.get_for_user(workspace.owner)
    from_addr = cfg.resend_from_email or owner_profile.from_email or ''

    import re as _re
    name_match  = _re.match(r'^([^<]+)<', from_addr)
    sender_name = name_match.group(1).strip() if name_match else (from_addr.split('@')[0] if from_addr else 'Our Team')
    sender_first = sender_name.split()[0] if sender_name else 'Me'

    booking_url      = cfg.calendar_booking_url or ''
    few_shot_examples = _get_few_shot_examples(workspace)

    system_prompt, user_prompt = _build_prompts(
        contact, sequence_number, cfg, workspace,
        sender_name, sender_first, booking_url, few_shot_examples,
    )

    messages = [{'role': 'user', 'content': user_prompt}]

    # ── Decide which model to call ─────────────────────────────────────────────
    use_openai = bool(cfg.drip_model_id)

    if use_openai:
        result = _call_openai(
            contact, cfg, system_prompt, user_prompt, messages,
            sender_first, AICallLog,
        )
        if result is None:
            # Fell back to Claude
            use_openai = False

    if not use_openai:
        result = _call_claude(
            contact, cfg, system_prompt, messages, sender_first, AICallLog,
        )

    return {
        'subject':           result['subject'],
        'body':              result['body'],
        'call_log':          result['call_log'],
        'full_system_prompt': system_prompt,
        'full_user_prompt':  user_prompt,
        'ai_raw_response':   result['raw_text'],
        'model_used':        result['model_used'],
    }


def _call_claude(contact, cfg, system_prompt, messages, sender_first, AICallLog):
    """Call Anthropic Claude. Returns dict with subject, body, call_log, raw_text, model_used."""
    import anthropic

    api_key = os.environ.get('ANTHROPIC_API_KEY', '')
    if not api_key:
        raise RuntimeError('ANTHROPIC_API_KEY not set')

    model_id = 'claude-sonnet-4-6'

    try:
        client = anthropic.Anthropic(api_key=api_key)
        ai_response = client.messages.create(
            model=model_id,
            max_tokens=512,
            system=system_prompt,
            messages=messages,
        )
        full_text     = ai_response.content[0].text.strip().replace('\u2014', ',').replace('\u2013', '-')
        input_tokens  = ai_response.usage.input_tokens
        output_tokens = ai_response.usage.output_tokens
    except Exception as e:
        AICallLog.objects.create(
            contact=contact,
            prompt=str(messages),
            response=f'DRIP CLAUDE ERROR: {e}',
            flagged=True,
        )
        raise RuntimeError(f'Claude API error: {e}') from e

    subject, body = _parse_subject_body(full_text, sender_first)
    _check_safeguards(body, contact, messages, full_text, AICallLog)

    call_log = AICallLog.objects.create(
        contact=contact,
        prompt=str(messages),
        response=full_text,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        flagged=False,
        status='pending',
        draft_subject=subject,
        draft_is_followup=False,
    )
    return {'subject': subject, 'body': body, 'call_log': call_log,
            'raw_text': full_text, 'model_used': model_id}


def _call_openai(contact, cfg, system_prompt, user_prompt, messages, sender_first, AICallLog):
    """
    Call OpenAI with the fine-tuned model. Returns dict or None (signals fallback to Claude).
    Never raises — failures log a warning and return None.
    """
    try:
        from openai import OpenAI
    except ImportError:
        log.warning(
            'openai package not installed; drip_model_id=%s set but falling back to Claude.',
            cfg.drip_model_id,
        )
        return None

    api_key = os.environ.get('OPENAI_API_KEY', '')
    if not api_key:
        log.warning(
            'drip_model_id=%s set but OPENAI_API_KEY not configured; falling back to Claude.',
            cfg.drip_model_id,
        )
        return None

    try:
        client = OpenAI(api_key=api_key)
        response = client.chat.completions.create(
            model=cfg.drip_model_id,
            messages=[
                {'role': 'system', 'content': system_prompt},
                {'role': 'user',   'content': user_prompt},
            ],
            max_tokens=1000,
        )
        full_text = (response.choices[0].message.content or '').strip()
        full_text = full_text.replace('\u2014', ',').replace('\u2013', '-')
    except Exception as e:
        log.warning('OpenAI drip call failed for contact %s: %s — falling back to Claude.', contact.pk, e)
        AICallLog.objects.create(
            contact=contact,
            prompt=str(messages),
            response=f'DRIP OPENAI ERROR (fallback to Claude): {e}',
            flagged=True,
        )
        return None

    subject, body = _parse_subject_body(full_text, '')

    try:
        _check_safeguards(body, contact, messages, full_text, AICallLog)
    except RuntimeError:
        return None

    call_log = AICallLog.objects.create(
        contact=contact,
        prompt=str(messages),
        response=full_text,
        flagged=False,
        status='pending',
        draft_subject=subject,
        draft_is_followup=False,
    )
    return {'subject': subject, 'body': body, 'call_log': call_log,
            'raw_text': full_text, 'model_used': cfg.drip_model_id}


def _parse_subject_body(full_text, sender_first):
    subject = f'Quick note — {sender_first}' if sender_first else 'Quick note'
    body    = full_text
    if full_text.lower().startswith('subject:'):
        lines = full_text.split('\n', 1)
        subject = lines[0][len('subject:'):].strip()
        body    = lines[1].strip() if len(lines) > 1 else full_text
    return subject, body


def _check_safeguards(body, contact, messages, full_text, AICallLog):
    """Raise RuntimeError if the body fails safeguard checks."""
    BAD_PATTERNS = ['[your', '{{', '}}', '[name', '[insert', '[contact', '[company', '[no reply']
    is_bad = not body or len(body) < 10 or any(p in body.lower() for p in BAD_PATTERNS)
    if is_bad:
        AICallLog.objects.create(
            contact=contact, prompt=str(messages), response=full_text, flagged=True,
        )
        raise RuntimeError(f'AI output failed safeguard check for contact {contact.pk}')


# ── Outcome scoring ────────────────────────────────────────────────────────────

def compute_outcome_score(drip_edit_example):
    """
    Compute a quality float for a DripEditExample.
    Returns the score; does NOT call .save().

    Scoring:
      reply_received=True,  was_edited=False → 1.0
      reply_received=True,  was_edited=True  → 0.8
      reply_received=False, was_edited=False → 0.5
      reply_received=False, was_edited=True  → 0.2
    """
    was_edited = (
        bool(drip_edit_example.edited_body) and
        drip_edit_example.original_body != drip_edit_example.edited_body
    )
    if drip_edit_example.reply_received:
        return 1.0 if not was_edited else 0.8
    else:
        return 0.5 if not was_edited else 0.2


def update_outcome_scores_for_contact(contact):
    """
    When a contact replies, mark reply_received + recompute outcome scores on
    all DripEditExamples for this contact that haven't been exported yet.
    """
    from django.utils import timezone
    from .models import DripEditExample, DripEmail

    if not contact.drip_sequence_stopped:
        return

    now = timezone.now()

    # Approximate reply time from most recent inbound email thread entry
    reply_time = None
    try:
        last_inbound = contact.email_thread.filter(direction='inbound').order_by('-sent_at').first()
        if last_inbound:
            reply_time = last_inbound.sent_at
    except Exception:
        pass
    if reply_time is None:
        reply_time = now

    # Find all unexamined examples for this contact
    examples = DripEditExample.objects.filter(
        contact=contact,
        reply_received=False,
        exported_at__isnull=True,
    ).select_related('drip_email')

    for ex in examples:
        # Only mark reply if the drip email was actually sent before the reply arrived
        if ex.drip_email and ex.drip_email.sent_at and ex.drip_email.sent_at < reply_time:
            ex.reply_received    = True
            ex.reply_received_at = reply_time
        # Always recompute score regardless of whether reply is flagged
        score = compute_outcome_score(ex)
        ex.outcome_score  = score
        ex.is_high_quality = score >= 0.8
        ex.save(update_fields=[
            'reply_received', 'reply_received_at',
            'outcome_score', 'is_high_quality',
        ])


# ── Sending ────────────────────────────────────────────────────────────────────

def send_drip_email(drip_email):
    """
    Send a DripEmail via Resend and update all tracking state.
    Raises RuntimeError on send failure.
    """
    import resend as resend_sdk
    from django.utils import timezone
    from .models import HeatSettings, EmailThread, UserProfile

    contact   = drip_email.contact
    workspace = contact.workspace
    cfg       = HeatSettings.get_for_workspace(workspace)

    if not cfg.resend_api_key:
        raise RuntimeError('No Resend API key configured')
    if not contact.email:
        raise RuntimeError(f'Contact {contact.pk} has no email address')

    owner_profile = UserProfile.get_for_user(workspace.owner)
    from_addr = cfg.resend_from_email or owner_profile.from_email or 'noreply@wvvy.pro'

    new_message_id = f'<{uuid.uuid4()}@wvvy.pro>'

    reply_domain = cfg.reply_to_domain or INBOUND_DOMAIN
    reply_to_tag = f'reply+{contact.pk}@{reply_domain}' if reply_domain else from_addr

    resend_sdk.api_key = cfg.resend_api_key

    import base64 as _b64
    from .models import OutreachAttachment
    att_qs = OutreachAttachment.objects.filter(workspace=workspace)
    attachments = [
        {'filename': a.filename,
         'content':  _b64.b64encode(bytes(a.file_data)).decode('ascii')}
        for a in att_qs
    ]

    drip_payload = {
        'from':     from_addr,
        'to':       [contact.email],
        'subject':  drip_email.subject,
        'text':     drip_email.body,
        'reply_to': reply_to_tag,
        'headers':  {'Message-ID': new_message_id},
    }
    if attachments:
        drip_payload['attachments'] = attachments

    try:
        resend_sdk.Emails.send(drip_payload)
    except Exception as e:
        raise RuntimeError(f'Resend error: {e}') from e

    now = timezone.now()

    drip_email.status  = 'sent'
    drip_email.sent_at = now
    drip_email.save(update_fields=['status', 'sent_at'])

    if drip_email.ai_call_log_id:
        try:
            log_obj = drip_email.ai_call_log
            log_obj.status = 'approved'
            log_obj.save(update_fields=['status'])
        except Exception:
            pass

    contact.drip_followups_sent = (contact.drip_followups_sent or 0) + 1
    contact.last_message_id     = new_message_id
    contact.save(update_fields=['drip_followups_sent', 'last_message_id'])

    EmailThread.objects.create(
        contact=contact,
        message_id=new_message_id,
        direction='outbound',
        subject=drip_email.subject,
        body=drip_email.body,
    )

    _log_activity(
        contact,
        f'Drip email #{drip_email.sequence_number} sent: {drip_email.subject}',
    )


# ── Training data record helpers ───────────────────────────────────────────────

def _create_training_record(workspace, contact, drip, result):
    """
    Create a DripEditExample training record tied to a DripEmail.
    Called immediately after DripEmail is created.
    """
    from .models import DripEditExample
    DripEditExample.objects.create(
        workspace=workspace,
        contact=contact,
        drip_email=drip,
        original_body=result['body'],
        edited_body=result['body'],          # same as original until user edits
        full_system_prompt=result['full_system_prompt'],
        full_user_prompt=result['full_user_prompt'],
        ai_raw_response=result['ai_raw_response'],
        model_used=result['model_used'],
        sequence_number=drip.sequence_number,
        contact_industry=contact.industry or '',
    )


# ── On-demand generation ───────────────────────────────────────────────────────

def generate_drip_for_contact(contact, workspace):
    """
    Generate a drip email for a single contact and create a pending DripEmail.
    Returns the DripEmail instance.
    Raises RuntimeError if generation fails.
    """
    from .models import HeatSettings, DripEmail

    cfg = HeatSettings.get_for_workspace(workspace)
    seq = (contact.drip_followups_sent or 0) + 1

    result = _generate_drip_email(contact, seq, cfg, workspace)

    drip = DripEmail.objects.create(
        contact=contact,
        sequence_number=seq,
        subject=result['subject'],
        body=result['body'],
        status='pending',
        ai_call_log=result['call_log'],
    )

    _create_training_record(workspace, contact, drip, result)
    _log_activity(contact, f'Drip email #{seq} draft generated by AI')
    return drip


# ── Scheduled cycle ────────────────────────────────────────────────────────────

def run_drip_cycle(workspace):
    """
    Find all eligible cold leads in the workspace and generate/send drip emails.
    """
    from datetime import timedelta
    from django.utils import timezone
    from .models import Contact, HeatSettings, DripEmail

    cfg = HeatSettings.get_for_workspace(workspace)
    if not cfg.resend_api_key:
        log.info('Drip cycle skipped — no Resend API key for workspace %s', workspace.pk)
        return

    now = timezone.now()

    candidates = (
        Contact.objects
        .filter(workspace=workspace, stage='cold_lead',
                drip_sequence_stopped=False, drip_paused=False)
        .exclude(email='')
    )

    sent_count = 0
    queued_count = 0

    for contact in candidates:
        if contact.drip_followups_sent >= cfg.drip_max_followups:
            contact.drip_sequence_stopped = True
            contact.save(update_fields=['drip_sequence_stopped'])
            continue

        if contact.drip_emails.filter(status='pending').exists():
            continue

        last_sent = (
            contact.drip_emails
            .filter(status='sent')
            .order_by('-sent_at')
            .first()
        )
        if last_sent and last_sent.sent_at:
            elapsed = (now - last_sent.sent_at).days
            if elapsed < cfg.drip_interval_days:
                continue

        seq = (contact.drip_followups_sent or 0) + 1

        try:
            result = _generate_drip_email(contact, seq, cfg, workspace)
        except Exception as exc:
            log.warning('Drip generation failed for contact %s: %s', contact.pk, exc)
            continue

        drip = DripEmail.objects.create(
            contact=contact,
            sequence_number=seq,
            subject=result['subject'],
            body=result['body'],
            status='pending',
            ai_call_log=result['call_log'],
        )

        _create_training_record(workspace, contact, drip, result)

        if cfg.ai_review_mode:
            _log_activity(contact, f'Drip email #{seq} queued for review')
            queued_count += 1
        else:
            try:
                send_drip_email(drip)
                sent_count += 1
            except Exception as exc:
                log.warning('Drip send failed for contact %s: %s', contact.pk, exc)

    log.info(
        'Drip cycle complete for workspace %s — sent=%d queued=%d',
        workspace.pk, sent_count, queued_count,
    )
